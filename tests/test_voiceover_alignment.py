from __future__ import annotations

import time

import pytest

from stage2.sync_models import WordIndex, WordTimestamp
from voiceover.alignment import PartIndex, align_voiceover
from voiceover.models import VoiceoverRange

SENTENCES = [
    "Город просыпается рано утром",
    "Рабочие выходят на площадь",
    "Строители поднимают новый мост",
    "Река блестит под холодным солнцем",
    "Вечером огни зажигаются на набережной",
    "Музыканты играют старую мелодию",
    "Дети бегут к большому фонтану",
    "Старый маяк освещает дальний берег",
]


class Recording:
    """Synthetic ASR output: each ``say`` produces timed words after a pause."""

    def __init__(self, source_id: str = "rec_a", *, word_sec: float = 0.4) -> None:
        self.words: list[WordTimestamp] = []
        self.cursor = 1.0
        self.word_sec = word_sec
        self.source_id = source_id

    def say(self, text: str, *, pause: float = 3.0) -> tuple[float, float]:
        self.cursor += pause
        start = self.cursor
        for token in text.split():
            self.words.append(WordTimestamp(token, self.cursor, self.cursor + self.word_sec))
            self.cursor += self.word_sec + 0.05
        return start, self.cursor - 0.05

    @property
    def duration(self) -> float:
        return self.cursor + 5.0

    def index(self) -> WordIndex:
        return WordIndex(source_id=self.source_id, words=list(self.words))


def scenario(*items: tuple[str, str] | tuple[str, str, dict]) -> dict:
    blocks = []
    for order, item in enumerate(items, start=1):
        kind, text = item[0], item[1]
        extra = item[2] if len(item) > 2 else {}
        blocks.append(
            {
                "uid": f"b{order}",
                "order": order,
                "type": kind,
                "original_text": text,
                "normalized_text": text,
                "text_for_tts": text,
                "absorbed_by_uid": "",
                **extra,
            }
        )
    return {"status": "confirmed", "blocks": blocks}


def by_uid(ranges: list[VoiceoverRange]) -> dict[str, VoiceoverRange]:
    return {item.block_uid: item for item in ranges}


def test_three_voiceover_blocks_among_sync_blocks() -> None:
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("sync", "СНХ Иванов: это мы слышим от героя"),
        ("voiceover", SENTENCES[1]),
        ("sync", "СНХ Петров: другая цитата героя"),
        ("voiceover", SENTENCES[2]),
    )
    rec = Recording()
    spans = [rec.say(SENTENCES[0]), rec.say(SENTENCES[1]), rec.say(SENTENCES[2])]

    ranges = align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)

    assert [item.block_uid for item in ranges] == ["b1", "b3", "b5"]
    for item, (start, end) in zip(ranges, spans, strict=True):
        assert item.status == "needs_review"
        assert item.recording_id == "rec_a"
        assert item.takes == ()
        assert item.speech_in_sec == pytest.approx(start, abs=0.01)
        assert item.speech_out_sec == pytest.approx(end, abs=0.01)
        assert 0.0 <= item.source_in_sec <= item.speech_in_sec
        assert item.speech_out_sec <= item.source_out_sec <= rec.duration
        assert item.confidence > 0.8
        assert item.text_sha256
    # head/tail padding is used, neighbours are never overlapped
    assert ranges[0].speech_in_sec - ranges[0].source_in_sec == pytest.approx(2.0, abs=0.01)
    for left, right in zip(ranges, ranges[1:]):
        assert left.source_out_sec <= right.source_in_sec


def test_absorbed_blocks_and_other_types_are_skipped() -> None:
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", "этот блок поглощён соседним", {"absorbed_by_uid": "b1"}),
        ("life", "ЛАЙФ шум на площади"),
        ("voiceover", SENTENCES[1]),
    )
    rec = Recording()
    rec.say(SENTENCES[0])
    rec.say(SENTENCES[1])

    ranges = align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)

    assert [item.block_uid for item in ranges] == ["b1", "b4"]
    assert all(item.status == "needs_review" for item in ranges)


def test_spoken_text_is_matched_and_original_is_kept_for_display() -> None:
    spoken = "Тысяча девятьсот восемьдесят четвёртый год был трудным"
    doc = scenario(("voiceover", "1984 год был трудным", {"text_for_tts": spoken}))
    rec = Recording()
    rec.say(spoken)

    (item,) = align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)

    assert item.status == "needs_review"
    assert item.display_text == "1984 год был трудным"


def test_three_full_duplicates_become_takes_and_do_not_shift_next_blocks() -> None:
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", SENTENCES[1]),
        ("voiceover", SENTENCES[2]),
    )
    rec = Recording()
    rec.say(SENTENCES[0])
    first = rec.say(SENTENCES[1], pause=1.5)
    second = rec.say(SENTENCES[1], pause=1.5)
    third = rec.say(SENTENCES[1], pause=1.5)
    last = rec.say(SENTENCES[2])

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    block = result["b2"]
    assert len(block.takes) == 3
    assert [take.partial for take in block.takes] == [False, False, False]
    assert block.selected_take is None  # auto = last full take
    assert block.status == "needs_review"
    assert "дубли: 3" in block.note
    starts = [take.speech_in_sec for take in block.takes]
    assert starts == pytest.approx([first[0], second[0], third[0]], abs=0.01)
    # the default range is the LAST full take
    assert block.speech_in_sec == pytest.approx(third[0], abs=0.01)
    assert block.speech_out_sec == pytest.approx(third[1], abs=0.01)
    assert block.source_in_sec == block.takes[2].source_in_sec
    # takes do not overlap each other and stay inside neighbours
    for left, right in zip(block.takes, block.takes[1:]):
        assert left.source_out_sec <= right.source_in_sec
    # the following block is not shifted
    assert result["b3"].speech_in_sec == pytest.approx(last[0], abs=0.01)
    assert result["b3"].takes == ()
    assert result["b3"].source_in_sec >= block.takes[2].source_out_sec
    assert result["b1"].takes == ()


def test_partial_duplicate_of_one_word_is_flagged_without_cutting_the_middle() -> None:
    doc = scenario(("voiceover", SENTENCES[0]), ("voiceover", SENTENCES[1]))
    rec = Recording()
    rec.say(SENTENCES[0])
    # "рабочие выходят выходят на площадь": one word is repeated inside the block
    span = rec.say("Рабочие выходят выходят на площадь")
    ranges = align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)

    block = by_uid(ranges)["b2"]
    assert block.status == "needs_review"
    assert "частичный дубль" in block.note
    assert any(take.partial for take in block.takes)
    assert block.speech_in_sec == pytest.approx(span[0], abs=0.01)
    assert block.speech_out_sec == pytest.approx(span[1], abs=0.01)
    assert by_uid(ranges)["b1"].takes == ()


def test_restart_prefix_is_a_partial_take_and_excluded_from_the_range() -> None:
    doc = scenario(("voiceover", SENTENCES[2]), ("voiceover", SENTENCES[3]))
    rec = Recording()
    false_start = rec.say("Строители поднимают")  # stumbled and restarted
    clean = rec.say(SENTENCES[2], pause=1.0)
    nxt = rec.say(SENTENCES[3])

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    block = result["b1"]
    assert "частичный дубль" in block.note
    partial_takes = [take for take in block.takes if take.partial]
    assert len(partial_takes) == 1
    assert partial_takes[0].speech_in_sec == pytest.approx(false_start[0], abs=0.01)
    assert block.speech_in_sec == pytest.approx(clean[0], abs=0.01)
    assert block.source_in_sec > false_start[1]
    assert result["b2"].speech_in_sec == pytest.approx(nxt[0], abs=0.01)


def test_same_phrase_in_two_blocks_is_not_a_duplicate() -> None:
    doc = scenario(
        ("voiceover", "Москва не спит"),
        ("voiceover", SENTENCES[4]),
        ("voiceover", "Москва не спит"),
        ("voiceover", SENTENCES[5]),
    )
    rec = Recording()
    first = rec.say("Москва не спит")
    rec.say(SENTENCES[4])
    second = rec.say("Москва не спит")
    rec.say(SENTENCES[5])

    ranges = align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)

    result = by_uid(ranges)
    assert all(item.takes == () for item in ranges)
    assert all("дубли" not in item.note for item in ranges)
    assert result["b1"].speech_in_sec == pytest.approx(first[0], abs=0.01)
    assert result["b3"].speech_in_sec == pytest.approx(second[0], abs=0.01)
    assert [item.block_uid for item in ranges] == ["b1", "b2", "b3", "b4"]


def test_identical_adjacent_blocks_consume_one_reading_each() -> None:
    doc = scenario(
        ("voiceover", "Москва не спит"),
        ("voiceover", "Москва не спит"),
        ("voiceover", SENTENCES[4]),
    )
    rec = Recording()
    first = rec.say("Москва не спит", pause=1.5)
    second = rec.say("Москва не спит", pause=1.5)
    third = rec.say(SENTENCES[4])

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert result["b1"].takes == () and result["b2"].takes == ()
    assert result["b1"].speech_in_sec == pytest.approx(first[0], abs=0.01)
    assert result["b2"].speech_in_sec == pytest.approx(second[0], abs=0.01)
    assert result["b3"].speech_in_sec == pytest.approx(third[0], abs=0.01)


def test_skipped_word_is_still_found_but_skipped_block_is_missing_and_next_is_kept() -> None:
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", SENTENCES[1]),  # not read at all
        ("voiceover", SENTENCES[2]),
        ("voiceover", SENTENCES[3]),
    )
    rec = Recording()
    rec.say("Город просыпается утром")  # word "рано" skipped
    third = rec.say(SENTENCES[2])
    fourth = rec.say(SENTENCES[3])

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert result["b1"].status == "needs_review"
    assert result["b2"].status == "missing"
    assert result["b2"].voice_source == "none"
    assert result["b2"].takes == ()
    assert result["b3"].status == "needs_review"
    assert result["b3"].speech_in_sec == pytest.approx(third[0], abs=0.01)
    assert result["b4"].speech_in_sec == pytest.approx(fourth[0], abs=0.01)
    # a missing proposal survives a JSON round trip
    restored = VoiceoverRange.from_dict(result["b2"].to_dict(), duration_sec=rec.duration)
    assert restored.status == "missing"


def test_unread_blocks_in_a_row_do_not_lose_following_blocks() -> None:
    doc = scenario(
        *[("voiceover", sentence) for sentence in SENTENCES[:6]],
    )
    rec = Recording()
    rec.say(SENTENCES[0])
    rec.say("Тут диктор отвлёкся и сказал что-то совсем постороннее про погоду")
    last = rec.say(SENTENCES[5])

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert [result[f"b{n}"].status for n in range(1, 7)] == [
        "needs_review",
        "missing",
        "missing",
        "missing",
        "missing",
        "needs_review",
    ]
    assert result["b6"].speech_in_sec == pytest.approx(last[0], abs=0.01)


def test_empty_asr_marks_every_block_missing() -> None:
    doc = scenario(("voiceover", SENTENCES[0]), ("voiceover", SENTENCES[1]))
    empty = WordIndex(source_id="rec_a", words=[])

    ranges = align_voiceover(doc, empty, recording_id="rec_a", duration_sec=30.0)

    assert [item.status for item in ranges] == ["missing", "missing"]
    assert all(item.takes == () for item in ranges)


def test_two_recording_parts_are_aligned_in_order_without_intersection() -> None:
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", SENTENCES[1]),
        ("voiceover", SENTENCES[2]),
        ("voiceover", SENTENCES[3]),
    )
    part1 = Recording("rec_p1")
    part1.say(SENTENCES[0])
    part1.say(SENTENCES[1])
    part2 = Recording("rec_p2")
    p2_first = part2.say(SENTENCES[2])
    part2.say(SENTENCES[3])

    ranges = align_voiceover(
        doc,
        [
            PartIndex("rec_p1", part1.index(), part1.duration),
            PartIndex("rec_p2", part2.index(), part2.duration),
        ],
    )

    assert [item.recording_id for item in ranges] == ["rec_p1", "rec_p1", "rec_p2", "rec_p2"]
    assert all(item.status == "needs_review" for item in ranges)
    assert ranges[2].speech_in_sec == pytest.approx(p2_first[0], abs=0.01)
    for part in ("rec_p1", "rec_p2"):
        own = sorted(
            (item for item in ranges if item.recording_id == part),
            key=lambda item: item.source_in_sec,
        )
        for left, right in zip(own, own[1:]):
            assert left.source_out_sec <= right.source_in_sec


def test_second_part_only_searches_remaining_blocks() -> None:
    # Block 3 text also appears at the start of part 2, but part 1 already consumed 1-2;
    # blocks that are read in part 1 are never searched again in part 2.
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", SENTENCES[1]),
        ("voiceover", SENTENCES[2]),
    )
    part1 = Recording("rec_p1")
    part1.say(SENTENCES[0])
    part1.say(SENTENCES[1])
    part2 = Recording("rec_p2")
    part2.say(SENTENCES[0])  # repeated take of block 1 in part 2 must not steal it
    third = part2.say(SENTENCES[2])

    result = by_uid(
        align_voiceover(
            doc,
            [
                PartIndex("rec_p1", part1.index(), part1.duration),
                PartIndex("rec_p2", part2.index(), part2.duration),
            ],
        )
    )

    assert result["b1"].recording_id == "rec_p1"
    assert result["b2"].recording_id == "rec_p1"
    assert result["b3"].recording_id == "rec_p2"
    assert result["b3"].speech_in_sec == pytest.approx(third[0], abs=0.01)


def _pseudo_word(number: int) -> str:
    consonants = "бвгдклмнпрстфх"
    vowels = "аеиоу"
    letters = []
    for _ in range(3):
        letters.append(consonants[number % len(consonants)])
        number //= len(consonants)
        letters.append(vowels[number % len(vowels)])
        number //= len(vowels)
    return "".join(letters) + "т"


def test_long_recording_aligns_in_reasonable_time() -> None:
    blocks = []
    rec = Recording("rec_long", word_sec=0.3)
    expected = []
    counter = 0
    for block_number in range(200):
        words = [_pseudo_word(counter + index * 7) for index in range(25)]
        counter += 25
        text = " ".join(words)
        blocks.append(("voiceover", text))
        expected.append(rec.say(text, pause=1.0))
    doc = scenario(*blocks)

    started = time.monotonic()
    ranges = align_voiceover(doc, rec.index(), recording_id="rec_long", duration_sec=rec.duration)
    elapsed = time.monotonic() - started

    assert elapsed < 15.0
    assert len(ranges) == 200
    assert all(item.status == "needs_review" for item in ranges)
    for item, (start, _end) in zip(ranges, expected, strict=True):
        assert item.speech_in_sec == pytest.approx(start, abs=0.01)


def test_filler_between_readings_that_is_the_next_block_is_not_a_duplicate() -> None:
    first = "Сегодня мы расскажем всё"
    second = "Мост строили годами"
    doc = scenario(("voiceover", first), ("voiceover", second), ("voiceover", first))
    rec = Recording()
    rec.say(first, pause=0.0)
    rec.say(second, pause=0.0)
    rec.say(first, pause=0.0)

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert all(item.status != "missing" for item in result.values())
    assert "дубли" not in result["b1"].note and result["b1"].takes == ()
    assert result["b1"].speech_out_sec < result["b2"].speech_in_sec < result["b3"].speech_in_sec


def test_unmarked_speech_of_a_skipped_block_is_not_padded_into_neighbour() -> None:
    a, b, c = SENTENCES[0], SENTENCES[1], SENTENCES[2]
    doc = scenario(("voiceover", a), ("voiceover", b), ("voiceover", c))
    rec = Recording()
    rec.say(a)
    rec.say(c, pause=0.3)
    rec.say(b, pause=0.3)  # read out of order: found after c, the cursor has moved on

    result = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    found = [item for item in result.values() if item.status != "missing"]
    spans = sorted((item.source_in_sec, item.source_out_sec, item.block_uid) for item in found)
    words = rec.words
    for item in found:
        own = [
            w
            for w in words
            if item.speech_in_sec - 1e-6 <= w.start and w.end <= item.speech_out_sec + 1e-6
        ]
        foreign = [
            w
            for w in words
            if w not in own and item.source_in_sec < w.end and w.start < item.source_out_sec
        ]
        assert not foreign, (item.block_uid, spans)


def test_spelled_numbers_match_digits_in_asr() -> None:
    text = "В две тысячи пятом году в шестидесятые годы было двадцать пять случаев"
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", text),
        ("voiceover", SENTENCES[1]),
    )
    rec = Recording()
    rec.say(SENTENCES[0])
    start, end = rec.say("В 2005 году в 60 -е годы было 25 случаев")
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "needs_review"
    assert ranges["b2"].speech_in_sec == pytest.approx(start, abs=0.01)
    assert ranges["b2"].speech_out_sec == pytest.approx(end, abs=0.01)
    assert ranges["b3"].speech_in_sec > ranges["b2"].speech_out_sec


def test_block_with_unrecognized_head_is_found_by_its_later_words() -> None:
    text = "Март 2005 года. Авиабаза Уоррен в Вайоминге одна из главных баз ракет"
    doc = scenario(
        ("voiceover", SENTENCES[0]),
        ("voiceover", text),
        ("voiceover", SENTENCES[1]),
    )
    rec = Recording()
    rec.say(SENTENCES[0])
    start, end = rec.say("авиабазу Уоррен в Вайоминге одна из главных баз ракет")
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "needs_review"
    assert ranges["b2"].speech_in_sec == pytest.approx(start, abs=0.01)
    assert ranges["b2"].speech_out_sec == pytest.approx(end, abs=0.01)
    assert ranges["b3"].status == "needs_review"
    assert ranges["b3"].speech_in_sec > ranges["b2"].speech_out_sec


def test_unread_tail_does_not_stretch_over_next_block_words() -> None:
    first = (
        "В шестидесятые годы на Тайване была программа по созданию ядерного оружия. "
        "Так что двадцать лет спустя тайваньским инженерам эти устройства без надобности"
    )
    second = (
        "Но что если Тайвань был перевалочным пунктом. Ведь за полтора года груз "
        "можно доставить куда угодно. Что могли узнать иностранные инженеры, изучив так устройства"
    )
    doc = scenario(("voiceover", first), ("voiceover", second), ("voiceover", SENTENCES[1]))
    rec = Recording()
    a_start, a_end = rec.say(
        "В 60 -е годы на Тайване была программа по созданию ядерного оружия", pause=1.0
    )
    b_start, b_end = rec.say(second, pause=0.3)
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "needs_review"
    assert ranges["b2"].speech_in_sec == pytest.approx(b_start, abs=0.01)
    assert ranges["b1"].speech_out_sec <= ranges["b2"].speech_in_sec
    assert ranges["b1"].speech_out_sec == pytest.approx(a_end, abs=0.5)
    assert "частичный дубль" not in ranges["b1"].note
    assert ranges["b3"].status == "needs_review"


def test_rereading_before_the_next_block_keeps_the_last_reading() -> None:
    first = " ".join(["Интересно как раз в нулевые обвиняют то одну то другую страну"] * 2)
    second = (
        "Взрыватели с Тайваня вернули. С другими стрелами повезло меньше. Ядерное оружие исчезло"
    )
    doc = scenario(("voiceover", first), ("voiceover", second), ("voiceover", SENTENCES[1]))
    rec = Recording()
    rec.say("Интересно как раз в нулевые обвиняют то одну то другую страну", pause=1.0)
    _, a_end = rec.say("Интересно как раз в нулевые обвиняют то одну то другую страну", pause=0.1)
    b_start, _ = rec.say(second, pause=1.0)
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "needs_review"
    assert ranges["b2"].speech_in_sec == pytest.approx(b_start, abs=0.01)
    assert ranges["b1"].speech_out_sec <= ranges["b2"].speech_in_sec


def test_partial_duplicate_never_reaches_words_of_the_next_block() -> None:
    first = (
        "Ночью над рекой плывёт густой туман и ветер. "
        "Утром туман рассеивается над старым каменным мостом"
    )
    second = "Утром туман рассеивается над старым каменным мостом. Потом звонят колокола"
    doc = scenario(("voiceover", first), ("voiceover", second), ("voiceover", SENTENCES[1]))
    rec = Recording()
    _, a_end = rec.say("Ночью над рекой плывёт густой густой туман и ветер", pause=1.0)
    b_start, _ = rec.say(second, pause=0.3)
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "needs_review"
    assert ranges["b2"].speech_in_sec == pytest.approx(b_start, abs=0.01)
    assert ranges["b1"].speech_out_sec <= ranges["b2"].speech_in_sec


def test_block_read_only_from_its_start_is_found_with_a_note() -> None:
    text = (
        "Двадцать пятого марта в Вашингтоне история с ядерными взрывателями утекает в прессу "
        "и сразу становится сенсацией. Журналисты не стесняются в выражениях и откровенно "
        "издеваются над генералами: заголовок телеканала начинается со слова упс "
        "и потом долго обсуждают это в студии и в газетах"
    )
    doc = scenario(("voiceover", SENTENCES[0]), ("voiceover", text), ("voiceover", SENTENCES[1]))
    rec = Recording()
    rec.say(SENTENCES[0])
    start, _ = rec.say(
        "25 марта в Вашингтоне история с ядерными взрывателями утекает в прессу "
        "и сразу становится сенсацией"
    )
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "needs_review"
    assert ranges["b2"].speech_in_sec == pytest.approx(start, abs=0.01)
    assert "прочитан не полностью" in ranges["b2"].note


def test_quote_repeated_from_the_middle_of_a_block_is_not_a_found_block() -> None:
    text = (
        "Мне наплевать что она там сказала. Я уверен что они близки к созданию бомбы. "
        "Иран не может иметь никакого ядерного оружия. Всё очень просто"
    )
    doc = scenario(("voiceover", SENTENCES[0]), ("voiceover", text), ("voiceover", SENTENCES[1]))
    rec = Recording()
    rec.say(SENTENCES[0])
    rec.say("Иран не может иметь никакого ядерного оружия. Всё очень просто")
    rec.say(SENTENCES[1])

    ranges = by_uid(
        align_voiceover(doc, rec.index(), recording_id="rec_a", duration_sec=rec.duration)
    )

    assert ranges["b2"].status == "missing"
    assert ranges["b3"].status == "needs_review"
