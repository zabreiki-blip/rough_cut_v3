"""Проверка кандидата синхрона «по звуку» аудио-LLM (пока только оценка).

Фрагмент интервью [start - margin, end + margin] вырезается из WAV, уходит в ту
же аудио-модель и тем же способом, что и облачный ASR (`cloud_asr`:
OpenRouter `input_audio` + `usage.include`, те же заголовки и разбор стоимости),
и модель сравнивает услышанное с цитатой из сценария. Таймкоды модель не
выдаёт — только вердикт, покрытие цитаты и услышанный текст.

Любая сетевая ошибка или нечитаемый ответ превращается в вердикт ``error`` с
причиной; исчерпание бюджета (`CostBudgetExceeded`) пробрасывается наружу,
чтобы пакетный вызов мог остановиться.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from cost_budget import CostBudget

from .cloud_asr import (
    DEFAULT_MODEL,
    CloudAudioAsrAdapter,
    CloudAudioAsrError,
    _load_json_payload,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
AUDIO_FORMAT = "mp3"  # формат, который облачный ASR проекта отправляет в input_audio
DEFAULT_MARGIN_SEC = 0.5
VERIFY_MAX_OUTPUT_TOKENS = 2048
_FFMPEG_TIMEOUT_SEC = 300.0
_PCM_BYTES_PER_SEC = 16_000 * 2  # mono s16le 16 kHz
_VERDICTS = ("match", "partial", "no_match")
# Observed ~0.0035 $/min for the audio model; doubled as a safety margin for unknown costs.
DEFAULT_USD_PER_MINUTE = Decimal("0.007")
RATE_ENV = "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE"
RETRY_PARSE_ENV = "SYNC_AUDIO_VERIFY_RETRY_PARSE"
_TRUE_VALUES = {"1", "true", "yes", "on"}

_PROMPT_RU = (
    "Ты проверяешь синхрон интервью. Послушай аудиофрагмент и сравни услышанную "
    "речь с цитатой из сценария.\n"
    "Говорящий: {speaker}\n"
    "Цитата из сценария: «{quote}»\n\n"
    "Правила:\n"
    "- Цитата могла быть слегка отредактирована: пунктуация, мелкие слова, "
    "слова-паразиты, повторы — это всё ещё совпадение.\n"
    "- Пересказ своими словами или другая фраза на ту же тему — это no_match.\n"
    "- match: цитата прозвучала в фрагменте целиком (с допустимыми правками).\n"
    "- partial: прозвучала только часть цитаты (coverage — доля цитаты, 0..1).\n"
    "- no_match: цитаты в фрагменте нет.\n"
    "- Таймкоды не указывай.\n\n"
    "Верни строго один JSON-объект без пояснений и markdown:\n"
    '{{"verdict": "match|partial|no_match", "coverage": 0.0, '
    '"heard_text": "дословно услышанная речь", "confidence": 0.0}}'
)


@dataclass
class AudioVerdict:
    verdict: str  # "match" | "partial" | "no_match" | "error"
    coverage: float = 0.0
    heard_text: str = ""
    confidence: float = 0.0
    model: str = ""
    cost_usd: float | None = None
    reason: str = ""
    cached: bool = False
    paid: bool = False  # a billed request was made (an unreadable answer is cached, not repeated)
    cost_estimated: bool = False  # cost_usd is our estimate, the provider reported none

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# request_fn(audio_b64=..., audio_format=..., prompt=..., model=..., duration_sec=...)
#   -> (content, cost_usd | None, raw_response)
RequestFn = Callable[..., tuple[str, "float | None", dict]]


def resolve_model(settings: Mapping[str, str] | None = None) -> str:
    env = os.environ if settings is None else settings
    for key in ("SYNC_AUDIO_VERIFY_MODEL", "SYNC_CLOUD_ASR_MODEL"):
        value = str(env.get(key) or "").strip()
        if value:
            return value
    return DEFAULT_MODEL


def clip_usd_per_minute(settings: Mapping[str, str] | None = None) -> Decimal:
    """Rate for estimating the cost of one clip when the provider reports none."""
    env = os.environ if settings is None else settings
    try:
        value = Decimal(str(env.get(RATE_ENV) or "").strip())
    except InvalidOperation:
        return DEFAULT_USD_PER_MINUTE
    return value if value.is_finite() and value > 0 else DEFAULT_USD_PER_MINUTE


def estimate_clip_usd(duration_sec: float, settings: Mapping[str, str] | None = None) -> Decimal:
    return Decimal(str(max(0.0, duration_sec))) * clip_usd_per_minute(settings) / Decimal("60")


def build_prompt(quote: str, speaker: str) -> str:
    return _PROMPT_RU.format(
        speaker=(speaker or "не указан").strip(),
        quote=" ".join(str(quote or "").split()),
    )


def _run_ffmpeg(args: list[str], data: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            args,
            input=data,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SEC,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CloudAudioAsrError(f"Не найден исполняемый файл {args[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CloudAudioAsrError("ffmpeg не ответил вовремя при нарезке фрагмента") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise CloudAudioAsrError(f"ffmpeg завершился с кодом {proc.returncode}: {detail}")
    return proc.stdout


def extract_pcm(audio_path: Path, start_sec: float, end_sec: float, *, ffmpeg: str = "ffmpeg") -> bytes:
    """Фрагмент как сырой PCM s16le mono 16 кГц (для хэша кэша и кодирования)."""
    duration = max(0.01, end_sec - start_sec)
    return _run_ffmpeg(
        [
            ffmpeg, "-nostdin", "-y", "-loglevel", "error",
            "-ss", f"{max(0.0, start_sec):.3f}", "-t", f"{duration:.3f}",
            "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "pipe:1",
        ]
    )  # fmt: skip


def encode_mp3(pcm: bytes, *, ffmpeg: str = "ffmpeg") -> bytes:
    """PCM -> mp3 mono 16 кГц: тот формат, что облачный ASR кладёт в input_audio."""
    return _run_ffmpeg(
        [
            ffmpeg, "-nostdin", "-y", "-loglevel", "error",
            "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-f", "mp3", "pipe:1",
        ],
        pcm,
    )  # fmt: skip


def cache_key(pcm: bytes, quote: str, model: str) -> str:
    digest = hashlib.sha256(pcm).hexdigest()
    payload = json.dumps([digest, quote, model, PROMPT_VERSION], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def parse_verdict_content(content: str) -> dict[str, Any]:
    """Разбирает JSON-ответ модели; ValueError при нечитаемом/невалидном ответе."""
    payload = _load_json_payload(content or "")
    if not isinstance(payload, dict):
        raise ValueError("ответ модели не является JSON-объектом")
    verdict = str(payload.get("verdict") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if verdict not in _VERDICTS:
        raise ValueError(f"недопустимый verdict: {payload.get('verdict')!r}")
    return {
        "verdict": verdict,
        "coverage": _clamp01(payload.get("coverage")),
        "heard_text": str(payload.get("heard_text") or "").strip(),
        "confidence": _clamp01(payload.get("confidence")),
    }


def make_default_request_fn(settings: Mapping[str, str] | None = None) -> RequestFn:
    """Запрос тем же путём, что и облачный ASR: `CloudAudioAsrAdapter._call_model`."""
    env = os.environ if settings is None else settings
    api_key = None
    for key in ("SYNC_CLOUD_ASR_API_KEY", "OPENROUTER_API_KEY"):
        api_key = str(env.get(key) or "").strip() or None
        if api_key:
            break
    base_url = str(env.get("OPENROUTER_BASE_URL") or "").strip() or None

    def request(
        *, audio_b64: str, audio_format: str, prompt: str, model: str, duration_sec: float
    ) -> tuple[str, float | None, dict]:
        del audio_format  # _call_model отправляет mp3, как и весь облачный ASR
        adapter = CloudAudioAsrAdapter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout_sec=120.0,
            max_retries=2,
        )
        adapter._require_available()
        adapter._max_output_tokens = VERIFY_MAX_OUTPUT_TOKENS

        async def run() -> tuple[str, float | None, dict]:
            async with httpx.AsyncClient() as client:
                return await adapter._call_model(
                    client, audio_b64, prompt=prompt, duration_sec=duration_sec
                )

        return asyncio.run(run())

    return request


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_cache(path: Path, verdict: AudioVerdict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique tmp in the same folder: parallel writers of one key never clash.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.stem + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(verdict.to_dict(), ensure_ascii=False))
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError:
        logger.debug("Не удалось записать кэш sync_audio_verify", exc_info=True)


def verify_clip(
    *,
    audio_path: Path | str,
    start_sec: float,
    end_sec: float,
    quote: str,
    speaker: str,
    settings: Mapping[str, str] | None = None,
    request_fn: RequestFn | None = None,
    budget: CostBudget | None = None,
    usage_callback: Callable[[dict[str, Any]], Any] | None = None,
    margin_sec: float = DEFAULT_MARGIN_SEC,
    cache_dir: Path | str | None = None,
    retry_parse_errors: bool | None = None,
) -> AudioVerdict:
    model = resolve_model(settings)
    if retry_parse_errors is None:
        env = os.environ if settings is None else settings
        retry_parse_errors = str(env.get(RETRY_PARSE_ENV) or "").strip().lower() in _TRUE_VALUES
    try:
        pcm = extract_pcm(
            Path(audio_path), max(0.0, start_sec - margin_sec), end_sec + margin_sec
        )
        if not pcm:
            raise CloudAudioAsrError("пустой аудио-фрагмент")
    except CloudAudioAsrError as exc:
        return AudioVerdict("error", model=model, reason=f"extract: {exc}")

    cache_path = None
    if cache_dir is not None:
        cache_path = Path(cache_dir) / f"{cache_key(pcm, quote, model)}.json"
        cached = _read_cache(cache_path)
        if cached is not None:
            try:
                result = AudioVerdict(**cached)
            except TypeError:
                result = None
            if result is not None and (
                result.verdict in _VERDICTS
                # A paid answer we could not parse is not paid for again, unless asked.
                or (result.verdict == "error" and result.paid and not retry_parse_errors)
            ):
                result.cached = True
                return result

    duration_sec = len(pcm) / _PCM_BYTES_PER_SEC
    try:
        audio_b64 = base64.b64encode(encode_mp3(pcm)).decode("ascii")
    except CloudAudioAsrError as exc:
        return AudioVerdict("error", model=model, reason=f"encode: {exc}")

    call = request_fn or make_default_request_fn(settings)
    estimate = estimate_clip_usd(duration_sec, settings)
    # CostBudgetExceeded намеренно не перехватывается: пакет должен остановиться.
    reservation_id = (
        budget.reserve_usd(estimate, "Audio verification clip") if budget is not None else None
    )
    try:
        content, cost, raw = call(
            audio_b64=audio_b64,
            audio_format=AUDIO_FORMAT,
            prompt=build_prompt(quote, speaker),
            model=model,
            duration_sec=duration_sec,
        )
    except BaseException as exc:
        # Provably unsent (no connection) is free; anything else may have been billed,
        # so the estimate is booked as spent instead of staying reserved forever.
        unsent = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
        if budget is not None and reservation_id is not None:
            if unsent:
                budget.release(reservation_id)
            else:
                budget.settle(reservation_id, None, estimate_if_unknown=True)
        if not isinstance(exc, Exception):
            raise  # cancellation / interpreter exit propagates after the books are settled
        return AudioVerdict(
            "error",
            model=model,
            reason=f"request: {exc}",
            cost_usd=None if unsent else float(estimate),
            cost_estimated=not unsent,
        )
    if budget is not None and reservation_id is not None:
        budget.settle(reservation_id, cost, estimate_if_unknown=True)
    cost_estimated = cost is None
    effective_cost = float(estimate) if cost is None else cost
    if usage_callback is not None:
        try:
            usage_callback(
                {
                    "model": model,
                    "cost_usd": cost,
                    "generation_id": str((raw or {}).get("id") or ""),
                }
            )
        except Exception:
            logger.debug("sync_audio_verify usage callback failed", exc_info=True)

    try:
        parsed = parse_verdict_content(content)
    except ValueError as exc:
        failed = AudioVerdict(
            "error",
            model=model,
            cost_usd=effective_cost,
            reason=f"parse: {exc}",
            paid=True,
            cost_estimated=cost_estimated,
        )
        if cache_path is not None:
            _write_cache(cache_path, failed)
        return failed
    result = AudioVerdict(
        model=model, cost_usd=effective_cost, paid=True, cost_estimated=cost_estimated, **parsed
    )
    if cache_path is not None:
        _write_cache(cache_path, result)
    return result
