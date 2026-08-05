"""Kabar dari AI dibacakan lewat pengeras suara laptop.

Selama ini setiap kabar antar-langkah ("sekarang kuperbarui berkas uji tim…")
hanya muncul sebagai teks. Kalau jendela terminalnya tertutup jendela lain —
dan saat menunggu AI bekerja itu yang paling sering terjadi — kabar itu lewat
begitu saja. Suara tak punya masalah itu: ia terdengar di jendela mana pun.

MESIN BERLAPIS
--------------
Tak ada satu mesin suara pun yang ada di semua laptop, jadi dipakai yang
terbaik di antara yang BENAR-BENAR tersedia, diperiksa saat dipakai:

  1. edge  — suara Indonesia natural (id-ID-ArdiNeural/GadisNeural). Paling
             enak didengar, tapi butuh paket `edge-tts` DAN internet.
  2. sapi  — System.Speech bawaan Windows. Selalu ada di Windows mana pun,
             jalan tanpa internet. Suara Indonesia dipakai kalau terpasang;
             kalau tidak, teksnya tetap dibacakan dengan pelafalan Inggris.
  3. say   — macOS.
  4. espeak— Linux (spd-say / espeak-ng).

Turun lapis terjadi SENDIRI dan permanen untuk sesi itu: sekali edge gagal
(mis. laptop sedang luring), sisanya langsung memakai sapi tanpa mencoba lagi —
mencoba ulang tiap kabar berarti menambah beberapa detik hening pada tiap
langkah.

TIGA PEMBATAS
-------------
  1. SATU proses pembantu, bukan satu proses per kalimat. Menyalakan PowerShell
     TERUKUR ~0,5 detik; membayarnya tiap kabar membuat suara selalu tertinggal.
  2. Kabar BASI dibuang. Begitu giliran dibatalkan atau giliran baru dimulai,
     antreannya dikosongkan dan yang sedang diucapkan dihentikan — mendengar
     rencana langkah yang sudah lewat lebih membingungkan daripada diam.
  3. Yang diucapkan dipendekkan & dibersihkan: blok kode, penanda protokol, dan
     tautan tak ada gunanya didengar.

Kalau apa pun di jalur ini gagal, fitur ini DIAM dan tak pernah melempar ke
pemanggil. Notifikasi yang menjatuhkan giliran jauh lebih buruk daripada
notifikasi yang tak berbunyi.
"""
from __future__ import annotations

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
# Antrean lebih panjang dari ini berarti suaranya tertinggal terlalu jauh untuk
# masih berguna; yang paling lama menunggu dibuang lebih dulu.
_MAKS_ANTRE = 3
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
# Menganggur selama ini -> proses pembantu dilepas (hemat memori di laptop kecil).
# Jangan terlalu pendek: menyalakannya lagi memakan waktu, dan di laptop pelan
# waktu itu jatuh tepat di kabar PERTAMA giliran berikutnya.
_JEDA_NGANGGUR = 90.0

# Proses pembantu Windows: SAPI + pemutar berkas suara dalam SATU proses.
# Protokolnya satu baris per perintah, "S"/"P" lalu muatan Base64 (Base64 supaya
# teks & path apa pun aman lewat stdin — kutip, aksen, spasi tak perlu di-escape).
_SKRIP = r"""
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Speech
try { Add-Type -AssemblyName presentationCore } catch { }
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
# Suara berbahasa Indonesia dipakai bila ADA; kalau tidak, suara apa pun yang
# terpasang. Melempar saat id-ID tak ada akan mematikan fitur ini di Windows
# yang cuma punya suara Inggris — dan itu bawaan kebanyakan mesin.
$pilih = $s.GetInstalledVoices() | Where-Object { $_.Enabled } |
         Where-Object { $_.VoiceInfo.Culture.Name -like 'id*' } |
         Select-Object -First 1
if ($pilih) { $s.SelectVoice($pilih.VoiceInfo.Name) }
$s.Rate = RATE
$s.Volume = 100
[Console]::Out.WriteLine('SIAP')
[Console]::Out.Flush()
while ($null -ne ($baris = [Console]::In.ReadLine())) {
  if ($baris.Length -lt 2) { continue }
  $jenis = $baris.Substring(0,1)
  try {
    $isi = [System.Text.Encoding]::UTF8.GetString(
             [System.Convert]::FromBase64String($baris.Substring(1)))
    if ($jenis -eq 'S') {
      $s.Speak($isi)
    } elseif ($jenis -eq 'P') {
      $mp = New-Object System.Windows.Media.MediaPlayer
      $mp.Open([uri]$isi)
      $n = 0
      while (-not $mp.NaturalDuration.HasTimeSpan -and $n -lt 160) {
        Start-Sleep -Milliseconds 25; $n++
      }
      if ($mp.NaturalDuration.HasTimeSpan) {
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
_JALUR = re.compile(r"\b[\w.\-]+[/\\][\w./\\-]+")
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


def _edge_ada() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def mesin_tersedia() -> list[str]:
    """Mesin suara yang bisa dipakai di laptop ini, terbaik lebih dulu."""
    out: list[str] = []
    if _edge_ada():
        out.append("edge")
    if sys.platform == "win32":
        out.append("sapi")
    elif sys.platform == "darwin":
        if shutil.which("say"):
            out.append("say")
    else:
        if shutil.which("spd-say") or shutil.which("espeak-ng") \
                or shutil.which("espeak"):
            out.append("espeak")
    return out


class Pengucap:
    """Antrean ucapan + mesin yang membacakannya."""

    def __init__(self, rate: int = _RATE) -> None:
        self.rate = max(-10, min(10, int(rate)))
        self._antre: queue.Queue[str] = queue.Queue()
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
    def ucap(self, teks: str, penuh: bool = False) -> None:
        """Antrekan satu kabar. Tak pernah melempar, tak pernah menahan.

        `penuh=True` untuk JAWABAN AKHIR: dibacakan jauh lebih panjang, sebab
        tak ada langkah berikutnya yang perlu dikejar."""
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
        self._dahului()
        self._antre.put(bersih)
        self._pastikan_jalan()

    def _buang_antre(self) -> None:
        """Kosongkan antrean. Antreannya memang cuma menyimpan yang TERBARU."""
        while True:
            try:
                self._antre.get_nowait()
            except queue.Empty:
                return

    def _hentikan(self) -> None:
        """Matikan ucapan yang sedang berbunyi SEKARANG juga.

        Nomor gilirannya dinaikkan supaya thread pengucap tahu kegagalan yang
        sebentar lagi ia lihat adalah pemotongan yang DISENGAJA, bukan mesin
        suara yang rusak."""
        with self._kunci:
            self._gen += 1
            self._mulai_bunyi = None
            p, self._proc = self._proc, None
        if p is not None:
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
        tertinggal sedikit masih jauh lebih berguna daripada senyap."""
        self._buang_antre()
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
                teks = self._antre.get(timeout=_JEDA_NGANGGUR)
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
                with self._kunci:
                    if not self._antre.empty():
                        continue
                    self._thread = None
                self._potong()
                return
            with self._kunci:
                gen = self._gen
            try:
                self._ucapkan(teks, urutan, gen)
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
        if (self._lambat_beruntun >= _BATAS_LAMBAT
                and len(urutan) > 1 and urutan[0] == "edge"):
            self._lambat_beruntun = 0
            urutan.pop(0)
            self.galat = (f"suara natural terlalu lambat disiapkan di laptop "
                          f"ini (≥{_EDGE_LAMBAT:.0f} detik/kabar) — memakai "
                          f"{urutan[0]}")
        while urutan:
            mesin = urutan[0]
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
                    subprocess.run(["say", teks], timeout=90)
                    return True
                elif mesin == "espeak":
                    alat = (shutil.which("spd-say") or shutil.which("espeak-ng")
                            or shutil.which("espeak"))
                    if alat:
                        bendera = ["-l", "id"] if alat.endswith("spd-say") \
                            else ["-v", "id"]
                        subprocess.run([alat, *bendera, teks], timeout=90)
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
            # Sudah gagal berkali-kali: ini bukan gangguan sesaat. Turun lapis
            # PERMANEN untuk sesi ini — mencoba mesin daring lagi pada tiap
            # kabar berarti menambah beberapa detik hening ke setiap langkah,
            # dan penyebabnya (luring) hampir tak pernah berubah di tengah sesi.
            self._gagal_beruntun = 0
            urutan.pop(0)
            if urutan:
                self.galat = f"{mesin} gagal {_BATAS_GAGAL}x, memakai {urutan[0]}"
        self.galat = "semua mesin suara gagal"
        return False

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
        berkas = os.path.join(
            tempfile.gettempdir(),
            f"bagasai-suara-{os.getpid()}-{self._urut}.mp3")

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
        # Perintahnya sudah sampai: SEJAK DETIK INI bunyinya keluar dari
        # pengeras suara. Penandanya sengaja dipasang di sini, bukan sebelum
        # proses pembantunya dinyalakan — waktu menyalakan PowerShell (yang di
        # laptop pelan bisa beberapa detik) bukan waktu yang terdengar, dan
        # menghitungnya sebagai "sudah terdengar" akan membuat kabar dipotong
        # tepat sebelum bunyinya keluar.
        with self._kunci:
            self._mulai_bunyi = time.monotonic()
        try:
            return p.stdout.readline().strip() == "OK"
        except Exception:  # noqa: BLE001 - proses dibunuh oleh diam()
            with self._kunci:
                if self._proc is p:
                    self._proc = None
            return False
        finally:
            with self._kunci:
                if self._proc is p or self._proc is None:
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


def getar() -> None:
    """Tanda bahwa giliran sudah sampai di KESIMPULAN.

    Gunanya persis sama dengan fitur suara: memberitahu tanpa perlu melihat
    layar. Bedanya, ini menandai satu momen — bukan membacakan isi — jadi ia
    berguna justru saat pengguna sedang mengerjakan hal lain di jendela lain
    dan hanya perlu tahu "sudah selesai".

    Tak pernah menahan pemanggil dan tak pernah melempar."""
    try:
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
            _PENGUCAP = Pengucap()
        return _PENGUCAP


def ucap(teks: str, penuh: bool = False) -> None:
    """Bacakan satu kabar (tak menahan pemanggil, tak pernah melempar)."""
    try:
        pengucap().ucap(teks, penuh)
    except Exception:  # noqa: BLE001 - notifikasi tak boleh menjatuhkan giliran
        log.debug("gagal mengucap", exc_info=True)


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


def catatan() -> str:
    """Alasan terakhir suara tak berbunyi atau berganti mesin ("" bila tak ada).

    Ada supaya perubahan suara SELALU bisa dijelaskan. Suara yang berganti
    sendiri tanpa keterangan mustahil dibedakan dari fitur yang rusak — dan
    itu keluhan nyata, bukan dugaan."""
    return getattr(_PENGUCAP, "galat", "") or ""


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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        return []
    return [b.strip() for b in (out.stdout or "").splitlines() if b.strip()]
