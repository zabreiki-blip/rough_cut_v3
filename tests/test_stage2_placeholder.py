from unittest.mock import patch

from PIL import ImageFont

from stage2 import placeholder


def test_get_font_caches_each_size() -> None:
    placeholder._get_font.cache_clear()
    sentinel = ImageFont.load_default()

    with patch.object(placeholder.ImageFont, "truetype", return_value=sentinel) as load:
        assert placeholder._get_font(52) is sentinel
        assert placeholder._get_font(52) is sentinel

    load.assert_called_once()
    placeholder._get_font.cache_clear()
