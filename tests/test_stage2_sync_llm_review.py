import pytest

from stage1.config import get_settings
from stage2.sync_llm_review import SemanticCandidate, review_candidates_with_llm


class _Response:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "id": "gen-review",
            "usage": {"cost": 0.0011},
            "choices": [{"message": {"content": self.content}}],
        }


def _settings():
    settings = get_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test/model"
    settings.openrouter_base_url = "https://example.test/v1"
    return settings


def _candidates():
    return [
        SemanticCandidate("source", 10, 15, 0.61, "первый текст"),
        SemanticCandidate("source", 20, 27, 0.58, "нужное смысловое совпадение"),
    ]


def test_llm_can_only_choose_a_supplied_candidate():
    calls = []
    usage = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return _Response('{"selected_index":1,"confidence":0.91,"reason":"тот же смысл"}')

    result = review_candidates_with_llm(
        query_text="цитата",
        speaker="Спикер",
        candidates=_candidates(),
        settings=_settings(),
        request_fn=request,
        usage_callback=usage.append,
    )
    assert result is not None
    assert result.candidate_index == 1
    assert result.confidence == 0.91
    assert calls[0][0] == "https://example.test/v1/chat/completions"
    assert calls[0][1]["json"]["temperature"] == 0
    assert usage[0]["generation_id"] == "gen-review"
    assert usage[0]["cost_usd"] == "0.0011"


def test_llm_may_reject_every_candidate():
    result = review_candidates_with_llm(
        query_text="цитата",
        speaker="",
        candidates=_candidates(),
        settings=_settings(),
        request_fn=lambda *_args, **_kwargs: _Response(
            '{"selected_index":-1,"confidence":0.2,"reason":"нет совпадения"}'
        ),
    )
    assert result is None


@pytest.mark.parametrize("index", [2, 99, -2])
def test_llm_cannot_invent_a_candidate(index):
    with pytest.raises(ValueError, match="unknown candidate"):
        review_candidates_with_llm(
            query_text="цитата",
            speaker="",
            candidates=_candidates(),
            settings=_settings(),
            request_fn=lambda *_args, **_kwargs: _Response(
                f'{{"selected_index":{index},"confidence":0.9,"reason":""}}'
            ),
        )


def test_llm_is_not_called_without_an_api_key():
    settings = _settings()
    settings.openrouter_api_key = ""
    result = review_candidates_with_llm(
        query_text="цитата",
        speaker="",
        candidates=_candidates(),
        settings=settings,
        request_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    assert result is None
