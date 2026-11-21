# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/EazyWeb)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline bersama Turbo: kumpulkan temuan -> skor -> DNS -> parallel page+redirect check -> tulis blocklist."""
from __future__ import annotations

import html
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout

from .core import (
    DAY, FILE_TARGET, Ctx, Deadline, Sighting, bersihkan_html, day_of, dns_many,
    ekstrak_domain_aman, log, norm_text, punya_induk,
)

DEFAULT_STATE = {"version": 3, "domains": {}}

RE_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*?\burl\s*=\s*([^"'>\s]+)""",
    re.I,
)
RE_JS_REDIRECT = re.compile(
    r"""(?:window\.location(?:\.href)?|location\.replace|location\.assign)\s*(?:=|\()\s*["'](https?://[^"']+)["']""",
    re.I,
)


def total(p: dict) -> int:
    return sum(p["src"].values())


def merge(doms: dict, sightings: list[Sighting], ts: float) -> int:
    """Gabungkan temuan. Skor = jumlah skor dari SUMBER BERBEDA (satu sumber tak bisa menggandakan diri)."""
    added = 0
    for s in sightings:
        p = doms.get(s.domain)
        if p is None:
            p = doms[s.domain] = {"src": {}, "first": int(ts), "last": int(ts), "notes": [],
                                  "dns_fail_days": [], "page_checked": 0, "page_ok": None}
            added += 1
        p["last"] = int(ts)
        p["src"][s.source] = max(p["src"].get(s.source, 0), s.pts)
        if s.note and s.note not in p["notes"] and len(p["notes"]) < 5:
            p["notes"].append(s.note)
        if len(p["src"]) > 12:
            for k in sorted(p["src"], key=p["src"].get)[:len(p["src"]) - 12]:
                p["src"].pop(k, None)
    return added


def _extract_redirect_hosts(cfg, base_url: str, raw_html: str, allowlist: set) -> list[str]:
    hosts: list[str] = []
    for rx in (RE_META_REFRESH, RE_JS_REDIRECT):
        for m in rx.finditer(raw_html or ""):
            absu = urllib.parse.urljoin(base_url, html.unescape(m.group(1).strip()))
            h = urllib.parse.urlparse(absu).netloc.lower()
            dom = ekstrak_domain_aman(cfg, h, allowlist)
            if dom and dom not in hosts:
                hosts.append(dom)
    return hosts


def page_confirms(ctx: Ctx, domain: str, deadline: Deadline) -> tuple[bool | None, list[str]]:
    """-> (True=judol | False=bukan | None=gagal cek, bonus_redirect_domains)."""
    cfg, T = ctx.cfg, ctx.cfg.T
    bonus_domains: list[str] = []
    r = ctx.http.get(f"https://{domain}/", timeout=14, retries=0, max_bytes=800_000, deadline=deadline)
    if not r.ok and r.kind != "gone":
        r = ctx.http.get(f"http://{domain}/", timeout=14, retries=0, max_bytes=800_000, deadline=deadline)
    if not r.ok:
        return (False if r.kind == "gone" else None), bonus_domains

    # 1. Cek HTTP 301/302 final destination host
    final_host = urllib.parse.urlparse(r.url or "").netloc.lower()
    final_dom = ekstrak_domain_aman(cfg, final_host, ctx.allowlist)
    if final_dom and final_dom != domain:
        bonus_domains.append(final_dom)
        if punya_induk(final_dom, ctx.known) or cfg.re_strong.search(final_dom):
            return True, bonus_domains

    # 2. Cek Meta Refresh & JS Redirect di HTML
    raw = r.text or ""
    for rdom in _extract_redirect_hosts(cfg, r.url or f"https://{domain}/", raw, ctx.allowlist):
        if rdom != domain:
            bonus_domains.append(rdom)
            if punya_induk(rdom, ctx.known) or cfg.re_strong.search(rdom):
                return True, bonus_domains

    # 3. Cek kepadatan kata kunci judol pada teks halaman
    text = norm_text(bersihkan_html(raw))[:300_000]
    is_judol = len(cfg.re_ctx_judol.findall(text)) >= T["page_confirm_hits"]
    return is_judol, bonus_domains


def _confirm_many_parallel(
    ctx: Ctx,
    domains: list[str],
    deadline: Deadline,
    workers: int = 10,
) -> dict[str, tuple[bool | None, list[str]]]:
    res: dict[str, tuple[bool | None, list[str]]] = {}
    if not domains or deadline.expired(6):
        return res
    total_to = max(6.0, min(deadline.left() - 3.0, 45.0))
    ex = ThreadPoolExecutor(max_workers=max(1, min(workers, len(domains))))
    futs = {ex.submit(page_confirms, ctx, d, deadline): d for d in domains}
    try:
        for f in as_completed(futs, timeout=total_to):
            d = futs[f]
            try:
                res[d] = f.result()
            except Exception:
                res[d] = (None, [])
    except FutureTimeout:
        log("PAGE", "batas waktu cek halaman paralel — sisa dicek run berikutnya")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return res


def tulis_blocklist(domains: list[str]) -> None:
    existing = FILE_TARGET.read_text(encoding="utf-8", errors="ignore") if FILE_TARGET.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    tmp = FILE_TARGET.with_name(FILE_TARGET.name + ".tmp")
    tmp.write_text(existing + "".join(f"||{d}^\n" for d in sorted(domains)), encoding="utf-8")
    os.replace(tmp, FILE_TARGET)


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

    # 3) DNS paralel
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

    # 4) cek isi halaman & rantai redirect secara PARALEL untuk kandidat abu-abu
    page_results = _confirm_many_parallel(ctx, alive_border, deadline, workers=10)
    extra_sightings: list[Sighting] = []
    for d, (res, bonus_doms) in page_results.items():
        if res is None:
            continue
        p = doms[d]
        p["page_checked"] = int(ts)
        p["page_ok"] = res
        if res:
            p["src"]["page"] = T["score_page_confirm"]
            stats["page_yes"] += 1
            if total(p) >= T["promote_at"]:
                alive_ready.append(d)
            for bdom in bonus_doms:
                if not punya_induk(bdom, ctx.known):
                    extra_sightings.append(Sighting(bdom, f"redirect:{d}", T["promote_at"], "redirect_hop"))
        else:
            stats["page_no"] += 1

    if extra_sightings:
        merge(doms, extra_sightings, ts)

    # 5) promosi dengan pagar: batas per-sumber & per-run
    promoted, per_src = [], {}
    for d in sorted(set(alive_ready), key=lambda x: -total(doms[x])):
        if len(promoted) >= T["max_new_domains_per_run"]:
            stats["capped"] += 1
            continue
        p = doms[d]
        top = max(p["src"], key=p["src"].get)
        if per_src.get(top, 0) >= T["max_promoted_per_source"]:
            stats["capped"] += 1
            continue
        per_src[top] = per_src.get(top, 0) + 1
        promoted.append(d)

    skor = {d: total(doms[d]) for d in promoted}
    if promoted and not ctx.dry:
        tulis_blocklist(promoted)
    for d in promoted:
        if not ctx.dry:
            doms.pop(d, None)
        ctx.known.add(d)
        log("+DOMAIN", f"{d}  (skor {skor[d]})")
    stats["promoted"] = len(promoted)
    stats["pending"] = len(doms)
    return promoted, stats
