"""Label aktivitas untuk UI; nama tool asli tetap ada dalam detail."""
from __future__ import annotations


_LABELS = {
    "read_file": "Baca file", "read_files": "Baca file",
    "write_file": "Tulis file", "append_file": "Tulis file",
    "edit_file": "Ubah file", "edit_files": "Ubah file",
    "delete_file": "Hapus file", "list_dir": "Lihat folder",
    "glob_files": "Cari file", "grep": "Cari teks",
    "search_text": "Cari teks", "search_multi_text": "Cari teks",
    "run_command": "Jalankan perintah", "run_command_bg": "Jalankan proses",
    "bg_output": "Periksa proses", "bg_send": "Kirim input",
    "web_search": "Cari di web", "fetch_url": "Baca halaman",
    "take_screenshot": "Ambil screenshot", "read_image_local": "Baca gambar",
    "plan": "Susun rencana", "plan_step": "Perbarui rencana",
    "sasaran": "Periksa proyek", "project_info": "Periksa proyek",
    "save_script": "Simpan skrip", "catat_kerja": "Catat hasil",
    "kerja_terakhir": "Lihat catatan", "cari_tool": "Cari alat",
    "list_tools": "Lihat alat", "ask_user": "Minta masukan",
}


def activity_label(name: str, args: dict | None = None) -> str:
    label = _LABELS.get(name, name.replace("_", " "))
    args = args or {}
    path = args.get("path") or args.get("filePath")
    if isinstance(path, str) and path:
        target = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        label += " · " + target
    elif name in ("run_command", "run_command_bg") and isinstance(args.get("command"), str):
        command = " ".join(args["command"].split())
        label += " · " + (command[:47] + "…" if len(command) > 48 else command)
    return label
