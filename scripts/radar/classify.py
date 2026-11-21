# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pengklasifikasi halaman per kategori (film/porn/comic/anime/judol).
Dipakai bersama oleh indexer (cari situs) dan kurasi radar (verifikasi ulang).
Hanya membaca judul, meta, h1 dan cuplikan teks — isi halaman TIDAK disimpan."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .core import ConfigError, _compile, _literals, bersihkan_html, norm_text

RE_TITLE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.I)
RE_META = re.compile(r"<meta\b[^>]*>", re.I)
RE_META_NAME = re.compile(
    r'(?:name|property)=["\'](?:description|keywords|og:title|og:description|og:site_name)["\']', re.I)
RE_META_CONTENT = re.compile(r'content=["\']([^"\']*)["\']', re.I)
RE_H1 = re.compile(r"<h[12][^>]*>([\s\S]*?)</h[12]>", re.I)


@dataclass
class Page:
    title: str
    meta: str
    h1: str
    body: str


def extract_page(raw: str, max_body: int = 20000) -> Page:
    raw = raw or ""
    t = RE_TITLE.search(raw)
    metas = []
    for tag in RE_META.findall(raw[:200_000]):
        if RE_META_NAME.search(tag):
            m = RE_META_CONTENT.search(tag)
            if m:
                metas.append(m.group(1))
    h1 = " ".join(bersihkan_html(x) for x in RE_H1.findall(raw[:300_000])[:6])
    return Page(bersihkan_html(t.group(1))[:200] if t else "",
                bersihkan_html(" ".join(metas))[:600], h1[:300],
                bersihkan_html(raw)[:max_body])


@dataclass
class Cat:
    name: str
    label: str
    re_domain: re.Pattern
    re_strong: re.Pattern
    re_weak: re.Pattern
    re_neg: re.Pattern
    min_score: int
    curate_score: int


class Classifier:
    def __init__(self, cats: dict[str, Cat]):
        self.cats = cats

    def domain_hint(self, dom: str) -> bool:
        return any(c.re_domain.search(dom) for c in self.cats.values())

    def scores(self, host: str, page: Page) -> dict[str, int]:
        head = norm_text(f"{page.title} {page.meta} {page.h1}").lower()
        body = norm_text(page.body).lower()
        out: dict[str, int] = {}
        for name, c in self.cats.items():
            s = 2 if c.re_domain.search(host) else 0
            hs = {m.group(0) for m in c.re_strong.finditer(head)}
            s += 3 * min(3, len(hs))
            s += min(4, len({m.group(0) for m in c.re_strong.finditer(body)} - hs))
            s += min(4, len({m.group(0) for m in c.re_weak.finditer(body)}))
            neg = {m.group(0) for m in c.re_neg.finditer(head + " " + body)}
            s -= 2 * min(2, len(neg))
            if s > 0:
                out[name] = s
        return out

    def best(self, scores: dict[str, int]) -> tuple[str | None, int]:
        """Kategori terbaik yang lolos min_score (atau (None, skor tertinggi))."""
        ok = [(s, n) for n, s in scores.items() if s >= self.cats[n].min_score]
        if ok:
            s, n = max(ok)
            return n, s
        return None, max(scores.values(), default=0)


def build_classifier(cfg, icfg: dict) -> Classifier:
    cats: dict[str, Cat] = {}
    for name, c in (icfg.get("categories") or {}).items():
        lab = f"categories.{name}"
        hints = _literals(c.get("domain_hint", []), f"{lab}.domain_hint", False)
        re_domain = _compile(hints, f"{lab}.domain_hint") if hints else (
            cfg.re_strong if name == "judol" else re.compile(r"(?!x)x"))
        wrap = dict(prefix=r"(?<!\w)", suffix=r"(?!\w)")
        if not c.get("strong"):
            raise ConfigError(f"{lab}.strong kosong")
        min_score = int(c.get("min_score", 7))
        cats[name] = Cat(
            name, str(c.get("label", name)), re_domain,
            _compile(c.get("strong", []), f"{lab}.strong", **wrap),
            _compile(c.get("weak", []), f"{lab}.weak", **wrap),
            _compile(c.get("negative", []), f"{lab}.negative", **wrap),
            min_score, int(c.get("curate_score", min_score + 3)))
    return Classifier(cats)
          
