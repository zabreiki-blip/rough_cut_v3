import pytest
from docx import Document

from translated_sync.transcript import TranscriptCue, parse_docx_transcript


def _doc(tmp_path, blocks):
    doc = Document()
    for kind, value in blocks:
        if kind == "table":
            table = doc.add_table(rows=1, cols=1)
            table.cell(0, 0).text = value
        else:
            p = doc.add_paragraph()
            run = p.add_run(value)
            if kind == "bold":
                run.bold = True
    path = tmp_path / "input.docx"
    doc.save(path)
    return path


def test_real_export_bilingual_and_pronunciation_excluded(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "Source: camera A"),
            ("plain", "ОТВЕТ  Анна\n[00:00:02:00] Hello from London."),
            ("plain", "Транскрипция кириллицей (приблизительно): Хеллоу"),
            ("bold", "Привет из Лондона."),
        ],
    )
    cue = parse_docx_transcript(path, "src", duration_sec=20)[0]
    assert cue.speaker == "Анна"
    assert cue.original_text == "Hello from London."
    assert cue.russian_text == "Привет из Лондона."
    assert cue.start_sec == pytest.approx(2)
    assert cue.end_sec == pytest.approx(20)
    assert cue.approximate


def test_russian_only_range_tables_order_and_sparse_anchor(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "SPEAKER: Борис"),
            ("plain", "[00:00:02:00-00:00:04:00] Русский текст в абзаце"),
            ("table", "[00:00:05:00] Следующий русский текст"),
        ],
    )
    cues = parse_docx_transcript(path, "src", duration_sec=10)
    assert [c.russian_text for c in cues] == ["Русский текст в абзаце", "Следующий русский текст"]
    assert (cues[0].start_sec, cues[0].end_sec, cues[0].approximate) == (2, 4, False)
    assert (cues[1].start_sec, cues[1].end_sec, cues[1].approximate) == (5, 10, True)


def test_display_offset_and_stable_ids_ignore_filename(tmp_path):
    blocks = [("plain", "[01:00:02:00] Неударное начало"), ("bold", "Переведено.")]
    one = parse_docx_transcript(
        _doc(tmp_path, blocks),
        "s",
        duration_sec=10,
        timecode_mode="display",
        sequence_start_sec=3600,
    )
    another_path = tmp_path / "renamed.docx"
    _doc(tmp_path, blocks).replace(another_path)
    two = parse_docx_transcript(
        another_path, "s", duration_sec=10, timecode_mode="display", sequence_start_sec=3600
    )
    assert one[0].start_sec == 2
    assert one[0].id == two[0].id


def test_explicit_translation_and_equal_anchors_end_at_next_greater_time(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "[00:00:01:00] Original words"),
            ("plain", "Перевод: Первый перевод"),
            ("plain", "[00:00:01:00] More original words"),
            ("plain", "Перевод: Второй перевод"),
            ("plain", "[00:00:03:00] Следующий якорь"),
        ],
    )
    cues = parse_docx_transcript(path, "s", duration_sec=8)
    assert [cue.russian_text for cue in cues] == [
        "Первый перевод",
        "Второй перевод",
        "Следующий якорь",
    ]
    assert cues[0].end_sec == cues[1].end_sec == 3


@pytest.mark.parametrize("duration,fps", [(float("inf"), 25), (10, float("nan"))])
def test_non_finite_media_parameters_rejected(tmp_path, duration, fps):
    path = _doc(tmp_path, [("plain", "[00:00:01:00] Русский текст")])
    with pytest.raises(ValueError):
        parse_docx_transcript(path, "s", duration_sec=duration, fps=fps)


def test_headings_without_timed_russian_are_not_cues(tmp_path):
    path = _doc(tmp_path, [("plain", "Заголовок камеры"), ("plain", "Локация: студия")])
    with pytest.raises(ValueError, match="Russian transcript cues"):
        parse_docx_transcript(path, "s", duration_sec=10)


def test_untimed_bold_continuation_is_coarse_and_anchor_bounded(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "Source: camera A"),
            ("plain", "ОТВЕТ Анна\n[00:00:01:00] First original."),
            ("bold", "Первая переведённая фраза."),
            ("bold", "Продолжение перевода без метки."),
            ("plain", "[00:00:04:00] Next foreign original words."),
            ("bold", "Следующая реплика."),
        ],
    )
    cues = parse_docx_transcript(path, "s", duration_sec=10)
    assert [cue.russian_text for cue in cues] == [
        "Первая переведённая фраза.",
        "Продолжение перевода без метки.",
        "Следующая реплика.",
    ]
    assert cues[0].end_sec == cues[1].end_sec == 4
    assert cues[1].approximate and cues[1].start_sec == 1
    assert cues[2].start_sec == 4


def test_explicit_range_and_foreign_only_anchor_bound_prior_russian(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "[00:00:01:00] Первый русский фрагмент"),
            ("plain", "[00:00:03:00-00:00:05:00] Foreign only text."),
            ("plain", "[00:00:06:00] Второй русский фрагмент"),
        ],
    )
    cues = parse_docx_transcript(path, "s", duration_sec=10)
    assert cues[0].end_sec == 3
    assert cues[1].end_sec == 10


@pytest.mark.parametrize(
    "line", ["[00:60:00:00] Текст", "[00:00:60:00] Текст", "[00:00:01:25] Текст"]
)
def test_invalid_time_fields_rejected(tmp_path, line):
    path = _doc(tmp_path, [("plain", line)])
    with pytest.raises(ValueError, match="timecode"):
        parse_docx_transcript(path, "s", duration_sec=100)


def test_multiple_source_headings_rejected(tmp_path):
    doc = Document()
    doc.add_heading("CAM_A.wav", level=1)
    doc.add_paragraph("[00:00:01:00] Первый русский текст")
    doc.add_heading("CAM_B.wav", level=1)
    doc.add_paragraph("[00:00:02:00] Второй русский текст")
    path = tmp_path / "multi.docx"
    doc.save(path)
    with pytest.raises(ValueError, match="multiple media sources"):
        parse_docx_transcript(path, "s", duration_sec=10)


@pytest.mark.parametrize("line", ["[nonsense] Русский текст", "[00:01:00:00] Русский текст"])
def test_bad_or_out_of_bounds_timing_rejected(tmp_path, line):
    path = _doc(tmp_path, [("plain", line)])
    with pytest.raises(ValueError):
        parse_docx_transcript(path, "s", duration_sec=10)


def test_cue_round_trip():
    cue = TranscriptCue("i", "s", "Анна", "Русский текст", "Hello", 1, 2, True, 0)
    assert TranscriptCue.from_dict(cue.to_dict()) == cue


def test_approximate_short_and_fractional_timecodes(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "[~01:02.5] Пример текста"),
            ("plain", "[00:01:04.25 - 00:01:05.5] Второй текст"),
        ],
    )
    cues = parse_docx_transcript(path, "s", duration_sec=70)
    assert (cues[0].start_sec, cues[0].end_sec, cues[0].approximate) == (62.5, 64.25, True)
    assert (cues[1].start_sec, cues[1].end_sec, cues[1].approximate) == (64.25, 65.5, False)


def test_plain_translations_grouped_under_foreign_anchor(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "[00:00:02:00] ОТВЕТ: Hello world"),
            ("plain", "Первое предложение."),
            ("plain", "Второе предложение."),
            ("plain", "外国語"),
            ("plain", "Третье предложение."),
            ("plain", "[00:00:08:00] Next answer"),
            ("plain", "Другая реплика."),
        ],
    )
    cues = parse_docx_transcript(path, "s", duration_sec=12)
    assert len(cues) == 2
    assert cues[0].russian_text == "Первое предложение. Второе предложение. Третье предложение."
    assert cues[0].original_text == "ОТВЕТ: Hello world"
    assert (cues[0].start_sec, cues[0].end_sec) == (2, 8)


def test_foreign_script_with_cyrillic_label_is_not_a_translation(tmp_path):
    path = _doc(
        tmp_path,
        [
            ("plain", "[00:00:02:00] ОТВЕТ: བོད་ཡིག"),
            ("plain", "Настоящий перевод."),
        ],
    )
    cue = parse_docx_transcript(path, "s", duration_sec=10)[0]
    assert cue.russian_text == "Настоящий перевод."
    assert cue.original_text == "ОТВЕТ: བོད་ཡིག"
