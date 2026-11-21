# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generator query: template × pengisi per kategori, plus perburuan mirror."""
from __future__ import annotations

import random
import time

from radar.core import DAY


def build_all(icfg: dict, ts: float) -> list[tuple[str, str]]:
    year = str(time.gmtime(ts).tm_year)
    out: list[tuple[str, str]] = []
    for cat, qc in (icfg.get("queries") or {}).items():
        if cat == "mirror":
            continue
        for q in qc.get("plain", []):
            out.append((cat, q.replace("{year}", year)))
        fill = qc.get("fillers", [])
        for tpl in qc.get("templates", []):
            if "{t}" in tpl:
                out += [(cat, tpl.replace("{t}", f).replace("{year}", year)) for f in fill]
            else:
                out.append((cat, tpl.replace("{year}", year)))
    return list(dict.fromkeys(out))


def pick(all_q: list[tuple[str, str]], done: dict, ts: float, n: int, repeat_days: int,
         cats: set[str] | None = None) -> list[tuple[str, str]]:
    """Ambil n query yang belum dijalankan dalam repeat_days, tersebar merata antar kategori."""
    by_cat: dict[str, list] = {}
    for cat, q in all_q:
        if cats and cat not in cats:
            continue
        if ts - done.get(q, 0) > repeat_days * DAY:
            by_cat.setdefault(cat, []).append((cat, q))
    for lst in by_cat.values():
        random.shuffle(lst)
    out: list = []
    while len(out) < n and any(by_cat.values()):
        for lst in by_cat.values():  # bergiliran antar kategori
            if lst and len(out) < n:
                out.append(lst.pop())
    return out


def mirror_queries(icfg: dict, brands: list[str], n: int) -> list[tuple[str, str]]:
    tpls = ((icfg.get("queries") or {}).get("mirror") or {}).get("templates", [])
    out = [("mirror", t.replace("{b}", b)) for b in brands for t in tpls]
    random.shuffle(out)
    return out[:n]
