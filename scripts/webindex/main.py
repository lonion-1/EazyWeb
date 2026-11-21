# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web Indexer — cari situs film/dewasa/komik/anime/judol via mesin pencari + crawl tautan keluar.
Keluaran: Candidates/<kategori>.txt + Candidates/index.json → dikurasi radar (EazyWeb)."""
from __future__ import annotations

import argparse
import os
import time
import traceback
import urllib.parse

from radar.core import ConfigError, Ctx, Deadline, Http, load_config, load_known, log, now

from . import queries
from .crawler import Indexer
from .engines import build_pool


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Web Indexer")
    ap.add_argument("--budget", type=int, default=0, help="detik (timpa run.time_budget_seconds)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--cats", default="", help="batasi kategori query, mis. film,comic")
    ap.add_argument("--engines", default="", help="batasi mesin: searxng,mojeek,ddg_html,bing_html")
    ap.add_argument("--queries", type=int, default=0, help="timpa queries_per_run")
    ap.add_argument("--dry-run", action="store_true", help="jalan penuh tanpa menulis file apa pun")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        log("CONFIG", f"GAGAL: {e}")
        return 2
    if not cfg.index or cfg.classifier is None:
        log("CONFIG", "config/index.toml tidak ada — Indexer tak bisa jalan")
        return 2
    icfg, R = cfg.index, cfg.index.get("run") or {}
    if args.workers:
        R["workers"] = args.workers
    budget = args.budget or int(R.get("time_budget_seconds", 3300))
    cats = {c.strip() for c in args.cats.split(",") if c.strip()} or None
    only_eng = {e.strip() for e in args.engines.split(",") if e.strip()} or None

    ctx = Ctx(cfg, Http(cfg.T), set(), set(), now(), dry=args.dry_run)
    load_known(ctx, Deadline(90))
    log("CACHE", f"{len(ctx.known)} domain sudah dikenal (tidak akan dijadikan kandidat)")
    pool = build_pool(icfg, only_eng)
    log("ENGINE", "mesin aktif: " + (", ".join(e.name for e in pool.engines) or "TIDAK ADA (hanya crawl frontier)"))

    ix = Indexer(ctx, icfg, pool)
    if not ix.frontier:  # frontier kosong & tanpa pencarian → mulai dari seed situs komik
        for u in cfg.seeds_comic:
            ix.push(u, 0, 80, "seed", force=True)
    total = Deadline(budget - 60)   # 60 dtk cadangan untuk simpan & ekspor
    t0 = time.monotonic()
    try:
        # 1) pencarian awal
        all_q = queries.build_all(icfg, ctx.ts)
        qs = queries.pick(all_q, ix.qdone, ctx.ts, args.queries or int(R.get("queries_per_run", 60)),
                          int(R.get("query_repeat_days", 7)), cats)
        log("PLAN", f"{len(all_q)} query tersedia → {len(qs)} dijalankan; frontier awal {len(ix.frontier)}")
        ix.search(qs, total.sub(total.left() * 0.15))
        # 2) crawl
        ix.crawl(total.sub(total.left() * 0.75))
        # 3) perburuan mirror: merek situs relevan + merek bawaan config
        n_m = int(R.get("mirror_queries_per_run", 40))
        brands = ix.top_brands(max(5, n_m // 3))
        for c, qc in (icfg.get("queries") or {}).items():
            if c != "mirror" and (not cats or c in cats):
                brands += list(qc.get("brands", []))
        mq = queries.mirror_queries(icfg, list(dict.fromkeys(brands)), n_m)
        log("MIRROR", f"{len(mq)} query perburuan mirror")
        ix.search(mq, total.sub(total.left() * 0.3))
        # 4) crawl sisa waktu
        ix.crawl(total)
    except Exception as e:
        log("ERROR", f"indexer crash: {e!r}")
        traceback.print_exc()
    finally:
        try:
            ix.save(args.dry_run)          # progres selalu tersimpan
        except Exception as e:
            log("ERROR", f"gagal simpan cache: {e!r}")
        exp = ix.export(args.dry_run)

    st = ix.stats
    log("HASIL", f"{round(time.monotonic() - t0)} dtk | query={st['queries']} hasil_cari={st['search_hits']} "
                 f"fetch={st['fetched']} ok={st['ok']} relevan_baru={st['relevant_new']} "
                 f"| kandidat diekspor={exp['exported']} (sudah dikenal: {exp['skipped_known']})")
    for k in sorted(k for k in st if k.startswith("new_")):
        log("HASIL", f"  {k[4:]}: +{st[k]} situs baru")
    ringkasan(st, exp, ix.new_domains, args.dry_run)
    return 0


def ringkasan(st: dict, exp: dict, new: list[str], dry: bool) -> None:
    lines = ["## 🕸️ Web Indexer" + (" (dry-run)" if dry else ""), "",
             "| Statistik | Nilai |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(st.items())]
    lines += [f"| kandidat diekspor | {exp['exported']} |", ""]
    if new:
        lines += [f"**{len(new)} situs relevan baru (contoh):** " + ", ".join(f"`{d}`" for d in new[:40])]
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass
