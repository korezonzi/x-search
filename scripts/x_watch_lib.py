#!/usr/bin/env python3
"""x_watch_lib.py — shared helpers for the X watch pipeline scripts.

Sibling-imported (not a package) by x-watch-slack.py, x-watch-filter.py, and
x-bookmarks.py: all three live in this same scripts/ directory, so Python's
default sys.path[0] (the directory of the script being run) makes
`import x_watch_lib` resolve without any package setup, provided each script
stays a directly-invoked top-level script (not imported from elsewhere).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
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


# --- Shared config & vault helpers (moved here from x-watch-slack.py so
# x-bookmarks.py can reuse them without duplicating the implementation) ---

CONFIG_PATH = Path.home() / ".config" / "x-watch" / "env"


def load_config() -> dict[str, str]:
    """Read KEY=VALUE lines from CONFIG_PATH; empty dict if unavailable."""
    config: dict[str, str] = {}
    try:
        for line in CONFIG_PATH.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def copy_to_vault(digest_path: Path, config: dict[str, str]) -> str | None:
    """Copy the digest into the Obsidian vault; return an obsidian:// URI or None."""
    vault_dir = config.get("VAULT_DIR")
    vault_name = config.get("VAULT_NAME")
    subdir = config.get("VAULT_SUBDIR", "00-inbox/x-watch")
    if not vault_dir or not vault_name:
        return None
    try:
        dest_dir = Path(vault_dir) / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / digest_path.name
        shutil.copyfile(digest_path, dest)
    except OSError as exc:
        print(f"vault copy failed: {exc}", file=sys.stderr)
        return None
    note = f"{subdir}/{digest_path.stem}"
    return (
        "obsidian://open?vault="
        + urllib.parse.quote(vault_name)
        + "&file="
        + urllib.parse.quote(note)
    )


# --- Atomic JSON persistence (same implementation as xq.py's _atomic_write_json) ---


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to `path` atomically via a temp file + os.replace.

    Prevents a crash or concurrent run mid-write from leaving a truncated
    or corrupted state file behind. Same implementation as xq.py's
    _atomic_write_json — kept as an independent copy rather than an import,
    since xq.py is a standalone CLI entrypoint, not a package this shared
    module should depend on.
    """
    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
