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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from ..tools import vision

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

OnEvent = Callable[[str, str], None]  # (kind, text): 'in'|'out'|'perm'|'info'|'error'


def _get_agent(chat_id: int) -> Agent:
    if chat_id not in _agents:
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
    """Kirim balasan; pecah bila panjang, dan kirim GAMBAR sebagai foto asli."""
    text = (text or "(kosong)").strip() or "(kosong)"
    # Jangan pernah membuang data URI mentah ke chat (tak berguna & sangat panjang).
    text = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]{50,}",
                  "[gambar dikirim sebagai foto]", text)
    imgs = _find_images(text)
    for i in range(0, len(text), _TG_LIMIT):
        await update.message.reply_text(text[i:i + _TG_LIMIT])
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


def build_application(on_event: OnEvent | None = None) -> Application:
    """Rakit Application Telegram lengkap dengan handler + izin + pemantauan CLI."""

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
                                     on_message=lambda c: emit("out", c),
                                     on_notice=_notice)
                finally:
                    interaction.reset_context_handler(tok)

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
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.jpg"
            await tg_file.download_to_drive(str(path))
            reply = await _run_with_typing(update, context, vision.analyze_image,
                                           path, caption)
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
        for _, key, spec in models.catalog():
            mark = "● " if spec.id == agent.model else ""
            rows.append([InlineKeyboardButton(f"{mark}{spec.label}"[:60],
                                              callback_data=f"model:{key}")])
        await update.message.reply_text("🔀 Pilih model:",
                                        reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        agent = _get_agent(update.effective_chat.id)
        if not agent.model_spec.supports_effort():
            await update.message.reply_text(
                f"Model {agent.model_spec.label} menjawab langsung — tak punya "
                f"mode berpikir yang bisa diatur.")
            return
        rows = [[InlineKeyboardButton(
            f"{'● ' if k == agent.effort else ''}{icon} {title}"[:60],
            callback_data=f"effort:{k}")]
            for k, title, _desc, icon in agent.model_spec.effort_info()]
        await update.message.reply_text("🎚 Mode berpikir:",
                                        reply_markup=InlineKeyboardMarkup(rows))

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _guard(update):
            return
        agent = _get_agent(update.effective_chat.id)
        eff = f"\n🎚 Effort: {agent.effort}" if agent.effort else ""
        await update.message.reply_text(
            f"⬢ bagas-ai\n🤖 Model: {agent.model_spec.label}{eff}\n"
            f"📁 Folder: {config.PROJECT_ROOT}\n"
            f"⚡ Token sesi: {agent.tokens_session.total:,}".replace(",", "."))

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
                await cq.edit_message_text(f"✓ Model: {label}")
                emit("info", f"model diganti lewat Telegram -> {label}")
            except Exception as e:  # noqa: BLE001
                await cq.edit_message_text(f"✖ gagal: {e}")
        elif data.startswith("effort:"):
            try:
                val = agent.set_effort(data.split(":", 1)[1])
                await cq.edit_message_text(f"✓ Mode berpikir: {val}")
                emit("info", f"effort diganti lewat Telegram -> {val}")
            except Exception as e:  # noqa: BLE001
                await cq.edit_message_text(f"✖ gagal: {e}")

    # concurrent_updates(True): WAJIB agar balasan pengguna atas pertanyaan agent
    # bisa diproses SELAGI handler pemicu masih menunggu (kalau tidak -> deadlock).
    app = (Application.builder()
           .token(config.TELEGRAM_BOT_TOKEN)
           .concurrent_updates(True)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
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

    def start(self, on_event: OnEvent | None = None) -> bool:
        if self.running:
            return True
        if not config.TELEGRAM_BOT_TOKEN:
            self.error = RuntimeError("TELEGRAM_BOT_TOKEN belum diisi di .env")
            return False
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(on_event,),
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

    def _run(self, on_event: OnEvent | None) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._app = build_application(on_event)
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
    config.require_api_key()
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
