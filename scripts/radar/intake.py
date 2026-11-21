# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collector intake: baca Candidates/index.json (hasil Indexer) → temuan untuk dikurasi pipeline."""
from __future__ import annotations

import json

from .core import (
    DAY, ROOT, CollectorResult, Ctx, Deadline, Sighting, ekstrak_domain_aman, log, punya_induk,
)

DEFAULT_STATE = {"version": 3, "done": {}}
INDEX_FILE = ROOT / "Candidates" / "index.json"


def run(ctx: Ctx, st: dict, deadline: Deadline) -> CollectorResult:
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    stats = {"emitted": 0, "skipped_known": 0}
    if cfg.classifier is None or not INDEX_FILE.exists():
        stats["skipped"] = 1
        return CollectorResult("intake", [], [], stats)
    try:
        doms = json.loads(INDEX_FILE.read_text(encoding="utf-8")).get("domains", {})
    except (ValueError, OSError) as e:
        log("INTAKE", f"index.json tak terbaca ({e})")
        return CollectorResult("intake", [], [], {"error_read": 1})

    allowed = set((cfg.index.get("curate") or {}).get("intake", [])) & set(cfg.classifier.cats)
    done = st["done"] = {d: t for d, t in st["done"].items() if ts - t < T["intake_recheck_days"] * DAY}
    out: list[Sighting] = []
    for dom, rec in sorted(doms.items(), key=lambda kv: -int(kv[1].get("s", 0))):
        if len(out) >= T["intake_per_run"]:
            break
        cat = rec.get("cat")
        if cat not in allowed or dom in done:
            continue
        if punya_induk(dom, ctx.known):
            stats["skipped_known"] += 1
            continue
        if ekstrak_domain_aman(cfg, dom, ctx.allowlist) != dom:
            continue
        out.append(Sighting(dom, f"index:{cat}", T["score_intake"], f"index s={rec.get('s')}", cat=cat))
        done[dom] = int(ts)
    stats["emitted"] = len(out)
    log("INTAKE", f"{len(out)} kandidat dari Indexer masuk antrean kurasi")
    return CollectorResult("intake", out, [], stats)
