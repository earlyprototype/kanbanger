"""
kanban-project-sync

Sync a markdown kanban board to GitHub Projects.
"""
import re
import os
import sys
import json
import hashlib
import tempfile
import argparse
from contextlib import nullcontext
from uuid import uuid4
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kanban_io import (
    MalformedSyncState,
    RECOVERY_SUFFIX,
    SCHEMA_VERSION,
    StateClaimConflict,
    atomic_write_json,
    extract_board_key,
    parse_sync_state,
    read_sync_state_bytes,
    kanban_lock,
    parse_task_title_with_description,
)

from kanban_recovery import (
    OwnershipConflict, inspect_drafts, item_marker, read_item_marker,
)

# Load environment variables from .env file if it exists.
# override=True: the project's `.env` is the authoritative target for
# this sync run. Without it, a shell-level export (e.g. a stale
# GITHUB_REPO=owner/other-project from another workspace's profile
# script) silently shadows the `.env` value and routes the sync to
# the wrong project — items get written into another repo's Project
# instead of the intended one, with no error surfaced.
# find_dotenv(usecwd=True): the default `find_dotenv()` walks upward
# from the *caller module's file location* (i.e. this file's
# directory), not from the user's CWD. When kanban-sync is invoked
# from a target project, that lookup can latch onto a rogue
# parent-of-source-directory `.env` (e.g. ~/Desktop/AI/.env) and the
# target project's `.env` is never considered. usecwd=True makes the
# search start at os.getcwd() so the CWD-closest `.env` wins.
def _load_cli_dotenv() -> None:
    """Load the CLI cwd's `.env`, where project values override the shell."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    load_dotenv(find_dotenv(usecwd=True), override=True)


# GitHub GraphQL endpoint
GITHUB_API = "https://api.github.com/graphql"
GITHUB_TIMEOUT_SEC = 30
PROVENANCE_FIELD = "Kanbanger provenance"

# E1: typed exceptions raised by the sync_kanban library so callers
# (CLI entry-point, MCP subprocess wrapper, future direct importers)
# can distinguish failure categories instead of pattern-matching on
# stdout. Library code raises; CLI catches at __main__.
class KanbangerError(Exception):
    """Base class for all kanbanger library errors."""


class GitHubAPIError(KanbangerError):
    """A GitHub GraphQL/HTTP call failed: HTTP non-200, GraphQL
    error array, or a missing field that the GraphQL contract
    promised."""


class ProjectNotFoundError(KanbangerError):
    """No GitHub Project matched the requested repo/number."""


class ConfigurationError(KanbangerError):
    """Required configuration is missing or invalid: env var, file
    path, or runtime dependency."""


class CorruptStateError(ConfigurationError):
    """State exists but its contents cannot be used."""

    def __init__(self, path, cause):
        super().__init__(f"Sync state is corrupt: {path} ({cause}). "
                         "Restore a backup or inspect --recover --dry-run.")
        self.bindings = cause.bindings if isinstance(cause, MalformedSyncState) else {}
        self.claims = cause.claims if isinstance(cause, MalformedSyncState) else {}


class BoardReadError(ConfigurationError):
    """The board cannot be read."""


def build_sync_plan(
    local_flat: dict[str, str], state_tasks: dict[str, dict]
) -> dict:
    """Return the local-source-of-truth changes required by a sync."""
    operations = []
    summary = {"create": 0, "update": 0, "archive": 0}

    for title, to_status in local_flat.items():
        stored = state_tasks.get(title)
        if stored is None:
            operations.append({
                "action": "CREATE",
                "title": title,
                "from_status": None,
                "to_status": to_status,
                "item_id": None,
            })
            summary["create"] += 1
        elif stored.get("status") != to_status:
            action = "PENDING_UPDATE" if stored.get("recovered") else "UPDATE"
            operations.append({
                "action": action,
                "title": title,
                "from_status": stored.get("status"),
                "to_status": to_status,
                "item_id": stored.get("item_id"),
            })
            key = action.lower()
            summary[key] = summary.get(key, 0) + 1

    for title, stored in state_tasks.items():
        if title not in local_flat:
            action = "PENDING_ARCHIVE" if stored.get("recovered") else "ARCHIVE"
            operations.append({
                "action": action,
                "title": title,
                "from_status": stored.get("status"),
                "to_status": None,
                "item_id": stored.get("item_id"),
            })
            key = action.lower()
            summary[key] = summary.get(key, 0) + 1

    return {"operations": operations, "summary": summary}


def _flatten_local_tasks(tasks: Dict[str, List[Dict]]) -> dict[str, str]:
    local = {}
    for column, column_tasks in tasks.items():
        for task in column_tasks:
            title = task["title"]
            if title in local:
                raise ConfigurationError(f"Ownership conflict: {title!r} appears in multiple columns")
            local[title] = column
    return local


class LocalBoard:
    """Handles parsing of markdown kanban files."""
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.tasks: Dict[str, List[Dict]] = {}

    def read_source(self) -> str:
        try:
            with open(self.file_path, encoding="utf-8", newline="") as source:
                return source.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise BoardReadError(
                f"Unable to read kanban board: {self.file_path} ({exc})"
            ) from exc

    def verify_source(self):
        if self.read_source() != self.source:
            raise ConfigurationError("Board changed during sync. Inspect the board and retry.")
    
    def parse(self) -> Dict[str, List[Dict]]:
        """Parse a markdown kanban file and return tasks by column.

        D4: same-title rows in the same section are deduped (first
        occurrence wins) with a stderr warning. Without dedup the
        sync path would create duplicate GitHub items for what looks
        like one logical task to the user.

        D8: dedup uses the stripped title (text before any ` - desc`
        separator) computed via the shared
        `kanban_io.parse_task_title_with_description` helper — same
        semantics as the MCP-tools side. Without the shared helper the
        two parsers drifted and `* [ ] X` + `* [ ] X - extra` pushed
        as two separate GH items even though MCP-tools dedup'd them.
        The pushed `title` field still carries the full post-checkbox
        text so the description survives to GitHub on the first
        occurrence; only the dedup key is the stripped form.
        """
        self.source = self.read_source()
        content = self.source.replace("\r\n", "\n").replace("\r", "\n")

        self.board_key = extract_board_key(content)
        key_lines = re.findall(r"^[ \t]*<!--[ \t]*kanbanger:board-id:", content, re.MULTILINE)
        if len(key_lines) != int(self.board_key is not None):
            raise ConfigurationError("Restore the board's original board-id marker")

        tasks = {}
        seen_per_section: Dict[str, set] = {}
        current_section = None

        # Detect section headers (## N. TITLE or ## TITLE)
        section_pattern = re.compile(r'^##\s+(?:\d+\.\s+)?(.+)', re.IGNORECASE)
        task_pattern = re.compile(r'^\*\s+\[([ xX])\]\s+(.+)')

        for line in content.split('\n'):
            # Check for section header
            section_match = section_pattern.match(line.strip())
            if section_match:
                section_name = section_match.group(1).strip()
                # Normalize common section names
                normalized = section_name.upper()
                if 'BACKLOG' in normalized:
                    current_section = 'Backlog'
                elif 'TO DO' in normalized or 'TODO' in normalized:
                    current_section = 'Todo'
                elif 'DOING' in normalized or 'IN PROGRESS' in normalized:
                    current_section = 'InProgress'
                elif 'REVIEW' in normalized:
                    current_section = 'Review'
                elif 'DONE' in normalized or 'COMPLETE' in normalized:
                    current_section = 'Done'
                else:
                    current_section = section_name

                tasks.setdefault(current_section, [])
                seen_per_section.setdefault(current_section, set())
                continue

            # Check for task item
            if current_section:
                task_match = task_pattern.match(line.strip())
                if task_match:
                    is_done = task_match.group(1).lower() == 'x'
                    title = task_match.group(2).strip()
                    parsed = parse_task_title_with_description(line)
                    dedup_key = parsed[0] if parsed is not None else title
                    if dedup_key in seen_per_section[current_section]:
                        print(
                            f"Warning: duplicate task title in section "
                            f"'{current_section}': '{dedup_key}'. Keeping "
                            f"first occurrence; dropping subsequent "
                            f"duplicate to prevent duplicate GitHub items "
                            f"on sync.",
                            file=sys.stderr,
                        )
                        continue
                    seen_per_section[current_section].add(dedup_key)
                    tasks[current_section].append({
                        'title': title,
                        'done': is_done
                    })

        self.tasks = tasks
        return tasks


class StateManager:
    """Manages the .kanban.json sidecar file for state tracking."""
    
    def __init__(self, kanban_file_path: str):
        self.kanban_file = Path(kanban_file_path)
        self.state_file = self.kanban_file.parent / ".kanban.json"
        self.displaced_file = self.state_file.with_suffix(RECOVERY_SUFFIX)
        self.source: Optional[bytes] = None
        self.state = self.empty_state()

    @staticmethod
    def empty_state():
        return {
            "schema_version": SCHEMA_VERSION,
            "repo_node_id": None,
            "project_id": None,
            # ADR 0002: the minted board key this state belongs to. None
            # until the first sync against a keyed board adopts it.
            "board_key": None,
            "tasks": {}
        }

    def read_source(self) -> Optional[bytes]:
        try:
            return read_sync_state_bytes(self.state_file)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"Unable to read sync state: {self.state_file} ({exc})") from exc

    def verify_source(self):
        if self.read_source() != self.source:
            raise ConfigurationError("Sync state changed during sync. Inspect the sidecar and retry.")

    def load(self, *, read_only: bool = False) -> Dict:
        """Parse a single snapshot so recovery can detect a concurrent restore."""
        self.source = self.read_source()
        try:
            self.state = self.empty_state() if self.source is None else parse_sync_state(self.source)
        except StateClaimConflict as exc:
            raise ConfigurationError(
                f"Ownership conflict: {exc}. Restore a known-good sidecar."
            ) from exc
        except (ValueError, RecursionError) as exc:
            if (isinstance(exc, MalformedSyncState) and type(exc.version) is int
                    and exc.version > SCHEMA_VERSION):
                raise ConfigurationError("Sync state requires a newer version of Kanbanger") from exc
            raise CorruptStateError(self.state_file, exc) from exc
        self.state.setdefault("schema_version", SCHEMA_VERSION)
        if self.state["schema_version"] > SCHEMA_VERSION:
            raise ConfigurationError("Sync state requires a newer version of Kanbanger")
        return self.state

    def save(self):
        with kanban_lock(str(self.kanban_file.parent)):
            self.save_unlocked()

    @staticmethod
    def _install_if_absent(source: Path, target: Path):
        """Publish a complete file, refusing to replace an existing name."""
        if os.name == "nt":
            source.rename(target)  # Windows rename fails if the target exists.
        else:
            os.link(source, target)
            source.unlink()

    def save_unlocked(self, *, corrupt: bool = False) -> Optional[Path]:
        """Replace only the verified snapshot and remember the exact bytes published."""
        try:
            with tempfile.TemporaryDirectory(dir=self.state_file.parent) as staging:
                replacement = Path(staging) / self.state_file.name
                atomic_write_json(str(replacement), self.state)
                published = replacement.read_bytes()
                self.verify_source()
                if published == self.source:
                    return None
                if self.source is not None:
                    self.state_file.rename(self.displaced_file)
                try:
                    if self.source is not None and self.displaced_file.read_bytes() != self.source:
                        raise ConfigurationError("Sync state changed before replacement. "
                                                 "Inspect the sidecar and retry.")
                    self._install_if_absent(replacement, self.state_file)
                except BaseException:
                    if self.source is not None:
                        # If an editor restored another file here, retain both
                        # versions for inspection and block the next sync.
                        self._install_if_absent(self.displaced_file, self.state_file)
                    raise
                backup = None
                if self.source is not None:
                    if corrupt:
                        backup = self.state_file.with_suffix(f".json.corrupt-{uuid4().hex}")
                        self.displaced_file.rename(backup)
                    else:
                        self.displaced_file.unlink()
                self.source = published
                return backup
        except FileExistsError as exc:
            raise ConfigurationError("Sync state changed before replacement. Inspect the sidecar "
                                     "and any .kanban.json.recovering file before retrying.") from exc
        except OSError as exc:
            raise ConfigurationError(f"Could not install sync state: {exc}") from exc

    def save_after(self, action: str, title: str, item_id: str):
        try:
            self.save_unlocked()
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"Sync interrupted after {action} of {title!r} ({item_id}). {exc} "
                "Inspect --recover --dry-run before resuming."
            ) from exc

    def verify_board_key(self, board_key: Optional[str]) -> None:
        """Check the existing binding. An empty sidecar can adopt the board ID."""
        recorded = self.state.get("board_key")
        if recorded is not None and recorded != board_key:
            raise ConfigurationError(
                f"sync state belongs to a different board (copied?): {self.state_file} "
                f"records {recorded}, but {self.kanban_file} carries {board_key}. "
                "Restore the matching board file and its board-id marker."
            )
        if not self.state["tasks"]:
            self.state["board_key"] = board_key

    def get_item_id(self, task_title: str) -> Optional[str]:
        """Get the GitHub item ID for a task title."""
        return self.state["tasks"].get(task_title, {}).get("item_id")
    
    def get_status(self, task_title: str) -> Optional[str]:
        """Get the stored status for a task title."""
        return self.state["tasks"].get(task_title, {}).get("status")
    
    def update_task(self, task_title: str, item_id: str, status: Optional[str]):
        """Update or add a task in the state."""
        self.state["tasks"].setdefault(task_title, {}).update(
            item_id=item_id, status=status, marked=True
        )
    
    def remove_task(self, task_title: str):
        """Remove a task from the state."""
        if task_title in self.state["tasks"]:
            del self.state["tasks"][task_title]
    
    def set_project_info(self, repo_node_id: str, project_id: str):
        """Set the repository and project IDs."""
        self.state["repo_node_id"] = repo_node_id
        self.state["project_id"] = project_id


class GitHubClient:
    """Handles GitHub GraphQL API interactions."""
    
    def __init__(self, token: str):
        self.token = token
        self.status_field_name = "Status"
        self.provenance_field_id = None
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        try:
            import requests
            self.requests = requests
        except ImportError:
            raise ConfigurationError(
                "requests not installed. Run: pip install requests"
            )
    
    def _query(self, query: str, variables: Dict) -> Dict:
        """Execute a GraphQL query."""
        try:
            response = self.requests.post(
                GITHUB_API,
                headers=self.headers,
                json={"query": query, "variables": variables},
                timeout=GITHUB_TIMEOUT_SEC,
            )
        except self.requests.RequestException as exc:
            raise GitHubAPIError(f"GitHub API request failed: {exc}") from exc
        
        if response.status_code != 200:
            raise GitHubAPIError(
                f"GitHub API returned status {response.status_code}: "
                f"{response.text}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GitHubAPIError(f"GitHub API returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GitHubAPIError("GitHub API returned JSON that is not an object")
        if "errors" in data:
            details = "\n".join(
                f"  - {error.get('message', str(error))}"
                for error in data["errors"]
            )
            raise GitHubAPIError(f"GraphQL errors:\n{details}")
        
        return data
    
    def get_linked_projects(self, owner: str, repo_name: str) -> Tuple[str, List[Dict]]:
        """Read every linked Project before selecting a target for sync or doctor."""
        query = """
        query($owner: String!, $repo: String!, $cursor: String) {
            repository(owner: $owner, name: $repo) {
                id
                projectsV2(first: 10, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                        id
                        number
                        title
                        fields(first: 20) {
                            nodes {
                                ... on ProjectV2SingleSelectField {
                                    id
                                    name
                                    options {
                                        id
                                        name
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        projects = []
        cursor = None
        cursors = set()
        while True:
            data = self._query(query, {"owner": owner, "repo": repo_name, "cursor": cursor})
            repo = (data.get("data") or {}).get("repository")
            if not repo:
                raise GitHubAPIError("GitHub API returned no readable repository")
            page = repo["projectsV2"]
            projects.extend(page["nodes"])
            info = page["pageInfo"]
            if info["hasNextPage"] is False:
                break
            cursor = info["endCursor"]
            if info["hasNextPage"] is not True or not cursor or cursor in cursors:
                raise GitHubAPIError("GitHub API returned incomplete linked Projects")
            cursors.add(cursor)
        return repo["id"], projects

    @staticmethod
    def select_linked_project(projects, project_number=None, bound_id=None) -> Dict:
        """Require one linked Project consistent with both selectors."""
        if project_number is not None:
            projects = [p for p in projects if p["number"] == project_number]
            if not projects:
                raise ProjectNotFoundError(f"Project #{project_number} not found")
        if bound_id is not None:
            projects = [p for p in projects if p["id"] == bound_id]
            if not projects:
                raise ConfigurationError(f"Ownership conflict: expected linked Project {bound_id}")
        if len(projects) != 1:
            raise ConfigurationError("Multiple linked Projects match. Select the original Project "
                                     "with --project or GITHUB_PROJECT_NUMBER.")
        return projects[0]

    def get_repo_project(self, owner: str, repo_name: str, project_number: Optional[int] = None,
                         bound_id: Optional[str] = None) -> Tuple[str, str, str, Dict]:
        """Return repository, Project, Status field and Status option IDs for sync."""
        repo_node_id, projects = self.get_linked_projects(owner, repo_name)
        if not projects:
            raise ProjectNotFoundError(f"No projects found linked to {owner}/{repo_name}")
        project = self.select_linked_project(projects, project_number, bound_id)
        if project_number is None:
            print(f"Info: Using project #{project['number']}: {project['title']}", file=sys.stderr)

        project_id = project["id"]

        # Find the Status field
        status_field = None
        for field in project["fields"]["nodes"]:
            if field and field.get("name") in ["Status", "status"]:
                status_field = field
                break

        if not status_field:
            raise GitHubAPIError("No 'Status' field found in project")
        
        self.status_field_name = status_field["name"]
        status_field_id = status_field["id"]
        status_options = {opt["name"]: opt["id"] for opt in status_field["options"]}
        
        return repo_node_id, project_id, status_field_id, status_options
    
    def get_project_items(self, project_id: str) -> List[Dict]:
        """Read every page before making ownership decisions."""
        query = """
        query($projectId: ID!, $cursor: String, $status: String!, $provenance: String!) {
            node(id: $projectId) {
                ... on ProjectV2 {
                    provenanceField: field(name: $provenance) {
                        ... on ProjectV2Field { id dataType }
                    }
                    items(first: 100, after: $cursor, archivedStates: [NOT_ARCHIVED, ARCHIVED]) {
                        pageInfo { hasNextPage endCursor }
                        nodes {
                            id isArchived
                            content {
                                __typename
                                ... on DraftIssue { id title body }
                                ... on Issue { id title body }
                                ... on PullRequest { id title body }
                            }
                            fieldValueByName(name: $status) {
                                ... on ProjectV2ItemFieldSingleSelectValue { name }
                            }
                            provenance: fieldValueByName(name: $provenance) {
                                ... on ProjectV2ItemFieldTextValue { text }
                            }
                        }
                    }
                }
            }
        }
        """
        items = []
        cursor = None
        cursors = set()
        item_ids = set()
        while True:
            data = self._query(query, {"projectId": project_id, "cursor": cursor,
                                       "status": self.status_field_name, "provenance": PROVENANCE_FIELD})
            try:
                project = data["data"]["node"]
                field = project["provenanceField"]
                if field is not None and (not isinstance(field, dict) or field.get("dataType") != "TEXT"
                                          or not isinstance(field.get("id"), str) or not field["id"]):
                    raise ConfigurationError(f"Project field {PROVENANCE_FIELD!r} must be a text field")
                field_id = field["id"] if field else None
                if cursor is not None and field_id != self.provenance_field_id:
                    raise ConfigurationError("Project provenance field changed during inventory. Retry sync.")
                self.provenance_field_id = field_id
                page = project["items"]
                if not isinstance(page["nodes"], list):
                    raise ValueError("missing item list")
                for node in page["nodes"]:
                    item_id = node["id"]
                    if not isinstance(item_id, str) or not item_id or item_id in item_ids:
                        raise ValueError("missing or repeated project item ID")
                    item_ids.add(item_id)
                    content = node["content"]
                    if not content or content["__typename"] not in ("DraftIssue", "Issue", "PullRequest"):
                        raise ValueError("unreadable project item")
                    if not all(isinstance(content[key], str) for key in ("id", "body", "title")):
                        raise ValueError("unreadable draft")
                    status = node["fieldValueByName"]
                    if status is not None and not isinstance(status["name"], str):
                        raise ValueError("invalid status field value")
                    if type(node["isArchived"]) is not bool or not content["id"]:
                        raise ValueError("incomplete project item")
                    kind = content["__typename"]
                    items.append(dict(id=item_id, type=kind,
                                      draft_id=content["id"] if kind == "DraftIssue" else None,
                                      title=content["title"], body=content["body"],
                                      provenance=self._provenance_value(node),
                                      archived=node["isArchived"],
                                      status=None if status is None else status["name"]))
                info = page["pageInfo"]
                if info["hasNextPage"] is False:
                    return items
                cursor = info["endCursor"]
                if info["hasNextPage"] is not True or not cursor or cursor in cursors:
                    raise ValueError("invalid pagination cursor")
                cursors.add(cursor)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubAPIError(f"GitHub API returned an incomplete project inventory: {exc}") from exc

    @staticmethod
    def _provenance_value(node):
        value = node["provenance"]
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise GitHubAPIError("Unreadable Project provenance value")
        return value["text"]

    def _get_active_draft(self, item_id: str) -> Dict:
        """Resolve a Project item to its current active draft content."""
        query = """
        query($id: ID!, $provenance: String!) {
            node(id: $id) {
                ... on ProjectV2Item {
                    id isArchived
                    provenance: fieldValueByName(name: $provenance) {
                        ... on ProjectV2ItemFieldTextValue { text }
                    }
                    content { __typename ... on DraftIssue { id title body } }
                }
            }
        }
        """
        data = self._query(query, {"id": item_id, "provenance": PROVENANCE_FIELD})
        item = (data.get("data") or {}).get("node") or {}
        draft = item.get("content") or {}
        if (item.get("id") != item_id or item.get("isArchived") is not False
                or draft.get("__typename") != "DraftIssue" or not draft.get("id")
                or not isinstance(draft.get("body"), str) or "provenance" not in item):
            raise ConfigurationError(
                f"Ownership conflict: {item_id} is no longer a readable active draft. "
                "Inspect it and retry."
            )
        return dict(draft, provenance=self._provenance_value(item))

    def verify_owned_draft(self, item_id: str, board_key: str, title: str):
        """Recheck ownership immediately before a status or archive mutation."""
        draft = self._get_active_draft(item_id)
        try:
            if read_item_marker(draft["body"], draft["provenance"]) != (board_key, title, item_id):
                raise OwnershipConflict(f"expected bound ownership marker for {title!r}")
        except OwnershipConflict as exc:
            raise ConfigurationError(f"Ownership conflict: {item_id}: {exc}. Inspect it and retry.") from exc

    def mark_draft(self, project_id: str, item_id: str, expected_title: str,
                   expected_body: str, marker: str):
        """Set provenance on the draft returned by this sync's CREATE."""
        draft = self._get_active_draft(item_id)
        if (draft.get("title") != expected_title or draft["body"] != expected_body
                or draft["provenance"] is not None):
            raise ConfigurationError(f"Draft {item_id} changed during sync. Inspect it and retry.")
        if self.provenance_field_id is None:
            create_field = """
            mutation($project: ID!, $name: String!) {
                createProjectV2Field(input: {projectId: $project, name: $name, dataType: TEXT}) {
                    projectV2Field { ... on ProjectV2Field { id } }
                }
            }
            """
            data = self._query(create_field, {"project": project_id, "name": PROVENANCE_FIELD})
            created = (data.get("data") or {}).get("createProjectV2Field") or {}
            self.provenance_field_id = (created.get("projectV2Field") or {}).get("id")
            if not self.provenance_field_id:
                raise GitHubAPIError("GitHub API returned no provenance field ID")
        mutation = """
        mutation($project: ID!, $id: ID!, $field: ID!, $marker: String!) {
            updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $id,
                    fieldId: $field, value: {text: $marker}}) {
                projectV2Item { id }
            }
        }
        """
        data = self._query(mutation, {"project": project_id, "id": item_id,
                                      "field": self.provenance_field_id, "marker": marker})
        updated = (data.get("data") or {}).get("updateProjectV2ItemFieldValue") or {}
        if (updated.get("projectV2Item") or {}).get("id") != item_id:
            raise GitHubAPIError("GitHub API returned no confirmation of the draft marker")

    def create_draft_issue(self, project_id: str, title: str, body: str = "") -> str:
        """Create a draft issue in the project. Returns the item ID."""
        mutation = """
        mutation($projectId: ID!, $title: String!, $body: String) {
            addProjectV2DraftIssue(input: {projectId: $projectId, title: $title, body: $body}) {
                projectItem {
                    id
                }
            }
        }
        """
        
        variables = {
            "projectId": project_id,
            "title": title,
            "body": body
        }
        
        data = self._query(mutation, variables)
        created = (data.get("data") or {}).get("addProjectV2DraftIssue") or {}
        item_id = (created.get("projectItem") or {}).get("id")
        if not isinstance(item_id, str) or not item_id:
            raise GitHubAPIError("GitHub API returned no item ID for the new draft")
        return item_id
    
    def update_item_status(self, project_id: str, item_id: str, status_field_id: str, status_option_id: str):
        """Update the status field of a project item."""
        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
            updateProjectV2ItemFieldValue(input: {
                projectId: $projectId,
                itemId: $itemId,
                fieldId: $fieldId,
                value: {singleSelectOptionId: $optionId}
            }) {
                projectV2Item {
                    id
                }
            }
        }
        """
        
        variables = {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": status_field_id,
            "optionId": status_option_id
        }
        
        self._query(mutation, variables)
    
    def archive_item(self, project_id: str, item_id: str):
        """Archive a project item."""
        mutation = """
        mutation($projectId: ID!, $itemId: ID!) {
            archiveProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
                item {
                    id
                }
            }
        }
        """
        
        variables = {
            "projectId": project_id,
            "itemId": item_id
        }
        
        self._query(mutation, variables)


class Syncer:
    """Orchestrates the synchronization between local kanban and GitHub project."""
    
    def __init__(self, board: LocalBoard, state: StateManager, client: GitHubClient):
        self.board = board
        self.state = state
        self.client = client
    
    def sync(self, repo: str, project_number: Optional[int] = None, *,
             recover: bool = False, preview: bool = False,
             reviewed_item_ids: Optional[List[str]] = None, review_token: Optional[str] = None,
             confirmed_legacy_item_ids: Optional[List[str]] = None):
        """Check ownership under the same workspace lock used for all writes."""
        if preview and not recover:
            raise ConfigurationError("Use --dry-run for a local preview")
        if (reviewed_item_ids or confirmed_legacy_item_ids or review_token) and (not recover or preview):
            raise ConfigurationError("Use review options when applying --recover.")
        if (reviewed_item_ids or confirmed_legacy_item_ids) and not review_token:
            raise ConfigurationError("Supply --review-token from --recover --dry-run with item IDs.")
        lock = nullcontext() if preview else kanban_lock(str(self.state.kanban_file.parent))
        with lock:
            return self._sync(repo, project_number, recover, preview, reviewed_item_ids or [],
                              review_token, set(confirmed_legacy_item_ids or []))

    def _sync(self, repo, project_number, recover, preview, reviewed_item_ids,
              review_token, confirmed_legacy_ids):
        parts = repo.split('/')
        if len(parts) != 2 or not all(parts):
            raise ConfigurationError("GITHUB_REPO must be 'owner/repo'")
        local = _flatten_local_tasks(self.board.parse())
        board_key = self.board.board_key

        state_error = None
        damaged_claims = {}
        try:
            self.state.load()
            state_kind = "missing" if self.state.source is None else "valid"
        except CorruptStateError as exc:
            if not recover:
                raise
            state_kind = "corrupt"
            state_error = str(exc)
            self.state.state = dict(self.state.empty_state(), **exc.bindings)
            damaged_claims = exc.claims
        self.state.verify_board_key(board_key)
        bound_id = self.state.state.get("project_id") if state_kind == "valid" else None
        repo_id, project_id, field_id, options = self.client.get_repo_project(
            *parts, project_number, bound_id
        )
        for key, expected in (("repo_node_id", repo_id), ("project_id", project_id)):
            if self.state.state.get(key) not in (None, expected):
                raise ConfigurationError(f"Ownership conflict: sidecar {key} does not match {expected}")
        self.state.set_project_info(repo_id, project_id)
        inventory = self._inventory(project_id, local, recover)
        self.state.verify_source()
        for item_id, title in sorted(damaged_claims.items()):
            if (inventory.tasks.get(title, {}).get("item_id") != item_id
                    and inventory.archived.get(item_id) != title):
                raise ConfigurationError(f"Ownership conflict: cannot verify corrupt sidecar "
                                         f"claim {item_id} for {title!r}. Restore a backup.")
        missing_records = inventory.tasks.keys() - self.state.state["tasks"].keys()
        if not recover and (missing_records or (state_kind == "missing" and inventory.unmarked)):
            raise ConfigurationError(
                "Sync state needs recovery. Inspect --recover --dry-run, then run --recover."
            )

        unknown_claims = confirmed_legacy_ids - inventory.pending_legacy.keys()
        if unknown_claims:
            raise ConfigurationError(f"IDs absent from legacy claims: {', '.join(sorted(unknown_claims))}")
        pending = {item_id: title for item_id, title in inventory.pending_legacy.items()
                   if item_id not in confirmed_legacy_ids}
        if pending and board_key is not None and not preview:
            raise ConfigurationError(
                f"Legacy sidecar has no board-id binding for items {', '.join(pending)}. "
                "Inspect --recover --dry-run and confirm each claim with --confirm-legacy-item "
                "and --review-token, or restore the matching sidecar."
            )
        # Keep unconfirmed claims out of both the owned inventory and the plan.
        for item_id, title in pending.items():
            local.pop(title, None)
            inventory.tasks.pop(title, None)
            inventory.legacy.pop(title, None)
            inventory.archived.pop(item_id, None)
        # A Project field write cannot require that a legacy item is still a draft.
        # Reserve its task title while an operator binds the draft in its editor.
        if inventory.legacy and not recover:
            ids = ", ".join(sorted(inventory.legacy.values()))
            raise ConfigurationError(
                f"Legacy draft binding pending for {ids}. Inspect pending_legacy_bindings in "
                "--recover --dry-run. Verify each draft and place its listed marker on the "
                "last line of its notes, replacing any provisional marker. Then retry sync."
            )
        plan = build_sync_plan(
            {title: status for title, status in local.items() if title not in inventory.legacy},
            {title: record for title, record in inventory.tasks.items() if title not in inventory.legacy},
        )
        create_titles = sorted(op["title"] for op in plan["operations"] if op["action"] == "CREATE")
        scope = [board_key, project_id, create_titles, inventory.pending_legacy,
                 inventory.unmarked, inventory.legacy_evidence]
        current_token = hashlib.sha256(json.dumps(scope, sort_keys=True).encode("utf-8")).hexdigest()
        if review_token is not None and review_token != current_token:
            raise ConfigurationError("Review token does not match the current preview. "
                                     "Inspect --recover --dry-run again.")
        # An intact backup can predate an unmarked card. Only an operator can
        # rule out a renamed or converted legacy draft before a CREATE.
        unknown_ids = set(reviewed_item_ids) - inventory.unmarked.keys()
        if unknown_ids:
            raise ConfigurationError(
                f"Reviewed IDs absent from the unmarked inventory: {', '.join(sorted(unknown_ids))}. "
                "Inspect --recover --dry-run again."
            )
        reviewed = {item_id: inventory.unmarked[item_id] for item_id in reviewed_item_ids}
        if self.state.state.get("reviewed_create_titles") == create_titles:
            saved = self.state.state.get("reviewed_unmarked_items", {})
            reviewed.update((item_id, item) for item_id, item in inventory.unmarked.items()
                            if saved.get(item_id) == item)
        unreviewed = sorted(inventory.unmarked.keys() - reviewed.keys())
        if not recover and plan["summary"]["create"] and unreviewed:
            raise ConfigurationError(
                f"Review unmarked Project items before CREATE: {', '.join(unreviewed)}. "
                "Inspect --recover --dry-run, then apply --recover with its --review-token "
                "and --reviewed-item ID for each item you have checked."
            )
        # Older acknowledgments contain IDs alone and cannot authorize a CREATE.
        self.state.state.pop("reviewed_unmarked_ids", None)
        if reviewed:
            self.state.state["reviewed_unmarked_items"] = reviewed
            self.state.state["reviewed_create_titles"] = create_titles
        else:
            self.state.state.pop("reviewed_unmarked_items", None)
            self.state.state.pop("reviewed_create_titles", None)
        self.state.state["tasks"] = inventory.tasks
        marker_guidance = (
            "Sync and recovery require a board-id marker to apply changes. "
            "Restore the original marker if this board was previously keyed. "
            "For a never-keyed board, run kanbanger init or setup_project with "
            "the board closed, or add a board-id marker with a fresh UUID in your editor."
        )
        if recover:
            report = {
                "state": state_kind, "board_key": board_key, "project_id": project_id,
                "owned": {title: record["item_id"] for title, record in inventory.tasks.items()},
                "archived": inventory.archived,
                "unmarked": {item_id: item["title"] for item_id, item in inventory.unmarked.items()},
                "unmarked_items": inventory.unmarked,
                "unreviewed_item_ids": unreviewed,
                "pending_legacy_claims": pending,
                "pending_legacy_items": {item_id: inventory.legacy_evidence[item_id] for item_id in pending},
                "review_token": current_token,
                "mark_on_next_sync": {},
                "pending_legacy_bindings": {
                    item_id: item_marker(board_key, title, item_id)
                    for title, item_id in inventory.legacy.items()
                },
                "plan": plan,
            }
            if state_error:
                report["state_error"] = state_error
            if preview:
                if board_key is None:
                    report["board_key_guidance"] = marker_guidance
                return report

        self.board.verify_source()
        if board_key is None:
            raise ConfigurationError(marker_guidance)
        self.state.state["board_key"] = board_key
        self.state.state["schema_version"] = SCHEMA_VERSION
        if recover:
            backup = self.state.save_unlocked(corrupt=state_kind == "corrupt")
            if backup is not None:
                report["backup"] = str(backup)
            return report

        self._apply_plan(plan, project_id, field_id, options, board_key, local, reviewed)

    def _inventory(self, project_id, local, recover=False):
        try:
            return inspect_drafts(
                self.client.get_project_items(project_id), self.board.board_key,
                self.state.state["tasks"], local, recover=recover,
                sidecar_bound=self.state.state.get("board_key") is not None,
            )
        except OwnershipConflict as exc:
            raise ConfigurationError(f"Ownership conflict: {exc}") from exc

    def _verify_create(self, project_id, title, local, reviewed):
        inventory = self._inventory(project_id, local)
        unreviewed = sorted(item_id for item_id, item in inventory.unmarked.items()
                            if reviewed.get(item_id) != item)
        if unreviewed:
            raise ConfigurationError(f"Review unmarked items {', '.join(unreviewed)} before CREATE. "
                                     "Inspect --recover --dry-run again.")
        if title in inventory.tasks or inventory.tasks.keys() - self.state.state["tasks"].keys():
            raise ConfigurationError("Ownership changed before CREATE. Inspect --recover --dry-run again.")
        self.board.verify_source()
        self.state.verify_source()

    def _apply_plan(self, plan, project_id, status_field_id, status_options, board_key, local, reviewed):
        for operation in plan["operations"]:
            action = operation["action"]
            title = operation["title"]
            desired_status = operation["to_status"]
            item_id = operation["item_id"]

            if action == "CREATE":
                # New task - create it
                self._verify_create(project_id, title, local, reviewed)
                print(f"  [CREATE] {title} => {desired_status}")
                provisional = item_marker(board_key, title)
                item_id = self.client.create_draft_issue(project_id, title, provisional)
                # D7+D12: persist item_id with status=None first. The
                # confirmed status is only persisted AFTER the status
                # update mutation succeeds. Prevents the stuck-no-status
                # idempotency bug where a transient failure between
                # create_draft_issue and update_item_status leaves state
                # desynced from GH (state thinks status set; GH has none;
                # next sync sees stored == desired and skips forever).
                self.state.update_task(title, item_id, None)
                # This CREATE consumes the reviewed plan. A retry with a
                # different set of remaining creates needs its own review.
                self.state.state.pop("reviewed_unmarked_items", None)
                self.state.state.pop("reviewed_create_titles", None)
                self.state.save_after(action, title, item_id)
                bound_marker = item_marker(board_key, title, item_id)
                self.client.mark_draft(project_id, item_id, title, provisional, bound_marker)
                self.state.save_after("MARK", title, item_id)

                # Set initial status
                if desired_status in status_options:
                    self.client.verify_owned_draft(item_id, board_key, title)
                    self.client.update_item_status(
                        project_id, item_id, status_field_id,
                        status_options[desired_status]
                    )
                    # Confirmed — persist status
                    self.state.update_task(title, item_id, desired_status)
                    self.state.save_after("UPDATE", title, item_id)
                else:
                    # No matching Status option on the Project. Item is
                    # created with no Status; state stays at None so the
                    # next sync will [UPDATE]-retry. Loud + persistent:
                    # the user must add the option or remove the kanban
                    # entry. (Closes the partymix REVIEW-sync gap class
                    # — audit D11.)
                    print(
                        f"  WARNING: '{desired_status}' has no matching "
                        f"Status option on the GitHub Project; item "
                        f"created with no Status. Sync will retry next "
                        f"run.",
                        file=sys.stderr,
                    )
            elif action == "UPDATE":
                # Status changed (or first status set after a previous
                # failed attempt — see D12 pattern above)
                print(
                    f"  [UPDATE] {title}: {operation['from_status']} "
                    f"=> {desired_status}"
                )
                if desired_status in status_options:
                    self.client.verify_owned_draft(item_id, board_key, title)
                    self.client.update_item_status(
                        project_id, item_id, status_field_id,
                        status_options[desired_status]
                    )
                    # Confirmed — persist
                    self.state.update_task(title, item_id, desired_status)
                    self.state.save_after(action, title, item_id)
                else:
                    # No matching Status option. Skip the update; state
                    # stays at stored_status. Sync will keep flagging
                    # until the option is added or the kanban entry is
                    # removed.
                    print(
                        f"  WARNING: '{desired_status}' has no matching "
                        f"Status option on the GitHub Project; status "
                        f"not updated. Sync will retry next run.",
                        file=sys.stderr,
                    )
            elif action in ("PENDING_UPDATE", "PENDING_ARCHIVE"):
                self.client.verify_owned_draft(item_id, board_key, title)
                instruction = (f"set its Status to {desired_status!r}"
                               if action == "PENDING_UPDATE" else "archive it")
                print(f"  [{action}] {title} ({item_id}). Confirm this is the expected draft "
                      f"on GitHub before you {instruction} manually.")
            elif action == "ARCHIVE":
                self.client.verify_owned_draft(item_id, board_key, title)
                print(f"  [ARCHIVE] {title}")
                self.client.archive_item(project_id, item_id)
                self.state.remove_task(title)
                # D7: persist after each archive so the local state matches
                # the GH-side archive even if the loop is interrupted.
                self.state.save_after(action, title, item_id)

        # End-of-loop save remains as a defensive flush; a no-op when
        # per-item saves already covered every mutation, but cheap and
        # keeps the existing "Sync complete" semantics intact.
        print(f"\nSaving state...")
        self.state.save_unlocked()
        print(f"Sync complete!")


def local_sync_preview(path: str):
    """Compute a local estimate using the same board and state checks as sync."""
    board = LocalBoard(path)
    local = _flatten_local_tasks(board.parse())
    state = StateManager(path)
    state.load(read_only=True)
    state.verify_board_key(board.board_key)
    return dict(build_sync_plan(local, state.state["tasks"]), scope="local")


def _main():
    _load_cli_dotenv()

    # Fix console encoding for Windows; 'replace' (R11) so a stray byte cannot raise into the parent's pipe drain.
    if sys.platform == 'win32':
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'replace')
    
    parser = argparse.ArgumentParser(description='Sync markdown kanban to GitHub Projects')
    parser.add_argument('kanban_file', help='Path to the markdown kanban file')
    parser.add_argument('--repo', help='GitHub repo (owner/name)', default=os.environ.get('GITHUB_REPO') or None)
    parser.add_argument('--project', type=int, help='GitHub Project number (optional if only one project linked)',
                        default=os.environ.get('GITHUB_PROJECT_NUMBER') or None)
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview locally, or read GitHub when used with --recover')
    
    parser.add_argument('--recover', action='store_true',
                        help='Rebuild sync state from marked drafts. Combine with --dry-run to preview.')
    parser.add_argument('--reviewed-item', action='append', metavar='ID',
                        help='With --recover, acknowledge an unmarked Project item by ID. Repeat for each item.')
    parser.add_argument('--confirm-legacy-item', action='append', metavar='ID',
                        help='With --recover, confirm this board owns the legacy sidecar claim for ID.')
    parser.add_argument('--review-token', help='Use the token from the recovery preview for these item IDs.')
    args = parser.parse_args()
    review_options = args.reviewed_item or args.confirm_legacy_item or args.review_token
    if review_options and (not args.recover or args.dry_run):
        raise ConfigurationError("Use review options when applying --recover.")
    
    # Convert project number from env var
    if args.project and isinstance(args.project, str):
        args.project = int(args.project) if args.project.isdigit() else None

    if not os.path.exists(args.kanban_file):
        raise ConfigurationError(f"File not found: {args.kanban_file}")

    # Initialize components
    if args.dry_run and not args.recover:
        print(json.dumps(local_sync_preview(args.kanban_file)))
        return

    if not args.repo:
        raise ConfigurationError(
            "--repo or GITHUB_REPO environment variable required"
        )

    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ConfigurationError("GITHUB_TOKEN environment variable not set")
    
    state = StateManager(args.kanban_file)
    client = GitHubClient(token)
    syncer = Syncer(LocalBoard(args.kanban_file), state, client)
    
    result = syncer.sync(args.repo, args.project, recover=args.recover, preview=args.dry_run,
                         reviewed_item_ids=args.reviewed_item, review_token=args.review_token,
                         confirmed_legacy_item_ids=args.confirm_legacy_item)
    if args.recover:
        print(json.dumps(result))


def main():
    """Run the console entry point without leaking expected errors as traces."""
    try:
        _main()
    except KanbangerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # E1: catch typed library errors and present them with the same
    # 'Error: <message>' shape callers used to print directly. Same
    # exit code (1), same human-facing text; messages now uniformly
    # land on stderr (was a mix of stdout + stderr previously).
    # Other exceptions propagate with their traceback — those are bugs.
    try:
        sys.exit(main())
    finally:
        # R11: flush before exit so the parent sees a clean EOF on its pipes.
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
