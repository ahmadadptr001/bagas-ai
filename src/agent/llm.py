"""Wrapper klien NVIDIA (endpoint OpenAI-compatible) + retry tahan-banting.

Poin penting: free tier NVIDIA (~40 request/menit) sering membalas error
throttle yang BENTUKNYA bermacam-macam — bukan cuma HTTP 429/RateLimitError,
tetapi juga pesan seperti "worker local total request limit reached", body
kosong (HTTP 200), atau error server 5xx sesaat. bagasAI harus MENAHAN semua
itu: menunggu dengan sabar lalu MENGULANG langkah yang sama sampai berhasil,
sehingga progres tugas berlanjut dan tidak dibatalkan.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from openai import (
    OpenAI,
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    APIError,
    InternalServerError,
)

from . import config


class EmptyResponseError(Exception):
    """NVIDIA kadang membalas HTTP 200 dengan body kosong saat throttle (40 RPM).

    Diperlakukan sebagai kondisi sementara agar di-retry dengan backoff.
    """


class Cancelled(Exception):
    """Dipakai untuk membatalkan generasi di tengah jalan (mis. Ctrl+C)."""


# Kata kunci pada PESAN error yang menandakan kondisi SEMENTARA (throttle /
# kapasitas / gangguan sesaat). NVIDIA sering mengirim "worker local total
# request limit reached" dengan kode status yang tak terduga, jadi kita juga
# mengklasifikasikan lewat isi pesan, bukan cuma tipe/kode.
_TRANSIENT_KEYWORDS = (
    "request limit", "rate limit", "too many request", "limit reached",
    "overloaded", "capacity", "try again", "temporarily", "unavailable",
    "timeout", "timed out", "connection", "throttl", "429", "server error",
    "bad gateway", "gateway timeout", "worker", "quota", "busy",
)
# Kode status yang FATAL (percuma di-retry): permintaan salah / auth / model
# tidak ada. Selain ini, 5xx dianggap sementara.
_FATAL_STATUS = {400, 401, 403, 404, 405, 422}


def _is_transient(exc: Exception) -> bool:
    """True bila error layak dicoba ulang (rate limit / throttle / gangguan)."""
    if isinstance(exc, Cancelled):
        return False
    if isinstance(
        exc,
        (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            EmptyResponseError,
        ),
    ):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    # Pesan throttle menang atas kode status (kode bisa aneh saat limit).
    if any(k in msg for k in _TRANSIENT_KEYWORDS):
        return True
    status = getattr(exc, "status_code", None)
    if status in _FATAL_STATUS:
        return False
    if isinstance(status, int) and status >= 500:
        return True
    # APIError umum tanpa kode jelas -> anggap sementara (lebih baik menunggu
    # daripada membatalkan tugas pengguna).
    if isinstance(exc, APIError):
        return True
    return False


def _sleep_cancellable(seconds: float, cancel_event: Any) -> None:
    """Tidur `seconds` detik tapi bisa dibatalkan (cek cancel_event / Ctrl+C)."""
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        time.sleep(min(0.2, remaining))


def _call_with_retry(
    do: Callable[[], Any],
    *,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Any:
    """Jalankan `do()` dengan retry SABAR untuk error NVIDIA yang sementara.

    Backoff eksponensial (maks. 60s/percobaan) sampai TOTAL tunggu melewati
    `config.RETRY_MAX_SECONDS`, lalu baru menyerah. Tunggu bisa dibatalkan.
    Saat akan mengulang, `on_retry(attempt, wait, exc)` dipanggil supaya UI bisa
    memberi tahu pengguna bahwa bagasAI menunggu lalu MELANJUTKAN — bukan gagal.
    """
    attempt = 0
    waited = 0.0
    delay = 3.0
    budget = config.RETRY_MAX_SECONDS
    while True:
        attempt += 1
        try:
            return do()
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_transient(exc) or waited >= budget:
                raise
            wait = min(delay, 60.0)
            delay *= 1.8
            waited += wait
            if on_retry:
                try:
                    on_retry(attempt, wait, exc)
                except Exception:
                    pass
            _sleep_cancellable(wait, cancel_event)


# Satu klien dipakai ulang di seluruh aplikasi.
_client: OpenAI | None = None


def get_client() -> OpenAI:
    """Kembalikan klien OpenAI yang diarahkan ke endpoint NVIDIA (lazy init)."""
    global _client
    if _client is None:
        config.require_api_key()
        _client = OpenAI(
            base_url=config.NVIDIA_BASE_URL,
            api_key=config.NVIDIA_API_KEY,
            timeout=config.REQUEST_TIMEOUT,
            max_retries=0,  # retry ditangani _call_with_retry di bawah
        )
    return _client


def _base_kwargs(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str | None,
    temperature: float | None,
    extra_body: dict[str, Any] | None,
    stream: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model or config.CHAT_MODEL,
        "messages": messages,
        "temperature": (
            temperature if temperature is not None else config.TEMPERATURE
        ),
        "stream": stream,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if extra_body:
        kwargs["extra_body"] = extra_body
    if stream:
        kwargs["stream_options"] = {"include_usage": True}
    return kwargs


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    stream: bool = False,
    extra_body: dict[str, Any] | None = None,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> Any:
    """Panggil chat completions (non-stream) dengan retry tahan-banting."""
    client = get_client()
    kwargs = _base_kwargs(messages, tools, model, temperature, extra_body, stream)

    def _do() -> Any:
        response = client.chat.completions.create(**kwargs)
        # Saat throttle, NVIDIA bisa membalas 200 tapi tanpa choices -> anggap
        # sementara agar di-retry.
        if not stream and not getattr(response, "choices", None):
            raise EmptyResponseError(
                "Respons kosong dari NVIDIA (kemungkinan rate limit 40 RPM)."
            )
        return response

    return _call_with_retry(_do, cancel_event=cancel_event, on_retry=on_retry)


def stream_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    temperature: float | None = None,
    extra_body: dict[str, Any] | None = None,
    on_content: Any = None,
    cancel_event: Any = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Streaming chat completion dengan retry tahan-banting.

    Memanggil `on_content(teks)` tiap potongan tiba (token realtime) dan memeriksa
    `cancel_event` tiap chunk agar responsif di-Ctrl+C. Bila NVIDIA rate-limit di
    tengah/awal, seluruh panggilan diulang otomatis (dengan backoff) via
    `_call_with_retry`, dan `on_retry` memberi tahu UI. Mengembalikan
    (teks_final, daftar_tool_calls, usage).
    """
    client = get_client()
    kwargs = _base_kwargs(messages, tools, model, temperature, extra_body, True)

    def _do() -> tuple[str, list[dict[str, Any]], Any]:
        stream = client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage = None
        finish_reason = None
        try:
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = choice.delta
                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
                    if on_content:
                        on_content(piece)
                # Model reasoning (Nemotron/gpt-oss/DeepSeek/dll) mengalirkan
                # "pikiran" di field terpisah. Tangkap agar TIDAK hilang: dipakai
                # sbg jawaban cadangan bila `content` akhirnya kosong, sekaligus
                # menggerakkan penghitung token supaya UI tak terlihat macet.
                rpiece = (getattr(delta, "reasoning_content", None)
                          or getattr(delta, "reasoning", None))
                if rpiece:
                    reasoning_parts.append(rpiece)
                    if on_content:
                        on_content(rpiece)
                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = tool_slots.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn and fn.name:
                        slot["name"] += fn.name
                    if fn and fn.arguments:
                        slot["arguments"] += fn.arguments
        finally:
            try:
                stream.close()
            except Exception:
                pass

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = [tool_slots[i] for i in sorted(tool_slots)]
        # Model hanya "berpikir" tanpa menghasilkan jawaban akhir (mis. anggaran
        # thinking habis): pakai isi pikirannya agar pengguna TETAP dapat respons,
        # bukan layar kosong.
        if not content and reasoning and not tool_calls:
            content = reasoning.strip()
        if not content and not tool_calls:
            # Benar-benar tak ada apa pun. Tanpa sinyal `finish_reason`, ini khas
            # body kosong saat throttle -> perlakukan sementara agar di-retry.
            # Bila ADA finish_reason (model memang berhenti), jangan spam retry:
            # kembalikan kosong, biar core.py yang memberi pesan cadangan.
            if finish_reason is None:
                raise EmptyResponseError(
                    "Stream kosong (kemungkinan rate limit 40 RPM)."
                )
        return content, tool_calls, usage

    return _call_with_retry(_do, cancel_event=cancel_event, on_retry=on_retry)
