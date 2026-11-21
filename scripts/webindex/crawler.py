# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crawler indexer: frontier berprioritas + pekerja paralel + pengejaran tautan keluar/mirror.
Hanya menyimpan DOMAIN, kategori, skor, dan judul singkat — isi halaman tidak disimpan."""
from __future__ import annotations

import difflib
import heapq
import html
import itertools
import json
import os
import random
import re
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from radar.classify import Page, extract_page
from radar.comics import RE_DATA_URL
from radar.core import (
    DAY, ROOT, Ctx, Deadline, Store, ekstrak_domain_aman, log, polite, punya_induk,
)

CACHE_DIR = ROOT / ".cache" / "webindex"   # via actions/cache, TIDAK di-commit
CAND_DIR = ROOT / "Candidates"             # di-commit: keluaran Indexer
RE_HREF = re.compile(r'href=["\']([^"\'#\s]+)["\']', re.I)
DEEP_EXTRA = r"movie|film|nonton|watch|episode|anime|category|genre|tag|page|latest|terbaru"


def similar(a: str, b: str) -> bool:
    """Apakah dua domain tampak seperti mirror/saudara (lk21.xyz ~ lk21official.net)?"""
    la, lb = a.split(".")[0], b.split(".")[0]
    if la == lb:
        return False
    if len(la) >= 4 and len(lb) >= 4 and (la in lb or lb in la):
        return True
    return difflib.SequenceMatcher(None, la, lb).ratio() >= 0.75


class Indexer:
    def __init__(self, ctx: Ctx, icfg: dict, pool):
        self.ctx, self.icfg, self.pool = ctx, icfg, pool
        self.R = icfg.get("run") or {}
        self.cls = ctx.cfg.classifier
        self.ts = ctx.ts
        self.s_front = Store("frontier", {"items": {}}, CACHE_DIR).load()
        self.s_seen = Store("seen", {"domains": {}}, CACHE_DIR).load()
        self.s_index = Store("index", {"domains": {}}, CACHE_DIR).load()
        self.s_q = Store("queries", {"done": {}}, CACHE_DIR).load()
        self.frontier: dict = self.s_front.data["items"]
        self.seen: dict = self.s_seen.data["domains"]
        self.index: dict = self.s_index.data["domains"]
        self.qdone: dict = self.s_q.data["done"]
        self.heap: list = []
        self.seq = itertools.count()
        for url, m in self.frontier.items():
            heapq.heappush(self.heap, (-m["p"], next(self.seq), url))
        self.run_pages: dict[str, int] = {}
        self.last_hit: dict[str, float] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.deadline = Deadline(0)
        self.new_domains: list[str] = []
        self.stats: dict = {"queries": 0, "search_hits": 0, "fetched": 0, "ok": 0, "relevant_new": 0}
        deep = ctx.cfg.re_chapter_path.pattern
        self.re_deep = re.compile(f"(?:{deep})|{DEEP_EXTRA}", re.I)

    # ───────────────────────── frontier ─────────────────────────
    def push(self, url: str, hop: int, prio: float, via: str, force: bool = False) -> bool:
        R, cfg = self.R, self.ctx.cfg
        host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
        dom = ekstrak_domain_aman(cfg, host, self.ctx.allowlist)
        if not dom:
            return False
        if not force:
            sd = self.seen.get(dom)
            if sd:
                bad = sd.get("k") in ("dead", "blocked", "transient", "robots")
                lim = R.get("dead_revisit_days", 14) if bad else R.get("revisit_days", 30)
                if self.ts - sd.get("t", 0) < lim * DAY:
                    return False
        if self.run_pages.get(dom, 0) >= R.get("max_pages_per_domain", 3):
            return False
        if len(self.frontier) >= R.get("frontier_max", 30000) * 2:
            return False
        cur = self.frontier.get(url)
        if cur is not None:
            if prio <= cur["p"]:
                return False
            cur["p"], cur["h"] = prio, min(cur["h"], hop)
        else:
            self.frontier[url] = {"h": hop, "p": prio, "v": via, "d": dom}
        heapq.heappush(self.heap, (-prio, next(self.seq), url))
        return True

    def pop_task(self, inflight_hosts: set[str]):
        delay = self.R.get("per_host_delay", 1.5)
        deferred, found, scanned = [], None, 0
        while self.heap and scanned < 300:
            negp, seq, url = heapq.heappop(self.heap)
            scanned += 1
            meta = self.frontier.get(url)
            if meta is None or meta["p"] != -negp:
                continue  # entri basi (prioritas sudah dinaikkan / sudah diambil)
            host = urllib.parse.urlparse(url).netloc.lower()
            if self.ctx.http.is_tripped(host):
                del self.frontier[url]
                continue
            if host in inflight_hosts or time.monotonic() - self.last_hit.get(host, 0) < delay:
                deferred.append((negp, seq, url))
                continue
            found = (url, meta)
            break
        for d in deferred:
            heapq.heappush(self.heap, d)
        if found:
            del self.frontier[found[0]]
        return found

    # ───────────────────────── pencarian ─────────────────────────
    def ingest_hit(self, h, q: str) -> None:
        cfg = self.ctx.cfg
        p = urllib.parse.urlparse(h.url)
        dom = ekstrak_domain_aman(cfg, p.netloc.lower().removeprefix("www."), self.ctx.allowlist)
        if not dom:
            return
        via = f"q:{q[:50]}"
        rec = self.index.get(dom)
        if rec is not None and via not in rec["via"]:  # bukti tambahan untuk domain yang sudah dikenal
            rec["via"] = (rec["via"] + [via])[-5:]
            rec["n"] += 1
        pre = self.cls.scores(dom, Page(h.title, h.snippet, "", ""))
        bonus = min(max(pre.values(), default=0), 15)
        self.push(h.url, 0, 100 + bonus, via)
        root = f"{p.scheme}://{p.netloc}/"
        if root != h.url:
            self.push(root, 0, 90 + bonus, via)

    def search(self, queries: list[tuple[str, str]], deadline: Deadline) -> None:
        R = self.R
        lang = (self.icfg.get("engines") or {}).get("language", "id")
        for cat, q in queries:
            if deadline.expired(10):
                break
            got = 0
            for page in range(1, R.get("search_pages", 2) + 1):
                hits = self.pool.search(self.ctx.http, q, page, deadline, lang)
                if not hits:
                    break
                for h in hits:
                    self.ingest_hit(h, q)
                got += len(hits)
                polite(self.ctx, deadline, 1.0)
            self.qdone[q] = int(self.ts)
            self.stats["queries"] += 1
            self.stats["search_hits"] += got
            log("SEARCH", f"[{cat}] {q[:60]!r}: {got} hasil")
            polite(self.ctx, deadline, 1.5)

    # ───────────────────────── fetch (di thread pekerja) ─────────────────────────
    def robots_ok(self, p: urllib.parse.ParseResult) -> bool:
        host = p.netloc.lower()
        rp = self.robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            r = self.ctx.http.get(f"{p.scheme}://{host}/robots.txt", timeout=8, retries=0,
                                  max_bytes=200_000, deadline=self.deadline)
            rp.parse(r.text.splitlines() if r.ok and r.text else [])
            self.robots[host] = rp
        try:
            return rp.can_fetch("*", p.geturl())
        except Exception:
            return True

    def extract_links(self, raw: str, base: str, site_dom: str) -> tuple[dict, list]:
        cfg, allow = self.ctx.cfg, self.ctx.allowlist
        ext: dict[str, int] = {}
        internal: list[str] = []
        cache: dict[str, str | None] = {}
        chunk = raw[:600_000]
        for href in RE_HREF.findall(chunk)[:1500] + RE_DATA_URL.findall(chunk)[:100]:
            u = urllib.parse.urljoin(base, html.unescape(href))
            p = urllib.parse.urlparse(u)
            if p.scheme not in ("http", "https") or not p.netloc:
                continue
            host = p.netloc.lower().removeprefix("www.")
            if host not in cache:
                cache[host] = ekstrak_domain_aman(cfg, host, allow)
            d = cache[host]
            if d == site_dom:
                internal.append(u)
            elif d:
                ext[d] = ext.get(d, 0) + 1
        return ext, internal

    def fetch(self, task: dict) -> dict:
        R = self.R
        p = urllib.parse.urlparse(task["url"])
        if R.get("respect_robots", True) and not self.robots_ok(p):
            return {"task": task, "kind": "robots"}
        r = self.ctx.http.get(task["url"], timeout=R.get("fetch_timeout", 12), retries=0,
                              max_bytes=R.get("body_max_bytes", 400000), deadline=self.deadline)
        if not r.ok:
            return {"task": task, "kind": r.kind}
        page = extract_page(r.text)
        return {"task": task, "kind": "ok", "final": r.url, "title": page.title,
                "scores": self.cls.scores(task["dom"], page),
                "links": self.extract_links(r.text, r.url, task["dom"])}

    # ───────────────────────── serap hasil (thread utama) ─────────────────────────
    def record(self, dom: str, cat: str, scores: dict, title: str, via: str) -> None:
        cats = dict(sorted(scores.items(), key=lambda kv: -kv[1])[:3])
        rec = self.index.get(dom)
        if rec is None:
            self.index[dom] = {"cat": cat, "s": scores[cat], "cats": cats, "title": title[:100],
                               "via": [via], "n": 1, "first": int(self.ts), "last": int(self.ts)}
            self.stats["relevant_new"] += 1
            self.stats["new_" + cat] = self.stats.get("new_" + cat, 0) + 1
            self.new_domains.append(dom)
            return
        if scores[cat] >= rec["s"]:
            rec.update(cat=cat, s=scores[cat], title=title[:100] or rec["title"])
        merged = dict(rec["cats"])
        for c, sc in cats.items():           # simpan skor TERTINGGI per kategori dari semua halaman
            merged[c] = max(merged.get(c, 0), sc)
        rec["cats"] = dict(sorted(merged.items(), key=lambda kv: -kv[1])[:3])
        rec["last"] = int(self.ts)
        if via not in rec["via"]:
            rec["via"] = (rec["via"] + [via])[-5:]
            rec["n"] += 1

    def follow(self, dom: str, res: dict, t: dict, top: int) -> None:
        R = self.R
        ext, internal = res["links"]
        hop = t["hop"]
        if hop < R.get("max_hops", 2):
            ranked = []
            for d2 in ext:
                pr = 60 - 20 * hop + min(top, 20)
                if self.cls.domain_hint(d2):
                    pr += 20
                if similar(dom, d2):
                    pr += 25   # kemungkinan mirror
                ranked.append((pr, d2))
            ranked.sort(reverse=True)
            for pr, d2 in ranked[:R.get("max_out_per_page", 40)]:
                self.push(f"https://{d2}/", hop + 1, pr, via=dom)
        left = R.get("max_pages_per_domain", 3) - self.run_pages.get(dom, 0)
        if left > 0 and internal:
            seen_paths, cand = set(), []
            for u in internal:
                path = urllib.parse.urlparse(u).path
                if path in ("", "/") or path in seen_paths or u == t["url"]:
                    continue
                seen_paths.add(path)
                cand.append((bool(self.re_deep.search(path)), random.random(), u))
            cand.sort(reverse=True)
            for _, _, u in cand[:left]:
                self.push(u, hop, 55, via=dom, force=True)

    def absorb(self, res: dict) -> None:
        t, kind = res["task"], res["kind"]
        dom = t["dom"]
        self.stats["fetched"] += 1
        sd = self.seen.setdefault(dom, {})
        sd["t"] = int(self.ts)
        if kind != "ok":
            sd["k"] = "dead" if kind == "gone" else kind
            self.stats["fail_" + kind] = self.stats.get("fail_" + kind, 0) + 1
            return
        sd["k"] = "ok"
        self.stats["ok"] += 1
        self.run_pages[dom] = self.run_pages.get(dom, 0) + 1
        scores = res["scores"]
        cat, _ = self.cls.best(scores)
        top = max(scores.values(), default=0)
        sd["c"], sd["s"] = cat or "", top
        if cat:
            self.record(dom, cat, scores, res["title"], t["via"])
        if top >= self.R.get("follow_min_score", 4):
            self.follow(dom, res, t, top)

    # ───────────────────────── crawl ─────────────────────────
    def crawl(self, deadline: Deadline) -> None:
        R = self.R
        self.deadline = deadline
        workers = int(R.get("workers", 12))
        ex = ThreadPoolExecutor(max_workers=workers)
        inflight: dict = {}
        hosts: set[str] = set()
        last_log = time.monotonic()
        try:
            while not deadline.expired(3):
                while len(inflight) < workers * 2:
                    t = self.pop_task(hosts)
                    if t is None:
                        break
                    url, meta = t
                    host = urllib.parse.urlparse(url).netloc.lower()
                    task = {"url": url, "dom": meta["d"], "hop": meta["h"], "via": meta["v"], "host": host}
                    self.last_hit[host] = time.monotonic()
                    hosts.add(host)
                    inflight[ex.submit(self.fetch, task)] = task
                if not inflight:
                    if not self.heap:
                        break          # frontier habis
                    time.sleep(0.3)    # semua tertunda jeda per-host
                    continue
                done, _ = wait(list(inflight), timeout=3, return_when=FIRST_COMPLETED)
                for f in done:
                    task = inflight.pop(f)
                    hosts.discard(task["host"])
                    try:
                        self.absorb(f.result())
                    except Exception as e:  # satu halaman rusak tak boleh menghentikan crawl
                        self.stats["errors"] = self.stats.get("errors", 0) + 1
                        if self.stats["errors"] <= 5:
                            log("ERROR", f"{task['url'][:70]}: {e!r}")
                if time.monotonic() - last_log > 30:
                    last_log = time.monotonic()
                    log("CRAWL", f"fetched={self.stats['fetched']} ok={self.stats['ok']} "
                                 f"relevan_baru={self.stats['relevant_new']} frontier={len(self.frontier)} "
                                 f"sisa={int(deadline.left())}s")
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    # ───────────────────────── keluaran ─────────────────────────
    def top_brands(self, n: int) -> list[str]:
        """Label domain situs relevan terbaik (untuk perburuan mirror)."""
        rows = sorted(self.index.items(), key=lambda kv: (-kv[1]["s"], -kv[1]["n"]))
        labels = []
        for d, _ in rows:
            lab = d.split(".")[0]
            if len(lab) >= 4 and lab not in labels:
                labels.append(lab)
            if len(labels) >= n * 3:
                break
        random.shuffle(labels)
        return labels[:n]

    def save(self, dry: bool) -> None:
        R = self.R
        fm = R.get("frontier_max", 30000)
        if len(self.frontier) > fm:
            keep = sorted(self.frontier.items(), key=lambda kv: -kv[1]["p"])[:fm]
            self.frontier.clear()
            self.frontier.update(keep)
        sm = R.get("seen_max", 100000)
        if len(self.seen) > sm:
            keep = sorted(self.seen.items(), key=lambda kv: -kv[1].get("t", 0))[:sm]
            self.seen.clear()
            self.seen.update(keep)
        im = R.get("index_max", 60000)
        if len(self.index) > im:
            keep = sorted(self.index.items(), key=lambda kv: (-kv[1]["s"], -kv[1]["last"]))[:im]
            self.index.clear()
            self.index.update(keep)
        if len(self.qdone) > 20000:
            keep = sorted(self.qdone.items(), key=lambda kv: -kv[1])[:20000]
            self.qdone.clear()
            self.qdone.update(keep)
        for s in (self.s_front, self.s_seen, self.s_index, self.s_q):
            s.save(dry)

    def export(self, dry: bool) -> dict:
        """Tulis Candidates/<kategori>.txt (kumulatif) + Candidates/index.json (metadata untuk radar)."""
        known, R = self.ctx.known, self.R
        out = {"exported": 0, "skipped_known": 0}
        old_meta: dict = {}
        idx_path = CAND_DIR / "index.json"
        if idx_path.exists():
            try:
                old_meta = json.loads(idx_path.read_text(encoding="utf-8")).get("domains", {})
            except (ValueError, OSError):
                old_meta = {}
        rows = dict(old_meta)
        for d, r in self.index.items():
            rows[d] = {"cat": r["cat"], "s": r["s"], "cats": r["cats"], "title": r["title"],
                       "n": r["n"], "via": r["via"][:3], "last": r["last"]}
        keep = {}
        for d, r in rows.items():
            if punya_induk(d, known):
                out["skipped_known"] += 1
            else:
                keep[d] = r
        top = dict(sorted(keep.items(), key=lambda kv: (-kv[1]["s"], -kv[1].get("last", 0)))[:R.get("export_max", 40000)])
        out["exported"] = len(top)
        if dry:
            return out
        CAND_DIR.mkdir(parents=True, exist_ok=True)
        by_cat: dict[str, list[str]] = {}
        for d, r in top.items():
            by_cat.setdefault(r["cat"], []).append(d)
        for cat, lst in by_cat.items():
            path = CAND_DIR / f"{cat}.txt"
            merged = sorted(set(lst))
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("\n".join(merged) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            out["cat_" + cat] = len(merged)
        for p in CAND_DIR.glob("*.txt"):   # kategori yang sudah kosong
            if p.stem not in by_cat:
                p.unlink()
        tmp = idx_path.with_name("index.json.tmp")
        tmp.write_text(json.dumps({"version": 1, "generated": int(self.ts), "domains": top},
                                  ensure_ascii=False, indent=0, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, idx_path)
        return out
      
