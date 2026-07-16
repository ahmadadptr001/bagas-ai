# bagasAI — AI Agent Serbaguna untuk Terminal

**bagasAI** adalah AI agent Python yang berjalan **gratis** dan bisa benar-benar
mengambil tindakan lewat **tool calling** (mencari web, mengelola file,
menjalankan kode, menganalisis gambar). Setelah dipasang, panggil `bagasAI`
dari **terminal mana pun** — mirip CLI `claude`.

---

## ✨ Kemampuan

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

Installer akan memeriksa Python, memasang perintah global `bagasAI`, mengatur
PATH, lalu menuntun proses **login**.

**Linux / macOS / Git-Bash**
```bash
curl -fsSL https://raw.githubusercontent.com/ahmadadptr/bagasai/main/install.sh | bash
```

**Windows (PowerShell)**
```powershell
irm https://raw.githubusercontent.com/ahmadadptr/bagasai/main/install.ps1 | iex
```

> Sudah punya foldernya? Jalankan dari dalam proyek: `bash install.sh`
> (atau `./install.ps1` di PowerShell).

<details>
<summary>Pasang manual (tanpa installer)</summary>

```bash
pip install -e .     # dari folder proyek
bagasAI login        # masukkan API key
```

**Windows — jika `bagasAI` tidak dikenali:** tambahkan folder `Scripts` Python
ke **Environment Variables → Path** (user), lalu buka terminal baru.
</details>

---

## 🔑 Login

Installer menjalankan wizard login otomatis. Kapan pun bisa diulang:

```bash
bagasAI login
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
bagasAI              # chat di terminal (default)
bagasAI --resume     # lanjutkan percakapan terakhir di folder ini
bagasAI login        # masukkan / ganti API key (+ Telegram)
bagasAI update       # cek & terapkan pembaruan dari GitHub
bagasAI telegram     # bot Telegram
bagasAI api          # server API di http://localhost:8000
bagasAI help         # bantuan
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

Semua opsional kecuali API key (diisi otomatis lewat `bagasAI login`).
Disimpan di `~/.bagasai/.env`.

| Variabel | Keterangan |
|---|---|
| `CHAT_MODEL` | Model chat default. Ganti kapan pun lewat `/model`. |
| `VISION_MODEL` | Model untuk analisis gambar. |
| `TELEGRAM_BOT_TOKEN` | Token bot untuk mode `telegram`. |
| `RETRY_MAX_SECONDS` | Berapa lama bertahan mencoba ulang saat kena rate-limit (default 300). |
| `MAX_TOOL_ITERATIONS` | Batas loop tool per giliran (default 8). |
| `ALLOW_CODE_EXEC` | `true`/`false` — aktifkan eksekusi kode. |
| `CODE_EXEC_TIMEOUT` | Timeout eksekusi kode (detik). |

---

## 🔒 Keamanan

- Tool file & shell **dibatasi ke folder kerja** (mitigasi path traversal).
- Eksekusi kode punya **timeout** dan bisa **dimatikan** (`ALLOW_CODE_EXEC=false`).
- File `.env` (berisi key) sudah masuk `.gitignore` — **jangan pernah di-commit**.

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
  __main__.py    # dispatcher perintah `bagasAI`
pyproject.toml   # definisi perintah global
run.py           # entry point mode pengembangan
```

---

## ➕ Menambah tool baru

Buat fungsi dengan dekorator `@tool` di `src/agent/tools/`, lalu import di
`src/agent/tools/__init__.py`. Skema untuk model dibuat **otomatis** dari
type hints + docstring — tak perlu menulis JSON manual.
