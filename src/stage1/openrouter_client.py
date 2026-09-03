from __future__ import annotations

import json
import logging
import re
from typing import Any

try:
    import requests
    from requests.exceptions import ConnectionError, Timeout
except ImportError:
    requests = None
    ConnectionError = OSError  # type: ignore[misc]
    Timeout = OSError  # type: ignore[misc]

try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
except ImportError:
    retry = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

from .config import Settings
from .models import NarrationBlock


SYSTEM_PROMPT = """Ты редактор закадрового текста для телевизионного диктора.

Твоя задача — извлечь ТОЛЬКО закадровый (дикторский/авторский) текст из сценария.

ИСКЛЮЧАЙ (НЕ включай в результат):
- Реплики ведущих: строки с "ВЕДУЩИЙ-1", "ВЕДУЩИЙ-2" и т.д.
- Синхроны экспертов: строки с "СНХ", имена экспертов, описания файлов
- Таймкоды интервью: строки начинающиеся с [06:34], (…), и цитаты экспертов
- Технические метки: ШАПКА, ОТБИВКА, ТИТР, ПЛАШКА, ВИДЕО, ЛАЙФ, ГРАФИКА, КАРТА, ФОТО
- Ремарки в скобках: (За спиной Ведущего...), (Ставит на стол...)
- Прямую речь героев/экспертов из интервью

ВКЛЮЧАЙ:
- Авторский закадровый текст (повествование от автора)
- Фактологические абзацы без привязки к конкретному спикеру

Пример сценария:
---
В 1958 году стратеги Армии США задумали невероятное.
СНХ Олег Шаландин. Файл УМП Шаландин гренландия. С0003.
[06:34] (…) Предполагалось, что межконтинентальные баллистические ракеты...
Проект «Ледяной червь» предполагал прокладку 4000 километров тоннелей.
ВЕДУЩИЙ-2. (Ведущий с планшетом.)
В апреле 2024 года над Гренландией совершал полёт Gulfstream III.
ОТБИВКА. НАЗВАНИЕ ПРОГРАММЫ
---
Правильный результат — ТОЛЬКО:
- "В 1958 году стратеги Армии США задумали невероятное."
- "Проект «Ледяной червь» предполагал прокладку 4000 километров тоннелей."
(строки ведущего, СНХ, таймкоды, отбивки — исключены)

Верни JSON-объект формата {"items":[...]}.
Без markdown, без комментариев, без лишнего текста.
"""


def _extract_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("{"):
        return json.loads(content)

    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, flags=re.S)
    if fence_match:
        return json.loads(fence_match.group(1))

    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(content[start : end + 1])
    raise ValueError("LLM response does not contain JSON object")


def _normalize_blocks(raw_items: list[dict[str, Any]]) -> list[NarrationBlock]:
    blocks: list[NarrationBlock] = []
    for i, item in enumerate(raw_items, start=1):
        block_id = f"zk{i}"
        text_original = str(item.get("text_original", "")).strip()
        text_for_tts = str(item.get("text_for_tts", "")).strip() or text_original
        pause_ms = item.get("pause_ms", 300)
        try:
            pause_ms = int(pause_ms)
        except (TypeError, ValueError):
            pause_ms = 300
        notes = str(item.get("notes", "")).strip()
        blocks.append(
            NarrationBlock(
                id=block_id,
                text_original=text_original,
                text_for_tts=text_for_tts,
                pause_ms=pause_ms,
                notes=notes,
            )
        )
    return blocks


def analyze_with_openrouter(settings: Settings, scenario_text: str) -> list[NarrationBlock]:
    if requests is None:
        raise RuntimeError("requests is not installed. Install dependencies from requirements.txt.")

    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    user_prompt = f"""
Извлеки ТОЛЬКО закадровый дикторский текст из сценария ниже.

Правила:
1) Каждый блок: {{"id":"zk1", "text_original":"...", "text_for_tts":"...", "pause_ms":300, "notes":""}}.
2) text_for_tts — естественный для озвучки (числа, даты, аббревиатуры → словами при необходимости).
3) НЕ включай: строки ВЕДУЩИЙ, СНХ, таймкоды [XX:XX], (…), ОТБИВКА, ШАПКА, ТИТР, ВИДЕО, ЛАЙФ, ремарки в скобках.
4) НЕ включай цитаты экспертов из интервью (даже без таймкода).
5) Формат ответа: {{"items":[...]}}.

Сценарий:
{scenario_text}
""".strip()

    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }

    def _do_request():
        resp = requests.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp

    if retry is not None:

        def _is_retryable(exc: BaseException) -> bool:
            if isinstance(exc, (ConnectionError, Timeout)):
                return True
            import requests as _req

            if isinstance(exc, _req.HTTPError):
                code = exc.response.status_code if exc.response is not None else 0
                return code in (429, 500, 502, 503, 504)
            return False

        _do_request_with_retry = retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=2, min=4, max=30),
            reraise=True,
        )(_do_request)
        logger.debug("OpenRouter: отправляю запрос (retry включён)")
        response = _do_request_with_retry()
    else:
        logger.debug("OpenRouter: tenacity не установлен, retry отключён")
        response = _do_request()

    data = response.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json(content if isinstance(content, str) else json.dumps(content))
    items = parsed.get("items", [])
    if not isinstance(items, list) or not items:
        raise ValueError("OpenRouter returned empty or invalid items")
    return _normalize_blocks(items)
