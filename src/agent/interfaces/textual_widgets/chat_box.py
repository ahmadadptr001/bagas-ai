"""ChatBox widget — kotak input di bagian bawah layar.

Satu-satunya tempat mengetik. Bentuk & posisinya sama persis saat idle
maupun saat AI bekerja. Mendukung:

- Input MULTI-BARIS: teks yang melebihi lebar kotak turun sendiri ke
  baris berikutnya (soft wrap), kotak tumbuh sampai maksimal 5 baris,
  dan MENGgulir bila teksnya lebih panjang lagi. Enter tetap mengirim
  (bukan baris baru) — baris-baris tadi cuma pembungkusan visual.
- Autocomplete slash command: dropdown DI ATAS kotak input (seperti
  Claude Code), navigasi panah, terima dengan Tab/Enter, tutup dengan Esc.
- Paste: deteksi berkas gambar/video, tempelan panjang jadi penanda,
  tempelan multi-baris TIDAK terpotong satu baris.
- Navigasi kursor (panah, Home/End, Ctrl+W/U/K).

CATATAN BUG YANG SUDAH DIPERBAIKI (jangan diulang):

1. ``self._x = var(False)`` di ``__init__`` BUKAN reactive — ``var``/``reactive``
   adalah descriptor yang HANYA bekerja sebagai atribut kelas. Dulu
   ``_autocomplete_visible`` dibuat begitu, jadi ``watch__autocomplete_visible``
   tak pernah dipanggil dan dropdown TAK PERNAH muncul. Sekarang dropdown
   dikendalikan langsung lewat ``.display`` (tanpa reactive, tanpa watcher).
2. ``Input._on_paste`` menyisipkan HANYA baris pertama lalu ``event.stop()``,
   sehingga ``ChatBox.on_paste`` mati total. Sekarang ``ChatInput._on_paste``
   meneruskan tempelan utuh ke ChatBox (berlaku juga untuk TextArea).
3. Tombol panah/Tab/Esc dulu tak pernah sampai ke dropdown (Input yang
   fokus). Sekarang ``ChatInput.on_key`` menangani lebih dulu lalu
   ``prevent_default()`` + ``stop()`` agar perilaku bawaan dan binding
   global (fokus-berikutnya) tidak ikut jalan.
4. ``TextArea`` TIDAK punya ``height: auto`` yang mengikuti isi
   (``get_content_height``-nya warisan ScrollView). Tinggi kotak diatur
   manual di ``_sesuaikan_tinggi`` dari ``wrapped_document.height`` —
   cara lain (CSS auto/max-height) membuat kotak tetap 1 baris.
"""
from __future__ import annotations

from pathlib import Path

from textual import events
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option
from rich.text import Text

from ...ui import tema

try:
    from ...ui.ascii_art import is_image_path, is_video_path
except Exception:  # noqa: BLE001 — modul opsional
    def is_image_path(text: str) -> bool:  # type: ignore[misc]
        return False

    def is_video_path(text: str) -> bool:  # type: ignore[misc]
        return False


# (perintah, keterangan, butuh_argumen)
# Keterangan ikut tampil di dropdown supaya pengguna tak perlu hafal.
# Daftar ini WAJIB sinkron dengan _handle_command() di textual_app.py.
_SLASH_COMMANDS: list[tuple[str, str, bool]] = [
    ("/help", "daftar semua perintah", False),
    ("/model", "ganti model / varian situs", True),
    ("/effort", "usaha berpikir (web & API)", True),
    ("/theme", "ganti tema warna", True),
    ("/compact", "padatkan konteks ke berkas ingatan", False),
    ("/send-compact", "kirim memory terakhir ke percakapan", False),
    ("/memory", "kelola ingatan panjang", True),
    ("/add-dir", "tambah folder konteks", True),
    ("/dirs", "lihat folder konteks", False),
    ("/scan", "pindai ulang & segarkan peta proyek", False),
    ("/review", "buru bug di seluruh proyek", False),
    ("/tim", "tim review spesialis", True),
    ("/live", "screenshot layar tiap pertanyaan", False),
    ("/video", "alias mode screenshot /live", False),
    ("/stream", "hidup/matikan tampilan mengalir", False),
    ("/mic", "bacakan kabar dan jawaban AI", True),
    ("/voice", "mikrofon sebagai input perintah", True),
    ("/image", "baca gambar lokal via Python", True),
    ("/new", "mulai sesi baru", False),
    ("/reset", "reset percakapan sesi ini", False),
    ("/clear", "bersihkan layar", False),
    ("/version", "lihat versi bagas-ai", False),
    ("/exit", "keluar", False),
]

_CMD_LEBAR = max(len(c) for c, _, _ in _SLASH_COMMANDS)
_MAKS_BARIS = 9
# Tinggi maksimal kotak input (baris layar) sebelum mulai menggulir.
_MAKS_TINGGI_INPUT = 5


class ChatInput(TextArea):
    """TextArea yang menyerahkan tombol navigasi & paste ke ChatBox induknya.

    TextArea dipilih (bukan Input) karena Input tak bisa membungkus teks:
    tempelan panjang hilang ke samping. ``soft_wrap=True`` membungkus teks
    di batas kolom; ``tab_behavior="focus"`` supaya Tab tidak disisipkan
    sebagai indentasi (ChatBox memakainya untuk autocomplete).
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("show_line_numbers", False)
        kwargs.setdefault("compact", True)
        kwargs.setdefault("tab_behavior", "focus")
        super().__init__("", **kwargs)

    def _chatbox(self) -> "ChatBox | None":
        node = self.parent
        while node is not None and not isinstance(node, ChatBox):
            node = node.parent
        return node  # type: ignore[return-value]

    def on_key(self, event: events.Key) -> None:
        """Tangani tombol SEBELUM perilaku bawaan TextArea & binding global.

        ``prevent_default()`` menghentikan pencarian handler di MRO (jadi
        ``TextArea._on_key`` tak ikut jalan) dan ``stop()`` menghentikan
        gelembung ke Screen/App (jadi binding global tak merebut tombol).
        """
        box = self._chatbox()
        if box is None:
            return
        if box.proses_tombol(event.key):
            event.prevent_default()
            event.stop()
            return
        # Enter SELALU mengirim, bukan menyisipkan baris baru: kotak ini
        # satu paragraf rata-kanan; baris-baris di layar hanyalah
        # pembungkusan visual oleh soft wrap.
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            box.kirim()

    def _on_paste(self, event: events.Paste) -> None:
        """Teruskan tempelan UTUH ke ChatBox.

        Widget bawaan menyisipkan mentah apa adanya — deteksi berkas
        gambar dan penanda tempelan panjang jalan di ChatBox, bukan di sini.
        """
        event.stop()
        event.prevent_default()
        box = self._chatbox()
        if box is not None:
            box.handle_paste(event.text or "")


class ChatBox(Widget):
    """Kotak input + dropdown autocomplete.

    Susunan (atas ke bawah)::

        +--------------------------------------+
        | /model    ganti atau lihat model     |  <- dropdown (opsional)
        | /memory   kelola ingatan panjang     |
        +--------------------------------------+
          panah pilih - tab lengkapi - esc tutup
        +--------------------------------------+
        | > ketik di sini                      |
        +--------------------------------------+
    """

    # Pesan — HARUS turunan textual.message.Message.
    # ``namespace="chatbox"`` PENTING: tanpa itu Textual menurunkan nama
    # handler dari nama kelas (ChatBox -> "chat_box") dan aplikasi harus
    # mendefinisikan ``on_chat_box_submitted`` — padahal yang ada
    # ``on_chatbox_submitted``. Akibatnya pesan TERKIRIM tapi tidak pernah
    # ditangani: Enter terlihat tak melakukan apa pun.
    class Submitted(Message, namespace="chatbox"):
        """Pengguna mengirim teks."""

        def __init__(self, text: str):
            self.text = text
            super().__init__()

    class Cancelled(Message, namespace="chatbox"):
        """Pengguna membatalkan."""

    class Recall(Message, namespace="chatbox"):
        """Panah-atas/bawah dengan dropdown tertutup.

        Dua kemungkinan makna (ditentukan app, bukan ChatBox — antrean &
        riwayat milik app): panah-ATAS menarik teks antrean terakhir
        kembali ke sini untuk diedit, atau bila antrean kosong menempel
        teks sebelumnya dari riwayat; panah-BAWAH berjalan kembali ke
        bawah dalam riwayat."""

        def __init__(self, maju: bool = False):
            self.maju = maju  # True = panah-bawah (riwayat, bukan antrean)
            super().__init__()

    class Pasted(Message, namespace="chatbox"):
        """Tempelan terdeteksi."""

        def __init__(self, text: str, is_media: bool = False,
                     media_path: str = ""):
            self.text = text
            self.is_media = is_media
            self.media_path = media_path
            super().__init__()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._input = ChatInput(
            placeholder="Ketik pesan, / untuk perintah…",
            id="chat-input",
        )
        self._autocomplete = OptionList(id="autocomplete-list")
        self._hint = Static("", id="autocomplete-hint")
        self._prompt = Static("❯", id="input-prompt")
        # Perintah yang sedang ditawarkan, searah indeks dengan OptionList.
        self._matches: list[tuple[str, str, bool]] = []
        self._open = False

    def compose(self):
        # Urutan penting: dropdown DI ATAS baris input.
        yield self._autocomplete
        yield self._hint
        with Horizontal(id="input-row"):
            yield self._prompt
            yield self._input

    def on_mount(self):
        # RichLog/OptionList bisa merebut fokus dari input saat diklik.
        self._autocomplete.can_focus = False
        self._autocomplete.display = False
        self._hint.display = False
        self._prompt.update(Text("❯", style=f"bold {tema.p('aksen')}"))
        self._input.focus()
        self._sesuaikan_tinggi()

    def on_resize(self) -> None:
        # Lebar berubah -> pembungkusan berubah -> tinggi bisa berubah.
        # Tunggu refresh dulu supaya scrollable_content_region terukur
        # dengan ukuran baru, baru hitung ulang.
        self.call_after_refresh(self._sesuaikan_tinggi)

    # --- Tinggi dinamis --------------------------------------------------

    def _sesuaikan_tinggi(self) -> None:
        """Tumbuhkan/kecilkan kotak mengikuti isi, maks _MAKS_TINGGI_INPUT.

        TextArea tidak punya tinggi-otomatis-mengikuti-isi (lihat catatan
        #4 di docstring modul), jadi dihitung manual dari jumlah baris
        TERBUNGKUS. Bila teks melebihi 5 baris layar, kotak berhenti
        tumbuh dan TextArea menggulir sendiri (cursor tetap dibuat
        terlihat). Dibatasi juga dari bawah: minimal 1 baris.
        """
        try:
            # TextArea.edit() memang membungkus ulang wrapped_document;
            # baca saja hasilnya. (Rewrap eksplisit hanya perlu saat
            # resize — ditangani on_resize di bawah.)
            tinggi = self._input.wrapped_document.height
        except Exception:  # noqa: BLE001 — belum ter-mount
            return
        tinggi = max(1, min(tinggi, _MAKS_TINGGI_INPUT))
        if self._input.region.height != tinggi:
            self._input.styles.height = tinggi
        # Setelah tinggi berubah, pastikan kursor masih kelihatan (bila
        # teks sudah menggulir, editan di ujung bawah tak men-scroll
        # sendiri).
        try:
            self._input.scroll_cursor_visible()
        except Exception:  # noqa: BLE001 — belum ter-mount
            pass

    # --- Fokus ---------------------------------------------------------

    def focus(self, scroll_visible: bool = True):
        """Selalu arahkan fokus ke field input, bukan ke wadahnya."""
        try:
            self._input.focus(scroll_visible)
        except Exception:  # noqa: BLE001 — saat belum ter-mount
            pass
        return self

    # --- Paste ---------------------------------------------------------

    def handle_paste(self, data: str) -> None:
        """Tangani tempelan (dipanggil dari ``ChatInput._on_paste``)."""
        if not data:
            return
        data = data.replace("\r\n", "\n").replace("\r", "\n")

        # Berkas media yang di-drop -> penanda [foto] + pratinjau.
        try:
            cek = data.strip().strip('"').strip("'")
            if cek and (is_image_path(cek) or is_video_path(cek)):
                resolved = str(Path(cek).resolve(strict=False))
                self._set_value("[foto]")
                self.post_message(
                    self.Pasted("[foto]", is_media=True, media_path=resolved))
                return
        except Exception:  # noqa: BLE001 — path aneh dari OS
            pass

        lines = data.split("\n")

        # Tempelan panjang -> penanda ringkas, isi disimpan di tempelan.py.
        # Selalu simpan (jangan digate perlu_diringkas) supaya isi tak pernah
        # hilang: penanda mengandung nomor yang bisa dikembalikan saat kirim.
        if len(lines) > 15 or len(data) > 500:
            marker = ""
            try:
                from ...tempelan import simpanan
                s = simpanan()
                marker = s.simpan(data)
            except Exception:  # noqa: BLE001 — modul opsional
                marker = ""
            if not marker:
                # Penyimpanan gagal: tampilkan penanda berisi jumlah baris
                # agar yang ditempel tetap terlihat (isi bisa dikembangkan
                # manual nanti).
                marker = f"[tempelan {len(lines)} baris]"
            self.insert_text_at_cursor(marker)
            self.post_message(self.Pasted(marker))
            return

        # Tempelan pendek: sisipkan apa adanya (baris jadi spasi).
        cleaned = " ".join(x for x in (b.strip() for b in lines) if x)
        if cleaned:
            self.insert_text_at_cursor(cleaned)
            self.post_message(self.Pasted(cleaned))

    def on_paste(self, event: events.Paste) -> None:
        """Cadangan bila Textual mengirim Paste langsung ke ChatBox."""
        event.stop()
        self.handle_paste(event.text or "")

    # --- Tombol --------------------------------------------------------

    def proses_tombol(self, key: str) -> bool:
        """Tangani satu tombol. True = sudah ditangani, jangan diteruskan.

        PENTING: JANGAN beri nama ``handle_key`` — itu API internal Textual
        (``Widget._on_key`` menjalankan ``await self.handle_key(event)``).
        Versi lama menimpanya dengan fungsi sync yang mengembalikan bool,
        sehingga setiap tombol yang sampai ke ChatBox melempar
        ``TypeError: object bool can't be used in 'await' expression``.
        """
        if self._open:
            if key in ("down", "ctrl+n"):
                self._move(1)
                return True
            if key in ("up", "ctrl+p"):
                self._move(-1)
                return True
            if key == "pagedown":
                self._move(5)
                return True
            if key == "pageup":
                self._move(-5)
                return True
            if key == "escape":
                self._close()
                return True
            if key == "tab":
                self._accept()
                return True
            if key == "enter":
                # Enter pada kandidat: lengkapi dulu. Kalau yang diketik
                # SUDAH sama dengan kandidat, biarkan Enter mengirim.
                typed = self._input.text.strip().lower()
                exact = [m for m in self._matches if m[0] == typed]
                if exact and not exact[0][2]:
                    self._close()
                    return False
                self._accept()
                return True
            return False

        # Dropdown tertutup: Tab membuka tawaran untuk teks "/...".
        if key == "tab":
            if self._input.text.startswith("/"):
                self._refresh_matches(self._input.text, paksa=True)
                return True
            return False
        # Panah-atas/-bawah = antrean & riwayat (lihat Recall). Teks yang
        # membungkus ke beberapa baris layar tetap SATU baris logika, dan
        # kursor berpindah antar baris layar lewat kiri/kanan — jadi
        # aman merampas panah vertikal untuk riwayat.
        # (Dropdown TERBUKA sudah menyimpan panah di atas untuk
        # navigasi tawaran.)
        if key == "up":
            self.post_message(self.Recall())
            return True
        if key == "down":
            self.post_message(self.Recall(maju=True))
            return True
        if key == "escape":
            if self._input.text:
                self._set_value("")
                return True
            return False
        return False

    # --- Peristiwa TextArea ---------------------------------------------

    def kirim(self) -> None:
        """Enter di kotak input — kirim teks (dipanggil ChatInput)."""
        if self._open:
            self._accept()
            return
        text = self._input.text.strip()
        if not text:
            return
        self._set_value("")
        self._close()
        self.post_message(self.Submitted(text))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Teks berubah — segarkan tawaran autocomplete & tinggi kotak."""
        event.stop()
        self._refresh_matches(self._input.text)
        self._sesuaikan_tinggi()

    def on_option_list_option_selected(
            self, event: OptionList.OptionSelected) -> None:
        """Klik pada salah satu tawaran."""
        event.stop()
        self._autocomplete.highlighted = event.option_index
        self._accept()

    # --- Autocomplete --------------------------------------------------

    def _refresh_matches(self, text: str, paksa: bool = False) -> None:
        """Hitung & tampilkan tawaran perintah untuk ``text``."""
        if not text.startswith("/"):
            self._close()
            return
        # Sudah ada spasi/baris-baru -> pengguna mengetik argumen, bukan
        # nama perintah.
        if " " in text or "\n" in text or text != text.rstrip():
            self._close()
            return

        q = text.lower()
        if q == "/":
            matches = list(_SLASH_COMMANDS)
        else:
            awalan = [c for c in _SLASH_COMMANDS if c[0].startswith(q)]
            isi = [c for c in _SLASH_COMMANDS
                   if not c[0].startswith(q) and q[1:] in c[0][1:]]
            matches = awalan + isi

        if not matches:
            self._close()
            return
        # Satu kandidat yang PERSIS sama dengan yang diketik: tak ada lagi
        # yang bisa dilengkapi, jadi jangan halangi pandangan.
        if not paksa and len(matches) == 1 and matches[0][0] == q:
            self._close()
            return
        self._open_with(matches)

    def _open_with(self, matches: list[tuple[str, str, bool]]) -> None:
        """Buka dropdown berisi ``matches``.

        Teks opsi sengaja TIDAK diberi warna (cuma bold/dim): warna diatur
        CSS. Dulu perintah diberi ``bold aksen`` di sini — saat baris
        tersorot background-nya juga aksen, jadi teks menyatu dengan
        background dan tak terlihat.
        """
        self._matches = matches[:_MAKS_BARIS]
        lebar = self.size.width or 80
        # Keterangan hanya ditampilkan bila memang ada ruangnya.
        tampil_ket = lebar >= _CMD_LEBAR + 22
        sisa = max(8, lebar - _CMD_LEBAR - 8)

        self._autocomplete.clear_options()
        for cmd, ket, _args in self._matches:
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(cmd.ljust(_CMD_LEBAR), style="bold")
            if tampil_ket and ket:
                row.append("  ")
                row.append(ket[:sisa], style="dim")
            self._autocomplete.add_option(Option(row))

        self._autocomplete.highlighted = 0
        self._autocomplete.display = True
        self._hint.update(Text(
            "↑↓ pilih · tab lengkapi · esc tutup",
            style=f"dim {tema.p('redup')}",
        ))
        self._hint.display = True
        self._open = True

    def _close(self) -> None:
        """Tutup dropdown."""
        if not self._open and not self._autocomplete.display:
            return
        self._open = False
        self._matches = []
        self._autocomplete.display = False
        self._hint.display = False
        try:
            self._autocomplete.clear_options()
        except Exception:  # noqa: BLE001 — saat unmount
            pass

    def _move(self, delta: int) -> None:
        """Geser sorotan dengan pembungkusan (wrap-around)."""
        n = len(self._matches)
        if n == 0:
            return
        cur = self._autocomplete.highlighted
        cur = 0 if cur is None else cur
        self._autocomplete.highlighted = (cur + delta) % n

    def _accept(self) -> None:
        """Terapkan kandidat yang sedang disorot ke field input."""
        if not self._matches:
            self._close()
            return
        idx = self._autocomplete.highlighted or 0
        idx = max(0, min(idx, len(self._matches) - 1))
        cmd, _ket, butuh_arg = self._matches[idx]
        self._close()
        # Spasi HANYA untuk perintah yang menerima argumen — kalau tidak,
        # spasi tersisa membuat perintah tak dikenal saat dikirim.
        self._set_value(cmd + (" " if butuh_arg else ""))

    def _set_value(self, text: str) -> None:
        """Ganti isi input dan taruh kursor di ujung.

        Setter ``TextArea.text`` TIDAK memindahkan kursor sendiri (kursor
        malah lompat ke awal); tanpa ini huruf berikutnya masuk di tengah.
        """
        self._input.text = text
        baris = text.split("\n")
        self._input.move_cursor((len(baris) - 1, len(baris[-1])))

    # --- API publik ----------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """Tandai kotak input saat giliran AI berjalan (border meredup)."""
        try:
            self.query_one("#input-row").set_class(busy, "-sibuk")
            self._prompt.update(Text(
                "⋯" if busy else "❯",
                style=f"bold {tema.p('aksen')}"))
        except Exception:  # noqa: BLE001 — belum ter-mount
            pass

    def refresh_theme(self) -> None:
        """Terapkan warna tema baru pada bagian yang dirender manual."""
        self._prompt.update(Text("❯", style=f"bold {tema.p('aksen')}"))
        if self._open and self._matches:
            self._open_with(self._matches)

    def set_text(self, text: str) -> None:
        self._set_value(text)

    def clear(self) -> None:
        self._set_value("")
        self._close()

    @property
    def current_text(self) -> str:
        return self._input.text

    @current_text.setter
    def current_text(self, value: str) -> None:
        self._set_value(value)

    @property
    def cursor_position(self) -> int:
        """Posisi kursor sebagai offset karakter (kompatibilitas app).

        TextArea hanya punya lokasi (baris, kolom); offset dihitung manual.
        """
        baris, kolom = self._input.selection.end
        isi = self._input.text.split("\n")
        if baris >= len(isi):
            return len(self._input.text)
        kolom = min(kolom, len(isi[baris]))
        return sum(len(b) + 1 for b in isi[:baris]) + kolom

    @property
    def autocomplete_open(self) -> bool:
        return self._open

    def insert_text_at_cursor(self, text: str) -> None:
        self._input.insert(text)

    def delete_word_before_cursor(self) -> None:
        """Hapus kata sebelum kursor (Ctrl+W).

        TextArea tak punya API ini; rentang dihapusnya dihitung manual
        dari lokasi kursor lalu diterapkan lewat replace().
        """
        baris, kolom = self._input.selection.end
        isi = self._input.text.split("\n")
        if baris >= len(isi):
            return
        line = isi[baris]
        kolom = min(kolom, len(line))
        if kolom <= 0:
            return
        i = kolom - 1
        while i > 0 and line[i - 1].isspace():
            i -= 1
        while i > 0 and not line[i - 1].isspace():
            i -= 1
        self._input.replace("", (baris, i), (baris, kolom))

    def apply_completion(self, completion: str) -> None:
        self._set_value(completion + " ")
        self.focus()
