"""Probe tombol: lihat kode yang DIKIRIM terminal untuk tiap penekanan.

Jalankan:  python keyprobe.py
Lalu tekan berurutan (jeda sedikit tiap tombol):
  1) Backspace  (polos)
  2) Ctrl+Backspace
  3) Ctrl+W
  4) Alt+Backspace
Terakhir tekan  Enter  untuk keluar, lalu kirim SEMUA output ke saya.
"""
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys


kb = KeyBindings()


@kb.add(Keys.Any)
def _any(event):
    for kp in event.key_sequence:
        data_hex = " ".join(hex(ord(c)) for c in (kp.data or ""))
        print(f"   key = {str(kp.key):<18} data = {data_hex or '(kosong)'}")


@kb.add("enter")
def _enter(event):
    event.app.exit()


def main() -> None:
    print(__doc__)
    print("=" * 60)
    try:
        PromptSession(key_bindings=kb).prompt("probe> ")
    except (KeyboardInterrupt, EOFError):
        pass
    print("=" * 60)
    print("Selesai. Salin & kirim semua baris 'key = ... data = ...' di atas.")


if __name__ == "__main__":
    main()
