# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collector Telegram: pantau kanal publik (t.me/s/...), temukan kanal baru, panen domain."""
from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass

from .core import (
    DAY, CollectorResult, Ctx, Deadline, Sighting, bersihkan_html, ekstrak_domain_aman,
    extract_hosts, hl_cooling, hl_fail, hl_ok, hl_should_retire, log, norm_text, polite,
    punya_induk, score_domain,
)

RE_TG_MENTION = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([a-zA-Z][a-zA-Z0-9_]{4,31})\b")
RE_POST_MARK = re.compile(r'data-post="([^"]+)"')
RE_TG_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>')
RE_HREF = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
RE_TITLE = re.compile(r'class="tgme_channel_info_header_title"[^>]*>([\s\S]*?)</div>')
RE_DESC = re.compile(r'class="tgme_channel_info_description"[^>]*>([\s\S]*?)</div>')

DEFAULT_STATE = {"version": 3, "channels": {}, "retired": {}, "rejected": {}, "queue": {}, "disc_cursor": 0}


@dataclass
class Post:
    pid: str
    num: int
    text: str
    hrefs: list


def parse_posts(raw: str) -> list[Post]:
    """Pecah halaman t.me/s per-postingan. Link tersembunyi di balik teks ikut diambil (href)."""
    marks = list(RE_POST_MARK.finditer(raw))
    merged: dict[str, Post] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        block = raw[m.start():end]
        pid = m.group(1)
        tail = pid.rsplit("/", 1)[-1]
        text = " ".join(t for t in (bersihkan_html(x) for x in RE_TG_TEXT.findall(block)) if t)
        hrefs = [html.unescape(h) for h in RE_HREF.findall(block)]
        if pid in merged:
            merged[pid].text = (merged[pid].text + " " + text).strip()
            merged[pid].hrefs += hrefs
        else:
            merged[pid] = Post(pid, int(tail) if tail.isdigit() else 0, text, hrefs)
    return sorted(merged.values(), key=lambda p: p.num)


def tg_usernames(cfg, text: str) -> set[str]:
    out = set()
    for m in RE_TG_MENTION.finditer(text or ""):
        u = m.group(1)
        if u.lower() not in cfg.reserved_tg and not u.lower().endswith("bot"):
            out.add(u)
    return out


def new_meta(source: str, kind: str) -> dict:
    return {"source": source, "kind": kind, "scans": 0, "harvested": 0, "dry_runs": 0,
            "last_scan": 0, "last_post": 0}


# ──────────────────────────── ambil kanal ────────────────────────────
def fetch_channel(ctx: Ctx, name: str, last_post: int, deadline: Deadline) -> tuple[str, list[Post]]:
    """→ (status, posting_baru). status: ok|gone|blocked|transient|skipped."""
    T = ctx.cfg.T
    pages = T["tg_max_pages"]
    got: dict[str, Post] = {}
    before = None
    for page in range(pages):
        url = f"https://t.me/s/{name}" + (f"?before={before}" if before else "")
        r = ctx.http.get(url, timeout=18, retries=1, deadline=deadline)
        if not r.ok:
            if page == 0:
                return r.kind, []
            break
        if "tgme_widget_message" not in r.text:  # kanal privat / tanpa web-preview / tak ada
            if page == 0:
                return "gone", []
            break
        posts = parse_posts(r.text)
        if not posts:
            break
        for p in posts:
            got[p.pid] = p
        nums = [p.num for p in posts if p.num]
        if not nums:
            break
        oldest = min(nums)
        if oldest <= last_post or oldest <= 1 or oldest == before:
            break
        before = oldest
        polite(ctx, deadline)
    fresh = [p for p in got.values() if p.num > last_post] if last_post else list(got.values())
    return "ok", sorted(fresh, key=lambda p: p.num)


def classify(ctx: Ctx, name: str, deadline: Deadline) -> tuple[str, str | None]:
    """→ ('accept', kind) | ('reject', None) | ('retry', None)."""
    cfg, T = ctx.cfg, ctx.cfg.T
    r = ctx.http.get(f"https://t.me/s/{name}", timeout=18, retries=1, deadline=deadline)
    if r.kind == "skipped":
        return "retry", None
    if not r.ok:
        return ("reject", None) if r.kind == "gone" else ("retry", None)
    if "tgme_widget_message" not in r.text:
        return "reject", None
    posts = [p for p in parse_posts(r.text) if p.text]
    if len(posts) < 2:
        return "reject", None
    t = RE_TITLE.search(r.text)
    d = RE_DESC.search(r.text)
    sample = norm_text(" ".join([
        bersihkan_html(t.group(1)) if t else "",
        bersihkan_html(d.group(1)) if d else "",
        *[p.text for p in posts[-10:]],
    ]))
    if T["tg_require_indonesian"] and len(cfg.re_indo.findall(sample)) < 2:
        return "reject", None
    if cfg.re_ctx_judol.search(sample):
        return "accept", "judol"
    if cfg.re_comic_ctx.search(sample) and cfg.re_comic_hint.search(sample):
        return "accept", "comic"
    return "reject", None


# ──────────────────────────── panen postingan ────────────────────────────
def harvest_post(ctx: Ctx, ch: str, kind: str, post: Post, sightings: list, proposals: list, snow: set) -> int:
    cfg = ctx.cfg
    text = norm_text(post.text)
    blob = text + " " + " ".join(post.hrefs)
    snow.update(tg_usernames(cfg, blob))
    has_ctx = bool(cfg.re_ctx_judol.search(text))
    comic_ctx = bool(cfg.re_comic_ctx.search(text))
    found, seen_here = 0, set()
    for host in extract_hosts(cfg, blob):
        dom = ekstrak_domain_aman(cfg, host, ctx.allowlist)
        if not dom or dom in seen_here:
            continue
        seen_here.add(dom)
        if kind == "judol":
            if punya_induk(dom, ctx.known):
                continue
            pts, note = score_domain(cfg, dom, context=has_ctx)
            if pts:
                sightings.append(Sighting(dom, f"tg:@{ch}", pts, note))
                found += 1
        else:  # kanal komik: iklan judol (strong) + situs komik baru
            if cfg.re_strong.search(dom):
                if not punya_induk(dom, ctx.known):
                    pts, note = score_domain(cfg, dom)
                    sightings.append(Sighting(dom, f"tg:@{ch}", pts, note))
                    found += 1
            elif cfg.re_comic_hint.search(dom) or comic_ctx:
                proposals.append((f"https://{dom}/", f"tg:@{ch}"))
                found += 1
    return found


# ──────────────────────────── perawatan state ────────────────────────────
def _housekeeping(st: dict, ts: float, T: dict) -> None:
    st["retired"] = {k: v for k, v in st["retired"].items() if v > ts}
    cut = ts - T["reject_recheck_days"] * DAY
    st["rejected"] = {k: v for k, v in st["rejected"].items() if v > cut}
    if len(st["rejected"]) > 3000:
        keep = sorted(st["rejected"].items(), key=lambda kv: kv[1], reverse=True)[:3000]
        st["rejected"] = dict(keep)
    if len(st["queue"]) > T["tg_queue_max"]:
        keep = sorted(st["queue"].items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:T["tg_queue_max"]]
        st["queue"] = dict(keep)


def _sync_seeds(ctx: Ctx, st: dict) -> None:
    cfg, ts = ctx.cfg, ctx.ts
    ch = st["channels"]
    seeds_lc = {k.lower(): k for k in cfg.seeds_tg}
    # seed yang dihapus dari config → ikut dibuang
    for name in [n for n, m in ch.items() if m.get("source") == "seed" and n.lower() not in seeds_lc]:
        log("SEED", f"@{name} tak ada lagi di seeds.toml → dibuang")
        ch.pop(name, None)
    have = {n.lower() for n in ch}
    for lc, name in seeds_lc.items():
        if lc in have or st["retired"].get(lc, 0) > ts:
            continue
        ch[name] = new_meta("seed", cfg.seeds_tg[name])


def _retire(st: dict, name: str, why: str, ts: float, T: dict) -> None:
    log("PAROLE", f"@{name} dipensiunkan ({why}); dicoba lagi setelah {T['parole_days']} hari")
    st["channels"].pop(name, None)
    st["retired"][name.lower()] = int(ts + T["parole_days"] * DAY)


def _enqueue(ctx: Ctx, st: dict, name: str, src: str) -> None:
    lc = name.lower()
    if lc in {n.lower() for n in st["channels"]} or lc in st["rejected"] \
            or st["retired"].get(lc, 0) > ctx.ts or lc in st["queue"]:
        return
    st["queue"][lc] = {"name": name, "src": src, "ts": int(ctx.ts), "tries": 0}


# ──────────────────────────── discovery ────────────────────────────
def _discover(ctx: Ctx, st: dict, deadline: Deadline, stats: dict) -> None:
    cfg, T = ctx.cfg, ctx.cfg.T
    urls = [f"https://lyzem.com/search?q={urllib.parse.quote_plus(q)}"
            for kind in ("comic", "judol") for q in cfg.tg_disc.get(kind, [])]
    if not urls:
        return
    cur = int(st.get("disc_cursor", 0)) % len(urls)
    n = min(T["tg_discovery_queries_per_run"], len(urls))
    for i in range(n):
        if deadline.expired(5):
            break
        r = ctx.http.get(urls[(cur + i) % len(urls)], timeout=18, retries=1, deadline=deadline)
        st["disc_cursor"] = (cur + i + 1) % len(urls)
        if r.ok:
            for u in tg_usernames(cfg, r.text):
                _enqueue(ctx, st, u, "directory")
        polite(ctx, deadline)
    stats["queue"] = len(st["queue"])


def _verify_queue(ctx: Ctx, st: dict, deadline: Deadline, stats: dict) -> None:
    cfg, T = ctx.cfg, ctx.cfg.T
    order = sorted(st["queue"].items(), key=lambda kv: kv[1].get("ts", 0))
    done = 0
    for lc, q in order:
        if done >= T["tg_verify_per_run"] or deadline.expired(6):
            break
        if len(st["channels"]) >= T["tg_max_channels"]:
            break
        verdict, kind = classify(ctx, q["name"], deadline)
        if verdict == "retry":
            q["tries"] = q.get("tries", 0) + 1
            if q["tries"] >= 4:
                st["rejected"][lc] = int(ctx.ts)
                st["queue"].pop(lc, None)
            if ctx.http.is_tripped("t.me"):
                break
            continue
        done += 1
        st["queue"].pop(lc, None)
        if verdict == "accept":
            st["channels"][q["name"]] = new_meta(q["src"], kind)
            stats["new_channels"] = stats.get("new_channels", 0) + 1
            log("NEW-CHANNEL", f"{kind} @{q['name']} ({q['src']})")
        else:
            st["rejected"][lc] = int(ctx.ts)
        polite(ctx, deadline)


# ──────────────────────────── jalankan ────────────────────────────
def run(ctx: Ctx, st: dict, deadline: Deadline) -> CollectorResult:
    cfg, T, ts = ctx.cfg, ctx.cfg.T, ctx.ts
    stats: dict = {"scanned": 0, "posts": 0, "found": 0, "failed": 0}
    sightings: list = []
    proposals: list = []
    snow: set[str] = set()

    _housekeeping(st, ts, T)
    _sync_seeds(ctx, st)
    _discover(ctx, st, deadline.sub(deadline.left() * 0.25), stats)

    ch = st["channels"]
    order = sorted((m.get("last_scan", 0), n) for n, m in ch.items() if not hl_cooling(m, ts))
    to_retire: list[tuple[str, str]] = []
    scan_deadline = deadline.sub(deadline.left() * 0.8)

    for _, name in order[:T["tg_channels_per_run"]]:
        if scan_deadline.expired(10):
            stats["time_up"] = 1
            break
        m = ch[name]
        status, posts = fetch_channel(ctx, name, m.get("last_post", 0), scan_deadline)
        if status == "skipped":
            if ctx.http.is_tripped("t.me"):
                break
            continue
        m["last_scan"] = int(ts)
        m["scans"] = m.get("scans", 0) + 1
        if status != "ok":
            hl_fail(m, status, ts, T)
            stats["failed"] += 1
            if hl_should_retire(m, T):
                to_retire.append((name, "tak bisa diakses berulang"))
            polite(ctx, scan_deadline)
            continue
        hl_ok(m, ts)
        found = 0
        for p in posts:
            found += harvest_post(ctx, name, m.get("kind", "judol"), p, sightings, proposals, snow)
        if posts:
            m["last_post"] = max(m.get("last_post", 0), max(p.num for p in posts))
            if found:
                m["harvested"] = m.get("harvested", 0) + found
                m["dry_runs"] = 0
            else:
                m["dry_runs"] = m.get("dry_runs", 0) + 1  # hanya dihitung bila ada postingan baru
                if m["dry_runs"] >= T["tg_max_dry_runs"]:
                    to_retire.append((name, f"zonk {T['tg_max_dry_runs']}x"))
        stats["scanned"] += 1
        stats["posts"] += len(posts)
        stats["found"] += found
        log("TG", f"@{name} ({m.get('kind')}): {len(posts)} post baru, +{found} temuan")
        polite(ctx, scan_deadline)

    for name, why in to_retire:
        _retire(st, name, why, ts, T)

    for u in snow:
        _enqueue(ctx, st, u, "snowball")
    _verify_queue(ctx, st, deadline, stats)
    stats["channels"] = len(st["channels"])
    return CollectorResult("telegram", sightings, proposals, stats)
