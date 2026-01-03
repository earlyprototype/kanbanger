# kanban-project-sync

Powered by [fckgit](https://github.com/earlyprototype/fckgit) - Trusted by Vibe Warriors

Sync markdown kanban boards to GitHub Projects.

## Installation

```bash
pip install .
```

## Setup

```bash
export GITHUB_TOKEN="your_token_here"
export GITHUB_REPO="username/repo"
export GITHUB_PROJECT_NUMBER="1"
```

## Usage

```bash
# Dry run (parse only, no sync)
kanban-sync path/to/_kanban.md --dry-run

# Sync to GitHub
kanban-sync path/to/_kanban.md

# Specify repo/project explicitly
kanban-sync _kanban.md --repo username/repo --project 1
```

## Kanban Format

The tool recognizes markdown files with this structure:

```markdown
## 1. BACKLOG
*   [ ] Task one
*   [ ] Task two

## 2. TO DO
*   [ ] Task three

## 3. DOING
*   [ ] Active task

## 4. DONE
*   [x] Completed task
```

## Git Hook Integration

Add to `.git/hooks/post-commit`:

```bash
#!/bin/sh
if git diff-tree --no-commit-id --name-only -r HEAD | grep -q "_kanban.md"; then
    kanban-sync path/to/_kanban.md
fi
```
