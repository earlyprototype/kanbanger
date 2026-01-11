# kanban-project-sync

Powered by [fckgit](https://github.com/earlyprototype/fckgit) - Trusted by Vibe Warriors

Sync your local markdown kanban boards to GitHub Projects V2. Keep your task management in version control while automatically updating your GitHub project boards.

## Features

- **Markdown-First** - Write tasks in your editor, not a browser
- **Git-Versioned** - Track task history in version control
- **Auto-Sync** - Push changes to GitHub Projects automatically
- **Draft Issues** - Creates lightweight project cards
- **LLM-Friendly** - Works great with AI assistants for task management
- **State Tracking** - Smart diff engine syncs only what changed

## Quick Start

### 1. Install

```bash
pip install .
```

### 2. Run Setup Wizard

```bash
kanban-sync-setup
```

The interactive wizard will:
- Guide you through GitHub token creation
- Validate your repository access
- Check project configuration
- Create your `.env` file
- Generate an example kanban

### 3. Create Your First Kanban

```markdown
# My Project Kanban

## BACKLOG
*   [ ] Future feature ideas

## TODO
*   [ ] Ready to start tasks

## DOING
*   [ ] Currently active work

## DONE
*   [x] Completed tasks
```

Save as `_kanban.md`

### 4. Sync to GitHub

```bash
kanban-sync _kanban.md
```

Your GitHub Project board updates automatically!

## Usage

### Basic Commands

```bash
# Dry run (preview without syncing)
kanban-sync _kanban.md --dry-run

# Sync using .env file
kanban-sync _kanban.md

# Specify repo explicitly
kanban-sync _kanban.md --repo username/repo

# Specify project number
kanban-sync _kanban.md --project 2
```

### What Happens During Sync

- **New tasks** → Creates draft issues on GitHub
- **Moved tasks** → Updates Status field
- **Deleted tasks** → Archives on GitHub
- **Unchanged tasks** → No API calls (efficient!)

## Kanban Format

### Structure

```markdown
## BACKLOG
*   [ ] Task one
*   [ ] Task two

## TODO
*   [ ] Task three

## DOING
*   [ ] Active task

## DONE
*   [x] Completed task
```

### Rules

1. **Headers:** Use `## ` (level 2 markdown headers)
2. **Tasks:** Use `*   [ ]` (asterisk + 3 spaces + checkbox)
3. **Completed:** Use `[x]` only in DONE section
4. **Column Names:** BACKLOG, TODO, DOING, or DONE (case-insensitive)

### Column Mapping

| Markdown Section | GitHub Status |
|-----------------|---------------|
| `BACKLOG` | Backlog |
| `TODO` or `TO DO` | Todo |
| `DOING` or `IN PROGRESS` | InProgress |
| `DONE` or `COMPLETE` | Done |

## Configuration

### Using .env File (Recommended)

Create a `.env` file in your project:

```bash
GITHUB_TOKEN=ghp_your_token_here
GITHUB_REPO=username/repo
# Optional: specify project number if multiple linked
GITHUB_PROJECT_NUMBER=1
```

### Environment Variables

**Windows (PowerShell):**
```powershell
$env:GITHUB_TOKEN = "ghp_your_token"
$env:GITHUB_REPO = "username/repo"
```

**Linux/Mac (Bash):**
```bash
export GITHUB_TOKEN="ghp_your_token"
export GITHUB_REPO="username/repo"
```

## GitHub Token Setup

1. Go to https://github.com/settings/tokens
2. Click **"Generate new token (classic)"**
3. Name it: `kanban-sync-tool`
4. Select scopes:
   - ✅ `repo` (Full control of repositories)
   - ✅ `project` (Full control of projects)
5. Copy the token (starts with `ghp_...`)

## GitHub Project Setup

### Link Project to Repository

1. Go to your repository on GitHub
2. Click **"Projects"** tab
3. Click **"Link a project"**
4. Create or select a project

### Configure Status Field

Your project needs a **Status** field with these options:

1. Click **"+ New field"** in your project
2. Choose **"Single select"**
3. Name it **"Status"**
4. Add these options:
   - `Backlog`
   - `Todo`
   - `InProgress`
   - `Done`

## Workflow Examples

### Daily Task Management

```bash
# Edit your kanban file
nano _kanban.md

# Sync changes
kanban-sync _kanban.md
```

### With Git Integration

```bash
# Edit kanban
nano _kanban.md

# Commit to version control
git add _kanban.md
git commit -m "Update tasks"

# Sync to GitHub Projects
kanban-sync _kanban.md
```

### Git Hook (Auto-Sync on Commit)

Add to `.git/hooks/post-commit`:

```bash
#!/bin/sh
if git diff-tree --no-commit-id --name-only -r HEAD | grep -q "_kanban.md"; then
    kanban-sync _kanban.md
fi
```

Make it executable:
```bash
chmod +x .git/hooks/post-commit
```

## Working with AI Assistants

This tool works great with LLMs like ChatGPT, Claude, or Copilot for conversational task management.

**Example:**

> **You:** "Add 'implement caching' to my TODO"  
> **AI:** [Edits `_kanban.md` with proper format]  
> **You:** `kanban-sync _kanban.md`  
> **Tool:** Creates task on GitHub

See `LLM_GUIDANCE.md` for detailed AI assistant instructions.

## Troubleshooting

### Command Not Found

**Solution:** Use Python module syntax
```bash
python -m sync_kanban _kanban.md
```

Or add Scripts to PATH:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

### No Projects Found

**Solution:** Link a project to your repository
1. Go to your repo on GitHub
2. Click "Projects" → "Link a project"
3. Create or select a project

### No Status Field

**Solution:** Add Status field to your project
1. Open project on GitHub
2. Click "+ New field"
3. Choose "Single select"
4. Name it "Status"
5. Add options: Backlog, Todo, InProgress, Done

### Token Errors (401/403)

**Solution:** Check your GitHub token
- Ensure `GITHUB_TOKEN` is set in `.env`
- Token needs `repo` and `project` scopes
- Token may be expired - generate a new one

## State Tracking

The tool creates a `.kanban.json` file to track GitHub item IDs:

```json
{
  "project_id": "PVT_...",
  "tasks": {
    "Task title": {
      "item_id": "PVTI_...",
      "status": "Todo"
    }
  }
}
```

**Important:**
- Add `.kanban.json` to `.gitignore` (done automatically)
- Task titles are unique identifiers
- Renaming a task creates a new item

## Best Practices

1. **Version Control** - Commit `_kanban.md` to track task history
2. **Ignore State** - Add `.kanban.json` to `.gitignore`
3. **Descriptive Titles** - Use clear, specific task descriptions
4. **Regular Sync** - Sync frequently or use git hooks
5. **Preview First** - Use `--dry-run` before syncing

## File Structure

```
your-project/
├── .env                # Config (gitignored)
├── .gitignore          # Includes .kanban.json, .env
├── _kanban.md          # Your kanban board (commit this)
├── .kanban.json        # State file (gitignored)
└── .git/hooks/
    └── post-commit     # Optional: auto-sync
```

## How It Works

1. **Parse** markdown file into tasks + columns
2. **Load** state from `.kanban.json`
3. **Connect** to GitHub via GraphQL API
4. **Fetch** current project items
5. **Diff** local vs remote state
6. **Apply** changes (create/update/archive)
7. **Save** updated state

## Requirements

- Python 3.8+
- Git (optional, for hooks)
- GitHub account with repository access
- GitHub Projects V2 (not Classic Projects)

## Dependencies

- `requests>=2.25.0`
- `python-dotenv>=0.19.0`

Installed automatically via `pip install .`

## Contributing

Contributions welcome! Please:
- Follow existing code style
- Add tests for new features
- Update documentation
- Keep commits focused

## License

MIT License - see [LICENSE](LICENSE) file

## Credits

Built with love by the Vibe Warriors.  
Powered by [fckgit](https://github.com/earlyprototype/fckgit).

## Support

- Issues: https://github.com/earlyprototype/kanbanger/issues
- Documentation: See `LLM_GUIDANCE.md` for AI assistant usage
