"""
AI Development Agent — CLI

Usage examples:

  # Interactive mode (prompts for each field):
  python cli.py

  # Direct mode (all fields as arguments):
  python cli.py \\
    --task-id TASK-123 \\
    --title "Add email validation to user registration API" \\
    --description "Repository: https://github.com/org/repo\\nBranch: main\\n..."

  # From a JSON file:
  python cli.py --file task.json

  # Wait for result and print the execution report:
  python cli.py --task-id TASK-123 --title "..." --description "..." --wait

The CLI submits the task to the running FastAPI server (POST /api/tasks)
and optionally polls GET /api/tasks/{traceId}/report until completion.

Server URL defaults to http://localhost:8000 and can be overridden
with --server or the AI_AGENT_SERVER environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx is not installed. Run: pip install httpx")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.json import JSON
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

DEFAULT_SERVER = os.getenv("AI_AGENT_SERVER", "http://localhost:8000")
POLL_INTERVAL  = 3    # seconds between status polls
MAX_WAIT       = 300  # max seconds to wait for pipeline completion

console = Console() if HAS_RICH else None


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def _print(msg: str, style: str = "") -> None:
    if HAS_RICH and console:
        console.print(msg, style=style)
    else:
        print(msg)


def _print_report(report: dict) -> None:
    status = report.get("status", "unknown")
    pr     = report.get("pullRequest")
    test   = report.get("testResult")

    color = "green" if status == "success" else ("yellow" if status == "partial" else "red")

    _print(f"\n[bold {color}]Pipeline status: {status.upper()}[/bold {color}]" if HAS_RICH
           else f"\nPipeline status: {status.upper()}")

    if pr:
        _print(f"[bold]Pull Request:[/bold] {pr['url']}" if HAS_RICH
               else f"Pull Request: {pr['url']}")

    if test:
        icon = "✅" if test["status"] == "passed" else "❌"
        _print(f"[bold]Tests:[/bold] {icon} {test['status'].upper()} ({test['durationSeconds']}s)"
               if HAS_RICH else f"Tests: {icon} {test['status'].upper()} ({test['durationSeconds']}s)")

    if report.get("error"):
        _print(f"[bold red]Error:[/bold red] {report['error']}" if HAS_RICH
               else f"Error: {report['error']}")

    _print("\n[dim]Full report:[/dim]" if HAS_RICH else "\nFull report:")
    if HAS_RICH and console:
        console.print(JSON(json.dumps(report, indent=2, default=str)))
    else:
        print(json.dumps(report, indent=2, default=str))


# ─────────────────────────────────────────────────────────────────────────────
# Interactive input
# ─────────────────────────────────────────────────────────────────────────────

def _prompt(label: str, required: bool = True) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value or not required:
            return value
        print(f"  ⚠  {label} is required.")


def _interactive_input() -> dict:
    _print("\n[bold cyan]AI Development Agent — Interactive Mode[/bold cyan]\n"
           if HAS_RICH else "\nAI Development Agent — Interactive Mode\n")
    _print("Enter task details (or press Ctrl+C to cancel):\n")

    task_id     = _prompt("Task ID (e.g. TASK-123)")
    title       = _prompt("Title")

    _print("\nDescription (paste multi-line, end with a single '.' on its own line):")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == ".":
            break
        lines.append(line)
    description = "\n".join(lines)

    return {"taskId": task_id, "title": title, "description": description}


# ─────────────────────────────────────────────────────────────────────────────
# API calls
# ─────────────────────────────────────────────────────────────────────────────

def _submit_task(server: str, payload: dict) -> str:
    """POST /api/tasks → returns traceId."""
    url = f"{server}/api/tasks"
    try:
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["traceId"]
    except httpx.ConnectError:
        _print(f"[bold red]ERROR:[/bold red] Cannot connect to server at {server}.\n"
               f"Is the agent running? Start it with: python app/main.py"
               if HAS_RICH else f"ERROR: Cannot connect to {server}")
        sys.exit(1)
    except httpx.HTTPStatusError as exc:
        _print(f"[bold red]ERROR:[/bold red] Server returned {exc.response.status_code}: "
               f"{exc.response.text}" if HAS_RICH
               else f"ERROR: {exc.response.status_code}: {exc.response.text}")
        sys.exit(1)


def _poll_report(server: str, trace_id: str) -> dict:
    """Poll GET /api/tasks/{traceId}/report until complete."""
    url = f"{server}/api/tasks/{trace_id}/report"
    elapsed = 0

    if HAS_RICH and console:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Pipeline running...", total=None)
            while elapsed < MAX_WAIT:
                time.sleep(POLL_INTERVAL)
                elapsed += POLL_INTERVAL
                try:
                    resp = httpx.get(url, timeout=10)
                    if resp.status_code == 200:
                        return resp.json()
                    progress.update(task, description=f"Pipeline running... ({elapsed}s)")
                except Exception:
                    pass
    else:
        while elapsed < MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            try:
                resp = httpx.get(url, timeout=10)
                if resp.status_code == 200:
                    return resp.json()
                print(f"  Still running... ({elapsed}s)")
            except Exception:
                pass

    _print("[bold red]Timeout:[/bold red] Pipeline did not complete within "
           f"{MAX_WAIT}s. Check server logs." if HAS_RICH
           else f"Timeout: Pipeline did not complete within {MAX_WAIT}s.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python cli.py",
        description="Submit a task to the AI Development Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli.py                          # interactive mode
  python cli.py --file task.json         # from JSON file
  python cli.py --task-id TASK-1 \\
    --title "Add validation" \\
    --description "Repository: ..." \\
    --wait                               # submit and wait for report
        """,
    )
    parser.add_argument("--task-id",     metavar="ID",    help="Task ID (e.g. TASK-123)")
    parser.add_argument("--title",       metavar="TEXT",  help="Task title")
    parser.add_argument("--description", metavar="TEXT",  help="Task description (use \\n for newlines)")
    parser.add_argument("--file",        metavar="PATH",  help="Path to a JSON file containing the task payload")
    parser.add_argument("--server",      metavar="URL",   default=DEFAULT_SERVER,
                        help=f"Agent server URL (default: {DEFAULT_SERVER})")
    parser.add_argument("--wait",        action="store_true",
                        help="Wait for pipeline to finish and print the execution report")
    parser.add_argument("--timeout",     type=int, default=MAX_WAIT, metavar="SECS",
                        help=f"Max seconds to wait (default: {MAX_WAIT}, only with --wait)")
    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()

    # ── Build payload ─────────────────────────────────────────────────────
    if args.file:
        path = Path(args.file)
        if not path.exists():
            _print(f"[bold red]ERROR:[/bold red] File not found: {args.file}"
                   if HAS_RICH else f"ERROR: File not found: {args.file}")
            sys.exit(1)
        payload = json.loads(path.read_text())

    elif args.task_id and args.title and args.description:
        payload = {
            "taskId":      args.task_id,
            "title":       args.title,
            "description": args.description.replace("\\n", "\n"),
        }

    else:
        # Interactive mode
        try:
            payload = _interactive_input()
        except KeyboardInterrupt:
            _print("\nCancelled.")
            sys.exit(0)

    # ── Submit ────────────────────────────────────────────────────────────
    _print(f"\n[bold]Submitting task[/bold] [cyan]{payload.get('taskId')}[/cyan] "
           f"to [dim]{args.server}[/dim]..." if HAS_RICH
           else f"\nSubmitting task {payload.get('taskId')} to {args.server}...")

    trace_id = _submit_task(args.server, payload)

    if HAS_RICH and console:
        console.print(Panel(
            f"[bold green]✅ Accepted[/bold green]\n"
            f"Trace ID : [cyan]{trace_id}[/cyan]\n"
            f"Task ID  : [cyan]{payload.get('taskId')}[/cyan]\n\n"
            f"Poll status:\n"
            f"  [dim]curl {args.server}/api/tasks/{trace_id}/report[/dim]",
            title="Task Submitted",
            expand=False,
        ))
    else:
        print(f"\n✅ Task accepted")
        print(f"   Trace ID : {trace_id}")
        print(f"   Poll     : GET {args.server}/api/tasks/{trace_id}/report")

    # ── Wait for result ───────────────────────────────────────────────────
    if args.wait:
        global MAX_WAIT
        MAX_WAIT = args.timeout
        _print("\n[dim]Waiting for pipeline to complete...[/dim]" if HAS_RICH
               else "\nWaiting for pipeline to complete...")
        report = _poll_report(args.server, trace_id)
        _print_report(report)


if __name__ == "__main__":
    main()