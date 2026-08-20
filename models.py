"""Data models for the x-search pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Content type classification — maps tweet content to the correct ~/.claude/ target
ContentType = Literal[
    "behavioral_rule",    # Universal behavioral principle → CLAUDE.md (high threshold)
    "domain_hooks",       # Hook patterns → rules/common/hooks.md
    "domain_testing",     # TDD/coverage → rules/common/testing.md
    "domain_security",    # OWASP/secrets → rules/common/security.md
    "domain_agents",      # Multi-agent/subagent → rules/common/agents.md
    "domain_performance", # Model selection/cost → rules/common/performance.md
    "domain_git",         # Commit/PR workflow → rules/common/git-workflow.md
    "domain_workflow",    # Feature pipeline → rules/common/development-workflow.md
    "domain_coding",      # Coding style/immutability → rules/common/coding-style.md
    "workflow",           # Multi-step reusable procedure → skills/
    "general_knowledge",  # Reference facts/tips → knowledge/claude-code-best-practices.md
    "discard",            # Should not be applied anywhere
]


@dataclass
class TweetRecord:
    """Raw tweet/article record from SocialData API."""

    tweet_id: str
    text: str
    full_text: str  # Article full text or same as text for tweets
    author_username: str
    author_name: str
    author_bio: str
    created_at: str  # ISO 8601
    likes: int
    retweets: int
    bookmarks: int
    views: int
    is_article: bool
    tag: str  # Query tag that found this tweet
    url: str

    @property
    def created_at_dt(self) -> datetime:
        try:
            return datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now()


@dataclass
class EvalResult:
    """Evaluation result for a single tweet/article."""

    tweet_id: str
    tag: str
    title: str
    url: str
    score_engagement: float
    score_specificity: float
    score_alignment: float
    score_novelty: float
    score_freshness: float
    total_score: float
    verdict: Literal["apply", "hold", "discard"]
    target_file: str
    summary: str
    diff_proposal: str
    likes: int
    bookmarks: int
    views: int
    frozen_conflict: bool = False
    full_text: str = ""
    raw_labels: list[str] = field(default_factory=list)
    # Governance-aware fields
    content_type: str = "general_knowledge"
    target_section: str = ""
    governance_rationale: str = ""
    actionable_insight: str = ""
