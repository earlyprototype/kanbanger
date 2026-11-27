"""Identify draft ownership before planning any GitHub mutations."""

from dataclasses import dataclass, field
import re

from kanban_io import BOARD_KEY_PATTERN


class OwnershipConflict(ValueError):
    """The available evidence cannot safely identify a draft."""


_PREFIX = "<!-- kanbanger:item:"
_MARKER = re.compile(rf"<!-- kanbanger:item:v1 ({BOARD_KEY_PATTERN}) ([0-9a-f]+) -->")


def item_marker(board_key: str, title: str) -> str:
    # Preserve the original local key even if the draft is renamed or removed
    # from the board. Hex also keeps arbitrary Markdown out of the comment.
    return f"{_PREFIX}v1 {board_key} {title.encode('utf-8').hex()} -->"


def _read_marker(body: str):
    if _PREFIX not in body:
        return None
    footer = body.rstrip().rsplit("\n", 1)[-1]
    match = _MARKER.fullmatch(footer)
    if match and body.count(_PREFIX) == 1:
        board_key, encoded = match.groups()
        try:
            title = bytes.fromhex(encoded).decode("utf-8")
        except ValueError:
            pass
        else:
            if title.strip() and "\n" not in title and "\r" not in title:
                return board_key, title
    raise OwnershipConflict("invalid or multiple ownership markers")


@dataclass
class Inventory:
    tasks: dict = field(default_factory=dict)
    legacy: dict = field(default_factory=dict)
    unmarked: dict = field(default_factory=dict)


def inspect_drafts(items, board_key, stored, local, *, recover):
    """Check all active drafts, including records outside the local board."""
    result = Inventory()
    by_id = {task["item_id"]: title for title, task in stored.items()}
    seen = set()
    conflicts = []
    for item in sorted(items, key=lambda row: row["id"]):
        item_id = item["id"]
        if item["archived"]:
            # A retry after a successful archive can drop the stale record.
            if item_id in by_id and by_id[item_id] not in local:
                seen.add(item_id)
            continue
        stored_title = by_id.get(item_id)
        try:
            marker = _read_marker(item["body"])
        except OwnershipConflict as exc:
            conflicts.append(f"{item_id}: {exc}")
            continue
        if marker:
            owner, title = marker
            if board_key is None:
                conflicts.append(f"{item_id}: restore the board-id marker before syncing")
                continue
            if owner != board_key:
                if stored_title is not None:
                    conflicts.append(f"{item_id}: belongs to another board")
                continue
            if stored_title is not None and title != stored_title:
                conflicts.append(f"{item_id}: marker and sidecar name different tasks")
                continue
        elif stored_title is not None:
            title = stored_title
            if stored[title].get("marked") or item["title"] != title:
                conflicts.append(f"{item_id}: ownership marker missing or legacy title changed")
                continue
            result.legacy[title] = item
        else:
            result.unmarked[item_id] = item["title"]
            continue

        existing = result.tasks.get(title) or stored.get(title)
        if existing and existing["item_id"] != item_id:
            ids = sorted((existing["item_id"], item_id))
            conflicts.append(f"{title!r}: conflicting item IDs {', '.join(ids)}")
            continue
        seen.add(item_id)
        record = dict(stored.get(title, {}), item_id=item_id)
        if recover or title not in stored:
            record["status"] = item["status"]
        if marker:
            record["marked"] = True
        result.tasks[title] = record

    for item_id in sorted(by_id.keys() - seen):
        conflicts.append(f"{item_id}: cannot verify the recorded draft for {by_id[item_id]!r}")
    for item_id, title in result.unmarked.items():
        if title in local and title not in result.tasks:
            conflicts.append(f"{item_id}: unmarked draft matches {title!r}. Restore the "
                             "original sidecar or resolve the title collision on GitHub")
    if conflicts:
        raise OwnershipConflict("\n".join(sorted(set(conflicts))))
    return result
