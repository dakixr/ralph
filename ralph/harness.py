"""Core harness logic for ralph."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from .models import PRD, ItemState, WorkItem

console = Console()

RALPH_DIR = ".ralph"
PRD_FILE = "prd.json"
PROGRESS_FILE = "progress.txt"

# Default blocked threshold - mark as blocked after this many failed attempts
BLOCKED_THRESHOLD = 3


def get_ralph_dir(repo_root: Path) -> Path:
    """Get the .ralph directory path."""
    return repo_root / RALPH_DIR


def get_prd_path(repo_root: Path) -> Path:
    """Get the prd.json path."""
    return get_ralph_dir(repo_root) / PRD_FILE


def get_progress_path(repo_root: Path) -> Path:
    """Get the progress.log path."""
    return get_ralph_dir(repo_root) / PROGRESS_FILE


def load_prd(repo_root: Path) -> PRD:
    """Load and validate the PRD from disk."""
    prd_path = get_prd_path(repo_root)
    if not prd_path.exists():
        raise FileNotFoundError(f"PRD file not found: {prd_path}")

    with open(prd_path) as f:
        data = json.load(f)

    return PRD.model_validate(data)


def save_prd(repo_root: Path, prd: PRD) -> None:
    """Save the PRD to disk."""
    prd_path = get_prd_path(repo_root)
    with open(prd_path, "w") as f:
        json.dump(prd.model_dump(by_alias=True), f, indent=2, default=str)


def append_progress(repo_root: Path, message: str) -> None:
    """Append a message to progress.log with timestamp."""
    progress_path = get_progress_path(repo_root)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(progress_path, "a") as f:
        f.write(f"[{timestamp}] {message}\n")


def is_git_repo(path: Path) -> bool:
    """Check if the path is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def git_commit(repo_root: Path, message: str) -> bool:
    """Stage all changes and commit."""
    try:
        # Stage all changes
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_root,
            capture_output=True,
        )
        if result.returncode == 0:
            # No changes to commit
            return True
        # Commit
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Git commit failed: {e}[/red]")
        return False


def build_agent_prompt(item: WorkItem, prd: PRD, repo_root: Path) -> str:
    """Build the prompt for the LLM agent."""
    progress_path = get_progress_path(repo_root)

    prompt = f"""You are an autonomous coding agent working on a software project.

## Your Task

You must implement the following work item:

**ID:** {item.id}
**Title:** {item.title}

**Description:**
{item.description}

**Acceptance Criteria:**
{chr(10).join(f"- {criterion}" for criterion in item.acceptance_criteria)}

"""
    if item.files_hint:
        prompt += f"""**Files to focus on:**
{chr(10).join(f"- {f}" for f in item.files_hint)}

"""

    prompt += f"""## Project Context

- **Project Name:** {prd.project.name}
- **Language:** {prd.project.language}
- **Default Branch:** {prd.project.default_branch}

## Rules

1. Make minimal, focused changes to implement the task
2. Do NOT mark the item as done - the harness will do that after verification
3. Update or create tests as needed to cover your changes
4. Ensure the codebase compiles/runs after your changes
5. Follow existing code style and conventions
6. If you encounter blockers, document them clearly

## Verification Commands

After your changes, the following commands will be run to verify success:
{chr(10).join(f"- `{cmd}`" for cmd in (item.verify or prd.global_config.verify))}

## Long-term Memory

You can read and write to the progress log file at `{progress_path}` to maintain context across sessions.
Use this file to:
- Record important decisions and reasoning
- Note any issues encountered
- Track partial progress on complex tasks
- Leave notes for future iterations

## Instructions

Implement the task described above. Make the necessary code changes to satisfy all acceptance criteria.
When you are done making changes, simply finish your session - do not try to mark the task as complete.
"""
    return prompt


def run_verification(repo_root: Path, commands: list[str]) -> tuple[bool, list[dict]]:
    """Run verification commands and return success status and results."""
    results = []
    all_passed = True

    for cmd in commands:
        console.print(f"  Running: [cyan]{cmd}[/cyan]")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            passed = result.returncode == 0
            results.append(
                {
                    "command": cmd,
                    "passed": passed,
                    "returncode": result.returncode,
                    "stdout": result.stdout[-2000:] if result.stdout else "",
                    "stderr": result.stderr[-2000:] if result.stderr else "",
                }
            )
            if passed:
                console.print("    [green]PASSED[/green]")
            else:
                console.print(f"    [red]FAILED (exit code {result.returncode})[/red]")
                all_passed = False
        except Exception as e:
            results.append(
                {
                    "command": cmd,
                    "passed": False,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(e),
                }
            )
            console.print(f"    [red]ERROR: {e}[/red]")
            all_passed = False

    return all_passed, results


def run_agent(repo_root: Path, prompt: str, model: str) -> tuple[bool, str]:
    """Run opencode as a subprocess.

    Returns (success, error_message).
    """
    agent_command = ["opencode", "-m", model, "run", prompt]
    # Don't print the full prompt (it can be huge). Keep logging concise.
    console.print(f"  Agent command: [cyan]opencode -m {model} run <prompt>[/cyan]")

    try:
        # Important: do NOT pipe stdout/stderr. Some agents switch behavior and
        # may not emit output when not connected to a real TTY. Inheriting the
        # parent's streams guarantees visibility in the main process.
        result = subprocess.run(
            agent_command,
            cwd=repo_root,
        )

        if result.returncode != 0:
            return False, f"Exit code {result.returncode}"

        return True, ""

    except Exception as e:
        return False, str(e)


def run_loop(
    repo_root: Path,
    model: str,
    max_iterations: int = 50,
    max_failures: int = 10,
    no_commit: bool = False,
) -> int:
    """Run the main harness loop.

    Returns exit code (0 for success, non-zero for failure).
    """
    # Startup checks
    if not is_git_repo(repo_root):
        console.print("[red]Error: Not a git repository[/red]")
        return 1

    try:
        prd = load_prd(repo_root)
    except FileNotFoundError as e:
        console.print(f"[red]Error: {e}[/red]")
        console.print("Run 'ralph init' first to create the .ralph directory")
        return 1
    except json.JSONDecodeError as e:
        console.print(f"[red]Error: Invalid JSON in prd.json: {e}[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Error loading PRD: {e}[/red]")
        return 1

    # Ensure progress.log exists
    progress_path = get_progress_path(repo_root)
    if not progress_path.exists():
        progress_path.touch()

    iteration = 0
    failures = 0

    append_progress(
        repo_root,
        f"=== Harness started (max_iterations={max_iterations}, max_failures={max_failures}) ===",
    )

    while iteration < max_iterations and failures < max_failures:
        # Reload PRD each iteration (may have been modified)
        prd = load_prd(repo_root)

        # Check if all done
        if prd.all_done():
            console.print("\n[green]All items complete![/green]")
            append_progress(repo_root, "=== All items complete ===")
            return 0

        # Select next item (resume "doing" first, then "todo")
        item, is_resuming = prd.get_next_item()
        if item is None:
            console.print("\n[yellow]No more items to process[/yellow]")
            counts = prd.count_by_state()
            console.print(
                f"  Done: {counts[ItemState.DONE]}, Blocked: {counts[ItemState.BLOCKED]}"
            )
            append_progress(repo_root, "=== No more items to process ===")
            return 0 if counts[ItemState.BLOCKED] == 0 else 1

        iteration += 1
        console.print(f"\n[bold]Iteration {iteration}/{max_iterations}[/bold]")
        console.print(f"  Item: [cyan]{item.id}[/cyan] - {item.title}")
        if item.description:
            console.print(f"\n[bold]Description[/bold]\n{item.description.strip()}")
        if item.acceptance_criteria:
            console.print("\n[bold]Acceptance Criteria[/bold]")
            for criterion in item.acceptance_criteria:
                console.print(f"- {criterion}")
        if item.files_hint:
            console.print("\n[bold]Files[/bold]")
            for f in item.files_hint:
                console.print(f"- {f}")
        verify_commands_preview = item.verify or prd.global_config.verify
        if verify_commands_preview:
            console.print("\n[bold]Verify[/bold]")
            for cmd in verify_commands_preview:
                console.print(f"- {cmd}")

        if is_resuming:
            console.print(
                f"  [yellow]Resuming[/yellow] (attempt {item.status.attempts})"
            )
            append_progress(
                repo_root,
                f"Resuming item {item.id} (attempt {item.status.attempts}): {item.title}",
            )
        else:
            # Mark as doing and increment attempts (crash-safe write)
            item.status.state = ItemState.DOING
            item.status.attempts += 1
            save_prd(repo_root, prd)
            console.print(f"  Attempt: {item.status.attempts}")
            append_progress(
                repo_root,
                f"Starting item {item.id} (attempt {item.status.attempts}): {item.title}",
            )

        # Build prompt and run agent
        prompt = build_agent_prompt(item, prd, repo_root)

        console.print("\n[bold]Running agent...[/bold]")
        agent_success, agent_error = run_agent(repo_root, prompt, model)

        if not agent_success:
            console.print(f"[red]Agent failed: {agent_error}[/red]")
            failures += 1

            # Handle failure
            item.status.last_error = f"Agent error: {agent_error[:500]}"
            if item.status.attempts >= BLOCKED_THRESHOLD:
                item.status.state = ItemState.BLOCKED
                console.print(
                    f"[yellow]Item {item.id} marked as BLOCKED after {item.status.attempts} attempts[/yellow]"
                )
            else:
                item.status.state = ItemState.TODO

            save_prd(repo_root, prd)
            append_progress(
                repo_root, f"Item {item.id} failed (agent error): {agent_error[:200]}"
            )
            continue

        # Run verification
        console.print("\n[bold]Running verification...[/bold]")
        verify_commands = item.verify or prd.global_config.verify

        if not verify_commands:
            console.print("[yellow]No verification commands configured[/yellow]")
            verify_passed = True
            verify_results = []
        else:
            verify_passed, verify_results = run_verification(repo_root, verify_commands)

        if verify_passed:
            console.print("[green]Verification passed![/green]")

            # Mark as done
            item.status.state = ItemState.DONE
            item.status.done_at = datetime.now(timezone.utc)
            item.status.last_error = None
            save_prd(repo_root, prd)

            append_progress(repo_root, f"Item {item.id} completed successfully")

            # Commit if enabled
            if not no_commit:
                console.print("Committing changes...")
                commit_msg = f"ralph: Complete {item.id} - {item.title}"
                if git_commit(repo_root, commit_msg):
                    console.print(f"[green]Committed: {commit_msg}[/green]")
                else:
                    console.print(
                        "[yellow]Warning: Commit failed, continuing...[/yellow]"
                    )
        else:
            console.print("[red]Verification failed![/red]")
            failures += 1

            # Build error summary from verification results
            failed_cmds = [r for r in verify_results if not r["passed"]]
            error_summary = "; ".join(
                f"{r['command']}: {r['stderr'][:100]}" for r in failed_cmds[:3]
            )

            item.status.last_error = f"Verification failed: {error_summary[:500]}"
            if item.status.attempts >= BLOCKED_THRESHOLD:
                item.status.state = ItemState.BLOCKED
                console.print(
                    f"[yellow]Item {item.id} marked as BLOCKED after {item.status.attempts} attempts[/yellow]"
                )
            else:
                item.status.state = ItemState.TODO

            save_prd(repo_root, prd)
            append_progress(
                repo_root, f"Item {item.id} failed verification: {error_summary[:200]}"
            )

    # Loop ended due to limits
    if failures >= max_failures:
        console.print(f"\n[red]Stopped: Max failures ({max_failures}) reached[/red]")
        append_progress(
            repo_root, f"=== Stopped: Max failures ({max_failures}) reached ==="
        )
        return 1

    if iteration >= max_iterations:
        console.print(
            f"\n[yellow]Stopped: Max iterations ({max_iterations}) reached[/yellow]"
        )
        append_progress(
            repo_root, f"=== Stopped: Max iterations ({max_iterations}) reached ==="
        )
        return 1

    return 0
