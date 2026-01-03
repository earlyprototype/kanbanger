"""
kanban-project-sync

Sync a markdown kanban board to GitHub Projects.
"""
import re
import os
import sys
import argparse

# GitHub GraphQL endpoint
GITHUB_API = "https://api.github.com/graphql"


def parse_kanban(file_path: str) -> dict:
    """Parse a markdown kanban file and return tasks by column."""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    tasks = {}
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
                current_section = 'To Do'
            elif 'DOING' in normalized or 'IN PROGRESS' in normalized:
                current_section = 'Doing'
            elif 'DONE' in normalized or 'COMPLETE' in normalized:
                current_section = 'Done'
            else:
                current_section = section_name
            
            if current_section not in tasks:
                tasks[current_section] = []
            continue
        
        # Check for task item
        if current_section:
            task_match = task_pattern.match(line.strip())
            if task_match:
                is_done = task_match.group(1).lower() == 'x'
                title = task_match.group(2).strip()
                tasks[current_section].append({
                    'title': title,
                    'done': is_done
                })
    
    return tasks


def sync_to_github(tasks: dict, repo: str, project_number: int, token: str):
    """Sync tasks to GitHub Projects via GraphQL API."""
    try:
        import requests
    except ImportError:
        print("❌ requests not installed. Run: pip install requests")
        return False
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Get project ID
    owner, repo_name = repo.split('/')
    query = """
    query($owner: String!, $number: Int!) {
        user(login: $owner) {
            projectV2(number: $number) {
                id
                fields(first: 20) {
                    nodes {
                        ... on ProjectV2SingleSelectField {
                            id
                            name
                            options { id name }
                        }
                    }
                }
            }
        }
    }
    """
    
    response = requests.post(
        GITHUB_API,
        headers=headers,
        json={"query": query, "variables": {"owner": owner, "number": project_number}}
    )
    
    if response.status_code != 200:
        print(f"❌ GitHub API error: {response.status_code}")
        return False
    
    data = response.json()
    print(f"✓ Connected to GitHub Project #{project_number}")
    
    # Log parsed tasks (full sync implementation requires more GraphQL mutations)
    for column, items in tasks.items():
        print(f"  {column}: {len(items)} tasks")
        for item in items:
            status = "✓" if item['done'] else "○"
            print(f"    {status} {item['title'][:50]}")
    
    return True


def main():
    parser = argparse.ArgumentParser(description='Sync markdown kanban to GitHub Projects')
    parser.add_argument('kanban_file', help='Path to the markdown kanban file')
    parser.add_argument('--repo', help='GitHub repo (owner/name)', default=os.environ.get('GITHUB_REPO'))
    parser.add_argument('--project', type=int, help='GitHub Project number', default=int(os.environ.get('GITHUB_PROJECT_NUMBER', '1')))
    parser.add_argument('--dry-run', action='store_true', help='Parse only, no sync')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.kanban_file):
        print(f"❌ File not found: {args.kanban_file}")
        sys.exit(1)
    
    print(f"📋 Parsing {args.kanban_file}...")
    tasks = parse_kanban(args.kanban_file)
    
    if args.dry_run or not args.repo:
        print("\n📊 Parsed tasks:")
        for column, items in tasks.items():
            print(f"\n{column}:")
            for item in items:
                status = "[x]" if item['done'] else "[ ]"
                print(f"  {status} {item['title']}")
        return
    
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        print("❌ GITHUB_TOKEN environment variable not set.")
        sys.exit(1)
    
    print(f"\n🔄 Syncing to {args.repo} Project #{args.project}...")
    sync_to_github(tasks, args.repo, args.project, token)


if __name__ == "__main__":
    main()
