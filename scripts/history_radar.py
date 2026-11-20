# Copyright (c) 2026 EazyWhale (https://github.com/lonion-1/WhaleGuard)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Licensed under the GNU Affero General Public License v3.0
"""EazyWhale Isekai Wall - Engine 3: History Archive & 30-Day Resurrection Crawler.

Alur Kerja:
1. Merapikan daftar aktif (.github/judol_radar_state.json): memindahkan situs komik/kanal
   yang sedang 'dingin' (sudah dipindai berkali-kali tapi 0 temuan baru) ke folder .github/history/
   agar radar utama tetap ringan dan cepat.
2. Memeriksa arsip (.github/history/archive_sites.json):
   - Jika umur pemeriksaan < 30 hari: lewati (tidur).
   - Jika umur pemeriksaan >= 30 hari (> 1 bulan): tembak ulang!
     * Ketemu banner judol baru -> panen domainnya + promosikan situs kembali ke daftar aktif.
     * Tetap 200 OK tapi 0 iklan -> perbarui timestamp, tunggu 30 hari berikutnya.
     * Kena HTTP 403 atau domain mati (NXDOMAIN 2x) -> pindahkan ke banned_permanent.json.
"""
import datetime
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

ACTIVE_STATE_FILE = Path(".github/judol_radar_state.json")
HISTORY_DIR = Path(".github/history")
ARCHIVE_FILE = HISTORY_DIR / "archive_sites.json"
BANNED_FILE = HISTORY_DIR / "banned_permanent.json"

WHALELIST_CACHE_URLS = [
    ("WhaleList IsekaiWall", "https://raw.githubusercontent.com/lonion-1/WhaleList/main/EazyLife-IsekaiWall.txt"),
    ("WhaleList Security", "https://raw.githubusercontent.com/lonion-1/WhaleList/main/EazyLife-Security.txt"),
]

COOLDOWN_DAYS = 30          # Tembak ulang jika sudah > 1 bulan (30 hari)
MAX_RECHECK_PER_RUN = 25    # Batas pemeriksaan arsip per siklus agar eksekusi tetap cepat
MAX_NEW_DOMAINS_PER_RUN = 150

RE_DOMAIN = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:xn--[a-z0-9-]{1,59}|[a-z]{2,63})$"
)
RE_URL_EXTRACT = re.compile(
    r"(?:https?://|www\.)([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)"
)
RE_HTML_LINK_BLOCK = re.compile(
    r'<a\b[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>([\s\S]*?)</a>',
    re.IGNORECASE,
)

RE_COMIC_SITE_HINT = re.compile(
    r"(?i)(?:komik|manga|manhwa|manhua|pornhwa|webtoon|doujin|desu|sekte|crot|ikiru|shinigami|kiryuu|westmanga|bacakomik|toon)"
)
RE_JUDOL_DOMAIN_STRONG = re.compile(
    r"(?i)("
    r"slotgacor|gacorslot|rtpslot|slotrtp|maxwin|judol|judibola|agenbola|"
    r"togel|toto(?:4d|88|77|99|macau|hk|sgp|sdy)|(?:4d|88|77|99)toto|"
    r"mahjongways|olympus(?:88|77|1000)|zeus(?:88|77|1000)|"
    r"slot(?:77|88|99|138|303|365|5000|777)|gacor(?:77|88|99|303|5000)|"
    r"pkvgames|idnplay|sbobet|sabungayam|situsjudi|bandarjudi"
    r")"
)
RE_BANNER_JUDOL_ATTR = re.compile(
    r"(?i)(?:slot|gacor|rtp|togel|toto|maxwin|judol|judi|depo|withdraw|wd|bonus|zeus|mahjong|pragmatic|scatter)"
)

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

PROTECTED_TLD_SUFFIXES = (
    ".go.id", ".ac.id", ".sch.id", ".mil.id", ".or.id", ".desa.id", ".ponpes.id",
    ".gov", ".edu", ".mil",
)

FREE_HOST_SUFFIXES = {
    "blogspot.com", "blogspot.co.id", "wordpress.com", "github.io", "gitlab.io",
    "pages.dev", "workers.dev", "netlify.app", "vercel.app", "herokuapp.com",
    "firebaseapp.com", "web.app", "wixsite.com", "weebly.com", "tumblr.com",
    "notion.site", "canva.site", "000webhostapp.com", "infinityfreeapp.com",
    "rf.gd", "byethost.com", "kesug.com", "great-site.net",
}

PROTECTED_DOMAINS = {
    "google.com", "googleapis.com", "gstatic.com", "youtube.com", "youtu.be", "android.com",
    "facebook.com", "fb.com", "instagram.com", "whatsapp.com", "wa.me", "threads.net", "meta.com",
    "t.me", "telegram.org", "telegram.me", "telegra.ph",
    "twitter.com", "x.com", "tiktok.com", "discord.com", "discord.gg", "reddit.com",
    "github.com", "githubusercontent.com", "gitlab.com", "cloudflare.com", "imgur.com",
    "wp.com", "gravatar.com", "disqus.com", "histats.com", "schema.org", "w3.org",
    "linktr.ee", "bit.ly", "tinyurl.com", "s.id", "cutt.ly", "rebrand.ly", "heylink.me", "taplink.cc",
    "trakteer.id", "saweria.co", "karyakarsa.com", "ko-fi.com", "sociabuzz.com", "patreon.com", "paypal.com", "paypal.me",
    "medium.com", "substack.com", "archive.org", "wikipedia.org",
    "eu.com", "uk.com", "us.com", "de.com", "eu.org",
    "kompas.com", "detik.com", "cnnindonesia.com", "cnbcindonesia.com", "tribunnews.com",
    "liputan6.com", "tempo.co", "suara.com", "okezone.com", "sindonews.com", "kumparan.com",
    "idntimes.com", "antaranews.com", "republika.co.id", "kemkomdigi.go.id", "kominfo.go.id",
    "bca.co.id", "bri.co.id", "bni.co.id", "bankmandiri.co.id", "dana.id", "ovo.id", "gopay.co.id",
    "tokopedia.com", "shopee.co.id", "lazada.co.id", "blibli.com",
}


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def parse_iso(ts: str | None) -> datetime.datetime:
    if not ts:
        return datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    try:
        dt = datetime.datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


def http_get(url: str, timeout: int = 20) -> tuple[str | None, str, int]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final_url = r.geturl() or url
            return r.read().decode("utf-8", errors="ignore"), final_url, getattr(r, "status", 200)
    except urllib.error.HTTPError as e:
        return None, url, e.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, url, 0


def bersihkan_html(raw_html: str) -> str:
    teks = re.sub(r"<[^>]+>", " ", html.unescape(raw_html or ""))
    return re.sub(r"\s+", " ", teks).strip()


def ekstrak_domain_aman(raw_host: str, allowlist: set[str]) -> str | None:
    h = raw_host.strip().lower().lstrip("*.")
    h = re.split(r"[/?#:^*]", h, maxsplit=1)[0].strip(".")
    if not h or not RE_DOMAIN.match(h) or h.endswith(PROTECTED_TLD_SUFFIXES):
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
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


def muat_cache_diabaikan() -> tuple[set[str], set[str]]:
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
        raw, _, _ = http_get(url, timeout=45)
        if not raw:
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


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ekstrak_banner_judol(raw_html: str, final_url: str, allowlist: set[str], diabaikan: set[str]) -> tuple[set[str], list[str]]:
    """Ekstrak domain iklan judol & mirror situs komik baru dari halaman web."""
    site_host = urllib.parse.urlparse(final_url).netloc.lower().removeprefix("www.")
    comic_hosts = {site_host}
    base_dom = ekstrak_domain_aman(site_host, set())
    if base_dom:
        comic_hosts.add(base_dom)

    judol_found: set[str] = set()
    mirrors_found: list[str] = []

    for href, inner_html in RE_HTML_LINK_BLOCK.findall(raw_html):
        parsed = urllib.parse.urlparse(href)
        target_host = parsed.netloc.lower().removeprefix("www.")
        if not target_host or target_host == site_host or target_host in comic_hosts:
            continue

        dom = ekstrak_domain_aman(target_host, allowlist | comic_hosts)
        if not dom:
            continue

        if RE_COMIC_SITE_HINT.search(dom):
            m_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/"
            if m_url not in mirrors_found:
                mirrors_found.append(m_url)
            comic_hosts.add(dom)
            continue

        if punya_induk_di_cache(dom, diabaikan):
            continue

        has_img = "<img" in inner_html.lower()
        attr_text = bersihkan_html(inner_html)
        alt_matches = " ".join(re.findall(r'(?:alt|title)=["\']([^"\']+)["\']', inner_html, re.I))
        konteks = f"{dom} {attr_text} {alt_matches}"

        if RE_JUDOL_DOMAIN_STRONG.search(dom) or (
            (has_img or len(parsed.path) > 1) and RE_BANNER_JUDOL_ATTR.search(konteks)
        ):
            judol_found.add(dom)

    for m in RE_URL_EXTRACT.finditer(raw_html):
        dom = ekstrak_domain_aman(m.group(1), allowlist | comic_hosts)
        if not dom or dom in comic_hosts or punya_induk_di_cache(dom, diabaikan):
            continue
        if RE_JUDOL_DOMAIN_STRONG.search(dom):
            judol_found.add(dom)

    return judol_found, mirrors_found


def main() -> int:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    sekarang = now_utc()
    iso_now = sekarang.isoformat()

    active_state = load_json(ACTIVE_STATE_FILE, {"seen": [], "channels": {}, "comic_sites": {}, "rejected": [], "blocked_urls": []})
    archive: dict = load_json(ARCHIVE_FILE, {})
    banned: dict = load_json(BANNED_FILE, {})

    # 1. Bersihkan daftar aktif: pindahkan situs yang 0 hits & menumpuk ke folder arsip
    active_comics: dict = active_state.setdefault("comic_sites", {})
    kept_active: dict = {}
    dipindahkan = 0

    for idx, (url, meta) in enumerate(active_comics.items()):
        hits = int(meta.get("hits", 0))
        # Pertahankan maksimal 18 situs paling aktif di daftar utama; sisanya masuk arsip cooldown
        if idx < 18 and hits > 0:
            kept_active[url] = meta
        elif idx < 10 and int(meta.get("misses", 0)) == 0:
            kept_active[url] = meta
        else:
            if url not in banned:
                entry = archive.get(url, {
                    "last_checked": iso_now,
                    "total_hits": hits,
                    "rechecks": 0,
                    "dead_strikes": 0,
                })
                entry["last_checked"] = iso_now
                entry["total_hits"] = max(entry.get("total_hits", 0), hits)
                archive[url] = entry
                dipindahkan += 1

    # Pindahkan URL non-403 dari blocked_urls lama ke arsip agar bisa dites ulang > 1 bulan
    sisa_blocked: list[str] = []
    for u in active_state.get("blocked_urls", []):
        if u in banned:
            sisa_blocked.append(u)
        elif u not in archive:
            archive[u] = {
                "last_checked": iso_now,
                "total_hits": 0,
                "rechecks": 0,
                "dead_strikes": 0,
            }
            dipindahkan += 1

    active_state["comic_sites"] = kept_active
    active_state["blocked_urls"] = sisa_blocked

    if dipindahkan:
        print(f"[ARCHIVE-MOVE] {dipindahkan} URL dipindahkan dari daftar utama ke {ARCHIVE_FILE} (cooldown {COOLDOWN_DAYS} hari).")

    # 2. Cari URL di arsip yang sudah berumur >= 30 hari (> 1 bulan)
    jatuh_tempo: list[tuple[datetime.datetime, str]] = []
    for url, meta in archive.items():
        if url in banned:
            continue
        last_dt = parse_iso(meta.get("last_checked"))
        if (sekarang - last_dt).days >= COOLDOWN_DAYS:
            jatuh_tempo.append((last_dt, url))

    jatuh_tempo.sort(key=lambda x: x[0])  # Prioritaskan yang paling lama tidak diperiksa
    print(f"[HISTORY] Total arsip: {len(archive)} URL | Jatuh tempo (>{COOLDOWN_DAYS} hari): {len(jatuh_tempo)} URL | Banned permanen: {len(banned)}")

    if not jatuh_tempo:
        save_json(ACTIVE_STATE_FILE, active_state)
        save_json(ARCHIVE_FILE, archive)
        save_json(BANNED_FILE, banned)
        print("History Radar: Belum ada URL arsip yang melewati 30 hari. Selesai.")
        return 0

    diabaikan, allowlist = muat_cache_diabaikan()
    kandidat_judol: set[str] = set()

    # 3. Tembak ulang URL yang sudah > 1 bulan
    for _, url in jatuh_tempo[:MAX_RECHECK_PER_RUN]:
        meta = archive[url]
        meta["rechecks"] = int(meta.get("rechecks", 0)) + 1
        meta["last_checked"] = iso_now

        raw, final_url, status = http_get(url, timeout=20)

        # Jika kena 403 Forbidden -> Banned permanen agar tidak buang waktu
        if status == 403:
            print(f"[BANNED-403] {url} membalas HTTP 403 -> masuk {BANNED_FILE}")
            banned[url] = {"banned_at": iso_now, "reason": "HTTP 403 Forbidden"}
            archive.pop(url, None)
            continue

        # Jika gagal terhubung, cek apakah domainnya memang sudah mati total (NXDOMAIN)
        if status != 200 or not raw:
            host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
            if host and not domain_aktif_dns(host):
                meta["dead_strikes"] = int(meta.get("dead_strikes", 0)) + 1
                if meta["dead_strikes"] >= 2:
                    print(f"[BANNED-DEAD] {url} NXDOMAIN 2x berturut-turut -> masuk {BANNED_FILE}")
                    banned[url] = {"banned_at": iso_now, "reason": "DNS NXDOMAIN (Mati Permanen)"}
                    archive.pop(url, None)
                else:
                    print(f"[HISTORY-WAIT] {url} DNS tidak merespons (strike 1/2), tunggu 30 hari lagi.")
            else:
                print(f"[HISTORY-WAIT] {url} gangguan sementara (HTTP {status}), tunggu 30 hari lagi.")
            time.sleep(1.0)
            continue

        # Status 200 OK -> Sedot banner iklan & cek apakah muncul domain judol baru!
        meta["dead_strikes"] = 0
        judol_baru, mirrors = ekstrak_banner_judol(raw, final_url, allowlist, diabaikan | kandidat_judol)

        for m_url in mirrors:
            if m_url not in archive and m_url not in banned and m_url not in active_comics:
                archive[m_url] = {
                    "last_checked": iso_now,
                    "total_hits": 0,
                    "rechecks": 0,
                    "dead_strikes": 0,
                }
                print(f"[HISTORY-MIRROR] Mirror baru disimpan ke arsip: {m_url}")

        if judol_baru:
            kandidat_judol.update(judol_baru)
            meta["total_hits"] = int(meta.get("total_hits", 0)) + len(judol_baru)
            # Promosikan kembali ke daftar aktif karena sedang ada kontrak iklan judol baru!
            active_comics[url] = {"misses": 0, "hits": meta["total_hits"]}
            archive.pop(url, None)
            print(f"[RESURRECT] {url} hidup kembali dengan +{len(judol_baru)} domain judol -> naik ke Daftar Aktif!")
        else:
            print(f"[HISTORY-OK] {url} (200 OK, 0 judol baru) -> tidur 30 hari lagi.")

        time.sleep(1.2)

    save_json(ACTIVE_STATE_FILE, active_state)
    save_json(ARCHIVE_FILE, archive)
    save_json(BANNED_FILE, banned)

    # 4. Verifikasi DNS aktif untuk setiap kandidat domain judol yang ditemukan
    domain_terverifikasi: list[str] = []
    for dom in sorted(kandidat_judol):
        if len(domain_terverifikasi) >= MAX_NEW_DOMAINS_PER_RUN:
            break
        if punya_induk_di_cache(dom, diabaikan):
            continue
        if domain_aktif_dns(dom):
            domain_terverifikasi.append(dom)
            diabaikan.add(dom)
            print(f"[+DOMAIN-HISTORY] {dom}")

    if not domain_terverifikasi:
        print("History Radar: 0 domain judol baru pada siklus ini.")
        return 0

    FILE_TARGET.parent.mkdir(parents=True, exist_ok=True)
    existing = FILE_TARGET.read_text(encoding="utf-8") if FILE_TARGET.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"

    tambahan = "\n".join(f"||{d}^" for d in domain_terverifikasi) + "\n"
    FILE_TARGET.write_text(existing + tambahan, encoding="utf-8")
    print(f"[SUKSES] {len(domain_terverifikasi)} domain judol baru dari arsip lama ditambahkan ke {FILE_TARGET}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
