# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Radar Judol v3 — orkestrator. Tiap collector terisolasi: satu crash ≠ semua mati."""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback

from . import comics, crt, pipeline, telegram
from .core import (
    DAY, LEGACY_STATE, Config, ConfigError, Ctx, Deadline, Http, Store, CollectorResult,
    ekstrak_domain_aman, load_config, load_known, log, now, score_domain,
)


def migrate_legacy(tg: Store, cm: Store, ts: float, T: dict) -> None:
    """Impor state lama (.github/judol_radar_state.json) sekali saja."""
    if tg.existed or cm.existed or not LEGACY_STATE.exists():
        return
    try:
        old = json.loads(LEGACY_STATE.read_text(encoding="utf-8"))
    except Exception as e:
        log("MIGRASI", f"state lama tak terbaca ({e}); mulai bersih")
        return
    for ch, m in (old.get("channels") or {}).items():
        tg.data["channels"][ch] = telegram.new_meta(m.get("source", "legacy"), m.get("kind", "judol")) | {
            "scans": m.get("scans", 0), "harvested": m.get("harvested", 0)}
    # yang dulu "ditolak" dicek ulang dalam ±3 hari (banyak yang sebenarnya korban error sementara)
    stamp = int(ts - (T["reject_recheck_days"] - 3) * DAY)
    for r in old.get("rejected", []):
        tg.data["rejected"][str(r).lower()] = stamp
    for url, m in (old.get("comic_sites") or {}).items():
        cm.data["sites"][comics.origin_of(url)] = comics.new_meta("legacy") | {"hits": m.get("hits", 0)}
    log("MIGRASI", f"state lama diimpor: {len(tg.data['channels'])} kanal, {len(cm.data['sites'])} situs "
                   f"(blocked_urls lama diabaikan — seed diberi kesempatan baru)")


def check_config(cfg: Config) -> int:
    log("CONFIG", "✔ semua file config valid")
    log("CONFIG", f"seed: {len(cfg.seeds_tg)} kanal TG, {len(cfg.seeds_comic)} situs komik | "
                  f"query crt.sh: {sum(len(v) for v in cfg.crt_queries.values())}")
    log("CONFIG", f"dilindungi: {len(cfg.protected)} domain, {len(cfg.free_hosts)} hosting gratis")
    # query crt.sh yang tak akan lolos penyaring strong = sia-sia
    for region, lst in cfg.crt_queries.items():
        for q in lst:
            probe = q.strip("%").lower() + ".com"
            if not cfg.re_strong.search(probe):
                log("CONFIG", f"⚠ crt query {q} tak cocok pola [judol].strong → hasilnya akan disaring semua")
    for w in cfg.warnings:
        log("CONFIG", f"⚠ {w}")
    tests = ["slotgacor88.com", "bk8vip.com", "law8firm.com", "google.com", "x.blogspot.com",
             "cuan-vip77.net", "situsberita.co.id"]
    for d in tests:
        dom = ekstrak_domain_aman(cfg, d)
        pts = score_domain(cfg, dom)[0] if dom else "—"
        log("TEST", f"{d:20} → aman_diblokir={dom or 'TIDAK'}  skor_tanpa_konteks={pts}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Radar Judol v3")
    ap.add_argument("--check-config", action="store_true", help="validasi config lalu keluar")
    ap.add_argument("--dry-run", action="store_true", help="jalan penuh tapi TIDAK menulis file apa pun")
    ap.add_argument("--only", default="", help="koma: telegram,comics,crt (default semua)")
    ap.add_argument("--budget", type=int, default=0, help="timpa time_budget_seconds")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        log("CONFIG", f"GAGAL: {e}")
        return 2
    if args.check_config:
        return check_config(cfg)
    for w in cfg.warnings:
        log("CONFIG", f"peringatan: {w}")

    T = cfg.T
    budget = args.budget or T["time_budget_seconds"]
    collect_dl = Deadline(budget - T["finalize_reserve_seconds"])
    full_dl = Deadline(budget)
    ts = now()
    http = Http(T)
    ctx = Ctx(cfg, http, set(), set(), ts, dry=args.dry_run)
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    if args.dry_run:
        log("MODE", "DRY-RUN: tidak ada file yang ditulis")

    load_known(ctx, Deadline(90))
    log("CACHE", f"{len(ctx.known)} domain dikenal, {len(ctx.allowlist)} allowlist")

    st_tg = Store("telegram", telegram.DEFAULT_STATE).load()
    st_cm = Store("comics", comics.DEFAULT_STATE).load()
    st_crt = Store("crt", crt.DEFAULT_STATE).load()
    st_pend = Store("pending", pipeline.DEFAULT_STATE).load()
    migrate_legacy(st_tg, st_cm, ts, T)

    results: list[CollectorResult] = []
    incoming: list = []

    def guarded(name: str, store: Store, fn, weight_left: int, w: int) -> None:
        if only and name not in only:
            return
        dl = collect_dl.sub(collect_dl.left() * w / max(1, weight_left))
        t0 = time.monotonic()
        try:
            res = fn(dl)
        except Exception as e:  # satu collector crash tidak boleh mematikan yang lain
            log("ERROR", f"collector {name} crash: {e!r}")
            traceback.print_exc()
            res = CollectorResult(name, error=repr(e))
        finally:
            try:
                store.save(args.dry_run)  # simpan progres SEGERA
            except Exception as e:
                log("ERROR", f"gagal simpan state {name}: {e!r}")
        res.stats["detik"] = round(time.monotonic() - t0)
        results.append(res)
        incoming.extend(res.proposals)

    guarded("telegram", st_tg, lambda dl: telegram.run(ctx, st_tg.data, dl), 10, 4)
    guarded("comics", st_cm, lambda dl: comics.run(ctx, st_cm.data, dl, incoming), 6, 4)
    guarded("crt", st_crt, lambda dl: crt.run(ctx, st_crt.data, dl), 2, 2)

    # gabung temuan → pending → finalisasi
    for r in results:
        added = pipeline.merge(st_pend.data["domains"], r.sightings, ts)
        log("MERGE", f"{r.name}: {len(r.sightings)} temuan ({added} domain baru di pending)")
    promoted: list[str] = []
    fin_stats: dict = {}
    try:
        promoted, fin_stats = pipeline.finalize(ctx, st_pend, full_dl)
    except Exception as e:
        log("ERROR", f"finalisasi crash: {e!r}")
        traceback.print_exc()
    finally:
        st_pend.save(args.dry_run)

    ringkasan(results, promoted, fin_stats, args.dry_run)
    crashed = [r for r in results if r.error]
    if results and len(crashed) == len(results):
        return 1
    if not promoted:
        log("HASIL", "0 domain baru yang lolos skor + DNS + verifikasi.")
    return 0


def ringkasan(results, promoted, fin, dry) -> None:
    lines = ["## 🐋 Radar Judol v3" + (" (dry-run)" if dry else ""), "",
             "| Collector | Status | Statistik |", "|---|---|---|"]
    for r in results:
        stat = ", ".join(f"{k}={v}" for k, v in r.stats.items())
        lines.append(f"| {r.name} | {'❌ ' + r.error if r.error else '✅'} | {stat} |")
    lines += ["", f"**Pipeline:** " + ", ".join(f"{k}={v}" for k, v in fin.items()), ""]
    if promoted:
        lines += [f"**{len(promoted)} domain ditambahkan:**", ""] + [f"- `{d}`" for d in promoted[:60]]
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass
