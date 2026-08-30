# bagas-ai — AI Agent Serbaguna untuk Terminal

**bagas-ai** adalah AI agent Python yang berjalan **gratis** dan bisa benar-benar
mengambil tindakan lewat **tool calling** (mencari web, mengelola file,
menjalankan kode, menganalisis gambar). Setelah dipasang, panggil `bagas-ai`
dari **terminal mana pun** — mirip CLI `claude`.

---
.
## ✨ Kemampuann

| | |
|---|---|
| 💬 **Chat + reasoning** | Percakapan multi-giliran dengan riwayat & mode berpikir (`/effort`). |
| 🔎 **Pencarian web** | Cari info terkini via DuckDuckGo (tanpa API key). |
| 📁 **File** | Baca, tulis, dan daftar file di folder kerja. |
| 🖥️ **Eksekusi kode** | Jalankan Python & perintah shell (dengan timeout, bisa dimatikan). |
| 🖼️ **Gambar lokal + multimodal** | `/image <path>` membaca metadata, warna, struktur, QR, dan OCR secara lokal tanpa upload; model vision tetap tersedia untuk analisis semantik. |
| 📹 **Live screen** | `/live on` mengambil screenshot terbaru pada tiap pertanyaan dan melampirkannya ke model vision (`/video` adalah alias). |
| 🧠 **Memori** | Ingat preferensi & fakta penting lintas sesi; simpan skrip reusable. |
| 🔁 **Banyak model** | Ganti model kapan pun lewat `/model`. |

Satu core agent, tiga antarmuka:

- **CLI** — chat di terminal
- **Bot Telegram**
- **API** (FastAPI) — sekaligus bisa dipakai sebagai **library** Python

---

## 🚀 Pasang — satu perintah

Installer akan memeriksa Python, memeriksa kecocokan sistem (OS, RAM, ruang
disk ±3,7 GB, internet) dan menampilkan Ketentuan & Kebijakan sebelum
memasang apa pun, lalu memasang perintah global `bagas-ai`, mengatur PATH,
dan menuntun proses **login**.

**Linux / macOS / Git-Bash**
```bash
curl -fsSL https://raw.githubusercontent.com/ahmadadptr001/bagas-ai/master/install.sh | bash
```

**Windows (PowerShell)**
```powershell
irm https://raw.githubusercontent.com/ahmadadptr001/bagas-ai/master/install.ps1 | iex
```

> Sudah punya foldernya? Jalankan dari dalam proyek: `bash install.sh`
> (atau `./install.ps1` di PowerShell).

<details>
<summary>Pasang manual (tanpa installer)</summary>

```bash
pip install -e .     # dari folder proyek
bagas-ai login        # masukkan API key
```

**Windows — jika `bagas-ai` tidak dikenali:** tambahkan folder `Scripts` Python
ke **Environment Variables → Path** (user), lalu buka terminal baru.
</details>

---

## 🔑 Login

Installer menjalankan wizard login otomatis. Kapan pun bisa diulang:

```bash
bagas-ai login
```

Wizard akan:

1. Meminta **API key** lalu **memvalidasinya langsung** (menolak key yang salah).
2. Menanyakan apakah mau **menghubungkan Telegram**; jika ya, minta token
   [@BotFather](https://t.me/BotFather) dan memvalidasinya juga.
3. Menyimpan semuanya secara aman ke config global.

> API key gratis bisa diambil di penyedia model dan langsung ditempel saat login.
> Config global tersimpan di `~/.bagasai/` (Windows: `C:\Users\<nama>\.bagasai\`).

### Model gratis lewat OpenCode Zen

Tujuh model **gratis TANPA API key** (akses anonim, kuota per-IP) tersedia
lewat gateway [OpenCode Zen](https://opencode.ai/docs/zen) — semuanya berada
di posisi teratas daftar `/model`: `opencode/big-pickle`, `opencode/hy3-free`,
`opencode/ling-3.0-flash-fin-free`, `opencode/mimo-v2.5-free`,
`opencode/muse-spark-1.2-contributor-free`, `opencode/nemotron-3-ultra-free`,
`opencode/nemotron-3.5-lightning-free`.

Tidak perlu API key untuk mencoba — langsung:

1. Pilih modelnya lewat `/model` (contoh: `/model big-pickle`).
2. Selesai — tidak perlu login, tidak perlu key.

Akses anonim tetap mempunyai kuota bersama/per-IP. Jika Zen membalas HTTP 429
`FreeUsageLimitError`, itu berarti kuota gratis jaringan tersebut sedang habis,
bukan kerusakan tool. Bagas-AI menampilkan penyebab ini secara spesifik dan
tidak lagi mengulang request yang sama selama lima menit. Tunggu kuota tersedia,
pakai kredensial akun, atau pilih model web lewat `/model`.

Ingin kuota pribadi alih-alih kuota anonim per-IP? `OPENCODE_API_KEY`
opsional: ambil gratis di **https://opencode.ai/auth**, lalu tulis
`OPENCODE_API_KEY=...` di `~/.bagasai/.env` — atau cukup jalankan
`opencode auth login` bila CLI opencode terpasang (installer menawarkan
memasangnya; bagas-ai membaca key-nya otomatis dari auth.json CLI itu).

Opsi `/effort` tidak mengirim field `variant` ke API OpenCode Zen. `--variant`
pada CLI OpenCode adalah konfigurasi sisi klien yang dipetakan ke opsi masing-
masing provider/model, bukan parameter universal endpoint; karena itu Bagas-AI
hanya menawarkan effort untuk model API yang parameternya sudah terverifikasi.

---

## ▶️ Jalankan

```bash
bagas-ai              # chat di terminal (default)
bagas-ai --resume     # lanjutkan percakapan terakhir di folder ini
bagas-ai login        # masukkan / ganti API key (+ Telegram)
bagas-ai update       # cek & terapkan pembaruan dari GitHub
bagas-ai telegram     # bot Telegram
bagas-ai api          # server API di http://localhost:8000
bagas-ai help         # bantuan
```

### Perintah dalam chat

`/menu` `/model` `/effort` `/live` `/video` `/stream` `/mic` `/voice` `/image`
`/new` `/delete` `/reset` `/memory` `/scripts` `/clear` `/update` `/help` `/exit`

`/live on` hanya dapat diaktifkan jika model terpilih mendukung vision dan
jalur lampiran gambar. Selama aktif, bagas-ai mengambil satu screenshot tepat
sebelum setiap pertanyaan biasa dikirim; `/live off` menghentikannya dan
menghapus screenshot sementara. `/video` mempunyai perilaku yang sama.

`/mic on` membacakan kabar proses dan jawaban akhir; gunakan `/mic tes` untuk
memeriksa suara. Tekan ikon mikrofon di kotak chat atau `F4` untuk dikte satu
perintah langsung tanpa wake word; rekaman berhenti otomatis setelah diam dan
diproses lokal. `/voice on` tetap tersedia sebagai mode hands-free: sebut
“bagas ai”, ucapkan perintah, lalu diam dua detik. Mikrofon selalu mulai dalam
keadaan mati pada sesi baru dan statusnya terlihat permanen di footer.
Audio Windows diambil lewat WASAPI pada sample rate asli perangkat, kemudian
WebRTC lokal menjalankan peredam bising, automatic gain control, dan deteksi
suara sebelum Whisper. Model Whisper `small` (±460 MB) wajib diunduh dan diuji
saat instalasi maupun pembaruan; ukurannya diatur lewat `VOICE_STT_MODEL`.

`/image "path gambar.png"` membuka gambar langsung melalui Python/Pillow di
laptop dan menampilkan format, dimensi, frame, transparansi, warna dominan,
kecerahan, sketsa luminans, QR (jika OpenCV tersedia), serta OCR (Tesseract;
di Windows dipasang otomatis oleh installer & `bagas-ai update` lewat winget).
Tidak ada byte gambar yang dikirim ke provider.
Tool agent dengan perilaku yang sama bernama `read_image_local`. Karena ini
bukan model vision lokal, pengenalan objek atau makna adegan yang mendalam tetap
memerlukan model vision dan lampiran gambar.

---

## 🧩 Pakai sebagai API / library

**Panggil server API:**
```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Cari berita AI terbaru\", \"session_id\": \"u1\"}"
```

**Sebagai library Python:**
```python
import sys; sys.path.insert(0, "src")
from agent import Agent

agent = Agent()
print(agent.run("Hitung 15% dari 2.400.000 dan jelaskan caranya"))
```

> Mode pengembangan tanpa install: `python run.py [chat|telegram|api]`.

---

## 🧠 Konteks kerja & pengujian (tanpa baca ulang)

Tiga tool bawaan yang membuat AI bekerja lebih efektif di proyek ini:

- **`kerja_terakhir()`** — ingat kembali apa yang barusan dilakukan sesi ini
  (setiap tool call tercatat otomatis di worklog), **tanpa membaca ulang berkas**.
  `catat_kerja("...")` menambah catatan sadar (penyebab error, keputusan desain).
- **`sasaran(tugas)`** — hitung berkas paling relevan untuk satu tugas (dari
  peta proyek + kata kunci) lengkap dengan alasannya, supaya AI tidak membuka
  berkas asal-asalan.
- **`test_function(path, symbol, args)`** — uji satu fungsi/kelas dari berkas
  proyek dengan argumen contoh di subproses terisolasi; `read_file` juga sudah
  anti-baca-ulang (berkas yang tak berubah tidak dikirim ulang isinya).

---

## 🎯 Server MCP — sasaran tepat untuk AI

`bagas-ai mcp` menjalankan server **MCP** (Model Context Protocol, transport
stdio) yang menegakkan tiga disiplin kerja saat AI menangani proyek ini:

1. **Sasaran tepat** — tool `sasaran(tugas)` menghitung berkas yang relevan
   untuk tugas itu (dari peta proyek + kata kunci) lengkap dengan alasannya,
   jadi AI langsung tahu berkas mana yang boleh disentuh, bukan menebak.
2. **Tanpa baca ulang** — tool `baca` mengingat sidik jari tiap berkas yang
   sudah dibaca sesi ini; dipanggil lagi untuk berkas yang tak berubah, ia
   menolak mengirim isi ulang dan menyuruh memakai yang sudah ada di konteks.
3. **Tidak asal buka file** — begitu `sasaran` dipanggil, `baca` MENOLAK
   berkas di luar daftar; perpanjangan hanya lewat `perluas_sasaran` dengan
   alasan.

Server ini memakai mesin yang sama dengan agent (peta proyek `projectindex`,
batas folder aman `workspace`, logika `read_file`), jadi perilakunya konsisten.

**Jalankan:**
```bash
bagas-ai mcp
```

**Hubungkan dari klien MCP** (mis. Claude Desktop → Settings → Developer →
Edit Config):
```json
{
  "mcpServers": {
    "bagas-ai": {
      "command": "bagas-ai",
      "args": ["mcp"]
    }
  }
}
```

Tool yang tersedia: `sasaran(tugas)` · `perluas_sasaran(paths, alasan)` ·
`sasaran_aktif()` · `tutup_sasaran()` · `baca(path, ...)` · `cari(query)` — plus
resource `peta://proyek` (peta proyek) dan `peta://sasaran` (sasaran aktif).

---

## ⚙️ Konfigurasi

Semua opsional kecuali API key (diisi otomatis lewat `bagas-ai login`).
Disimpan di `~/.bagasai/.env`.

| Variabel | Keterangan |
|---|---|
| `CHAT_MODEL` | Model chat default. Ganti kapan pun lewat `/model`. |
| `CONNECTOR_BROWSER_CHANNEL` | Browser yang dipakai connector: `brave` (default), `chrome`, `chrome-beta`, `msedge`. Kalau yang diminta belum terpasang, browser asli lain dipakai otomatis. Tiap browser punya profil login sendiri, jadi berganti berarti login ulang. |
| `VOICE_JANGKAUAN` | Seberapa jauh mikrofon `/voice` boleh mendengar: `jauh` (default — bisa dari kasur / ruangan sebelah), `normal`, `dekat`. Ukur dari tempat dudukmu sendiri lewat `/voice jangkau`. |
| `VOICE_STT_MODEL` | Model Whisper untuk `/voice`: `tiny` (±75 MB, tercepat), `base`, `small` (default, ±460 MB), `medium` (±1,5 GB). |
| `VISION_MODEL` | Model untuk analisis gambar. |
| `TELEGRAM_BOT_TOKEN` | Token bot untuk mode `telegram`. |
| `RETRY_MAX_SECONDS` | Berapa lama bertahan mencoba ulang saat kena rate-limit (default 300). |
| `MAX_TOOL_ITERATIONS` | Batas loop tool per giliran (default 8). |
| `ALLOW_CODE_EXEC` | `true`/`false` — aktifkan eksekusi kode. |
| `CODE_EXEC_TIMEOUT` | Timeout eksekusi kode (detik). |

---

## 🗑️ Mencopot

```bash
bagas-ai uninstall
```

Menghapus **paket** dan **seluruh data** di `~/.bagasai` sekaligus: sesi
percakapan, memory jangka panjang, script memory, profil login browser, dan
`.env`. Berkas proyekmu sendiri tidak disentuh. Ada konfirmasi (ketik `HAPUS`);
lewati dengan `--yes`.

| Opsi | Efek |
|---|---|
| `--yes` / `-y` | Jangan tanya konfirmasi |
| `--data-only` | Hapus data saja, paket tetap terpasang |
| `--keep-data` | Copot paket saja, data tetap disimpan |

> **`pip uninstall bagasai` tidak menghapus datamu.** Bukan kelalaian: pip
> memang **tidak punya hook uninstall** — saat mencopot, ia hanya menghapus
> berkas yang tercatat di RECORD paket dan tak menjalankan kode apa pun dari
> paket itu (wheel melarangnya). Karena `~/.bagasai` berada di luar RECORD,
> folder itu selalu tertinggal. `bagas-ai uninstall` ada justru untuk
> menutup celah tersebut. Kalau paket sudah terlanjur dicopot lewat pip,
> hapus manual: `rm -rf ~/.bagasai` (Windows: `Remove-Item -Recurse -Force
> $HOME\.bagasai`).

Pencopotan paket berjalan **beberapa detik setelah perintah selesai** lewat
proses pendamping — pip tak bisa menghapus `bagasai.exe` selagi ia dipakai
menjalankan perintah ini. Hasilnya ditulis ke `bagasai_uninstall.log` di
folder TEMP.

---

## 🔒 Keamanan

- Tool file & shell **dibatasi ke folder kerja** (mitigasi path traversal).
- Berkas **di luar** folder kerja butuh **izinmu** — lihat bagian di bawah.
- Eksekusi kode punya **timeout** dan bisa **dimatikan** (`ALLOW_CODE_EXEC=false`).
- File `.env` (berisi key) sudah masuk `.gitignore` — **jangan pernah di-commit**.

### 🛡️ Izin akses folder luar

bagas-ai hanya menyentuh **folder proyek** (folder terminal saat dipanggil) dan
folder konteks yang kamu tambahkan lewat `add-dir`. Begitu ia butuh berkas di
luar itu, kamu ditanya lebih dulu:

```
╭──────────────────────────────────────────────────
│
│  Izinkan bagas-ai MENULIS di luar folder proyek?
│  C:\Users\kamu\Downloads\aset\logo.png
│
│  ❯ 1. Izinkan sekali ini saja
│    2. Izinkan folder ini selama sesi
│    3. Izinkan permanen (jadikan folder konteks)
│    4. Tolak
╰──────────────────────────────────────────────────
```

Pertanyaannya menyebut **tindakannya** (membaca / MENULIS / MENGHAPUS) dan
jawabannya diingat **per folder**, jadi kamu tak ditanya berulang untuk folder
yang sama. Penolakan juga diingat — agent tak bisa menghujanimu dengan
pertanyaan yang sama. Kalau tak ada yang bisa ditanya (mode `api`), jawabannya
otomatis **tolak**, bukan izinkan.

Mau tanpa gangguan?

```bash
bagas-ai --skip-permissions      # semua folder boleh, tanpa konfirmasi
```

> Flag ini membuang lapisan pengaman: satu langkah keliru bisa menulis atau
> menghapus di mana saja di laptopmu. Selama aktif, statusnya tampil merah di
> banner sesi. Untuk permanen (termasuk mode `telegram`/`api`), set
> `BAGASAI_SKIP_PERMISSIONS=true` di `~/.bagasai/.env`.

---

## 🗂️ Struktur

```
src/agent/
  config.py      # baca .env & path config
  llm.py         # klien model + retry otomatis
  core.py        # Agent: loop tool-calling
  memory.py      # riwayat percakapan
  prompts.py     # system prompt
  tools/         # web_search, files, shell, vision
  interfaces/    # cli, telegram_bot, api
  __main__.py    # dispatcher perintah `bagas-ai`
pyproject.toml   # definisi perintah global
run.py           # entry point mode pengembangan
```

---

## ➕ Menambah tool baru

Buat fungsi dengan dekorator `@tool` di `src/agent/tools/`, lalu import di
`src/agent/tools/__init__.py`. Skema untuk model dibuat **otomatis** dari
type hints + docstring — tak perlu menulis JSON manual.
