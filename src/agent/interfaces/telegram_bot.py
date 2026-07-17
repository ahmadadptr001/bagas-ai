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
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import config, interaction, telegram_perms
from ..core import Agent
from ..tools import vision

_agents: dict[int, Agent] = {}
# Pertanyaan agent yang sedang menunggu jawaban pengguna, per chat_id. Bila ada,
# pesan berikutnya dari chat itu diperlakukan sebagai JAWABAN, bukan tugas baru.
_pending: dict[int, "queue.Queue[str]"] = {}
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


async def _reply_long(update: Update, text: str) -> None:
    text = (text or "(kosong)").strip() or "(kosong)"
    for i in range(0, len(text), _TG_LIMIT):
        await update.message.reply_text(text[i:i + _TG_LIMIT])


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
            lines = ["❓ " + question]
            if options:
                for i, o in enumerate(options, 1):
                    lines.append(f"{i}. {o}")
                hint = "Balas dengan nomor"
                if multiple:
                    hint += " (boleh beberapa, pisah koma)"
                lines.append(hint + " atau ketik jawabanmu.")
            emit("info", f"❓ menanyakan di Telegram: {question}")
            q: "queue.Queue[str]" = queue.Queue(maxsize=1)
            _pending[chat_id] = q
            # Kirim pertanyaan & PASTIKAN benar-benar terkirim; kalau gagal, jangan
            # menggantung 10 menit — laporkan agar agent bisa ambil keputusan.
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    bot.send_message(chat_id, "\n".join(lines)), loop)
                fut.result(timeout=20)
            except Exception as e:  # noqa: BLE001
                _pending.pop(chat_id, None)
                return (f"(gagal mengirim pertanyaan ke Telegram: {e}; "
                        f"ambil keputusan paling wajar lalu lanjutkan)")
            try:
                ans = q.get(timeout=600)  # tunggu jawaban hingga 10 menit
            except queue.Empty:
                return ("(pengguna tak menjawab dalam 10 menit — ambil keputusan "
                        "paling wajar lalu lanjutkan)")
            finally:
                _pending.pop(chat_id, None)
            if options:
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
            "bagas-ai via Telegram:\n"
            "• Kirim teks untuk memberi tugas/pertanyaan.\n"
            "• Kirim foto (+caption) untuk dianalisis.\n"
            "• /reset hapus riwayat · /new sesi baru.\n"
            f"Saya bekerja di folder: {config.PROJECT_ROOT}"
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
                pend.put_nowait(update.message.text)
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
                    return agent.run(txt)
                finally:
                    interaction.reset_context_handler(tok)

        reply = await _run_with_typing(update, context, _run, update.message.text)
        emit("out", reply)
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
