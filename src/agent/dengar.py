"""/voice — bagas-ai mendengarkan mikrofon dan menunggu namanya disebut.

CARA PAKAI (dari sisi pengguna):

    /voice on          mikrofon menyala, bagas-ai mendengarkan
    "bagas ai ..."     namanya disebut -> ucapan berikutnya dianggap perintah
    "... lakukan"      kata penutup -> perintah dikirim ke kotak terminal
    "... batalkan"     buang rekaman yang sedang berjalan
    /voice off         mikrofon mati (ini keadaan bawaannya)

Kata penutupnya lentur: "lakukan", "laksanakan", "kerjakan", "lakuin",
"eksekusi" — sama saja. Begitu pula pembatalnya: "batalkan",
"batal", "batalin". Orang tak mengucapkan kata yang persis sama tiap kali, dan
aba-aba yang menuntut hafalan satu kata justru bikin perintah gagal berangkat.

Satu perintah boleh sepanjang 30 detik sejak namanya disebut (MAKS_REKAM).
Lewat itu ia dibatalkan, bukan dikirim setengah jadi.

KENAPA HARUS ADA KATA PEMICU DAN KATA PENUTUP
---------------------------------------------
Mikrofon yang hidup terus mendengar SEMUA yang terucap di ruangan — obrolan
dengan orang lain, telepon, suara video. Tanpa kata pemicu, semua itu jadi
perintah. Dan tanpa kata penutup, bagas-ai harus MENEBAK kapan sebuah kalimat
selesai; tebakan yang salah memotong perintah di tengah ("tolong hapus" —
padahal lanjutannya "…berkas sementara saja"). Dua kata itu memindahkan
keputusan mulai & selesai ke pengguna, dan itu satu-satunya tempat yang benar.

APA YANG DIKIRIM
----------------
Hanya yang berada DI ANTARA keduanya. "bagas ai tolong buka main.py
lakukan" menjadi perintah "tolong buka main.py" — kata pemicu & penutupnya dibuang,
sebab keduanya ditujukan ke programnya, bukan ke AI.

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
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# --- kata pemicu & penutup -------------------------------------------------
# Ditulis sebagai POLA PER-KATA, bukan potongan teks: pengenal suara memisah
# "bagasai" jadi dua kata dan sesekali salah dengar konsonan awalnya.
# KATA PEMICU: NAMANYA SENDIRI. "on" sempat dipakai lalu dikembalikan ke sini
# atas permintaan pengguna — dan memang itu pilihan yang lebih sehat: aba-aba
# dua huruf gampang salah dengar DAN gampang terpicu tanpa sengaja, sementara
# "bagas ai" khas dan hampir mustahil muncul kebetulan.
_NAMA_KEDUA = {"ai", "hai", "ay", "a", "i", "eye", "ei"}
_NAMA_RAPAT = {"bagasai", "bagas-ai", "bagasi", "pagasai", "bagasay",
               "bagasih", "bagaskara", "bagasi"}
_NAMA_PERTAMA = {"bagas", "pagas", "bagus", "begas", "bagaz"}
# Yang boleh jadi panggilan TUNGGAL di awal kalimat — lihat cari_nama().
_NAMA_SENDIRI = {"bagas", "bagaz"}
# KATA PENUTUP: "lakukan" beserta kerabatnya. "enter" sempat dipakai lalu
# dikembalikan atas permintaan pengguna. Beberapa bentuk diterima sekaligus
# karena orang tak mengucapkan kata yang persis sama tiap kali — dan aba-aba
# yang menuntut hafalan satu kata justru bikin perintah gagal berangkat.
#
# "jalankan" sempat ikut lalu DIBUANG: ia justru kata yang paling sering muncul
# DI DALAM perintahnya sendiri ("jalankan ujinya", "jalankan npm run build"),
# jadi ia akan memotong kalimat tepat di tengah lalu mengirim separuhnya.
# Ujinya menangkap ini sebelum sempat dipakai. Aturannya: kata penutup harus
# yang JARANG jadi isi perintah — bukan sekadar yang terdengar mirip artinya.
PENUTUP = {"lakukan", "lakuin", "laksanakan", "kerjakan", "eksekusi"}
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


def cari_penutup(kata: list[str]) -> int:
    """Indeks kata penutup pertama, atau -1."""
    for i, k in enumerate(kata):
        if k in PENUTUP:
            return i
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
        j = cari_penutup(kata)
        b = cari_pembatal(kata)
        # Yang DULUAN terucap yang berlaku. "…lakukan, eh batalkan" berarti
        # perintahnya sudah berangkat; "…batalkan saja, jangan lakukan" berarti
        # dibuang. Membaca urutannya adalah satu-satunya cara menghormati
        # maksud pengguna tanpa menebak.
        if b >= 0 and (j < 0 or b < j):
            self.batalkan()
            return BATAL
        if j < 0:
            if kata:
                self.potongan.append(" ".join(kata))
            return None
        # Kata penutup terdengar: yang dikirim hanya yang SEBELUMNYA. Sisa
        # kalimat sesudah "lakukan" sengaja dibuang — di situlah orang biasanya
        # menambahkan komentar untuk dirinya sendiri, bukan untuk AI.
        if j:
            self.potongan.append(" ".join(kata[:j]))
        perintah = self.sementara
        self.batalkan()
        return perintah

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
_LANTAI = 180.0         # ambang terendah; di bawah ini mikrofon dianggap sunyi


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
        import speech_recognition  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, ("paket `SpeechRecognition` belum ada. Pasang dengan:\n"
                       "    pip install SpeechRecognition")
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
        d = sd.query_devices(kind="input")
        return str(d.get("name", "")).strip()
    except Exception:  # noqa: BLE001
        return ""


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


class Pendengar:
    """Mendengarkan mikrofon di THREAD LATAR sampai dihentikan.

    Alurnya: potong aliran mikrofon jadi UCAPAN (dipisah oleh sunyi), kirim tiap
    ucapan ke pengenal suara, lalu umpankan teksnya ke Perakit. Pengenalannya
    dikerjakan thread TERSENDIRI supaya perekaman tak pernah berhenti selagi
    menunggu jawaban jaringan — kalau tidak, kalimat berikutnya terpotong tepat
    saat pengguna masih bicara."""

    def __init__(self, on_perintah: Callable[[str], None],
                 on_kabar: Callable[[str], None] | None = None,
                 on_dengar: Callable[[str, bool], None] | None = None,
                 maks_rekam: float = MAKS_REKAM) -> None:
        self.on_perintah = on_perintah
        self.on_kabar = on_kabar or (lambda _m: None)
        # on_dengar(teks, sedang_merekam) -> ditampilkan sebagai "yang terdengar"
        self.on_dengar = on_dengar or (lambda _t, _m: None)
        self.perakit = Perakit(maks_rekam)
        self._stop = threading.Event()
        # Sampai kapan blok audio DIBUANG (selagi ketukan penanda berbunyi).
        # Tanpa ini, bunyi bagas-ai sendiri ikut terekam lalu dikirim ke
        # pengenal suara — dan yang lebih buruk, ia menahan pemotong ucapan
        # supaya tak pernah melihat sunyi.
        self._abaikan_sampai = 0.0
        self._antre: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self.galat = ""

    @property
    def aktif(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    @property
    def merekam(self) -> bool:
        """True bila namaku sudah disebut & perintahnya sedang dikumpulkan.

        Dibaca bar status terminal: selama ini menyala, apa pun yang terucap
        ikut jadi perintah — keadaan yang harus terbaca sekali lihat."""
        return self.perakit.merekam

    def mulai(self) -> str:
        """Nyalakan mikrofon. Return "" bila berhasil, atau alasan gagalnya."""
        if self.aktif:
            return ""
        ok, alasan = siap()
        if not ok:
            return alasan
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

    # --- thread 1: mikrofon -> potongan ucapan ---
    def _rekam(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self._gagal(f"paket audio tak bisa dimuat: {exc}")
            return
        try:
            with sd.InputStream(samplerate=LAJU, channels=1, dtype="int16",
                                blocksize=BLOK) as stream:
                self._gelung(stream, np)
        except Exception as exc:  # noqa: BLE001 - mikrofon dipakai aplikasi lain, dsb
            self._gagal(f"mikrofon tak bisa dibuka: {exc}")

    def _gelung(self, stream: Any, np: Any) -> None:
        # Derau ruangan diukur DULU. Ambang tetap yang dipatok di kode selalu
        # salah di salah satu sisi: di ruangan sunyi ia melewatkan bisikan, di
        # ruangan berkipas ia menganggap kipasnya bicara sepanjang waktu.
        contoh: list[float] = []
        habis = time.time() + _KALIBRASI
        while time.time() < habis and not self._stop.is_set():
            data, _ = stream.read(BLOK)
            contoh.append(_rms(data))
        derau = sorted(contoh)[len(contoh) // 2] if contoh else 0.0
        ambang = max(derau * 3.5, _LANTAI)
        log.debug("derau ruangan %.0f -> ambang %.0f", derau, ambang)

        potongan: list[Any] = []
        # Sedikit rekaman SEBELUM ambang terlampaui ikut disimpan: suku kata
        # pertama selalu lebih pelan dari sisanya, dan tanpa ini "bagas" kerap
        # sampai sebagai "gas".
        awalan: list[Any] = []
        sunyi = 0.0
        mulai = 0.0
        tik = 0.0
        while not self._stop.is_set():
            try:
                data, _ = stream.read(BLOK)
            except Exception as exc:  # noqa: BLE001
                self._gagal(f"aliran mikrofon terputus: {exc}")
                return
            # Ketukan penanda "sedang merekam". Dibunyikan HANYA di sela
            # ucapan (potongan kosong): kalau disisipkan di tengah kalimat, ia
            # ikut terekam dan merusak pengenalannya.
            if self.perakit.merekam and not potongan:
                if time.time() - tik >= _JEDA_TIK:
                    tik = time.time()
                    self._abaikan_sampai = tik + _NADA_TIK[0][1] / 1000 + 0.25
                    threading.Thread(target=bunyi, args=("tik",),
                                     daemon=True).start()
            elif not self.perakit.merekam:
                tik = 0.0
            if time.time() < self._abaikan_sampai:
                continue          # bunyi sendiri — jangan direkam
            keras = _rms(data) > ambang
            lama_blok = BLOK / LAJU
            if not potongan:
                awalan.append(data.copy())
                if len(awalan) > 5:
                    awalan.pop(0)
                if keras:
                    potongan = awalan + [data.copy()]
                    awalan = []
                    sunyi, mulai = 0.0, time.time()
                continue
            potongan.append(data.copy())
            sunyi = 0.0 if keras else sunyi + lama_blok
            panjang = time.time() - mulai
            if sunyi >= _DIAM_SELESAI or panjang >= _MAKS_UCAPAN:
                if panjang - sunyi >= _MIN_UCAPAN:
                    self._antre.put(np.concatenate(potongan).tobytes())
                potongan = []
                sunyi = 0.0
            # Perintah yang menggantung tanpa kata penutup dibatalkan di sini —
            # bukan di thread pengenal, yang bisa saja sedang menunggu jaringan.
            if self.perakit.kedaluwarsa():
                self.on_kabar(
                    f"perintah suara dibatalkan — {MAKS_REKAM:.0f} detik "
                    "habis dan kata `lakukan` tak terdengar")

    # --- thread 2: potongan ucapan -> teks -> perintah ---
    def _kenali(self) -> None:
        try:
            import speech_recognition as sr
        except Exception as exc:  # noqa: BLE001
            self._gagal(f"pengenal suara tak bisa dimuat: {exc}")
            return
        rec = sr.Recognizer()
        while not self._stop.is_set():
            data = self._antre.get()
            if data is None:
                return
            try:
                teks = rec.recognize_google(sr.AudioData(data, LAJU, 2),
                                            language="id-ID")
            except sr.UnknownValueError:
                # Diteruskan lewat on_dengar, TIDAK dicetak sendiri. Terminal
                # sengaja tak menampilkan transkrip apa pun di atas kotak chat
                # (permintaan pengguna: itu barisan debug, bukan untuk dipakai
                # sehari-hari) — jalurnya tetap ada untuk penelusuran.
                self.on_dengar("(tertangkap, tapi tak terdengar jelas)",
                               self.perakit.merekam)
                continue
            except Exception as exc:  # noqa: BLE001 - jaringan/layanan
                self.on_kabar(f"pengenalan suara gagal: {exc}")
                continue
            if not teks.strip():
                continue
            self.on_dengar(teks, self.perakit.merekam)
            sedang = self.perakit.merekam
            hasil = self.perakit.dengar(teks)
            if not sedang and self.perakit.merekam:
                # Namaku baru saja terdeteksi. Ketukan ini yang menjawab
                # pertanyaan "tadi kedengeran nggak sih?" — tanpa itu pengguna
                # baru tahu jawabannya setelah selesai bicara panjang lebar.
                threading.Thread(target=bunyi, args=("mulai",),
                                 daemon=True).start()
            if hasil is BATAL:
                # Bunyinya MENURUN, kebalikan dari nada mulai — supaya
                # "dibatalkan" dan "dikirim" tak pernah tertukar walau layarnya
                # tak dilihat.
                threading.Thread(target=bunyi, args=("batal",),
                                 daemon=True).start()
                self.on_kabar("rekaman DIBATALKAN, tak ada yang dikirim — "
                              "sebut namaku lagi untuk perintah baru")
                continue
            if hasil is None:
                # "lakukan" terdengar padahal belum merekam: pengguna mengira
                # ia sedang memerintah, bagas-ai menganggapnya obrolan. Dulu
                # keadaan ini SENYAP total — dan itu persis yang membuatnya
                # tampak "tak berbuat apa-apa". (Dilaporkan pengguna.)
                if not sedang and cari_penutup(_kata(teks)) >= 0:
                    self.on_kabar(
                        "terdengar kata penutup, tapi namaku belum disebut — "
                        "awali dengan \"bagas ai\", mis. "
                        "\"bagas ai tolong baca file ini lakukan\"")
                continue
            if not hasil:
                self.on_kabar("kata `lakukan` terdengar, tapi tak ada perintah "
                              "di antaranya — diabaikan")
                continue
            try:
                self.on_perintah(hasil)
            except Exception:  # noqa: BLE001 - UI tak boleh menjatuhkan mikrofon
                log.debug("penerima perintah suara melempar", exc_info=True)

    def _gagal(self, pesan: str) -> None:
        self.galat = pesan
        self._stop.set()
        self.on_kabar(pesan)


def dengar_sekali(detik: float = 5.0) -> tuple[str, float]:
    """Rekam sebentar lalu kenali — dipakai `/voice tes`.

    Mengembalikan (teks, tingkat_suara_tertinggi). Tingkat suaranya ikut
    dikembalikan karena dua kegagalan yang paling sering terjadi TAMPAK SAMA
    dari luar (tak ada teks): mikrofon yang bisu, dan pengenalan yang tak
    paham. Angka itu memisahkan keduanya."""
    import numpy as np
    import sounddevice as sd
    import speech_recognition as sr

    blok: list[Any] = []
    puncak = 0.0
    with sd.InputStream(samplerate=LAJU, channels=1, dtype="int16",
                        blocksize=BLOK) as stream:
        habis = time.time() + detik
        while time.time() < habis:
            data, _ = stream.read(BLOK)
            blok.append(data.copy())
            puncak = max(puncak, _rms(data))
    if not blok:
        return "", 0.0
    data = np.concatenate(blok).tobytes()
    try:
        teks = sr.Recognizer().recognize_google(
            sr.AudioData(data, LAJU, 2), language="id-ID")
    except Exception:  # noqa: BLE001
        teks = ""
    return teks, puncak
