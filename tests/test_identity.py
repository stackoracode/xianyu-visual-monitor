from xianyu_monitor.detection.identity import CustomerDirectory, DirectoryEntry, match_row
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import RowObservation

RT = RuntimeSettings()
AV_A = "ffffffff00000000"
AV_A2 = "ffffffff00000001"
AV_B = "0f0f0f0f0f0f0f0f"
AV_DEFAULT = "aaaaaaaaaaaaaaaa"


def row(name, avatar=None, conf=0.98):
    return RowObservation(index=0, present=True, name=name, name_conf=conf, avatar_hash=avatar)


def directory():
    return CustomerDirectory([
        DirectoryEntry("c1", "买家_10012345", "baseline", {"买家_10012345"}, {AV_A}),
        DirectoryEntry("c2", "小明", "known", {"小明"}, {AV_B}),
        DirectoryEntry("c3", "小明", "known", {"小明"}, {AV_A}),
        DirectoryEntry("sys", "闲小蜜", "system", {"闲小蜜"}, set()),
        DirectoryEntry("d1", "默认1", "baseline", {"默认1"}, {AV_DEFAULT}),
        DirectoryEntry("d2", "默认2", "baseline", {"默认2"}, {AV_DEFAULT}),
    ])


def test_existing_customer_matches():
    out = match_row(row("买家_10012345", AV_A2), directory(), RT)
    assert out.kind == "matched" and out.customer_id == "c1"


def test_new_customer_is_new():
    out = match_row(row("全新买家", AV_B[::-1]), directory(), RT)
    assert out.kind == "new"


def test_new_customer_with_platform_default_avatar_is_still_new():
    out = match_row(row("另一个新买家", AV_DEFAULT), directory(), RT)
    assert out.kind == "new"


def test_duplicate_nickname_resolved_only_by_avatar():
    assert match_row(row("小明", AV_B), directory(), RT).customer_id == "c2"
    out = match_row(row("小明", None), directory(), RT)
    assert out.kind == "uncertain" and "duplicate_nickname" in out.reasons
    assert set(out.candidates) == {"c2", "c3"}


def test_same_name_with_conflicting_avatar_is_uncertain():
    out = match_row(row("买家_10012345", "0000000000000000"), directory(), RT)
    assert out.kind == "uncertain" and "avatar_conflicts_with_known_name" in out.reasons


def test_similar_name_is_uncertain_not_new():
    out = match_row(row("买家_1OO12345"), directory(), RT)
    assert out.kind == "uncertain" and "similar_name_possible_ocr_error_or_rename" in out.reasons


def test_possible_rename_by_distinctive_avatar():
    d = directory()
    out = match_row(row("完全不同的名字", AV_B), d, RT)
    # AV_B belongs only to c2 -> possible rename, needs review
    assert out.kind == "uncertain" and out.candidates == ["c2"]


def test_low_ocr_confidence_is_uncertain():
    out = match_row(row("买家_10012345", AV_A, conf=0.4), directory(), RT)
    assert out.kind == "uncertain" and out.reasons == ["low_ocr_confidence"]


def test_system_and_ignored():
    assert match_row(row("闲小蜜"), directory(), RT).kind == "system"
    rt = RuntimeSettings({"ignored_customer_names": ["spam-bot"]})
    assert match_row(row("spam-bot"), directory(), rt).kind == "ignored"
