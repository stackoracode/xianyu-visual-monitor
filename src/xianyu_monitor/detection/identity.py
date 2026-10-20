"""Match an observed conversation row to an internal customer identity.

Display names are not identities. A row is matched confidently only when the normalized
name is unique (or disambiguated by avatar evidence) and nothing contradicts it. Duplicate
nicknames, similar names (possible OCR error or rename), avatar conflicts and low OCR
confidence all produce ``uncertain`` with explicit reasons; nothing is merged automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from xianyu_monitor.detection.textnorm import normalize_name, similarity
from xianyu_monitor.runtime_settings import RuntimeSettings
from xianyu_monitor.vision.analyzer import RowObservation
from xianyu_monitor.vision.imageops import hamming


@dataclass
class DirectoryEntry:
    customer_id: str
    display_name: str
    status: str
    aliases: set[str] = field(default_factory=set)
    avatars: set[str] = field(default_factory=set)


@dataclass
class MatchOutcome:
    kind: str  # matched | new | uncertain | system | ignored
    customer_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    name_norm: str = ""


class CustomerDirectory:
    """In-memory index of customers, aliases and avatar hashes (loaded from MySQL)."""

    def __init__(self, entries: list[DirectoryEntry] | None = None):
        self.entries: dict[str, DirectoryEntry] = {}
        self._by_alias: dict[str, set[str]] = {}
        for entry in entries or []:
            self.add(entry)

    def add(self, entry: DirectoryEntry) -> None:
        self.entries[entry.customer_id] = entry
        for alias in entry.aliases:
            self._by_alias.setdefault(alias, set()).add(entry.customer_id)

    def add_alias(self, customer_id: str, alias_norm: str) -> None:
        entry = self.entries[customer_id]
        entry.aliases.add(alias_norm)
        self._by_alias.setdefault(alias_norm, set()).add(customer_id)

    def add_avatar(self, customer_id: str, avatar_hash: str) -> None:
        self.entries[customer_id].avatars.add(avatar_hash)

    def by_alias(self, alias_norm: str) -> list[DirectoryEntry]:
        return [self.entries[cid] for cid in sorted(self._by_alias.get(alias_norm, ()))
                if self.entries[cid].status != "merged"]

    def similar_aliases(self, alias_norm: str, threshold: float) -> list[DirectoryEntry]:
        found: dict[str, DirectoryEntry] = {}
        for alias, ids in self._by_alias.items():
            if alias == alias_norm:
                continue
            if similarity(alias, alias_norm) >= threshold:
                for cid in ids:
                    if self.entries[cid].status != "merged":
                        found[cid] = self.entries[cid]
        return list(found.values())

    def avatar_distance(self, entry: DirectoryEntry, avatar_hash: str | None) -> int | None:
        if not avatar_hash or not entry.avatars:
            return None
        return min(hamming(avatar_hash, known) for known in entry.avatars)

    def by_avatar(self, avatar_hash: str | None, max_distance: int) -> list[DirectoryEntry]:
        if not avatar_hash:
            return []
        hits = []
        for entry in self.entries.values():
            if entry.status == "merged":
                continue
            distance = self.avatar_distance(entry, avatar_hash)
            if distance is not None and distance <= max_distance:
                hits.append(entry)
        return hits


def match_row(row: RowObservation, directory: CustomerDirectory, rt: RuntimeSettings) -> MatchOutcome:
    name_norm = normalize_name(row.name)
    system_names = {normalize_name(n) for n in rt.system_conversation_names}
    ignored_names = {normalize_name(n) for n in rt.ignored_customer_names}
    if not name_norm or row.name_conf < float(rt.ocr_min_confidence):
        return MatchOutcome("uncertain", reasons=["low_ocr_confidence"], name_norm=name_norm)
    if name_norm in system_names:
        return MatchOutcome("system", reasons=["system_conversation"], name_norm=name_norm)

    exact = directory.by_alias(name_norm)
    match_distance = int(rt.avatar_match_distance)
    conflict_distance = int(rt.avatar_conflict_distance)
    avatar_hash = row.avatar_hash
    if avatar_hash and len(directory.by_avatar(avatar_hash, match_distance)) >= 2:
        # Shared by several customers: a platform default picture or a loading placeholder.
        avatar_hash = None

    if len(exact) == 1:
        entry = exact[0]
        distance = directory.avatar_distance(entry, avatar_hash)
        if distance is not None and distance > conflict_distance:
            return MatchOutcome("uncertain", entry.customer_id, ["avatar_conflicts_with_known_name"],
                                [entry.customer_id], name_norm)
        if entry.status == "ignored" or name_norm in ignored_names:
            return MatchOutcome("ignored", entry.customer_id, ["ignored_customer"], name_norm=name_norm)
        if entry.status == "system":
            return MatchOutcome("system", entry.customer_id, ["system_conversation"], name_norm=name_norm)
        return MatchOutcome("matched", entry.customer_id, ["unique_name"], name_norm=name_norm)

    if len(exact) > 1:
        narrowed = [e for e in exact if (d := directory.avatar_distance(e, avatar_hash)) is not None
                    and d <= match_distance]
        if len(narrowed) == 1:
            entry = narrowed[0]
            if entry.status in {"ignored", "system"}:
                return MatchOutcome(entry.status, entry.customer_id, ["duplicate_name_avatar_resolved"], name_norm=name_norm)
            return MatchOutcome("matched", entry.customer_id, ["duplicate_name_avatar_resolved"], name_norm=name_norm)
        return MatchOutcome("uncertain", None, ["duplicate_nickname"], [e.customer_id for e in exact], name_norm)

    if name_norm in ignored_names:
        return MatchOutcome("ignored", reasons=["ignored_name"], name_norm=name_norm)

    similar = directory.similar_aliases(name_norm, float(rt.name_fuzzy_similarity))
    if similar:
        return MatchOutcome("uncertain", None, ["similar_name_possible_ocr_error_or_rename"],
                            [e.customer_id for e in similar], name_norm)
    avatar_hits = directory.by_avatar(avatar_hash, match_distance)
    # An avatar shared by several known customers (e.g. the platform default picture) is not
    # distinctive evidence of a rename.
    if len(avatar_hits) == 1:
        return MatchOutcome("uncertain", None, ["possible_rename_avatar_match"],
                            [e.customer_id for e in avatar_hits], name_norm)
    return MatchOutcome("new", None, ["name_absent_from_directory"], name_norm=name_norm)
