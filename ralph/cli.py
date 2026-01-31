"""CLI interface for ralph using Typer."""

import json
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .harness import (
    get_prd_path,
    get_progress_path,
    get_ralph_dir,
    is_git_repo,
    load_prd,
    run_loop,
)
from .models import PRD, GlobalConfig, ItemState, ProjectMeta

app = typer.Typer(
    name="ralph",
    help="A CLI harness for running LLM agents on PRD-driven tasks",
    no_args_is_help=True,
)
console = Console()


def get_available_models() -> list[str]:
    """Get list of available models from opencode."""
    try:
        result = subprocess.run(
            ["opencode", "models"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
        return []
    except Exception:
        return []


def select_model_interactive() -> Optional[str]:
    """Show interactive model selection prompt."""
    models = get_available_models()
    if not models:
        console.print("[red]Error: Could not fetch models from opencode[/red]")
        console.print("Make sure opencode is installed and configured.")
        return None

    console.print("\n[bold]Available models:[/bold]\n")
    for i, model in enumerate(models, 1):
        console.print(f"  {i:3}. {model}")

    console.print()
    while True:
        try:
            choice = console.input("[bold]Select model number:[/bold] ")
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                return models[idx]
            console.print(
                f"[red]Please enter a number between 1 and {len(models)}[/red]"
            )
        except ValueError:
            console.print("[red]Please enter a valid number[/red]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled[/yellow]")
            return None


def get_repo_root() -> Path:
    """Get the repository root (current working directory)."""
    return Path.cwd()


@app.command()
def init(
    name: str = typer.Option("my-project", "--name", "-n", help="Project name"),
    language: str = typer.Option(
        "unknown", "--language", "-l", help="Project language"
    ),
    branch: str = typer.Option("main", "--branch", "-b", help="Default branch name"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing files"),
):
    """Initialize a new .ralph directory with empty PRD and progress files."""
    repo_root = get_repo_root()
    ralph_dir = get_ralph_dir(repo_root)
    prd_path = get_prd_path(repo_root)
    progress_path = get_progress_path(repo_root)

    # Check if already initialized
    if ralph_dir.exists() and not force:
        console.print(
            "[yellow].ralph directory already exists. Use --force to overwrite.[/yellow]"
        )
        raise typer.Exit(1)

    # Create directory
    ralph_dir.mkdir(parents=True, exist_ok=True)

    # Create empty PRD
    prd = PRD(
        version=1,
        project=ProjectMeta(
            name=name,
            language=language,
            default_branch=branch,
        ),
        global_config=GlobalConfig(verify=[]),
        items=[],
    )

    with open(prd_path, "w") as f:
        json.dump(prd.model_dump(by_alias=True), f, indent=2)

    # Create empty progress log
    progress_path.touch()

    # Create README for agents
    readme_path = ralph_dir / "README.md"
    readme_content = """# Ralph Project Directory

This directory contains the PRD (Product Requirements Document) and progress tracking for this project.

## Files

- **prd.json**: The PRD containing all work items. Edit this file to add, remove, or update tasks.
- **progress.txt**: A timestamped log of all actions taken by the Ralph harness.

## Working with the PRD

The PRD is organized into work items, each with:
- `id`: Unique identifier (e.g., "001", "002")
- `title`: Brief description
- `description`: Detailed task description
- `acceptance_criteria`: List of conditions that must be met
- `files_hint` (optional): Suggested files to work on
- `verify` (optional): Verification commands to run
- `status`: Current state (todo/doing/done/blocked) and tracking

### Item States

- **todo**: Ready to start
- **doing**: Currently in progress (resumes here after crashes)
- **done**: Successfully completed
- **blocked**: Failed multiple times (check `last_error`)

### Example Work Item

```json
{
  "id": "001",
  "title": "Implement user authentication",
  "description": "Add login and registration endpoints",
  "acceptance_criteria": [
    "Users can register with email/password",
    "Users can login and receive a session token",
    "Passwords are hashed"
  ],
  "files_hint": ["auth.py", "models.py"],
  "verify": ["pytest tests/test_auth.py"],
  "status": {
    "state": "todo",
    "attempts": 0,
    "last_error": null,
    "done_at": null
  }
}
```

## Progress Tracking

The `progress.txt` file contains a chronological log of all actions:
- Items started/resumed
- Completion or failure messages
- Error details for debugging

When an agent is interrupted, it will resume from the last "doing" item.
"""
    with open(readme_path, "w") as f:
        f.write(readme_content)

    console.print(f"[green]Initialized ralph in {ralph_dir}[/green]")
    console.print(f"  Created: {prd_path}")
    console.print(f"  Created: {progress_path}")
    console.print(f"  Created: {readme_path}")
    console.print()
    console.print("Next steps:")
    console.print(f"  1. Edit {prd_path} to add your work items")
    console.print("  2. Run 'ralph run' to start the harness")


@app.command()
def run(
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Model to use with opencode (e.g. claude-sonnet-4-20250514)",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-I",
        help="Interactively select model from available options",
    ),
    max_iterations: int = typer.Option(
        50, "--max-iterations", "-i", help="Maximum number of iterations"
    ),
    max_failures: int = typer.Option(
        10, "--max-failures", "-f", help="Maximum consecutive failures before stopping"
    ),
    no_commit: bool = typer.Option(
        False, "--no-commit", help="Don't commit changes (dry run)"
    ),
):
    """Start the harness loop to process PRD items."""
    repo_root = get_repo_root()

    # Handle model selection
    if interactive:
        selected_model = select_model_interactive()
        if selected_model is None:
            raise typer.Exit(1)
        model = selected_model
    elif model is None:
        console.print("[red]Error: Either --model or --interactive is required[/red]")
        console.print(
            "Use --model to specify a model directly, or --interactive to select from a list."
        )
        raise typer.Exit(1)

    # Check if git repo
    if not is_git_repo(repo_root):
        console.print("[red]Error: Not a git repository[/red]")
        console.print("Please run this command from within a git repository.")
        raise typer.Exit(1)

    # Check if initialized
    ralph_dir = get_ralph_dir(repo_root)
    if not ralph_dir.exists():
        console.print("[red]Error: .ralph directory not found[/red]")
        console.print("Run 'ralph init' first to initialize.")
        raise typer.Exit(1)

    console.print("[bold]Starting ralph harness...[/bold]")
    console.print(f"  Model: {model}")
    console.print(f"  Max iterations: {max_iterations}")
    console.print(f"  Max failures: {max_failures}")
    console.print(f"  Commit changes: {not no_commit}")
    console.print()

    exit_code = run_loop(
        repo_root=repo_root,
        max_iterations=max_iterations,
        max_failures=max_failures,
        no_commit=no_commit,
        model=model,
    )

    raise typer.Exit(exit_code)


@app.command()
def status():
    """Show the current status of PRD items."""
    repo_root = get_repo_root()

    try:
        prd = load_prd(repo_root)
    except FileNotFoundError:
        console.print("[red]Error: .ralph directory not found[/red]")
        console.print("Run 'ralph init' first to initialize.")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error loading PRD: {e}[/red]")
        raise typer.Exit(1)

    # Print project info
    console.print(f"[bold]Project:[/bold] {prd.project.name}")
    console.print(f"[bold]Language:[/bold] {prd.project.language}")
    console.print()

    # Count by state
    counts = prd.count_by_state()
    console.print("[bold]Summary:[/bold]")
    console.print(f"  Todo: {counts[ItemState.TODO]}")
    console.print(f"  Doing: {counts[ItemState.DOING]}")
    console.print(f"  Done: {counts[ItemState.DONE]}")
    console.print(f"  Blocked: {counts[ItemState.BLOCKED]}")
    console.print()

    # Create table
    table = Table(title="Work Items")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("State", justify="center")
    table.add_column("Attempts", justify="right")
    table.add_column("Last Error")

    state_colors = {
        ItemState.TODO: "white",
        ItemState.DOING: "yellow",
        ItemState.DONE: "green",
        ItemState.BLOCKED: "red",
    }

    for item in prd.items:
        state_str = f"[{state_colors[item.status.state]}]{item.status.state.value}[/{state_colors[item.status.state]}]"
        error_str = (
            item.status.last_error[:50] + "..."
            if item.status.last_error and len(item.status.last_error) > 50
            else (item.status.last_error or "")
        )
        table.add_row(
            item.id,
            item.title[:40] + ("..." if len(item.title) > 40 else ""),
            state_str,
            str(item.status.attempts),
            error_str,
        )

    console.print(table)


@app.command()
def validate():
    """Validate the prd.json file."""
    repo_root = get_repo_root()
    prd_path = get_prd_path(repo_root)

    if not prd_path.exists():
        console.print(f"[red]Error: {prd_path} not found[/red]")
        raise typer.Exit(1)

    try:
        prd = load_prd(repo_root)
        console.print("[green]prd.json is valid![/green]")
        console.print(f"  Version: {prd.version}")
        console.print(f"  Project: {prd.project.name}")
        console.print(f"  Items: {len(prd.items)}")
        console.print(f"  Global verify commands: {len(prd.global_config.verify)}")
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON: {e}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Validation error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def reset(
    item_id: Optional[str] = typer.Argument(
        None, help="Item ID to reset (resets all if not specified)"
    ),
    include_blocked: bool = typer.Option(
        False, "--include-blocked", help="Also reset blocked items"
    ),
):
    """Reset item(s) back to todo state."""
    repo_root = get_repo_root()

    try:
        prd = load_prd(repo_root)
    except Exception as e:
        console.print(f"[red]Error loading PRD: {e}[/red]")
        raise typer.Exit(1)

    reset_count = 0

    if item_id:
        # Reset specific item
        item = prd.get_item_by_id(item_id)
        if item is None:
            console.print(f"[red]Item not found: {item_id}[/red]")
            raise typer.Exit(1)

        if item.status.state == ItemState.BLOCKED and not include_blocked:
            console.print(
                f"[yellow]Item {item_id} is blocked. Use --include-blocked to reset.[/yellow]"
            )
            raise typer.Exit(1)

        item.status.state = ItemState.TODO
        item.status.attempts = 0
        item.status.last_error = None
        item.status.done_at = None
        reset_count = 1
    else:
        # Reset all items
        for item in prd.items:
            if item.status.state == ItemState.BLOCKED and not include_blocked:
                continue
            if item.status.state != ItemState.TODO:
                item.status.state = ItemState.TODO
                item.status.attempts = 0
                item.status.last_error = None
                item.status.done_at = None
                reset_count += 1

    # Save PRD
    from .harness import save_prd

    save_prd(repo_root, prd)

    console.print(f"[green]Reset {reset_count} item(s) to todo state[/green]")


if __name__ == "__main__":
    app()
