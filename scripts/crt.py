# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collector crt.sh: sadap Certificate Transparency untuk domain judol yang baru terbit."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

from .core import (
    DAY, CollectorResult, Ctx, Deadline, Sighting, ekstrak_domain_aman, hl_cooling,
    hl_fail, hl_ok, log, polite, punya_induk, score_domain,
)

DEFAULT_STATE = {"version": 3, "queries": {}}


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _fresh(entry: dict, ts: float, max_age_days: int) -> bool:
    nb = str(entry.get("not_before") or "")[:10]
    try:
        t = datetime.strptime(nb, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return True  # tanggal tak terbaca → jangan buang
    return ts - t <= max_age_days * DAY


def run(ctx: Ctx, st: dict, deadline: Deadline) -> CollectorResult:
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    stats = {"queries_ok": 0, "queries_failed": 0, "found": 0}
    sightings: list = []
    qs = st["queries"]
    allq = [(f"{region}|{q}", region, q) for region, lst in cfg.crt_queries.items() for q in lst]
    valid = {k for k, _, _ in allq}
    for k in [k for k in qs if k not in valid]:
        del qs[k]

    # giliran: query yang paling lama tak sukses dulu; kursor TIDAK maju kalau gagal
    order = sorted((a for a in allq if not hl_cooling(qs.get(a[0], {}), ts)),
                   key=lambda a: qs.get(a[0], {}).get("last_ok", 0))
    for key, region, q in order[:T["crt_batch_per_run"]]:
        if deadline.expired(T["crt_timeout"] * 0.6):
            stats["time_up"] = 1
            break
        m = qs.setdefault(key, {})
        url = ("https://crt.sh/?q=" + urllib.parse.quote(q)
               + "&output=json&exclude=expired&deduplicate=Y")
        r = ctx.http.get(url, timeout=T["crt_timeout"], retries=1, max_bytes=30_000_000, deadline=deadline)
        if r.kind == "skipped":
            if ctx.http.is_tripped("crt.sh"):
                break
            continue
        if not r.ok:
            hl_fail(m, r.kind, ts, T)
            stats["queries_failed"] += 1
            continue
        try:
            entries = json.loads(r.text)
            if not isinstance(entries, list):
                raise ValueError("bukan daftar")
        except (ValueError, TypeError):
            hl_fail(m, "transient", ts, T)   # JSON terpotong / halaman error
            stats["queries_failed"] += 1
            continue
        hl_ok(m, ts)
        stats["queries_ok"] += 1
        entries.sort(key=lambda e: _num(e.get("id")) if isinstance(e, dict) else 0, reverse=True)
        found = 0
        seen = set()
        for e in entries[:T["crt_max_entries"]]:
            if not isinstance(e, dict) or not _fresh(e, ts, T["crt_max_age_days"]):
                continue
            for line in str(e.get("name_value", "")).splitlines():
                dom = ekstrak_domain_aman(cfg, line, ctx.allowlist)
                if not dom or dom in seen or punya_induk(dom, ctx.known):
                    continue
                seen.add(dom)
                pts, note = score_domain(cfg, dom)
                if pts:
                    sightings.append(Sighting(dom, f"crt:{q}", pts, note))
                    found += 1
        stats["found"] += found
        log("CRT", f"[{region}] {q}: {len(entries)} sertifikat, +{found} kandidat")
        polite(ctx, deadline, 1.5)
    return CollectorResult("crt", sightings, [], stats)
