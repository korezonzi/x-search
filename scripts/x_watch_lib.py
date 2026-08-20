#!/usr/bin/env python3
"""x_watch_lib.py — shared helpers for the X watch pipeline scripts.

Sibling-imported (not a package) by x-watch-slack.py and x-watch-filter.py:
both live in this same scripts/ directory, so Python's default sys.path[0]
(the directory of the script being run) makes `import x_watch_lib` resolve
without any package setup, provided each script stays a directly-invoked
top-level script (not imported from elsewhere).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

# Matches both the legacy digest header ("... ♥N RTN") and the new one
# xq.py's format_md_entry emits, which appends optional bookmark/reply/score
# fields ("... 🔖N 💬N ⚡N.N") — so digests written before this change still
# parse correctly (the new groups simply come back as None).
ENTRY_RE = re.compile(r"^\d+\. (@\w+) \(([^)]+)\) ♥(\d+) RT(\d+)(?: 🔖(\d+))?(?: 💬(\d+))?(?: ⚡([\d.]+))?$")

CLAUDE_CANDIDATES = (
    Path.home() / ".local" / "bin" / "claude",
    Path("/usr/local/bin/claude"),
    Path("/opt/homebrew/bin/claude"),
)

DEFAULT_CLAUDE_MODEL = "haiku"


def find_claude() -> str | None:
    """Locate the claude CLI even under launchd's minimal PATH."""
    for cand in CLAUDE_CANDIDATES:
        if cand.is_file():
            return str(cand)
    return shutil.which("claude")


def run_claude_prompt(prompt: str, timeout_seconds: int, model: str = DEFAULT_CLAUDE_MODEL) -> str | None:
    """Run `claude -p <prompt> --model <model>` and return its stdout.

    Returns None on any failure — claude CLI not found, subprocess error,
    non-zero exit, or timeout — after printing a stderr warning. Callers in
    this pipeline treat None as "fall back" rather than crash: this is the
    shared fail-open primitive behind both the Slack translator and the LLM
    relevance filter.
    """
    claude_bin = find_claude()
    if not claude_bin:
        print("WARNING: claude CLI not found; skipping LLM call.", file=sys.stderr)
        return None
    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        # Never str(exc): CalledProcessError/TimeoutExpired embed the full
        # argv (including the prompt text) in their message.
        detail = f" (returncode={exc.returncode})" if isinstance(exc, subprocess.CalledProcessError) else ""
        print(f"WARNING: claude -p call failed: {type(exc).__name__}{detail}", file=sys.stderr)
        return None
    return result.stdout
