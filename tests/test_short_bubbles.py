"""Short incoming bubbles must not merge with detached avatar highlights."""
from PIL import Image, ImageDraw

from xianyu_monitor.vision.pane import find_bubbles
from xianyu_monitor.vision.profile import Colors


def test_short_bubble_ignores_disconnected_avatar_pixels():
    frame = Image.new('RGB', (200, 160), 'white')
    draw = ImageDraw.Draw(frame)
    draw.rectangle((70, 60, 117, 99), fill=(240, 240, 240))
    draw.rectangle((82, 70, 101, 89), fill='black')
    draw.point((24, 67), fill=(240, 240, 240))
    colors = Colors(incoming_bubble=(240, 240, 240), incoming_tolerance=4)
    bubbles = find_bubbles(frame, (0, 0, 200, 160), colors)
    assert [(b.direction, b.box) for b in bubbles] == [('incoming', (70, 60, 118, 100))]


def test_disconnected_bubbles_on_same_rows_stay_separate():
    frame = Image.new('RGB', (400, 160), 'white')
    draw = ImageDraw.Draw(frame)
    for left in (50, 120):
        draw.rectangle((left, 60, left + 47, 99), fill=(240, 240, 240))
    colors = Colors(incoming_bubble=(240, 240, 240), incoming_tolerance=4)
    bubbles = find_bubbles(frame, (0, 0, 400, 160), colors)
    assert [b.box for b in bubbles] == [(50, 60, 98, 100), (120, 60, 168, 100)]
