"""Deteksi sistem operasi & sinkronisasi ke memory jangka panjang.

Tiap bagas-ai dijalankan, OS dideteksi lalu disimpan ke memory:
  - belum ada  -> ditambahkan,
  - sudah ada & sama  -> DILEWATI (tidak menulis ulang),
  - sudah ada & beda  -> diperbarui.

OS juga disisipkan ke system prompt (lihat prompts.py) supaya bagas-ai
MENYESUAIKAN semua perintah terminal dengan OS yang terdeteksi (PowerShell/cmd
di Windows, bash di Linux/macOS).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from pathlib import Path

from . import longmem

# Prefix stabil sebagai "kunci" fakta OS di memory (untuk upsert).
_PREFIX = "Sistem operasi pengguna:"
# Kunci fakta spesifikasi perangkat (laptop/PC) di memory.
_HW_PREFIX = "Spesifikasi perangkat pengguna:"


def label() -> str:
    """Nama OS + versi yang ringkas & manusiawi."""
    system = platform.system()
    if system == "Windows":
        rel = platform.release()
        return f"Windows {rel}".strip()
    if system == "Darwin":
        mac, _, _ = platform.mac_ver()
        return f"macOS {mac}".strip() if mac else "macOS"
    if system:
        return f"{system} {platform.release()}".strip()
    return "Linux"


def shell_hint() -> str:
    """Shell/terminal khas OS ini — untuk mengarahkan sintaks perintah."""
    system = platform.system()
    if system == "Windows":
        return "PowerShell / cmd"
    if system == "Darwin":
        return "bash/zsh"
    return "bash"


def summary() -> str:
    """Ringkasan satu baris: OS + shell. Dipakai di system prompt & memory."""
    return f"{label()} (shell: {shell_hint()})"


def _fact() -> str:
    return (
        f"{_PREFIX} {summary()}. "
        "Sesuaikan SEMUA perintah terminal dengan OS ini."
    )


def sync_to_memory() -> str:
    """Deteksi OS & sinkronkan ke memory. Return 'added'|'updated'|'unchanged'."""
    try:
        return longmem.upsert(_PREFIX, _fact())
    except Exception:
        return "unchanged"


# --- Spesifikasi perangkat (laptop/PC) --------------------------------------
# Deteksi 100% LOKAL & instan: registry/ctypes di Windows, /proc & sysfs di
# Linux, sysctl di macOS. TANPA WMI/PowerShell (lambat) dan TANPA LLM.

def _win_hardware() -> dict:
    import ctypes
    import winreg

    def _reg(path: str, value: str) -> str:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
                return str(winreg.QueryValueEx(k, value)[0]).strip()
        except OSError:
            return ""

    info: dict = {}
    man = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer")
    prod = _reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
    model = f"{man} {prod}".strip()
    if model and "to be filled" not in model.lower():
        info["model"] = model
    cpu = _reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
               "ProcessorNameString")
    if cpu:
        info["cpu"] = re.sub(r"\s+", " ", cpu)

    class _MemStat(ctypes.Structure):
        _fields_ = ([("dwLength", ctypes.c_uint32),
                     ("dwMemoryLoad", ctypes.c_uint32)]
                    + [(n, ctypes.c_uint64) for n in (
                        "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                        "ullAvailPageFile", "ullTotalVirtual",
                        "ullAvailVirtual", "ullAvailExtendedVirtual")])

    try:
        st = _MemStat()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            info["ram_gb"] = round(st.ullTotalPhys / 1024 ** 3)
    except Exception:
        pass

    gpus: list[str] = []
    base = (r"SYSTEM\CurrentControlSet\Control\Class"
            r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                sub = winreg.EnumKey(k, i)
                if not sub.isdigit():
                    continue
                desc = _reg(base + "\\" + sub, "DriverDesc")
                if desc and desc not in gpus:
                    gpus.append(desc)
    except OSError:
        pass
    if gpus:
        info["gpu"] = " + ".join(gpus[:3])
    return info


def _linux_hardware() -> dict:
    info: dict = {}
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
            if line.lower().startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        for line in Path("/proc/meminfo").read_text(errors="ignore").splitlines():
            if line.startswith("MemTotal"):
                info["ram_gb"] = round(int(line.split()[1]) / 1024 ** 2)
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        dmi = Path("/sys/devices/virtual/dmi/id")
        model = ((dmi / "sys_vendor").read_text().strip() + " "
                 + (dmi / "product_name").read_text().strip()).strip()
        if model:
            info["model"] = model
    except OSError:
        pass
    return info


def _mac_hardware() -> dict:
    import subprocess

    def _sysctl(key: str) -> str:
        try:
            r = subprocess.run(["sysctl", "-n", key], capture_output=True,
                               text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    info: dict = {}
    if cpu := _sysctl("machdep.cpu.brand_string"):
        info["cpu"] = cpu
    if (mem := _sysctl("hw.memsize")).isdigit():
        info["ram_gb"] = round(int(mem) / 1024 ** 3)
    if model := _sysctl("hw.model"):
        info["model"] = model
    return info


def hardware_summary() -> str:
    """Ringkasan spesifikasi perangkat dalam satu baris (deteksi lokal murni)."""
    system = platform.system()
    if system == "Windows":
        info = _win_hardware()
    elif system == "Darwin":
        info = _mac_hardware()
    else:
        info = _linux_hardware()
    parts: list[str] = []
    if info.get("model"):
        parts.append(info["model"])
    if info.get("cpu"):
        parts.append(f"CPU {info['cpu']} ({os.cpu_count() or '?'} thread)")
    if info.get("ram_gb"):
        parts.append(f"RAM {info['ram_gb']} GB")
    if info.get("gpu"):
        parts.append(f"GPU {info['gpu']}")
    try:
        total = shutil.disk_usage(Path.home().anchor or "/").total
        parts.append(f"disk {total / 1024 ** 3:.0f} GB")
    except OSError:
        pass
    parts.append(f"arch {platform.machine()}")
    parts.append(f"Python {platform.python_version()}")
    return ", ".join(parts)


def sync_hardware_to_memory() -> str:
    """Simpan spesifikasi laptop/PC ke memory SEKALI saja, tanpa LLM.

    Bila fakta spesifikasi sudah ada di memory -> lewati TANPA mendeteksi
    (nol biaya). Belum ada -> deteksi lokal (instan) lalu simpan; AI memakainya
    saat relevan tanpa perlu bertanya/memanggil model. Return 'added'|'unchanged'.
    """
    try:
        if any(f.strip().lower().startswith(_HW_PREFIX.lower())
               for f in longmem.all_facts()):
            return "unchanged"
        hw = hardware_summary()
        if not hw:
            return "unchanged"
        return longmem.upsert(
            _HW_PREFIX,
            f"{_HW_PREFIX} {hw}. Gunakan saat relevan (kompatibilitas, saran "
            "performa/kapasitas) tanpa bertanya lagi; untuk nilai yang "
            "berubah-ubah (sisa disk/RAM terpakai) cek langsung lewat perintah.",
        )
    except Exception:
        return "unchanged"
