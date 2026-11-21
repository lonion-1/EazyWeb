# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/EazyWeb)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collector WhaleIndex & Threat Feeds: menyedot batch domain judol & phishing terindeks."""
from __future__ import annotations

import json

from .core import (
    DAY, CollectorResult, Ctx, Deadline, Sighting, ekstrak_domain_aman,
    log, punya_induk, score_domain,
)

DEFAULT_STATE = {"version": 1, "seen": {}}


def _parse_feed_domains(raw_text: str) -> list[str]:
    domains: list[str] = []
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "!", "[")):
            continue
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    val = obj.get("value") or obj.get("domain") or obj.get("host") or ""
                    if val:
                        domains.append(str(val).strip())
            except Exception:
                continue
        else:
            if s.startswith("||") and "^" in s:
                s = s[2:].split("^", 1)[0]
            domains.append(s)
    return domains


def run(ctx: Ctx, st: dict, deadline: Deadline) -> CollectorResult:
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    stats = {"sources_ok": 0, "sources_failed": 0, "found": 0, "skipped_known": 0}
    sightings: list[Sighting] = []

    seen_map: dict[str, int] = st.setdefault("seen", {})
    ttl_cut = ts - max(30, int(T.get("pending_ttl_days", 21)) * 2) * DAY
    st["seen"] = seen_map = {d: t for d, t in seen_map.items() if t > ttl_cut}

    sources = getattr(cfg, "feed_sources", [])
    if not sources:
        return CollectorResult("feeds", sightings, [], stats)

    for src in sources:
        if deadline.expired(8):
            stats["time_up"] = 1
            break
        name = str(src.get("name") or src.get("url") or "feed")
        url = str(src.get("url") or "").strip()
        kind = str(src.get("kind") or ("phishing" if "phishing" in name.lower() else "judol")).lower()
        if not url:
            continue

        r = ctx.http.get(url, timeout=25, retries=1, max_bytes=10_000_000, deadline=deadline)
        if not r.ok:
            if r.kind == "gone" and r.status == 404:
                log("FEEDS", f"{name}: belum terbit (404) — dilewati")
            else:
                log("FEEDS", f"{name}: gagal ({r.kind}/{r.status})")
                stats["sources_failed"] += 1
            continue

        stats["sources_ok"] += 1
        raw_domains = _parse_feed_domains(r.text or "")
        added_src = 0
        for raw_d in raw_domains:
            dom = ekstrak_domain_aman(cfg, raw_d, ctx.allowlist)
            if not dom:
                continue
            if punya_induk(dom, ctx.known):
                stats["skipped_known"] += 1
                continue
            if dom in seen_map:
                continue

            seen_map[dom] = int(ts)
            if kind == "phishing":
                pts = max(int(T["promote_at"]), int(T["score_strong_domain"]))
                note = "whaleindex:phishing"
            else:
                pts_calc, note_calc = score_domain(cfg, dom, context=True)
                pts = max(int(T["promote_at"]), pts_calc)
                note = f"whaleindex:{note_calc or 'judol'}"

            sightings.append(Sighting(dom, f"feed:{name}", pts, note))
            added_src += 1

        stats["found"] += added_src
        log("FEEDS", f"{name}: {len(raw_domains)} baris, +{added_src} kandidat baru")

    return CollectorResult("feeds", sightings, [], stats)
  
