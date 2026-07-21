"""Connector chat.qwen.ai (web).

Seluruh selektor di bawah DIVERIFIKASI LANGSUNG ke situs pada sesi yang sudah
login (2026-07-20): pesan uji dikirim, lalu DOM-nya dipetakan.

Catatan penting soal KONTROL: Qwen menaruh kontrolnya di DUA tempat berbeda —
pemilih MODEL di bar ATAS, dan pemilih MODE tepat di dekat kotak input. Keduanya
juga bukan elemen <button> melainkan [role="button"], sehingga selector bergaya
`button[aria-label=...]` TIDAK akan cocok. Tiap aksi /effort di bawah karena itu
membawa selector tombol pembukanya sendiri.
"""
from __future__ import annotations

from typing import Any

from .base import WebConnector
from .browser import BrowserError

# Tombol pembuka menu (keduanya [role="button"], bukan <button>).
_BTN_MODEL = '[aria-label="Select Model"]'   # bar ATAS
_BTN_MODE = '[aria-label="Select Mode"]'     # dekat kotak input


class QwenConnector(WebConnector):
    service = "qwen"
    label = "Qwen (web)"
    chat_url = "https://chat.qwen.ai/"
    # URL percakapan: https://chat.qwen.ai/c/<uuid> (terverifikasi) -> lanjut
    # sesi & --resume bisa menyambung ke chat yang sama.
    chat_url_template = "https://chat.qwen.ai/c/{id}"
    chat_id_pattern = r"/c/([0-9a-fA-F-]{16,})"

    # --- input ---
    # Kandidat BERURUTAN: kelas spesifik dulu, `textarea` polos hanya cadangan
    # bila situs mengganti nama kelasnya. (Dulu satu string berkoma — itu keliru:
    # daftar CSS tak berprioritas, jadi kotak pencarian yang muncul lebih dulu di
    # DOM bisa terpilih dan prompt diketik ke sana.)
    input_selector = ("textarea.message-input-textarea", "textarea")
    input_is_contenteditable = False
    submit_key = "Enter"
    # Enter TIDAK selalu mengirim di sini: TERUKUR gagal saat ada lampiran —
    # prompt tertinggal di kotak & URL tak pernah jadi /c/<id>, lalu giliran
    # gagal dengan "balasan tak terdeteksi" yang menyesatkan. Tombol Send dipakai
    # _submit() sebagai cadangan; inilah yang juga diklik pengguna.
    send_button_selector = 'button.send-button, [aria-label="Send"]'

    # --- deteksi belum-login ---
    # Qwen menampilkan kotak input untuk TAMU tetapi tak memproses pesannya,
    # jadi tombol Log in/Sign up-lah penanda sesungguhnya.
    logged_out_selector = (
        'button:has-text("Log in"), button:has-text("Sign up"), '
        'button:has-text("Get Started")'
    )

    # --- jawaban ---
    # PENTING — urutan ini hasil pengukuran, jangan dibalik:
    #   [class*='answer']    -> SATU elemen per pesan, isinya UTUH.
    #   [class*='assistant'] -> juga utuh, tapi tercampur "Thinking completed"
    #                           (disaring noise_pattern) — dipakai bila yang
    #                           pertama hilang karena situs berubah.
    #   [class*='markdown']  -> JANGAN didahulukan: itu per-PARAGRAF (terukur 19
    #                           elemen untuk satu jawaban), sehingga yang terbaca
    #                           cuma potongan terakhir — gejalanya jawaban mulai
    #                           di tengah kalimat, dan blok tool cuma terbaca
    #                           "[[/TOOL]]" sehingga usulan langkah hilang.
    message_selector = (
        "[class*='answer']",
        "[class*='assistant']",
        "[class*='markdown']",
    )
    read_as_markdown = True
    # Tombol Stop hanya ada selagi Qwen menjawab -> penanda paling andal.
    stop_selectors = ('[aria-label="Stop"]', '[role="button"]:has-text("Stop")')
    # Saat masih berpikir, satu-satunya teks yang muncul adalah indikator ini.
    noise_pattern = r"(?:Thinking[^\n]*\s*)+"

    # --- lampiran (screenshot dll) ---
    # DIPETAKAN LANGSUNG dari sesi nyata dengan merekam klik manual:
    #   [aria-label="Select Mode"] -> item menu "Upload attachment" -> input#filesUpload
    # Input-nya 0x0 (tersembunyi) dan bersarang di dalam kontrol "mode-select".
    file_input_selector = "input#filesUpload"
    # Sesudah file menempel, Qwen merender kartu file di komposer dengan kelas
    # fileitem-*. Menghitungnya jauh lebih andal daripada menunggu gambar:
    #   - `input.files` DIKOSONGKAN Qwen begitu unggahan dimulai (selalu 0);
    #   - thumbnail-nya datang dari URL https server (oss-accelerate.aliyuncs.com),
    #     bukan blob:, dan baru muncul SETELAH unggahan tuntas;
    #   - pratinjaunya BUKAN keturunan kotak input (ditelusuri 11 tingkat ke atas
    #     hasilnya tetap nol), jadi cara hitung <img> bawaan selalu 0 di sini.
    # Kartu ini muncul untuk gambar maupun dokumen, jadi tetap benar bila nanti
    # ada lampiran non-gambar.
    attach_item_selector = '[class*="fileitem-file-name-text"]'

    # --- /effort: kontrol ATAS (model) & BAWAH (mode) ---
    web_model_button = _BTN_MODEL
    web_actions = (
        ("Qwen3.7-Plus", ("Qwen3.7-Plus",),
         "model cepat & seimbang (bar atas)", _BTN_MODEL),
        ("Qwen3.7-Max", ("Qwen3.7-Max",),
         "model flagship, penalaran terkuat (bar atas)", _BTN_MODEL),
        ("Qwen3.8-Max-Preview", ("Qwen3.8-Max-Preview",),
         "pratinjau model generasi berikutnya (bar atas)", _BTN_MODEL),
        ("Mode: Deep Research", ("Deep Research",),
         "riset mendalam bertahap (tombol dekat input)", _BTN_MODE),
        ("Mode: Web Dev", ("Web Dev",),
         "mode bantu ngoding web (tombol dekat input)", _BTN_MODE),
    )

    # Teks pemberitahuan limit Qwen belum pernah terlihat; dikosongkan agar tak
    # ada pola longgar yang salah menangkap jawaban biasa.
    limit_patterns = ()

    # Item menu yang membuka pemilih file (dipetakan dari klik manual).
    _ITEM_UPLOAD = '[class*="mode-select-dropdown-item"]:has-text("Upload attachment")'

    def _upload(self, page: Any, paths: list[str]) -> None:
        """Lampirkan file lewat menu, meniru persis yang dilakukan pengguna.

        Kenapa tidak set_input_files saja pada input#filesUpload: TERUKUR tak
        berpengaruh — `input.files` tetap 0 dan tak ada kartu file yang muncul.
        Qwen baru menyiapkan unggahannya saat item menu "Upload attachment"
        diklik; klik itulah yang memicu pemilih file, dan Playwright
        menangkapnya lewat expect_file_chooser sehingga tak ada dialog OS yang
        benar-benar terbuka.

        Klik memakai _click_element: jendela connector berjalan tersembunyi, dan
        di situ klik mouse sungguhan gagal hit-test sehingga perlu jatuh ke
        dispatch event."""
        self._click_element(page.locator(_BTN_MODE).first)
        page.wait_for_timeout(500)
        try:
            with page.expect_file_chooser(timeout=15000) as chooser:
                self._click_element(page.locator(self._ITEM_UPLOAD).first)
            chooser.value.set_files(paths)
        except Exception as exc:  # noqa: BLE001
            try:  # jangan tinggalkan menu menggantung menutupi komposer
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            raise BrowserError(
                "pemilih file Qwen tak terbuka — menu 'Upload attachment' "
                f"mungkin berubah nama/letak: {exc}"
            ) from exc
