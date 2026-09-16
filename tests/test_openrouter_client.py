from __future__ import annotations

import unittest
from unittest.mock import patch

from stage1.config import get_settings
from stage1.openrouter_client import analyze_with_openrouter


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


if __name__ == "__main__":
    unittest.main()
