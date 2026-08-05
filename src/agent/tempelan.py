"""Tempelan panjang diringkas di kotak ketik — isinya TIDAK hilang.

Menempelkan log error 300 baris atau satu berkas utuh ke kotak chat itu hal
yang lumrah, dan sebelum ini akibatnya selalu sama: kotak ketiknya membengkak
menutupi seluruh layar, riwayat percakapan terdorong hilang, dan kalimat yang
sedang ditulis pengguna sendiri tak kelihatan lagi. Menggulung ke atas untuk
membaca apa yang barusan dikerjakan AI jadi mustahil sampai pesannya dikirim.

Yang dilakukan di sini: teks tempelan disimpan utuh di samping, dan yang
tampil di kotak cuma SATU baris penanda —

    [tempelan #1 · 312 baris · 14,2 KB]

Penandanya bisa dipindah, disalin, atau dihapus seperti teks biasa, dan bisa
diselingi kalimat sendiri ("perbaiki error ini: [tempelan #1 …] pakai cara
yang tadi"). Tepat sebelum pesannya dikirim, tiap penanda ditukar kembali
dengan isi aslinya — jadi yang diterima AI persis seperti yang ditempel, huruf
per huruf. Penanda yang dihapus pengguna berarti tempelannya memang tak jadi
dipakai; itu keputusan yang sah, dan isinya ikut dibuang.

Ambang panjangnya sengaja tak ketat: tempelan pendek (satu baris perintah,
sepotong URL) jauh lebih enak dilihat apa adanya, dan meringkasnya cuma
menambah satu lapis yang harus dibaca pengguna tanpa memberi apa pun.
"""
from __future__ import annotations

import re
import threading

# Ambang: tempelan diringkas kalau MELEBIHI salah satu batas ini. Dua-duanya
# dipakai karena bentuk tempelan yang mengganggu ada dua macam — banyak baris
# (log, berkas kode) dan satu baris yang sangat panjang (JSON, base64).
_MIN_BARIS = 4
_MIN_HURUF = 320
# Berapa banyak tempelan yang ditahan di memori sekaligus.
_MAKS_SIMPAN = 24

# Penanda yang tampil di kotak. Kurung siku dipilih (bukan kurung kurawal atau
# tanda kurung biasa) supaya tak tertukar dengan sintaks apa pun yang lazim
# ditempel orang, dan bentuknya tetap terbaca kalau ikut tersalin ke tempat
# lain.
_POLA = re.compile(r"\[tempelan #(\d+) · [^\]]*\]")


def _ukuran(n: int) -> str:
    """Ukuran teks dalam satuan yang enak dibaca sekilas."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB".replace(".", ",")
    return f"{n / (1024 * 1024):.1f} MB".replace(".", ",")


class Tempelan:
    """Simpanan tempelan untuk satu kotak ketik.

    Dipakai dari dua tempat (kotak idle & ketikan saat giliran berjalan) yang
    hidup di thread berbeda, jadi simpanannya dikunci."""

    def __init__(self) -> None:
        self._isi: dict[int, str] = {}
        self._urut = 0
        self._kunci = threading.Lock()

    # ---- dipakai saat menempel ----
    def perlu_diringkas(self, teks: str) -> bool:
        """Tempelan sepanjang ini pantas diringkas?"""
        if not teks:
            return False
        return len(teks) > _MIN_HURUF or teks.count("\n") >= _MIN_BARIS

    def simpan(self, teks: str) -> str:
        """Simpan isinya, kembalikan penanda satu baris untuk ditampilkan."""
        with self._kunci:
            self._urut += 1
            nomor = self._urut
            self._isi[nomor] = teks
            # Yang paling lama disimpan dilepas begitu melewati batas: sesi
            # panjang yang berkali-kali menempel berkas besar tak boleh
            # menahan semuanya di memori sampai bagas-ai ditutup. Batasnya
            # jauh lebih besar daripada jumlah penanda yang masuk akal ada di
            # satu pesan, jadi tak ada penanda hidup yang bisa terkena.
            while len(self._isi) > _MAKS_SIMPAN:
                self._isi.pop(min(self._isi))
        # Baris-baru DI UJUNG tak menambah baris: hampir semua tempelan dari
        # editor & terminal berakhir dengan satu, dan menghitungnya membuat
        # angkanya selalu meleset satu dari yang dilihat pengguna di sumbernya.
        inti = teks[:-1] if teks.endswith("\n") else teks
        baris = inti.count("\n") + 1
        return f"[tempelan #{nomor} · {baris} baris · {_ukuran(len(teks))}]"

    # ---- dipakai saat mengirim ----
    def kembangkan(self, teks: str) -> str:
        """Tukar tiap penanda dengan isi aslinya.

        Penanda yang isinya sudah tak ada (mis. sisa salinan dari kotak
        sebelumnya) dibiarkan apa adanya — lebih baik AI melihat penanda yang
        aneh daripada kalimat pengguna diam-diam kehilangan sepotong."""
        if not teks or "[tempelan #" not in teks:
            return teks

        def ganti(m: re.Match) -> str:
            with self._kunci:
                isi = self._isi.get(int(m.group(1)))
            return m.group(0) if isi is None else isi

        return _POLA.sub(ganti, teks)

    def bersihkan(self) -> None:
        """Lupakan semua tempelan. Hanya untuk pengujian & saat keluar.

        SENGAJA tak dipanggil sesudah pesan terkirim: penanda bisa saja masih
        menunggu di kotak ketik yang satunya (ketikan yang belum di-Enter saat
        giliran berjalan), dan mengosongkan simpanan di titik itu membuat
        penanda tersebut kehilangan isinya tanpa satu pun tanda. Pembatasan
        memorinya dilakukan lewat _MAKS_SIMPAN, bukan lewat pengosongan."""
        with self._kunci:
            self._isi.clear()

    def ada(self) -> int:
        with self._kunci:
            return len(self._isi)


# Satu simpanan untuk seluruh program: kedua kotak ketik (idle & saat giliran
# berjalan) menyerahkan ketikannya ke antrean yang sama, jadi penanda yang
# dibuat di satu kotak harus bisa dikembangkan di kotak lain.
_TEMPELAN = Tempelan()


def simpanan() -> Tempelan:
    return _TEMPELAN
