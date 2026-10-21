"""Calibration profile: screen regions for one desktop layout.

Coordinates are pixels in the captured frame, ``[left, top, right, bottom]`` with the origin
at the upper-left corner. Row field boxes are relative to each row's origin
``(row.left, row.first_top + index * row.height)``. Profiles are validated strictly: a frame
whose size differs from ``frame_width``/``frame_height`` is reported as a layout mismatch
rather than being rescaled silently.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

Box = tuple[int, int, int, int]
ROW_FIELDS = ("avatar", "badge", "name", "preview", "time")


def _check_box(name: str, box: Box, width: int, height: int) -> None:
    left, top, right, bottom = box
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError(f"region {name} {list(box)} is outside the {width}x{height} frame or empty")


class Anchor(BaseModel):
    """A region expected to look the same whenever the chat page is in its normal layout."""

    name: str
    region: Box
    dhash: str = ""
    max_distance: int = 12
    # Mean gray level of the region in the reference frame; catches flat covering windows
    # whose difference hash happens to be close to that of sparse text.
    mean: float | None = None
    max_mean_delta: float = 25.0


class RowLayout(BaseModel):
    left: int
    right: int
    first_top: int
    height: int = Field(gt=4)
    count: int = Field(ge=1, le=40)
    fields: dict[str, Box]

    @model_validator(mode="after")
    def _fields_present(self) -> RowLayout:
        missing = [name for name in ROW_FIELDS if name not in self.fields]
        if missing:
            raise ValueError(f"row.fields missing: {', '.join(missing)}")
        return self


class Colors(BaseModel):
    badge_red_min: int = 200
    badge_green_max: int = 120
    badge_blue_max: int = 120
    badge_min_ratio: float = 0.18
    outgoing_bubble: tuple[int, int, int] = (255, 230, 15)
    outgoing_tolerance: int = 40
    incoming_bubble: tuple[int, int, int] = (242, 242, 242)
    incoming_tolerance: int = 6
    pane_background: tuple[int, int, int] = (255, 255, 255)
    selected_row: tuple[int, int, int] = (235, 235, 235)
    selected_tolerance: int = 6
    blank_stddev_max: float = 2.0


class Profile(BaseModel):
    name: str = "default"
    frame_width: int
    frame_height: int
    conversation_list: Box
    overall_unread_badge: Box | None = None
    row: RowLayout
    conversation_title: Box | None = None
    message_pane: Box | None = None
    composer: Box | None = None
    composer_click: tuple[int, int] | None = None
    composer_placeholder: str = ""
    challenge_probe: Box | None = None
    anchors: list[Anchor] = Field(default_factory=list)
    colors: Colors = Field(default_factory=Colors)
    min_bubble_height: int = 18
    min_bubble_width: int = 24
    # Legacy profile field retained for compatibility; system rows are never clicked.
    park_system_conversation: bool = False

    @model_validator(mode="after")
    def _geometry(self) -> Profile:
        width, height = self.frame_width, self.frame_height
        _check_box("conversation_list", self.conversation_list, width, height)
        for name in ("overall_unread_badge", "conversation_title", "message_pane", "composer", "challenge_probe"):
            box = getattr(self, name)
            if box is not None:
                _check_box(name, box, width, height)
        for anchor in self.anchors:
            _check_box(f"anchor:{anchor.name}", anchor.region, width, height)
        for index in range(self.row.count):
            row_box = self.row_box(index)
            _check_box(f"row[{index}]", row_box, width, height)
            for field_name in ROW_FIELDS:
                _check_box(f"row[{index}].{field_name}", self.field_box(index, field_name), width, height)
        return self

    def row_box(self, index: int) -> Box:
        top = self.row.first_top + index * self.row.height
        return (self.row.left, top, self.row.right, top + self.row.height)

    def field_box(self, index: int, field_name: str) -> Box:
        left, top, _, _ = self.row_box(index)
        fl, ft, fr, fb = self.row.fields[field_name]
        return (left + fl, top + ft, left + fr, top + fb)

    def row_center(self, index: int) -> tuple[int, int]:
        left, top, right, bottom = self.row_box(index)
        # Click on the name/preview text area rather than the avatar (avatar may open a profile).
        name = self.field_box(index, "name")
        return ((name[0] + name[2]) // 2, (top + bottom) // 2)


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(json.loads(path.read_text(encoding="utf-8")))


def profile_from_dict(data: dict) -> Profile:
    return Profile.model_validate(data)
