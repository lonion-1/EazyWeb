# Copyright (c) 2026 <EazyWhale>
# Licensed under the MIT License.

import argparse
import datetime
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

FILE_TARGET = Path("EazyBlock.txt")
DNSFILTER_TARGET = Path("domains/EazyBlock.txt")
GRADUATED_FILE = Path("Graduated.txt")

BATAS_BUANG = 0.5  # Gagal jika >50% aturan lama hilang (kecuali PAKSA)
NOW_UTC = datetime.datetime.now(datetime.timezone.utc)
WAKTU = NOW_UTC.strftime("%Y.%m.%d.%H%M")
TANGGAL = NOW_UTC.strftime("%Y-%m-%d")

# Daftar luar yang diintip untuk menghapus redundansi (jangan masukkan WhaleList agar tidak loop)
UPSTREAM_SOURCES = [
    ("HaGeZi Gambling", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/gambling.txt"),
    ("HaGeZi Pro", "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/pro.txt"),
    ("ABPindo", "https://raw.githubusercontent.com/ABPindo/indonesianadblockrules/master/subscriptions/abpindo.txt"),
]

OPSI_AMAN = {"third-party", "3p", "important", "all", "popup"}
RE_DOMAIN = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:xn--[a-z0-9-]{1,59}|[a-z]{2,63})$")
RE_ABP = re.compile(r"^\|\|([a-z0-9.-]+)\^(?:\$([a-z0-9,~=-]+))?$")
RE_HOSTS = re.compile(r"^(?:0\.0\.0\.0|127\.0\.0\.1|::1?)\s+([^\s#]+)")
RE_DOM_AWAL = re.compile(r"^\|\|([a-z0-9.-]+)\^")


def parse_upstream_line(s):
    if s.startswith("||"):
        m = RE_ABP.match(s)
        if not m:
            return None
        if m.group(2) and not set(m.group(2).split(",")) <= OPSI_AMAN:
            return None
        d = m.group(1)
    elif s.startswith(("0.0.0.0", "127.0.0.1", "::")):
        m = RE_HOSTS.match(s)
        d = m.group(1) if m else None
    else:
        d = s
    return d if d and RE_DOMAIN.match(d) else None


def fetch_upstream(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "IsekaiWall-Cleaner/2.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", errors="ignore").splitlines()
        except Exception as e:
            print(f"  percobaan {i + 1}/{tries} gagal: {e}")
            time.sleep(3 * (i + 1))
    return None


def bersihkan_domain(b):
    """Ubah URL/domain mentah jadi domain bersih, atau None jika tidak valid."""
    d = re.sub(r"^(?:\|\||\||https?://|//)+", "", b.strip().lower())
    d = re.split(r"[/?#:^*]", d, maxsplit=1)[0].strip(".")
    if d and not d.isascii():
        try:
            d = d.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    return d if RE_DOMAIN.match(d) else None


def cari_induk(domain, kumpulan):
    bagian = domain.split(".")
    for i in range(1, len(bagian) - 1):
        induk = ".".join(bagian[i:])
        if induk in kumpulan:
            return induk
    return None


def load_graduated(path):
    records = {}
    if not path.exists():
        return records
    for l in path.read_text(encoding="utf-8").splitlines():
        s = l.strip()
        if not s or s.startswith(("!", "#")):
            continue
        parts = [p.strip() for p in s.split("|")]
        dom = parts[0].lower().removeprefix("||").removesuffix("^")
        if dom:
            tgl = parts[1] if len(parts) > 1 else "-"
            src = parts[2] if len(parts) > 2 else "-"
            records[dom] = (tgl, src)
    return records


def save_graduated(path, records):
    header = [
        "# EazyBlock Graduated - Catatan Domain yang Sudah Terindeks Sumber Global",
        "# Format: domain | tanggal_lulus (UTC) | terindeks_di",
        "# -----------------------------------------------------------------------",
        "",
    ]
    baris = [f"{dom} | {tgl} | {src}" for dom, (tgl, src) in sorted(records.items())]
    teks_baru = "\n".join(header + baris) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == teks_baru:
        return False
    path.write_text(teks_baru, encoding="utf-8")
    return True


def fmt_angka(n):
    return f"{n:,}".replace(",", ".")


def fmt_ukuran(b):
    if b < 1024 * 1024:
        return f"{round(b / 1024)} KB"
    return f"{b / (1024 * 1024):.1f} MB".replace(".", ",")


def update_readme():
    readme_path = Path("README.md")
    if not readme_path.exists() or not FILE_TARGET.exists():
        return
    teks_awal = readme_path.read_text(encoding="utf-8")
    jml = sum(1 for l in FILE_TARGET.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith(("!", "#")))
    ukuran = fmt_ukuran(FILE_TARGET.stat().st_size)

    def isi_tag(teks, tag, nama_file, nilai):
        pola = rf"(<!-- {tag}:{re.escape(nama_file)} -->).*?(<!-- /{tag} -->)"
        return re.sub(pola, lambda m: m.group(1) + nilai + m.group(2), teks)

    teks_baru = isi_tag(teks_awal, "COUNT", "EazyBlock.txt", fmt_angka(jml))
    teks_baru = isi_tag(teks_baru, "SIZE", "EazyBlock.txt", ukuran)
    if teks_baru != teks_awal:
        readme_path.write_text(teks_baru, encoding="utf-8")
        print("README.md: statistik diperbarui.")


def main():
    parser = argparse.ArgumentParser(description="EazyBlock Cleaner & Builder")
    parser.add_argument("--paksa", action="store_true", help="Lewati pengaman batas buang 50%%")    
    args = parser.parse_args()
    paksa = args.paksa or os.environ.get("PAKSA", "").lower() == "true"

    # 1. Intip sumber luar (simpan mapping domain -> nama sumber)
    upstream_map = {}
    sumber_ok = 0
    for nama, url in UPSTREAM_SOURCES:
        print(f"Mengintip sumber luar: {nama}")
        baris_luar = fetch_upstream(url)
        if baris_luar is None:
            print(f"  LEWATI {nama} (gagal diambil, data lokal tetap aman)")
            continue
        sumber_ok += 1
        sebelum = len(upstream_map)
        for b in baris_luar:
            s = b.strip().lower()
            if not s or s[0] in "!#[" or s.startswith("@@"):
                continue
            d = parse_upstream_line(s)
            if d and d not in upstream_map:
                upstream_map[d] = nama
        print(f"  +{len(upstream_map) - sebelum} domain terindeks")

    if sumber_ok < len(UPSTREAM_SOURCES):
        print(f"PERINGATAN: hanya {sumber_ok}/{len(UPSTREAM_SOURCES)} sumber berhasil diambil.")

    def sumber_luar_untuk(domain):
        if not upstream_map:
            return None
        if domain in upstream_map:
            return upstream_map[domain]
        induk = cari_induk(domain, upstream_map)
        return upstream_map[induk] if induk else None

    # 2. Baca & rapikan EazyBlock.txt
    if not FILE_TARGET.exists():
        sys.exit("GAGAL: File EazyBlock.txt tidak ditemukan!")

    teks_lama = FILE_TARGET.read_text(encoding="utf-8").splitlines()
    aturan_lama = [x.strip() for x in teks_lama if x.strip() and not x.strip().startswith(("!", "#"))]

    # Catat domain yang sebelumnya sudah sah berada di EazyBlock.txt
    domain_sebelumnya = set()
    for l in aturan_lama:
        if l.startswith("||") and l.endswith("^") and "$" not in l:
            domain_sebelumnya.add(l[2:-1].lower())

    aturan_khusus = set()
    domain_lokal = set()
    graduated_records = load_graduated(GRADUATED_FILE)
    graduated_changed = False
    n_redundan = n_invalid = 0

    for b in teks_lama:
        b = b.strip()
        if not b or b.startswith(("!", "#", "[")):
            continue
        if b.startswith(("@@", "/", "|http")) or "##" in b or "#@#" in b or "#?#" in b:
            aturan_khusus.add(b)
            continue

        b = re.sub(r"\s+", "", b).lower()

        if "$" in b:
            if not b.startswith("||"):
                b = "||" + b
            m = RE_DOM_AWAL.match(b)
            if not m:
                print(f"  LEWATI aturan beropsi tidak valid: {b}")
                n_invalid += 1
                continue
            src_hit = sumber_luar_untuk(m.group(1))
            if src_hit:
                n_redundan += 1
                continue
            aturan_khusus.add(b)
            continue

        d = bersihkan_domain(b)
        if not d:
            print(f"  LEWATI baris tidak valid: {b}")
            n_invalid += 1
            continue

        src_hit = sumber_luar_untuk(d)
        if src_hit:
            n_redundan += 1
            # Jika sebelumnya memang ada di EazyBlock.txt, catat kelulusannya ke Graduated.txt
            if d in domain_sebelumnya and d not in graduated_records:
                graduated_records[d] = (TANGGAL, src_hit)
                graduated_changed = True
            continue

        domain_lokal.add(d)

    # Eliminasi subdomain internal
    domain_bersih_list = []
    aturan_domain = []
    n_subdomain = 0
    for d in sorted(domain_lokal):
        if cari_induk(d, domain_lokal):
            n_subdomain += 1
            continue
        domain_bersih_list.append(d)
        aturan_domain.append(f"||{d}^")

    aturan_akhir = sorted(aturan_khusus | set(aturan_domain))

    # 3. Pengaman
    if not aturan_akhir:
        sys.exit("GAGAL: Hasil akhir kosong! File EazyBlock.txt dibiarkan utuh.")

    if (not paksa and len(aturan_lama) >= 20
            and len(aturan_akhir) < len(aturan_lama) * (1 - BATAS_BUANG)):
        sys.exit(
            f"GAGAL: aturan turun {len(aturan_lama)} -> {len(aturan_akhir)} "
            f"(>{int(BATAS_BUANG * 100)}%). Periksa dulu, atau jalankan manual dengan opsi 'paksa'."
        )

    # 4. Simpan Graduated.txt jika ada domain yang baru lulus ke hulu
    if graduated_changed or not GRADUATED_FILE.exists():
        if save_graduated(GRADUATED_FILE, graduated_records):
            print(f"Graduated.txt: diperbarui ({len(graduated_records)} total domain lulus ke hulu).")

    # 5. Simpan format Wildcard personalDNSfilter (domains/EazyBlock.txt)
    DNSFILTER_TARGET.parent.mkdir(parents=True, exist_ok=True)
    baris_dns = [item for d in domain_bersih_list for item in (d, f"*.{d}")]
    lama_dns = [l for l in DNSFILTER_TARGET.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")] if DNSFILTER_TARGET.exists() else []
    if lama_dns != baris_dns:
        header_dns = [
            "# Title: EazyWhale Isekai Wall (personalDNSfilter Wildcard)",
            f"# Version: {WAKTU}",
            f"# Count: {len(domain_bersih_list)}",
            "",
        ]
        DNSFILTER_TARGET.write_text("\n".join(header_dns + baris_dns) + "\n", encoding="utf-8")
        print("domains/EazyBlock.txt: diperbarui.")

    # 6. Tulis EazyBlock.txt jika ada perubahan aturan
    if aturan_lama != aturan_akhir:
        header = [
            "! Title: EazyWhale Isekai Wall",
            "! License: Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)",
            "! Maintainer: EazyWhale",
            "! Expires: 1 hours",
            f"! Version: {WAKTU}",
            f"! Total Rules: {len(aturan_akhir)}",
            "! --------------------------------------------------",
            "",
        ]
        FILE_TARGET.write_text("\n".join(header + aturan_akhir) + "\n", encoding="utf-8")
        print("\nSelesai merapikan EazyBlock.txt!")
        print(f"  Dibuang (sudah terindeks HaGeZi/ABPindo) : {n_redundan}")
        print(f"  Dibuang (induk sudah ada di list)        : {n_subdomain}")
        print(f"  Baris tidak valid dilewati               : {n_invalid}")
        print(f"  Total aturan eksklusif tersisa           : {len(aturan_akhir)}")
    else:
        print("Tidak ada perubahan aturan, EazyBlock.txt dibiarkan.")

    update_readme()
    Path("last-run.txt").write_text(f"Last check (UTC): {WAKTU}\n", encoding="utf-8")


if __name__ == "__main__":
    main()  
