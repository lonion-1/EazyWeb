# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mesin pencari: SearXNG (JSON) + cadangan HTML (Mojeek / DuckDuckGo / Bing) tanpa API key."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.parse
from dataclasses import dataclass

from radar.core import Deadline, Http, bersihkan_html, log

RE_A = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', re.I)


@dataclass
class Hit:
    url: str
    title: str = ""
    snippet: str = ""
    engine: str = ""


def decode_redirect(url: str) -> str:
    """Bongkar tautan pengalihan mesin pencari (DDG /l/?uddg=, Bing /ck/a?u=a1.., Google /url?q=)."""
    u = html.unescape(url)
    if u.startswith("//"):
        u = "https:" + u
    p = urllib.parse.urlparse(u)
    host, q = p.netloc.lower(), urllib.parse.parse_qs(p.query)
    if host.endswith("duckduckgo.com") and p.path.startswith("/l/") and "uddg" in q:
        return urllib.parse.unquote(q["uddg"][0])
    if host.endswith("bing.com") and p.path.startswith("/ck/") and q.get("u", [""])[0].startswith("a1"):
        b = q["u"][0][2:]
        b += "=" * (-len(b) % 4)
        try:
            return base64.urlsafe_b64decode(b).decode("utf-8", "ignore")
        except (ValueError, UnicodeError):
            return ""
    if host.endswith("google.com") and p.path == "/url" and "q" in q:
        return q["q"][0]
    return u


class SearxngEngine:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.name = "searxng@" + urllib.parse.urlparse(self.base).netloc

    def search(self, http: Http, q: str, page: int, deadline: Deadline, lang: str) -> tuple[str, list[Hit]]:
        url = (f"{self.base}/search?q={urllib.parse.quote_plus(q)}&format=json&pageno={page}"
               f"&language={lang}&safesearch=0&categories=general")
        r = http.get(url, timeout=25, retries=1, deadline=deadline)
        if not r.ok:
            return r.kind, []
        try:
            data = json.loads(r.text)
        except ValueError:
            return "blocked", []   # biasanya: JSON dimatikan, halaman HTML yang kembali
        hits = [Hit(x["url"], x.get("title", "") or "", x.get("content", "") or "", x.get("engine", ""))
                for x in data.get("results", []) if isinstance(x, dict) and x.get("url")]
        return "ok", hits


class HtmlEngine:
    def __init__(self, name: str, template: str):
        self.name, self.template = name, template
        host = urllib.parse.urlparse(template).netloc.lower()
        self.root = ".".join(host.split(".")[-2:])

    def search(self, http: Http, q: str, page: int, deadline: Deadline, lang: str) -> tuple[str, list[Hit]]:
        if page > 1:
            return "ok", []
        r = http.get(self.template.replace("{q}", urllib.parse.quote_plus(q)), timeout=20, retries=1, deadline=deadline)
        if not r.ok:
            return r.kind, []
        seen, hits = set(), []
        for href, inner in RE_A.findall(r.text):
            u = decode_redirect(href)
            host = urllib.parse.urlparse(u).netloc.lower()
            if not u.startswith(("http://", "https://")) or not host or host.endswith(self.root) or u in seen:
                continue
            seen.add(u)
            hits.append(Hit(u, bersihkan_html(inner)[:150], "", self.name))
        return "ok", hits


class EnginePool:
    """Putar mesin per query; mesin yang gagal berturut-turut dinonaktifkan sementara."""

    def __init__(self, engines: list):
        self.engines = engines
        self.fails = {e.name: 0 for e in engines}
        self.turn = 0

    def alive(self) -> list:
        return [e for e in self.engines if self.fails[e.name] < 3]

    def search(self, http: Http, q: str, page: int, deadline: Deadline, lang: str) -> list[Hit]:
        live = self.alive()
        if not live:
            return []
        self.turn += 1
        order = live[self.turn % len(live):] + live[:self.turn % len(live)]
        for e in order:
            kind, hits = e.search(http, q, page, deadline, lang)
            if kind == "ok":
                self.fails[e.name] = 0
                if hits:
                    return hits
            else:
                self.fails[e.name] += 1
                if self.fails[e.name] == 3:
                    log("ENGINE", f"{e.name} gagal 3x berturut-turut → dinonaktifkan sisa run")
        return []


def build_pool(icfg: dict, only: set[str] | None = None) -> EnginePool:
    ec = icfg.get("engines") or {}
    engines: list = []
    urls = [u for u in ec.get("searxng", [])]
    urls += [u.strip() for u in os.environ.get("SEARXNG_URL", "").split(",") if u.strip()]
    for u in dict.fromkeys(urls):
        engines.append(SearxngEngine(u))
    tmpl = ec.get("templates") or {}
    for name in ec.get("fallback", []):
        if name in tmpl:
            engines.append(HtmlEngine(name, tmpl[name]))
    if only:
        engines = [e for e in engines if e.name.split("@")[0] in only]
    return EnginePool(engines)
