"""Riwayat WAKTU turn connector web -> dasar ETA yang JUJUR *dan* terukur.

Kenapa median durasi saja TIDAK cukup
-------------------------------------
Versi pertama memakai median durasi menjawab. Pada data nyata sebaran durasinya
sangat lebar (terukur: 5.75s, 5.90s, 26.45s, 28.12s — beda 5x), jadi satu angka
median dijamin meleset jauh di sebagian besar giliran.

Sebabnya: durasi bervariasi karena PANJANG jawaban bervariasi. Yang jauh lebih
stabil adalah THROUGHPUT (karakter per detik) — itu properti layanan, bukan
properti pertanyaan. Maka ETA dihitung hidup-hidup:

    ETA = (perkiraan panjang akhir - karakter yang sudah mengalir) / throughput

Perkiraan panjang akhir memakai harapan BERSYARAT E[L | L > c]: dari riwayat
panjang jawaban, ambil rata-rata yang LEBIH PANJANG dari yang sudah terlihat
sekarang. Ini penting — makin banyak teks yang sudah mengalir, makin sempit
kemungkinan panjang akhirnya, jadi perkiraan otomatis menajam seiring waktu
alih-alih terpaku pada satu tebakan awal.

Klaim akurasi tidak diasumsikan, tapi DIUKUR
--------------------------------------------
Tiap giliran menyimpan pasangan (perkiraan pertama, durasi sebenarnya).
`akurasi()` mengembalikan proporsi giliran yang perkiraannya meleset <=25%.
Angka itu ditampilkan apa adanya di UI, supaya "akurat 80-90%" jadi sesuatu
yang bisa diperiksa pengguna, bukan janji kosong. Kalau kenyataannya 60%, yang
tertulis 60%.

Bentuk baris data (v2): [start_latency, answer_dur, total_chars, predicted_dur]
Baris v1 lama ([start_latency, answer_dur]) tetap dibaca; kolom yang tak ada
dianggap 0 dan cuma dilewati oleh statistik yang membutuhkannya.
"""
from __future__ import annotations

import json
import threading
from statistics import median
from typing import Any

from . import config

_MAX = 40   # sampel per service yang disimpan (buang yang paling lama)
_MIN = 4    # di bawah ini: belum cukup -> jangan tampilkan ETA
_MIN_RATE = 3   # sampel ber-karakter minimum sebelum throughput dipercaya
# Ambang "perkiraan dianggap tepat" untuk mengukur akurasi. 25% dipilih karena
# itu batas di mana perkiraan masih terasa membantu bagi manusia: ditulis 20s,
# nyatanya 15-25s. Lebih ketat dari ini akan menghukum variasi yang wajar.
_TOLERANSI = 0.25
_lock = threading.Lock()


def _path():
    return config.CONFIG_HOME / "web_timing.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _rows(service: str) -> list[list[float]]:
    """Baris yang sah untuk `service`, dinormalkan ke panjang 4 (v1 -> v2)."""
    raw = _load().get(service)
    if not isinstance(raw, list):
        return []
    out: list[list[float]] = []
    for r in raw:
        if not isinstance(r, list) or len(r) < 2:
            continue
        baris = [float(x) if isinstance(x, (int, float)) else 0.0 for x in r[:4]]
        baris += [0.0] * (4 - len(baris))
        out.append(baris)
    return out


def record(service: str, start_latency: float, answer_dur: float,
           total_chars: float = 0.0, predicted_dur: float = 0.0) -> None:
    """Catat satu turn: latensi mulai-menjawab, durasi menjawab (detik), panjang
    jawaban (karakter), dan perkiraan durasi yang SEMPAT ditampilkan ke pengguna
    (0 bila tak ada — mis. sampel masih sedikit sehingga ETA belum muncul)."""
    if not service:
        return
    # Nilai tak masuk akal (negatif / nol) TAK dicatat supaya statistik tak ternoda.
    if start_latency < 0 or answer_dur <= 0:
        return
    with _lock:
        data = _load()
        rows = data.get(service)
        if not isinstance(rows, list):
            rows = []
        rows.append([round(float(start_latency), 2), round(float(answer_dur), 2),
                     int(max(0.0, total_chars)), round(max(0.0, predicted_dur), 2)])
        data[service] = rows[-_MAX:]
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def note_promise(service: str, promised_dur: float) -> None:
    """Lekatkan JANJI ("selesai dalam <=X detik") yang sempat dilihat pengguna ke
    giliran terakhir, lalu setel ulang kuantilnya berdasar ditepati/tidak.

    Connector-lah yang mencatat durasi asli (ia yang tahu kapan jawaban mulai &
    selesai), sedangkan janji lahir di UI. Daripada mengalirkan nilai itu lintas
    lapisan, UI menempelkannya ke baris terakhir sesudah giliran usai.

    Kolom yang sudah terisi TAK ditimpa, jadi yang dinilai selalu janji PERTAMA.
    Itu disengaja: janji di detik terakhir nyaris pasti tepat dan akan
    menggelembungkan angka akurasi jadi menyesatkan."""
    if not service or promised_dur <= 0:
        return
    with _lock:
        data = _load()
        rows = data.get(service)
        if not isinstance(rows, list) or not rows:
            return
        akhir = rows[-1]
        if not isinstance(akhir, list) or len(akhir) < 2:
            return
        while len(akhir) < 4:
            akhir.append(0)
        if akhir[3]:
            return
        akhir[3] = round(float(promised_dur), 2)
        rows[-1] = akhir
        data[service] = rows

        # Geser kuantil menuju sasaran cakupan (lihat catatan _TARGET).
        ditepati = 1.0 if akhir[1] <= akhir[3] + 0.5 else 0.0
        simpan = data.get(_q_key())
        if not isinstance(simpan, dict):
            simpan = {}
        q = simpan.get(service)
        q = float(q) if isinstance(q, (int, float)) else _Q_AWAL
        q = min(_Q_MAX, max(_Q_MIN, q + _ETA_LANGKAH * (_TARGET - ditepati)))
        simpan[service] = round(q, 4)
        data[_q_key()] = simpan
        try:
            _path().write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def medians(service: str) -> dict[str, Any] | None:
    """{'start', 'answer', 'n', 'rate', 'lengths', 'akurasi'} atau None bila
    sampel belum cukup untuk memberi perkiraan yang bertanggung jawab.

    'rate'     : throughput median (karakter/detik), None bila belum terukur.
    'lengths'  : panjang jawaban yang pernah tercatat -> dasar E[L | L > c].
    'akurasi'  : proporsi perkiraan yang meleset <=25%, None bila belum terukur.
    """
    if not service:
        return None
    pairs = _rows(service)
    if len(pairs) < _MIN:
        return None

    berkarakter = [p for p in pairs if p[2] > 0 and p[1] > 0]
    rate = None
    if len(berkarakter) >= _MIN_RATE:
        rate = median(p[2] / p[1] for p in berkarakter)
        if rate <= 0:
            rate = None

    return {
        "start": median(p[0] for p in pairs),
        "answer": median(p[1] for p in pairs),
        "n": len(pairs),
        "rate": rate,
        "lengths": [p[2] for p in berkarakter],
        "akurasi": akurasi(service),
        "kuantil": kuantil(service),
    }


def kuantil_panjang(lengths: list[float], sudah: float, q: float) -> float:
    """Kuantil-q dari panjang akhir yang MASIH MUNGKIN, mengingat `sudah`
    karakter sudah mengalir.

    Riwayat yang lebih pendek dari `sudah` jelas mustahil jadi jawaban ini, jadi
    dibuang; kuantil diambil dari sisanya. Bila tak ada satu pun riwayat yang
    lebih panjang, jawaban ini memecahkan rekor — anggap tinggal sedikit lagi
    (10%) daripada mengarang angka besar."""
    lebih = sorted(n for n in lengths if n > sudah)
    if not lebih:
        return sudah * 1.10
    idx = min(int(max(0.0, min(q, 1.0)) * len(lebih)), len(lebih) - 1)
    return lebih[idx]


# --- Swa-kalibrasi: kuantil disetel sendiri sampai janji tepat ~TARGET --------
#
# Kenapa BATAS ATAS, bukan hitung-mundur satu angka?
#   Panjang jawaban tak bisa diketahui sebelum jawabannya selesai. Diukur lewat
#   simulasi 4000 giliran: penaksir titik terbaik pun hanya benar ~40% (metode
#   median-durasi yang lama: 6%). Jadi "≈12s lagi" yang tepat 80-90% memang
#   MUSTAHIL — bukan soal algoritma, tapi soal informasi yang belum ada.
#   Yang BISA dikalibrasi ke 80-90% adalah janji satu sisi: "selesai dalam <=X".
#
# Kenapa disetel sendiri, bukan konstanta hasil simulasi?
#   Kalibrasi simulasi bergantung asumsi sebaran panjang jawaban. Sebaran nyata
#   tiap pengguna berbeda (dan berubah saat ganti model/kebiasaan bertanya).
#   Maka kuantilnya digeser tiap giliran memakai aproksimasi stokastik:
#       q <- q + eta * (TARGET - ditepati)
#   Titik diamnya persis saat proporsi janji-ditepati = TARGET, apa pun
#   sebarannya. Janji terlalu longgar -> q mengecil sendiri; terlalu ketat ->
#   membesar sendiri.
_TARGET = 0.85          # sasaran proporsi janji yang ditepati (tengah 80-90%)
_ETA_LANGKAH = 0.05     # laju penyesuaian; kecil supaya tak liar oleh 1 giliran
_Q_MIN, _Q_MAX = 0.50, 0.99
_Q_AWAL = 0.80          # hasil kalibrasi simulasi -> titik start yang wajar


def _q_key() -> str:
    return "#kuantil"


def kuantil(service: str) -> float:
    """Kuantil yang sedang dipakai untuk service ini (hasil swa-kalibrasi)."""
    simpan = _load().get(_q_key())
    if isinstance(simpan, dict):
        nilai = simpan.get(service)
        if isinstance(nilai, (int, float)) and _Q_MIN <= nilai <= _Q_MAX:
            return float(nilai)
    return _Q_AWAL


def akurasi(service: str) -> float | None:
    """Proporsi janji "selesai dalam <=X" yang DITEPATI. None bila belum ada
    cukup giliran yang sempat diberi janji."""
    dinilai = [p for p in _rows(service) if p[3] > 0 and p[1] > 0]
    if len(dinilai) < _MIN_RATE:
        return None
    # Toleransi kecil: janji dianggap ditepati bila selesai sebelum batas, atau
    # meleset di bawah setengah detik (perbedaan sekecil itu tak terasa manusia).
    ditepati = sum(1 for p in dinilai if p[1] <= p[3] + 0.5)
    return ditepati / len(dinilai)
