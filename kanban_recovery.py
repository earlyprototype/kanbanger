"""Identify draft ownership before planning any GitHub mutations."""

from dataclasses import dataclass, field
import re

from kanban_io import BOARD_KEY_PATTERN


class OwnershipConflict(ValueError):
    """The available evidence cannot safely identify a draft."""


_PREFIX = "<!-- kanbanger:item:"
# A marker line or an embedded, board-specific claim deserves inspection.
# Quoting the comment prefix in prose does not claim a Project item.
_CLAIM = re.compile(
    rf"^[ \t]*{_PREFIX}(?:v\d+\b|[^\r\n]*-->[ \t]*\r?$)"
    rf"|{_PREFIX}[\w-]+[ \t]+{BOARD_KEY_PATTERN}(?=\s|-->|$)",
    re.MULTILINE,
)
_MARKER = re.compile(
    rf"<!-- kanbanger:item:(v1|v2) ({BOARD_KEY_PATTERN}) ([0-9a-f]+)"
    r"(?: ([A-Za-z0-9_+/=-]+))? -->"
)


def item_marker(board_key: str, title: str, item_id: str | None = None) -> str:
    # Preserve the original local key even if the draft is renamed or removed
    # from the board. Hex also keeps arbitrary Markdown out of the comment.
    key = f"{board_key} {title.encode('utf-8').hex()}"
    if item_id is None:
        return f"{_PREFIX}v1 {key} -->"
    return f"{_PREFIX}v2 {key} {item_id} -->"


def _read_marker(body: str):
    if _PREFIX not in body:
        return None
    claims = _CLAIM.findall(body)
    if not claims:
        return None
    footer = body.rstrip().rsplit("\n", 1)[-1]
    match = _MARKER.fullmatch(footer)
    if match and len(claims) == 1:
        version, board_key, encoded, item_id = match.groups()
        try:
            title = bytes.fromhex(encoded).decode("utf-8")
        except ValueError:
            pass
        else:
            if ((version == "v2") == (item_id is not None)
                    and title.strip() and "\n" not in title and "\r" not in title):
                return board_key, title, item_id
    raise OwnershipConflict("invalid or multiple ownership markers")


def read_item_marker(body, provenance):
    """Reconcile legacy body comments with the dedicated Project text field."""
    comment = _read_marker(body)
    if provenance is None:
        return comment
    bound = _read_marker(provenance)
    if not bound or bound[2] is None or provenance != item_marker(*bound):
        raise OwnershipConflict("invalid Project provenance field")
    owner, title, _ = bound
    if comment and comment not in (bound, (owner, title, None)):
        raise OwnershipConflict("Project provenance and draft comment disagree")
    return bound


@dataclass
class Inventory:
    tasks: dict = field(default_factory=dict)
    archived: dict = field(default_factory=dict)
    legacy: dict = field(default_factory=dict)
    pending_legacy: dict = field(default_factory=dict)
    legacy_evidence: dict = field(default_factory=dict)
    unmarked: dict = field(default_factory=dict)


def inspect_drafts(items, board_key, stored, local, *, recover, sidecar_bound):
    """Check active and archived items before planning changes to the Project."""
    result = Inventory()
    by_id = {task["item_id"]: title for title, task in stored.items()}
    seen = set()
    conflicts = []
    for item in sorted(items, key=lambda row: row["id"]):
        item_id = item["id"]
        stored_title = by_id.get(item_id)
        kind = item.get("type", "DraftIssue")
        converted = {"Issue": "repository issue", "PullRequest": "pull request"}.get(kind)
        try:
            marker = read_item_marker(item["body"], item.get("provenance"))
        except OwnershipConflict as exc:
            conflicts.append(f"{item_id}: {exc}")
            continue
        bound_id = None
        if marker:
            owner, title, bound_id = marker
            if bound_id is not None and bound_id != item_id:
                conflicts.append(f"marker for {bound_id} found on {item_id} "
                                 f"({converted or 'draft'} {item['title']!r}). Verify the original item.")
                continue
            if board_key is None:
                conflicts.append(f"{item_id}: restore the board-id marker before syncing")
                continue
            if owner != board_key:
                if stored_title is not None:
                    conflicts.append(f"{item_id}: belongs to another board")
                elif bound_id is None:
                    conflicts.append(f"{item_id}: unbound marker for another board. "
                                     "Verify the original item ID with that board's sidecar.")
                continue
            if converted:
                conflicts.append(f"{item_id}: {converted} {item['title']!r} carries the marker for {title!r}. "
                                 "Resolve the converted draft before syncing")
                continue
            if stored_title is not None and title != stored_title:
                conflicts.append(f"{item_id}: marker and sidecar name different tasks")
                continue
            if bound_id is None and stored_title is None:
                conflicts.append(f"{item_id}: unbound marker for {title!r}. Restore the "
                                 "sidecar with the confirmed item ID or resolve it on GitHub")
                continue
        elif stored_title is not None:
            if converted:
                conflicts.append(f"{item_id}: recorded draft is now a {converted}")
                continue
            title = stored_title
            if stored[title].get("marked") or item["title"] != title:
                conflicts.append(f"{item_id}: ownership marker missing or legacy title changed")
                continue
        else:
            result.unmarked[item_id] = {"type": kind, "title": item["title"]}
            continue

        if bound_id is None and not sidecar_bound:
            result.pending_legacy[item_id] = title
            result.legacy_evidence[item_id] = {"type": kind, "title": item["title"]}
        if item["archived"]:
            result.archived[item_id] = title
            seen.add(item_id)
            continue
        existing = result.tasks.get(title) or stored.get(title)
        if existing and existing["item_id"] != item_id:
            ids = sorted((existing["item_id"], item_id))
            conflicts.append(f"{title!r}: conflicting item IDs {', '.join(ids)}")
            continue
        seen.add(item_id)
        if bound_id is None:
            result.legacy[title] = item
        record = dict(stored.get(title, {}), item_id=item_id)
        if recover or record.get("recovered") or title not in stored:
            record["status"] = item["status"]
        if marker:
            record["marked"] = True
        if recover and (title not in stored or title not in local
                        or local[title] != item["status"]
                        or stored[title]["status"] != item["status"]):
            # Status and archive writes on recovered items require an operator.
            record["recovered"] = True
        result.tasks[title] = record

    for item_id in sorted(by_id.keys() - seen):
        conflicts.append(f"{item_id}: cannot verify the recorded draft for {by_id[item_id]!r}")
    for item_id, title in result.archived.items():
        if title in local and title not in result.tasks:
            conflicts.append(f"{item_id}: archived draft for {title!r} is still on the board. "
                             "Restore the draft on GitHub or remove the local task before syncing")
    for item_id, evidence in result.unmarked.items():
        title = evidence["title"]
        if title in local and title not in result.tasks:
            conflicts.append(f"{item_id}: unmarked Project item matches {title!r}. Restore the "
                             "original sidecar or resolve the title collision on GitHub")
    if conflicts:
        raise OwnershipConflict("\n".join(sorted(set(conflicts))))
    return result
