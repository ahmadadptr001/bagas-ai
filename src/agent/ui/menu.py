"""Prompt interaktif bagas-ai: menu pilih, centang, konfirmasi, input teks.

MENGGANTIKAN InquirerPy. Alasannya bukan sekadar selera tampilan:

  - InquirerPy menggambar daftar polos tanpa bingkai, tanpa penomoran, dan
    tanpa pencarian — pada daftar 80 chat web, satu-satunya cara mencapai
    entri terakhir adalah menahan panah bawah.
  - Markup rich TIDAK diproses olehnya, jadi tiap pemanggil harus menulis
    label polos dan mengatur perataan kolom sendiri dengan f-string.
  - Ia menambah satu dependensi lagi di atas prompt_toolkit, padahal
    prompt_toolkit sudah dipakai bagas-ai untuk baris input utamanya.

Modul ini dibangun LANGSUNG di atas prompt_toolkit (dependensi yang memang
sudah ada) dan menyediakan API yang KOMPATIBEL dengan pemakaian lama —
`inquirer.select(...).execute()`, `Choice(value, name)` — sehingga pemanggil
lama cukup mengganti barisan impornya.

Bentuk bingkainya sengaja BERSISI-TIGA (atas, rel kiri, bawah) tanpa garis
tepi kanan. Itu keputusan teknis, bukan estetika: label menu bagas-ai penuh
emoji (🔀 🎚 ✨ 🗑 🌐), dan lebar emoji di terminal TIDAK disepakati seragam —
Windows Terminal, konsol lama, dan terminal Unix bisa berbeda satu kolom untuk
karakter yang sama. Garis tepi kanan menuntut lebar tiap baris tepat, jadi
selisih satu kolom saja membuat sisi kanannya bergerigi. Tanpa tepi kanan,
tampilannya selalu rapi di mana pun.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Iterable, Sequence

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.utils import get_cwidth

from . import tema

# Palet menu mengikuti TEMA AKTIF (ui/tema.py) supaya prompt ini menyatu
# dengan panel & footer di sekitarnya — berganti /theme, menu ikut berganti.
#
# Berbentuk FUNGSI (dibaca tiap render), bukan konstanta: konstanta terikat
# sekali saat impor, sehingga pergantian tema di tengah sesi tak pernah
# sampai ke sini. HIJAU/MERAH tetap konstanta — warna SEMANTIK (sukses/
# bahaya), bukan bagian identitas tema.


def EMAS() -> str:
    return "fg:" + tema.p("aksen")


def ORANYE() -> str:
    return "fg:" + tema.p("aksen2")


def KUNING() -> str:
    return "fg:" + tema.p("aksen_terang")


def REDUP() -> str:
    return "fg:" + tema.p("redup")


def TEKS() -> str:
    return "fg:" + tema.p("teks")


HIJAU = "fg:#9fc93c"
MERAH = "fg:#f0603c"

# Sebanyak-banyaknya baris pilihan yang tampil sekaligus; sisanya digulung.
_MAKS_TAMPIL = 9
# Daftar sepanjang ini ke atas mendapat kotak pencarian (ketik untuk menyaring).
_AMBANG_CARI = 8


class Choice:
    """Satu pilihan: `value` yang dikembalikan, `name` yang ditampilkan.

    Bentuknya sengaja sama dengan InquirerPy.base.control.Choice supaya
    pemanggil lama tak perlu diubah sama sekali.

    `nonaktif` = entri yang TAMPIL tapi tak bisa dipilih. Dipakai untuk pilihan
    yang sengaja ditunda: menghapusnya dari daftar membuat pengguna mengira
    fiturnya hilang, sedangkan membiarkannya bisa dipilih membuat ia menabrak
    penolakan sesudah menekan Enter. Yang benar: kelihatan, jelas alasannya,
    dan kursor melewatinya begitu saja."""

    __slots__ = ("value", "name", "enabled", "nonaktif")

    def __init__(self, value: Any, name: str | None = None,
                 enabled: bool = False, nonaktif: bool = False) -> None:
        self.value = value
        self.name = str(value) if name is None else str(name)
        self.enabled = bool(enabled)
        self.nonaktif = bool(nonaktif)

    def __repr__(self) -> str:  # pragma: no cover - bantu debug saja
        return f"Choice({self.value!r}, {self.name!r})"


def _sebagai_choices(items: Iterable[Any]) -> list[Choice]:
    """Terima daftar Choice ATAU string polos (dipakai handler ask_user)."""
    out: list[Choice] = []
    for it in items or []:
        out.append(it if isinstance(it, Choice) else Choice(it, str(it)))
    return out


def _lebar_terminal() -> int:
    """Lebar terminal LANGSUNG dari sumbernya, bukan var lingkungan.

    shutil.get_terminal_size mengutamakan COLUMNS/LINES warisan shell; nilai
    warisan itu bisa basi (terminal sudah diubah ukurannya, environ belum)
    sehingga menu yang sedang tampil digambar dengan lebar lama sampai
    ditutup. Tanyakan ke output prompt_toolkit yang sedang menggambar dulu,
    lalu ke terminal lewat os.get_terminal_size — keduanya selalu segar.

    Yang dikembalikan JUMLAH KOLOM TERMINAL apa adanya, di semua jalur. Dulu
    jalur prompt_toolkit memotong satu kolom sendiri sementara jalur
    os/shutil tidak, jadi terminal yang sama dilaporkan beda satu kolom
    tergantung ada-tidaknya app yang sedang berjalan — selisih sekecil itu
    sudah cukup menggeser tepi kotak. Urusan "kolom terakhir tak boleh
    dilukis" sekarang dipusatkan di _lebar_kotak, satu tempat saja."""
    try:
        from prompt_toolkit.application.current import get_app_or_none
        app = get_app_or_none()
        if app is not None and app.is_running:
            kolom = app.output.get_size().columns
            if kolom > 1:
                return kolom
    except Exception:  # noqa: BLE001
        pass
    import os
    for f in (sys.__stdout__, sys.stdout, sys.__stderr__):
        try:
            kolom = os.get_terminal_size(f.fileno()).columns
            if kolom > 0:
                return kolom
        except Exception:  # noqa: BLE001
            continue
    try:
        import shutil
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:  # noqa: BLE001
        return 80


def _lebar_kotak() -> int:
    """Lebar tergambar tiap kotak menu = lebar terminal dikurangi SATU kolom.

    Kembaran persis _lebar_kotak di interfaces/cli.py, dan itu justru
    maksudnya: kotak chat + bar status milik CLI dipaku di kaki tiap menu
    (lihat _jalankan), jadi kotak menu, kotak chat, dan bar status WAJIB
    berbagi satu tepi kanan. Beda sedikit saja langsung terbaca mata sebagai
    kotak kepotong yang menggantung di atas dua baris yang penuh sampai tepi.

    Kenapa W-1 dan bukan W: penggambar prompt_toolkit tak pernah melukis kolom
    terakhir terminal (supaya tak kena auto-wrap), jadi W-1 adalah lebar yang
    BENAR-BENAR habis tergambar — termasuk rel kanan dan sudut-sudutnya.

    Tak ada lagi batas atas (dulu konstanta 96 kolom) maupun potongan dua
    kolom. Batas itu masuk akal ketika kotak menu masih berdiri sendiri di
    tengah layar, tapi sejak kaki CLI menempel di bawahnya ia berubah jadi
    sumber kerusakan: di terminal 133 kolom, kotaknya berhenti 36 kolom
    sebelum tepi kanan sementara kotak chat tepat di bawahnya penuh."""
    # Selama ada kaki, lebarnya DIUKUR dari kaki itu — tidak dihitung ulang.
    # Alasannya: kaki digambar rich SEBELUM app menu berjalan, kotak menu
    # digambar prompt_toolkit SESUDAHNYA, dan kedua penggambar itu tak selalu
    # sepakat soal jumlah kolom (rich memotong satu kolom sendiri di konsol
    # warisan Windows). Menghitung ulang berarti bertaruh keduanya sepakat;
    # mengukur berarti kotak menu memakai angka yang SAMA dengan yang sudah
    # dipakai kaki, jadi tepi kanannya sejajar dengan sendirinya.
    kaki = kaki_aktif           # diisi cli.py — lihat catatan di kaki_aktif
    if kaki:
        try:
            teks = "".join(t for _, t in kaki)
            lebar = max(get_cwidth(b) for b in teks.split("\n"))
            # Waras dulu: kaki yang kosong/rusak jangan sampai mengecilkan
            # kotak jadi tak terpakai atau melebarkannya keluar terminal.
            if 20 <= lebar <= _lebar_terminal():
                return lebar
        except Exception:  # noqa: BLE001
            pass
    return max(20, _lebar_terminal() - 1)


def _potong(teks: str, maks: int) -> str:
    """Potong teks berdasarkan LEBAR TAMPIL (emoji dihitung 2 kolom)."""
    if maks <= 1:
        return "…" if maks == 1 else ""
    if get_cwidth(teks) <= maks:
        return teks
    out = ""
    lebar = 0
    for ch in teks:
        w = get_cwidth(ch)
        if lebar + w > maks - 1:
            break
        out += ch
        lebar += w
    return out + "…"


# Anggap terminal selalu siap, walau stdin/stdout bukan TTY. Dipakai PENGUJIAN,
# yang menyuapkan tombol lewat pipa prompt_toolkit (create_pipe_input) — tanpa
# ini, uji apa pun akan mendarat di jalur cadangan dan penanganan tombolnya
# tak pernah benar-benar teruji.
paksa_interaktif = False

# "KAKI" menu: baris-baris statis (kotak chat + bar status milik CLI) yang
# dipasang DI BAGIAN BAWAH layout menu sendiri. Tanpa ini, app inline
# prompt_toolkit mengisi ruang hingga dasar layar dan MENIMPA kotak chat —
# menu terbuka, kotaknya lenyap. Dengan kaki, tumpukan bawah tetap tampak
# selama menu apa pun terbuka; cli.py yang mengisinya lewat kaki_aktif
# (None = menu berdiri sendiri, seperti di luar sesi chat).
kaki_aktif: list[tuple[str, str]] | None = None


def _interaktif() -> bool:
    """True bila terminalnya benar-benar bisa dipakai menggambar prompt."""
    if paksa_interaktif:
        return True
    try:
        return bool(sys.stdin) and sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- kerangka
def _bungkus_teks(teks: str, lebar: int) -> list[str]:
    """Pecah teks jadi beberapa baris menurut LEBAR TAMPIL (bukan jumlah huruf)."""
    if lebar <= 0:
        lebar = 20
    baris: list[str] = []
    for asli in teks.splitlines() or [""]:
        kini, w = "", 0
        # split() tanpa argumen: spasi ganda tak menghasilkan "kata kosong"
        # yang akan muncul sebagai spasi menggantung di ujung baris.
        for kata in asli.split():
            kw = get_cwidth(kata)
            if kw > lebar:
                if kini:
                    baris.append(kini)
                    kini, w = "", 0
                potongan = ""
                pw = 0
                for ch in kata:
                    cw = get_cwidth(ch)
                    if pw + cw > lebar:
                        baris.append(potongan)
                        potongan, pw = ch, cw
                    else:
                        potongan += ch
                        pw += cw
                if potongan:
                    kini, w = potongan, pw
                continue
            if kini and w + 1 + kw > lebar:
                baris.append(kini)
                kini, w = kata, kw
            else:
                kini = f"{kini} {kata}" if kini else kata
                w += (1 + kw) if kini != kata else kw
        if kini:
            baris.append(kini)
    return baris or [""]


def _kotak(judul: str, isi: list[list[tuple[str, str]]], footer: str,
           warna: str = "") -> FormattedText:
    """Bungkus baris-baris isi dengan bingkai bersisi tiga (lihat docstring).

    Judul PANJANG — mis. permintaan izin yang memuat path lengkap — tidak
    dipaksa masuk ke garis atas: ia pindah ke dalam kotak dan dibungkus jadi
    beberapa baris. Kalau dipaksa di garis atas, judulnya akan melewati lebar
    terminal lalu dilipat sendiri oleh terminal, dan bingkainya berantakan."""
    warna = warna or EMAS()
    lebar = _lebar_kotak()
    frag: list[tuple[str, str]] = []

    def baris(bagian: list[tuple[str, str]]) -> None:
        frag.extend(bagian)
        frag.append(("", "\n"))

    judul_di_dalam = judul and get_cwidth(judul) > lebar - 8
    kepala = "─" if judul_di_dalam or not judul else f" {judul} "
    sisa = max(2, lebar - get_cwidth(kepala) - 2)
    baris([(warna, "╭─"), (f"{warna} bold", kepala), (warna, "─" * sisa)])
    baris([(warna, "│")])
    if judul_di_dalam:
        for t in _bungkus_teks(judul, lebar - 5):
            baris([(warna, "│  "), (f"{TEKS()} bold", t)])
        baris([(warna, "│")])
    for isi_baris in isi:
        baris([(warna, "│  ")] + isi_baris)
    if footer:
        baris([(warna, "│")])
        baris([(warna, "│  "), (REDUP(), _potong(footer, lebar - 4))])
    frag.extend([(warna, "╰" + "─" * (lebar - 1))])
    return FormattedText(frag)


def _jalankan(bangun: Callable[[], FormattedText], kb: KeyBindings,
              kaki: list[tuple[str, str]] | None = None) -> Any:
    """Jalankan satu Application inline (tidak layar-penuh) sampai app.exit().

    `kaki` = baris statis yang dipaku di DASAR layout (kotak chat + bar
    status milik CLI). Aplikasi inline mengisi ruang sampai dasar layar;
    tanpa penopang, baris-baris itu ditimpa spasi kosong — kotak chat
    lenyap selama menu terbuka. Pendorong di atas kaki menjaga kaki tetap
    menempel dasar berapa pun tinggi menu/terminalnya."""
    if kaki is None:
        kaki = kaki_aktif
    lapisan: list = [Window(FormattedTextControl(bangun, focusable=True,
                                                 show_cursor=False),
                            dont_extend_height=True)]
    if kaki:
        lapisan.append(Window())          # pendorong: kaki selalu di dasar
        lapisan.append(Window(
            FormattedTextControl(lambda: kaki),
            dont_extend_height=True, wrap_lines=False))
    app: Application = Application(
        layout=Layout(HSplit(lapisan)),
        key_bindings=kb,
        full_screen=False,
        # Prompt-nya HILANG sesudah dijawab, lalu diganti satu baris ringkasan —
        # kalau tidak, riwayat terminal penuh kotak-kotak menu lama.
        erase_when_done=True,
        mouse_support=False,
    )
    return app.run()


def _ringkas(judul: str, jawaban: str, warna: str = HIJAU) -> None:
    """Satu baris jejak sesudah prompt hilang: apa yang ditanya & dipilih."""
    if not judul and not jawaban:
        return
    print_formatted_text(FormattedText([
        (warna, "  ✓ "),
        (REDUP(), _potong(judul, 46) + ("  ·  " if jawaban else "")),
        (TEKS(), _potong(jawaban, 60)),
    ]))


def _batal(judul: str) -> None:
    print_formatted_text(FormattedText([
        (KUNING(), "  ◼ "), (REDUP(), _potong(judul, 46) + "  ·  dibatalkan")]))


# ------------------------------------------------------------------ select
def select(message: str = "", choices: Sequence[Any] = (), default: Any = None,
           hint: str = "", warna: str = "", ringkas: bool = True,
           **_lain: Any) -> Any:
    """Menu pilih-satu. Kembalikan `value` pilihan; batal -> KeyboardInterrupt."""
    warna = warna or EMAS()
    opsi = _sebagai_choices(choices)
    if not opsi:
        raise ValueError("select butuh minimal satu pilihan")
    if not _interaktif():
        return _pilih_polos(message, opsi, default)

    # Kursor tak boleh MULAI di entri yang tak bisa dipilih — kalau tidak,
    # Enter pertama tak melakukan apa-apa dan menunya tampak beku.
    idx = next((i for i, c in enumerate(opsi) if not c.nonaktif), 0)
    for i, c in enumerate(opsi):
        if default is not None and c.value == default and not c.nonaktif:
            idx = i
            break
    boleh_cari = len(opsi) > _AMBANG_CARI
    keadaan = {"idx": idx, "kueri": ""}

    def tersaring() -> list[int]:
        """Entri yang TAMPAK (termasuk yang nonaktif — ia memang harus terlihat)."""
        q = keadaan["kueri"].lower()
        if not q:
            return list(range(len(opsi)))
        return [i for i, c in enumerate(opsi) if q in c.name.lower()]

    def bisa_dipilih() -> list[int]:
        """Entri yang boleh dihinggapi kursor: tampak DAN tidak nonaktif."""
        return [i for i in tersaring() if not opsi[i].nonaktif]

    def selaraskan() -> None:
        """Pastikan kursor menunjuk entri yang MASIH lolos saringan.

        Dipanggil dari penangan tombol, bukan dari fungsi penggambar. Kalau
        hanya diperbaiki saat menggambar, deretan tombol yang datang tanpa jeda
        — ketikan cepat, teks yang ditempel, atau masukan terprogram — bisa
        sampai ke Enter sebelum satu pun gambar-ulang terjadi, sehingga yang
        terpilih adalah entri pertama daftar ASLI, bukan hasil pencarian."""
        boleh = bisa_dipilih()
        if boleh and keadaan["idx"] not in boleh:
            keadaan["idx"] = boleh[0]

    def bangun() -> FormattedText:
        lihat = tersaring()
        if not lihat:
            isi = [[(KUNING(), "tak ada yang cocok dengan "),
                    (f"{KUNING()} bold", repr(keadaan["kueri"]))]]
            return _kotak(message, isi, "ketik untuk ubah pencarian · esc batal",
                          warna)
        selaraskan()
        pos = lihat.index(keadaan["idx"])
        awal = 0 if len(lihat) <= _MAKS_TAMPIL else max(
            0, min(pos - _MAKS_TAMPIL // 2, len(lihat) - _MAKS_TAMPIL))
        akhir = min(len(lihat), awal + _MAKS_TAMPIL)
        lebar = _lebar_kotak() - 8

        isi: list[list[tuple[str, str]]] = []
        if awal > 0:
            isi.append([(REDUP(), f"  ↑ {awal} lainnya di atas")])
        for urut in range(awal, akhir):
            i = lihat[urut]
            c = opsi[i]
            dipilih = i == keadaan["idx"]
            nomor = "" if boleh_cari else f"{urut + 1}. "
            if c.nonaktif:
                # Tampil REDUP() seluruhnya & tanpa penunjuk: sekali lihat sudah
                # jelas ia ada tapi bukan pilihan.
                isi.append([(REDUP(), "  " + nomor
                             + _potong(c.name, lebar - len(nomor)))])
                continue
            isi.append([
                (f"{warna} bold" if dipilih else "", "❯ " if dipilih else "  "),
                (REDUP() if not dipilih else f"{warna} bold", nomor),
                (f"{warna} bold" if dipilih else TEKS(),
                 _potong(c.name, lebar - len(nomor))),
            ])
        if akhir < len(lihat):
            isi.append([(REDUP(), f"  ↓ {len(lihat) - akhir} lainnya di bawah")])

        petunjuk = hint or "↑↓ pilih  ·  ⏎ konfirmasi  ·  esc batal"
        if boleh_cari:
            petunjuk = ("ketik untuk mencari  ·  " + petunjuk)
            if keadaan["kueri"]:
                petunjuk = f"cari: {keadaan['kueri']}   ({len(lihat)} cocok)  ·  " \
                           + petunjuk
        else:
            petunjuk = "1-9 langsung  ·  " + petunjuk
        return _kotak(message, isi, petunjuk, warna)

    kb = KeyBindings()

    def geser(langkah: int) -> None:
        # Bergerak di antara yang BISA DIPILIH saja: entri nonaktif dilewati,
        # jadi menahan ↓ tak pernah menyangkut di baris yang menolak Enter.
        boleh = bisa_dipilih()
        if not boleh:
            return
        pos = boleh.index(keadaan["idx"]) if keadaan["idx"] in boleh else 0
        keadaan["idx"] = boleh[(pos + langkah) % len(boleh)]

    @kb.add("up")
    @kb.add("c-p")
    def _(_e): geser(-1)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("tab")
    def _(_e): geser(1)

    @kb.add("pageup")
    def _(_e): geser(-_MAKS_TAMPIL)

    @kb.add("pagedown")
    def _(_e): geser(_MAKS_TAMPIL)

    @kb.add("home")
    def _(_e):
        boleh = bisa_dipilih()
        if boleh:
            keadaan["idx"] = boleh[0]

    @kb.add("end")
    def _(_e):
        boleh = bisa_dipilih()
        if boleh:
            keadaan["idx"] = boleh[-1]

    @kb.add("enter")
    def _(e):
        if keadaan["idx"] in bisa_dipilih():
            e.app.exit(result=opsi[keadaan["idx"]])
        # tak ada yang cocok / entri nonaktif -> jangan keluar dengan hasil kosong

    @kb.add("escape")
    @kb.add("c-c")
    def _(e): e.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @kb.add("backspace")
    def _(_e):
        if boleh_cari:
            keadaan["kueri"] = keadaan["kueri"][:-1]
            selaraskan()

    @kb.add(Keys.Any)
    def _(e):
        ch = e.data
        if not ch or len(ch) != 1 or ch < " ":
            return
        if boleh_cari:
            keadaan["kueri"] += ch
            selaraskan()
            return
        if ch.isdigit() and ch != "0":
            lihat = tersaring()
            n = int(ch) - 1
            # Nomornya mengikuti yang TAMPIL (termasuk entri nonaktif), supaya
            # angka di layar dan angka yang diketik selalu sama. Yang nonaktif
            # cuma tak jadi dipilih.
            if n < len(lihat) and not opsi[lihat[n]].nonaktif:
                keadaan["idx"] = lihat[n]
                e.app.exit(result=opsi[keadaan["idx"]])

    try:
        pilihan: Choice = _jalankan(bangun, kb)
    except KeyboardInterrupt:
        _batal(message)
        raise
    if ringkas:
        _ringkas(message, pilihan.name)
    return pilihan.value


# ---------------------------------------------------------------- checkbox
def checkbox(message: str = "", choices: Sequence[Any] = (), hint: str = "",
             warna: str = "", ringkas: bool = True, **_lain: Any) -> list[Any]:
    """Menu pilih-banyak. Kembalikan daftar `value` yang dicentang."""
    warna = warna or ORANYE()
    opsi = _sebagai_choices(choices)
    if not opsi:
        return []
    if not _interaktif():
        return []

    ditandai = {i for i, c in enumerate(opsi) if c.enabled}
    keadaan = {"idx": 0, "kueri": ""}
    boleh_cari = len(opsi) > _AMBANG_CARI

    def tersaring() -> list[int]:
        q = keadaan["kueri"].lower()
        if not q:
            return list(range(len(opsi)))
        return [i for i, c in enumerate(opsi) if q in c.name.lower()]

    def selaraskan() -> None:
        """Jaga kursor tetap di entri yang lolos saringan — lihat select()."""
        lihat = tersaring()
        if lihat and keadaan["idx"] not in lihat:
            keadaan["idx"] = lihat[0]

    def bangun() -> FormattedText:
        lihat = tersaring()
        if not lihat:
            return _kotak(message, [[(KUNING(), "tak ada yang cocok")]],
                          "ketik untuk ubah pencarian · esc batal", warna)
        selaraskan()
        pos = lihat.index(keadaan["idx"])
        awal = 0 if len(lihat) <= _MAKS_TAMPIL else max(
            0, min(pos - _MAKS_TAMPIL // 2, len(lihat) - _MAKS_TAMPIL))
        akhir = min(len(lihat), awal + _MAKS_TAMPIL)
        lebar = _lebar_kotak() - 10

        isi: list[list[tuple[str, str]]] = []
        if awal > 0:
            isi.append([(REDUP(), f"  ↑ {awal} lainnya di atas")])
        for urut in range(awal, akhir):
            i = lihat[urut]
            c = opsi[i]
            aktif = i == keadaan["idx"]
            isi.append([
                (f"{warna} bold" if aktif else "", "❯ " if aktif else "  "),
                (HIJAU if i in ditandai else REDUP(), "◉ " if i in ditandai else "○ "),
                (f"{warna} bold" if aktif else TEKS(), _potong(c.name, lebar)),
            ])
        if akhir < len(lihat):
            isi.append([(REDUP(), f"  ↓ {len(lihat) - akhir} lainnya di bawah")])

        petunjuk = hint or ("spasi tandai  ·  a semua  ·  ⏎ konfirmasi  ·  "
                            "esc batal")
        petunjuk = f"{len(ditandai)} dipilih  ·  " + petunjuk
        if boleh_cari and keadaan["kueri"]:
            petunjuk = f"cari: {keadaan['kueri']}  ·  " + petunjuk
        return _kotak(message, isi, petunjuk, warna)

    kb = KeyBindings()

    def geser(langkah: int) -> None:
        lihat = tersaring()
        if not lihat:
            return
        pos = lihat.index(keadaan["idx"]) if keadaan["idx"] in lihat else 0
        keadaan["idx"] = lihat[(pos + langkah) % len(lihat)]

    @kb.add("up")
    @kb.add("c-p")
    def _(_e): geser(-1)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("tab")
    def _(_e): geser(1)

    @kb.add("space")
    def _(_e):
        i = keadaan["idx"]
        if i in ditandai:
            ditandai.discard(i)
        else:
            ditandai.add(i)

    @kb.add("enter")
    def _(e): e.app.exit(result=sorted(ditandai))

    @kb.add("escape")
    @kb.add("c-c")
    def _(e): e.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @kb.add("backspace")
    def _(_e):
        if boleh_cari:
            keadaan["kueri"] = keadaan["kueri"][:-1]
            selaraskan()

    @kb.add(Keys.Any)
    def _(e):
        ch = e.data
        if not ch or len(ch) != 1 or ch < " ":
            return
        if ch == "a" and not boleh_cari:
            lihat = set(tersaring())
            if lihat <= ditandai:
                ditandai.difference_update(lihat)
            else:
                ditandai.update(lihat)
            return
        if boleh_cari:
            keadaan["kueri"] += ch
            selaraskan()

    try:
        terpilih: list[int] = _jalankan(bangun, kb)
    except KeyboardInterrupt:
        _batal(message)
        raise
    nilai = [opsi[i].value for i in terpilih]
    if ringkas:
        _ringkas(message, f"{len(nilai)} dipilih" if nilai else "tak ada")
    return nilai


# ----------------------------------------------------------------- confirm
def confirm(message: str = "", default: bool = True, warna: str = "",
            ringkas: bool = True, **_lain: Any) -> bool:
    """Konfirmasi Ya/Tidak mendatar. Batal (esc) dianggap TIDAK.

    `warna` HARUS string kosong secara bawaan — BUKAN `KUNING` begitu saja.
    Dulu tertulis `warna: str = KUNING`: yang terikat ke parameter adalah
    OBJEK FUNGSI-NYA (default dievaluasi sekali saat def), sehingga fragmen
    FormattedText bergaya callable dan prompt_toolkit meledak saat menggambar
    ("argument of type 'function' is not iterable") — kotak konfirmasi tak
    pernah muncul di mana pun confirm dipakai. Pola yang benar sama dengan
    select()/checkbox(): bawaan string kosong, dinormalkan DI SINI dengan
    MEMANGGIL fungsi paletnya."""
    warna = warna or KUNING()
    if not _interaktif():
        return default
    keadaan = {"ya": bool(default)}

    def bangun() -> FormattedText:
        ya, tidak = keadaan["ya"], not keadaan["ya"]
        isi = [[
            (f"{HIJAU} bold" if ya else REDUP(), "❯ Ya" if ya else "  Ya"),
            ("", "      "),
            (f"{MERAH} bold" if tidak else REDUP(),
             "❯ Tidak" if tidak else "  Tidak"),
        ]]
        return _kotak(message, isi,
                      "←→ pilih  ·  y/t langsung  ·  ⏎ konfirmasi  ·  esc batal",
                      warna)

    kb = KeyBindings()

    @kb.add("left")
    @kb.add("right")
    @kb.add("tab")
    @kb.add("up")
    @kb.add("down")
    def _(_e): keadaan["ya"] = not keadaan["ya"]

    @kb.add("enter")
    def _(e): e.app.exit(result=keadaan["ya"])

    @kb.add("escape")
    @kb.add("c-c")
    def _(e): e.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @kb.add(Keys.Any)
    def _(e):
        ch = (e.data or "").lower()
        if ch in ("y", "j"):          # ya / iya
            e.app.exit(result=True)
        elif ch in ("t", "n"):        # tidak / no
            e.app.exit(result=False)

    try:
        hasil: bool = _jalankan(bangun, kb)
    except KeyboardInterrupt:
        _batal(message)
        raise
    if ringkas:
        _ringkas(message, "ya" if hasil else "tidak",
                 HIJAU if hasil else KUNING())
    return hasil


# ------------------------------------------------------- input teks & path
def _prompt_teks(message: str, hint: str, *, rahasia: bool = False,
                 default: str = "", completer: Any = None,
                 ringkas: bool = True,
                 kaki: list[tuple[str, str]] | None = None) -> str:
    """Prompt satu-baris berbingkai (dipakai text/secret/filepath).

    `kaki` = tumpukan bawah milik CLI (kotak chat + bar status) supaya ia
    tak tertimpa app inline ini — lihat kaki_aktif."""
    if kaki is None:
        kaki = kaki_aktif
    if not _interaktif():
        import getpass

        try:
            return input(f"{message}: ") if not rahasia else getpass.getpass(f"{message}: ")
        except (EOFError, KeyboardInterrupt):
            return default

    buf = Buffer(completer=completer, complete_while_typing=bool(completer))
    if default:
        buf.text = str(default)

    def _terima(_b: Buffer) -> None:
        get_app().exit(result=_b.text)

    buf.accept_handler = _terima

    lebar = _lebar_kotak()
    # Dulu tertulis `warna = warna or ORANYE()` padahal fungsi ini TAK PUNYA
    # parameter warna — UnboundLocalError di tiap prompt interaktif, yang
    # ditelan pemanggil lalu terdegradasi diam-diam ke getpass polos.
    warna = ORANYE()
    judul_di_dalam = bool(message) and get_cwidth(message) > lebar - 8

    def bagian_atas() -> FormattedText:
        """Tepi atas: bingkai + judul + baris kosong (tempat input menyusul)."""
        frag: list[tuple[str, str]] = []
        kepala = "─" if judul_di_dalam or not message else f" {message} "
        sisa = max(2, lebar - get_cwidth(kepala) - 2)
        frag.append((warna, "╭─"))
        frag.append((f"{warna} bold", kepala))
        frag.append((warna, "─" * sisa))
        frag.append(("", "\n"))
        frag.append((warna, "│"))
        if judul_di_dalam:
            for t in _bungkus_teks(message, lebar - 5):
                frag.append(("", "\n"))
                frag.append((warna, "│  "))
                frag.append((f"{TEKS()} bold", t))
        return FormattedText(frag)

    def bagian_bawah() -> FormattedText:
        """Tepi bawah: rel kosong + petunjuk + bingkai bawah."""
        frag: list[tuple[str, str]] = []
        # Baris pertama LANGSUNG rel "│" (tanpa \n awal): baris kosong di
        # antara baris input dan petunjuk harus tetap memakai rel kiri,
        # persis seperti _kotak — kalau tidak, bingkainya putus di situ.
        frag.append((warna, "│"))
        if hint:
            frag.append(("", "\n"))
            frag.append((warna, "│  "))
            frag.append((REDUP(), _potong(hint, lebar - 4)))
        frag.append(("", "\n"))
        frag.append((warna, "╰" + "─" * (lebar - 1)))
        return FormattedText(frag)

    # "│  ❯ " = rel kiri kotak + penanda input; teks ketikan di sebelahnya.
    # LEBARNYA DIKUNCI: tanpa ini, jendela penanda ikut "fleksibel" di dalam
    # VSplit dan membagi lebar kotak 50:50 dengan jendela buffer — input lalu
    # melompat ke TENGAH kotak, bukan di kiri setelah "❯".
    teks_penanda = [(warna, "│  "), (f"{EMAS()} bold", "❯ ")]
    penanda = FormattedTextControl(lambda: teks_penanda, show_cursor=False)

    # Rahasia: tampilkan ••• alih-alih huruf aslinya (PasswordProcessor),
    # sama seperti is_password=True milik PromptSession dulu.
    prosesor = [PasswordProcessor(char="•")] if rahasia else []
    isi_input = BufferControl(
        buffer=buf, focusable=True, input_processors=prosesor,
        include_default_input_processors=True)

    window_input = Window(isi_input, height=1, wrap_lines=False)

    kb = KeyBindings()

    @kb.add("c-c")
    def _batal_cb(e):
        e.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @kb.add("enter")
    @kb.add("c-j")
    def _kirim(e):
        e.current_buffer.validate_and_handle()

    lapisan: list = [
        Window(FormattedTextControl(bagian_atas), dont_extend_height=True),
        VSplit([
            Window(penanda, dont_extend_height=True, wrap_lines=False,
                   width=get_cwidth("│  ❯ ")),
            window_input,
        ]),
        Window(FormattedTextControl(bagian_bawah), dont_extend_height=True),
    ]
    if kaki:
        lapisan.append(Window())          # pendorong: kaki menempel dasar
        lapisan.append(Window(
            FormattedTextControl(lambda: kaki),
            dont_extend_height=True, wrap_lines=False))
    app: Application = Application(
        layout=Layout(HSplit(lapisan), focused_element=window_input),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )
    try:
        hasil = app.run()
    except KeyboardInterrupt:
        if ringkas:
            _batal(message)
        raise
    if ringkas:
        _ringkas(message, "••••••" if rahasia else (hasil or "(kosong)"))
    return hasil


def text(message: str = "", default: str = "", hint: str = "",
         ringkas: bool = True, **_lain: Any) -> str:
    return _prompt_teks(message, hint or "⏎ kirim  ·  Ctrl+C batal",
                        default=str(default or ""), ringkas=ringkas)


def secret(message: str = "", ringkas: bool = True, **_lain: Any) -> str:
    return _prompt_teks(message, "ketikan disembunyikan  ·  Ctrl+C batal",
                        rahasia=True, ringkas=ringkas)


def filepath(message: str = "", only_directories: bool = False,
             hint: str = "", ringkas: bool = True, **_lain: Any) -> str:
    from prompt_toolkit.completion import PathCompleter

    return _prompt_teks(
        message,
        hint or "Tab autolengkap  ·  ⏎ pilih  ·  Ctrl+C batal",
        completer=PathCompleter(only_directories=bool(only_directories),
                                expanduser=True),
        ringkas=ringkas,
    )


# ------------------------------------------------- cadangan non-interaktif
def _pilih_polos(message: str, opsi: list[Choice], default: Any) -> Any:
    """Terminal tak interaktif (mis. keluaran dialihkan): daftar bernomor biasa.

    Tanpa ini, prompt_toolkit melempar galat yang tak berarti bagi pengguna."""
    print(message)
    for i, c in enumerate(opsi, 1):
        print(f"  {i}. {c.name}" + ("   (tak bisa dipilih)" if c.nonaktif else ""))
    boleh = [c for c in opsi if not c.nonaktif] or list(opsi)
    try:
        jawab = input("Pilih nomor: ").strip()
        pilih = opsi[int(jawab) - 1]
        if pilih.nonaktif:               # entri ditunda -> jatuh ke bawah
            raise ValueError(pilih.name)
        return pilih.value
    except (ValueError, IndexError, EOFError, OSError):
        for c in boleh:
            if c.value == default:
                return c.value
        return boleh[0].value


# ------------------------------------------- lapisan kompatibel InquirerPy
class _Tertunda:
    """Hasil `inquirer.select(...)` — baru berjalan saat .execute() dipanggil.

    Ada semata demi kompatibilitas dengan pemanggil gaya InquirerPy."""

    __slots__ = ("_fn", "_kw")

    def __init__(self, fn: Callable[..., Any], kw: dict) -> None:
        self._fn = fn
        self._kw = kw

    def execute(self) -> Any:
        return self._fn(**self._kw)


class _Inquirer:
    """Sinonim gaya lama: inquirer.select(...).execute().

    `pointer`, `qmark`, `amark`, `instruction`, dan `long_instruction` milik
    InquirerPy tetap diterima supaya pemanggil lama tak perlu disunting; yang
    dipakai hanya instruksinya (jadi teks petunjuk di kaki kotak)."""

    @staticmethod
    def _hint(kw: dict) -> dict:
        petunjuk = kw.pop("long_instruction", "") or kw.pop("instruction", "")
        kw.pop("instruction", None)
        kw.pop("pointer", None)
        kw.pop("qmark", None)
        kw.pop("amark", None)
        if petunjuk:
            kw["hint"] = str(petunjuk).strip("() ")
        return kw

    def select(self, **kw: Any) -> _Tertunda:
        return _Tertunda(select, self._hint(kw))

    def checkbox(self, **kw: Any) -> _Tertunda:
        return _Tertunda(checkbox, self._hint(kw))

    def confirm(self, **kw: Any) -> _Tertunda:
        return _Tertunda(confirm, self._hint(kw))

    def text(self, **kw: Any) -> _Tertunda:
        return _Tertunda(text, self._hint(kw))

    def secret(self, **kw: Any) -> _Tertunda:
        return _Tertunda(secret, self._hint(kw))

    def filepath(self, **kw: Any) -> _Tertunda:
        return _Tertunda(filepath, self._hint(kw))


inquirer = _Inquirer()
