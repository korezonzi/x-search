"""Phase 2: Score and classify collected tweets/articles."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import (
    CACHE_DIR,
    CLAUDE_MD_SCORE_PENALTY,
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
from models import ContentType, EvalResult, TweetRecord

# Keywords indicating concrete, actionable content
SPECIFICITY_PATTERNS = [
    r"```",                                          # triple backtick code block
    r"`[^`\n]+`",                                    # inline code
    r"now supports|introducing|new feature|just shipped",  # feature announcements
    r"\.md\b",                                       # markdown file reference
    r"\.py\b",                                       # python file
    r"\$\s+\w+",                                     # shell command
    r"~/\.claude",                                   # claude config path
    r"CLAUDE\.md",
    r"SKILL\.md",
    r"PostToolUse|PreToolUse",
    r"--\w+",                                        # CLI flag
    r"\d+%",                                         # percentage
    r"import \w+",
]

# Patterns indicating new feature announcements (override novelty to high)
ANNOUNCEMENT_PATTERNS = [
    r"now supports",
    r"just shipped",
    r"introducing",
    r"new feature",
    r"we've added",
    r"released today",
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
    return min(hits / 3.0, 1.0)


def score_alignment(tag: str, text: str) -> float:
    """Check whether content aligns with the query tag's theme.

    Uses min(hits / 2.0, 1.0) so a single strong keyword match gives 0.5
    and two matches give a perfect 1.0.
    """
    tag_keywords: dict[str, list[str]] = {
        "cc_hooks": ["hook", "PostToolUse", "PreToolUse", "event"],
        "cc_skills": ["skill", "SKILL.md", "slash command", "/"],
        "cc_prompt": ["CLAUDE.md", "system prompt", "rules", "instruction"],
        "cc_agents": ["subagent", "agent", "orchestrat", "parallel", "multi-agent"],
        "cc_cost": ["cost", "token", "context window", "model", "haiku", "sonnet"],
        "cc_article_en": ["Claude Code"],
        "cc_article_ja": ["Claude Code"],
        "cc_general_ja": ["設定", "コツ", "Tips", "効率", "使い方", "Claude Code"],
        "cc_updates": ["Claude Code", "supports", "shipped", "feature", "introducing"],
        "cc_official": ["Claude Code"],
        "cc_hooks_ja": ["hook", "フック", "自動化", "PostToolUse", "PreToolUse"],
    }
    keywords = tag_keywords.get(tag, ["Claude Code"])
    hits = sum(1 for kw in keywords if kw.lower() in text.lower())
    return min(hits / 2.0, 1.0)


def score_novelty(text: str, target: str = "") -> float:
    """Return higher score if content is not already captured in the target file.

    Feature announcements always return 0.8 (new info by definition).
    Otherwise compares against the specific target file only (not all of ~/.claude/).
    """
    # New feature announcements are always novel
    if any(re.search(p, text, re.I) for p in ANNOUNCEMENT_PATTERNS):
        return 0.8

    # Compare against target file only (not entire ~/.claude/ or Obsidian vault)
    if target:
        target_path = Path(target.replace("~", str(Path.home())))
        if target_path.exists() and not target_path.is_dir():
            try:
                existing = target_path.read_text(errors="ignore").lower()
            except OSError:
                return 0.5
        else:
            return 0.5
    else:
        return 0.5

    # Extract meaningful keywords (words ≥6 chars, no common words)
    stopwords = {"should", "always", "never", "where", "which", "their", "there",
                 "claude", "hooks", "agent", "model", "skill", "rules"}
    words = [w for w in re.findall(r"\b[a-z]{6,}\b", text.lower()) if w not in stopwords]
    if not words:
        return 0.5

    novel_words = [w for w in words if w not in existing]
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


# ---------------------------------------------------------------------------
# Governance-aware classification
# ---------------------------------------------------------------------------

# Keyword patterns per ContentType (ordered: more specific first)
_DOMAIN_KEYWORDS: list[tuple[ContentType, list[str]]] = [
    ("domain_hooks", ["PostToolUse", "PreToolUse", "hook", "exit code", "hookSpecificOutput"]),
    ("domain_testing", ["TDD", "pytest", "test coverage", "unit test", "integration test", "mock"]),
    ("domain_security", ["OWASP", "secret", "injection", "XSS", "CSRF", "SQL inject", "authentication"]),
    ("domain_agents", ["subagent", "multi-agent", "orchestrat", "parallel agent", "spawn"]),
    ("domain_performance", ["context window", "token budget", "model selection", "haiku", "cost optim"]),
    ("domain_git", ["git commit", "pull request", "branch strategy", "conventional commit"]),
    ("domain_workflow", ["workflow", "feature pipeline", "research phase", "plan mode"]),
    ("domain_coding", ["immutab", "single responsibility", "pure function", "error handling pattern"]),
]

# Tag signals that strongly suggest a particular content type
_TAG_TYPE_HINTS: dict[str, ContentType] = {
    "cc_hooks": "domain_hooks",
    "cc_agents": "domain_agents",
    "cc_cost": "domain_performance",
    "cc_skills": "workflow",
    "cc_article_en": "general_knowledge",
    "cc_article_ja": "general_knowledge",
    "cc_general_ja": "general_knowledge",
    "cc_prompt": "behavioral_rule",
}

# Section name per ContentType
_CONTENT_TYPE_SECTION: dict[ContentType, str] = {
    "behavioral_rule": "",  # determined dynamically by content
    "domain_hooks": "## Hook Types",
    "domain_testing": "## Test-Driven Development",
    "domain_security": "## Mandatory Security Checks",
    "domain_agents": "## Parallel Task Execution",
    "domain_performance": "## Model Selection Strategy",
    "domain_git": "## Commit Message Format",
    "domain_workflow": "## Feature Implementation Workflow",
    "domain_coding": "## Error Handling",
    "workflow": "",
    "general_knowledge": "",
    "discard": "",
}

# Target file per ContentType
_CONTENT_TYPE_TARGET: dict[ContentType, str] = {
    "behavioral_rule": "~/.claude/CLAUDE.md",
    "domain_hooks": "~/.claude/rules/common/hooks.md",
    "domain_testing": "~/.claude/rules/common/testing.md",
    "domain_security": "~/.claude/rules/common/security.md",
    "domain_agents": "~/.claude/rules/common/agents.md",
    "domain_performance": "~/.claude/rules/common/performance.md",
    "domain_git": "~/.claude/rules/common/git-workflow.md",
    "domain_workflow": "~/.claude/rules/common/development-workflow.md",
    "domain_coding": "~/.claude/rules/common/coding-style.md",
    "workflow": "~/.claude/skills/",
    "general_knowledge": "~/.claude/knowledge/claude-code-best-practices.md",
    "discard": "",
}


def classify_content(text: str, tag: str) -> ContentType:
    """Classify tweet content into a ContentType for governance-aware routing."""
    text_lower = text.lower()

    # Multi-step workflow pattern (numbered phases / steps)
    step_count = len(re.findall(r"\b(?:step|phase|stage)\s*\d+|^\d+\.", text_lower, re.MULTILINE))
    if step_count >= 2:
        return "workflow"

    # Domain keyword matching (specific domains first)
    for content_type, keywords in _DOMAIN_KEYWORDS:
        if any(kw.lower() in text_lower for kw in keywords):
            return content_type

    # Tag-based hint for remaining ambiguous content
    tag_hint = _TAG_TYPE_HINTS.get(tag)
    if tag_hint:
        return tag_hint

    # Foreign language (non-CJK, non-ASCII Latin keywords) → discard
    non_latin_ratio = len(re.findall(r"[^\x00-\x7F\u3000-\u9FFF\uFF00-\uFFEF]", text)) / max(len(text), 1)
    if non_latin_ratio > 0.2:
        return "discard"

    return "general_knowledge"


def determine_target_and_section(content_type: ContentType, tag: str, text: str) -> tuple[str, str]:
    """Return (target_file_path, section_name) for the given content type."""
    target = _CONTENT_TYPE_TARGET.get(content_type, "~/.claude/knowledge/claude-code-best-practices.md")
    section = _CONTENT_TYPE_SECTION.get(content_type, "")

    # behavioral_rule: map to the most relevant CLAUDE.md section
    if content_type == "behavioral_rule":
        text_lower = text.lower()
        if any(w in text_lower for w in ["model", "sonnet", "opus", "haiku", "cost"]):
            section = "## モデル切り替えの推奨"
        elif any(w in text_lower for w in ["git", "commit", "branch"]):
            section = "## Git運用"
        elif any(w in text_lower for w in ["security", "secret", "env"]):
            section = "## 禁止事項"
        elif any(w in text_lower for w in ["workflow", "plan", "phase"]):
            section = "## 作業フロー"
        else:
            section = "## コーディング規約"

    return target, section


def build_governance_rationale(content_type: ContentType, target: str, section: str) -> str:
    """Explain why this content was routed to the given target."""
    type_reason: dict[ContentType, str] = {
        "behavioral_rule": "全プロジェクト共通の行動原則（高閾値スコアペナルティ適用）",
        "domain_hooks": "フックパターン（PostToolUse/PreToolUse）→ ドメイン固有ルール",
        "domain_testing": "テスト戦略（TDD/カバレッジ）→ ドメイン固有ルール",
        "domain_security": "セキュリティ要件（OWASP/シークレット）→ ドメイン固有ルール",
        "domain_agents": "マルチエージェント設計→ ドメイン固有ルール",
        "domain_performance": "モデル選択・コスト最適化→ ドメイン固有ルール",
        "domain_git": "Git運用パターン→ ドメイン固有ルール",
        "domain_workflow": "開発ワークフロー→ ドメイン固有ルール",
        "domain_coding": "コーディングスタイル→ ドメイン固有ルール",
        "workflow": "複数ステップの再利用可能な手順→ スキルとして作成",
        "general_knowledge": "Claude Code 参考情報・ヒント→ ナレッジベース",
        "discard": "廃棄基準に該当",
    }
    reason = type_reason.get(content_type, "分類不明")
    section_note = f" § {section}" if section else ""
    return f"ContentType={content_type}: {reason} → {target}{section_note}"


def extract_actionable_insight(text: str) -> str:
    """Extract the most concrete, actionable 1-3 sentences from tweet text.

    Avoids returning the raw tweet verbatim — picks the lines that contain
    concrete patterns (code blocks, paths, CLI flags, percentages).
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return text[:200]

    # Score each line by concreteness
    def line_score(ln: str) -> int:
        score = 0
        if re.search(r"```|`[^`]+`", ln):
            score += 3
        if re.search(r"~/\.claude|CLAUDE\.md|SKILL\.md", ln):
            score += 2
        if re.search(r"\$\s+\w+|--\w+", ln):
            score += 2
        if re.search(r"\d+%|\d+ (lines|tokens|files)", ln):
            score += 1
        if re.search(r"PostToolUse|PreToolUse|subagent|TDD", ln):
            score += 2
        if len(ln) > 20:
            score += 1
        return score

    scored = sorted(enumerate(lines), key=lambda x: -line_score(x[1]))
    top_lines = [lines[i] for i, _ in sorted(scored[:3], key=lambda x: x[0])]
    return " ".join(top_lines)[:400]


def build_diff_proposal(rec: TweetRecord, target: str, section: str, insight: str) -> str:
    """Build a diff proposal against the actual target file section.

    Reads the target file to:
    1. Identify the insertion point within the section
    2. Check for duplicates (returns duplicate notice if found)
    3. Format the diff to match the existing style
    """
    target_path = Path(target.replace("~", str(Path.home())))

    if not target_path.exists() or target_path.is_dir():
        snippet = insight.replace("\n", "\n+ ")
        return f"--- {target}\n+++ {target}\n@@ ... @@\n+ {snippet}"

    try:
        content = target_path.read_text(errors="ignore")
    except OSError:
        snippet = insight.replace("\n", "\n+ ")
        return f"--- {target}\n+++ {target}\n@@ ... @@\n+ {snippet}"

    # Duplicate check: if ≥40% of meaningful words already appear in file
    words = re.findall(r"\b[a-zA-Z]{5,}\b", insight.lower())
    if words:
        content_lower = content.lower()
        overlap = sum(1 for w in words if w in content_lower)
        overlap_ratio = overlap / len(words)
        if overlap_ratio >= 0.4:
            return (
                f"# ⚠️ 重複チェック: 既存内容と {overlap_ratio:.0%} 重複\n"
                f"# 新規追加ではなく、既存行の改善提案を検討してください\n"
                f"--- {target}\n+++ {target}\n@@ 既存内容の改善案 @@\n"
                f"# 元の洞察: {insight[:200]}"
            )

    # Find insertion point within section
    if section and section in content:
        section_start = content.index(section)
        # Find next ## heading after section_start
        next_section = re.search(r"\n##\s", content[section_start + len(section):])
        if next_section:
            insert_at = section_start + len(section) + next_section.start()
            context_before = content[max(0, insert_at - 100):insert_at].rstrip()[-80:]
            context_after = content[insert_at:insert_at + 80].lstrip()[:80]
            snippet_line = f"- {insight[:200]}" if not insight.startswith("-") else insight[:200]
            return (
                f"--- {target}\n+++ {target}\n"
                f"@@ {section} @@\n"
                f"  {context_before}\n"
                f"+ {snippet_line}\n"
                f"  {context_after}"
            )

    # Fallback: append to end of file
    snippet = insight.replace("\n", "\n+ ")
    return f"--- {target}\n+++ {target}\n@@ 末尾へ追加 @@\n+ {snippet}"


def evaluate_record(rec: TweetRecord) -> EvalResult:
    """Score a single TweetRecord and return EvalResult."""
    text = rec.full_text or rec.text

    # Governance-aware classification (needed before novelty check)
    content_type = classify_content(text, rec.tag)
    target, section = determine_target_and_section(content_type, rec.tag, text)

    e = score_engagement(rec)
    sp = score_specificity(text)
    al = score_alignment(rec.tag, text)
    nv = score_novelty(text, target)
    fr = score_freshness(rec.created_at)

    total = e * 0.20 + sp * 0.30 + al * 0.25 + nv * 0.15 + fr * 0.10

    # Apply score penalty for CLAUDE.md (high threshold — must be truly universal)
    if content_type == "behavioral_rule":
        total = total * CLAUDE_MD_SCORE_PENALTY

    frozen = has_frozen_conflict(text)

    if total >= SCORE_APPLY and not frozen and content_type != "discard":
        verdict = "apply"
    elif total >= SCORE_HOLD and not frozen and content_type != "discard":
        verdict = "hold"
    else:
        verdict = "discard"

    insight = extract_actionable_insight(text)
    rationale = build_governance_rationale(content_type, target, section)
    diff = build_diff_proposal(rec, target, section, insight)

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
        diff_proposal=diff,
        likes=rec.likes,
        bookmarks=rec.bookmarks,
        views=rec.views,
        frozen_conflict=frozen,
        full_text=rec.full_text,
        content_type=content_type,
        target_section=section,
        governance_rationale=rationale,
        actionable_insight=insight,
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
