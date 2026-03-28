"""Phase 3: Generate Markdown report from evaluated results."""

import json
import sys
from datetime import date
from pathlib import Path

from config import OUTPUT_DIR
from models import EvalResult

COST_PER_CALL = 0.0002  # USD per SocialData API call
JPY_PER_USD = 150


def load_results(input_file: Path) -> list[EvalResult]:
    """Load EvalResult records from JSONL."""
    results: list[EvalResult] = []
    if not input_file.exists():
        print(f"[error] {input_file} not found. Run evaluate.py first.", file=sys.stderr)
        sys.exit(1)

    for line in input_file.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            results.append(EvalResult(**data))
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[warn] parse error: {e}", file=sys.stderr)
    return results


def format_engagement(r: EvalResult) -> str:
    return f"いいね{r.likes:,} / BM {r.bookmarks:,} / 閲覧 {r.views:,}"


def render_apply_section(items: list[EvalResult]) -> str:
    if not items:
        return "_なし_\n"

    lines: list[str] = []
    for i, r in enumerate(items, 1):
        lines.append(f"### [{i}] {r.title}")
        lines.append(f"- スコア: **{r.total_score:.2f}** | タグ: `{r.tag}`")
        lines.append(f"- 適用先: `{r.target_file}`")
        lines.append(f"- 元投稿: {r.url}")
        lines.append(f"- エンゲージメント: {format_engagement(r)}")
        lines.append(f"- 要約: {r.summary[:200]}")
        lines.append("")
        lines.append("**変更提案:**")
        lines.append("")
        lines.append("```diff")
        lines.append(r.diff_proposal)
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def render_hold_section(items: list[EvalResult]) -> str:
    if not items:
        return "_なし_\n"

    lines: list[str] = []
    for i, r in enumerate(items, 1):
        lines.append(f"### [{i}] {r.title}")
        lines.append(f"- スコア: {r.total_score:.2f} | タグ: `{r.tag}`")
        lines.append(f"- 元投稿: {r.url}")
        lines.append(f"- 要約: {r.summary[:150]}")
        lines.append("")
    return "\n".join(lines)


def generate_report(results: list[EvalResult], call_count: int | None = None) -> str:
    today = date.today().strftime("%Y-%m-%d")

    apply_items = sorted([r for r in results if r.verdict == "apply"], key=lambda r: -r.total_score)
    hold_items = sorted([r for r in results if r.verdict == "hold"], key=lambda r: -r.total_score)
    discard_items = [r for r in results if r.verdict == "discard"]

    cost_usd = (call_count or 0) * COST_PER_CALL
    cost_jpy = cost_usd * JPY_PER_USD

    lines: list[str] = [
        f"# Claude Code Best Practice Report {today}",
        "",
        f"## 適用候補 ({len(apply_items)}件)",
        "",
        render_apply_section(apply_items),
        f"## 保留 ({len(hold_items)}件)",
        "",
        render_hold_section(hold_items),
        f"## 破棄 ({len(discard_items)}件)",
        "",
        f"{len(discard_items)} 件（スコア < 0.40 またはFROZEN競合）",
        "",
        "## 実行コスト",
        "",
        f"- APIコール数: {call_count or '不明'} 回",
        f"- 合計: ${cost_usd:.4f}（約 {cost_jpy:.0f} 円）",
        "",
    ]
    return "\n".join(lines)


def main(call_count: int | None = None) -> Path:
    input_file = OUTPUT_DIR / "evaluated.jsonl"
    results = load_results(input_file)

    print(f"Loaded {len(results)} evaluated results")

    report = generate_report(results, call_count)

    today = date.today().strftime("%Y-%m-%d")
    output_file = OUTPUT_DIR / f"report-{today}.md"
    output_file.write_text(report)

    apply_count = sum(1 for r in results if r.verdict == "apply")
    hold_count = sum(1 for r in results if r.verdict == "hold")
    discard_count = sum(1 for r in results if r.verdict == "discard")

    print(f"Report: apply={apply_count}, hold={hold_count}, discard={discard_count}")
    print(f"Saved → {output_file}")
    return output_file


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate best practice report from evaluated results")
    parser.add_argument("--calls", type=int, help="Number of API calls used (for cost estimate)")
    args = parser.parse_args()
    main(args.calls)
