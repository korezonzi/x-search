#!/usr/bin/env python3
"""Post a Slack summary of an xq.py watch digest via incoming webhook.

Usage: x-watch-slack.py <digest.md>

Config: ~/.config/x-watch/env (KEY=VALUE lines)
  SLACK_WEBHOOK_URL_AI_NEWS  required for posting (missing -> skip silently)
  VAULT_DIR                  Obsidian vault root (missing -> no vault copy/link)
  VAULT_NAME                 Obsidian vault name for obsidian:// links
  VAULT_SUBDIR               subdir inside the vault for digest copies

Pipeline: copy digest into the Obsidian vault -> translate top posts to
Japanese via `claude -p` (haiku; falls back to original text on any failure)
-> post summary. Slack must never break the watch run. Never print secrets.

Shared with x-watch-filter.py via x_watch_lib.py (sibling module in this
same scripts/ directory): the digest-entry regex and the `claude -p`
subprocess call.
"""
from __future__ import annotations

import re
import shutil
import sys
import urllib.parse
from pathlib import Path

import requests

import x_watch_lib

CONFIG_PATH = Path.home() / ".config" / "x-watch" / "env"
WEBHOOK_KEY = "SLACK_WEBHOOK_URL_AI_NEWS"
TOP_N = 5
TEXT_HEAD_CHARS = 120
REQUEST_TIMEOUT_SECONDS = 15
TRANSLATE_TIMEOUT_SECONDS = 120

ENTRY_RE = x_watch_lib.ENTRY_RE
SECTION_RE = re.compile(r"^## (.+)$")
URL_RE = re.compile(r"^\s*(https://x\.com/\S+)$")
ENTRY_LIKE_RE = re.compile(r"^\d+\. @", re.M)


def load_config() -> dict[str, str]:
    """Read KEY=VALUE lines from the config file; empty dict if unavailable."""
    config: dict[str, str] = {}
    try:
        for line in CONFIG_PATH.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def parse_digest(md_text: str) -> tuple[list[dict], dict[str, int]]:
    """Extract post entries and per-section new-post counts from digest markdown."""
    entries: list[dict] = []
    section_counts: dict[str, int] = {}
    section = ""
    current: dict | None = None
    for line in md_text.splitlines():
        sec = SECTION_RE.match(line)
        if sec:
            section = sec.group(1).split(" — ")[0].strip()
            continue
        head = ENTRY_RE.match(line)
        if head:
            # Groups 5-7 (bookmarks/replies/score) are only present in
            # digests written after xq.py's format_md_entry header change;
            # older digests leave them None, and downstream code (top-N
            # selection, display line) falls back to favs-only accordingly.
            current = {
                "author": head.group(1),
                "when": head.group(2),
                "favs": int(head.group(3)),
                "bookmarks": int(head.group(5)) if head.group(5) else None,
                "replies": int(head.group(6)) if head.group(6) else None,
                "score": float(head.group(7)) if head.group(7) else None,
                "section": section,
                "text": "",
                "url": "",
            }
            entries.append(current)
            section_counts[section] = section_counts.get(section, 0) + 1
            continue
        if current is not None:
            url = URL_RE.match(line)
            if url:
                current["url"] = url.group(1)
                current = None
            elif line.startswith("   ") and not current["text"]:
                current["text"] = line.strip()[:TEXT_HEAD_CHARS]
    return entries, section_counts


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


def translate_snippets(texts: list[str]) -> list[str]:
    """Translate snippets to Japanese via claude haiku; fall back to originals."""
    if not texts:
        return texts
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "以下はX投稿の抜粋です。各行を自然な日本語1行に翻訳してください。"
        "既に日本語のものはそのまま返してください。"
        "出力は番号つきの翻訳行のみ（説明・前置き禁止）:\n" + numbered
    )
    stdout = x_watch_lib.run_claude_prompt(prompt, TRANSLATE_TIMEOUT_SECONDS)
    if stdout is None:
        return texts
    translated = dict((int(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(\d+)\.[ \t]*(.+)$", stdout, re.M))
    return [translated.get(i + 1, t) for i, t in enumerate(texts)]


def _select_top(entries: list[dict], top_n: int) -> list[dict]:
    """Pick the top `top_n` entries.

    Ranks by composite score when every entry has one (new-format digest);
    otherwise falls back to favs (legacy digest). A single digest file never
    mixes old/new entry formats, so a genuine mid-list fallback should not
    occur in practice — this is a defensive default, not an expected case.
    """
    if entries and all(e.get("score") is not None for e in entries):
        return sorted(entries, key=lambda e: e["score"], reverse=True)[:top_n]
    return sorted(entries, key=lambda e: e["favs"], reverse=True)[:top_n]


def _format_top_line(entry: dict, ja_text: str) -> str:
    """Render one Top-posts bullet: prefixed with ⚡score when available
    (new-format digest), the legacy ♥favs-only line otherwise."""
    if entry.get("score") is not None:
        return f"• ⚡{entry['score']:.1f} ♥{entry['favs']:,} {entry['author']} [{entry['section']}] {ja_text}\n  {entry['url']}"
    return f"• ♥{entry['favs']:,} {entry['author']} [{entry['section']}] {ja_text}\n  {entry['url']}"


def build_message(
    digest_path: Path,
    entries: list[dict],
    counts: dict[str, int],
    obsidian_uri: str | None,
) -> str:
    """Render the Slack mrkdwn summary (top posts translated to Japanese)."""
    date_label = digest_path.stem.replace("watch-", "")
    doc_line = (
        f"📄 <{obsidian_uri}|Obsidianで全文を開く>" if obsidian_uri else f"📄 全文digest: `{digest_path}`"
    )
    if not entries:
        return f"📡 *X Watch {date_label}*\n新着なし\n{doc_line}"
    top = _select_top(entries, TOP_N)
    ja_texts = translate_snippets([e["text"] for e in top])
    lines = [f"📡 *X Watch {date_label}* — 新着 {len(entries)}件", "*Top posts:*"]
    for e, ja in zip(top, ja_texts):
        lines.append(_format_top_line(e, ja))
    breakdown = " / ".join(f"{name} {n}" for name, n in counts.items() if n > 0)
    lines.append(f"*内訳:* {breakdown}")
    lines.append(doc_line)
    return "\n".join(lines)


def post_to_slack(webhook_url: str, text: str) -> None:
    """POST the message; raises on HTTP failure."""
    resp = requests.post(webhook_url, json={"text": text}, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: x-watch-slack.py <digest.md>", file=sys.stderr)
        return 1
    digest_path = Path(sys.argv[1])
    if not digest_path.is_file():
        print(f"digest not found: {digest_path}", file=sys.stderr)
        return 1
    config = load_config()
    obsidian_uri = copy_to_vault(digest_path, config)
    webhook_url = config.get(WEBHOOK_KEY)
    if not webhook_url:
        print("slack webhook not configured; skipping post", file=sys.stderr)
        return 0
    digest_text = digest_path.read_text()
    entries, counts = parse_digest(digest_text)
    if not entries and ENTRY_LIKE_RE.search(digest_text):
        print("WARNING: entry-like lines found but parse_digest matched 0 — possible digest format drift", file=sys.stderr)
    message = build_message(digest_path, entries, counts, obsidian_uri)
    try:
        post_to_slack(webhook_url, message)
    except Exception as exc:  # noqa: BLE001 — Slack must never break the watch run
        # Never str(exc): requests embeds the request URL (which carries the
        # webhook secret) in its exception messages.
        status = getattr(getattr(exc, "response", None), "status_code", "n/a")
        print(f"slack post failed: {type(exc).__name__} (status={status})", file=sys.stderr)
        return 0
    print(f"slack summary posted ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
