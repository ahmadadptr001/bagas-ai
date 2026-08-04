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

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.utils import get_cwidth

# Palet mengikuti tema yang sudah dipakai CLI (catppuccin-ish) supaya prompt
# ini menyatu dengan panel & jejak langkah di sekitarnya.
UNGU = "fg:#cba6f7"
BIRU = "fg:#89b4fa"
HIJAU = "fg:#a6e3a1"
KUNING = "fg:#f9e2af"
MERAH = "fg:#f38ba8"
REDUP = "fg:#6c7086"
TEKS = "fg:#cdd6f4"

# Sebanyak-banyaknya baris pilihan yang tampil sekaligus; sisanya digulung.
_MAKS_TAMPIL = 9
# Daftar sepanjang ini ke atas mendapat kotak pencarian (ketik untuk menyaring).
_AMBANG_CARI = 8
_LEBAR_MAKS = 96


class Choice:
    """Satu pilihan: `value` yang dikembalikan, `name` yang ditampilkan.

    Bentuknya sengaja sama dengan InquirerPy.base.control.Choice supaya
    pemanggil lama tak perlu diubah sama sekali."""

    __slots__ = ("value", "name", "enabled")

    def __init__(self, value: Any, name: str | None = None,
                 enabled: bool = False) -> None:
        self.value = value
        self.name = str(value) if name is None else str(name)
        self.enabled = bool(enabled)

    def __repr__(self) -> str:  # pragma: no cover - bantu debug saja
        return f"Choice({self.value!r}, {self.name!r})"


def _sebagai_choices(items: Iterable[Any]) -> list[Choice]:
    """Terima daftar Choice ATAU string polos (dipakai handler ask_user)."""
    out: list[Choice] = []
    for it in items or []:
        out.append(it if isinstance(it, Choice) else Choice(it, str(it)))
    return out


def _lebar_terminal() -> int:
    try:
        import shutil
        return max(40, min(shutil.get_terminal_size((80, 24)).columns, 200))
    except Exception:  # noqa: BLE001
        return 80


def _potong(teks: str, maks: int) -> str:
    """Potong teks berdasarkan LEBAR TAMPIL (emoji dihitung 2 kolom)."""
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
    baris: list[str] = []
    for asli in teks.splitlines() or [""]:
        kini, w = "", 0
        # split() tanpa argumen: spasi ganda tak menghasilkan "kata kosong"
        # yang akan muncul sebagai spasi menggantung di ujung baris.
        for kata in asli.split():
            kw = get_cwidth(kata)
            if kini and w + 1 + kw > lebar:
                baris.append(kini)
                kini, w = kata, kw
            else:
                kini = f"{kini} {kata}" if kini else kata
                w += (1 + kw) if kini != kata else kw
        baris.append(kini)
    return baris or [""]


def _kotak(judul: str, isi: list[list[tuple[str, str]]], footer: str,
           warna: str = UNGU) -> FormattedText:
    """Bungkus baris-baris isi dengan bingkai bersisi tiga (lihat docstring).

    Judul PANJANG — mis. permintaan izin yang memuat path lengkap — tidak
    dipaksa masuk ke garis atas: ia pindah ke dalam kotak dan dibungkus jadi
    beberapa baris. Kalau dipaksa di garis atas, judulnya akan melewati lebar
    terminal lalu dilipat sendiri oleh terminal, dan bingkainya berantakan."""
    lebar = min(_lebar_terminal() - 2, _LEBAR_MAKS)
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
            baris([(warna, "│  "), (f"{TEKS} bold", t)])
        baris([(warna, "│")])
    for isi_baris in isi:
        baris([(warna, "│  ")] + isi_baris)
    if footer:
        baris([(warna, "│")])
        baris([(warna, "│  "), (REDUP, _potong(footer, lebar - 4))])
    frag.extend([(warna, "╰" + "─" * (lebar - 1))])
    return FormattedText(frag)


def _jalankan(bangun: Callable[[], FormattedText], kb: KeyBindings) -> Any:
    """Jalankan satu Application inline (tidak layar-penuh) sampai app.exit()."""
    ctrl = FormattedTextControl(bangun, focusable=True, show_cursor=False)
    app: Application = Application(
        layout=Layout(HSplit([Window(ctrl, dont_extend_height=True)])),
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
        (REDUP, _potong(judul, 46) + ("  ·  " if jawaban else "")),
        (TEKS, _potong(jawaban, 60)),
    ]))


def _batal(judul: str) -> None:
    print_formatted_text(FormattedText([
        (KUNING, "  ◼ "), (REDUP, _potong(judul, 46) + "  ·  dibatalkan")]))


# ------------------------------------------------------------------ select
def select(message: str = "", choices: Sequence[Any] = (), default: Any = None,
           hint: str = "", warna: str = UNGU, ringkas: bool = True,
           **_lain: Any) -> Any:
    """Menu pilih-satu. Kembalikan `value` pilihan; batal -> KeyboardInterrupt."""
    opsi = _sebagai_choices(choices)
    if not opsi:
        raise ValueError("select butuh minimal satu pilihan")
    if not _interaktif():
        return _pilih_polos(message, opsi, default)

    idx = 0
    for i, c in enumerate(opsi):
        if default is not None and c.value == default:
            idx = i
            break
    boleh_cari = len(opsi) > _AMBANG_CARI
    keadaan = {"idx": idx, "kueri": ""}

    def tersaring() -> list[int]:
        q = keadaan["kueri"].lower()
        if not q:
            return list(range(len(opsi)))
        return [i for i, c in enumerate(opsi) if q in c.name.lower()]

    def selaraskan() -> None:
        """Pastikan kursor menunjuk entri yang MASIH lolos saringan.

        Dipanggil dari penangan tombol, bukan dari fungsi penggambar. Kalau
        hanya diperbaiki saat menggambar, deretan tombol yang datang tanpa jeda
        — ketikan cepat, teks yang ditempel, atau masukan terprogram — bisa
        sampai ke Enter sebelum satu pun gambar-ulang terjadi, sehingga yang
        terpilih adalah entri pertama daftar ASLI, bukan hasil pencarian."""
        lihat = tersaring()
        if lihat and keadaan["idx"] not in lihat:
            keadaan["idx"] = lihat[0]

    def bangun() -> FormattedText:
        lihat = tersaring()
        if not lihat:
            isi = [[(KUNING, "tak ada yang cocok dengan "),
                    (f"{KUNING} bold", repr(keadaan["kueri"]))]]
            return _kotak(message, isi, "ketik untuk ubah pencarian · esc batal",
                          warna)
        selaraskan()
        pos = lihat.index(keadaan["idx"])
        awal = 0 if len(lihat) <= _MAKS_TAMPIL else max(
            0, min(pos - _MAKS_TAMPIL // 2, len(lihat) - _MAKS_TAMPIL))
        akhir = min(len(lihat), awal + _MAKS_TAMPIL)
        lebar = min(_lebar_terminal() - 2, _LEBAR_MAKS) - 8

        isi: list[list[tuple[str, str]]] = []
        if awal > 0:
            isi.append([(REDUP, f"  ↑ {awal} lainnya di atas")])
        for urut in range(awal, akhir):
            i = lihat[urut]
            c = opsi[i]
            dipilih = i == keadaan["idx"]
            nomor = "" if boleh_cari else f"{urut + 1}. "
            isi.append([
                (f"{warna} bold" if dipilih else "", "❯ " if dipilih else "  "),
                (REDUP if not dipilih else f"{warna} bold", nomor),
                (f"{warna} bold" if dipilih else TEKS,
                 _potong(c.name, lebar - len(nomor))),
            ])
        if akhir < len(lihat):
            isi.append([(REDUP, f"  ↓ {len(lihat) - akhir} lainnya di bawah")])

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

    @kb.add("pageup")
    def _(_e): geser(-_MAKS_TAMPIL)

    @kb.add("pagedown")
    def _(_e): geser(_MAKS_TAMPIL)

    @kb.add("home")
    def _(_e):
        lihat = tersaring()
        if lihat:
            keadaan["idx"] = lihat[0]

    @kb.add("end")
    def _(_e):
        lihat = tersaring()
        if lihat:
            keadaan["idx"] = lihat[-1]

    @kb.add("enter")
    def _(e):
        if tersaring():
            e.app.exit(result=opsi[keadaan["idx"]])
        # tak ada yang cocok -> jangan keluar dengan hasil kosong

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
            if n < len(lihat):
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
             warna: str = BIRU, ringkas: bool = True, **_lain: Any) -> list[Any]:
    """Menu pilih-banyak. Kembalikan daftar `value` yang dicentang."""
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
            return _kotak(message, [[(KUNING, "tak ada yang cocok")]],
                          "ketik untuk ubah pencarian · esc batal", warna)
        selaraskan()
        pos = lihat.index(keadaan["idx"])
        awal = 0 if len(lihat) <= _MAKS_TAMPIL else max(
            0, min(pos - _MAKS_TAMPIL // 2, len(lihat) - _MAKS_TAMPIL))
        akhir = min(len(lihat), awal + _MAKS_TAMPIL)
        lebar = min(_lebar_terminal() - 2, _LEBAR_MAKS) - 10

        isi: list[list[tuple[str, str]]] = []
        if awal > 0:
            isi.append([(REDUP, f"  ↑ {awal} lainnya di atas")])
        for urut in range(awal, akhir):
            i = lihat[urut]
            c = opsi[i]
            aktif = i == keadaan["idx"]
            isi.append([
                (f"{warna} bold" if aktif else "", "❯ " if aktif else "  "),
                (HIJAU if i in ditandai else REDUP, "◉ " if i in ditandai else "○ "),
                (f"{warna} bold" if aktif else TEKS, _potong(c.name, lebar)),
            ])
        if akhir < len(lihat):
            isi.append([(REDUP, f"  ↓ {len(lihat) - akhir} lainnya di bawah")])

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
def confirm(message: str = "", default: bool = True, warna: str = KUNING,
            ringkas: bool = True, **_lain: Any) -> bool:
    """Konfirmasi Ya/Tidak mendatar. Batal (esc) dianggap TIDAK."""
    if not _interaktif():
        return default
    keadaan = {"ya": bool(default)}

    def bangun() -> FormattedText:
        ya, tidak = keadaan["ya"], not keadaan["ya"]
        isi = [[
            (f"{HIJAU} bold" if ya else REDUP, "❯ Ya" if ya else "  Ya"),
            ("", "      "),
            (f"{MERAH} bold" if tidak else REDUP,
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
                 HIJAU if hasil else KUNING)
    return hasil


# ------------------------------------------------------- input teks & path
def _prompt_teks(message: str, hint: str, *, rahasia: bool = False,
                 default: str = "", completer: Any = None) -> str:
    """Kotak pertanyaan + satu baris input (memakai PromptSession).

    Line editing (Home/End/Ctrl+W/riwayat) diserahkan ke prompt_toolkit —
    menulisnya sendiri berarti mengulang persoalan Backspace yang berbeda
    antar-terminal, dan itu sudah pernah menghabiskan waktu di proyek ini."""
    from prompt_toolkit import PromptSession

    if not _interaktif():
        raise KeyboardInterrupt
    print_formatted_text(_kotak(message, [], hint, BIRU))
    sesi: Any = PromptSession()
    try:
        return sesi.prompt(
            FormattedText([(f"{UNGU} bold", "  ❯ ")]),
            is_password=rahasia, default=default or "",
            completer=completer, complete_while_typing=bool(completer),
        )
    except EOFError as exc:  # Ctrl+D -> setara batal
        raise KeyboardInterrupt from exc


def text(message: str = "", default: str = "", hint: str = "",
         **_lain: Any) -> str:
    return _prompt_teks(message, hint or "⏎ kirim  ·  Ctrl+C batal",
                        default=str(default or ""))


def secret(message: str = "", **_lain: Any) -> str:
    return _prompt_teks(message, "ketikan disembunyikan  ·  Ctrl+C batal",
                        rahasia=True)


def filepath(message: str = "", only_directories: bool = False,
             hint: str = "", **_lain: Any) -> str:
    from prompt_toolkit.completion import PathCompleter

    return _prompt_teks(
        message,
        hint or "Tab autolengkap  ·  ⏎ pilih  ·  Ctrl+C batal",
        completer=PathCompleter(only_directories=bool(only_directories),
                                expanduser=True),
    )


# ------------------------------------------------- cadangan non-interaktif
def _pilih_polos(message: str, opsi: list[Choice], default: Any) -> Any:
    """Terminal tak interaktif (mis. keluaran dialihkan): daftar bernomor biasa.

    Tanpa ini, prompt_toolkit melempar galat yang tak berarti bagi pengguna."""
    print(message)
    for i, c in enumerate(opsi, 1):
        print(f"  {i}. {c.name}")
    try:
        jawab = input("Pilih nomor: ").strip()
        return opsi[int(jawab) - 1].value
    except (ValueError, IndexError, EOFError, OSError):
        for c in opsi:
            if c.value == default:
                return c.value
        return opsi[0].value


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
