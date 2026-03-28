"""Phase 2: Score and classify collected tweets/articles."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import (
    CACHE_DIR,
    FROZEN_DECISIONS,
    OUTPUT_DIR,
    SCORE_APPLY,
    SCORE_HOLD,
    TAG_TO_TARGET,
    ENGAGEMENT_NORMALIZATION_LIKES,
    ENGAGEMENT_NORMALIZATION_RT,
    ENGAGEMENT_NORMALIZATION_BM,
    ENGAGEMENT_NORMALIZATION_VIEWS,
    FRESHNESS_MAX_DAYS,
)
from models import EvalResult, TweetRecord

# Keywords indicating concrete, actionable content
SPECIFICITY_PATTERNS = [
    r"```",            # code block
    r"\.md\b",         # markdown file reference
    r"\.py\b",         # python file
    r"\$\s+\w+",       # shell command
    r"~/\.claude",     # claude config path
    r"CLAUDE\.md",
    r"SKILL\.md",
    r"PostToolUse|PreToolUse",
    r"--\w+",          # CLI flag
    r"\d+%",           # percentage
    r"import \w+",
]


def score_engagement(rec: TweetRecord) -> float:
    """Normalize engagement signals to 0–1."""
    raw = (
        rec.likes / ENGAGEMENT_NORMALIZATION_LIKES
        + rec.retweets * 2 / ENGAGEMENT_NORMALIZATION_RT
        + rec.bookmarks * 3 / ENGAGEMENT_NORMALIZATION_BM
        + rec.views / ENGAGEMENT_NORMALIZATION_VIEWS
    )
    return min(raw / 4.0, 1.0)


def score_specificity(text: str) -> float:
    """Count concrete code/path indicators in text."""
    hits = sum(1 for p in SPECIFICITY_PATTERNS if re.search(p, text))
    return min(hits / 5.0, 1.0)


def score_alignment(tag: str, text: str) -> float:
    """Check whether content aligns with the query tag's theme."""
    tag_keywords: dict[str, list[str]] = {
        "cc_hooks": ["hook", "PostToolUse", "PreToolUse", "event"],
        "cc_skills": ["skill", "SKILL.md", "slash command", "/"],
        "cc_prompt": ["CLAUDE.md", "system prompt", "rules", "instruction"],
        "cc_agents": ["subagent", "agent", "orchestrat", "parallel", "multi-agent"],
        "cc_cost": ["cost", "token", "context window", "model", "haiku", "sonnet"],
        "cc_article_en": ["Claude Code"],
        "cc_article_ja": ["Claude Code"],
        "cc_general_ja": ["設定", "コツ", "Tips", "効率", "使い方", "Claude Code"],
    }
    keywords = tag_keywords.get(tag, ["Claude Code"])
    hits = sum(1 for kw in keywords if kw.lower() in text.lower())
    return min(hits / max(len(keywords), 1), 1.0)


def score_novelty(text: str) -> float:
    """Return higher score if content is not already captured in ~/.claude."""
    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return 1.0

    # Collect a sample of existing text
    existing_texts: list[str] = []
    for md_file in list(claude_dir.rglob("*.md"))[:30]:
        try:
            existing_texts.append(md_file.read_text(errors="ignore"))
        except OSError:
            continue

    combined_existing = " ".join(existing_texts).lower()

    # Extract keywords (words ≥6 chars, no common words)
    stopwords = {"should", "always", "never", "where", "which", "their", "there"}
    words = [w for w in re.findall(r"\b[a-z]{6,}\b", text.lower()) if w not in stopwords]
    if not words:
        return 0.5

    novel_words = [w for w in words if w not in combined_existing]
    return min(len(novel_words) / max(len(words), 1), 1.0)


def score_freshness(created_at: str) -> float:
    """1.0 for today, 0.0 for content older than FRESHNESS_MAX_DAYS."""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    age_days = (datetime.now(timezone.utc) - dt).days
    return max(0.0, 1.0 - age_days / FRESHNESS_MAX_DAYS)


def has_frozen_conflict(text: str) -> bool:
    """Return True if text appears to conflict with settled decisions."""
    text_lower = text.lower()
    return any(fd.lower() in text_lower for fd in FROZEN_DECISIONS)


def build_summary(rec: TweetRecord) -> str:
    """Generate a one-line summary from the tweet text."""
    first_line = rec.full_text.split("\n")[0][:200].strip()
    return first_line if first_line else rec.text[:200]


def build_diff_proposal(rec: TweetRecord, target: str) -> str:
    """Build a rough diff proposal string."""
    snippet = rec.full_text[:400].strip().replace("\n", "\n+ ")
    return f"--- {target}\n+++ {target}\n@@ ... @@\n+ {snippet}"


def evaluate_record(rec: TweetRecord) -> EvalResult:
    """Score a single TweetRecord and return EvalResult."""
    text = rec.full_text or rec.text

    e = score_engagement(rec)
    sp = score_specificity(text)
    al = score_alignment(rec.tag, text)
    nv = score_novelty(text)
    fr = score_freshness(rec.created_at)

    total = e * 0.20 + sp * 0.30 + al * 0.25 + nv * 0.15 + fr * 0.10

    frozen = has_frozen_conflict(text)

    if total >= SCORE_APPLY and not frozen:
        verdict = "apply"
    elif total >= SCORE_HOLD and not frozen:
        verdict = "hold"
    else:
        verdict = "discard"

    target = TAG_TO_TARGET.get(rec.tag, "~/.claude/knowledge/claude-code-best-practices.md")

    return EvalResult(
        tweet_id=rec.tweet_id,
        tag=rec.tag,
        title=build_summary(rec)[:80],
        url=rec.url,
        score_engagement=round(e, 3),
        score_specificity=round(sp, 3),
        score_alignment=round(al, 3),
        score_novelty=round(nv, 3),
        score_freshness=round(fr, 3),
        total_score=round(total, 3),
        verdict=verdict,
        target_file=target,
        summary=build_summary(rec),
        diff_proposal=build_diff_proposal(rec, target),
        likes=rec.likes,
        bookmarks=rec.bookmarks,
        views=rec.views,
        frozen_conflict=frozen,
        full_text=rec.full_text,
    )


def load_records_from_cache(date_str: str | None = None) -> list[TweetRecord]:
    """Load TweetRecords from cache JSONL files."""
    records: list[TweetRecord] = []

    if date_str:
        files = [CACHE_DIR / f"{date_str}.jsonl"]
    else:
        files = sorted(CACHE_DIR.glob("*.jsonl"))

    for jsonl_file in files:
        if not jsonl_file.exists():
            print(f"[warn] cache file not found: {jsonl_file}", file=sys.stderr)
            continue
        for line in jsonl_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                records.append(TweetRecord(**data))
            except (json.JSONDecodeError, TypeError) as e:
                print(f"[warn] failed to parse line: {e}", file=sys.stderr)

    return records


def main(date_str: str | None = None) -> None:
    records = load_records_from_cache(date_str)
    if not records:
        print("No records found in cache. Run search.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Evaluating {len(records)} records...")

    results: list[EvalResult] = [evaluate_record(r) for r in records]

    output_file = OUTPUT_DIR / "evaluated.jsonl"
    with output_file.open("w") as f:
        for r in results:
            f.write(json.dumps(r.__dict__) + "\n")

    apply_count = sum(1 for r in results if r.verdict == "apply")
    hold_count = sum(1 for r in results if r.verdict == "hold")
    discard_count = sum(1 for r in results if r.verdict == "discard")

    print(f"Results: apply={apply_count}, hold={hold_count}, discard={discard_count}")
    print(f"Saved → {output_file}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score and classify collected X posts")
    parser.add_argument("--date", type=str, help="YYYY-MM-DD (default: all cached files)")
    args = parser.parse_args()
    main(args.date)
