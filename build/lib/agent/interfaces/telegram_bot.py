"""Antarmuka Bot Telegram (python-telegram-bot v21, async)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import config
from ..core import Agent
from ..tools import vision

# Satu Agent per chat_id (sesi terpisah).
_agents: dict[int, Agent] = {}


def _get_agent(chat_id: int) -> Agent:
    if chat_id not in _agents:
        _agents[chat_id] = Agent()
    return _agents[chat_id]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Halo! Saya AI agent berbasis API gratis NVIDIA.\n"
        "Kirim pertanyaan, foto (untuk dianalisis), atau /reset untuk "
        "menghapus riwayat."
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _get_agent(update.effective_chat.id).reset()
    await update.message.reply_text("(riwayat percakapan dihapus)")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    agent = _get_agent(chat_id)
    await context.bot.send_chat_action(chat_id, "typing")
    # agent.run bersifat blocking -> jalankan di thread agar event loop bebas.
    reply = await asyncio.to_thread(agent.run, update.message.text)
    await update.message.reply_text(reply)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, "typing")
    caption = update.message.caption or "Deskripsikan gambar ini secara detail."

    photo = update.message.photo[-1]  # resolusi tertinggi
    tg_file = await photo.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "image.jpg"
        await tg_file.download_to_drive(str(path))
        reply = await asyncio.to_thread(vision.analyze_image, path, caption)
    await update.message.reply_text(reply)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Fitur suara (speech-to-text) belum diaktifkan. Silakan kirim teks "
        "atau foto untuk saat ini."
    )


def main() -> None:
    config.require_api_key()
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diisi di .env. Dapatkan dari @BotFather."
        )

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("Bot Telegram berjalan. Tekan Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()
