"""Tool rencana: daftar langkah yang terlihat di layar, bukan di kepala model.

Kenapa ini ada:

Keluhan yang paling sering muncul soal jalur AI web bukan "salah kode", tapi
"ngelantur" — melompat langkah, mengulang yang sudah selesai, atau berhenti di
tengah sambil mengira sudah beres. Sebabnya struktural: rencana tugas hanya
hidup di dalam balasan model, dan tiap giliran balasan itu tergeser oleh hasil
tool yang panjang. Yang tak tertulis di mana pun akan hilang.

`plan` memindahkan rencana itu KE LUAR: sekali ditulis, ia tersimpan di sisi
bagas-ai dan dicetak ulang tiap kali berubah. Efeknya dua arah — pengguna
melihat langkah mana yang sedang jalan, dan model menerima kembali daftarnya
sebagai teks pada setiap pemanggilan, jadi langkah yang dilewati jadi kelihatan
alih-alih hilang diam-diam.

SENGAJA sesederhana mungkin: satu daftar, satu penanda "sedang dikerjakan". Tak
ada sub-tugas, prioritas, atau ketergantungan antar langkah — struktur seperti
itu menambah hal yang bisa dipakai salah, sementara manfaatnya (langkah tak
hilang) sudah didapat penuh oleh daftar datar.
"""
from __future__ import annotations

import threading

from .base import tool

_lock = threading.Lock()
# Rencana giliran BERJALAN. Sengaja di memori proses, bukan di disk: rencana
# hanya bermakna selama tugasnya berlangsung, dan rencana basi dari sesi kemarin
# yang muncul kembali justru menyesatkan.
_state: dict = {"steps": [], "current": 0}
_MAKS_LANGKAH = 12


def reset() -> None:
    """Kosongkan rencana (dipanggil saat giliran/sesi baru dimulai)."""
    with _lock:
        _state["steps"] = []
        _state["current"] = 0


def render() -> str:
    """Rencana saat ini sebagai teks, atau "" bila belum ada."""
    with _lock:
        steps = list(_state["steps"])
        cur = _state["current"]
    if not steps:
        return ""
    baris = []
    for i, s in enumerate(steps, 1):
        if i < cur:
            baris.append(f"  ✓ {s}")
        elif i == cur:
            baris.append(f"  ▸ {s}   ← sedang dikerjakan")
        else:
            baris.append(f"  · {s}")
    return "\n".join(baris)


@tool
def plan(steps: list, current: int = 1) -> str:
    """Tuliskan rencana langkahmu sebagai daftar pendek supaya terlihat pengguna dan tak hilang di tengah jalan. Pakai untuk tugas yang butuh BEBERAPA langkah (mis. baca → ubah → validasi); tugas satu langkah tak perlu rencana. Panggil sekali di awal, lalu pakai plan_step untuk menandai kemajuan — jangan menulis ulang daftarnya tiap langkah.

    steps: daftar langkah singkat (2-12 item), masing-masing satu kalimat
        pendek berisi TINDAKAN konkret, mis. "baca WorksSection.tsx".
    current: nomor langkah yang sedang dikerjakan (1-based, default 1).
    """
    if not isinstance(steps, list) or len(steps) < 2:
        return ("[error] plan butuh minimal 2 langkah. Untuk tugas satu "
                "langkah, langsung kerjakan saja tanpa rencana.")
    bersih = [str(s).strip() for s in steps if str(s).strip()][:_MAKS_LANGKAH]
    if len(bersih) < 2:
        return "[error] langkahnya kosong semua."
    with _lock:
        _state["steps"] = bersih
        _state["current"] = max(1, min(int(current or 1), len(bersih)))
    return ("Rencana dicatat & ditampilkan ke pengguna:\n" + render()
            + "\n\nLanjutkan langkah yang bertanda ▸. Tandai kemajuan dengan "
              "plan_step(nomor) — jangan memanggil plan lagi kecuali "
              "rencananya memang BERUBAH.")


@tool
def plan_step(current: int, note: str = "") -> str:
    """Tandai langkah keberapa yang sedang kamu kerjakan sekarang, setelah langkah sebelumnya selesai. Murah dan cepat — pakai tiap kali berpindah langkah supaya pengguna tahu posisimu.

    current: nomor langkah yang SEDANG dikerjakan (1-based). Isi dengan angka
        melebihi jumlah langkah untuk menandai SEMUANYA selesai.
    note: catatan singkat opsional (mis. temuan dari langkah sebelumnya).
    """
    with _lock:
        n = len(_state["steps"])
        if not n:
            return ("[error] belum ada rencana. Panggil plan(steps=[...]) "
                    "lebih dulu, atau kerjakan langsung bila tugasnya sederhana.")
        _state["current"] = max(1, min(int(current or 1), n + 1))
        selesai = _state["current"] > n
    teks = render()
    if selesai:
        return ("Semua langkah rencana selesai:\n" + teks
                + "\n\nTutup dengan jawaban akhir berisi HASILnya."
                + (f"\ncatatan: {note.strip()}" if note.strip() else ""))
    return (teks + (f"\ncatatan: {note.strip()}" if note.strip() else "")
            + "\n\nKerjakan langkah bertanda ▸ sekarang — sertakan blok "
              "[[TOOL]]-nya di pesan yang sama.")
