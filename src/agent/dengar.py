"""Pipeline mikrofon lokal untuk sesi /voice dan kompatibilitas wake word.

CARA PAKAI (dari sisi pengguna):

    /voice on          membuka layar orb dan mikrofon kontinu
    (langsung bicara)  tidak perlu menyebut "bagas ai"
    (diam sejenak)     satu ucapan DITUTUP & dikirim ke chat utama
    /voice off         mikrofon mati (ini keadaan bawaannya)

TAK ADA kata pemicu maupun kata penutup di sesi langsung. Berhenti bicara
sebentar sudah cukup; kalau kalimat diteruskan sebelum jeda habis, hitungannya
diulang dari nol. Teks pengguna dan jawaban AI tetap masuk ke terminal utama;
layar khusus hanya menampilkan animasi yang bereaksi terhadap audio.

``Pendengar(langsung=False)`` masih mempertahankan Perakit berbasis wake word
untuk klien lama. UI Textual dan CLI memakai ``langsung=True``.

Karena sesi langsung mendengar semua ucapan di ruangan, pengguna keluar dengan
Esc atau tombol ⌵ ketika percakapan voice selesai. Audio keluaran BagasAI
dibuang dari jalur mikrofon agar jawaban AI tidak memicu dirinya sendiri.

PENGENALAN SUARANYA TIDAK SEMPURNA, DAN ITU DIPERHITUNGKAN
----------------------------------------------------------
Diuji dengan suara sintetis Indonesia: "bagas ai tolong buka berkas main titik
py lalu perbaiki galatnya lakukan" terbaca "Bagas Ai Tolong buka berkas main
titik p lalu perbaiki galaknya lakukan". Dua pelajarannya terpasang di sini:

  1. namanya kembali sebagai DUA kata ("Bagas Ai"), kadang salah dengar
     ("bagas hai", "pagas ai"). Karena itu pencocokannya per-kata dengan
     beberapa varian, bukan pencarian teks mentah;
  2. sisa kalimatnya bisa salah huruf. Itu TIDAK diperbaiki di sini — perintah
     yang salah dengar tetap ditampilkan apa adanya sebelum dikerjakan, jadi
     pengguna melihat apa yang akan dijalankan.

KESUNYIAN ITU BUG, BUKAN KEADAAN NORMAL
---------------------------------------
Dilaporkan pengguna: "sudah bilang … lakukan, tapi AI-nya tak berbuat apa-apa."
Yang terjadi terlihat di layarnya sendiri — tiap barisnya bertanda "·" (belum
merekam), artinya namanya tak pernah sampai ke pengenal suara. Bagas-ai tahu
itu dan DIAM SAJA; pengguna tak punya cara menebaknya.

Tiga hal yang lahir dari situ, semuanya soal membuat yang tak terlihat jadi
terlihat:

  - ucapan yang tertangkap tapi tak terpahami tetap DILAPORKAN. Di situlah kata
    pemicu paling sering hilang: ia pendek, diucapkan sendirian, lalu tak
    terbaca — dan dulu itu berakhir di `continue` tanpa sepatah kata;
  - kata penutup yang terdengar SEBELUM namanya disebut dijawab dengan
    petunjuk, bukan didiamkan;
  - jeda pemisah ucapan dilonggarkan (0,9 -> 1,3 detik), sebab orang menyebut
    nama lalu berhenti sebentar menunggu tanda diterima — dan jeda itulah yang
    memotong "bagas ai" jadi potongan tersendiri yang gagal dikenali.

Semua kegagalan di modul ini berakhir jadi kabar, bukan lemparan: mikrofon yang
bermasalah tak boleh menjatuhkan sesi yang sedang bekerja.
"""
from __future__ import annotations

import difflib
import logging
import queue
import re
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, NamedTuple

log = logging.getLogger(__name__)

# --- kata pemicu & penutup -------------------------------------------------
# Ditulis sebagai POLA PER-KATA, bukan potongan teks: pengenal suara memisah
# "bagasai" jadi dua kata dan sesekali salah dengar konsonan awalnya.
# KATA PEMICU: NAMANYA SENDIRI. "on" sempat dipakai lalu dikembalikan ke sini
# atas permintaan pengguna — dan memang itu pilihan yang lebih sehat: aba-aba
# dua huruf gampang salah dengar DAN gampang terpicu tanpa sengaja, sementara
# "bagas ai" khas dan hampir mustahil muncul kebetulan.
_NAMA_KEDUA = {"ai", "hai", "ay", "ayi", "a", "i", "eye", "ei"}
_NAMA_RAPAT = {"bagasai", "bagas-ai", "bagasi", "pagasai", "bagasay",
               "bagasih", "bagaskara", "bagasi"}
_NAMA_PERTAMA = {"bagas", "pagas", "bagus", "begas", "bagaz"}
# Yang boleh jadi panggilan TUNGGAL di awal kalimat — lihat cari_nama().
_NAMA_SENDIRI = {"bagas", "bagaz"}
# TAK ADA LAGI KATA PENUTUP. Perintah ditutup oleh BERHENTINYA suara — lihat
# JEDA_SELESAI. Perjalanannya: "lakukan" -> "enter" -> kembali "lakukan" ->
# akhirnya tak ada sama sekali, dan yang terakhir ini yang paling masuk akal.
# Dua sebabnya nyata: mengucapkan aba-aba di ujung kalimat tak alami (yang lupa
# kehilangan seluruh perintahnya tanpa tahu sebabnya), dan kata apa pun yang
# dipilih pasti muncul juga DI DALAM perintah — "jalankan ujinya lakukan"
# terpotong di tempat yang salah.
# Kata BATAL: membuang rekaman yang sedang berjalan. "off" sempat dipakai lalu
# dikembalikan ke "batalkan" atas permintaan pengguna — dan itu memang lebih
# aman: "off"/"of" gampang muncul dari salah dengar, sedangkan "batalkan" jelas
# maksudnya. Hanya berlaku SELAGI merekam; di luar itu ia kata biasa dalam
# obrolan, dan menanggapinya cuma menambah kebisingan.
PEMBATAL = {"batalkan", "batal", "batalin", "gajadi", "cancel", "batalkanlah"}

# Penanda hasil dengar(): perintahnya DIBATALKAN pengguna. Sengaja objek
# tersendiri, bukan string kosong atau None — keduanya sudah punya arti lain
# ("penutup tanpa isi" dan "belum apa-apa"), dan menumpuk arti ketiga di
# atasnya membuat pemanggil tak bisa membedakan mana yang mana.
BATAL = object()

# BATAS MEREKAM: 30 detik sejak namanya disebut. Lewat itu, perintahnya
# DIBATALKAN — bukan dikirim setengah jadi.
#
# Ada dua alasan, dan keduanya nyata. Pertama, kata pemicu bisa terpicu tanpa
# sengaja (nama bagas-ai kebetulan disebut dalam obrolan); tanpa batas ini
# seluruh percakapan ruangan sesudahnya ikut terkumpul lalu berangkat sebagai
# satu perintah raksasa. Kedua, orang yang lupa mengucapkan "lakukan" tak punya
# cara lain menyadarinya — mikrofon tak memberi tanda apa-apa.
#
# Dihitung dari namanya disebut, BUKAN dari ucapan terakhir: yang dibatasi
# panjang satu perintah, dan itu memang yang dimaksud "maksimal merekam".
MAKS_REKAM = 30.0

# Berapa lama SUNYI menandai perintah sudah selesai. Diukur dari suaranya, bukan
# dari teks hasil pengenalan: inferensi lokal tetap datang beberapa detik
# sesudah audio, dan menghitung diam dari situ akan memotong orang yang baru
# saja berhenti menarik napas.
#
# Kalau pembicara meneruskan kalimat sebelum 2 detik habis, hitungannya
# DIULANG dari nol — jadi jeda berpikir sependek apa pun tak pernah memotong.
JEDA_SELESAI = 2.0


# --- sapaan saat namaku terdengar -------------------------------------------
# Pola nama pengguna di memory jangka panjang. Ditulis longgar karena isinya
# kalimat bebas buatan model, bukan borang: "Nama pengguna: Bagas", "panggil
# saya Bagas", "namanya Bagas".
_ISI = r"([^\n.,;]{2,30})"
_POLA_NAMA = (
    re.compile(r"nama(?:\s+panggilan)?\s+(?:pengguna|user)\s*[:=-]\s*" + _ISI,
               re.I),
    re.compile(r"(?:panggil|sapa)\s+(?:saya|aku|gw|gue)\s+" + _ISI, re.I),
    re.compile(r"nama\s+(?:saya|aku|gw|gue)\s+(?:adalah\s+)?" + _ISI, re.I),
    re.compile(r"penggunanya?\s+bernama\s+" + _ISI, re.I),
)


def nama_pengguna() -> str:
    """Nama pengguna dari memory jangka panjang ("" bila memang tak ada).

    Sengaja TAK menebak dari sumber lain (nama folder Windows, akun) — nama
    yang salah tebak lalu diucapkan keras-keras jauh lebih canggung daripada
    sapaan tanpa nama."""
    try:
        from . import longmem
        fakta = longmem.all_facts()
    except Exception:  # noqa: BLE001
        return ""
    for f in fakta:
        for pola in _POLA_NAMA:
            m = pola.search(str(f))
            if m:
                nama = m.group(1).strip(" '\"")
                if nama and len(nama.split()) <= 3:
                    return nama
    return ""


def sapaan() -> str:
    """Kalimat yang diucapkan saat bagas-ai siap menerima perintah."""
    nama = nama_pengguna()
    return (f"Hai {nama}, saya siap menerima perintah." if nama
            else "Hai, saya siap menerima perintah.")


# Diucapkan saat perintahnya DITUTUP oleh diam, sebelum dikirim. Gunanya
# menjawab pertanyaan yang muncul persis di detik itu: "tadi kedengeran nggak,
# atau aku harus mengulang?" — dan tanpa jawaban, orang cenderung mengulangi
# perintah yang sebenarnya sudah berangkat.
DITERIMA = "Baik, saya telah menerima perintah."


def _kata(teks: str) -> list[str]:
    """Pecah jadi kata-kata polos: huruf kecil, tanda baca dibuang."""
    return re.sub(r"[^0-9a-zA-ZÀ-ɏ]+", " ", teks or "").lower().split()


# Seberapa mirip sebuah ucapan harus terdengar dengan "bagasai" untuk dianggap
# panggilan. 0,75 DIUKUR, bukan ditebak: pada 20 bentuk salah-dengar yang masuk
# akal (bagasi, pagasi, bakasi, bagas hai, bagus ai, …) dan 19 kata Indonesia
# yang lumrah membuka kalimat (tolong, bagus, bagian, tugas, biasa, …), 0,75
# adalah titik tertinggi yang masih menangkap semuanya dengan NOL salah
# tangkap. Di 0,65 kata "bagus" dan "bagi" ikut memicu; di 0,80 "bakasi" dan
# "bagaskara" lolos.
_MIRIP = 0.75


def _mirip_nama(gabung: str) -> bool:
    """True bila rangkaian huruf ini terdengar seperti "bagasai"."""
    return difflib.SequenceMatcher(None, gabung, "bagasai").ratio() >= _MIRIP


def cari_pemicu(kata: list[str]) -> int:
    """Indeks kata TEPAT SESUDAH namanya disebut, atau -1 bila tak disebut.

    Dilaporkan pengguna: "udah nyebut bagasai tapi susah amat kedeteksi."
    Sebabnya daftar kata tetap — sebanyak apa pun ditambah, pengenal suara
    selalu punya ejaan yang belum terdaftar. Karena itu daftar tegas di bawah
    hanya jalur cepat; keputusan sebenarnya di KEMIRIPAN BUNYI, yang menangkap
    bentuk yang belum pernah terlihat sekalipun."""
    for i, k in enumerate(kata):
        if k in _NAMA_RAPAT:
            return i + 1
        if k in _NAMA_PERTAMA and i + 1 < len(kata) and kata[i + 1] in _NAMA_KEDUA:
            return i + 2
    # "Bagas" SENDIRIAN diterima hanya bila ia kata PERTAMA. Orang yang
    # memanggil memang sering menjatuhkan "ai"-nya ("bagas, tolong buka…"),
    # sementara "bagas" di tengah kalimat jauh lebih mungkin bagian obrolan.
    #
    # Daftarnya SENGAJA lebih sempit daripada _NAMA_PERTAMA: salah-dengar yang
    # ditampung di sana ("bagus", "pagas") adalah kata-kata yang lumrah
    # diucapkan sendiri — "bagus sekali" akan memicu perintah tanpa seorang pun
    # bermaksud memanggil. Sebagai pasangan "ai" mereka aman, sebagai panggilan
    # tunggal tidak.
    if kata and kata[0] in _NAMA_SENDIRI:
        return 1

    # --- kemiripan bunyi (jaring utama) ---
    # Kata kedua hanya boleh disambung bila PENDEK. Namanya "bagas" + satu suku
    # kata ("ai"/"hai"/"ay"), jadi menyambung kata utuh cuma mengundang salah
    # tangkap: "bagas yang" (obrolan tentang orang bernama Bagas) jatuh persis
    # di ambang kemiripan dan sempat memicu perintah. TERUKUR, bukan dugaan.
    def _sambung(i: int) -> str:
        return kata[i] + kata[i + 1] if len(kata[i + 1]) <= 3 else ""

    if len(kata) >= 2 and _mirip_nama(_sambung(0)):
        return 2
    if kata and _mirip_nama(kata[0]):
        return 1
    # Lalu pasangan kata BERURUTAN di mana pun: orang sering mengawali dengan
    # kata lain ("oke bagas ai, tolong…"), dan pengenal suara ikut menuliskannya.
    for i in range(len(kata) - 1):
        if _mirip_nama(_sambung(i)):
            return i + 2
    return -1


def cari_pembatal(kata: list[str]) -> int:
    """Indeks kata pembatal pertama, atau -1."""
    for i, k in enumerate(kata):
        if k in PEMBATAL:
            return i
    return -1


class Perakit:
    """Menyusun satu perintah dari potongan-potongan ucapan.

    Tiga keadaan saja: diam -> merekam (sesudah namanya disebut) -> selesai
    (sesudah kata penutup). Dipisah dari mikrofonnya supaya bisa diuji tanpa
    suara sama sekali — dan memang di sinilah seluruh aturannya berada."""

    def __init__(self, maks_rekam: float = MAKS_REKAM) -> None:
        self.maks_rekam = float(maks_rekam)
        self.merekam = False
        self.potongan: list[str] = []
        self.disebut_pada = 0.0
        self.terakhir = 0.0

    @property
    def sisa_detik(self) -> float:
        """Sisa jatah merekam perintah ini (0 bila sedang tak merekam)."""
        if not self.merekam:
            return 0.0
        return max(0.0, self.maks_rekam - (time.time() - self.disebut_pada))

    @property
    def sementara(self) -> str:
        """Perintah yang sudah terkumpul sejauh ini (untuk ditampilkan)."""
        return " ".join(self.potongan).strip()

    def batalkan(self) -> None:
        self.merekam = False
        self.potongan = []

    def selesai(self) -> str:
        """Tutup perintah yang sedang direkam & kembalikan isinya.

        Dipanggil perekam saat pembicara BERHENTI cukup lama (lihat
        JEDA_SELESAI). Menggantikan kata penutup: orang tak alami mengucapkan
        aba-aba di akhir kalimat, dan yang lupa mengucapkannya kehilangan
        seluruh perintahnya tanpa tahu sebabnya."""
        isi = self.sementara
        self.batalkan()
        return isi

    def dengar(self, teks: str, sekarang: float | None = None) -> Any:
        """Umpankan satu hasil pengenalan suara.

        Return:
          - perintah lengkap (str) bila kata penutup terdengar;
          - "" bila penutupnya terdengar tapi tak ada isinya;
          - BATAL bila pengguna mengucapkan kata pembatal;
          - None bila belum apa-apa.
        """
        sekarang = time.time() if sekarang is None else sekarang
        kata = _kata(teks)
        if not kata:
            return None

        if not self.merekam:
            i = cari_pemicu(kata)
            if i < 0:
                return None            # bukan untuk kita — abaikan diam-diam
            self.merekam = True
            self.potongan = []
            self.disebut_pada = sekarang
            kata = kata[i:]

        self.terakhir = sekarang
        # PEMBATAL masih berupa kata: membatalkan harus bisa dilakukan SEKARANG,
        # tanpa menunggu diam. Kata PENUTUP tak lagi ada — perintah ditutup oleh
        # berhentinya suara (lihat Pendengar & JEDA_SELESAI), sebab mengucapkan
        # aba-aba di akhir kalimat tak alami dan yang lupa kehilangan seluruh
        # perintahnya tanpa tahu sebabnya.
        b = cari_pembatal(kata)
        if b >= 0:
            self.batalkan()
            return BATAL
        if kata:
            self.potongan.append(" ".join(kata))
        return None

    def kedaluwarsa(self, sekarang: float | None = None) -> bool:
        """True (dan membatalkan) bila perintah sudah melewati batas merekam.

        Diukur dari NAMANYA DISEBUT, bukan dari ucapan terakhir: yang dibatasi
        panjang satu perintah. Kalau diukur dari ucapan terakhir, orang yang
        terus bicara bisa merekam tanpa akhir — persis yang tak diinginkan saat
        pemicunya kebetulan terpicu di tengah obrolan."""
        if not self.merekam:
            return False
        sekarang = time.time() if sekarang is None else sekarang
        if sekarang - self.disebut_pada < self.maks_rekam:
            return False
        self.batalkan()
        return True


# --- mikrofon --------------------------------------------------------------
LAJU = 16000            # 16 kHz mono: yang diminta hampir semua pengenal suara
BLOK = 1024             # ±64 ms per blok
_DETIK_BLOK = BLOK / LAJU
_DIAM_SELESAI = 1.3     # sunyi selama ini -> satu ucapan dianggap selesai.
                        # DINAIKKAN dari 0,9: orang menyebut nama lalu
                        # BERHENTI SEBENTAR menunggu tanda diterima, dan
                        # jeda itu memotong "bagas ai" jadi ucapan
                        # tersendiri — potongan sependek itu paling sering
                        # gagal dikenali, jadi namanya hilang tanpa jejak
                        # dan sisa kalimatnya dikira obrolan ruangan.
_MIN_UCAPAN = 0.35      # lebih pendek dari ini: dentuman meja, bukan ucapan
_MAKS_UCAPAN = 15.0     # satu POTONGAN kirim ke pengenal (bukan batas
                        # perintah — lihat MAKS_REKAM). Dipotong berkala
                        # supaya yang terdengar muncul di layar sambil
                        # pengguna masih bicara, bukan menunggu 30 detik.
_KALIBRASI = 0.7        # lama mengukur derau ruangan sebelum mulai


# --- SEBERAPA JAUH MIKROFON BOLEH MENDENGAR --------------------------------
# Permintaan pengguna: "mic-nya bisa dijangkau walau dari jarak agak jauh,
# misal sambil rebahan atau dari ruang tengah ke depan pintu kamar."
#
# Yang menghalanginya bukan volume, melainkan AMBANG. Rumus lamanya satu baris,
# `max(derau * 6, 25)`, dan dua pengukuran di laptop yang sama menunjukkan
# kenapa satu angka tak pernah cukup:
#
#   dulu   derau  0,5  -> ambang  25   (lantainya yang menentukan)
#   kini   derau 57    -> ambang 343   (pengalinya yang menentukan)
#
# Suara dari seberang ruangan tiba sekitar 60-150 di mikrofon ini. Di angka 343
# ia tak pernah lewat sekali pun — dan itu persis keluhannya. Menaikkan pengali
# akan mematikan mikrofon ber-derau tinggi; menurunkannya akan membuat mikrofon
# ber-derau rendah mendengar desisnya sendiri. Jadi yang dipakai BUKAN satu
# angka, melainkan tiga hal sekaligus:
#
#   1. PROFIL JANGKAUAN. Jarak bicara itu pilihan pengguna, bukan sesuatu yang
#      bisa ditebak dari derau. Karena itu ia disetel, bukan dihitung.
#   2. DERAU RAMAI (persentil 90), bukan cuma derau tengah (persentil 50). Di
#      ruangan berdengung keduanya sama; di ruangan dengan kipas/ketikan yang
#      menyentak, p90 jauh di atas p50 — dan sentakan itulah yang salah dikira
#      ucapan kalau ambangnya cuma mengikuti p50.
#   3. HISTERESIS. Satu ambang untuk MEMULAI ucapan, ambang lebih rendah untuk
#      MENERUSKANNYA. Dari jauh, tengah kata kerap melorot di bawah ambang;
#      dengan satu ambang saja kalimatnya tercacah jadi potongan sependek suku
#      kata, dan potongan sependek itu paling sering gagal dikenali.
class Jangkauan(NamedTuple):
    kali_derau: float   # ambang MULAI = derau tengah x ini ...
    kali_ramai: float   # ... atau derau ramai (p90) x ini — ambil yang terbesar
    lanjut: float       # ambang LANJUT = ambang mulai x ini (histeresis)
    lantai: float       # ambang terendah mutlak
    awalan: int         # blok yang disimpan SEBELUM ambang terlampaui


JANGKAUAN: dict[str, Jangkauan] = {
    # Mikrofon nempel di depan muka. Bar tinggi: obrolan orang lain di ruangan
    # yang sama tak ikut terkirim.
    "dekat":  Jangkauan(8.0, 2.4, 0.60, 30.0, 5),
    # Duduk di depan laptop, tangan di keyboard.
    "normal": Jangkauan(6.0, 1.8, 0.55, 25.0, 6),
    # Rebahan, atau dari ruangan sebelah. Pengalinya cukup +4,3 dB di atas
    # derau; dua-blok beruntun dan VAD (lihat _gelung/dengar_dikte) yang
    # menyaring sentakan sesaat. Nilai 2,6 sebelumnya terukur melewatkan suara
    # jauh dengan puncak 98 ketika derau tengah 49 (ambangnya menjadi 127).
    # Awalan 12 blok (±0,8 detik) menjaga suku kata pertama agar tidak putus.
    "jauh":   Jangkauan(1.65, 1.05, 0.50, 10.0, 12),
}
# BAWAANNYA "jauh", atas permintaan pengguna. Ongkosnya jujur dan ada: bar yang
# lebih rendah berarti lebih banyak potongan derau ikut dikirim ke pengenal
# suara. Yang TIDAK ia rusak adalah ketepatan — potongan yang tak memuat nama
# bagas-ai dibuang oleh Perakit, jadi yang bertambah cuma beban pengenalan,
# bukan salah perintah. Turunkan lewat `/voice dekat` atau VOICE_JANGKAUAN
# di .env bila mikrofonmu jadi terlalu sering "mendengar" kipas.
_BAWAAN = "jauh"


def _nama_jangkauan() -> str:
    """Jangkauan pilihan pengguna dari .env (VOICE_JANGKAUAN)."""
    try:
        from . import config
        nama = (getattr(config, "VOICE_JANGKAUAN", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        nama = ""
    return nama if nama in JANGKAUAN else _BAWAAN


def profil(nama: str | None = None) -> Jangkauan:
    """Profil jangkauan bernama; nama tak dikenal -> bawaan."""
    kunci = (str(nama).strip().lower() if nama else _nama_jangkauan())
    return JANGKAUAN.get(kunci, JANGKAUAN[_BAWAAN])


def _persentil(urut: list[float], p: float) -> float:
    """Persentil dari daftar yang SUDAH terurut ([] -> 0)."""
    if not urut:
        return 0.0
    return urut[min(len(urut) - 1, int(len(urut) * p))]


def hitung_ambang(contoh: list[float],
                  jang: Jangkauan) -> tuple[float, float, float, float]:
    """(derau, derau_ramai, ambang_mulai, ambang_lanjut) dari contoh derau.

    Dipisah dari mikrofonnya supaya bisa diuji dengan angka saja — dan supaya
    `/voice jangkau` menghitung ambang dengan rumus yang PERSIS SAMA dengan
    yang dipakai saat mendengarkan sungguhan. Dua rumus yang mirip-tapi-beda
    adalah cara terpelan untuk membuat alat ukur berbohong."""
    urut = sorted(contoh)
    derau = _persentil(urut, 0.5)
    ramai = _persentil(urut, 0.9)
    mulai = max(derau * jang.kali_derau, ramai * jang.kali_ramai, jang.lantai)
    # Ambang lanjut tak boleh tenggelam DI BAWAH derau ruangan: kalau itu
    # terjadi, ucapan tak pernah dianggap selesai dan perintahnya menggantung
    # sampai batas 30 detik.
    lanjut = max(mulai * jang.lanjut, derau * 1.15, jang.lantai * 0.5)
    return derau, ramai, mulai, lanjut


# UCAPAN TIDAK DIKERASKAN — dan itu keputusan yang DIUKUR, bukan kelalaian.
#
# Menaikkan volume sebelum mengirim ke pengenal terdengar masuk akal untuk
# mikrofon ber-gain rendah, dan sempat dipasang. Percobaan dengan kalimat yang
# sama pada beberapa tingkat volume menunjukkan sebaliknya:
#
#   puncak 384  -> tanpa penguatan BENAR, dengan penguatan BENAR
#   puncak 192  -> tanpa penguatan BENAR, dengan penguatan "tak terdengar jelas"
#
# Pengenalnya sudah menormalkan sendiri; yang ikut dibesarkan oleh penguatan
# justru derau kuantisasi, dan di suara paling pelan itulah yang mematikannya.
# Jadi yang membuat mikrofon terasa "tuli" bukan volumenya, melainkan AMBANG
# di atas — dan itu yang diperbaiki.


def siap() -> tuple[bool, str]:
    """(bisa dipakai?, alasan bila tidak) — diperiksa SEBELUM /voice on.

    Diperiksa satu per satu supaya pesannya menyebut yang mana yang kurang,
    bukan "fitur suara tidak tersedia" yang tak bisa ditindaklanjuti."""
    try:
        import sounddevice  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, ("paket `sounddevice` belum ada. Pasang dengan:\n"
                       "    pip install sounddevice")
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, ("pengenal lokal `faster-whisper` belum ada. Pasang "
                       "ulang bagas-ai agar fitur suara tersinkron.")
    try:
        import aec_audio_processing  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, ("pemroses mikrofon lokal belum ada. Pasang ulang "
                       "bagas-ai agar peredam bising tersinkron.")
    try:
        import sounddevice as sd
        masuk = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    except Exception as exc:  # noqa: BLE001
        return False, f"daftar perangkat audio tak terbaca: {exc}"
    if not masuk:
        return False, "tak ada mikrofon yang terdeteksi di laptop ini."
    return True, ""


def nama_mikrofon() -> str:
    """Nama mikrofon yang akan dipakai (kosong bila tak terbaca)."""
    try:
        import sounddevice as sd
        d = _pilih_mikrofon(sd)
        jalur = f" via {d.hostapi}" if d.hostapi else ""
        return f"{d.name}{jalur}".strip()
    except Exception:  # noqa: BLE001
        return ""


# --- mesin pengenal: Whisper lokal (wajib) ---------------------------------
# Audio tidak pernah diam-diam dikirim ke endpoint STT internet. Installer dan
# updater wajib menguji model ini di muka; kegagalan runtime dilaporkan apa
# adanya supaya privasi dan perilakunya tetap dapat diprediksi.
def _nama_model_whisper() -> str:
    try:
        from . import config
        nama = (getattr(config, "VOICE_STT_MODEL", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        nama = ""
    return nama or "small"


_HALUSINASI_SUNYI = (
    "terima kasih kerana menonton",
    "terima kasih karena menonton",
    "terima kasih telah menonton",
    "terima kasih sudah menonton",
    "sampai jumpa di video berikutnya",
    "jangan lupa like comment dan subscribe",
)


def _halusinasi_sunyi(teks: str) -> bool:
    """Kenali frasa stok Whisper pada audio kosong/sangat pelan.

    Pencocokan sengaja sempit: hanya frasa stok utuh (termasuk bila diulang),
    bukan kalimat pengguna yang kebetulan mengandung beberapa katanya.
    """
    normal = " ".join(_kata(teks))
    for frasa in _HALUSINASI_SUNYI:
        sisa = normal
        while sisa == frasa or sisa.startswith(frasa + " "):
            sisa = sisa[len(frasa):].strip()
            if not sisa:
                return True
    return False


class _Pengenal:
    """Satu mesin Whisper lokal yang dipakai bersama seluruh listener.

    Singleton tingkat modul (lihat pengenal()) — Pendengar diciptakan ULANG
    tiap `/voice on`, dan model Whisper yang termuat di dalamnya akan ikut
    terbuang bila menempel pada instance. Yuk 500 MB diunduh sekali per
    proses, dipakai lintas nyala-mati mikrofon."""

    def __init__(self) -> None:
        # Diisi thread pemuat; dibaca thread pengenal. Penempatan atribut
        # tunggal atomik menurut CPython, cukup aman tanpa kunci.
        self._whisper: Any = None
        self._kabar: Callable[..., None] = lambda _m, **_k: None
        # Pemuatan sinkron dan latar berbagi satu kunci. Tanpanya dua
        # `/voice on` yang berdekatan dapat memuat dua salinan model besar.
        self._muat_lock = threading.Lock()
        self._muat_error = ""

    def pastikan(self, kabar: Callable[..., None] | None = None) -> tuple[bool, str]:
        """Pastikan Whisper benar-benar dapat dimuat sebelum merekam.

        Installer dan updater memanggil jalur yang sama, sehingga yang diuji
        bukan hanya metadata paket, tetapi bobot model serta CTranslate2-nya.
        """
        if kabar is not None:
            self._kabar = kabar
        if self._whisper is not None:
            return True, f"Whisper {_nama_model_whisper()} siap"
        with self._muat_lock:
            if self._whisper is not None:
                return True, f"Whisper {_nama_model_whisper()} siap"
            nama = _nama_model_whisper()
            try:
                from faster_whisper import WhisperModel
                self._whisper = WhisperModel(
                    nama, device="cpu", compute_type="int8")
                self._muat_error = ""
                return True, f"Whisper {nama} siap (lokal, int8)"
            except Exception as exc:  # noqa: BLE001
                self._muat_error = str(exc)[:180]
                log.debug("pemuatan Whisper gagal", exc_info=True)
                return False, f"Whisper {nama} gagal dimuat: {self._muat_error}"

    def siapkan(self, kabar: Callable[..., None] | None = None) -> None:
        """Muat Whisper di LATAR — unduhan pertama bisa beberapa menit.

        Jalur utama sekarang memakai pastikan() secara sinkron sebelum mic
        dibuka; fungsi latar dipertahankan untuk pemanggil lama."""
        def _muat() -> None:
            ok, pesan = self.pastikan(kabar)
            self._kabar(pesan, batal=not ok)

        threading.Thread(target=_muat, daemon=True,
                         name="bagasai-whisper").start()

    def kenali(self, data: bytes) -> tuple[str, str]:
        """Ubah audio mentah (int16 mono 16 kHz) jadi teks."""
        if self._whisper is None:
            ok, pesan = self.pastikan()
            if not ok:
                return "", f"galat: {pesan}"
        try:
            return self._whisperkenali(data), "whisper"
        except Exception as exc:  # noqa: BLE001
            log.debug("pengenalan Whisper lokal gagal", exc_info=True)
            return "", f"galat: Whisper lokal gagal: {str(exc)[:120]}"

    def _whisperkenali(self, data: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
        segmen, _info = self._whisper.transcribe(
            audio, language="id", beam_size=5, vad_filter=False,
            condition_on_previous_text=False,
            # Audio di sini sudah dideteksi dan dipotong oleh WebRTC di depan.
            # Menyalakan Silero VAD lagi membuat suara jauh yang pelan lolos
            # dari pemotong pertama tetapi dibuang seluruhnya oleh pemotong
            # kedua, sehingga Whisper terlihat "diam".
            # Jangan memakai initial_prompt: pada rekaman yang sangat pelan,
            # Whisper dapat mengulang prompt itu sebagai hasil seolah-olah
            # pengguna benar-benar mengucapkannya.
        )
        teks = " ".join(s.text.strip() for s in segmen).strip()
        if _halusinasi_sunyi(teks):
            log.debug("hasil Whisper dibuang sebagai halusinasi sunyi: %r", teks)
            return ""
        return teks


_PENGENAL: _Pengenal | None = None


def pengenal() -> _Pengenal:
    """Mesin pengenal bersama seluruh proses (dimuat malas sekali)."""
    global _PENGENAL
    if _PENGENAL is None:
        _PENGENAL = _Pengenal()
    return _PENGENAL


def pastikan_model() -> tuple[bool, str]:
    """Unduh bila perlu dan uji model Whisper lokal yang dikonfigurasi."""
    return pengenal().pastikan()


# --- bunyi tanda -----------------------------------------------------------
# Nada NAIK saat mikrofon menyala, nada TURUN saat mati. Bukan hiasan: begitu
# fiturnya dipakai, mata pengguna ada di kodenya, bukan di baris status — dan
# "mikrofon saya sedang hidup atau tidak" adalah pertanyaan yang jawabannya tak
# boleh butuh melihat layar. Arahnya (naik/turun) sengaja berlawanan supaya
# bisa dibedakan tanpa menghafal nada.
_NADA_ON = ((660, 90), (990, 130))
_NADA_OFF = ((880, 90), (440, 150))
# Satu ketuk pendek saat NAMAKU TERDETEKSI — tanda "aku mendengarkanmu
# sekarang". Sengaja SEKALI dan pendek, bukan nada menerus selama
# merekam: apa pun yang berbunyi selagi mikrofon hidup akan ikut terekam
# dan mengacaukan pengenalan kalimat yang sedang diucapkan. Keadaan
# "sedang merekam" ditunjukkan terus-menerus lewat bar status.
_NADA_MULAI = ((1320, 70),)
# KETUKAN BERULANG selama merekam — supaya "mikrofonku sedang merekam" bisa
# diketahui tanpa melihat layar sama sekali.
#
# Ia PENDEK dan JARANG dengan sengaja, dan blok audio selama ia berbunyi
# DIBUANG (lihat _abaikan_sampai). Sebabnya teknis, bukan selera: apa pun yang
# keluar dari pengeras suara ikut masuk ke mikrofon. Nada menerus akan membuat
# pemotong ucapan tak pernah melihat sunyi — kalimatnya tak pernah dianggap
# selesai — dan bunyinya sendiri ikut terkirim ke pengenal suara.
_NADA_TIK = ((980, 45),)
_JEDA_TIK = 2.5
# Nada BATAL: dua ketuk MENURUN & rendah. Sengaja tak mirip nada apa pun yang
# lain — dilaporkan pengguna bahwa "batalkan" terasa berfungsi sama dengan
# "lakukan", dan satu-satunya cara membedakan keduanya tanpa melihat layar
# adalah bunyinya sendiri. Yang dikirim berdering naik; yang dibuang menurun.
_NADA_BATAL = ((520, 90), (300, 140))


_NADA = {"mulai": _NADA_MULAI, "tik": _NADA_TIK, "batal": _NADA_BATAL,
         True: _NADA_ON, False: _NADA_OFF}


def bunyi(nyala: Any = True) -> None:
    """Bunyikan tanda: True=menyala, False=mati, "mulai"/"tik" saat merekam."""
    nada = _NADA.get(nyala, _NADA_ON)
    try:
        import winsound                      # Windows: paling ringan & instan
        for f, ms in nada:
            winsound.Beep(int(f), int(ms))
        return
    except Exception:  # noqa: BLE001 - bukan Windows / tak ada pengeras suara
        pass
    try:
        # Di luar Windows: nadanya dibangkitkan sendiri lewat perangkat yang
        # SUDAH dipakai fitur ini, jadi tak menambah satu pun kebergantungan.
        import numpy as np
        import sounddevice as sd
        laju = 44100
        potong = []
        for f, ms in nada:
            t = np.linspace(0, ms / 1000, int(laju * ms / 1000), endpoint=False)
            potong.append((0.25 * np.sin(2 * np.pi * f * t)).astype("float32"))
        sd.play(np.concatenate(potong), laju, blocking=True)
    except Exception:  # noqa: BLE001 - tanda suara tak boleh menggagalkan apa pun
        log.debug("bunyi tanda gagal", exc_info=True)


def _rms(blok: Any) -> float:
    import numpy as np
    x = np.asarray(blok, dtype="float32")
    if not x.size:
        return 0.0
    return float(np.sqrt(np.mean(x * x)))


class PilihanMikrofon(NamedTuple):
    index: int | None
    name: str
    sample_rate: int
    hostapi: str


def _pilih_mikrofon(sd: Any) -> PilihanMikrofon:
    """Pilih endpoint input terbaik; di Windows utamakan WASAPI native-rate.

    PortAudio memilih MME lebih dulu pada banyak laptop Windows. MME memang
    kompatibel, tetapi browser memakai jalur modern. Memilih WASAPI secara
    eksplisit mempertahankan sample rate perangkat sebelum diolah WebRTC.
    """
    perangkat = list(sd.query_devices())
    hostapis = list(sd.query_hostapis())

    kandidat: list[int] = []
    if sys.platform == "win32":
        for i, host in enumerate(hostapis):
            if "wasapi" not in str(host.get("name", "")).lower():
                continue
            bawaan = int(host.get("default_input_device", -1))
            if bawaan >= 0:
                kandidat.append(bawaan)
            kandidat.extend(
                j for j, dev in enumerate(perangkat)
                if int(dev.get("hostapi", -1)) == i
                and int(dev.get("max_input_channels", 0)) > 0
            )

    try:
        bawaan = sd.default.device[0]
        if bawaan is not None and int(bawaan) >= 0:
            kandidat.append(int(bawaan))
    except Exception:  # noqa: BLE001
        pass
    kandidat.extend(i for i, dev in enumerate(perangkat)
                    if int(dev.get("max_input_channels", 0)) > 0)

    dilihat: set[int] = set()
    for index in kandidat:
        if index in dilihat or not (0 <= index < len(perangkat)):
            continue
        dilihat.add(index)
        dev = perangkat[index]
        if int(dev.get("max_input_channels", 0)) < 1:
            continue
        laju = max(8000, int(round(float(dev.get("default_samplerate", LAJU)))))
        try:
            sd.check_input_settings(device=index, samplerate=laju,
                                    channels=1, dtype="int16")
        except Exception:  # noqa: BLE001
            continue
        api_idx = int(dev.get("hostapi", -1))
        api = (str(hostapis[api_idx].get("name", ""))
               if 0 <= api_idx < len(hostapis) else "")
        return PilihanMikrofon(index, str(dev.get("name", "mikrofon")).strip(),
                              laju, api)
    raise RuntimeError("tak ada endpoint mikrofon mono yang dapat dibuka")


def _ubah_laju(data: Any, sumber: int, tujuan: int = LAJU) -> Any:
    """Resample int16 mono secara lokal dan ringan menuju 16 kHz."""
    import numpy as np

    x = np.asarray(data, dtype=np.int16).reshape(-1)
    if not x.size or sumber == tujuan:
        return x.copy()
    # Native WASAPI laptop umumnya 48 kHz -> 16 kHz. Rata-rata tiga sampel
    # memberi low-pass murah sebelum desimasi dan jauh lebih aman dari x[::3].
    if sumber > tujuan and sumber % tujuan == 0:
        rasio = sumber // tujuan
        cukup = (len(x) // rasio) * rasio
        if cukup:
            y = x[:cukup].astype("float32").reshape(-1, rasio).mean(axis=1)
            return np.clip(y, -32768, 32767).astype(np.int16)
    jumlah = max(1, int(round(len(x) * tujuan / float(sumber))))
    asal = np.arange(len(x), dtype="float64")
    sasaran = np.linspace(0, max(0, len(x) - 1), jumlah, dtype="float64")
    y = np.interp(sasaran, asal, x.astype("float32"))
    return np.clip(y, -32768, 32767).astype(np.int16)


class _AudioFrontEnd:
    """Resample + WebRTC noise suppression, AGC, dan VAD per 10 ms."""

    def __init__(self, sumber: int) -> None:
        import numpy as np

        self.sumber = int(sumber)
        self._np = np
        self._sisa = np.empty(0, dtype=np.int16)
        self._apm: Any = None
        try:
            from aec_audio_processing import AudioProcessor
            # Echo dari BagasAI dicegah pada level produk: TTS dihentikan saat
            # dikte dan listener membuang blok saat speaker aktif. AEC tanpa
            # reverse stream justru menebak-nebak dan dapat merusak ucapan.
            apm = AudioProcessor(enable_aec=False, enable_ns=True, ns_level=1,
                                 enable_agc=True, agc_mode=1, enable_vad=True)
            apm.set_stream_format(LAJU, 1, LAJU, 1)
            # Mode 1 cukup peka untuk orang yang bicara agak jauh. Mode 2/3
            # lebih mudah membuang suku kata pelan pada mikrofon laptop.
            apm.set_vad_aggressiveness(1)
            self._apm = apm
        except Exception:  # noqa: BLE001
            log.debug("WebRTC audio processing tidak tersedia", exc_info=True)

    @property
    def vad_tersedia(self) -> bool:
        return self._apm is not None

    def reset(self) -> None:
        self._sisa = self._np.empty(0, dtype=self._np.int16)

    def proses(self, data: Any) -> tuple[Any, Any, bool]:
        """Return (audio_bersih, audio_mentah_16k, ada_suara_manusia)."""
        mentah = _ubah_laju(data, self.sumber)
        if self._apm is None or not mentah.size:
            return mentah, mentah, False
        gabung = self._np.concatenate((self._sisa, mentah))
        bingkai = int(self._apm.get_frame_size())
        jumlah = len(gabung) // bingkai
        if not jumlah:
            self._sisa = gabung
            kosong = self._np.empty(0, dtype=self._np.int16)
            return kosong, kosong, False
        batas = jumlah * bingkai
        dipakai = gabung[:batas]
        self._sisa = gabung[batas:]
        keluaran: list[Any] = []
        suara = False
        try:
            for i in range(0, batas, bingkai):
                hasil = self._apm.process_stream(dipakai[i:i + bingkai].tobytes())
                keluaran.append(self._np.frombuffer(hasil, dtype=self._np.int16).copy())
                suara = bool(self._apm.has_voice()) or suara
            return self._np.concatenate(keluaran), dipakai, suara
        except Exception:  # noqa: BLE001
            log.debug("pemrosesan WebRTC gagal; memakai audio mentah", exc_info=True)
            self._apm = None
            return dipakai, dipakai, False


def _buka_input(sd: Any) -> tuple[Any, PilihanMikrofon, int]:
    pilihan = _pilih_mikrofon(sd)
    blok = max(160, int(round(pilihan.sample_rate * _DETIK_BLOK)))
    stream = sd.InputStream(device=pilihan.index,
                            samplerate=pilihan.sample_rate,
                            channels=1, dtype="int16", blocksize=blok)
    return stream, pilihan, blok


class Pendengar:
    """Mendengarkan mikrofon di THREAD LATAR sampai dihentikan.

    Alurnya: potong aliran mikrofon jadi UCAPAN (dipisah oleh sunyi), kirim tiap
    ucapan ke pengenal suara, lalu umpankan teksnya ke Perakit. Pengenalannya
    dikerjakan thread TERSENDIRI supaya perekaman tak pernah berhenti selagi
    menunggu inferensi lokal — kalau tidak, kalimat berikutnya terpotong tepat
    saat pengguna masih bicara."""

    def __init__(self, on_perintah: Callable[[str], None],
                 on_kabar: Callable[..., None] | None = None,
                 on_dengar: Callable[[str, bool], None] | None = None,
                 maks_rekam: float = MAKS_REKAM,
                 jangkauan: str | None = None,
                 langsung: bool = False,
                 on_level: Callable[[float, bool], None] | None = None) -> None:
        self.on_perintah = on_perintah
        # on_kabar(pesan, batal=False). Penanda `batal` ada karena TAMPILANNYA
        # memperlakukan pembatalan berbeda dari kabar lain: cuma pembatalan
        # yang dicetak (permintaan pengguna — sisanya membuat layar penuh teks
        # yang tak menambah apa-apa). Penandanya dipasang DI SINI, di tempat
        # kejadiannya, bukan ditebak dari bunyi kalimatnya di sisi tampilan:
        # tebakan begitu putus diam-diam begitu kalimatnya diperhalus, dan yang
        # hilang justru satu-satunya kabar yang masih ingin dilihat.
        self.on_kabar = on_kabar or (lambda _m, **_k: None)
        # on_dengar(teks, sedang_merekam) -> ditampilkan sebagai "yang terdengar"
        self.on_dengar = on_dengar or (lambda _t, _m: None)
        # Mode langsung dipakai layar /voice baru: setiap ucapan yang sudah
        # ditutup oleh jeda sunyi langsung menjadi prompt, tanpa wake word.
        # Jalur lama tetap tersedia untuk antarmuka/klien yang memang ingin
        # memakai Perakit dan kata pemicu.
        self.langsung = bool(langsung)
        self.on_level = on_level or (lambda _level, _mendengar: None)
        self._sedang_ucapan = False
        self._level_terakhir = 0.0
        self._status_level_terakhir = False
        self.perakit = Perakit(maks_rekam)
        # Seberapa jauh boleh mendengar. Disimpan NAMANYA juga, bukan cuma
        # angkanya: bar status & `/voice` menyebutnya, dan "jauh" jauh lebih
        # bisa ditindaklanjuti daripada "ambang 148".
        self.jangkauan = (str(jangkauan).strip().lower()
                          if jangkauan else _nama_jangkauan())
        self.profil = profil(self.jangkauan)
        # Hasil kalibrasi derau ruangan — ditampilkan ke pengguna supaya
        # "mikrofonnya tuli" bisa dijawab angka, bukan dugaan.
        self.derau = 0.0
        self.derau_ramai = 0.0
        self.ambang = 0.0
        # Ambang untuk MENERUSKAN ucapan yang sudah berjalan (histeresis).
        # Selalu lebih rendah dari `ambang`; lihat Jangkauan.
        self.ambang_lanjut = 0.0
        # Mesin pengenal Whisper BERSAMA — singleton modul, bukan milik
        # instance ini: Pendengar diciptakan ulang tiap `/voice on`, dan model
        # Whisper yang ikut terbuang tiap kali itu terjadi berarti unduhan 500 MB
        # yang tak pernah habis dipakai.
        self._pengenal = pengenal()
        # Riwayat potongan ucapan (mulai, akhir, audio) ±35 detik terakhir.
        # Bahan pengenalan SATU UTUHAN: perintah yang tercacah jeda di tengah
        # kalimat dienali ulang menyeluruh saat ditutup — potongan-potongannya
        # tak saling kenal, utuhannya saling kenal.
        self._riwayat: list[tuple[float, float, bytes]] = []
        self._stop = threading.Event()
        # Sampai kapan blok audio DIBUANG (selagi ketukan penanda berbunyi).
        # Tanpa ini, bunyi bagas-ai sendiri ikut terekam lalu dikirim ke
        # pengenal suara — dan yang lebih buruk, ia menahan pemotong ucapan
        # supaya tak pernah melihat sunyi.
        self._abaikan_sampai = 0.0
        # Sejak kapan pembicara BERHENTI (0 = sedang bicara / belum mulai).
        # Dibaca penyelesai: perintah ditutup bila diamnya sudah cukup lama.
        self._sunyi_sejak = 0.0
        # Berapa potongan ucapan yang masih menunggu jawaban pengenal suara.
        # Perintah TAK BOLEH ditutup selagi ini > 0: teksnya datang beberapa
        # detik sesudah suaranya, dan menutup lebih dulu berarti mengirim
        # kalimat yang belum lengkap.
        self._menunggu = 0
        self._antre: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self.galat = ""
        self.mikrofon: PilihanMikrofon | None = None
        self.audio_overflow = 0

    @property
    def aktif(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    @property
    def merekam(self) -> bool:
        """True bila namaku sudah disebut & perintahnya sedang dikumpulkan.

        Dibaca bar status terminal: selama ini menyala, apa pun yang terucap
        ikut jadi perintah — keadaan yang harus terbaca sekali lihat."""
        return self._sedang_ucapan if self.langsung else self.perakit.merekam

    def mulai(self) -> str:
        """Nyalakan mikrofon. Return "" bila berhasil, atau alasan gagalnya."""
        if self.aktif:
            return ""
        ok, alasan = siap()
        if not ok:
            return alasan
        # Jangan mulai merekam sebelum model lokal benar-benar siap. Installer
        # mengunduhnya di muka; ini tetap memverifikasi runtime dan mencegah
        # giliran pertama gagal sesudah pengguna telanjur bicara.
        model_ok, model_pesan = self._pengenal.pastikan(self.on_kabar)
        if not model_ok:
            return model_pesan
        self._stop.clear()
        self.galat = ""
        self._threads = [
            threading.Thread(target=self._rekam, daemon=True,
                             name="bagasai-mic"),
            threading.Thread(target=self._kenali, daemon=True,
                             name="bagasai-stt"),
        ]
        for t in self._threads:
            t.start()
        return ""

    def berhenti(self) -> None:
        self._stop.set()
        self._antre.put(None)
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        self.perakit.batalkan()
        self._sedang_ucapan = False
        self._lapor_level(0.0, False, paksa=True)

    # --- thread 1: mikrofon -> potongan ucapan ---
    def _rekam(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self._gagal(f"paket audio tak bisa dimuat: {exc}")
            return
        try:
            stream, pilihan, blok_sumber = _buka_input(sd)
            self.mikrofon = pilihan
            with stream:
                self._gelung(stream, np, pilihan.sample_rate, blok_sumber)
        except Exception as exc:  # noqa: BLE001 - mikrofon dipakai aplikasi lain, dsb
            self._gagal(f"mikrofon tak bisa dibuka: {exc}")

    def _gelung(self, stream: Any, np: Any,
                laju_sumber: int = LAJU, blok_sumber: int = BLOK) -> None:
        depan = _AudioFrontEnd(laju_sumber)
        # Derau ruangan diukur DULU. Ambang tetap yang dipatok di kode selalu
        # salah di salah satu sisi: di ruangan sunyi ia melewatkan bisikan, di
        # ruangan berkipas ia menganggap kipasnya bicara sepanjang waktu.
        contoh: list[float] = []
        suara_saat_kalibrasi: list[Any] = []
        habis = time.time() + _KALIBRASI
        while time.time() < habis and not self._stop.is_set():
            data, overflow = stream.read(blok_sumber)
            if overflow:
                self.audio_overflow += 1
            bersih, mentah, vad = depan.proses(data)
            if len(bersih) and vad:
                # Pengguna langsung bicara setelah /voice on: jangan jadikan
                # suaranya "derau" dan jangan buang wake word pertamanya.
                suara_saat_kalibrasi.append(bersih.copy())
            elif len(mentah):
                contoh.append(_rms(mentah))
        derau, ramai, ambang, ambang_lanjut = hitung_ambang(contoh, self.profil)
        self.derau, self.derau_ramai = derau, ramai
        self.ambang, self.ambang_lanjut = ambang, ambang_lanjut
        log.debug("jangkauan %s: derau %.0f (ramai %.0f) -> ambang %.0f/%.0f",
                  self.jangkauan, derau, ramai, ambang, ambang_lanjut)

        potongan: list[Any] = list(suara_saat_kalibrasi)
        self._sedang_ucapan = bool(potongan)
        # Sedikit rekaman SEBELUM ambang terlampaui ikut disimpan: suku kata
        # pertama selalu lebih pelan dari sisanya, dan tanpa ini "bagas" kerap
        # sampai sebagai "gas". Panjangnya mengikuti jangkauan — dari jauh
        # selisih pelan-keras itu jauh lebih lebar.
        awalan: list[Any] = []
        sunyi = 0.0
        energi_beruntun = 0
        mulai = (time.time() - sum(len(x) for x in potongan) / LAJU
                 if potongan else 0.0)
        tik = 0.0
        # Noise floor menyesuaikan perlahan selama bagian yang oleh VAD dinilai
        # bukan suara manusia. Kipas yang dinyalakan setelah startup tidak lagi
        # membuat ambang awal menjadi usang sepanjang sesi.
        derau_adaptif = deque(contoh, maxlen=max(32, int(10 / _DETIK_BLOK)))
        adaptasi_terakhir = time.time()
        while not self._stop.is_set():
            try:
                data, overflow = stream.read(blok_sumber)
            except Exception as exc:  # noqa: BLE001
                self._gagal(f"aliran mikrofon terputus: {exc}")
                return
            if overflow:
                self.audio_overflow += 1
            data_bersih, data_mentah, vad_suara = depan.proses(data)
            if not len(data_bersih):
                continue
            # Ketukan penanda "sedang merekam". Dibunyikan HANYA di sela
            # ucapan (potongan kosong): kalau disisipkan di tengah kalimat, ia
            # ikut terekam dan merusak pengenalannya.
            if not self.langsung and self.perakit.merekam and not potongan:
                if time.time() - tik >= _JEDA_TIK:
                    tik = time.time()
                    self._abaikan_sampai = tik + _NADA_TIK[0][1] / 1000 + 0.25
                    threading.Thread(target=bunyi, args=("tik",),
                                     daemon=True).start()
            elif not self.perakit.merekam:
                tik = 0.0
            if time.time() < self._abaikan_sampai or self._bagasai_bicara():
                # Bagas-ai sedang mengeluarkan suara (sapaan, ketukan, atau
                # kabar yang dibacakan). Bloknya DIBUANG supaya suaranya sendiri
                # tak ikut jadi perintah, dan hitungan diam DIULANG supaya
                # bacaannya tak dihitung sebagai "pengguna sudah berhenti".
                potongan = []
                awalan = []
                energi_beruntun = 0
                self._sunyi_sejak = time.time()
                self._sedang_ucapan = False
                self._lapor_level(0.0, False, paksa=True)
                depan.reset()
                continue
            # HISTERESIS. Bar TINGGI untuk memulai ucapan, bar RENDAH untuk
            # meneruskannya — `potongan` yang tak kosong berarti pembicara
            # sedang di tengah kalimat.
            #
            # Ini yang membuat jarak jauh bisa bekerja. Dari seberang ruangan
            # tenaga suara turun drastis di tengah kata, jadi dengan satu
            # ambang saja kalimatnya tercacah jadi kepingan sependek suku kata
            # — dan kepingan sependek itu hampir selalu gagal dikenali, lalu
            # namanya hilang tanpa jejak. Ambang tingginya tetap dipakai di
            # SELA ucapan, supaya derau ruangan tak menahan hitungan diam yang
            # menutup perintah.
            tingkat = _rms(data_mentah)
            batas_energi = (
                max(self.ambang_lanjut, self.ambang * 0.85)
                if potongan and depan.vad_tersedia else
                self.ambang_lanjut if potongan else self.ambang
            )
            energi_suara = tingkat > batas_energi
            energi_beruntun = energi_beruntun + 1 if energi_suara else 0
            # VAD tetap jalur utama. Dua blok energi berturut-turut menjadi
            # jaring untuk suara jauh yang dianggap bukan-suara oleh WebRTC;
            # satu dentuman keyboard tidak cukup untuk membuka rekaman.
            keras = (vad_suara or energi_beruntun >= 2
                     if depan.vad_tersedia else energi_suara)
            self._lapor_level(tingkat, keras)
            lama_blok = len(data_bersih) / LAJU
            if not potongan and not vad_suara and not self.perakit.merekam:
                # Tanpa VAD, jangan belajar dari bunyi yang sudah melewati
                # ambang lama — itu mungkin ucapan, bukan perubahan derau.
                if depan.vad_tersedia or tingkat < self.ambang:
                    derau_adaptif.append(tingkat)
            if (len(derau_adaptif) >= 12
                    and time.time() - adaptasi_terakhir >= 0.8):
                nd, nr, nm, nl = hitung_ambang(list(derau_adaptif), self.profil)
                # Pelan-pelan agar satu perubahan mendadak tidak memotong kata.
                bobot = 0.18
                self.derau = self.derau * (1 - bobot) + nd * bobot
                self.derau_ramai = self.derau_ramai * (1 - bobot) + nr * bobot
                self.ambang = self.ambang * (1 - bobot) + nm * bobot
                self.ambang_lanjut = self.ambang_lanjut * (1 - bobot) + nl * bobot
                adaptasi_terakhir = time.time()
            # Penanda "sejak kapan sunyi" — dasar penutup perintah. Diukur
            # dari SUARA, bukan teks yang datang setelah inferensi selesai.
            if keras:
                self._sunyi_sejak = 0.0
            elif not self._sunyi_sejak:
                self._sunyi_sejak = time.time()
            if not self.langsung:
                self._periksa_selesai()
            if not potongan:
                awalan.append(data_bersih.copy())
                if len(awalan) > self.profil.awalan:
                    awalan.pop(0)
                if keras:
                    # `awalan` sudah memuat blok sekarang; menambahkannya lagi
                    # menduplikasi ±64 ms dan mengaburkan konsonan awal.
                    potongan = list(awalan)
                    awalan = []
                    sunyi, mulai = 0.0, time.time()
                    self._sedang_ucapan = True
                continue
            potongan.append(data_bersih.copy())
            sunyi = 0.0 if keras else sunyi + lama_blok
            panjang = time.time() - mulai
            if sunyi >= _DIAM_SELESAI or panjang >= _MAKS_UCAPAN:
                if panjang - sunyi >= _MIN_UCAPAN:
                    blob = np.concatenate(potongan).tobytes()
                    self._menunggu += 1
                    self._antre.put(blob)
                    if not self.langsung:
                        # Riwayat untuk pengenalan satu-utuhan (lihat
                        # _periksa_selesai). Mode langsung sudah mengirim satu
                        # ucapan ini apa adanya dan tidak membutuhkan riwayat.
                        self._riwayat.append((mulai, mulai + panjang, blob))
                        batas = time.time() - (MAKS_REKAM + 5.0)
                        if self._riwayat and self._riwayat[0][0] < batas:
                            self._riwayat = [r for r in self._riwayat
                                             if r[0] >= batas]
                potongan = []
                self._sedang_ucapan = False
                self._lapor_level(0.0, False, paksa=True)
                sunyi = 0.0
                energi_beruntun = 0
            # Perintah yang menggantung tanpa kata penutup dibatalkan di sini —
            # bukan di thread pengenal, yang bisa saja sedang menjalankan inferensi.
            if not self.langsung and self.perakit.kedaluwarsa():
                self.on_kabar(
                    f"perintah suara dibatalkan — {MAKS_REKAM:.0f} detik "
                    "habis sebelum perintah selesai", batal=True)

    def _lapor_level(self, tingkat: float, mendengar: bool,
                     *, paksa: bool = False) -> None:
        """Kirim level 0..1 untuk visualisasi tanpa membebani thread UI."""
        sekarang = time.monotonic()
        berubah = bool(mendengar) != self._status_level_terakhir
        if not paksa and not berubah and sekarang - self._level_terakhir < 0.06:
            return
        # Ambang adalah titik mulai bicara. Dua kali ambang sudah dianggap
        # gerak maksimum agar suara biasa tetap terlihat hidup, bukan hanya
        # teriakan yang mampu menggerakkan orb.
        dasar = max(float(self.ambang or self.profil.lantai), 1.0)
        level = max(0.0, min(1.0, float(tingkat) / (dasar * 2.0)))
        self._level_terakhir = sekarang
        self._status_level_terakhir = bool(mendengar)
        try:
            self.on_level(level, bool(mendengar))
        except Exception:  # noqa: BLE001 - visual tak boleh mematikan mikrofon
            log.debug("penerima level suara melempar", exc_info=True)

    @staticmethod
    def _bagasai_bicara() -> bool:
        """True bila bagas-ai sendiri sedang mengeluarkan suara."""
        try:
            from . import suara
            return suara.sibuk()
        except Exception:  # noqa: BLE001
            return False

    def _sapa(self) -> None:
        """Ketuk singkat saat wake word diterima, tanpa menutupi ucapan."""
        threading.Thread(target=bunyi, args=("mulai",), daemon=True).start()

    def _periksa_selesai(self) -> None:
        """Tutup perintah bila pembicara sudah diam cukup lama.

        Tiga syarat, dan ketiganya perlu:
          1. memang sedang merekam & sudah ada isinya;
          2. diam sudah melewati JEDA_SELESAI — bicara lagi sebelum itu
             mengulang hitungannya dari nol (lihat _sunyi_sejak);
          3. TAK ADA potongan yang masih menunggu jawaban pengenal. Tanpa
             syarat ketiga, kalimat terakhir yang masih di jalan akan hilang —
             dan yang terkirim cuma separuh perintah.
        """
        if not (self.perakit.merekam and self._sunyi_sejak):
            return
        if time.time() - self._sunyi_sejak < JEDA_SELESAI:
            return
        if self._menunggu or not self._antre.empty():
            return
        if not self.perakit.sementara:
            return
        # Waktu namanya disebut harus ditangkap SEBELUM selesai() me-reset
        # perakit — dipakai memilih potongan mana yang jadi perintah utuh.
        sebut = self.perakit.disebut_pada
        perintah = self.perakit.selesai()
        self._sunyi_sejak = 0.0
        # Ketuk singkat, bukan kalimat TTS. Sapaan panjang membuat mikrofon
        # membuang ucapan berikutnya selama speaker aktif dan terasa "tuli".
        threading.Thread(target=bunyi, args=("mulai",), daemon=True).start()
        # PENGECUALIAN DUA TAHAP. Teks per-potongan di atas cuma cadangan: yang
        # benar-benar dikirim adalah pengenalan ulang SATU UTUH perintah (semua
        # potongannya dijadikan satu). Ini penutup masalah terbesar pengenal
        # lawas: potongan yang terpisah jeda dienali TANPA saling kenal, dan
        # potongan pendek persis yang paling sering salah baca — cara kerja
        # ChatGPT di browser persis kebalikannya, satu utuhan sekali baca.
        audio = self._audio_perintah(sebut)
        if audio:
            self._menunggu += 1
            self._antre.put(("utuh", audio, perintah))
        else:
            self._kirim_perintah(perintah)

    def _audio_perintah(self, sebut: float) -> bytes:
        """Audio utuh satu perintah: potongan-potongan sejak namanya disebut.

        Jendela seleksinya dimulai 5 detik SEBELUM nama terdeteksi: nama berada
        di AWAL potongannya, dan `disebut_pada` baru dicatat sesudah potongan
        itu selesai dikenali (plus jeda inferensi) — jadi awal potongan bisa jauh
        lebih awal dari stempel itu. Nama ikut terbawa di teks utuh, lalu
        dibuang lagi oleh cari_pemicu — sama seperti jalur per-potongan."""
        if not self._riwayat or not sebut:
            return b""
        ambil = [b for _m, akhir, b in self._riwayat if akhir >= sebut - 5.0]
        return b"".join(ambil)

    def _kirim_perintah(self, perintah: str) -> None:
        if not perintah:
            return
        try:
            self.on_perintah(perintah)
        except Exception:  # noqa: BLE001 - UI tak boleh menjatuhkan mikrofon
            log.debug("penerima perintah suara melempar", exc_info=True)

    # --- thread 2: potongan ucapan -> teks -> perintah ---
    def _kenali(self) -> None:
        """Thread pengenal: ambil dari antrean, ubah jadi teks.

        Antreannya berisi dua hal: potongan ucapan biasa (bytes) untuk
        menampilkan "yang terdengar" & mendeteksi nama SECARA LANGSUNG, dan
        perintah utuh (tuple) hasil pengenalan satu-utuhan yang TIDAK lagi
        melalui perakit — perakit sudah selesai, kalimatnya tinggal dikirim.
        """
        while not self._stop.is_set():
            data = self._antre.get()
            if data is None:
                return
            try:
                if isinstance(data, tuple):
                    self._satu_utuhan(data)
                else:
                    self._satu_ucapan(data)
            except Exception:  # noqa: BLE001 - satu potongan gagal, jangan mati
                log.debug("pengenalan satu ucapan gagal", exc_info=True)
            finally:
                # Diturunkan SESUDAH teksnya masuk ke perakit. Kalau lebih awal,
                # penyelesai bisa menutup perintah tepat di celah antara "audio
                # selesai dikenali" dan "teksnya tercatat" — dan kalimat
                # terakhir hilang.
                self._menunggu = max(0, self._menunggu - 1)

    def _satu_ucapan(self, data: bytes) -> None:
        """Kenali SATU potongan ucapan lalu umpankan ke perakit."""
        teks, info = self._pengenal.kenali(data)
        # Esc dapat ditekan saat Whisper masih mengolah potongan terakhir.
        # Hasil yang baru selesai sesudah listener dihentikan tidak boleh
        # menyelinap menjadi prompt dari sesi voice yang sudah ditutup.
        if self._stop.is_set():
            return
        if not teks:
            if info.startswith("galat"):
                self.on_kabar(f"pengenalan suara gagal: {info[6:]}")
            else:
                # Diteruskan lewat on_dengar, TIDAK dicetak sendiri: terminal
                # sengaja tak menampilkan transkrip apa pun di atas kotak chat
                # (permintaan pengguna — itu barisan debug). Jalurnya tetap ada
                # untuk penelusuran.
                self.on_dengar("(tertangkap, tapi tak terdengar jelas)",
                               self.perakit.merekam)
            return

        if self.langsung:
            self.on_dengar(teks, False)
            self._kirim_perintah(teks)
            return

        self.on_dengar(teks, self.perakit.merekam)
        sedang = self.perakit.merekam
        hasil = self.perakit.dengar(teks)
        if not sedang and self.perakit.merekam:
            # Wake word baru terdeteksi: ketuk singkat. Sapaan TTS panjang
            # sengaja dihapus agar pengguna dapat langsung meneruskan kalimat.
            self._sapa()
        if hasil is BATAL:
            # Bunyinya MENURUN, kebalikan nada mulai — supaya "dibatalkan" dan
            # "dikirim" tak pernah tertukar walau layarnya tak dilihat.
            threading.Thread(target=bunyi, args=("batal",), daemon=True).start()
            self.on_kabar("rekaman DIBATALKAN, tak ada yang dikirim — sebut "
                          "namaku lagi untuk perintah baru", batal=True)
            return
        # Selain itu tak ada yang perlu dilakukan di sini: perintahnya ditutup
        # oleh BERHENTINYA suara, bukan oleh kata apa pun (lihat
        # _periksa_selesai). Yang mengirimnya thread perekam, yang memang tahu
        # persis kapan pembicara berhenti.

    def _satu_utuhan(self, item: tuple) -> None:
        """Kenali SATU PERINTAH UTUH dan kirim — pengganti teks per-potongan.

        Dipanggil dari antrean sesudah perintah ditutup (lihat
        _periksa_selesai). Teksnya diuji cari_pemicu SEKALI LAGI: audio utuh
        memuat nama di awalnya, dan yang dikirim memang hanya yang SESUDAH
        nama — jalur yang sama dengan per-potongan, jadi tak ada dua logika
        pemangkasan yang bisa saling bertentangan."""
        _jenis, audio, cadangan = item
        perintah = cadangan
        teks, info = self._pengenal.kenali(audio)
        if self._stop.is_set():
            return
        if teks:
            self.on_dengar(teks, False)
            kata = _kata(teks)
            i = cari_pemicu(kata)
            if i >= 0 and kata[i:]:
                perintah = " ".join(kata[i:])
        elif info.startswith("galat"):
            # Pengenalan utuhan lokal gagal — cadangan per-potongan
            # sudah ada di tangan, jadi ini bukan bencana; cukup dicatat.
            log.debug("pengenalan utuhan gagal: %s", info)
        self._kirim_perintah(perintah)

    def _gagal(self, pesan: str) -> None:
        self.galat = pesan
        self._stop.set()
        self._sedang_ucapan = False
        self._lapor_level(0.0, False, paksa=True)
        self.on_kabar(pesan)


def dengar_dikte(
    *,
    berhenti: threading.Event | None = None,
    on_status: Callable[[str], None] | None = None,
    jangkauan: str | None = None,
    tunggu_mulai: float = 12.0,
    maks_detik: float = MAKS_REKAM,
    jeda_selesai: float = 1.0,
) -> tuple[str, dict[str, Any]]:
    """Rekam satu prompt langsung, tanpa wake word, lalu transkripsikan lokal.

    Tombol/F4 memanggil fungsi blocking ini dari thread. Rekaman mulai pada
    suara manusia pertama (VAD WebRTC), berhenti otomatis setelah sunyi, atau
    segera ketika tombol ditekan lagi melalui ``berhenti``.
    """
    import numpy as np
    import sounddevice as sd

    stop = berhenti or threading.Event()
    status = on_status or (lambda _fase: None)
    ok, alasan = siap()
    if not ok:
        raise RuntimeError(alasan)
    ok, alasan = pengenal().pastikan()
    if not ok:
        raise RuntimeError(alasan)
    # Hilangkan sumber echo sebelum endpoint mikrofon dibuka. Untuk listener
    # hands-free, blok speaker tetap dibuang oleh Pendengar._gelung.
    try:
        from . import suara
        suara.diam()
    except Exception:  # noqa: BLE001
        pass

    stream, pilihan, blok_sumber = _buka_input(sd)
    depan = _AudioFrontEnd(pilihan.sample_rate)
    jang = profil(jangkauan)
    derau: deque[float] = deque(maxlen=max(24, int(8 / _DETIK_BLOK)))
    awalan: deque[Any] = deque(maxlen=max(3, int(0.45 / _DETIK_BLOK)))
    audio: list[Any] = []
    mulai = time.time()
    bicara_mulai = 0.0
    sunyi = 0.0
    durasi_bicara = 0.0
    puncak = 0.0
    ambang = jang.lantai
    energi_beruntun = 0
    status("menunggu")

    with stream:
        while not stop.is_set() and time.time() - mulai < tunggu_mulai:
            data, _overflow = stream.read(blok_sumber)
            bersih, mentah, vad_suara = depan.proses(data)
            if not len(bersih):
                continue
            tingkat = _rms(mentah)
            puncak = max(puncak, tingkat)
            awalan.append(bersih.copy())
            energi_bicara = False
            if not vad_suara:
                # Empat blok pertama membentuk baseline. Sesudah itu, sampel
                # yang sudah melampaui ambang jangan ikut dimasukkan sebagai
                # "derau" karena ia bisa jadi suara jauh yang dilewatkan VAD.
                if len(derau) < 4:
                    derau.append(tingkat)
                    _d, _r, ambang, _l = hitung_ambang(list(derau), jang)
                else:
                    energi_bicara = tingkat > max(jang.lantai, ambang * 0.85)
                    if not energi_bicara:
                        derau.append(tingkat)
                        _d, _r, ambang, _l = hitung_ambang(list(derau), jang)
            # VAD adalah penentu utama. Dua blok energi berturut-turut menjadi
            # jaring untuk suara jauh; satu sentakan kipas/keyboard tetap tidak
            # membuka rekaman.
            energi_beruntun = energi_beruntun + 1 if energi_bicara else 0
            suara_manusia = (vad_suara or energi_beruntun >= 2
                             if depan.vad_tersedia else energi_bicara)
            if suara_manusia:
                audio.extend(list(awalan))
                awalan.clear()
                bicara_mulai = time.time()
                # Titik mulai sendiri sudah mensyaratkan VAD atau dua blok
                # energi beruntun. Anggap syarat ucapan minimum terpenuhi;
                # bila tidak, ucapan pendek yang baru terdeteksi di ujungnya
                # tak akan pernah auto-stop dan menunggu batas maksimum.
                durasi_bicara = _MIN_UCAPAN
                status("merekam")
                break

        energi_lanjut_beruntun = 0
        while bicara_mulai and not stop.is_set():
            if time.time() - bicara_mulai >= maks_detik:
                break
            data, _overflow = stream.read(blok_sumber)
            bersih, mentah, vad_suara = depan.proses(data)
            if not len(bersih):
                continue
            tingkat = _rms(mentah)
            puncak = max(puncak, tingkat)
            audio.append(bersih.copy())
            lama = len(bersih) / LAJU
            # Dua blok di atas ambang penuh diperlukan bila VAD melewatkan
            # suara jauh. Satu lonjakan derau tidak lagi mengulang penghitung
            # sunyi sampai batas maksimum rekaman.
            energi_lanjut = tingkat > max(jang.lantai, ambang * 0.85)
            energi_lanjut_beruntun = (
                energi_lanjut_beruntun + 1 if energi_lanjut else 0)
            keras = vad_suara or energi_lanjut_beruntun >= 2
            if keras:
                sunyi = 0.0
                durasi_bicara += lama
            else:
                sunyi += lama
            if sunyi >= jeda_selesai and durasi_bicara >= _MIN_UCAPAN:
                break

    info: dict[str, Any] = {
        "mikrofon": pilihan.name,
        "hostapi": pilihan.hostapi,
        "sample_rate": pilihan.sample_rate,
        "puncak": puncak,
        "durasi": (sum(len(x) for x in audio) / LAJU if audio else 0.0),
        "durasi_bicara": durasi_bicara,
        "ambang": ambang,
        "engine": "",
    }
    if not audio:
        return "", info
    status("menganalisis")
    teks, engine = pengenal().kenali(np.concatenate(audio).tobytes())
    info["engine"] = engine
    if engine.startswith("galat:"):
        raise RuntimeError(engine.removeprefix("galat:").strip())
    if teks:
        kata = _kata(teks)
        pemicu = cari_pemicu(kata)
        if pemicu >= 0 and pemicu < len(kata):
            teks = " ".join(kata[pemicu:])
    return teks.strip(), info


def dengar_sekali(detik: float = 5.0) -> tuple[str, float]:
    """Rekam sebentar lalu kenali — dipakai `/voice tes`.

    Mengembalikan (teks, tingkat_suara_tertinggi). Tingkat suaranya ikut
    dikembalikan karena dua kegagalan yang paling sering terjadi TAMPAK SAMA
    dari luar (tak ada teks): mikrofon yang bisu, dan pengenalan yang tak
    paham. Angka itu memisahkan keduanya."""
    import numpy as np
    import sounddevice as sd

    ok, alasan = pengenal().pastikan()
    if not ok:
        raise RuntimeError(alasan)
    blok: list[Any] = []
    puncak = 0.0
    stream, pilihan, blok_sumber = _buka_input(sd)
    depan = _AudioFrontEnd(pilihan.sample_rate)
    with stream:
        habis = time.time() + detik
        while time.time() < habis:
            data, _ = stream.read(blok_sumber)
            bersih, mentah, _vad = depan.proses(data)
            if len(bersih):
                blok.append(bersih.copy())
                puncak = max(puncak, _rms(mentah))
    if not blok:
        return "", 0.0
    # Mesin yang sama dengan yang dipakai mendengarkan sungguhan — /voice tes
    # yang memakai mesin lain mengukur mesin yang salah.
    teks, _info = pengenal().kenali(np.concatenate(blok).tobytes())
    return teks, puncak


def ukur(detik: float = 6.0, kalibrasi: float = 1.2,
         jangkauan: str | None = None) -> dict[str, Any]:
    """Ukur apakah suara DARI TEMPATMU BERDIRI sampai ke mikrofon.

    Dipakai `/voice jangkau`. Alasannya sederhana: "mic-nya kurang jauh" tak
    bisa dijawab dari kursi tempat kodenya ditulis — jaraknya, ruangannya, dan
    mikrofonnya cuma ada di rumah pengguna. Jadi yang disediakan bukan tebakan
    ambang yang lebih baik, melainkan cara MENGUKURNYA dari titik yang
    sebenarnya dipakai.

    Urutannya: diam dulu (kalibrasi derau), lalu pengguna bicara. Ambangnya
    dihitung dengan hitung_ambang() yang sama persis dengan yang dipakai
    mendengarkan sungguhan — alat ukur yang punya rumus sendiri cuma
    memindahkan letak salahnya."""
    import numpy as np
    import sounddevice as sd

    ok, alasan = pengenal().pastikan()
    if not ok:
        raise RuntimeError(alasan)
    jang = profil(jangkauan)
    sunyi: list[float] = []
    blok: list[Any] = []
    tingkat: list[float] = []
    stream, pilihan, blok_sumber = _buka_input(sd)
    depan = _AudioFrontEnd(pilihan.sample_rate)
    with stream:
        habis = time.time() + kalibrasi
        while time.time() < habis:
            data, _ = stream.read(blok_sumber)
            _bersih, mentah, _vad = depan.proses(data)
            if len(mentah):
                sunyi.append(_rms(mentah))
        habis = time.time() + detik
        while time.time() < habis:
            data, _ = stream.read(blok_sumber)
            bersih, mentah, _vad = depan.proses(data)
            if len(bersih):
                blok.append(bersih.copy())
                tingkat.append(_rms(mentah))

    derau, ramai, ambang, ambang_lanjut = hitung_ambang(sunyi, jang)
    urut = sorted(tingkat)
    # Puncak dipakai untuk "apakah SEMPAT lewat", p90 untuk "apakah lewat
    # DENGAN ENAK". Satu dentuman yang kebetulan tinggi tak berarti kalimatnya
    # akan tertangkap; p90 mewakili tenaga bicara yang sesungguhnya.
    hasil: dict[str, Any] = {
        "jangkauan": (str(jangkauan).strip().lower() if jangkauan
                      else _nama_jangkauan()),
        "derau": derau, "derau_ramai": ramai,
        "ambang": ambang, "ambang_lanjut": ambang_lanjut,
        "puncak": urut[-1] if urut else 0.0,
        "suara_p90": _persentil(urut, 0.9),
        "teks": "",
    }
    # Jangkauan TERKECIL yang masih menangkap suara dari titik ini. Itulah
    # angka yang benar-benar ingin diketahui pengguna — bukan ambangnya,
    # melainkan setelan mana yang harus dipilih.
    hasil["saran"] = ""
    for nama in ("dekat", "normal", "jauh"):
        _, _, amb, _ = hitung_ambang(sunyi, JANGKAUAN[nama])
        if hasil["suara_p90"] > amb:
            hasil["saran"] = nama
            break
    if blok:
        try:
            hasil["teks"], _info = pengenal().kenali(
                np.concatenate(blok).tobytes())
        except Exception:  # noqa: BLE001
            pass
    return hasil


def _main(argv: list[str] | None = None) -> int:
    """Perintah internal installer: unduh dan verifikasi Whisper lokal."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--prepare-model"]:
        print(f"Menyiapkan Whisper {_nama_model_whisper()} lokal…", flush=True)
        ok, pesan = pastikan_model()
        print(("[ok] " if ok else "[gagal] ") + pesan, flush=True)
        return 0 if ok else 1
    print("Modul audio internal bagas-ai. Gunakan /voice dari aplikasi.")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
