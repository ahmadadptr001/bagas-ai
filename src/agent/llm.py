"""Sinyal lintas-lapisan untuk giliran model.

Modul ini dulu berisi klien HTTP ke NVIDIA (integrate.api.nvidia.com): streaming
chat-completion, tool-calling, deteksi rate-limit, retry berjenjang, dan
watchdog anti-macet. Semua itu DIHAPUS bersama model ber-API-key — bagas-ai kini
hanya memakai model AI web lewat browser, yang alurnya ditangani
agent/connectors dan Agent._run_connector. Penanganan "sementara" yang setara
tetap ada di sana dalam bentuk yang sesuai medianya: WebBusyError + tunggu-lalu-
ulangi untuk server penuh, dan WebLimitError untuk kuota situs habis.

Yang tersisa cuma Cancelled, dan itu memang harus hidup di sini: ia dilempar
oleh lapisan connector, ditangkap oleh core, lalu dikenali lagi oleh CLI. Kalau
didefinisikan di salah satu lapisan itu, dua lapisan lain harus mengimpor
lapisan yang bukan urusannya — dan connectors -> core akan jadi impor melingkar.
"""
from __future__ import annotations


class Cancelled(Exception):
    """Pengguna membatalkan giliran (Esc / Ctrl+C).

    Dibedakan dari kegagalan sungguhan supaya UI tak menampilkannya sebagai
    error, dan supaya core tetap merapikan tool yang menggantung lalu menyimpan
    sesi — pembatalan tidak boleh membuat konteks percakapan rusak.
    """
