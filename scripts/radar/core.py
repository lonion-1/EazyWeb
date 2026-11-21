# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inti bersama Radar Judol: config, HTTP tangguh, keamanan domain, state, skor."""
from __future__ import annotations

import html
import http.client
import json
import os
import random
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

# ──────────────────────────── Path ────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / ".github" / "radar_state"
LEGACY_STATE = ROOT / ".github" / "judol_radar_state.json"
FILE_TARGET = ROOT / "EazyBlock.txt"
GRADUATED_FILE = ROOT / "Graduated.txt"
ALLOWLIST_FILE = ROOT / "Allowlist.txt"

DAY = 86400


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def now() -> float:
    return time.time()


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────── Tuning bawaan ────────────────────────────
DEFAULTS: dict = {
    "time_budget_seconds": 660, "finalize_reserve_seconds": 120,
    "polite_sleep": 1.0, "http_trip_after": 4,
    "promote_at": 3, "score_strong_domain": 3, "score_moderate_domain": 1,
    "score_context": 1, "score_banner": 1, "score_page_confirm": 3,
    "confirm_min_score": 1, "page_confirm_hits": 2,
    "page_confirm_max_per_run": 40, "page_recheck_days": 7,
    "max_new_domains_per_run": 250, "max_promoted_per_source": 40,
    "pending_ttl_days": 21, "pending_max": 3000, "dns_fail_drop_days": 4,
    "dns_workers": 16, "dns_timeout_seconds": 8,
    "retire_hard_days": 4, "retire_soft_days": 14, "max_cooldown_hours": 72,
    "parole_days": 30, "reject_recheck_days": 30,
    "tg_channels_per_run": 30, "tg_max_channels": 120, "tg_max_pages": 4,
    "tg_max_dry_runs": 20, "tg_verify_per_run": 20,
    "tg_discovery_queries_per_run": 4, "tg_require_indonesian": True,
    "tg_queue_max": 500,
    "comic_sites_per_run": 25, "comic_max_sites": 80, "comic_verify_per_run": 8,
    "comic_deep_pages": 2, "comic_verify_hits": 8,
    "crt_batch_per_run": 6, "crt_max_entries": 400, "crt_max_age_days": 120,
    "crt_timeout": 45,
}


class ConfigError(Exception):
    pass


# ──────────────────────────── Config ────────────────────────────
class Config:
    """Hasil muat semua file config/*.toml (regex sudah terkompilasi)."""

    T: dict
    warnings: list


def _toml(name: str) -> dict:
    p = CONFIG_DIR / name
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config/{name} tidak ditemukan")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config/{name} salah format TOML: {e}")


def _compile(parts, label: str, prefix: str = "", suffix: str = "") -> re.Pattern:
    good = []
    for p in parts:
        p = str(p)
        if not p:
            continue
        try:
            re.compile(p, re.I)
        except re.error as e:
            raise ConfigError(f"{label}: regex rusak {p!r} ({e})")
        good.append(f"(?:{p})")
    if not good:
        return re.compile(r"(?!x)x")  # tak pernah cocok
    try:
        return re.compile(f"{prefix}(?:{'|'.join(good)}){suffix}", re.I)
    except re.error as e:
        raise ConfigError(f"{label}: gabungan regex gagal ({e}); jangan pakai (?i)")


def _literals(words, label: str, anchor_short: bool) -> list[str]:
    out = []
    for w in words:
        w = str(w).strip().lower()
        if not w:
            continue
        if re.search(r"\s", w):
            raise ConfigError(f"{label}: kata '{w}' mengandung spasi (domain tak boleh berspasi)")
        e = re.escape(w)
        out.append(rf"(?:^|[-.]){e}" if anchor_short and len(w) <= 4 else e)
    return out


def load_config() -> Config:
    if tomllib is None:
        raise ConfigError("Butuh Python 3.11+ (modul tomllib).")
    kw, seeds, qs = _toml("keywords.toml"), _toml("seeds.toml"), _toml("queries.toml")
    sf, tn = _toml("safety.toml"), _toml("tuning.toml")
    c = Config()
    c.warnings = []

    # tuning
    c.T = dict(DEFAULTS)
    for k, v in tn.items():
        if k not in DEFAULTS:
            c.warnings.append(f"tuning.toml: kunci tak dikenal '{k}' (typo?)")
            continue
        d = DEFAULTS[k]
        if isinstance(d, bool):
            if not isinstance(v, bool):
                raise ConfigError(f"tuning.toml: '{k}' harus true/false")
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ConfigError(f"tuning.toml: '{k}' harus angka")
        c.T[k] = v

    # kata kunci
    j, cm, lg = kw.get("judol", {}), kw.get("comic", {}), kw.get("language", {})
    strong_lit = _literals(j.get("strong", []), "judol.strong", True)
    if not strong_lit and not j.get("strong_regex"):
        raise ConfigError("keywords.toml: [judol].strong kosong — radar tak punya pola apa pun")
    c.re_strong = _compile(strong_lit + list(j.get("strong_regex", [])), "judol.strong")
    c.re_moderate = _compile(_literals(j.get("moderate", []), "judol.moderate", False), "judol.moderate")
    c.re_ctx_judol = _compile(j.get("context_regex", []), "judol.context_regex", r"\b", r"\b")
    c.re_banner = _compile([re.escape(str(w).lower()) for w in j.get("banner", [])],
                           "judol.banner", r"(?<![a-z0-9])")
    c.re_comic_hint = _compile(_literals(cm.get("site_hint", []), "comic.site_hint", False), "comic.site_hint")
    c.re_comic_ctx = _compile(cm.get("context_regex", []), "comic.context_regex", r"\b", r"\b")
    c.re_chapter_path = _compile(cm.get("chapter_path", []), "comic.chapter_path")
    c.re_indo = _compile([re.escape(str(w).lower()) for w in lg.get("indonesian_hint", [])],
                         "language.indonesian_hint", r"\b", r"\b")

    # pagar pengaman
    c.protected = {str(x).lower() for x in sf.get("protected_domains", [])}
    c.protected_suffixes = tuple(str(x).lower() for x in sf.get("protected_tld_suffixes", []))
    c.free_hosts = {str(x).lower() for x in sf.get("free_host_suffixes", [])}
    c.multi_tlds = {str(x).lower() for x in sf.get("multi_part_tlds", [])}
    c.reserved_tg = {str(x).lower() for x in sf.get("reserved_tg_names", [])}
    tlds = sorted({str(x).lower().lstrip(".") for x in sf.get("bare_tlds", []) if x}, key=len, reverse=True)
    c.bare_tlds = tlds
    if tlds:
        alt = "|".join(re.escape(t) for t in tlds)
        c.re_bare = re.compile(
            rf"(?<![\w@/.:-])((?:[a-z0-9][a-z0-9-]{{1,61}}\.)+(?:{alt}))(?![\w-])", re.I)
        c.re_dot_word = re.compile(rf"(?<=\w)\s+(?:dot|titik)\s+(?=(?:{alt})\b)", re.I)
    else:
        c.re_bare = re.compile(r"(?!x)x")
        c.re_dot_word = re.compile(r"(?!x)x")

    # seed
    c.seeds_tg = {}
    for name, kind in (seeds.get("telegram") or {}).items():
        if kind not in ("comic", "judol"):
            raise ConfigError(f"seeds.toml: kanal '{name}' jenis harus \"comic\" atau \"judol\"")
        c.seeds_tg[str(name)] = kind
    c.seeds_comic = [str(u).strip() for u in (seeds.get("comic_sites") or {}).get("urls", []) if str(u).strip()]

    # query
    c.crt_queries = {r: [str(q) for q in lst] for r, lst in ((qs.get("crt") or {}).get("queries") or {}).items()}
    disc = qs.get("telegram_discovery") or {}
    c.tg_disc = {"judol": list(disc.get("judol", [])), "comic": list(disc.get("comic", []))}
        c.cache_sources = [x for x in qs.get("cache", []) if x.get("url")]
    c.feed_sources = [
        x for x in (list(qs.get("whaleindex_feeds", [])) + list(qs.get("web_sources", [])))
        if isinstance(x, dict) and x.get("url")
    ]
    return c


# ──────────────────────────── Util teks ────────────────────────────
RE_DOMAIN = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:xn--[a-z0-9-]{1,59}|[a-z]{2,63})$"
)
RE_URL_EXTRACT = re.compile(r"(?:https?://|www\.)([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)")


def bersihkan_html(raw: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_text(s: str) -> str:
    """NFKC: huruf 'fancy' 𝗦𝗟𝗢𝗧 / ｓｌｏｔ jadi ASCII biasa."""
    return unicodedata.normalize("NFKC", s or "")


def defang(cfg: Config, s: str) -> str:
    s = re.sub(r"\[\s*\.\s*\]|\(\s*\.\s*\)|\{\s*\.\s*\}", ".", s)
    s = cfg.re_dot_word.sub(".", s)
    return re.sub(r"hxxp", "http", s, flags=re.I)


def extract_hosts(cfg: Config, text: str) -> set[str]:
    t = defang(cfg, norm_text(text))
    hosts = {m.group(1) for m in RE_URL_EXTRACT.finditer(t)}
    hosts.update(m.group(1) for m in cfg.re_bare.finditer(t))
    return hosts


def ekstrak_domain_aman(cfg: Config, raw_host: str, allowlist=frozenset()) -> str | None:
    """Host mentah → domain induk yang aman diblokir (atau None)."""
    h = (raw_host or "").strip().lower().rsplit("@", 1)[-1].lstrip("*.")
    h = re.split(r"[/?#:^*\s]", h, maxsplit=1)[0].strip(".")
    if not h:
        return None
    try:
        h = h.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if not RE_DOMAIN.match(h) or h.endswith(cfg.protected_suffixes):
        return None
    parts = h.split(".")
    if len(parts) < 2:
        return None
    s2 = ".".join(parts[-2:])
    s3 = ".".join(parts[-3:]) if len(parts) >= 3 else ""
    if s2 in cfg.free_hosts:
        if len(parts) < 3 or parts[-3] in {"www", "m"}:
            return None
        target = ".".join(parts[-3:])
    elif s3 in cfg.free_hosts:
        if len(parts) < 4 or parts[-4] in {"www", "m"}:
            return None
        target = ".".join(parts[-4:])
    elif s2 in cfg.multi_tlds:
        if len(parts) < 3:
            return None
        target = ".".join(parts[-3:])
    else:
        target = s2
    guard = cfg.protected | set(allowlist)
    if target in guard or any(target.endswith("." + p) for p in guard):
        return None
    return target if RE_DOMAIN.match(target) else None


def punya_induk(domain: str, cache: set[str]) -> bool:
    if domain in cache:
        return True
    parts = domain.split(".")
    return any(".".join(parts[i:]) in cache for i in range(1, len(parts) - 1))


def score_domain(cfg: Config, dom: str, *, context: bool = False, banner: bool = False) -> tuple[int, str]:
    """Skor kecurigaan sebuah domain. context=teks postingan judol; banner=teks banner judol."""
    T = cfg.T
    if cfg.re_strong.search(dom):
        return T["score_strong_domain"], "strong"
    moderate = bool(cfg.re_moderate.search(dom))
    pts, note = 0, ""
    if banner:
        pts += T["score_banner"] + (T["score_moderate_domain"] if moderate else 0)
        note = "banner"
    elif context and moderate:
        pts += T["score_moderate_domain"] + T["score_context"]
        note = "context+moderate"
    return pts, note


# ──────────────────────────── Deadline ────────────────────────────
class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + max(0.0, seconds)

    def left(self) -> float:
        return self.end - time.monotonic()

    def expired(self, margin: float = 0.0) -> bool:
        return self.left() <= margin

    def sub(self, seconds: float) -> "Deadline":
        return Deadline(min(seconds, self.left()))


def polite(ctx: "Ctx", deadline: Deadline | None, base: float | None = None) -> None:
    base = ctx.cfg.T["polite_sleep"] if base is None else base
    wait = base + random.uniform(0, base * 0.6)
    if deadline is not None:
        wait = min(wait, max(0.0, deadline.left()))
    if wait > 0:
        time.sleep(wait)


# ──────────────────────────── HTTP ────────────────────────────
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]
_NX = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)}


@dataclass
class Resp:
    status: int
    text: str | None
    url: str
    # ok | gone (404/410/NXDOMAIN) | blocked (401/403/429/451) | transient (5xx/timeout) | skipped
    kind: str
    retry_after: float = 0.0

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


class Http:
    def __init__(self, T: dict):
        self.T = T
        self.streak: dict[str, int] = {}
        self.tripped: set[str] = set()

    def is_tripped(self, host: str) -> bool:
        return host in self.tripped

    def _success(self, host: str) -> None:
        self.streak[host] = 0

    def _failure(self, host: str) -> None:
        self.streak[host] = self.streak.get(host, 0) + 1
        if self.streak[host] >= self.T["http_trip_after"] and host not in self.tripped:
            self.tripped.add(host)
            log("BREAKER", f"{host} gagal {self.streak[host]}x berturut-turut → dilewati sisa run ini")

    def _once(self, url: str, timeout: float, max_bytes: int) -> Resp:
        req = urllib.request.Request(url, headers={
            "User-Agent": random.choice(UA_POOL),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(max_bytes + 1)[:max_bytes]
                cs = r.headers.get_content_charset() or "utf-8"
                try:
                    text = raw.decode(cs, errors="ignore")
                except LookupError:
                    text = raw.decode("utf-8", errors="ignore")
                return Resp(getattr(r, "status", 200), text, r.geturl() or url, "ok")
        except urllib.error.HTTPError as e:
            st = e.code
            ra = 0.0
            try:
                ra = min(float(e.headers.get("Retry-After", "0")), 30.0)
            except (TypeError, ValueError):
                pass
            if st in (404, 410):
                kind = "gone"
            elif st >= 500 or st in (408, 425):
                kind = "transient"
            else:
                kind = "blocked"
            return Resp(st, None, url, kind, ra)
        except urllib.error.URLError as e:
            r = e.reason
            if isinstance(r, socket.gaierror) and r.errno in _NX:
                return Resp(0, None, url, "gone")
            return Resp(0, None, url, "transient")
        except (OSError, http.client.HTTPException):
            return Resp(0, None, url, "transient")
        except ValueError:
            return Resp(0, None, url, "gone")

    def get(self, url: str, *, timeout: float = 20, retries: int = 1,
            max_bytes: int = 3_000_000, deadline: Deadline | None = None) -> Resp:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host in self.tripped:
            return Resp(0, None, url, "skipped")
        last = Resp(0, None, url, "skipped")
        for attempt in range(retries + 1):
            if deadline is not None and deadline.left() < min(timeout, 6):
                return last
            resp = self._once(url, timeout, max_bytes)
            if resp.kind == "ok":
                self._success(host)
                return resp
            if resp.kind == "gone":
                return resp  # 404 bukan salah host
            last = resp
            if resp.kind == "blocked" and resp.status != 429:
                break  # 401/403/451 tak berguna diulang
            if attempt < retries:
                wait = min(resp.retry_after or (2 ** attempt) * 1.5, 20) + random.uniform(0, 0.8)
                if deadline is not None and deadline.left() < wait + timeout:
                    break
                time.sleep(wait)
        self._failure(host)
        return last


# ──────────────────────────── DNS ────────────────────────────
def dns_state(domain: str) -> bool | None:
    """True=resolve, False=NXDOMAIN pasti, None=tak tahu (timeout/SERVFAIL)."""
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror as e:
        return False if e.errno in _NX else None
    except OSError:
        return None


def dns_many(domains: list[str], workers: int, per_timeout: float, deadline: Deadline) -> dict[str, bool | None]:
    res: dict[str, bool | None] = {}
    if not domains:
        return res
    total = max(5.0, min(deadline.left(), per_timeout * (len(domains) / max(1, workers) + 1)))
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    futs = {ex.submit(dns_state, d): d for d in domains}
    try:
        for f in as_completed(futs, timeout=total):
            res[futs[f]] = f.result()
    except FutureTimeout:
        log("DNS", "batas waktu — sisa domain dicek di run berikutnya")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    for d in domains:
        res.setdefault(d, None)
    return res


# ──────────────────────────── State ────────────────────────────
class Store:
    """JSON state dengan tulis-atomik & pemulihan bila file rusak."""

    def __init__(self, name: str, default: dict):
        self.path = STATE_DIR / f"{name}.json"
        self.data = json.loads(json.dumps(default))
        self.existed = False

    def load(self) -> "Store":
        if not self.path.exists():
            return self
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("bukan objek JSON")
            self.data.update(d)
            self.existed = True
        except Exception as e:
            bad = self.path.with_name(self.path.name + ".bad")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            log("STATE", f"{self.path.name} rusak ({e}) → dipindah ke {bad.name}, mulai bersih")
        return self

    def save(self, dry: bool = False) -> None:
        if dry:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


# ──────────────────────────── Kesehatan sumber ────────────────────────────
def hl_ok(m: dict, ts: float) -> None:
    m.update(last_ok=int(ts), fails=0, cool_until=0, hard_days=[], soft_days=[])


def hl_fail(m: dict, kind: str, ts: float, T: dict) -> None:
    """Catat kegagalan. 'gone' = keras; lainnya (403/429/5xx/timeout) = lunak."""
    m["fails"] = int(m.get("fails", 0)) + 1
    key = "hard_days" if kind == "gone" else "soft_days"
    days = m.setdefault(key, [])
    d = day_of(ts)
    if d not in days:
        days.append(d)
        del days[:-30]
    hours = min(2 ** min(m["fails"], 8), T["max_cooldown_hours"])
    m["cool_until"] = int(ts + hours * 3600)


def hl_cooling(m: dict, ts: float) -> bool:
    return m.get("cool_until", 0) > ts


def hl_should_retire(m: dict, T: dict) -> bool:
    return len(m.get("hard_days", [])) >= T["retire_hard_days"] or len(m.get("soft_days", [])) >= T["retire_soft_days"]


# ──────────────────────────── Tipe bersama ────────────────────────────
@dataclass
class Sighting:
    domain: str
    source: str   # mis. "tg:@kanal", "comic:situs.com", "crt:%slotgacor%"
    pts: int
    note: str = ""


@dataclass
class CollectorResult:
    name: str
    sightings: list = field(default_factory=list)
    proposals: list = field(default_factory=list)   # (origin_url, sumber)
    stats: dict = field(default_factory=dict)
    error: str = ""


@dataclass
class Ctx:
    cfg: Config
    http: Http
    known: set
    allowlist: set
    ts: float
    dry: bool = False


# ──────────────────────────── Cache domain dikenal ────────────────────────────
def load_known(ctx: Ctx, deadline: Deadline) -> None:
    known, allow = ctx.known, ctx.allowlist
    if ALLOWLIST_FILE.exists():
        for line in ALLOWLIST_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.split("#", 1)[0].strip().lower().removeprefix("||").removesuffix("^")
            if s and not s.startswith(("!", "@@")):
                allow.add(s)
    for path in (FILE_TARGET, GRADUATED_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith(("!", "#")):
                continue
            # "||domain^$opsi" / "domain|komentar" → ambil domainnya saja
            # (kode lama memotong di "|" PERTAMA sehingga "||x^" jadi kosong → tak pernah terbaca)
            dom = re.split(r"[\^|$\s#/]", s.lower().removeprefix("||"), maxsplit=1)[0]
            if dom:
                known.add(dom)
    for src in ctx.cfg.cache_sources:
        r = ctx.http.get(src["url"], timeout=45, retries=2, max_bytes=60_000_000, deadline=deadline)
        if not r.ok:
            log("CACHE", f"{src.get('name', src['url'])}: gagal ({r.kind}/{r.status}) — dilewati")
            continue
        before = len(known)
        for line in r.text.splitlines():
            s = line.strip().lower()
            if s and s[0] not in "!#[" and not s.startswith("@@") \
                    and s.startswith("||") and s.endswith("^") and "$" not in s:
                known.add(s[2:-1])
        log("CACHE", f"{src.get('name', '?')}: +{len(known) - before} domain terindeks")
