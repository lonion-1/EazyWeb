# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline bersama: kumpulkan temuan → skor → DNS → cek isi halaman → tulis blocklist."""
from __future__ import annotations

import os

from pathlib import Path

from .classify import extract_page
from .core import (
    DAY, FILE_TARGET, ROOT, Ctx, Deadline, Sighting, bersihkan_html, day_of, dns_many,
    ekstrak_domain_aman, log, norm_text, punya_induk,
)

DEFAULT_STATE = {"version": 3, "domains": {}}


def total(p: dict) -> int:
    return sum(p["src"].values())


def merge(doms: dict, sightings: list[Sighting], ts: float) -> int:
    """Gabungkan temuan. Skor = jumlah skor dari SUMBER BERBEDA (satu sumber tak bisa menggandakan diri)."""
    added = 0
    for s in sightings:
        p = doms.get(s.domain)
        if p is None:
            p = doms[s.domain] = {"src": {}, "cat": s.cat, "first": int(ts), "last": int(ts), "notes": [],
                                  "dns_fail_days": [], "page_checked": 0, "page_ok": None}
            added += 1
        p["last"] = int(ts)
        p["src"][s.source] = max(p["src"].get(s.source, 0), s.pts)
        if s.note and s.note not in p["notes"] and len(p["notes"]) < 5:
            p["notes"].append(s.note)
        if len(p["src"]) > 12:  # batasi ukuran state
            for k in sorted(p["src"], key=p["src"].get)[:len(p["src"]) - 12]:
                p["src"].pop(k, None)
    return added


def page_confirms(ctx: Ctx, domain: str, deadline: Deadline, cat: str = "judol") -> bool | None:
    """True=halaman sesuai kategori, False=bukan, None=tak bisa dicek (coba lagi nanti)."""
    cfg, T = ctx.cfg, ctx.cfg.T
    r = ctx.http.get(f"https://{domain}/", timeout=15, retries=0, max_bytes=800_000, deadline=deadline)
    if not r.ok and r.kind != "gone":
        r = ctx.http.get(f"http://{domain}/", timeout=15, retries=0, max_bytes=800_000, deadline=deadline)
    if not r.ok:
        return False if r.kind == "gone" else None
    clf = cfg.classifier
    if cat != "judol" and clf is not None and cat in clf.cats:
        # kurasi kategori: syaratnya curate_score (lebih ketat dari saat masuk kandidat)
        sc = clf.scores(domain, extract_page(r.text)).get(cat, 0)
        return sc >= clf.cats[cat].curate_score
    text = norm_text(bersihkan_html(r.text))[:300_000]
    return len(cfg.re_ctx_judol.findall(text)) >= T["page_confirm_hits"]


def target_for(cfg, cat: str) -> Path:
    if cat in cfg.targets:
        return cfg.targets[cat]
    return FILE_TARGET if cat == "judol" else ROOT / "Curated" / f"{cat}.txt"


def tulis_blocklist(domains: list[str], path: Path = FILE_TARGET) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(existing + "".join(f"||{d}^\n" for d in sorted(domains)), encoding="utf-8")
    os.replace(tmp, path)


def finalize(ctx: Ctx, store, deadline: Deadline) -> tuple[list[str], dict]:
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    doms = store.data["domains"]
    stats = {"pending": 0, "dns_dead": 0, "page_yes": 0, "page_no": 0, "promoted": 0, "capped": 0}

    # 1) bersih-bersih
    for d in list(doms):
        p = doms[d]
        if ts - p["last"] > T["pending_ttl_days"] * DAY or punya_induk(d, ctx.known) \
                or ekstrak_domain_aman(cfg, d, ctx.allowlist) != d:
            del doms[d]
    if len(doms) > T["pending_max"]:
        keep = sorted(doms, key=lambda d: (total(doms[d]), doms[d]["last"]), reverse=True)[:T["pending_max"]]
        for d in [d for d in doms if d not in set(keep)]:
            del doms[d]

    # 2) pilih yang layak
    ready = [d for d, p in doms.items() if total(p) >= T["promote_at"]]
    border = sorted(
        (d for d, p in doms.items()
         if d not in ready and total(p) >= T["confirm_min_score"]
         and ts - p.get("page_checked", 0) > T["page_recheck_days"] * DAY),
        key=lambda d: -total(doms[d]))[:T["page_confirm_max_per_run"]]

    # 3) DNS
    check = ready + border
    dns = dns_many(check, T["dns_workers"], T["dns_timeout_seconds"], deadline)
    alive_ready, alive_border = [], []
    for d in check:
        p, st = doms[d], dns[d]
        if st is False:
            days = p.setdefault("dns_fail_days", [])
            today = day_of(ts)
            if today not in days:
                days.append(today)
            stats["dns_dead"] += 1
            if len(days) >= T["dns_fail_drop_days"]:
                del doms[d]
        elif st is True:
            p["dns_fail_days"] = []
            (alive_ready if d in ready else alive_border).append(d)
        # None → tak tahu; biarkan, coba lagi run berikutnya

    # 4) cek isi halaman untuk yang abu-abu
    for d in alive_border:
        if deadline.expired(6):
            break
        p = doms[d]
        res = page_confirms(ctx, d, deadline, p.get("cat", "judol"))
        if res is None:
            continue
        p["page_checked"] = int(ts)
        p["page_ok"] = res
        if res:
            p["src"]["page"] = T["score_page_confirm"]
            stats["page_yes"] += 1
            if total(p) >= T["promote_at"]:
                alive_ready.append(d)
        else:
            stats["page_no"] += 1

    # 5) promosi dengan pagar: batas per-sumber & per-run
    promoted, per_src = [], {}
    for d in sorted(set(alive_ready), key=lambda x: -total(doms[x])):
        if len(promoted) >= T["max_new_domains_per_run"]:
            stats["capped"] += 1
            continue
        p = doms[d]
        top = max(p["src"], key=p["src"].get)
        cap = T["index_cap_per_source"] if top.startswith("index:") else T["max_promoted_per_source"]
        if per_src.get(top, 0) >= cap:
            stats["capped"] += 1
            continue
        per_src[top] = per_src.get(top, 0) + 1
        promoted.append(d)

    skor = {d: total(doms[d]) for d in promoted}
    by_target: dict[Path, list[str]] = {}
    for d in promoted:
        by_target.setdefault(target_for(cfg, doms[d].get("cat", "judol")), []).append(d)
    if promoted and not ctx.dry:
        for path, lst in by_target.items():
            tulis_blocklist(lst, path)  # tulis dulu; baru hapus dari pending
    for d in promoted:
        if not ctx.dry:
            doms.pop(d, None)
        ctx.known.add(d)
        log("+DOMAIN", f"{d}  (skor {skor[d]})")
    for path, lst in by_target.items():
        log("TARGET", f"{path.name}: +{len(lst)}")
    stats["promoted"] = len(promoted)
    stats["pending"] = len(doms)
    return promoted, stats
