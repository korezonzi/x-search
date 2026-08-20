"""Constants and configuration for the x-search pipeline."""

import os
from pathlib import Path

# Directories
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
QUERIES_FILE = BASE_DIR / "queries.yaml"

CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# SocialData API
SOCIALDATA_BASE_URL = "https://api.socialdata.tools"
SOCIALDATA_SEARCH_URL = f"{SOCIALDATA_BASE_URL}/twitter/search"
SOCIALDATA_ARTICLE_URL = f"{SOCIALDATA_BASE_URL}/twitter/article"

# Rate limiting
MAX_CALLS_PER_RUN = 200
REQUEST_TIMEOUT_SECONDS = 30

# Scoring thresholds
SCORE_APPLY = 0.70
SCORE_HOLD = 0.40

# Engagement normalization (calibrated to real data: max ~1122 likes, ~30k views)
ENGAGEMENT_NORMALIZATION_LIKES = 1000
ENGAGEMENT_NORMALIZATION_RT = 300
ENGAGEMENT_NORMALIZATION_BM = 150
ENGAGEMENT_NORMALIZATION_VIEWS = 30000

# Freshness: days
FRESHNESS_MAX_DAYS = 365

# Decisions already settled in ~/.claude/ — proposals to overwrite are discarded
FROZEN_DECISIONS = [
    "conventional commits",
    "80% coverage",
    "plan-then-execute",
    "immutability",
]

# Tag → target file mapping (fallback; ContentType-based routing takes precedence)
TAG_TO_TARGET: dict[str, str] = {
    "cc_hooks": "~/.claude/rules/common/hooks.md",
    "cc_skills": "~/.claude/skills/",
    "cc_prompt": "~/.claude/CLAUDE.md",
    "cc_agents": "~/.claude/rules/common/agents.md",
    "cc_cost": "~/.claude/rules/common/performance.md",
    "cc_article_en": "~/.claude/knowledge/claude-code-best-practices.md",
    "cc_article_ja": "~/.claude/knowledge/claude-code-best-practices.md",
    "cc_general_ja": "~/.claude/knowledge/claude-code-best-practices.md",
}

# Score penalty multiplier when target is CLAUDE.md (high threshold)
CLAUDE_MD_SCORE_PENALTY = 0.7
