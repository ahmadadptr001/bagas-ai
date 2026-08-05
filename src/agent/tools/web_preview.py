"""Tool pratinjau halaman web — menutup loop "tulis kode UI → LIHAT hasilnya".

web_preview membuka sebuah URL (paling sering dev server localhost) di browser
Chromium HEADLESS milik Playwright — dependensi yang memang sudah dibawa
bagas-ai untuk connector — lalu mengembalikan status HTTP, judul, error
console/JS/permintaan-gagal, dan SCREENSHOT halamannya. Penanda [GAMBAR] pada
hasil membuat core melampirkan PNG-nya ke pesan berikutnya sehingga model
benar-benar MELIHAT tampilan yang ia bangun, bukan menebak dari kode.

Berbeda dari take_screenshot (memotret layar/desktop pengguna): ini merender
URL-nya sendiri, jadi bekerja tanpa perlu browser user terbuka di halaman itu.
Instance Playwright-nya terpisah dari hub connector (dibuat & ditutup per
panggilan) — tak mengganggu sesi chat web yang sedang berjalan.
"""
from __future__ import annotations

import re
import time

from .base import tool
from .files import _display, _safe_path
from .screen import IMAGE_MARK

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_SC_DIR = "screenshots"
# Batas jumlah error per kategori yang dilaporkan (halaman rusak bisa memuntahkan
# ratusan error identik — sisanya cuma bising).
_MAX_ERR = 8

# --- memastikan halaman BENAR-BENAR siap sebelum dipotret -------------------
#
# `wait_until="load"` + jeda tetap beberapa detik ternyata tak cukup: pada SPA
# (Next.js/Vite dev) event `load` menyala SEBELUM hidrasi, pengambilan data, dan
# pemuatan font selesai. Yang terpotret jadi kerangka kosong atau teks tanpa
# gaya — lalu model menilai "tampilannya sudah benar" atas dasar gambar yang
# bukan tampilan sebenarnya. Kesalahan seperti itu lebih buruk daripada tak
# memotret sama sekali, karena ia terlihat seperti bukti.
#
# `networkidle` sengaja TIDAK dipakai: dev server memakai websocket HMR yang
# nyaris tak pernah diam, jadi ia kerap habis waktu di halaman yang sebenarnya
# sudah siap.
#
# Gantinya dua lapis:
#   1. tunggu hal yang bisa DITANYAKAN ke halaman — font siap, gambar selesai,
#      dan tak ada penanda memuat yang masih terlihat;
#   2. tunggu sampai TAMPILANNYA BERHENTI BERUBAH: potret berkali-kali sampai
#      dua potret berturut-turut identik. Lapis kedua ini tak peduli framework
#      apa pun yang dipakai — ia menilai dari hasil akhirnya, bukan dari janji
#      pustaka.
_JS_SIAP = """() => {
  const fonts = !document.fonts || document.fonts.status === 'loaded';
  const imgs = Array.from(document.images || []);
  const gambar = imgs.every(i => i.complete);
  // Penanda "sedang memuat" yang lazim di banyak framework/komponen UI.
  const memuat = document.querySelector(
    '[aria-busy="true"], .skeleton, .animate-pulse, [data-loading="true"], ' +
    '.loading, .spinner, .MuiSkeleton-root');
  const terlihat = memuat && memuat.offsetParent !== null;
  return {fonts, gambar, memuat: !!terlihat,
          teks: (document.body ? document.body.innerText.trim().length : 0)};
}"""
_BATAS_SIAP_MS = 6000        # total penantian kesiapan
_JEDA_STABIL_MS = 350        # jarak antar potret pembanding
_MAKS_POTRET = 8             # 8 x 350ms ~ 2,8 dtk sebelum menyerah


def _tunggu_siap(page) -> dict:
    """Tunggu font/gambar/penanda-memuat beres. Kembalikan keadaan terakhir."""
    batas = time.time() + _BATAS_SIAP_MS / 1000
    keadaan = {}
    while time.time() < batas:
        try:
            keadaan = page.evaluate(_JS_SIAP) or {}
        except Exception:  # noqa: BLE001 - halaman bisa navigasi di tengah cek
            return keadaan
        if keadaan.get("fonts") and keadaan.get("gambar") \
                and not keadaan.get("memuat") and keadaan.get("teks", 0) > 0:
            return keadaan
        page.wait_for_timeout(200)
    return keadaan


# --- memeriksa RESPONSIF: apa yang meluber di layar sempit ------------------
#
# "Responsif" tak bisa dinilai dari gambar saja — di gambar, isi yang meluber
# ke kanan justru TERPOTONG, jadi tampak seolah tak ada masalah. Yang
# menentukan ada di angka: dokumen lebih lebar dari viewport, dan elemen mana
# yang menyebabkannya. Tanpa nama elemennya, "tidak responsif" cuma keluhan
# yang tak bisa ditindaklanjuti.
_JS_LUBER = """() => {
  const W = document.documentElement.clientWidth;
  const out = [];
  const seen = new Set();
  for (const e of document.querySelectorAll('body *')) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right <= W + 1 && r.left >= -1) continue;
    // Anak yang meluber karena induknya meluber tak perlu dilaporkan dua kali.
    let induk = e.parentElement, lewati = false;
    while (induk && induk !== document.body) {
      if (seen.has(induk)) { lewati = true; break; }
      induk = induk.parentElement;
    }
    if (lewati) continue;
    seen.add(e);
    const cls = (e.className || '').toString().trim().split(/\\s+/)
                  .slice(0, 3).join('.');
    out.push({
      tag: e.tagName.toLowerCase(),
      id: e.id || '',
      cls: cls,
      lebar: Math.round(r.width),
      kanan: Math.round(r.right),
      teks: (e.innerText || '').trim().slice(0, 40),
    });
    if (out.length >= 6) break;
  }
  return {
    lebarDok: Math.round(document.documentElement.scrollWidth),
    lebarLayar: W,
    geserMendatar: document.documentElement.scrollWidth > W + 1,
    pelanggar: out,
  };
}"""


def _teks_halaman(page) -> list[str]:
    """Baris teks yang TERLIHAT di halaman (untuk membandingkan sebelum/sesudah)."""
    try:
        teks = page.evaluate(
            "() => document.body ? document.body.innerText : ''") or ""
    except Exception:  # noqa: BLE001
        return []
    return [b.strip() for b in teks.splitlines() if b.strip()]


def _teks_baru(sebelum: list[str], sesudah: list[str], maks: int = 6) -> list[str]:
    """Baris yang BARU muncul sesudah halaman disentuh.

    Dibandingkan sebagai himpunan, bukan urutan: yang dicari "apa yang muncul",
    dan baris yang cuma berpindah posisi bukan hasil yang menarik."""
    lama = set(sebelum)
    keluar: list[str] = []
    for b in sesudah:
        if b not in lama and b not in keluar:
            keluar.append(b)
            if len(keluar) >= maks:
                break
    return keluar


def _periksa_luber(page) -> dict:
    try:
        return page.evaluate(_JS_LUBER) or {}
    except Exception:  # noqa: BLE001 - halaman bisa navigasi di tengah cek
        return {}


def _potret_stabil(page, target, full_page: bool) -> tuple[bool, float]:
    """Potret berulang sampai dua hasil berturut-turut identik.

    Kembalikan (stabil, detik_yang_dihabiskan). Berkas terakhir yang ditulis
    adalah yang dipakai — jadi walau tak pernah stabil, yang tersimpan tetap
    keadaan TERBARU, bukan yang paling basi."""
    mulai = time.time()
    sebelumnya = None
    for _ in range(_MAKS_POTRET):
        data = page.screenshot(path=str(target), full_page=full_page)
        if sebelumnya is not None and data == sebelumnya:
            return True, time.time() - mulai
        sebelumnya = data
        page.wait_for_timeout(_JEDA_STABIL_MS)
    return False, time.time() - mulai


# --- MENGOTAK-ATIK halaman: isi, klik, gulir, tunggu ------------------------
#
# Memotret saja hanya membuktikan tampilan DIAM. Sebagian besar yang diminta
# pengguna justru soal apa yang terjadi SESUDAH disentuh: form diisi lalu
# menghasilkan sesuatu, menu dibuka, tab diganti, tombol ditekan lalu muncul
# pesan galat. Tanpa jalan masuk ke halaman, model cuma bisa menduga bagian itu
# dari kode — dan dugaan yang terdengar yakin adalah cara paling umum
# "sudah kuperbaiki" ternyata tak benar.
#
# Aksinya sengaja berupa KALIMAT PENDEK, bukan JSON bersarang: model menulisnya
# lewat protokol teks, dan tiap tingkat kurung tambahan menambah peluang
# blok rusak. Bentuk dict tetap diterima untuk yang lebih suka begitu.
_AKSI_BANTUAN = (
    "klik <selector> · isi <selector> = <teks> · ketik <selector> = <teks> · "
    "pilih <selector> = <nilai> · centang <selector> · lepas <selector> · "
    "hover <selector> · tekan <tombol> · gulir <px|selector> · "
    "tunggu <ms|selector> · buka <url> · potret <nama>"
)
_MAKS_AKSI = 20
_MAKS_LEBAR = 4


def _belah_nilai(sisa: str) -> tuple[str, str]:
    """Pisahkan "<selector> = <nilai>" dengan MENGHORMATI kurung siku.

    Selector CSS lazim memuat "=" di dalam kurung siku, dan memotong di "="
    yang pertama merusaknya. TERBUKTI saat diuji: "klik button[type=submit]"
    terpotong jadi selector "button[type" — Playwright lalu menolaknya dengan
    galat parser CSS yang sama sekali tak menyinggung sebab sebenarnya."""
    dalam = 0
    for i, ch in enumerate(sisa):
        if ch == "[":
            dalam += 1
        elif ch == "]":
            dalam = max(0, dalam - 1)
        elif ch == "=" and dalam == 0:
            return sisa[:i].strip(), sisa[i + 1:].strip()
    return sisa.strip(), ""


def _pisah_aksi(a) -> tuple[str, str, str]:
    """Ubah satu aksi (str ATAU dict) jadi (kerja, sasaran, nilai)."""
    if isinstance(a, dict):
        kerja = str(a.get("kerja") or a.get("action") or a.get("do") or "").strip()
        sasaran = str(a.get("selector") or a.get("sasaran")
                      or a.get("target") or "").strip()
        nilai = str(a.get("nilai") or a.get("value") or a.get("text") or "").strip()
        return kerja.lower(), sasaran, nilai
    teks = str(a or "").strip()
    if not teks:
        return "", "", ""
    kerja, _, sisa = teks.partition(" ")
    sasaran, nilai = _belah_nilai(sisa)
    return kerja.lower(), sasaran, nilai


# Sinonim: model menulis dalam dua bahasa dan tak selalu memilih kata yang sama.
# Menolak "click" karena kita menamainya "klik" hanya membuang satu langkah
# untuk hal yang maksudnya sudah jelas.
_SINONIM = {
    "click": "klik", "tap": "klik", "press": "tekan", "key": "tekan",
    "fill": "isi", "set": "isi", "type": "ketik", "select": "pilih",
    "check": "centang", "uncheck": "lepas", "scroll": "gulir",
    "wait": "tunggu", "goto": "buka", "navigate": "buka", "open": "buka",
    "screenshot": "potret", "snap": "potret", "shot": "potret",
}


def _jalankan_aksi(page, daftar, potret_cb) -> list[str]:
    """Jalankan aksi berurutan. Kembalikan catatan hasil TIAP aksi.

    Aksi yang gagal TIDAK menghentikan sisanya, tapi selalu dicatat apa
    adanya: rangkaian yang berhenti diam-diam di tengah menghasilkan gambar
    keadaan setengah jadi yang tak bisa dibedakan dari hasil yang benar."""
    catatan: list[str] = []
    for i, a in enumerate(daftar[:_MAKS_AKSI], 1):
        kerja, sasaran, nilai = _pisah_aksi(a)
        kerja = _SINONIM.get(kerja, kerja)
        if not kerja:
            continue
        label = f"{i}. {kerja} {sasaran}{(' = ' + nilai) if nilai else ''}".strip()
        try:
            if kerja == "klik":
                page.click(sasaran, timeout=8000)
            elif kerja == "isi":
                page.fill(sasaran, nilai, timeout=8000)
            elif kerja == "ketik":
                page.click(sasaran, timeout=8000)
                page.type(sasaran, nilai, delay=25, timeout=8000)
            elif kerja == "pilih":
                page.select_option(sasaran, nilai, timeout=8000)
            elif kerja == "centang":
                page.check(sasaran, timeout=8000)
            elif kerja == "lepas":
                page.uncheck(sasaran, timeout=8000)
            elif kerja == "hover":
                page.hover(sasaran, timeout=8000)
            elif kerja == "tekan":
                # "tekan Enter" (fokus sekarang) atau "tekan #form Enter".
                if nilai:
                    page.press(sasaran, nilai, timeout=8000)
                elif " " in sasaran:
                    sel, _, tombol = sasaran.rpartition(" ")
                    page.press(sel, tombol, timeout=8000)
                else:
                    page.keyboard.press(sasaran)
            elif kerja == "gulir":
                if sasaran.lstrip("-").isdigit():
                    page.mouse.wheel(0, int(sasaran))
                else:
                    page.locator(sasaran).first.scroll_into_view_if_needed(
                        timeout=8000)
            elif kerja == "tunggu":
                if sasaran.isdigit():
                    page.wait_for_timeout(min(int(sasaran), 15000))
                else:
                    page.wait_for_selector(sasaran, timeout=15000)
            elif kerja == "buka":
                page.goto(sasaran, wait_until="load", timeout=20000)
            elif kerja == "potret":
                nama = sasaran or nilai or f"langkah{i}"
                potret_cb(nama)
                catatan.append(f"✓ {label} → gambar '{nama}' diambil")
                continue
            else:
                catatan.append(
                    f"✗ {label} — aksi '{kerja}' tak dikenal. Yang ada: "
                    f"{_AKSI_BANTUAN}")
                continue
            catatan.append(f"✓ {label}")
        except Exception as exc:  # noqa: BLE001 - galat Playwright beragam
            pesan = str(exc).splitlines()[0][:200]
            # Selector yang tak ketemu adalah kegagalan paling sering, dan
            # penyebabnya hampir selalu sama: elemennya memang tak ada, atau
            # baru muncul sesudah sesuatu. Dikatakan langsung supaya model tak
            # mengulang aksi yang sama dengan harapan berbeda.
            if "Timeout" in pesan or "waiting for" in pesan:
                catatan.append(
                    f"✗ {label} — '{sasaran}' tak ditemukan/tak bisa disentuh "
                    "dalam 8 dtk. Periksa selectornya, atau sisipkan "
                    f"'tunggu <selector>' lebih dulu. ({pesan[:90]})")
            else:
                catatan.append(f"✗ {label} — {pesan}")
    if len(daftar) > _MAKS_AKSI:
        catatan.append(f"⚠ {len(daftar) - _MAKS_AKSI} aksi sisanya tak "
                       f"dijalankan (batas {_MAKS_AKSI} per panggilan).")
    return catatan


@tool
def web_preview(url: str, wait_seconds: float = 2.0, full_page: bool = False,
                actions: list | None = None, widths: list | None = None,
                width: int = 0, height: int = 0) -> str:
    """MASUK ke halaman web & otak-atik seperti pengguna sungguhan: buka URL (mis. localhost:3000) di browser headless, isi form, klik, gulir, uji di berbagai lebar layar, lalu dapatkan status HTTP, error console/JS, laporan elemen yang MELUBER, dan screenshot tiap tahap (otomatis terlampir). WAJIB dipakai sesudah mengubah kode UI — jangan mengaku tampilannya benar tanpa melihatnya.

    Server harus sudah berjalan (nyalakan lewat run_command_bg, mis.
    'npm run dev', tunggu siap via bg_output, baru panggil ini).

    url: alamat halaman (mis. 'http://localhost:3000' — 'localhost:3000' juga
        diterima, otomatis diberi http://).
    wait_seconds: jeda ekstra setelah halaman load (0-10 dtk, default 2) —
        beri waktu SPA/animasi selesai merender.
    full_page: true = screenshot seluruh panjang halaman, bukan cuma viewport.
    actions: daftar langkah yang dijalankan DI HALAMAN, berurutan. Tiap langkah
        satu kalimat pendek, mis.
        ["isi #email = a@b.com", "isi #sandi = rahasia", "klik button[type=submit]",
         "tunggu .hasil", "potret sesudah-kirim"].
        Yang tersedia: klik · isi · ketik · pilih · centang · lepas · hover ·
        tekan · gulir · tunggu · buka · potret. Pakai ini untuk menguji ALUR
        (form menghasilkan sesuatu, menu terbuka, tab berganti) — bukan cuma
        tampilan diamnya.
    widths: daftar lebar layar untuk uji RESPONSIF, mis. [360, 768, 1280].
        Tiap lebar dipotret sendiri DAN diperiksa elemen mana yang meluber
        keluar layar — hal yang tak kelihatan di gambar karena justru terpotong.
    width/height: ukuran layar tunggal (dipakai bila `widths` kosong).
    """
    u = (url or "").strip()
    if not u:
        return "[error] url kosong — contoh: web_preview('http://localhost:3000')"
    if not _URL_RE.match(u):
        u = "http://" + u
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("[GAGAL] Playwright belum terpasang — jalankan: "
                "pip install playwright && python -m playwright install chromium")

    try:
        wait = max(0.0, min(float(wait_seconds), 10.0))
    except (TypeError, ValueError):
        wait = 2.0
    # Lebar layar yang diuji. `widths` (jamak) untuk uji responsif; kalau kosong
    # dipakai satu lebar saja.
    daftar_lebar: list[int] = []
    for w in (widths or []):
        try:
            n = int(w)
        except (TypeError, ValueError):
            continue
        if 240 <= n <= 3840:
            daftar_lebar.append(n)
    daftar_lebar = daftar_lebar[:_MAKS_LEBAR]
    if not daftar_lebar:
        try:
            satu = int(width) or 1280
        except (TypeError, ValueError):
            satu = 1280
        daftar_lebar = [max(240, min(satu, 3840))]
    try:
        tinggi = int(height) or 800
    except (TypeError, ValueError):
        tinggi = 800
    tinggi = max(320, min(tinggi, 2160))

    cap = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
    dipakai: list = []          # (label, Path) tiap gambar yang jadi

    def _berkas(label: str):
        aman = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "preview"
        return _safe_path(f"{_SC_DIR}/preview-{cap}-{aman}.png")

    try:
        _berkas("cek").parent.mkdir(parents=True, exist_ok=True)
    except ValueError as e:
        return f"[GAGAL] {e}"

    # Disiapkan sebelum blok try supaya perakitan laporan di bawah tak pernah
    # bergantung pada apakah bagian dalam try sempat berjalan.
    siap: dict = {}
    stabil = True
    lama_stabil = 0.0
    console_err: list[str] = []
    js_err: list[str] = []
    req_gagal: list[str] = []
    status = None
    title = ""
    catatan_aksi: list[str] = []
    muncul: list[str] = []
    luber_per_lebar: list[tuple[int, dict]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, timeout=60000)
            try:
                page = browser.new_page(
                    viewport={"width": daftar_lebar[0], "height": tinggi})
                page.on("console", lambda m: console_err.append(
                    m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: js_err.append(str(e)))
                page.on("requestfailed", lambda r: req_gagal.append(
                    f"{r.url} ({r.failure or 'gagal'})"))
                resp = page.goto(u, wait_until="load", timeout=20000)
                if wait:
                    page.wait_for_timeout(int(wait * 1000))
                siap = _tunggu_siap(page)
                status = resp.status if resp is not None else None
                title = page.title() or ""

                # AKSI DIJALANKAN LEBIH DULU, sebelum satu gambar pun diambil:
                # yang ingin dilihat adalah keadaan SESUDAH halaman disentuh.
                # Gambar antara bisa diminta sendiri lewat aksi "potret".
                if actions:
                    def _potret_antara(nama: str) -> None:
                        f = _berkas(nama)
                        page.screenshot(path=str(f), full_page=bool(full_page))
                        dipakai.append((nama, f))

                    teks_sebelum = _teks_halaman(page)
                    catatan_aksi = _jalankan_aksi(
                        page, list(actions), _potret_antara)
                    # Apa yang BERUBAH di halaman sesudah disentuh. Gambar saja
                    # tak cukup: teks kecil sering tak terbaca di potret, dan
                    # justru teks itulah bukti bahwa alurnya benar-benar
                    # menghasilkan sesuatu ("Halo, Bagas!", "Email tidak
                    # valid", jumlah baris tabel yang bertambah).
                    muncul = _teks_baru(teks_sebelum, _teks_halaman(page))
                    # Sesudah disentuh, halaman kerap merender ulang — tunggu
                    # ia diam lagi supaya gambar akhirnya bukan keadaan transisi.
                    siap = _tunggu_siap(page)

                # Tiap lebar: pasang viewport, tunggu tata letaknya menyesuaikan,
                # potret, lalu periksa yang meluber.
                for i, lebar in enumerate(daftar_lebar):
                    if len(daftar_lebar) > 1 or lebar != daftar_lebar[0] or i:
                        page.set_viewport_size(
                            {"width": lebar, "height": tinggi})
                        # Media query & pengukur berbasis JS butuh satu tarikan
                        # napas sebelum tata letaknya benar-benar berubah.
                        page.wait_for_timeout(450)
                    f = _berkas(f"{lebar}px")
                    st, lm = _potret_stabil(page, f, bool(full_page))
                    dipakai.append((f"{lebar}px", f))
                    if i == 0:
                        stabil, lama_stabil = st, lm
                    luber_per_lebar.append((lebar, _periksa_luber(page)))
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - galat Playwright beragam & mentah
        pesan = str(exc).splitlines()[0] if str(exc) else repr(exc)
        if "ERR_CONNECTION_REFUSED" in pesan or "net::ERR" in pesan:
            return (f"[GAGAL] tak bisa membuka {u}: {pesan}\n"
                    "Kemungkinan servernya BELUM berjalan. Nyalakan dulu "
                    "(run_command_bg, mis. 'npm run dev'), pastikan siap lewat "
                    "bg_output, lalu panggil web_preview lagi.")
        if "Executable doesn't exist" in pesan or "playwright install" in pesan:
            return ("[GAGAL] browser Chromium Playwright belum terunduh — "
                    "jalankan: python -m playwright install chromium")
        if "Timeout" in pesan or "timeout" in pesan:
            return (f"[GAGAL] {u} tidak selesai dimuat dalam 20 detik: {pesan}\n"
                    "Server lambat/menggantung? Cek lognya via bg_output.")
        return f"[GAGAL] web_preview {u}: {pesan}"

    baris = [
        f"web_preview {u} — HTTP {status if status is not None else '?'} · "
        f"judul: {title or '(tanpa judul)'}"
    ]
    if status is not None and status >= 400:
        baris.append(f"⚠ Server membalas ERROR {status} — halaman ini bukan "
                     "tampilan yang diinginkan. Periksa log servernya.")

    def _bagian(nama: str, items: list[str]) -> None:
        if not items:
            return
        tampil = items[:_MAX_ERR]
        lebih = f"\n  … {len(items) - _MAX_ERR} lagi" if len(items) > _MAX_ERR else ""
        baris.append(f"{nama} ({len(items)}):\n  " + "\n  ".join(
            t.replace("\n", " ")[:300] for t in tampil) + lebih)

    # Hasil TIAP aksi dilaporkan, yang berhasil maupun yang gagal. Aksi yang
    # gagal diam-diam adalah cara paling mudah menyimpulkan hal yang salah:
    # gambar sesudahnya tampak wajar, padahal tombolnya tak pernah tertekan.
    if catatan_aksi:
        baris.append("Langkah di halaman:\n  " + "\n  ".join(catatan_aksi))
        if muncul:
            baris.append("Yang MUNCUL di halaman sesudah langkah di atas:\n  "
                         + "\n  ".join(f"“{m[:120]}”" for m in muncul))
        else:
            baris.append(
                "⚠ Tak ada teks BARU yang muncul sesudah langkah di atas. "
                "Kalau alurnya memang seharusnya menghasilkan sesuatu (pesan, "
                "hasil, baris tabel), berarti ia TIDAK terjadi — periksa "
                "handler-nya, jangan simpulkan dari gambarnya saja.")
        gagal = sum(1 for c in catatan_aksi if c.startswith("✗"))
        if gagal:
            baris.append(
                f"⚠ {gagal} langkah GAGAL — keadaan halaman di gambar bukan "
                "hasil alur yang kamu maksud. Betulkan selectornya dulu "
                "(pakai read_file pada komponennya untuk melihat id/class yang "
                "sebenarnya) sebelum menilai tampilannya.")

    _bagian("✗ Error console", console_err)
    _bagian("✗ Error JavaScript (uncaught)", js_err)
    _bagian("⚠ Permintaan gagal", req_gagal)
    if not (console_err or js_err):
        baris.append("✓ Tidak ada error console/JS.")

    # RESPONSIF: yang menentukan bukan gambarnya (isi yang meluber justru
    # terpotong di gambar, jadi tampak baik-baik saja) melainkan angka lebar
    # dokumen vs lebar layar, plus nama elemen penyebabnya.
    for lebar, luber in luber_per_lebar:
        if not luber:
            continue
        if luber.get("geserMendatar"):
            rinci = []
            for e in luber.get("pelanggar", []):
                nama = e.get("tag", "?")
                if e.get("id"):
                    nama += f"#{e['id']}"
                elif e.get("cls"):
                    nama += f".{e['cls']}"
                ket = f" \"{e['teks']}\"" if e.get("teks") else ""
                rinci.append(f"{nama} (lebar {e.get('lebar')}px, tepi kanan "
                             f"{e.get('kanan')}px){ket}")
            baris.append(
                f"✗ RESPONSIF {lebar}px: isi MELUBER — dokumen "
                f"{luber.get('lebarDok')}px di layar {luber.get('lebarLayar')}px "
                f"(muncul scroll mendatar).\n  Penyebabnya: "
                + ("\n  ".join(rinci) if rinci
                   else "tak terlacak ke satu elemen tertentu."))
        else:
            baris.append(f"✓ RESPONSIF {lebar}px: tak ada yang meluber.")

    # KEADAAN KESIAPAN dilaporkan apa adanya. Tanpa ini, gambar halaman yang
    # belum jadi tak bisa dibedakan dari gambar halaman yang memang begitu
    # bentuknya — dan model akan menilai tampilan atas dasar bukti palsu.
    if not stabil:
        baris.append(
            f"⚠ Tampilan MASIH BERUBAH setelah {lama_stabil:.1f} dtk — gambar "
            "ini "
            "kemungkinan menangkap halaman yang belum selesai merender. "
            "Jangan menyimpulkan tampilannya dari sini: tunggu sebentar lalu "
            "web_preview lagi (naikkan wait_seconds), atau periksa apakah ada "
            "data yang tak pernah selesai dimuat.")
    if siap and not siap.get("fonts", True):
        baris.append("⚠ Font belum selesai dimuat — huruf di gambar mungkin "
                     "bukan huruf yang sebenarnya.")
    if siap and siap.get("memuat"):
        baris.append("⚠ Masih ada penanda memuat (skeleton/spinner) yang "
                     "terlihat — isinya belum lengkap.")
    if siap and not siap.get("gambar", True):
        baris.append("⚠ Sebagian <img> belum selesai dimuat.")
    if stabil and siap and siap.get("fonts", True) \
            and not siap.get("memuat") and siap.get("gambar", True):
        baris.append(f"✓ Halaman stabil & siap ({lama_stabil:.1f} dtk).")

    if dipakai:
        baris.append("Screenshot:\n  " + "\n  ".join(
            f"[{label}] {_display(f)}" for label, f in dipakai))
    # Instruksi menilai ditaruh DI SINI, bukan cuma di pesan pembuka sesi:
    # ia sampai tepat saat gambarnya ada di tangan, yaitu satu-satunya saat ia
    # bisa dikerjakan. Tanpa ini, gambarnya kerap cuma dilewati dan model
    # menyatakan "sudah sesuai" tanpa benar-benar membandingkan apa pun.
    urut = ", ".join(label for label, _ in dipakai) or "gambar di atas"
    baris.append(
        f"SEKARANG lihat gambarnya ({urut}) dan JAWAB tiga hal sebelum lanjut:\n"
        "  1. Apa yang benar-benar terlihat di TIAP gambar? Sebut konkret — "
        "susunan, warna, teks yang terbaca, bagian yang kosong. Kalau ada "
        "beberapa lebar layar, sebutkan bedanya.\n"
        "  2. Apakah itu COCOK dengan yang diminta pengguna di giliran ini? "
        "Bandingkan poin demi poin, bukan kesan umum.\n"
        "  3. Kalau ada yang meleset atau belum ada: perbaiki kodenya lalu "
        "web_preview lagi. Jangan menyerahkan pemeriksaan ini ke pengguna, dan "
        "jangan menyatakan selesai selagi masih meleset.")
    for _, f in dipakai:
        baris.append(f"{IMAGE_MARK} {f}")
    return "\n".join(baris)
