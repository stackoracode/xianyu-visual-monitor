"""Thumbnail evidence on generated, non-private images. Runs with desktop OCR dependencies."""
import pytest
from PIL import Image, ImageDraw

from xianyu_monitor.vision.reply_image import find_outgoing_image

pytest.importorskip('cv2')


def reference():
    image = Image.new('RGB', (640, 360), (230, 240, 250))
    draw = ImageDraw.Draw(image)
    for index in range(18):
        x, y = (index * 73) % 610, (index * 41) % 330
        draw.rectangle((x, y, x + 20, y + 18), fill=(index * 13, 180 - index * 7, 20 + index * 9))
    return image


def test_outgoing_match_requires_content_and_right_alignment():
    source = reference()
    frame = Image.new('RGB', (800, 400), 'white')
    thumb = source.resize((160, 90), Image.Resampling.LANCZOS)
    frame.paste(thumb, (580, 240))
    found = find_outgoing_image(frame, source, (100, 100, 780, 380))
    assert found and found.score > .98 and found.box == (580, 240, 740, 330)
    frame = Image.new('RGB', (800, 400), 'white')
    frame.paste(thumb, (130, 240))
    assert find_outgoing_image(frame, source, (100, 100, 780, 380)) is None
    assert find_outgoing_image(frame, Image.new('RGB', (300, 200), 'black'), (100, 100, 780, 380)) is None


def test_visible_center_of_tall_thumbnail_can_be_verified():
    source = reference()
    frame = Image.new('RGB', (800, 400), 'white')
    thumb = source.resize((300, 169), Image.Resampling.LANCZOS)
    # The fixed product header clips the upper part of the newest outgoing image.
    frame.paste(thumb.crop((0, 20, 300, 169)), (460, 201))
    found = find_outgoing_image(frame, source, (100, 200, 780, 350))
    assert found and found.score > .95 and found.box[0] == 460
