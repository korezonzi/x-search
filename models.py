"""Data models for the x-search pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


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
