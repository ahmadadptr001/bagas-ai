"""Kabar dari AI dibacakan lewat pengeras suara laptop.

Selama ini setiap kabar antar-langkah ("sekarang kuperbarui berkas uji tim…")
hanya muncul sebagai teks. Kalau jendela terminalnya tertutup jendela lain —
dan saat menunggu AI bekerja itu yang paling sering terjadi — kabar itu lewat
begitu saja. Suara tak punya masalah itu: ia terdengar di jendela mana pun.

BAHASA INDONESIA ATAU DIAM SEKALIAN
-----------------------------------
Aturan pertama dan tak bisa ditawar: yang keluar dari pengeras suara HARUS
berbahasa Indonesia. Sebuah mesin yang "bisa membunyikan teksnya" belum tentu
memenuhi syarat itu — suara Inggris yang membacakan kalimat Indonesia
menghasilkan bunyi yang sulit dimengerti DAN terdengar seperti suara asing yang
muncul entah dari mana. Itu keluhan nyata, dan sebabnya ada di rancangan lama:
mesin Windows dulu memakai "suara Indonesia kalau ada, kalau tidak ya suara apa
pun yang terpasang" — padahal Windows kebanyakan cuma punya suara Inggris.

Maka mesin yang TIDAK punya suara Indonesia tidak dianggap tersedia sama
sekali. Kalau akhirnya tak ada satu pun yang memenuhi syarat, fiturnya diam dan
alasannya dikatakan — jauh lebih baik daripada berbunyi dalam bahasa yang salah.

MESIN BERLAPIS
--------------
Diurutkan dari yang terbaik, dan yang tak berbahasa Indonesia dicoret:

  1. edge  — suara Indonesia natural (id-ID-ArdiNeural/GadisNeural). Butuh
             paket `edge-tts` DAN internet.
  2. sapi  — System.Speech bawaan Windows, HANYA bila ada suara berkultur
             `id-*` terpasang. Tanpa itu ia dilewati (lihat aturan di atas).
  3. say   — macOS, HANYA bila suara Indonesia (mis. Damayanti) terpasang.
  4. espeak— Linux; punya pelafalan `id` bawaan.

Mesin yang gagal berulang tidak dicoret selamanya, melainkan DIISTIRAHATKAN
sementara lalu dicoba lagi. Pencoretan permanen dulu berarti satu gangguan
jaringan yang agak panjang membuat sisa sesi bisu total — dan itu tak bisa
dipulihkan tanpa menutup bagas-ai.

TIGA PEMBATAS
-------------
  1. SATU proses pembantu, bukan satu proses per kalimat. Menyalakan PowerShell
     TERUKUR ~0,5 detik (jauh lebih lama di laptop pelan); membayarnya tiap
     kabar membuat suara selalu tertinggal. Karena itu ia hanya dibunuh saat
     benar-benar ada bunyi yang harus dihentikan, bukan tiap giliran usai.
  2. Kabar BASI dibuang. Begitu giliran dibatalkan atau giliran baru dimulai,
     antreannya dikosongkan dan yang sedang diucapkan dihentikan — mendengar
     rencana langkah yang sudah lewat lebih membingungkan daripada diam.
     Antrean langkah hanya menyimpan SATU kabar: yang terbaru. Pengecualiannya
     potongan LANJUTAN jawaban akhir: satu jawaban dibacakan utuh walau kabar
     langkah datang di sela-selanya.
  3. Yang diucapkan dipendekkan & dibersihkan: blok kode, penanda protokol, dan
     tautan tak ada gunanya didengar.

Kalau apa pun di jalur ini gagal, fitur ini DIAM dan tak pernah melempar ke
pemanggil. Notifikasi yang menjatuhkan giliran jauh lebih buruk daripada
notifikasi yang tak berbunyi.
"""
from __future__ import annotations

from typing import Any

import base64
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

log = logging.getLogger(__name__)

# Panjang maksimal satu ucapan. Diturunkan dari pengukuran, bukan ditebak:
# TERUKUR ~64 ms per huruf, jadi 160 huruf ≈ 10 detik — batas atas yang masih
# masuk akal untuk satu kabar antar-langkah.
_MAKS_UCAP = 160
# Batas untuk JAWABAN AKHIR — jauh lebih longgar, dan sengaja. Batas 160 huruf
# di atas ada supaya suara tak tertinggal saat langkah datang beruntun; pada
# jawaban akhir tak ada langkah berikutnya yang perlu dikejar, jadi memotongnya
# cuma membuat pesannya terdengar separuh. Tetap ada batasnya supaya jawaban
# sepanjang halaman tak dibacakan bermenit-menit.
_MAKS_UCAP_AKHIR = 900
# Batas potongan LANJUTAN jawaban akhir yang dibacakan per kalimat, dan batas
# TOTALnya. Dua kali lipat _MAKS_UCAP_AKHIR itu sengaja: sejak jawaban bisa
# DISELA kapan saja (barge-in di dengar.py), pengguna tak lagi terjebak
# mendengarkan bacaan panjang tanpa jalan keluar — jadi batas lamanya boleh
# dilonggarkan.
_MAKS_UCAP_LANJUT = 320
_MAKS_TOTAL = 2000
# Antrean kabar LANGKAH hanya menyimpan yang TERBARU — tiap kabar baru
# mengosongkan antrean (lihat _dahului). Pengecualian: potongan LANJUTAN satu
# jawaban akhir (sambung) tidak saling mengalahkan, jadi satu jawaban panjang
# tetap dibacakan utuh.
_MAKS_ANTRE = 1
# Kecepatan bicara SAPI (-10..10). TERUKUR untuk kalimat 67 huruf: rate 0 = 5,9
# detik, rate 2 = 4,3 detik, rate 4 = 3,4 detik. Dipilih 2 — cukup cepat untuk
# mengejar langkah beruntun, belum terdengar terburu-buru.
_RATE = 2
# Suara Indonesia natural, laki-laki lebih dulu; keduanya dicoba supaya satu
# suara yang sedang bermasalah di sisi layanan tak mematikan fiturnya.
_SUARA_EDGE = ("id-ID-ArdiNeural", "id-ID-GadisNeural")
# Batas menyiapkan suara lewat jaringan. Lebih lama dari ini, kabarnya sudah
# basi duluan — lebih baik turun ke mesin luring.
_TIMEOUT_EDGE = 12.0
# Berapa kali BERTURUT-TURUT sebuah mesin harus gagal sebelum ditinggalkan.
# Bukan 1: gangguan sesaat (jaringan tersendat sedetik, berkas masih terpegang
# pemutar) akan menukar suara untuk seluruh sesi, dan bagi pengguna itu tampak
# seperti suaranya berubah sendiri tanpa sebab.
_BATAS_GAGAL = 3
# Berapa lama sebuah kabar harus SEMPAT TERDENGAR sebelum boleh dipotong kabar
# yang lebih baru.
#
# Inilah pembatas yang membuat fitur ini tetap berbunyi di laptop pelan. Aturan
# "kabar terbaru menang" tanpa syarat masuk akal di mesin cepat, tapi di laptop
# 4 GB menyiapkan+membunyikan satu kabar makan beberapa detik sementara langkah
# berikutnya sudah datang — jadi setiap kabar dipotong SEBELUM satu kata pun
# keluar, dan yang terdengar cuma kabar pertama. Persis gejala yang dilaporkan:
# "awal-awalnya ada, setelah itu hilang tanpa sebab".
#
# Dengan pembatas ini, kabar yang belum sempat berbunyi dibiarkan selesai dulu;
# yang terbaru menunggu di antrean (isinya cuma satu, jadi tak pernah
# tertinggal lebih dari satu kabar) dan langsung menyusul sesudahnya.
_MIN_DENGAR = 2.0
# Menyiapkan suara natural lebih lama dari ini = terlalu lambat untuk berguna.
# Berbeda dari _TIMEOUT_EDGE yang menjaring kegagalan, ini menjaring KEBERHASILAN
# yang datangnya kelewat telat — di laptop lemot itu yang jauh lebih sering.
_EDGE_LAMBAT = 6.0
# Berapa kali berturut-turut boleh selambat itu sebelum pindah ke mesin luring.
_BATAS_LAMBAT = 2
# Berapa lama sebuah mesin DIISTIRAHATKAN sesudah gagal berulang.
#
# Dulu ia dicoret PERMANEN untuk sesi itu. Akibatnya fatal justru pada laptop
# yang cuma punya satu mesin yang memenuhi syarat bahasa: satu gangguan jaringan
# yang agak panjang menghabiskan seluruh jatah kegagalan, mesinnya dicoret, dan
# sisa sesi bisu total — bahkan setelah internetnya kembali. Diistirahatkan
# sebentar memberi keduanya: tak ada percobaan sia-sia tiap kabar, tapi tetap
# pulih sendiri.
_ISTIRAHAT = 120.0
# Menganggur selama ini -> proses pembantu dilepas (hemat memori di laptop kecil).
# Jangan terlalu pendek: menyalakannya lagi memakan waktu, dan di laptop pelan
# waktu itu jatuh tepat di kabar PERTAMA giliran berikutnya.
_JEDA_NGANGGUR = 90.0

# Proses pembantu Windows: SAPI + pemutar berkas suara dalam SATU proses.
# Protokolnya satu baris per perintah, "S"/"P" lalu muatan Base64 (Base64 supaya
# teks & path apa pun aman lewat stdin — kutip, aksen, spasi tak perlu di-escape).
# Baris status yang dikirim balik ke Python:
#   SIAP  - proses siap menerima perintah
#   MULAI - bunyinya BARU SAJA keluar dari pengeras suara (bukan sekadar
#           perintahnya diterima). Dipakai menghitung "sudah sempat terdengar";
#           tanpa ini, waktu memuat mp3 — yang bisa beberapa detik — ikut
#           terhitung sebagai waktu terdengar, sehingga kabar bisa dipotong
#           sebelum satu kata pun keluar.
#   TOLAK - perintahnya menolak dijalankan (mis. diminta bicara padahal tak ada
#           suara Indonesia terpasang). BEDA dari gagal: mesinnya sehat, cuma
#           tak boleh dipakai untuk ini.
#   OK    - perintah selesai
_SKRIP = r"""
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Speech
try { Add-Type -AssemblyName presentationCore } catch { }
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
# HANYA suara berkultur id-*. Kalau tak ada, perintah bicara DITOLAK — bukan
# dibacakan dengan suara Inggris. Suara Inggris yang melafalkan kalimat
# Indonesia sulit dimengerti dan terdengar seperti suara asing yang muncul
# sendiri; diam jauh lebih baik daripada itu.
$idv = $s.GetInstalledVoices() | Where-Object { $_.Enabled } |
       Where-Object { $_.VoiceInfo.Culture.Name -like 'id*' } |
       Select-Object -First 1
if ($idv) { $s.SelectVoice($idv.VoiceInfo.Name) }
$s.Rate = RATE
$s.Volume = 100
[Console]::Out.WriteLine('SIAP')
[Console]::Out.Flush()
while ($null -ne ($baris = [Console]::In.ReadLine())) {
  if ($baris.Length -lt 2) { continue }
  $jenis = $baris.Substring(0,1)
  if ($jenis -eq 'S' -and -not $idv) {
    [Console]::Out.WriteLine('TOLAK'); [Console]::Out.Flush(); continue
  }
  try {
    $isi = [System.Text.Encoding]::UTF8.GetString(
             [System.Convert]::FromBase64String($baris.Substring(1)))
    if ($jenis -eq 'S') {
      [Console]::Out.WriteLine('MULAI'); [Console]::Out.Flush()
      $s.Speak($isi)
    } elseif ($jenis -eq 'P') {
      $mp = New-Object System.Windows.Media.MediaPlayer
      $mp.Open([uri]$isi)
      $n = 0
      while (-not $mp.NaturalDuration.HasTimeSpan -and $n -lt 160) {
        Start-Sleep -Milliseconds 25; $n++
      }
      if ($mp.NaturalDuration.HasTimeSpan) {
        # Diumumkan TEPAT sebelum Play(), sesudah pemuatan selesai.
        [Console]::Out.WriteLine('MULAI'); [Console]::Out.Flush()
        $mp.Play()
        Start-Sleep -Milliseconds ($mp.NaturalDuration.TimeSpan.TotalMilliseconds + 150)
      }
      $mp.Close()
    }
  } catch { }
  [Console]::Out.WriteLine('OK')
  [Console]::Out.Flush()
}
"""

# --- pembersihan teks -------------------------------------------------------
_BLOK_KODE = re.compile(r"```.*?```", re.S)
_KODE_SEBARIS = re.compile(r"`[^`]*`")
# BLOK protokol beserta ISINYA — bukan cuma penandanya. Membuang penanda saja
# menyisakan JSON perintah tool dan seluruh keluaran tool, lalu laptop
# membacakan '{"name": "write_file", "arguments"…' sebagai kalau itu kabar dari
# model. Yang pantas didengar hanya kalimat yang ditulis modelnya sendiri.
_BLOK_PROTOKOL = re.compile(
    r"\[\[\s*(TOOL|HASIL)[^\]]*\]\].*?\[\[\s*/\s*\1[^\]]*\]\]",
    re.IGNORECASE | re.S)
# Blok yang TAK PERNAH DITUTUP (pesan terpotong situs): semua yang mengekor di
# belakang pembuka yatim adalah muatan mesin yang belum selesai.
_BUKA_YATIM = re.compile(r"\[\[\s*(TOOL|HASIL)[^\]]*\]\]", re.IGNORECASE)
_PENANDA = re.compile(r"\[\[\s*/?\s*(TOOL|HASIL|SISTEM|TIM)[^\]]*\]\]",
                      re.IGNORECASE)
_TAUTAN = re.compile(r"https?://\S+")
# PATH BERKAS — dan hanya itu. Polanya dulu `[\w.-]+[/\\][\w./\\-]+`, yang
# menyapu setiap kata bergaris miring lalu menyisakan potongan terakhirnya saja.
# TERUKUR merusak arti kalimat yang dibacakan:
#     "Pakai opsi dan/atau keduanya."  -> "Pakai opsi atau keduanya."
#     "Rapat 5/8/2026 pukul 10."       -> "Rapat 2026 pukul 10."
#     "Rasio 16/9 dipakai di sini."    -> "Rasio 9 dipakai di sini."
# Yang terdengar jadi BERBEDA dari yang tertulis di layar — cacat paling parah
# yang bisa dipunyai fitur pembaca.
#
# Sekarang syaratnya dipersempit: potongan terakhirnya harus benar-benar
# berbentuk nama berkas (berakhiran titik + 1-6 huruf/angka), ATAU jalurnya
# diawali penanda folder yang tak mungkin muncul di kalimat biasa (./ ../ / \
# C:\). Tanggal, pecahan, rasio, dan "dan/atau" tak lagi tersentuh.
_JALUR = re.compile(
    r"(?:(?:\.{1,2}[/\\]|[A-Za-z]:[/\\]|(?<![\w/])[/\\])[\w.\-]+"
    r"(?:[/\\][\w.\-]+)*"
    r"|[\w.\-]+(?:[/\\][\w.\-]+)+\.[A-Za-z0-9]{1,6})")
_HIAS = re.compile(r"[*_#>|`~]+")
_EMOJI = re.compile("[" "\U0001f300-\U0001faff" "☀-➿" "]+")
_SPASI = re.compile(r"\s+")


def _nama_saja(jalur: str) -> str:
    """`src/agent/tim.py` -> "tim". Nama berkasnya saja, tanpa folder & akhiran.

    Membuang path sepenuhnya membuat kabarnya kehilangan maksud ("Kuperbarui
    sekarang." — memperbarui APA?), sementara membacakannya utuh menghasilkan
    ejaan garis-miring yang panjang dan tak enak. Nama berkasnya sudah cukup
    untuk tahu yang dikerjakan."""
    dasar = re.split(r"[/\\]", jalur.strip())[-1]
    return re.sub(r"\.[A-Za-z0-9]{1,6}$", "", dasar) or dasar


def _ganti_kode(m: re.Match) -> str:
    """Isi backtick: path jadi nama berkasnya, sisanya dibiarkan terbaca.

    Nama fungsi/perintah di dalam backtick memang pantas didengar; yang tak
    pantas cuma path panjangnya."""
    isi = m.group(0).strip("`").strip()
    if not isi:
        return " "
    if re.search(r"[/\\]", isi) or re.search(r"\.[A-Za-z0-9]{1,6}$", isi):
        return f" {_nama_saja(isi)} "
    return f" {isi} "


def _ganti_jalur(m: re.Match) -> str:
    return f" {_nama_saja(m.group(0))} "


def bersihkan(teks: str, maks: int = _MAKS_UCAP) -> str:
    """Sisakan yang PANTAS didengar dari sepotong kabar.

    Yang dibuang bukan sekadar hiasan: blok kode, path berkas, dan tautan kalau
    dibacakan berubah jadi deretan simbol panjang yang menutupi kalimat
    intinya — dan justru kalimat itu satu-satunya alasan fitur ini ada."""
    # Urutannya penting: blok protokol dibuang BESERTA isinya lebih dulu,
    # sebelum apa pun yang lain. Kalau penandanya dibuang duluan, isinya
    # (JSON perintah tool & keluaran tool) berubah jadi teks biasa yang tak
    # bisa dibedakan lagi dari kalimat model.
    t = _BLOK_PROTOKOL.sub(" ", teks or "")
    yatim = _BUKA_YATIM.search(t)
    if yatim:
        t = t[:yatim.start()]
    t = _BLOK_KODE.sub(" ", t)
    t = _KODE_SEBARIS.sub(_ganti_kode, t)
    t = _PENANDA.sub(" ", t)
    t = _TAUTAN.sub(" tautan ", t)
    t = _JALUR.sub(_ganti_jalur, t)
    t = _EMOJI.sub(" ", t)
    t = _HIAS.sub(" ", t)
    t = _SPASI.sub(" ", t).strip()
    if len(t) <= maks:
        return t
    # Dipotong di batas KALIMAT bila ada, supaya tak berhenti di tengah kata.
    potong = t[:maks]
    for tanda in (". ", "! ", "? ", "; ", ", "):
        i = potong.rfind(tanda)
        if i > maks // 2:
            return potong[:i + 1].strip()
    return potong.rsplit(" ", 1)[0].strip()


def potong_kalimat(teks: str) -> list[str]:
    """Pecah JAWABAN AKHIR jadi potongan kalimat yang layak dibacakan.

    Potongan PERTAMA pendek (≤ _MAKS_UCAP) supaya bacaan bermula cepat;
    sisanya boleh lebih panjang. Tanpa pemecahan ini, jawaban dibacakan sebagai
    SATU bacaan: seluruhnya harus selesai disiapkan lebih dulu, dan selama
    dibacakan mikrofon tak bisa dipakai — persis keluhan "suaranya mengabaikan
    aku". Per kalimat, potongan pertama terdengar lebih cepat dan sisanya
    menyusul sambil potongan sebelumnya masih berbunyi."""
    t = bersihkan(teks, _MAKS_TOTAL)
    if not t:
        return []
    kalimat = [k.strip() for k in re.split(r"(?<=[.!?…])\s+|\n+", t)]
    kalimat = [k for k in kalimat if k]
    if not kalimat:
        return []
    # Kalimat tunggal yang sendirinya kepanjangan tetap dibelah — di batas
    # KATA, bukan di tengah suku kata, supaya yang terdengar masih wajar.
    pecahan: list[str] = []
    for k in kalimat:
        while len(k) > _MAKS_UCAP_LANJUT:
            i = k.rfind(" ", 0, _MAKS_UCAP_LANJUT)
            if i <= 0:
                i = _MAKS_UCAP_LANJUT
            pecahan.append(k[:i].strip())
            k = k[i:].strip()
        if k:
            pecahan.append(k)
    out: list[str] = []
    for p in pecahan:
        i = len(out) - 1
        if i >= 0:
            # potongan ke-0 (pembuka) dijaga pendek; sisanya boleh panjang
            batas = _MAKS_UCAP if i == 0 else _MAKS_UCAP_LANJUT
            if len(out[i]) + 1 + len(p) <= batas:
                out[i] = f"{out[i]} {p}"
                continue
        out.append(p)
    return out


def sapu_berkas_suara(umur: float = 3600.0) -> int:
    """Buang berkas suara sisa di folder sementara. Kembalikan jumlahnya.

    Berkas mp3 dihapus sesudah dibunyikan, tapi TIDAK selalu berhasil: pemutar
    yang baru dimatikan bisa masih memegangnya, dan sesi yang berakhir mendadak
    tak sempat membersihkan apa pun. TERUKUR menumpuk 6 berkas / 221 KB hanya
    dari beberapa sesi pengembangan.

    `umur` menjaga agar berkas milik bagas-ai LAIN yang sedang berjalan tak
    ikut terhapus di tengah ia membunyikannya."""
    import glob

    batas = time.time() - umur
    n = 0
    for f in glob.glob(os.path.join(tempfile.gettempdir(),
                                    "bagasai-suara-*.mp3")):
        try:
            if os.path.getmtime(f) <= batas:
                os.remove(f)
                n += 1
        except OSError:
            pass          # dipegang proses lain / sudah hilang
    return n


def _edge_ada() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# Hasil penyelidikan suara sistem — mahal (memanggil PowerShell / `say`), jadi
# dijawab sekali per sesi.
_PUNYA_ID: dict[str, Any] = {}


def sapi_indonesia() -> bool:
    """Windows ini punya suara SAPI berbahasa Indonesia yang terpasang?

    Inilah syarat yang dulu tak pernah diperiksa. Windows kebanyakan hanya
    membawa suara Inggris (TERUKUR di laptop pengguna: hanya 'Microsoft David
    Desktop|en-US' dan 'Microsoft Zira Desktop|en-US'), sehingga cadangan luring
    selalu membacakan kalimat Indonesia dengan pelafalan Amerika."""
    if "sapi" not in _PUNYA_ID:
        _PUNYA_ID["sapi"] = any(
            b.rsplit("|", 1)[-1].strip().lower().startswith("id")
            for b in suara_tersedia())
    return bool(_PUNYA_ID["sapi"])


def say_indonesia() -> str:
    """Nama suara Indonesia macOS ("" bila tak ada). Mis. 'Damayanti'."""
    if "say" not in _PUNYA_ID:
        nama = ""
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True, timeout=20)
            for baris in (out.stdout or "").splitlines():
                # Bentuk barisnya: "Damayanti          id_ID    # Halo, ..."
                bagian = baris.split()
                if len(bagian) >= 2 and bagian[1].lower().startswith("id"):
                    nama = bagian[0]
                    break
        except Exception:  # noqa: BLE001
            nama = ""
        _PUNYA_ID["say"] = nama
    return str(_PUNYA_ID["say"])


def mesin_tersedia() -> list[str]:
    """Mesin yang bisa berbahasa INDONESIA di laptop ini, terbaik lebih dulu.

    Mesin yang cuma bisa membunyikan teks — tanpa suara Indonesia — sengaja
    TIDAK dianggap tersedia. Lihat aturan di docstring modul: lebih baik diam
    daripada berbunyi dalam bahasa yang salah."""
    out: list[str] = []
    if _edge_ada():
        out.append("edge")
    if sys.platform == "win32":
        if sapi_indonesia():
            out.append("sapi")
    elif sys.platform == "darwin":
        if shutil.which("say") and say_indonesia():
            out.append("say")
    else:
        # espeak/espeak-ng membawa pelafalan `id` di dalam paketnya sendiri,
        # jadi tak ada yang perlu terpasang terpisah.
        if shutil.which("spd-say") or shutil.which("espeak-ng") \
                or shutil.which("espeak"):
            out.append("espeak")
    return out


def alasan_diam() -> str:
    """Kenapa laptop ini tak bisa berbunyi sama sekali ("" bila bisa)."""
    if mesin_tersedia():
        return ""
    if sys.platform == "win32":
        return ("tak ada suara Indonesia di laptop ini. Pasang paket suaranya "
                "lewat `pip install edge-tts` (butuh internet saat dipakai), "
                "atau tambahkan suara Indonesia lewat Settings > Time & "
                "language > Speech. Suara Inggris sengaja TIDAK dipakai.")
    return "tak ada mesin suara berbahasa Indonesia di sistem ini"


class Pengucap:
    """Antrean ucapan + mesin yang membacakannya."""

    def __init__(self, rate: int = _RATE) -> None:
        self.rate = max(-10, min(10, int(rate)))
        # Tiap item: (teks, sambung). `sambung` menandai potongan LANJUTAN satu
        # jawaban akhir — ia tak boleh terbuang saat kabar langkah baru datang,
        # sebab jawaban yang dibacakan setengah lalu berhenti lebih membingungkan
        # daripada kabar yang menyusul beberapa detik.
        self._antre: queue.Queue[tuple[str, bool]] = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._kunci = threading.Lock()
        self._mati = False
        self._loop = None            # loop asyncio khusus edge-tts
        # Nomor giliran ucapan. Naik tiap kali ucapan yang sedang berjalan
        # DIPOTONG. Tanpa penanda ini, pemotongan yang disengaja tak bisa
        # dibedakan dari mesin suara yang benar-benar gagal — dan pengucap akan
        # menganggap mesinnya rusak lalu turun lapis permanen, jadi satu kabar
        # baru saja cukup untuk menjatuhkan suara Indonesia natural ke suara
        # bawaan Windows selamanya.
        self._gen = 0
        self._urut = 0               # penomor berkas suara (nama selalu baru)
        # Kegagalan BERUNTUN mesin teratas. Satu kegagalan sesaat (jaringan
        # tersendat, berkas masih terkunci) tak boleh menukar suara untuk
        # seluruh sesi — perubahan suara yang tak jelas sebabnya jauh lebih
        # mengganggu daripada satu kabar yang telat sedetik.
        self._gagal_beruntun = 0
        # Sejak kapan kabar yang sekarang BENAR-BENAR terdengar (None = tak ada
        # yang sedang berbunyi; masih disiapkan juga dihitung None). Dipakai
        # _dahului untuk memutuskan boleh-tidaknya memotong.
        self._mulai_bunyi: float | None = None
        # Berapa kali berturut-turut mesin daring menyiapkan suara kelewat lama.
        self._lambat_beruntun = 0
        # mesin -> saat paling awal ia boleh dicoba lagi (lihat _ISTIRAHAT).
        # Menggantikan pencoretan permanen: mesin yang gagal karena laptop
        # sedang luring harus bisa dipakai lagi begitu internetnya kembali.
        self._istirahat: dict[str, float] = {}
        # Urutan mesin MILIK SESI, bukan milik thread. Dulu daftarnya dibuat
        # ulang tiap kali thread pengucap menyala, sehingga turun-lapis yang
        # sudah diputuskan hilang begitu pengucap sempat menganggur — di laptop
        # yang luring, tiap giliran baru membayar lagi 3 x 12 detik hening
        # sebelum menyerah ke suara Windows. Itu sendiri sudah cukup membuat
        # suaranya terasa "hilang".
        self._urutan: list[str] | None = None
        self.mesin: str = ""         # yang sedang dipakai (diisi saat jalan)
        self.galat: str = ""         # alasan kalau tak berbunyi

    # ---- pemakaian ----
    def ucap(self, teks: str, penuh: bool = False,
             sambung: bool = False) -> None:
        """Antrekan satu kabar. Tak pernah melempar, tak pernah menahan.

        `penuh=True` untuk JAWABAN AKHIR: dibacakan jauh lebih panjang, sebab
        tak ada langkah berikutnya yang perlu dikejar. `sambung=True` untuk
        potongan LANJUTAN jawaban itu: tidak mengalahkan apa pun dan tak
        dibuang oleh kabar langkah baru — dipakai ucap_panjang()."""
        if self._mati:
            return
        bersih = bersihkan(teks, _MAKS_UCAP_AKHIR if penuh else _MAKS_UCAP)
        if not bersih:
            return
        # KABAR TERBARU MENANG: yang mengantre dibuang, dan yang sedang
        # diucapkan dipotong BILA sudah sempat terdengar. Suara itu berurutan
        # sementara pekerjaan berjalan terus, jadi membiarkan yang lama selesai
        # semuanya berarti pengguna mendengar langkah yang SUDAH LEWAT — makin
        # lama makin tertinggal. Syarat "sudah sempat terdengar" itulah yang
        # menjaga agar aturan ini tak berbalik jadi kesunyian di laptop pelan
        # (lihat _MIN_DENGAR).
        if not sambung:
            self._dahului()
        self._antre.put((bersih, sambung))
        self._pastikan_jalan()

    def ucap_panjang(self, teks: str) -> None:
        """Bacakan JAWABAN AKHIR per potongan kalimat.

        Dulu jawaban akhir dibacakan sebagai SATU bacaan: seluruhnya harus
        selesai disiapkan lebih dulu, dan selama dibacakan pengguna tak bisa
        menyela. Dipecah per kalimat, potongan pertama terdengar lebih cepat,
        sisanya menyusul, dan penyela (barge-in) cukup memotong di batas
        kalimat yang sedang berbunyi."""
        if self._mati:
            return
        bagian = potong_kalimat(teks)
        if not bagian:
            return
        self.ucap(bagian[0], penuh=True)
        for lanjut in bagian[1:]:
            self.ucap(lanjut, penuh=True, sambung=True)

    def _buang_antre(self, simpan_sambung: bool = False) -> None:
        """Kosongkan antrean kabar. Potongan LANJUTAN boleh dipertahankan."""
        sisa: list[tuple[str, bool]] = []
        while True:
            try:
                item = self._antre.get_nowait()
            except queue.Empty:
                break
            if simpan_sambung and item[1]:
                sisa.append(item)
        for item in sisa:
            self._antre.put(item)

    def _hentikan(self) -> None:
        """Matikan ucapan yang sedang berbunyi SEKARANG juga.

        Nomor gilirannya dinaikkan supaya thread pengucap tahu kegagalan yang
        sebentar lagi ia lihat adalah pemotongan yang DISENGAJA, bukan mesin
        suara yang rusak.

        Proses pembantu HANYA dibunuh kalau memang sedang membunyikan sesuatu.
        Dulu ia dibunuh tanpa syarat, dan karena diam() dipanggil di SETIAP
        akhir giliran, pembantu yang sehat & menganggur ikut mati tiap kali —
        lalu dinyalakan lagi dari nol pada kabar pertama giliran berikutnya.
        TERUKUR di laptop cepat 0,45–0,53 detik sekali nyala; di laptop pelan
        beberapa detik, dan jatuh tepat di saat kabar pertama harus berbunyi."""
        with self._kunci:
            self._gen += 1
            berbunyi = self._mulai_bunyi is not None
            self._mulai_bunyi = None
            p = self._proc
            if berbunyi:
                self._proc = None
        if berbunyi and p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def _potong(self) -> None:
        """Berhenti TOTAL: antrean dibuang & yang sedang berbunyi dimatikan.

        Dipakai saat giliran dibatalkan/selesai — di sana memang tak ada lagi
        yang pantas didengar, jadi tak ada syarat apa pun."""
        self._buang_antre()
        self._hentikan()

    def _dahului(self) -> None:
        """Beri jalan untuk kabar baru — TANPA membuat semuanya bisu.

        Bedanya dengan _potong: yang sedang berbunyi hanya dipotong kalau sudah
        sempat terdengar _MIN_DENGAR detik. Kalau belum, ia dibiarkan selesai
        dan kabar barunya menunggu sebentar di antrean.

        Aturan ini lahir dari laptop pelan (i3, RAM kecil): di sana menyiapkan
        satu kabar makan beberapa detik, sementara kabar berikutnya datang
        lebih cepat dari itu. Memotong tanpa syarat berarti TAK SATU PUN kabar
        pernah selesai — pengeras suara diam total sesudah kabar pertama.
        Membiarkannya selesai memang membuat suara tertinggal sedikit, tapi
        tertinggal sedikit masih jauh lebih berguna daripada senyap.

        Yang dibuang hanya kabar LANGKAH: potongan lanjutan jawaban akhir
        (sambung) tetap di antrean, sebab jawaban yang berhenti setengah
        mengubah "kubacakan jawabannya" menjadi teka-teki."""
        self._buang_antre(simpan_sambung=True)
        with self._kunci:
            mulai = self._mulai_bunyi
        if mulai is None:
            # Belum ada bunyi yang keluar (masih disiapkan, atau memang tak ada
            # yang jalan). Membunuh proses pembantu di sini cuma menambah
            # ongkos menyalakannya lagi — yang di laptop pelan justru mahal.
            return
        if (time.monotonic() - mulai) < _MIN_DENGAR:
            return
        self._hentikan()

    def diam(self) -> None:
        """Hentikan yang sedang diucapkan & buang antreannya.

        Prosesnya DIMATIKAN, bukan diminta berhenti: selagi Speak/Play berjalan
        ia tak membaca stdin sama sekali, jadi tak ada perintah yang bisa
        sampai. Proses baru dinyalakan lagi saat ada kabar berikutnya."""
        self._potong()

    def tutup(self) -> None:
        """Matikan pengucap seterusnya (dipanggil saat bagas-ai keluar)."""
        self._mati = True
        self.diam()
        # Proses pembantu yang sedang MENGANGGUR tak ikut mati lewat diam()
        # (lihat _hentikan), jadi ia dibereskan di sini — kalau tidak, ia
        # tertinggal hidup sesudah bagas-ai ditutup.
        with self._kunci:
            p, self._proc = self._proc, None
            loop, self._loop = self._loop, None
        if p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        # Loop asyncio edge-tts dulu tak pernah ditutup sama sekali; soket &
        # deskriptor berkas miliknya menggantung sampai proses berakhir.
        if loop is not None:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
        sapu_berkas_suara()

    # ---- mesin ----
    def _pastikan_jalan(self) -> None:
        with self._kunci:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._kerja, daemon=True,
                                            name="bagasai-suara")
            self._thread.start()

    def _kerja(self) -> None:
        if self._urutan is None:
            self._urutan = mesin_tersedia()
        urutan = self._urutan
        if not urutan:
            self.galat = "tak ada mesin suara di sistem ini"
            return
        self.mesin = urutan[0]
        while not self._mati:
            try:
                item = self._antre.get(timeout=_JEDA_NGANGGUR)
            except queue.Empty:
                # Lama menganggur -> lepaskan prosesnya. Menahan PowerShell
                # hidup sepanjang sesi hanya untuk berjaga-jaga itu boros —
                # dan di laptop RAM kecil itu bukan penghematan sepele.
                #
                # Penandanya dilepas DI DALAM kunci yang sama dengan pemeriksaan
                # antrean: tanpa itu ada celah sempit di mana ucap() melihat
                # thread ini "masih hidup" lalu tak menyalakan penggantinya,
                # padahal sedetik kemudian ia pergi — dan kabar yang barusan
                # diantrekan tak pernah dibacakan siapa pun.
                #
                # Pelepasan pembantunya dikerjakan DI DALAM kunci yang sama,
                # dan TIDAK lewat _potong(). Dulu _potong() dipanggil di luar
                # kunci sesudah penanda dilepas: di sela sempit itu ucap() bisa
                # menyalakan thread pengganti yang langsung bicara, lalu thread
                # lama ini membunuh prosesnya. Kabar itu hilang tanpa jejak —
                # kematiannya bahkan tercatat sebagai "pemotongan yang
                # disengaja", jadi tak ada yang menghitungnya sebagai kegagalan.
                with self._kunci:
                    if not self._antre.empty():
                        continue
                    self._thread = None
                    p, self._proc = self._proc, None
                if p is not None:
                    try:
                        p.kill()
                    except Exception:  # noqa: BLE001
                        pass
                return
            with self._kunci:
                gen = self._gen
            try:
                self._ucapkan(item[0], urutan, gen)
            except Exception:  # noqa: BLE001 - thread ini TAK BOLEH mati diam-diam
                # Kalau thread pengucap berhenti karena galat tak terduga,
                # fiturnya tampak "hilang tanpa sebab": layar terus jalan,
                # pengeras suara diam selamanya, dan tak ada satu pun tanda.
                log.debug("kabar gagal diucapkan", exc_info=True)

    def _dipotong(self, gen: int | None) -> bool:
        """Giliran ucapan ini sudah didahului kabar yang lebih baru?

        `gen=None` berarti pemanggil tak melacak giliran sama sekali, dan
        jawabannya HARUS "tidak". Bawaannya dulu -1 — angka yang tak pernah
        sama dengan nomor giliran mana pun, sehingga tiap kegagalan dikira
        pemotongan dan mesin yang benar-benar rusak tak pernah diganti."""
        if gen is None:
            return False
        with self._kunci:
            return gen != self._gen

    def _ucapkan(self, teks: str, urutan: list[str],
                 gen: int | None = None) -> bool:
        """Ucapkan satu kabar; turun lapis bila mesin teratas gagal.

        `gen` = nomor giliran saat kabar ini diambil. Kalau nomornya sudah
        berubah, kegagalan yang terlihat di sini BUKAN mesin yang rusak
        melainkan pemotongan yang disengaja — dan menurunkan lapis karenanya
        akan mematikan mesin terbaik hanya karena pengguna mendapat kabar
        baru."""
        # MESIN YANG BERHASIL TAPI KELEWAT LAMBAT sama tak bergunanya dengan
        # mesin yang gagal: kabarnya baru berbunyi saat langkah di layar sudah
        # berganti dua kali. Ini keadaan khas laptop pelan — di sana suara
        # Windows yang berbunyi SEKARANG mengalahkan suara natural yang
        # berbunyi tujuh detik lagi. Sebabnya dicatat di `galat` supaya
        # pergantian suaranya bisa dijelaskan (lihat /mic), bukan tampak
        # berubah sendiri.
        siap = self._mesin_siap(urutan)
        if (self._lambat_beruntun >= _BATAS_LAMBAT
                and len(siap) > 1 and siap[0] == "edge"):
            self._lambat_beruntun = 0
            self._istirahatkan("edge")
            siap = self._mesin_siap(urutan)
            self.galat = (f"suara natural terlalu lambat disiapkan di laptop "
                          f"ini (≥{_EDGE_LAMBAT:.0f} detik/kabar) — memakai "
                          f"{siap[0] if siap else 'tak ada'}")
        while siap:
            mesin = siap[0]
            self.mesin = mesin
            try:
                if mesin == "edge":
                    berkas = self._edge_buat(teks)
                    # Menyiapkan suara lewat jaringan makan beberapa detik;
                    # kabar bisa keburu basi sebelum satu kata pun terdengar.
                    if self._dipotong(gen):
                        self._buang(berkas)
                        return True
                    if berkas and self._windows_kirim("P", berkas):
                        self._buang(berkas)
                        self._gagal_beruntun = 0
                        return True
                    self._buang(berkas)
                elif mesin == "sapi":
                    if self._windows_kirim("S", teks):
                        self._gagal_beruntun = 0
                        return True
                elif mesin == "say":
                    # Suaranya DISEBUT eksplisit: tanpa -v, `say` memakai suara
                    # bawaan sistem yang di hampir semua Mac berbahasa Inggris.
                    if self._jalankan_luar(
                            ["say", "-v", say_indonesia(), teks]):
                        self._gagal_beruntun = 0
                        return True
                elif mesin == "espeak":
                    alat = (shutil.which("spd-say") or shutil.which("espeak-ng")
                            or shutil.which("espeak"))
                    if alat:
                        bendera = ["-l", "id"] if alat.endswith("spd-say") \
                            else ["-v", "id"]
                        if self._jalankan_luar([alat, *bendera, teks]):
                            self._gagal_beruntun = 0
                            return True
            except Exception:  # noqa: BLE001 - mesin ini gagal; coba yang bawah
                log.debug("mesin suara %s gagal", mesin, exc_info=True)
            # DIPOTONG, bukan gagal: kabar yang lebih baru sudah masuk dan
            # mematikan prosesnya. Berhenti di sini tanpa menyalahkan mesinnya —
            # kalau tidak, tiap kali pengguna dapat kabar baru, mesin terbaik
            # dicoret satu per satu sampai habis.
            if self._dipotong(gen):
                self._gagal_beruntun = 0
                return True
            # Gagal SEKALI belum tentu mesinnya rusak: jaringan bisa tersendat
            # sebentar, berkas bisa masih terpegang pemutar yang baru dimatikan.
            # Menukar suara karena itu membuat suaranya berubah tanpa sebab yang
            # bisa dilihat pengguna. Kabar ini dilewatkan saja; mesinnya baru
            # diganti kalau memang gagal berkali-kali berturut-turut.
            self._gagal_beruntun += 1
            if self._gagal_beruntun < _BATAS_GAGAL:
                self.galat = (f"{mesin} gagal sekali "
                              f"({self._gagal_beruntun}/{_BATAS_GAGAL}) — "
                              "suara tak diganti")
                return False
            # Sudah gagal berkali-kali: ini bukan gangguan sesaat. Mesinnya
            # DIISTIRAHATKAN — bukan dicoret selamanya. Mencoba mesin daring
            # pada tiap kabar memang menambah hening ke setiap langkah, tapi
            # mencoretnya permanen membuat laptop yang cuma punya satu mesin
            # berbahasa Indonesia bisu sampai bagas-ai ditutup, bahkan setelah
            # internetnya kembali.
            self._gagal_beruntun = 0
            self._istirahatkan(mesin)
            siap = self._mesin_siap(urutan)
            if siap:
                self.galat = f"{mesin} gagal {_BATAS_GAGAL}x, memakai {siap[0]}"
        if not urutan:
            self.galat = alasan_diam() or "tak ada mesin suara berbahasa Indonesia"
        else:
            self.galat = (
                f"semua mesin suara sedang bermasalah — dicoba lagi otomatis "
                f"dalam {_ISTIRAHAT / 60:.0f} menit")
        return False

    # --- istirahat mesin (menggantikan pencoretan permanen) ---
    def _istirahatkan(self, mesin: str) -> None:
        self._istirahat[mesin] = time.monotonic() + _ISTIRAHAT

    def _mesin_siap(self, urutan: list[str]) -> list[str]:
        """Mesin yang boleh dicoba SEKARANG, urut dari yang terbaik."""
        now = time.monotonic()
        return [m for m in urutan if self._istirahat.get(m, 0.0) <= now]

    def _jalankan_luar(self, perintah: list[str]) -> bool:
        """Jalankan pemutar suara luar (macOS/Linux) supaya BISA DIHENTIKAN.

        Dulu memakai subprocess.run — memblokir, tanpa pegangan proses. Tiga
        akibatnya: diam() tak bisa menghentikan suara sama sekali, aturan
        'kabar terbaru menang' tak pernah bekerja di luar Windows, dan satu
        perintah yang macet menahan pengucap sampai 90 detik."""
        try:
            p = subprocess.Popen(perintah, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            return False
        with self._kunci:
            self._proc = p
            self._mulai_bunyi = time.monotonic()
        try:
            p.wait(timeout=90)
            return p.returncode == 0
        except Exception:  # noqa: BLE001 - dibunuh _hentikan / kelamaan
            return False
        finally:
            with self._kunci:
                self._mulai_bunyi = None
                if self._proc is p:
                    self._proc = None

    # --- edge-tts (suara Indonesia natural) ---
    def _edge_buat(self, teks: str) -> str | None:
        """Buat berkas suara Indonesia natural. None bila gagal (mis. luring)."""
        import asyncio

        import edge_tts

        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        # Nama berkas HARUS baru tiap ucapan. Dulu namanya tetap, dan itu
        # TERBUKTI merusak: pemutar Windows memegang berkas yang sedang
        # dibunyikan, jadi begitu ucapan dipotong, ucapan berikutnya gagal
        # menulis ke nama yang sama — kegagalan itu lalu dikira "mesinnya
        # rusak" dan suaranya turun ke suara bawaan Windows, padahal tak ada
        # yang rusak sama sekali. Gejalanya persis: "kok tiba-tiba berubah
        # suara padahal tak ada error".
        self._urut += 1
        berkas = self._nama_berkas(os.getpid(), self._urut)

        async def buat() -> None:
            galat: Exception | None = None
            for suara in _SUARA_EDGE:
                try:
                    await edge_tts.Communicate(teks, suara).save(berkas)
                    return
                except Exception as exc:  # noqa: BLE001 - coba suara berikutnya
                    galat = exc
            if galat is not None:
                raise galat

        mulai = time.monotonic()
        try:
            self._loop.run_until_complete(
                asyncio.wait_for(buat(), timeout=_TIMEOUT_EDGE))
        except Exception:  # noqa: BLE001
            self._catat_lama(time.monotonic() - mulai)
            self._buang(berkas)
            return None
        self._catat_lama(time.monotonic() - mulai)
        if os.path.exists(berkas) and os.path.getsize(berkas) > 1024:
            return berkas
        self._buang(berkas)
        return None

    def _catat_lama(self, detik: float) -> None:
        """Catat lamanya menyiapkan suara natural — berhasil maupun tidak.

        Yang dihitung KEBERUNTUNAN lambat, bukan rata-rata: satu kabar yang
        kebetulan lama (jaringan tersendat) tak boleh menukar suara, sedangkan
        laptop yang memang selalu selambat itu harus segera pindah ke mesin
        luring alih-alih membuat tiap langkah diawali beberapa detik hening."""
        if detik >= _EDGE_LAMBAT:
            self._lambat_beruntun += 1
        else:
            self._lambat_beruntun = 0

    @staticmethod
    def _nama_berkas(pid: int, urut: int) -> str:
        return os.path.join(tempfile.gettempdir(),
                            f"bagasai-suara-{pid}-{urut}.mp3")

    @staticmethod
    def _buang(berkas: str | None) -> None:
        if not berkas:
            return
        try:
            os.remove(berkas)
        except OSError:
            pass

    # --- proses pembantu Windows (SAPI + pemutar) ---
    def _windows_kirim(self, jenis: str, muatan: str) -> bool:
        if sys.platform != "win32":
            return False
        with self._kunci:
            p = self._proc
        if p is None or p.poll() is not None:
            p = self._nyalakan()
            if p is None:
                return False
            with self._kunci:
                self._proc = p
        pesan = jenis + base64.b64encode(muatan.encode("utf-8")).decode("ascii")
        try:
            p.stdin.write(pesan + "\n")
            p.stdin.flush()
        except Exception:  # noqa: BLE001 - proses dibunuh oleh diam()
            with self._kunci:
                if self._proc is p:
                    self._proc = None
            return False
        # Sekarang tunggu kabar dari proses pembantu. Penanda "sudah terdengar"
        # dipasang saat ia mengirim MULAI — yaitu ketika bunyinya BENAR-BENAR
        # keluar, sesudah mp3-nya selesai dimuat. Dulu penanda itu dipasang
        # begitu perintahnya terkirim, sehingga waktu memuat (sampai 4 detik)
        # ikut terhitung sebagai waktu terdengar dan kabar bisa dipotong
        # sebelum satu kata pun keluar.
        try:
            while True:
                baris = p.stdout.readline()
                if not baris:                  # proses mati / dibunuh diam()
                    with self._kunci:
                        if self._proc is p:
                            self._proc = None
                    return False
                status = baris.strip()
                if status == "MULAI":
                    with self._kunci:
                        self._mulai_bunyi = time.monotonic()
                    continue
                if status == "OK":
                    return True
                if status == "TOLAK":
                    # Mesinnya sehat, cuma menolak bicara dalam bahasa yang
                    # salah. Bukan kegagalan yang perlu dicoba-coba lagi.
                    self.galat = ("suara Indonesia tak terpasang di Windows "
                                  "ini — suara Inggris sengaja tak dipakai")
                    return False
        except Exception:  # noqa: BLE001 - proses dibunuh oleh diam()
            with self._kunci:
                if self._proc is p:
                    self._proc = None
            return False
        finally:
            with self._kunci:
                self._mulai_bunyi = None

    def _nyalakan(self) -> subprocess.Popen | None:
        """Nyalakan proses pembantu. None bila tak bisa — dan itu bukan galat
        yang pantas menjatuhkan apa pun; cukup dicatat."""
        skrip = _SKRIP.replace("RATE", str(self.rate))
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", skrip],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            self.galat = f"PowerShell tak bisa dijalankan: {exc}"
            return None
        # Tunggu tanda SIAP: kalau Add-Type/suara gagal, prosesnya mati di sini
        # dan kegagalannya ketahuan SEKARANG — bukan nanti sebagai kesunyian
        # yang tak bisa dijelaskan.
        try:
            baris = p.stdout.readline() if p.stdout else ""
        except Exception:  # noqa: BLE001
            baris = ""
        if baris.strip() != "SIAP":
            self.galat = "mesin suara Windows tak mau menyala"
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
            return None
        self.galat = ""
        return p


# --- getaran penanda kesimpulan ---------------------------------------------
# Laptop tak punya motor getar, jadi "getaran" di sini ditiru dengan cara yang
# paling mendekati rasanya: dua dengung PENDEK berfrekuensi rendah. Rendah dan
# pendek itu disengaja — nadanya jatuh di bawah rentang suara orang bicara,
# sehingga tak bisa tertukar dengan kabar yang sedang dibacakan, dan telinga
# menangkapnya sebagai "tuk-tuk" alih-alih bunyi bip alarm.
#
# (frekuensi Hz, lama ms); frekuensi 0 = jeda hening.
_GETAR = ((110, 80), (0, 55), (110, 120))


def _getar_sekarang() -> None:
    """Bunyikan dengungnya. Dipanggil di thread sendiri — winsound menahan."""
    if sys.platform == "win32":
        try:
            import winsound
            for hz, ms in _GETAR:
                if hz:
                    winsound.Beep(hz, ms)
                else:
                    time.sleep(ms / 1000.0)
            return
        except Exception:  # noqa: BLE001 - jatuh ke bel terminal di bawah
            log.debug("winsound gagal", exc_info=True)
    # Mesin lain (dan Windows tanpa pengeras suara internal): bel terminal.
    # Tak semirip dengung, tapi tetap terdengar dari jendela mana pun.
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def getar(latar: bool = True) -> None:
    """Tanda bahwa giliran sudah sampai di KESIMPULAN.

    `latar=False` menahan pemanggil sampai dengungnya habis — dipakai penanda
    "tugas selesai" (tanda.py) supaya getaran dan deringnya berurutan, bukan
    bertabrakan.

    Gunanya persis sama dengan fitur suara: memberitahu tanpa perlu melihat
    layar. Bedanya, ini menandai satu momen — bukan membacakan isi — jadi ia
    berguna justru saat pengguna sedang mengerjakan hal lain di jendela lain
    dan hanya perlu tahu "sudah selesai".

    Tak pernah menahan pemanggil dan tak pernah melempar."""
    try:
        if not latar:
            _getar_sekarang()
            return
        threading.Thread(target=_getar_sekarang, daemon=True,
                         name="bagasai-getar").start()
    except Exception:  # noqa: BLE001 - notifikasi tak boleh menjatuhkan giliran
        log.debug("gagal menggetarkan", exc_info=True)


# --- satu pengucap untuk seluruh program ------------------------------------
_PENGUCAP: Pengucap | None = None
_LOCK = threading.Lock()


def pengucap() -> Pengucap:
    global _PENGUCAP
    with _LOCK:
        if _PENGUCAP is None:
            # Sesi yang berakhir mendadak tak sempat membereskan berkas
            # suaranya. Disapu saat pertama dipakai, bukan saat impor: impor
            # terjadi di tiap perintah kecil (mis. --version) dan tak pantas
            # menyentuh disk.
            sapu_berkas_suara()
            _PENGUCAP = Pengucap()
        return _PENGUCAP


def ucap(teks: str, penuh: bool = False) -> None:
    """Bacakan satu kabar (tak menahan pemanggil, tak pernah melempar)."""
    try:
        pengucap().ucap(teks, penuh)
    except Exception:  # noqa: BLE001 - notifikasi tak boleh menjatuhkan giliran
        log.debug("gagal mengucap", exc_info=True)


def ucap_panjang(teks: str) -> None:
    """Bacakan JAWABAN AKHIR per potongan kalimat (tak menahan, tak melempar)."""
    try:
        pengucap().ucap_panjang(teks)
    except Exception:  # noqa: BLE001 - notifikasi tak boleh menjatuhkan giliran
        log.debug("gagal mengucap panjang", exc_info=True)


def diam() -> None:
    """Hentikan & buang semua yang belum diucapkan."""
    try:
        if _PENGUCAP is not None:
            _PENGUCAP.diam()
    except Exception:  # noqa: BLE001
        pass


def tutup() -> None:
    try:
        if _PENGUCAP is not None:
            _PENGUCAP.tutup()
    except Exception:  # noqa: BLE001
        pass


# Batas kewajaran satu bacaan. Dipakai sibuk(): penanda "mulai berbunyi" bisa
# tertinggal menyala kalau mesin suaranya mati di tengah jalan, dan tanpa batas
# ini penanda "tugas selesai" akan menunggu keadaan yang tak pernah berubah.
_MAKS_BACA = 300.0


def sibuk() -> bool:
    """True bila masih ada kabar yang mengantre atau sedang dibacakan."""
    p = _PENGUCAP
    if p is None:
        return False
    try:
        if not p._antre.empty():
            return True
        mulai = p._mulai_bunyi
        if mulai is None:
            return False
        # Sudah kelewat lama = penandanya basi, bukan bacaan yang masih jalan.
        return (time.monotonic() - mulai) < _MAKS_BACA
    except Exception:  # noqa: BLE001
        return False


def tunggu_diam(batas: float = 300.0) -> None:
    """Tahan sampai bacaannya selesai (atau `batas` detik terlampaui).

    Dipakai penanda "tugas selesai": bunyinya harus jatuh SESUDAH jawaban akhir
    selesai dibacakan, bukan menimpanya. Berbatas waktu dengan sengaja — mesin
    suara yang menggantung tak boleh ikut menahan penandanya selamanya."""
    habis = time.monotonic() + max(0.0, batas)
    # Jeda kecil dulu: ucapan yang baru saja diantrekan butuh sesaat sebelum
    # terhitung "sibuk", dan tanpa ini penantiannya selesai sebelum dimulai.
    time.sleep(0.35)
    while sibuk() and time.monotonic() < habis:
        time.sleep(0.15)


def catatan() -> str:
    """Alasan terakhir suara tak berbunyi atau berganti mesin ("" bila tak ada).

    Ada supaya perubahan suara SELALU bisa dijelaskan. Suara yang berganti
    sendiri tanpa keterangan mustahil dibedakan dari fitur yang rusak — dan
    itu keluhan nyata, bukan dugaan."""
    return getattr(_PENGUCAP, "galat", "") or ""


# Catatan yang SUDAH pernah ditampilkan ke pengguna — supaya alasan yang sama
# tak diulang-ulang tiap giliran.
_CATATAN_TAMPIL: set[str] = set()


def catatan_baru() -> str:
    """Catatan yang BELUM pernah ditampilkan ("" bila tak ada yang baru).

    Dipakai terminal untuk mengumumkan sendiri saat suaranya bermasalah atau
    berpindah mesin. Tanpa ini, alasannya cuma terbaca kalau pengguna kebetulan
    mengetik /mic — padahal yang ia rasakan justru saat itu juga: suaranya
    berubah, atau tiba-tiba tak ada bunyi sama sekali."""
    pesan = catatan()
    if not pesan or pesan in _CATATAN_TAMPIL:
        return ""
    _CATATAN_TAMPIL.add(pesan)
    return pesan


def suara_tersedia() -> list[str]:
    """Nama suara SAPI yang terpasang di Windows (untuk /mic)."""
    if sys.platform != "win32":
        return []
    ps = ("Add-Type -AssemblyName System.Speech; "
          "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
          ".GetInstalledVoices() | ForEach-Object "
          "{ $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        return []
    return [b.strip() for b in (out.stdout or "").splitlines() if b.strip()]
