# Copyright (c) 2026 <EazyWhale>
# Licensed under the MIT License.
"""EazyWhale Isekai Wall - Secondary Crawler Engine (judol_radar.py).

Fitur Safety-First & Zero-Redundancy:
1. Menggunakan 2 file WhaleList (EazyLife-IsekaiWall.txt & EazyLife-Security.txt) + Graduated.txt
   sebagai cache anti-redundansi agar cepat & tidak pernah mengambil domain yang sudah diblokir.
2. Melacak domain judol baru dari 3 jalur:
   - Jalur A: Certificate Transparency (crt.sh)
   - Jalur B: Comic-Site Banner & Mirror Crawler (termasuk redirect-hub & 2-hop mirror)
   - Jalur C: Telegram Auto-Discovery (t.me/s/ + lyzem.com) dengan rapor produktivitas kanal
3. Perlindungan ketat multi-part TLD (.my.id, .biz.id, .co.id), domain pemerintah/kampus,
   platform bersama (blogspot, pages.dev, linktr.ee, sosmed), & verifikasi DNS aktif.
"""
import html
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FILE_TARGET = Path("EazyBlock.txt")
GRADUATED_FILE = Path("Graduated.txt")
ALLOWLIST_FILE = Path("Allowlist.txt")
STATE_FILE = Path(".github/judol_radar_state.json")

# 2 URL Cache dari WhaleList (cepat, ringan, & mencakup HaGeZi Gambling + Fake + URLhaus + EazyBlock)
WHALELIST_CACHE_URLS = [
    ("WhaleList IsekaiWall", "https://raw.githubusercontent.com/lonion-1/WhaleList/main/EazyLife-IsekaiWall.txt"),
    ("WhaleList Security", "https://raw.githubusercontent.com/lonion-1/WhaleList/main/EazyLife-Security.txt"),
]

# Daftar situs utama baca komik/doujin/manhwa (termasuk portal redirect) untuk disedot banner judolnya
COMIC_SEED_URLS = [
    "https://doujin.desu.xxx/",
    "https://shinigami.to/",
    "https://ikiru.id/",
    "https://sektedoujin.cc/",
    "https://crotpedia.net/",
    "https://link.manhwaland.land/",
    "https://komiktap.info/",
    "https://komiku.org/",
    "https://mangaku.lol/",
]

# Query Certificate Transparency (crt.sh) untuk domain judol yang baru terbit SSL-nya
CRT_QUERIES = [
    "%slotgacor%",
    "%rtpslot%",
    "%maxwin%",
    "%judol%",
    "%mahjongways%",
]

# Pencarian direktori Telegram untuk auto-discovery kanal promo/laporan judol
DIRECTORY_QUERIES = [
    "https://lyzem.com/search?q=slot+gacor",
    "https://lyzem.com/search?q=situs+judol",
    "https://lyzem.com/search?q=link+alternatif+slot",
]

MAX_SEEN_POSTS = 3500
MAX_ACTIVE_CHANNELS = 25
MAX_COMIC_SITES = 35
MAX_VERIFY_PER_RUN = 15
MAX_REJECTED = 600
MAX_DRY_RUNS = 25
MAX_MISSES = 3
MAX_NEW_DOMAINS_PER_RUN = 150  # Batas aman per siklus agar list tetap terkendali

RE_DOMAIN = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:xn--[a-z0-9-]{1,59}|[a-z]{2,63})$"
)
RE_URL_EXTRACT = re.compile(
    r"(?:https?://|www\.)([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)"
)
RE_TG_MENTION = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([a-zA-Z][a-zA-Z0-9_]{4,31})\b"
)
RE_HTML_LINK_BLOCK = re.compile(
    r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>([\s\S]*?)</a>',
    re.IGNORECASE,
)

# Ciri domain situs baca komik/mirror untuk snowball otomatis (dan mencegahnya ikut terblokir)
RE_COMIC_SITE_HINT = re.compile(
    r"(?i)(?:komik|manga|manhwa|manhua|webtoon|doujin|desu|sekte|crot|ikiru|shinigami|kiryuu|westmanga|bacakomik)"
)

# Pola nama domain judol berkeyakinan tinggi (anti false-positive)
RE_JUDOL_DOMAIN_STRONG = re.compile(
    r"(?i)("
    r"slotgacor|gacorslot|rtpslot|slotrtp|maxwin|judol|judibola|agenbola|"
    r"togel|toto(?:4d|88|77|99|macau|hk|sgp|sdy)|(?:4d|88|77|99)toto|"
    r"mahjongways|olympus(?:88|77|1000)|zeus(?:88|77|1000)|"
    r"slot(?:77|88|99|138|303|365|5000|777)|gacor(?:77|88|99|303|5000)|"
    r"pkvgames|idnplay|sbobet|sabungayam|situsjudi|bandarjudi"
    r")"
)

# Pola nama domain moderat (hanya lolos jika muncul di banner gambar/promo judol eksplisit)
RE_JUDOL_DOMAIN_MODERATE = re.compile(
    r"(?i)(?:slot|gacor|jp|jackpot|cuan|hok|depo|wd|bet|win|spin|zeus|olympus|toto|4d|77|88|99|138|303|5000|vip|raja|dewa|bos|indo|asia|idn|link|alternatif)"
)

RE_BANNER_JUDOL_ATTR = re.compile(
    r"(?i)(?:slot|gacor|rtp|togel|toto|maxwin|judol|judi|depo|withdraw|wd|bonus|zeus|mahjong|pragmatic|scatter)"
)

RE_JUDOL_CONTEXT = re.compile(
    r"(?i)\b("
    r"slot\s*gacor|rtp\s*live|rtp\s*slot|maxwin|anti\s*rungkad|pola\s*gacor|"
    r"scatter\s*hitam|pragmatic\s*play|pg\s*soft|togel\s*online|bandar\s*togel|"
    r"depo\s*\d+|withdraw|bonus\s*new\s*member|link\s*alternatif|situs\s*judol"
    r")\b"
)

RE_INDONESIAN_HINT = re.compile(
    r"(?i)\b(dan|yang|ini|itu|daftar|login|link|alternatif|bonus|member|depo|resmi|gacor|hari)\b"
)

# TLD multi-bagian agar tidak pernah terpotong menjadi "my.id" atau "co.id"
MULTI_PART_TLDS = {
    "co.id", "my.id", "biz.id", "web.id", "or.id", "ac.id", "sch.id",
    "go.id", "mil.id", "desa.id", "ponpes.id", "net.id",
    "co.uk", "org.uk", "me.uk", "com.au", "net.au", "org.au",
    "com.sg", "com.my", "co.jp", "co.kr", "com.br", "com.tr",
    "com.vn", "co.th", "com.ph", "com.tw", "com.hk",
    "eu.com", "uk.com", "us.com", "de.com", "cn.com", "ru.com",
    "za.com", "br.com", "ar.com", "hu.com", "jpn.com", "kr.com",
    "no.com", "qc.com", "sa.com", "se.com", "uy.com", "web.com",
    "gb.com", "gb.net", "uk.net", "se.net", "in.net", "jp.net",
    "hu.net", "ae.org", "us.org", "eu.org",
}

# Ekstensi institusi/pemerintah yang mutlak dilindungi
PROTECTED_TLD_SUFFIXES = (
    ".go.id", ".ac.id", ".sch.id", ".mil.id", ".or.id", ".desa.id", ".ponpes.id",
    ".gov", ".edu", ".mil",
)

# Layanan hosting gratis: pertahankan subdomain lengkap, jangan pernah blokir induknya
FREE_HOST_SUFFIXES = {
    "blogspot.com", "blogspot.co.id", "wordpress.com", "github.io", "gitlab.io",
    "pages.dev", "workers.dev", "netlify.app", "vercel.app", "herokuapp.com",
    "firebaseapp.com", "web.app", "wixsite.com", "weebly.com", "tumblr.com",
    "notion.site", "canva.site", "000webhostapp.com", "infinityfreeapp.com",
    "rf.gd", "byethost.com", "kesug.com", "great-site.net",
}

# Domain platform besar, shortener, berita, CDN gambar, & perbankan yang mutlak dilarang diblokir
PROTECTED_DOMAINS = {
    "google.com", "googleapis.com", "gstatic.com", "youtube.com", "youtu.be", "android.com",
    "facebook.com", "fb.com", "instagram.com", "whatsapp.com", "wa.me", "threads.net", "meta.com",
    "t.me", "telegram.org", "telegram.me", "telegra.ph",
    "twitter.com", "x.com", "tiktok.com", "discord.com", "discord.gg", "reddit.com",
    "github.com", "githubusercontent.com", "gitlab.com", "cloudflare.com", "imgur.com",
    "wp.com", "gravatar.com", "disqus.com", "histats.com",
    "linktr.ee", "bit.ly", "tinyurl.com", "s.id", "cutt.ly", "rebrand.ly", "heylink.me", "taplink.cc",
    "medium.com", "substack.com", "archive.org", "wikipedia.org",
    "kompas.com", "detik.com", "cnnindonesia.com", "cnbcindonesia.com", "tribunnews.com",
    "liputan6.com", "tempo.co", "suara.com", "okezone.com", "sindonews.com", "kumparan.com",
    "idntimes.com", "antaranews.com", "republika.co.id", "kemkomdigi.go.id", "kominfo.go.id",
    "bca.co.id", "bri.co.id", "bni.co.id", "bankmandiri.co.id", "dana.id", "ovo.id", "gopay.co.id",
    "tokopedia.com", "shopee.co.id", "lazada.co.id", "blibli.com",
}

RESERVED_TG_NAMES = {
    "joinchat", "addstickers", "addtheme", "setlanguage", "share", "proxy",
    "socks", "confirmphone", "iv", "login", "contact", "support", "telegram",
    "admin", "channel", "group", "botfather",
}


def http_get(url: str, timeout: int = 25) -> tuple[str | None, str]:
    """Ambil isi URL beserta URL akhir setelah redirect (penting untuk portal situs komik)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final_url = r.geturl() or url
            return r.read().decode("utf-8", errors="ignore"), final_url
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[WARN] Gagal akses {url[:75]}: {e}")
        return None, url


def bersihkan_html(raw_html: str) -> str:
    teks = re.sub(r"<[^>]+>", " ", html.unescape(raw_html or ""))
    return re.sub(r"\s+", " ", teks).strip()


def ekstrak_domain_aman(raw_host: str, allowlist: set[str]) -> str | None:
    """Normalisasi ke domain registrable yang aman, menolak TLD pemerintah/platform besar."""
    h = raw_host.strip().lower().lstrip("*.")
    h = re.split(r"[/?#:^*]", h, maxsplit=1)[0].strip(".")
    if not h or not RE_DOMAIN.match(h):
        return None

    if h.endswith(PROTECTED_TLD_SUFFIXES):
        return None

    parts = h.split(".")
    if len(parts) < 2:
        return None

    suffix2 = ".".join(parts[-2:])
    suffix3 = ".".join(parts[-3:]) if len(parts) >= 3 else ""

    if suffix2 in FREE_HOST_SUFFIXES:
        if len(parts) < 3 or parts[-3] in {"www", "m"}:
            return None
        target = ".".join(parts[-3:])
    elif suffix3 in FREE_HOST_SUFFIXES:
        if len(parts) < 4 or parts[-4] in {"www", "m"}:
            return None
        target = ".".join(parts[-4:])
    elif suffix2 in MULTI_PART_TLDS:
        if len(parts) < 3:
            return None
        target = ".".join(parts[-3:])
    else:
        target = suffix2

    if target in PROTECTED_DOMAINS or target in allowlist:
        return None
    if any(target == p or target.endswith("." + p) for p in PROTECTED_DOMAINS | allowlist):
        return None

    return target if RE_DOMAIN.match(target) else None


def domain_aktif_dns(domain: str) -> bool:
    """Pastikan domain benar-benar memiliki catatan DNS aktif agar list tidak penuh domain mati."""
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


def muat_cache_diabaikan() -> tuple[set[str], set[str]]:
    """Muat cache dari WhaleList (2 URL), EazyBlock.txt, Graduated.txt, & Allowlist.txt."""
    diabaikan: set[str] = set()
    allowlist: set[str] = set()

    if ALLOWLIST_FILE.exists():
        for l in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
            s = l.split("#", 1)[0].strip().lower().removeprefix("||").removesuffix("^")
            if s and not s.startswith(("!", "@@")):
                allowlist.add(s)

    for path in (FILE_TARGET, GRADUATED_FILE):
        if not path.exists():
            continue
        for l in path.read_text(encoding="utf-8").splitlines():
            s = l.strip()
            if not s or s.startswith(("!", "#")):
                continue
            dom = s.split("|")[0].strip().lower().removeprefix("||").removesuffix("^")
            if "$" in dom:
                dom = dom.split("$", 1)[0]
            if dom:
                diabaikan.add(dom)

    for nama, url in WHALELIST_CACHE_URLS:
        raw, _ = http_get(url, timeout=45)
        if not raw:
            print(f"[WARN] Cache {nama} gagal dimuat, lanjut dengan cache lokal.")
            continue
        sebelum = len(diabaikan)
        for l in raw.splitlines():
            s = l.strip().lower()
            if not s or s[0] in "!#[" or s.startswith("@@"):
                continue
            if s.startswith("||") and s.endswith("^") and "$" not in s:
                diabaikan.add(s[2:-1])
        print(f"[CACHE] {nama}: +{len(diabaikan) - sebelum} domain terindeks")

    return diabaikan, allowlist


def punya_induk_di_cache(domain: str, cache: set[str]) -> bool:
    if domain in cache:
        return True
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in cache:
            return True
    return False


def fetch_crt_candidates(allowlist: set[str], diabaikan: set[str]) -> set[str]:
    """Jalur A: Sadapan Certificate Transparency (crt.sh) untuk sertifikat SSL domain judol baru."""
    kandidat: set[str] = set()
    for q in CRT_QUERIES:
        url = f"https://crt.sh/?q={urllib.parse.quote(q)}&output=json"
        raw, _ = http_get(url, timeout=30)
        if not raw:
            time.sleep(2.0)
            continue
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for entry in entries[-250:]:
            name_val = entry.get("name_value", "")
            for line in name_val.splitlines():
                dom = ekstrak_domain_aman(line, allowlist)
                if not dom or punya_induk_di_cache(dom, diabaikan):
                    continue
                if RE_JUDOL_DOMAIN_STRONG.search(dom):
                    kandidat.add(dom)
        print(f"[CRT.SH] Query {q}: total kandidat sementara {len(kandidat)}")
        time.sleep(2.0)
    return kandidat


def crawl_comic_banners(
    state: dict,
    allowlist: set[str],
    diabaikan: set[str],
) -> set[str]:
    """Jalur B: Sedot domain iklan judol dari banner <a>...<img></a> pada situs komik + 2-hop mirror."""
    comic_sites: dict = state.setdefault("comic_sites", {})
    for url in COMIC_SEED_URLS:
        u = url.strip()
        if u and u not in comic_sites:
            comic_sites[u] = {"misses": 0, "hits": 0}

    kandidat_judol: set[str] = set()
    comic_hosts: set[str] = set()

    def daftarkan_host_komik(url_str: str) -> None:
        host = urllib.parse.urlparse(url_str).netloc.lower().removeprefix("www.")
        if host:
            comic_hosts.add(host)
            base_dom = ekstrak_domain_aman(host, set())
            if base_dom:
                comic_hosts.add(base_dom)

    for u in comic_sites:
        daftarkan_host_komik(u)

    def pindai_satu_situs(site_url: str, meta: dict) -> list[str]:
        raw, final_url = http_get(site_url, timeout=20)
        if not raw:
            meta["misses"] = meta.get("misses", 0) + 1
            if site_url not in COMIC_SEED_URLS and meta["misses"] >= 3:
                comic_sites.pop(site_url, None)
            return []

        meta["misses"] = 0
        daftarkan_host_komik(final_url)

        site_host = urllib.parse.urlparse(final_url).netloc.lower().removeprefix("www.")
        hits_site = 0
        discovered_mirrors: list[str] = []

        for href, inner_html in RE_HTML_LINK_BLOCK.findall(raw):
            parsed = urllib.parse.urlparse(href)
            target_host = parsed.netloc.lower().removeprefix("www.")
            if not target_host or target_host == site_host or target_host in comic_hosts:
                continue

            dom = ekstrak_domain_aman(target_host, allowlist | comic_hosts)
            if not dom:
                continue

            # Jika mengarah ke situs komik partner / domain baru dari portal redirect
            if RE_COMIC_SITE_HINT.search(dom):
                base_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/"
                if base_url not in comic_sites and base_url not in discovered_mirrors:
                    discovered_mirrors.append(base_url)
                daftarkan_host_komik(base_url)
                continue

            if punya_induk_di_cache(dom, diabaikan):
                continue

            has_img = "<img" in inner_html.lower()
            attr_text = bersihkan_html(inner_html)
            alt_matches = " ".join(re.findall(r'(?:alt|title)=["\']([^"\']+)["\']', inner_html, re.I))
            konteks_banner = f"{dom} {attr_text} {alt_matches}"

            if RE_JUDOL_DOMAIN_STRONG.search(dom) or (
                (has_img or len(parsed.path) > 1) and RE_BANNER_JUDOL_ATTR.search(konteks_banner)
            ):
                if dom not in kandidat_judol:
                    kandidat_judol.add(dom)
                    hits_site += 1

        meta["hits"] = meta.get("hits", 0) + hits_site
        print(f"[COMIC] {site_host}: +{hits_site} domain banner judol (total situs: {meta['hits']})")
        time.sleep(1.5)
        return discovered_mirrors

    # Pass 1: Pindai situs utama & situs komik tersimpan
    antrean_mirror: list[str] = []
    for site_url, meta in list(comic_sites.items())[:18]:
        mirrors = pindai_satu_situs(site_url, meta)
        for m_url in mirrors:
            if len(comic_sites) + len(antrean_mirror) < MAX_COMIC_SITES and m_url not in antrean_mirror:
                antrean_mirror.append(m_url)

    # Pass 2 (2-Hop): Langsung selami mirror yang baru ditemukan dari portal (mis. shinigami.to / manhwaland)
    for new_u in antrean_mirror[:6]:
        if len(comic_sites) >= MAX_COMIC_SITES:
            break
        comic_sites[new_u] = {"misses": 0, "hits": 0}
        print(f"[NEW COMIC] Situs komik mirror/partner ditemukan: {new_u}")
        pindai_satu_situs(new_u, comic_sites[new_u])

    return kandidat_judol


def default_meta(source: str) -> dict:
    return {"source": source, "scans": 0, "harvested": 0, "dry_runs": 0, "misses": 0}


def muat_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": [], "channels": {}, "comic_sites": {}, "rejected": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {
            "seen": list(data.get("seen", []))[-MAX_SEEN_POSTS:],
            "channels": data.get("channels", {}),
            "comic_sites": data.get("comic_sites", {}),
            "rejected": list(data.get("rejected", []))[-MAX_REJECTED:],
        }
    except Exception:
        return {"seen": [], "channels": {}, "comic_sites": {}, "rejected": []}


def simpan_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["seen"] = list(dict.fromkeys(state["seen"]))[-MAX_SEEN_POSTS:]
    state["rejected"] = list(dict.fromkeys(state["rejected"]))[-MAX_REJECTED:]
    state["channels"] = dict(
        sorted(
            state["channels"].items(),
            key=lambda kv: kv[1].get("harvested", 0),
            reverse=True,
        )
    )
    state["comic_sites"] = dict(
        sorted(
            state.get("comic_sites", {}).items(),
            key=lambda kv: (kv[0] in COMIC_SEED_URLS, kv[1].get("hits", 0)),
            reverse=True,
        )
    )
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ekstrak_tg_username(teks: str) -> set[str]:
    hasil: set[str] = set()
    for m in RE_TG_MENTION.finditer(teks):
        u = m.group(1)
        if u.lower() not in RESERVED_TG_NAMES and not u.lower().endswith("bot"):
            hasil.add(u)
    return hasil


def verifikasi_kanal_judol(username: str) -> bool:
    raw, _ = http_get(f"https://t.me/s/{username}", timeout=20)
    if not raw or "tgme_widget_message_text" not in raw:
        return False
    msgs = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>', raw)
    if len(msgs) < 3:
        return False
    sampel = " ".join(bersihkan_html(x) for x in msgs[-10:])
    return bool(RE_JUDOL_CONTEXT.search(sampel)) and len(RE_INDONESIAN_HINT.findall(sampel)) >= 3


def fetch_telegram_channel(channel: str, max_pages: int = 3) -> list[dict]:
    hasil = []
    seen_local: set[str] = set()
    before_id: str | None = None

    for _ in range(max_pages):
        url = f"https://t.me/s/{channel}" + (f"?before={before_id}" if before_id else "")
        raw, _ = http_get(url, timeout=20)
        if not raw:
            break
        blocks = re.findall(
            r'data-post="([^"]+)"[\s\S]*?<div class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>',
            raw,
        )
        if not blocks:
            break
        for post_id, msg_html in blocks:
            if post_id in seen_local:
                continue
            seen_local.add(post_id)
            clean = bersihkan_html(msg_html)
            if clean:
                hasil.append({"id": f"tg:{post_id}", "text": clean})

        first_post = blocks[0][0]
        if "/" not in first_post:
            break
        oldest_num = first_post.split("/")[-1]
        if not oldest_num.isdigit() or oldest_num == before_id or int(oldest_num) <= 1:
            break
        before_id = oldest_num
        time.sleep(1.0)
    return hasil


def main() -> int:
    diabaikan, allowlist = muat_cache_diabaikan()
    state = muat_state()
    seen_set = set(state["seen"])
    channels_map: dict = state["channels"]
    rejected_set = {x.lower() for x in state["rejected"]}
    known_lower = {k.lower() for k in channels_map}

    kandidat_mentah: set[str] = set()
    snowball_tg: set[str] = set()
    to_prune: list[tuple[str, str]] = []

    # 1. Jalur A: Certificate Transparency (crt.sh)
    kandidat_mentah.update(fetch_crt_candidates(allowlist, diabaikan))

    # 2. Jalur B: Sedot Banner Iklan dari Situs Komik & Mirror
    kandidat_mentah.update(crawl_comic_banners(state, allowlist, diabaikan))

    # 3. Jalur C: Telegram Auto-Discovery & Rapor Kanal
    for ch, meta in list(channels_map.items()):
        items = fetch_telegram_channel(ch, max_pages=3)
        meta["scans"] = meta.get("scans", 0) + 1

        if not items:
            meta["misses"] = meta.get("misses", 0) + 1
            if meta["misses"] >= MAX_MISSES:
                to_prune.append((ch, "mati/kosong"))
            time.sleep(1.0)
            continue

        meta["misses"] = 0
        panen_kanal = 0

        for post in items:
            teks = post["text"]
            if RE_JUDOL_CONTEXT.search(teks):
                snowball_tg.update(ekstrak_tg_username(teks))

            if post["id"] in seen_set:
                continue
            seen_set.add(post["id"])
            state["seen"].append(post["id"])

            punya_konteks = bool(RE_JUDOL_CONTEXT.search(teks))
            for m in RE_URL_EXTRACT.finditer(teks):
                dom = ekstrak_domain_aman(m.group(1), allowlist)
                if not dom or punya_induk_di_cache(dom, diabaikan):
                    continue
                if RE_JUDOL_DOMAIN_STRONG.search(dom) or (
                    punya_konteks and RE_JUDOL_DOMAIN_MODERATE.search(dom)
                ):
                    if dom not in kandidat_mentah:
                        kandidat_mentah.add(dom)
                        panen_kanal += 1

        if panen_kanal > 0:
            meta["harvested"] = meta.get("harvested", 0) + panen_kanal
            meta["dry_runs"] = 0
        else:
            meta["dry_runs"] = meta.get("dry_runs", 0) + 1
            if meta["dry_runs"] >= MAX_DRY_RUNS:
                to_prune.append((ch, f"zonk {MAX_DRY_RUNS}x"))

        print(f"[INFO] TG @{ch}: +{panen_kanal} kandidat (total: {meta['harvested']}, zonk: {meta['dry_runs']}/{MAX_DRY_RUNS})")
        time.sleep(1.0)

    for ch, alasan in to_prune:
        print(f"[ROTATE] Memecat @{ch} ({alasan})")
        channels_map.pop(ch, None)
        known_lower.discard(ch.lower())
        state["rejected"].append(ch)
        rejected_set.add(ch.lower())

    # Cari kanal judol baru jika slot masih kosong
    if len(channels_map) < MAX_ACTIVE_CHANNELS:
        dir_cands: set[str] = set()
        for q_url in DIRECTORY_QUERIES:
            raw, _ = http_get(q_url, timeout=20)
            if raw:
                dir_cands.update(ekstrak_tg_username(raw))
            time.sleep(1.2)

        antrean = [
            u for u in (list(dir_cands) + list(snowball_tg))
            if u.lower() not in known_lower and u.lower() not in rejected_set
        ]
        for cand in antrean[:MAX_VERIFY_PER_RUN]:
            if len(channels_map) >= MAX_ACTIVE_CHANNELS:
                break
            if verifikasi_kanal_judol(cand):
                channels_map[cand] = default_meta("directory" if cand in dir_cands else "snowball")
                known_lower.add(cand.lower())
                print(f"[NEW CHANNEL] Kanal judol terverifikasi: @{cand}")
            else:
                state["rejected"].append(cand)
                rejected_set.add(cand.lower())
            time.sleep(1.0)

    simpan_state(state)

    # 4. Verifikasi DNS Aktif agar hanya domain yang benar-benar hidup yang ditambahkan
    domain_terverifikasi: list[str] = []
    for dom in sorted(kandidat_mentah):
        if len(domain_terverifikasi) >= MAX_NEW_DOMAINS_PER_RUN:
            break
        if punya_induk_di_cache(dom, diabaikan):
            continue
        if domain_aktif_dns(dom):
            domain_terverifikasi.append(dom)
            diabaikan.add(dom)
            print(f"[+DOMAIN] {dom}")

    if not domain_terverifikasi:
        print("Judol Radar: 0 domain baru yang lolos filter ketat & verifikasi DNS.")
        return 0

    FILE_TARGET.parent.mkdir(parents=True, exist_ok=True)
    existing = FILE_TARGET.read_text(encoding="utf-8") if FILE_TARGET.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"

    tambahan = "\n".join(f"||{d}^" for d in domain_terverifikasi) + "\n"
    FILE_TARGET.write_text(existing + tambahan, encoding="utf-8")
    print(f"[SUKSES] {len(domain_terverifikasi)} domain judol aktif ditambahkan ke {FILE_TARGET}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
   

def fetch_telegram_channel(channel: str, max_pages: int = 3) -> list[dict]:
    hasil = []
    seen_local: set[str] = set()
    before_id: str | None = None

    for _ in range(max_pages):
        url = f"https://t.me/s/{channel}" + (f"?before={before_id}" if before_id else "")
        raw, _ = http_get(url, timeout=20)
        if not raw:
            break
        blocks = re.findall(
            r'data-post="([^"]+)"[\s\S]*?<div class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>',
            raw,
        )
        if not blocks:
            break
        for post_id, msg_html in blocks:
            if post_id in seen_local:
                continue
            seen_local.add(post_id)
            clean = bersihkan_html(msg_html)
            if clean:
                hasil.append({"id": f"tg:{post_id}", "text": clean})

        first_post = blocks[0][0]
        if "/" not in first_post:
            break
        oldest_num = first_post.split("/")[-1]
        if not oldest_num.isdigit() or oldest_num == before_id or int(oldest_num) <= 1:
            break
        before_id = oldest_num
        time.sleep(1.0)
    return hasil


def main() -> int:
    diabaikan, allowlist = muat_cache_diabaikan()
    state = muat_state()
    seen_set = set(state["seen"])
    channels_map: dict = state["channels"]
    rejected
