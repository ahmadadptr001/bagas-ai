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
| 🖼️ **Multimodal** | Analisis gambar. |
| 🧠 **Memori** | Ingat preferensi & fakta penting lintas sesi; simpan skrip reusable. |
| 🔁 **Banyak model** | Ganti model kapan pun lewat `/model`. |

Satu core agent, tiga antarmuka:

- **CLI** — chat di terminal
- **Bot Telegram**
- **API** (FastAPI) — sekaligus bisa dipakai sebagai **library** Python

---

## 🚀 Pasang — satu perintah

Installer akan memeriksa Python, memasang perintah global `bagas-ai`, mengatur
PATH, lalu menuntun proses **login**.

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

`/menu` `/model` `/effort` `/new` `/delete` `/reset` `/memory` `/scripts`
`/clear` `/update` `/help` `/exit`

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

## ⚙️ Konfigurasi

Semua opsional kecuali API key (diisi otomatis lewat `bagas-ai login`).
Disimpan di `~/.bagasai/.env`.

| Variabel | Keterangan |
|---|---|
| `CHAT_MODEL` | Model chat default. Ganti kapan pun lewat `/model`. |
| `CONNECTOR_BROWSER_CHANNEL` | Browser yang dipakai connector: `brave` (default), `chrome`, `chrome-beta`, `msedge`. Kalau yang diminta belum terpasang, browser asli lain dipakai otomatis. Tiap browser punya profil login sendiri, jadi berganti berarti login ulang. |
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
