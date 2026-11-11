"""Perintah aktif untuk daftar cepat di kedua antarmuka terminal.

Perintah lanjutan yang masih didukung tersedia lewat /help semua.
Boolean terakhir menyatakan bahwa Tab melengkapi perintah dengan spasi
agar pengguna bisa langsung menambahkan argumen.
"""

QUICK_COMMANDS = (
    ("/help", "Bantuan", False),
    ("/model", "Pilih model", True),
    ("/effort", "Atur usaha berpikir", True),
    ("/theme", "Pilih tema", True),
    ("/new", "Mulai percakapan baru", False),
    ("/clear", "Bersihkan layar", False),
    ("/btw", "Obrolan sampingan", True),
    ("/voice", "Percakapan suara", False),
    ("/mic", "Bacakan jawaban", True),
    ("/image", "Baca gambar lokal", True),
    ("/live", "Lihat jendela terpilih", False),
    ("/compact", "Ringkas konteks", False),
    ("/memory", "Kelola ingatan", True),
    ("/add-dir", "Tambah folder konteks", True),
    ("/review", "Periksa bug proyek", False),
    ("/export", "Ekspor riwayat", True),
    ("/exit", "Keluar", False),
)
