"""Exact-pixel OCR result cache, raw-pixel IPC payloads and CPU pinning helpers."""

from PIL import Image, ImageDraw

from xianyu_monitor import cpu
from xianyu_monitor.ocr import pool as pool_mod
from xianyu_monitor.ocr.base import FakeOcrEngine
from xianyu_monitor.ocr.pool import BLOCK_PREFIX, BUBBLE_PREFIX, OcrPool


def crop(text: str, size=(80, 20), mode="RGB") -> Image.Image:
    image = Image.new(mode, size, "white")
    ImageDraw.Draw(image).text((2, 2), text, fill="black")
    return image


def make_pool(cache_size=16):
    fake = FakeOcrEngine()
    return fake, OcrPool("fake", engine_factory=lambda: fake, use_processes=False, cache_size=cache_size)


async def test_identical_pixels_are_read_once_and_changed_pixels_again():
    fake, pool = make_pool()
    a, b = crop("a"), crop("b")
    fake.register(a, "A")
    fake.register(b, "B")
    first = await pool.recognize([("r0.name", a), ("r1.name", b)])
    assert first["r0.name"].text == "A" and fake.calls == 2
    again = await pool.recognize([("title", a.copy()), ("r1.name", b)])
    assert again["title"].text == "A" and again["r1.name"].text == "B" and fake.calls == 2
    changed = a.copy()
    changed.putpixel((0, 0), (0, 0, 0))
    await pool.recognize([("r0.name", changed)])
    assert fake.calls == 3
    assert pool.stats()["cache_hits"] == 2 and pool.stats()["cache_misses"] == 3


async def test_cache_keeps_line_block_and_bubble_modes_apart():
    fake, pool = make_pool()
    image = crop("x")
    fake.register(image, "X")
    await pool.recognize([("line", image)])
    await pool.recognize([(BLOCK_PREFIX + "b", image)])
    await pool.recognize([(BUBBLE_PREFIX + "c", image)])
    assert fake.calls == 3


async def test_duplicate_crops_in_one_batch_run_once():
    fake, pool = make_pool()
    image = crop("dup")
    fake.register(image, "DUP")
    result = await pool.recognize([("a", image), ("b", image.copy())])
    assert result["a"].text == result["b"].text == "DUP" and fake.calls == 1


async def test_cache_is_bounded_and_can_be_disabled():
    fake, pool = make_pool(cache_size=2)
    images = [crop(str(i)) for i in range(3)]
    for i, image in enumerate(images):
        await pool.recognize([(f"k{i}", image)])
    assert pool.stats()["cache_entries"] == 2
    await pool.recognize([("k0", images[0])])  # evicted -> read again
    assert fake.calls == 4
    fake2, uncached = make_pool(cache_size=0)
    await uncached.recognize([("a", images[0])])
    await uncached.recognize([("a", images[0])])
    assert fake2.calls == 2


def test_raw_pixel_payload_round_trips_exactly(monkeypatch):
    fake = FakeOcrEngine()
    rgb, gray = crop("rgb"), crop("gray", mode="L")
    fake.register(rgb, "RGB")
    fake.register(gray, "GRAY")
    monkeypatch.setattr(pool_mod, "_ENGINE", fake)
    out = pool_mod._recognize_batch([("a", rgb.mode, rgb.size, rgb.tobytes()),
                                     ("b", gray.mode, gray.size, gray.tobytes())])
    assert out["a"][0] == "RGB" and out["b"][0] == "GRAY"


def fake_sys(tmp_path, values: dict[int, int], metric="cpu_capacity"):
    for index, value in values.items():
        path = tmp_path / f"cpu{index}" / metric
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n")
    return tmp_path


def test_rk3588_big_cores_are_selected(tmp_path):
    root = fake_sys(tmp_path, {0: 414, 1: 414, 2: 414, 3: 414, 4: 1024, 5: 1024, 6: 1024, 7: 1024})
    assert cpu.performance_cpus(root, list(range(8))) == [4, 5, 6, 7]
    assert cpu.resolve_affinity("auto", root, list(range(8))) == [4, 5, 6, 7]
    # Only the CPUs this process may use are considered.
    assert cpu.performance_cpus(root, [0, 1, 6]) == [6]


def test_uniform_or_unknown_topology_is_not_pinned(tmp_path):
    root = fake_sys(tmp_path, {0: 1024, 1: 1024})
    assert cpu.performance_cpus(root, [0, 1]) == []
    assert cpu.performance_cpus(tmp_path / "missing", [0, 1]) == []


def test_max_frequency_fallback(tmp_path):
    root = fake_sys(tmp_path, {0: 1800000, 1: 1800000, 2: 2400000, 3: 2400000}, "cpufreq/cpuinfo_max_freq")
    assert cpu.performance_cpus(root, [0, 1, 2, 3]) == [2, 3]


def test_explicit_affinity_lists():
    assert cpu.parse_cpu_list("0,2,4-6") == [0, 2, 4, 5, 6]
    assert cpu.resolve_affinity("4-7", allowed=[0, 1, 4, 5]) == [4, 5]
    assert cpu.resolve_affinity("none", allowed=[0, 1]) == []
    assert cpu.resolve_affinity("9", allowed=[0, 1]) == []


def test_pool_uses_one_thread_per_pinned_cpu():
    assert OcrPool("fake", cpus=[4, 5, 6, 7]).threads == 4
    assert OcrPool("fake", cpus=[4, 5], threads=1).threads == 1
    assert OcrPool("fake").threads == 0
