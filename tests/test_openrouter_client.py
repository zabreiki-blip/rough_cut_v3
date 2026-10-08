from __future__ import annotations

import unittest
from unittest.mock import patch

import requests

from stage1.config import get_settings
from stage1.openrouter_client import _extract_json, analyze_with_openrouter


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "id": "gen-analysis",
            "usage": {"cost": 0.0025},
            "choices": [
                {
                    "message": {
                        "content": (
                            '```json\n{"items":[{"text_original":"Исходный текст.",'
                            '"text_for_tts":"Исходный текст.","pause_ms":300}]}\n```'
                        )
                    }
                }
            ],
        }


class OpenRouterClientTests(unittest.TestCase):
    def test_analyze_with_openrouter_builds_request_and_parses_json(self) -> None:
        settings = get_settings()
        settings.openrouter_api_key = "test-key"

        usage: list[dict] = []
        with patch("stage1.openrouter_client.requests.post", return_value=_Response()) as post:
            blocks = analyze_with_openrouter(settings, "Исходный текст.", usage.append)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].id, "zk1")
        self.assertEqual(blocks[0].text_for_tts, "Исходный текст.")
        self.assertEqual(usage[0]["generation_id"], "gen-analysis")
        self.assertEqual(usage[0]["cost_usd"], "0.0025")

        request = post.call_args
        self.assertEqual(request.kwargs["json"]["model"], settings.openrouter_model)
        self.assertEqual(request.kwargs["json"]["response_format"], {"type": "json_object"})
        self.assertEqual(request.kwargs["timeout"], 120)
        self.assertEqual(request.kwargs["json"]["max_tokens"], settings.openrouter_max_tokens)
        self.assertGreater(settings.openrouter_max_tokens, 0)

    def test_max_tokens_is_configurable_from_environment(self) -> None:
        with patch.dict("os.environ", {"OPENROUTER_MAX_TOKENS": "1234"}):
            self.assertEqual(get_settings().openrouter_max_tokens, 1234)
        with patch.dict("os.environ", {"OPENROUTER_MAX_TOKENS": "bad"}):
            self.assertGreater(get_settings().openrouter_max_tokens, 0)

    def test_client_error_is_not_retried(self) -> None:
        class BadRequest:
            status_code = 400

            def raise_for_status(self) -> None:
                raise requests.HTTPError("bad request", response=self)

        settings = get_settings()
        settings.openrouter_api_key = "test-key"
        with patch("stage1.openrouter_client.requests.post", return_value=BadRequest()) as post:
            with self.assertRaises(requests.HTTPError):
                analyze_with_openrouter(settings, "Исходный текст.")
        self.assertEqual(post.call_count, 1)

    def _analyze_content(self, content: str) -> list:
        class Reply(_Response):
            def json(self) -> dict:
                return {"choices": [{"message": {"content": content}}]}

        settings = get_settings()
        settings.openrouter_api_key = "test-key"
        with patch("stage1.openrouter_client.requests.post", return_value=Reply()):
            return analyze_with_openrouter(settings, "Исходный текст.")

    def test_lenient_json_extraction_requires_items_object(self) -> None:
        blocks = self._analyze_content(
            'Вот ответ {не json} {"items":[{"text_original":"Текст."}]} конец {x}'
        )
        self.assertEqual(blocks[0].text_original, "Текст.")
        with self.assertRaises(ValueError):
            _extract_json('Мусор {"other": 1} мусор')

    def test_extract_json_skips_object_without_items(self) -> None:
        wrapped = '{"result": {"items": [{"text_original": "Т."}]}}'
        self.assertEqual(_extract_json(wrapped)["items"][0]["text_original"], "Т.")
        self.assertEqual(
            _extract_json("Ответ: " + wrapped + " конец")["items"][0]["text_original"], "Т."
        )
        self.assertEqual(_extract_json('{"meta": 1}\n{"items": [{"a": 1}]}')["items"], [{"a": 1}])
        with self.assertRaises(ValueError):
            _extract_json('{"result": {"other": 1}}')
        with self.assertRaises(ValueError):
            _extract_json('Префикс {"result": {"other": 1}}')

    def test_response_schema_is_validated(self) -> None:
        for content in (
            '{"items":[]}',
            '{"items":"text"}',
            '{"items":["строка"]}',
            '{"items":[{"pause_ms":300}]}',
            '{"other":[]}',
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self._analyze_content(content)


if __name__ == "__main__":
    unittest.main()
