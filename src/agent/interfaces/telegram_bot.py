"""Bot Telegram bagas-ai — bisa dijalankan DI DALAM sesi CLI (sebagai layanan latar)
maupun berdiri sendiri (`bagas-ai telegram`).

Lewat Telegram, bagas-ai mengontrol laptop tempat ia berjalan: mengobrol,
menjalankan perintah, membaca/menulis file, menganalisis foto — selama laptop &
proses ini menyala. Akses dibatasi lewat izin (lihat telegram_perms &
`/permissions-bot`). Aktivitas bisa dipantau di CLI lewat callback `on_event`.
"""
from __future__ import annotations

import asyncio
import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import config, interaction, longmem, models, projectindex, telegram_perms
from ..core import Agent

# Ekstensi gambar yang otomatis DIKIRIM sebagai foto (bukan teks/data URI).
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# Kata fase per-tool untuk progres yang tampil di Telegram (mirror dari CLI).
_PHASE_ID = {
    "write_file": "menulis", "delete_file": "menghapus", "read_file": "membaca",
    "list_dir": "menelusuri", "web_search": "mencari", "run_command": "menjalankan",
    "run_python": "menjalankan", "run_script": "menjalankan",
    "run_command_bg": "menjalankan (latar)", "bg_output": "cek log",
    "bg_stop": "menghentikan", "save_script": "menyimpan", "remember": "mengingat",
}


def _tool_label(name: str, args: dict) -> str:
    a = args if isinstance(args, dict) else {}
    val = (a.get("command") or a.get("path") or a.get("query") or a.get("name")
           or a.get("fact") or a.get("bg_id") or name)
    return str(val)[:70]

_agents: dict[int, Agent] = {}
# Pertanyaan agent yang sedang menunggu jawaban pengguna, per chat_id. Bila ada,
# pesan berikutnya dari chat itu diperlakukan sebagai JAWABAN, bukan tugas baru.
_pending: dict[int, dict] = {}   # {chat_id: {"q":Queue,"options":[...], "question":str}}
# Kunci per-chat: cegah dua agent.run berjalan berbarengan pada Agent yang SAMA
# (mereka berbagi self.memory) -> serialkan tugas per chat.
_locks: dict[int, threading.Lock] = {}
_TG_LIMIT = 4000  # < 4096 batas karakter/pesan Telegram

_TELEGRAM_CONTEXT = (
    "\n\n# Sesi Telegram\n"
    "Percakapan ini berlangsung lewat BOT TELEGRAM — bukan terminal.\n"
    "- Saat perlu bertanya/klarifikasi ke pengguna, pakai "
    "`ask_user_telegram` (BUKAN `ask_user`). "
    "Tool ini mengirim pertanyaan dengan TOMBOL INLINE ke chat Telegram, "
    "lalu menunggu jawaban pengguna di sana.\n"
    "- Di terminal, tak ada menu interaktif — cukup menunggu jawaban "
    "dari Telegram.\n"
    "- Format jawaban tetap sama: teks biasa atau pilihan dari tombol."
)

OnEvent = Callable[[str, str], None]  # (kind, text): 'in'|'out'|'perm'|'info'|'error'


_MAX_AGENTS = 50  # batas Agent serentak; lebih → eviksi tertua (FIFO)


def _inject_telegram_context(agent: Agent) -> None:
    """Suntikkan konteks Telegram ke system prompt agar AI tahu harus pakai
    ask_user_telegram (tombol inline), bukan ask_user (terminal)."""
    if getattr(agent, "_tg_prompt_set", False):
        return
    current = agent.memory.messages[0].get("content", "")
    if "# Sesi Telegram" not in current:
        agent.memory.set_system(current + _TELEGRAM_CONTEXT)
    agent._tg_prompt_set = True   # type: ignore[attr-defined]


_shared_agent: Agent | None = None


def _get_agent(chat_id: int) -> Agent:
    # Bot dijalankan dari CLI (agent dibagikan): pakai agent itu untuk semua chat.
    # Satu agent = satu sesi percakapan, jadi pesan dari Telegram melanjutkan yang
    # ada di terminal.
    if _shared_agent is not None:
        return _shared_agent
    if chat_id not in _agents:
        if len(_agents) >= _MAX_AGENTS:
            oldest = next(iter(_agents))
            del _agents[oldest]
            _locks.pop(oldest, None)
        _agents[chat_id] = Agent()
    return _agents[chat_id]


def _name(update: Update) -> str:
    u = update.effective_user
    if u and u.username:
        return "@" + u.username
    if u and u.first_name:
        return u.first_name
    return str(update.effective_chat.id)


def _find_images(text: str) -> list[Path]:
    """Cari path file GAMBAR yang disebut di jawaban & benar-benar ada di disk,
    supaya dikirim sebagai FOTO (bukan teks path / data:image...)."""
    out: list[Path] = []
    for m in re.finditer(r"[\w./\\:~-]+\.(?:png|jpe?g|gif|webp|bmp)", text,
                         re.IGNORECASE):
        raw = m.group(0).strip("`'\"()[],")
        p = Path(raw)
        if not p.is_absolute():
            p = config.PROJECT_ROOT / raw
        try:
            if p.is_file() and p.suffix.lower() in _IMG_EXT and p not in out:
                out.append(p)
        except OSError:
            continue
    return out[:5]


async def _reply_long(update: Update, text: str) -> None:
    """Kirim balasan; pecah di batas BARIS bila panjang, kirim GAMBAR sebagai foto."""
    text = (text or "(kosong)").strip() or "(kosong)"
    # Jangan pernah membuang data URI mentah ke chat (tak berguna & sangat panjang).
    text = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]{50,}",
                  "[gambar dikirim sebagai foto]", text)
    imgs = _find_images(text)
    # Pecah di batas baris (bukan tengah kata/kode/markdown).
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _TG_LIMIT:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, _TG_LIMIT + 1)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, _TG_LIMIT + 1)
        if cut <= 0:
            cut = _TG_LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for i, chunk in enumerate(chunks):
        await update.message.reply_text(chunk)
        if i < len(chunks) - 1:
            await asyncio.sleep(0.3)  # jeda antar pesan — cegah flood
    for p in imgs:  # kirim file gambar yang disebut sebagai FOTO
        try:
            with open(p, "rb") as fh:
                await update.message.reply_photo(fh, caption=p.name)
        except Exception:
            pass


async def _run_with_typing(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           func, *args) -> str:
    """Jalankan fungsi blocking di thread sambil menjaga indikator 'mengetik…'."""
    chat_id = update.effective_chat.id
    stop = asyncio.Event()

    async def _typing() -> None:
        while not stop.is_set():
            try:
                await context.bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_typing())
    try:
        return await asyncio.to_thread(func, *args)
    except Exception as e:  # noqa: BLE001
        return f"⚠ Terjadi error: {e}"
    finally:
        stop.set()
        try:
            await task
        except Exception:
            pass


def build_application(on_event: OnEvent | None = None, agent: Agent | None = None) -> Application:
    """Rakit Application Telegram lengkap dengan handler + izin + pemantauan CLI.

    Jika `agent` diberikan (dari CLI), bot akan MEMAKAI agent tersebut untuk semua
    chat yang diizinkan — sehingga percakapan Telegram melanjutkan sesi terminal.
    Jika tidak, bot berdiri sendiri dan membuat agent per chat seperti biasa."""
    global _shared_agent
    _shared_agent = agent
    # Kunci global: serialkan semua agent.run. Bot berdiri sendiri memakai kunci
    # per-chat (lihat _locks), tapi saat agent DIBAGIKAN dari CLI, seluruh chat
    # Telegram berbagi memori yang sama — harus bergiliran agar tak rusak.
    tg_lock = asyncio.Lock()

    def emit(kind: str, text: str) -> None:
        if on_event:
            try:
                on_event(kind, text)
            except Exception:
                pass

    def make_choice_handler(chat_id: int, bot, loop):
        """Handler ask_user KHUSUS Telegram: kirim pertanyaan ke chat & TUNGGU
        balasan pengguna di Telegram (bukan di terminal). Jalan di thread worker
        agent (blocking), sementara loop bot tetap bebas menerima balasan."""

        def handler(question: str, options: list[str], multiple: bool) -> str:
            emit("info", f"❓ menanyakan di Telegram: {question}")
            q: "queue.Queue[str]" = queue.Queue(maxsize=1)
            _pending[chat_id] = {"q": q, "options": list(options or []),
                                 "question": question}
            text = "❓ " + question
            markup = None
            if options and not multiple:
                # TOMBOL (inline keyboard) — bukan sekadar teks bernomor.
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(str(o)[:60], callback_data=f"ans:{i}")]
                     for i, o in enumerate(options)]
                )
            elif options and multiple:
                text += "\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
                text += "\n\nBalas nomor (boleh beberapa, pisah koma) atau ketik jawabanmu."
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    bot.send_message(chat_id, text, reply_markup=markup), loop)
                fut.result(timeout=20)
            except Exception as e:  # noqa: BLE001
                _pending.pop(chat_id, None)
                return (f"(gagal mengirim pertanyaan ke Telegram: {e}; "
                        f"ambil keputusan paling wajar lalu lanjutkan)")
            try:
                ans = q.get(timeout=600)  # tunggu jawaban (tombol/teks) 10 menit
            except queue.Empty:
                return ("(pengguna tak menjawab dalam 10 menit — ambil keputusan "
                        "paling wajar lalu lanjutkan)")
            finally:
                _pending.pop(chat_id, None)
            if options:  # jawaban teks berupa nomor -> petakan ke opsi
                picks = []
                for part in str(ans).replace(" ", "").split(","):
                    if part.isdigit() and 1 <= int(part) <= len(options):
                        picks.append(options[int(part) - 1])
                if picks:
                    return ", ".join(picks) if multiple else picks[0]
            return str(ans)

        return handler

    async def _guard(update: Update) -> bool:
        cid = update.effective_chat.id
        if telegram_perms.is_allowed(cid):
            return True
        # Trust-on-first-use: bila BELUM ada satu pun ID diizinkan, pengirim
        # PERTAMA otomatis jadi pemilik (sesuai janji di .env). Ini yang membuat
        # bot langsung bisa dipakai owner tanpa harus approve manual di CLI.
        if not telegram_perms.allowed_ids():
            telegram_perms.add_allowed(cid, _name(update))
            emit("info", f"{_name(update)} (id {cid}) menjadi PEMILIK bot "
                         f"(pengirim pertama)")
            await update.message.reply_text(
                f"🔑 Kamu kini pemilik bagas-ai ini (pengirim pertama, id {cid}).\n"
                f"Agar permanen, isi di .env: TELEGRAM_ALLOWED_IDS={cid}"
            )
            return True
        is_new = telegram_perms.add_pending(cid, _name(update))
        await update.message.reply_text(
            "🔒 Kamu belum diizinkan mengontrol bagas-ai ini.\n"
            f"Permintaan izin dikirim ke pemilik (ID kamu: {cid}). Tunggu persetujuan."
        )
        if is_new:
            emit("perm", f"{_name(update)} (id {cid}) minta izin — buka "
                         f"/permissions-bot di CLI untuk menyetujui")
        return False

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        await update.message.reply_text(
            "👋 Halo! Saya *bagas-ai* — mengontrol laptop ini dari Telegram.\n\n"
            "Kirim tugas/pertanyaan; kirim foto (+caption) untuk dianalisis. "
            "Perintah: /reset · /new · /help.",
            parse_mode="Markdown",
        )

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        await update.message.reply_text(
            "bagas-ai via Telegram — hampir semua fitur CLI ada di sini:\n"
            "• Kirim teks untuk memberi tugas/pertanyaan (progres tampil realtime).\n"
            "• Kirim foto (+caption) untuk dianalisis.\n"
            "• /model — ganti model (tombol)\n"
            "• /effort — mode berpikir (tombol)\n"
            "• /status — model, effort, folder, token\n"
            "• /scan — segarkan peta proyek\n"
            "• /memory — memori jangka panjang\n"
            "• /reset — hapus riwayat · /new — sesi baru\n"
            f"Folder kerja: {config.PROJECT_ROOT}\n"
            "(Izin bot diatur dari CLI: /permissions-bot)"
        )

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        _get_agent(update.effective_chat.id).reset()
        await update.message.reply_text("(riwayat percakapan dihapus)")

    async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        _agents.pop(update.effective_chat.id, None)
        _get_agent(update.effective_chat.id)
        await update.message.reply_text("(sesi baru dimulai)")

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cid = update.effective_chat.id
        # Bila agent sedang MENUNGGU jawaban dari chat ini, perlakukan pesan ini
        # sebagai jawabannya (bukan tugas baru).
        pend = _pending.get(cid)
        if pend is not None:
            try:
                pend["q"].put_nowait(update.message.text)
            except Exception:
                pass
            emit("in", f"{_name(update)} (jawaban): {update.message.text}")
            return
        if not await _guard(update):
            return
        emit("in", f"{_name(update)}: {update.message.text}")
        agent = _get_agent(cid)
        _inject_telegram_context(agent)
        loop = asyncio.get_running_loop()
        handler = make_choice_handler(cid, context.bot, loop)

        # Pesan STATUS langsung yang di-EDIT tiap langkah -> progres tampil di
        # Telegram, bukan cuma di terminal (dan tetap tampil di terminal via emit).
        status_msg = None
        try:
            status_msg = await context.bot.send_message(cid, "⏳ mulai mengerjakan…")
        except Exception:
            pass
        steps_log: list[str] = []

        last_edit = {"t": 0.0}

        def _render_status() -> None:
            """Perbarui pesan status di Telegram dari daftar langkah saat ini.
            Di-THROTTLE (maks ~1 edit/1.5 dtk): edit_message_text tiap langkah
            memicu flood-limit Telegram; keadaan akhir tetap dirapikan di akhir."""
            if status_msg is None:
                return
            now = time.time()
            if now - last_edit["t"] < 1.5:
                return
            last_edit["t"] = now
            body = "\n".join(steps_log[-12:])[:_TG_LIMIT] or "⏳ mengerjakan…"
            try:
                asyncio.run_coroutine_threadsafe(
                    context.bot.edit_message_text(body, chat_id=cid,
                                                  message_id=status_msg.message_id),
                    loop)
            except Exception:
                pass

        def _tg_on_tool(name: str, args: dict) -> None:
            lbl = _tool_label(name, args)
            emit("info", f"▶ {name}: {lbl}")                       # di TERMINAL
            steps_log.append(f"⏳ {_PHASE_ID.get(name, name)} · {lbl}")
            _render_status()                                       # dan di BOT

        def _tg_on_message(msg: str) -> None:
            emit("out", msg)
            try:
                asyncio.run_coroutine_threadsafe(
                    context.bot.send_message(cid, msg), loop)
            except Exception:
                pass

        def _tg_on_result(name: str, result: str) -> None:
            failed = (result or "").strip().startswith(("[GAGAL", "[error]"))
            # Cari MUNDUR baris langkah yang masih ⏳ (baris lain, mis. "⚡ naik
            # kelas", bisa menyelip di antaranya) lalu tandai selesai/gagal.
            for i in range(len(steps_log) - 1, -1, -1):
                if "⏳" in steps_log[i]:
                    steps_log[i] = steps_log[i].replace(
                        "⏳", "✗" if failed else "✓", 1)
                    break
            _render_status()

        lock = _locks.setdefault(cid, threading.Lock())

        def _run(txt: str) -> str:
            # Serialkan tugas per chat: cegah dua agent.run bersamaan pada Agent
            # yang sama (berbagi memory). Jawaban atas pertanyaan TIDAK lewat sini
            # (ditangani lebih dulu di on_text), jadi tak ikut terkunci.
            with lock:
                # Pasang handler ask_user Telegram di KONTEKS thread ini (disalin
                # oleh asyncio.to_thread) -> pertanyaan muncul di Telegram, bukan CLI.
                tok = interaction.set_context_handler(handler)
                try:
                    def _notice(msg: str) -> None:
                        label = ("⚡ naik kelas otomatis" if "→" in msg
                                 else "🛟 anti-macet")
                        emit("info", f"{label}: {msg}")
                        steps_log.append(f"{label}: {msg}")
                        _render_status()

                    return agent.run(txt, on_tool=_tg_on_tool,
                                     on_tool_result=_tg_on_result,
                                     on_message=_tg_on_message,
                                     on_notice=_notice)
                finally:
                    interaction.reset_context_handler(tok)

        async with tg_lock:
            reply = await _run_with_typing(update, context, _run, update.message.text)
            emit("out", reply)
            if status_msg is not None:   # rapikan pesan status jadi ringkasan langkah
                try:
                    if steps_log:
                        done = "\n".join(steps_log[-12:])
                        await context.bot.edit_message_text(
                            done[:_TG_LIMIT], chat_id=cid,
                            message_id=status_msg.message_id)
                    else:
                        # Chat murni tanpa langkah: hapus bubble status agar tak jadi
                        # sampah "✓ selesai" di atas tiap jawaban.
                        await context.bot.delete_message(cid, status_msg.message_id)
                except Exception:
                    pass
            await _reply_long(update, reply)

    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        caption = update.message.caption or "Deskripsikan gambar ini secara detail."
        emit("in", f"{_name(update)}: [foto] {caption}")
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        async with tg_lock:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "image.jpg"
                await tg_file.download_to_drive(str(path))
                # Foto DILAMPIRKAN ke percakapan web milik sesi ini, bukan dikirim ke
                # model vision terpisah seperti dulu. Bedanya nyata: gambar masuk ke
                # percakapan yang SAMA, jadi AI web bisa mengaitkannya dengan tugas
                # yang sedang berjalan dan bahkan menindaklanjuti dengan tool —
                # sementara panggilan VLM sekali-pakai hanya bisa mendeskripsikan.
                agent = _get_agent(update.effective_chat.id)
                _inject_telegram_context(agent)
                loop = asyncio.get_running_loop()
                handler = make_choice_handler(update.effective_chat.id, context.bot, loop)
                tok = interaction.set_context_handler(handler)
                try:
                    reply = await _run_with_typing(
                        update, context,
                        lambda teks: agent.run(teks, attachments=[str(path)],
                                               on_message=_tg_on_message),
                        caption)
                finally:
                    interaction.reset_context_handler(tok)
        emit("out", reply)
        await _reply_long(update, reply)

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        await update.message.reply_text(
            "Fitur suara belum diaktifkan. Kirim teks atau foto."
        )

    # --- Fitur CLI lewat Telegram (pakai TOMBOL) ---------------------------
    async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        agent = _get_agent(update.effective_chat.id)
        rows = []
        # Telegram tak punya tombol "mati", jadi model yang ditunda TIDAK
        # dijadikan tombol — kalau dijadikan, satu-satunya kabar yang didapat
        # pengguna adalah penolakan sesudah ia menekannya. Keberadaannya tetap
        # disebut di baris keterangan di bawah.
        tunda = [s.label for _, _k, s in models.catalog() if s.ditunda]
        for _, key, spec in models.catalog_aktif():
            mark = "● " if spec.id == agent.model else ""
            rows.append([InlineKeyboardButton(f"{mark}{spec.label}"[:60],
                                              callback_data=f"model:{key}")])
        catatan = ("\n\n⏸ ditunda sementara: " + ", ".join(tunda)) if tunda else ""
        await update.message.reply_text("🔀 Pilih model:" + catatan,
                                        reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        agent = _get_agent(update.effective_chat.id)
        # Mode berpikir kini diatur dengan MENGKLIK tombol di UI situs AI web
        # (lihat WebConnector.web_actions) — butuh jendela browser di laptop,
        # jadi tak bisa dijalankan dari Telegram. Menu effort ala API yang dulu
        # ada di sini ikut hilang bersama model ber-API-key.
        await update.message.reply_text(
            f"🎚 Mode berpikir {agent.model_spec.label} diatur lewat tombol di "
            "situsnya, jadi harus dari terminal: ketik /effort di sesi bagas-ai "
            "pada laptop.")

    async def cmd_browser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        pilihan = [
            ("brave", "Brave"), ("chrome", "Chrome"),
            ("msedge", "Microsoft Edge"), ("chrome-beta", "Chrome Beta"),
        ]
        aktif = (config.CONNECTOR_BROWSER_CHANNEL or "").strip().lower()
        rows = []
        for key, nama in pilihan:
            mark = "● " if key == aktif else ""
            rows.append([InlineKeyboardButton(f"{mark}{nama}", callback_data=f"browser:{key}")])
        await update.message.reply_text("🌐 Pilih browser:", reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        agent = _get_agent(update.effective_chat.id)
        total_str = f"{agent.tokens_session.total:,}".replace(",", ".")
        await update.message.reply_text(
            f"⬢ bagas-ai\n🌐 Model: {agent.model_spec.label} (via browser)\n"
            f"📁 Folder: {config.PROJECT_ROOT}\n"
            f"⚡ Token sesi: {total_str}")

    async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        await update.message.reply_text("🔍 memindai proyek…")
        txt = await asyncio.to_thread(projectindex.ensure, config.PROJECT_ROOT, True)
        _get_agent(update.effective_chat.id).refresh_system_prompt()
        await update.message.reply_text(
            f"✓ peta proyek diperbarui (~{txt.count(chr(10) + '- ')} file).")

    async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        facts = longmem.all_facts()
        body = "\n".join(f"• {f}" for f in facts) or "(kosong)"
        await _reply_long(update, "🧠 Memory jangka panjang:\n" + body)

    async def cmd_dirs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        from .. import workspace
        dirs = workspace.list_dirs()
        body = "\n".join(f"• {d}" for d in dirs) or "(kosong)"
        await update.message.reply_text(f"📂 Folder konteks aktif:\n{body}")

    async def cmd_add_dir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        if not context.args:
            await update.message.reply_text("Penggunaan: /add-dir <path>")
            return
        from .. import workspace
        try:
            p = workspace.add(" ".join(context.args))
            await update.message.reply_text(f"✓ Ditambahkan: {p}")
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"✖ gagal: {e}")

    async def cmd_rm_dir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        if not context.args:
            await update.message.reply_text("Penggunaan: /rm-dir <path>")
            return
        from .. import workspace
        try:
            ok = workspace.remove(" ".join(context.args))
            await update.message.reply_text("✓ Dihapus" if ok else "✖ tak ditemukan")
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(f"✖ gagal: {e}")

    async def _terminal_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        await update.message.reply_text("⚠️ Fitur ini hanya bisa dijalankan dari terminal.")

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Semua penekanan TOMBOL: jawaban ask_user, pilih model, pilih effort."""
        cq = update.callback_query
        try:
            await cq.answer()
        except Exception:
            pass
        cid = cq.message.chat_id
        if not telegram_perms.is_allowed(cid):
            return
        data = cq.data or ""
        pend = _pending.get(cid)
        if data.startswith("ans:") and pend is not None:
            try:
                idx = int(data[4:])
            except ValueError:
                return
            opts = pend.get("options") or []
            val = opts[idx] if 0 <= idx < len(opts) else data
            try:
                pend["q"].put_nowait(str(val))
            except Exception:
                pass
            emit("in", f"{_name(update)} (tombol): {val}")
            try:
                await cq.edit_message_text(f"❓ {pend.get('question', '')}\n\n✅ {val}")
            except Exception:
                pass
            return
        agent = _get_agent(cid)
        if data.startswith("model:"):
            try:
                label = agent.set_model(data.split(":", 1)[1])
                msg = f"✓ Model: {label}"
                if agent.model_spec.connector == "glm":
                    msg += ("\n\n⚠ GLM (chat.z.ai) memerlukan VPN aktif "
                            "— disarankan Cloudflare WARP.\n"
                            "Model ini sering bermasalah; jika error berulang, "
                            "coba ganti server VPN.")
                await cq.edit_message_text(msg)
                emit("info", f"model diganti lewat Telegram -> {label}")
            except Exception as e:  # noqa: BLE001
                await cq.edit_message_text(f"✖ gagal: {e}")
        if data.startswith("browser:"):
            pilihan_nama = {
                "brave": "Brave", "chrome": "Chrome",
                "msedge": "Microsoft Edge", "chrome-beta": "Chrome Beta",
            }
            key = data.split(":", 1)[1]
            from ..setup_wizard import _read_env, _write_env
            env_path = config.ENV_FILE
            env_data = _read_env(env_path)
            env_data["CONNECTOR_BROWSER_CHANNEL"] = key
            _write_env(env_path, env_data)
            config.CONNECTOR_BROWSER_CHANNEL = key
            nama = pilihan_nama.get(key, key)
            await cq.edit_message_text(
                f"✓ Browser: {nama}\n\nℹ Mulai ulang bagas-ai untuk menerapkan.")
            emit("info", f"browser diganti lewat Telegram -> {nama}")
        # Cabang "effort:" DIHAPUS bersama menunya — tombolnya tak pernah lagi
        # dikirim, dan set_effort sudah tak ada di Agent.

    # concurrent_updates(True): WAJIB agar balasan pengguna atas pertanyaan agent
    # bisa diproses SELAGI handler pemicu masih menunggu (kalau tidak -> deadlock).
    async def _post_init(app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("menu", "menu interaktif"),
            BotCommand("model", "pilih model + saran"),
            BotCommand("effort", "mode berpikir"),
            BotCommand("mode", "mode kerja situs"),
            BotCommand("tim", "24 spesialis meninjau"),
            BotCommand("mic", "suara AI (on/off/tes)"),
            BotCommand("voice", "mikrofon (on/off/tes)"),
            BotCommand("compact", "simpan riwayat"),
            BotCommand("send_compact", "kirim berkas memory"),
            BotCommand("add_dir", "tambah folder konteks"),
            BotCommand("dirs", "folder konteks aktif"),
            BotCommand("rm_dir", "hapus folder konteks"),
            BotCommand("new", "mulai sesi baru"),
            BotCommand("delete", "hapus sesi"),
            BotCommand("reset", "kosongkan riwayat"),
            BotCommand("clear", "bersihkan layar"),
            BotCommand("web", "kelola sesi AI web"),
            BotCommand("bot", "hidup/matikan bot Telegram"),
            BotCommand("browser", "ganti browser"),
            BotCommand("status", "model, effort, folder, token"),
            BotCommand("scan", "segarkan peta proyek"),
            BotCommand("memory", "memori jangka panjang"),
            BotCommand("permissions_bot", "atur izin"),
            BotCommand("help", "bantuan"),
        ])

    app = (Application.builder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)
           .post_init(_post_init)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("delete", _terminal_only))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("browser", cmd_browser))
    app.add_handler(CommandHandler("mode", _terminal_only))
    app.add_handler(CommandHandler("tim", _terminal_only))
    app.add_handler(CommandHandler("mic", _terminal_only))
    app.add_handler(CommandHandler("voice", _terminal_only))
    app.add_handler(CommandHandler("compact", _terminal_only))
    app.add_handler(CommandHandler("send_compact", _terminal_only))
    app.add_handler(CommandHandler("add_dir", cmd_add_dir))
    app.add_handler(CommandHandler("dirs", cmd_dirs))
    app.add_handler(CommandHandler("rm_dir", cmd_rm_dir))
    app.add_handler(CommandHandler("clear", _terminal_only))
    app.add_handler(CommandHandler("web", _terminal_only))
    app.add_handler(CommandHandler("bot", _terminal_only))
    app.add_handler(CommandHandler("permissions_bot", _terminal_only))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CallbackQueryHandler(on_callback))   # semua TOMBOL
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


class TelegramService:
    """Menjalankan bot Telegram di THREAD latar (di dalam sesi CLI), dengan event
    loop sendiri, sehingga bisa dihidup/matikan tanpa memblokir REPL."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: Application | None = None
        self._stop: asyncio.Future | None = None
        self.running = False
        self.error: Exception | None = None

    def start(self, on_event: OnEvent | None = None, agent: Agent | None = None) -> bool:
        if self.running:
            return True
        if not config.TELEGRAM_BOT_TOKEN:
            self.error = RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di .env")
            return False
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(on_event, agent),
                                        daemon=True)
        self._thread.start()
        for _ in range(120):  # tunggu ~12s hingga jalan / gagal (jaringan lambat)
            if self.running or self.error:
                break
            time.sleep(0.1)
        return self.running

    def alive(self) -> bool:
        """Thread masih hidup (mungkin masih proses menyala) walau belum 'running'."""
        return bool(self._thread and self._thread.is_alive())

    def _run(self, on_event: OnEvent | None, agent: Agent | None = None) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._app = build_application(on_event, agent)
            self._stop = self._loop.create_future()
            self._loop.run_until_complete(self._serve())
        except Exception as e:  # noqa: BLE001
            self.error = e
            self.running = False
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve(self) -> None:
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        self.running = True
        try:
            await self._stop
        finally:
            self.running = False
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass

    def stop(self) -> None:
        if self._loop and self._stop and not self._stop.done():
            try:
                self._loop.call_soon_threadsafe(
                    lambda: self._stop.set_result(True))
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=8)
        self.running = False


def main() -> None:
    """Mode berdiri sendiri: `bagas-ai telegram` (polling di thread utama)."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diisi di .env. Dapatkan dari @BotFather."
        )
    app = build_application()
    ids = telegram_perms.allowed_ids()
    print(f"ID diizinkan: {sorted(ids) or '(belum ada — pakai /permissions-bot di CLI)'}")
    print(f"Bot Telegram bagas-ai berjalan (folder: {config.PROJECT_ROOT}). "
          "Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()
