# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collector situs komik: sedot banner/iklan judol dari situs komik & halaman bacanya."""
from __future__ import annotations

import html
import os
import random
import re
import urllib.parse

from .core import (
    DAY, CollectorResult, Ctx, Deadline, Sighting, bersihkan_html, ekstrak_domain_aman,
    hl_cooling, hl_fail, hl_ok, hl_should_retire, log, norm_text, polite, punya_induk,
    RE_URL_EXTRACT, score_domain,
)

DEFAULT_STATE = {"version": 3, "sites": {}, "proposals": {}, "retired": {}, "rejected": {}}

RE_ANCHOR = re.compile(r"<a\b([^>]*)>([\s\S]*?)</a>", re.I)
RE_HREF_ATTR = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
RE_HREF_ANY = re.compile(r'href=["\']([^"\'#\s]+)["\']', re.I)
RE_DATA_URL = re.compile(
    r"""(?:data-(?:href|url|link|target)\s*=\s*|window\.open\(\s*|location(?:\.href)?\s*=\s*)["'](https?://[^"'\s<>]+)""",
    re.I)
RE_ATTR_TXT = re.compile(r'(?:alt|title|aria-label)=["\']([^"\']+)["\']', re.I)
RE_IMG_SRC = re.compile(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.I)


def origin_of(url: str) -> str:
    p = urllib.parse.urlparse(url if "//" in url else "https://" + url)
    host = p.netloc.lower()
    return f"{p.scheme or 'https'}://{host}/" if host else ""


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def new_meta(source: str) -> dict:
    return {"source": source, "hits": 0, "scans": 0, "last_scan": 0}


def _hosts_of_sites(cfg, sites: dict) -> set[str]:
    out = set()
    for u in sites:
        h = host_of(u)
        out.add(h)
        b = ekstrak_domain_aman(cfg, h)
        if b:
            out.add(b)
    return out


def outbound(raw: str) -> list[tuple[str, str, bool]]:
    """Semua tautan keluar: (href, teks_konteks_banner, ada_gambar)."""
    items: list[tuple[str, str, bool]] = []
    for attrs, inner in RE_ANCHOR.findall(raw):
        m = RE_HREF_ATTR.search(attrs)
        if not m:
            continue
        txt = bersihkan_html(inner)
        alts = " ".join(RE_ATTR_TXT.findall(attrs + " " + inner))
        srcs = " ".join(re.sub(r"[-_./]+", " ", os.path.basename(urllib.parse.urlparse(s).path))
                        for s in RE_IMG_SRC.findall(inner))
        items.append((html.unescape(m.group(1)), norm_text(f"{txt} {alts} {srcs}"), "<img" in inner.lower()))
    for href in RE_DATA_URL.findall(raw):
        items.append((html.unescape(href), "", False))
    return items


def pick_deep_links(cfg, raw: str, origin: str, host: str, n: int) -> list[str]:
    if n <= 0:
        return []
    seen, links = set(), []
    for href in RE_HREF_ANY.findall(raw):
        absu = urllib.parse.urljoin(origin, html.unescape(href))
        p = urllib.parse.urlparse(absu)
        if p.scheme not in ("http", "https") or p.netloc.lower().removeprefix("www.") != host:
            continue
        if p.path in ("", "/") or p.path in seen or not cfg.re_chapter_path.search(p.path):
            continue
        seen.add(p.path)
        links.append(absu)
    random.shuffle(links)
    return links[:n]


def looks_comic(cfg, host: str, text: str) -> bool:
    if cfg.re_comic_hint.search(host):
        return True
    hits = len(cfg.re_comic_hint.findall(bersihkan_html(text).lower()))
    return hits >= cfg.T["comic_verify_hits"]


# ──────────────────────────── pindai satu situs ────────────────────────────
def scan_site(ctx: Ctx, origin: str, meta: dict, comic_hosts: set, deadline: Deadline,
              sightings: list, proposals: list) -> str:
    """→ status http (ok/gone/blocked/transient/skipped)."""
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    site_host = host_of(origin)
    r = ctx.http.get(origin, timeout=18, retries=1, deadline=deadline)
    if r.kind == "skipped":
        return "skipped"
    meta["last_scan"] = int(ts)
    meta["scans"] = meta.get("scans", 0) + 1
    if not r.ok:
        hl_fail(meta, r.kind, ts, T)
        log("COMIC", f"{site_host}: {r.kind} ({r.status}) — cooldown {max(0, int((meta.get('cool_until', 0) - ts) / 3600))} jam")
        return r.kind
    hl_ok(meta, ts)

    # situs pindah domain? → usulkan domain tujuan (diverifikasi dulu)
    final_host = host_of(r.url)
    if final_host and final_host != site_host:
        proposals.append((origin_of(r.url), f"redirect:{site_host}"))
        comic_hosts.add(final_host)

    pages = [(origin, r.text)]
    for link in pick_deep_links(cfg, r.text, r.url, final_host or site_host, T["comic_deep_pages"]):
        if deadline.expired(8):
            break
        polite(ctx, deadline, 0.6)
        rr = ctx.http.get(link, timeout=15, retries=0, deadline=deadline)
        if rr.ok:
            pages.append((link, rr.text))

    best: dict[str, Sighting] = {}
    hits = 0
    for page_url, raw in pages:
        for href, ctx_txt, has_img in outbound(raw):
            p = urllib.parse.urlparse(href)
            th = p.netloc.lower().removeprefix("www.")
            if not th or th == site_host or th in comic_hosts:
                continue
            dom = ekstrak_domain_aman(cfg, th, ctx.allowlist | comic_hosts)
            if not dom:
                continue
            if cfg.re_comic_hint.search(dom):  # mirror / situs mitra komik
                proposals.append((origin_of(href), f"mirror:{site_host}"))
                comic_hosts.add(th)
                continue
            if punya_induk(dom, ctx.known):
                continue
            banner = bool(cfg.re_banner.search(ctx_txt)) and (has_img or len(p.path) > 1)
            pts, note = score_domain(cfg, dom, banner=banner)
            if pts and (dom not in best or pts > best[dom].pts):
                best[dom] = Sighting(dom, f"comic:{site_host}", pts, note)
        for m in RE_URL_EXTRACT.finditer(norm_text(raw)):  # URL polos di skrip/atribut
            dom = ekstrak_domain_aman(cfg, m.group(1), ctx.allowlist | comic_hosts)
            if dom and dom not in comic_hosts and dom not in best and not punya_induk(dom, ctx.known):
                if cfg.re_strong.search(dom):
                    pts, note = score_domain(cfg, dom)
                    best[dom] = Sighting(dom, f"comic:{site_host}", pts, note)
    sightings.extend(best.values())
    hits = len(best)
    meta["hits"] = meta.get("hits", 0) + hits
    log("COMIC", f"{site_host}: {len(pages)} halaman, +{hits} domain mencurigakan")
    return "ok"


# ──────────────────────────── jalankan ────────────────────────────
def run(ctx: Ctx, st: dict, deadline: Deadline, incoming: list) -> CollectorResult:
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    stats: dict = {"scanned": 0, "found": 0, "failed": 0, "verified": 0}
    sightings: list = []
    proposals_out: list = []
    sites, props = st["sites"], st["proposals"]

    # perawatan
    st["retired"] = {k: v for k, v in st["retired"].items() if v > ts}
    st["rejected"] = {k: v for k, v in st["rejected"].items() if v > ts - T["reject_recheck_days"] * DAY}

    # seed
    seeds = {origin_of(u) for u in cfg.seeds_comic if origin_of(u)}
    for o in [o for o, m in sites.items() if m.get("source") == "seed" and o not in seeds]:
        log("SEED", f"{o} tak ada lagi di seeds.toml → dibuang")
        sites.pop(o, None)
    for o in seeds:
        if o not in sites and st["retired"].get(o, 0) <= ts:
            sites[o] = new_meta("seed")

    comic_hosts = _hosts_of_sites(cfg, sites)

    # usulan masuk (dari Telegram) → antrean verifikasi
    for origin, src in incoming:
        o = origin_of(origin)
        h = host_of(o)
        if not o or h in comic_hosts or o in sites or o in props or o in st["rejected"] or st["retired"].get(o, 0) > ts:
            continue
        props[o] = {"src": src, "ts": int(ts), "tries": 0}

    # verifikasi usulan: hanya yang benar-benar situs komik yang masuk daftar
    vdl = deadline.sub(deadline.left() * 0.25)
    n = 0
    for o, pr in sorted(props.items(), key=lambda kv: kv[1].get("ts", 0)):
        if n >= T["comic_verify_per_run"] or vdl.expired(6) or len(sites) >= T["comic_max_sites"]:
            break
        r = ctx.http.get(o, timeout=18, retries=1, deadline=vdl)
        if r.kind == "skipped":
            break
        n += 1
        if r.ok:
            if looks_comic(cfg, host_of(o), r.text):
                sites[o] = new_meta(pr["src"])
                comic_hosts.add(host_of(o))
                stats["verified"] += 1
                log("NEW-COMIC", f"{o} ({pr['src']})")
            else:
                st["rejected"][o] = int(ts)
            props.pop(o, None)
        elif r.kind == "gone":
            st["rejected"][o] = int(ts)
            props.pop(o, None)
        else:
            pr["tries"] = pr.get("tries", 0) + 1
            if pr["tries"] >= 4:
                st["rejected"][o] = int(ts)
                props.pop(o, None)
        polite(ctx, vdl)

    # pindai bergilir: yang paling lama tak dipindai duluan (tak ada lagi kelaparan)
    order = sorted((m.get("last_scan", 0), o) for o, m in sites.items() if not hl_cooling(m, ts))
    retire: list[str] = []
    for _, o in order[:T["comic_sites_per_run"]]:
        if deadline.expired(10):
            stats["time_up"] = 1
            break
        status = scan_site(ctx, o, sites[o], comic_hosts, deadline, sightings, proposals_out)
        if status == "skipped":
            continue
        if status == "ok":
            stats["scanned"] += 1
        else:
            stats["failed"] += 1
            if hl_should_retire(sites[o], T):
                retire.append(o)
        polite(ctx, deadline)

    for o in retire:
        log("PAROLE", f"{o} dipensiunkan; dicoba lagi setelah {T['parole_days']} hari")
        sites.pop(o, None)
        st["retired"][o] = int(ts + T["parole_days"] * DAY)

    # usulan yang ditemukan saat memindai → antrean untuk run berikutnya
    active_hosts = _hosts_of_sites(cfg, sites)  # bukan comic_hosts: itu sudah memuat host usulan
    for origin, src in proposals_out:
        o = origin_of(origin)
        if o and host_of(o) not in active_hosts and o not in sites and o not in props \
                and o not in st["rejected"] and st["retired"].get(o, 0) <= ts:
            props[o] = {"src": src, "ts": int(ts), "tries": 0}

    stats["found"] = len(sightings)
    stats["sites"] = len(sites)
    return CollectorResult("comics", sightings, [], stats)
