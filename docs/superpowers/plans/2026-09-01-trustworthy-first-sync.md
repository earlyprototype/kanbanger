# Trustworthy First Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan test-first.

**Goal:** Make the first Kanbanger GitHub sync safe, previewable, observable, and provable against the existing sandbox.

**Architecture:** Keep `_kanban.md` and `.kanban.json` as the existing local authorities. Add one pure planning function that compares parsed local tasks with saved sync state; both dry-run and the existing mutation executor consume that plan. Keep GitHub at the outer boundary so deterministic tests replace only HTTP while exercising real parsing, state persistence, planning, and execution.

**Tech Stack:** Python 3.10+, pytest, FastMCP, GitHub Projects V2 GraphQL, stdlib plus existing `requests` dependency.

**Spec:** https://github.com/earlyprototype/kanbanger/issues/31

## Global Constraints

- Preserve the five canonical columns and one-way local-to-GitHub authority.
- Do not add a dependency, configuration system, workflow, inbound sync, card-editing feature, or identity migration.
- Dry-run must not require credentials and must not make a network request or write `.kanban.json`.
- A real sync must execute the same CREATE, UPDATE, and ARCHIVE operations reported by the planner.
- All production behavior changes start with a failing test that is observed failing for the intended reason.
- Live closure uses `earlyprototype/junk2` and user Project V2 `junk-kanban` #8; the run must create, update, verify, and archive only its unique run-scoped card.

---

### Task 1: Deliver the complete first-sync journey

**Files:**
- Modify: `kanbanger/provision.py`
- Modify: `sync_kanban.py`
- Modify: `kanbanger/tools.py`
- Modify: `kanbanger/resources.py`
- Modify: `README.md` only where current first-sync/dry-run wording changes
- Modify: `tests/test_provision.py`
- Modify: `tests/test_stdio_e2e.py`
- Create: `tests/test_sync_lifecycle.py`

**Interfaces:**
- Produce `build_sync_plan(local_flat: dict[str, str], state_tasks: dict[str, dict]) -> dict` in `sync_kanban.py`.
- The returned dictionary has `operations` in local task order followed by removed-state order, and `summary` counts for `create`, `update`, and `archive`.
- Each operation has `action`, `title`, `from_status`, `to_status`, and `item_id`; action is exactly `CREATE`, `UPDATE`, or `ARCHIVE`.
- CLI `kanban-sync <board> --dry-run` prints that dictionary as JSON and exits zero without GitHub configuration.
- MCP `sync_to_github(dry_run=True)` returns a JSON success envelope containing the same plan.

- [ ] **Step 1: Prove fresh provisioning is safe**

  Add failing tests showing `build_kanban_board()` parses to five empty columns and provisioning idempotently ignores all six runtime paths:

  ```python
  expected_ignored = {
      ".env", ".env.local", ".kanban.json", ".kanban.lock",
      ".claude/settings.local.json", ".venv/",
  }
  assert all(not tasks for tasks in LocalBoard(str(board_path)).parse().values())
  assert expected_ignored <= set(gitignore_path.read_text().splitlines())
  ```

  Run the focused tests and observe failures caused by parser-visible placeholders and missing ignore entries. Replace placeholder checkbox rows with parser-invisible guidance and extend the existing idempotent ignore writer; do not add a second provisioning helper.

- [ ] **Step 2: Prove the planner contract before implementing it**

  In `tests/test_sync_lifecycle.py`, write literal table cases for:

  ```python
  local = {"New": "Todo", "Moved": "Review"}
  state = {
      "Moved": {"item_id": "PVTI_moved", "status": "InProgress"},
      "Removed": {"item_id": "PVTI_removed", "status": "Done"},
  }
  ```

  Assert ordered CREATE, UPDATE, ARCHIVE operations and literal summary counts. Observe the import/test failure, then implement the smallest pure `build_sync_plan` function.

- [ ] **Step 3: Make dry-run a real observable endpoint**

  Add a failing CLI test and MCP stdio assertion proving dry-run:

  ```python
  assert payload["success"] is True
  assert payload["mode"] == "preview"
  assert payload["plan"]["operations"][0]["action"] == "CREATE"
  ```

  The test must run with `GITHUB_TOKEN` and `GITHUB_REPO` absent, assert no `.kanban.json` is created, and trap HTTP so any network access fails the test. Change the CLI and MCP wrapper minimally so they load board/state, call the planner, and return the plan without configuration prechecks or network access.

- [ ] **Step 4: Make real execution consume the plan**

  Add a deterministic lifecycle test using a complete fake GitHub GraphQL boundary. It must exercise real `LocalBoard`, `StateManager`, `Syncer`, and `GitHubClient` behavior while replacing only `requests.post` responses. Assert:

  ```python
  assert created_state[title]["status"] == "Todo"
  assert updated_state[title]["status"] == "Review"
  assert title not in archived_state
  ```

  Assert the emitted mutations carry the expected project item and Status option IDs. Observe RED, then refactor `Syncer.sync()` to execute `build_sync_plan(...)`. Preserve per-mutation state saves and missing-Status retry semantics. Remove only remote-fetch code proven dead by the plan; do not introduce remote reconciliation.

- [ ] **Step 5: Close the observable failure gaps on this path**

  Add failing tests proving:

  - `kanban://sync-status` reads `state["tasks"]` and matches the tool's titles/count/IDs.
  - `requests.post` receives a finite timeout.
  - the installed `kanban-sync` entry path reports `Error: ...` without a traceback for a missing file or configuration failure.

  Apply the smallest changes in the existing resource, `_query`, and CLI boundary. Do not sweep unrelated resource or doctor drift.

- [ ] **Step 6: Verify locally and document only changed behavior**

  Run focused RED/GREEN cycles throughout, then:

  ```powershell
  python -m pytest -q
  python -m build
  ```

  Install the wheel into a temporary virtual environment and smoke the four console entry points. Update only README statements that claim placeholders are tasks or dry-run merely parses/reports something different from the new behavior.

- [ ] **Step 7: Pass the live sandbox closure gate**

  Using the built branch artifact and a temporary workspace, create a title prefixed `kanbanger-smoke-<UTC timestamp>`. Run preview, real creation, local move to REVIEW, real update, and local removal/archive against `earlyprototype/junk2`, Project #8. Query GitHub after each mutation to verify the card and exact Status. Confirm the archived item is no longer active and that no unrelated item was mutated. Put redacted commands, item ID, timestamps, and outcomes in the closing PR; never print or commit the token.

- [ ] **Step 8: Commit and hand off for review**

  Commit the tested implementation with issue #31 in the message. The controller dispatches a code review, resolves findings through the worker, pushes the branch, and opens a PR with `Closes #31`. The Kanbanger task moves to REVIEW only after CI and the live sandbox gate both pass.
