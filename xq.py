#!/usr/bin/env python3
"""xq.py — Generic X (Twitter) search CLI backed by the SocialData API.

Subcommands:
  search  Raw-query search with convenience operator flags (see `search --help`)
  watch   Check watch.yaml handles for posts newer than the last run
  usage   Show this month's usage ledger and estimated cost

Convenience flags on `search` (e.g. --from-user, --min-faves) are appended to
the raw query as additional X search operators via simple string concatenation.
If the raw query already contains an equivalent operator, this CLI does not
deduplicate or take precedence — X's own query parser resolves the duplicate.
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv

from config import LOGS_DIR, OUTPUT_DIR, REQUEST_TIMEOUT_SECONDS, SOCIALDATA_SEARCH_URL, BASE_DIR

load_dotenv()

# --- Constants ---

DEFAULT_LIMIT = 20
MAX_LIMIT = 200
MAX_PAGES = 10  # safety upper bound on pagination per fetch call
TEXT_PREVIEW_LENGTH = 160

MAX_RETRIES = 3  # retries for 429/5xx responses, on top of the first attempt
BASE_BACKOFF_SECONDS = 1.0

DEFAULT_MONTHLY_TWEET_CAP = 50_000  # ≈ $10 at $0.0002/tweet
COST_PER_TWEET_USD = 0.0002
USAGE_WARNING_RATIO = 0.8

WATCH_FILE = BASE_DIR / "watch.yaml"
WATCH_STATE_FILE = LOGS_DIR / "watch-state.json"
DEFAULT_WATCH_LIMIT_PER_HANDLE = 10
DEFAULT_WATCH_LIMIT_PER_SEARCH = 15
DEFAULT_WATCH_SINCE_DAYS = 7

# Composite engagement score weights (see engagement_score()). Bookmarks are
# weighted highest as the strongest "worth catching up on" signal (saves =
# read-later intent); replies are deliberately damped since they spike
# hardest on outrage/gossip rather than genuine value.
ENGAGEMENT_WEIGHTS: dict[str, float] = {
    "bookmark_count": 4.0,  # saves = read-later value, strongest catch-up signal
    "retweet_count": 2.0,  # endorsement / spread
    "quote_count": 1.5,  # spread with commentary; spikes on controversy, below RT
    "favorite_count": 1.0,  # weak baseline signal
    "reply_count": 0.5,  # spikes hardest on outrage/gossip, deliberately damped
    "views_count": 0.1,  # denominator-ish, tie-breaker
}

JST = timezone(timedelta(hours=9))


class FetchError(RuntimeError):
    """Raised when a paginated fetch fails partway through.

    Carries whatever tweets/api_calls were already fetched — and therefore
    already billed by SocialData — before the failure, so callers can still
    record_usage() for them instead of silently losing that spend from the
    ledger.
    """

    def __init__(self, message: str, tweets: list[dict], api_calls: int) -> None:
        super().__init__(message)
        self.tweets = tweets
        self.api_calls = api_calls
        self.raw_tweet_count = len(tweets)


# --- Time helpers ---


def _parse_utc_datetime(raw: str) -> datetime | None:
    """Parse a SocialData tweet_created_at string (ISO8601, UTC) into a datetime."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_jst(raw: str) -> datetime | None:
    """Convert a raw UTC created_at string to a JST-aware datetime."""
    dt = _parse_utc_datetime(raw)
    return dt.astimezone(JST) if dt else None


def _resolve_since(last_checked: str | None) -> datetime:
    """Resolve the watch window start.

    Parses `last_checked` (ISO8601). Falls back to DEFAULT_WATCH_SINCE_DAYS
    ago — with a stderr warning — if it is missing or unparseable, so a
    corrupted watch-state.json can never crash the whole run.
    """
    if last_checked:
        try:
            return datetime.fromisoformat(last_checked)
        except ValueError:
            print(
                f"WARNING: unparseable last_checked '{last_checked}'; falling back to "
                f"{DEFAULT_WATCH_SINCE_DAYS} days ago.",
                file=sys.stderr,
            )
    return datetime.now(timezone.utc) - timedelta(days=DEFAULT_WATCH_SINCE_DAYS)


# --- API access ---


def _api_headers() -> dict[str, str]:
    """Build request headers. Never logs or prints the API key."""
    api_key = os.environ.get("SOCIALDATA_API_KEY", "")
    if not api_key:
        raise RuntimeError("SOCIALDATA_API_KEY not set. Copy .env.example to .env and add your key.")
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _backoff_delay(attempt: int, retry_after_header: str | None) -> float:
    """Delay before the next retry: honors Retry-After if present/parseable,
    otherwise exponential backoff (BASE_BACKOFF_SECONDS * 2**attempt)."""
    if retry_after_header:
        try:
            return float(retry_after_header)
        except ValueError:
            pass
    return BASE_BACKOFF_SECONDS * (2**attempt)


def _search_page(query: str, cursor: str | None) -> dict[str, Any]:
    """Fetch one page of search results from SocialData.

    Retries on 429/5xx with exponential backoff (respecting Retry-After when
    present), up to MAX_RETRIES times. Any other 4xx fails immediately —
    those are not transient and retrying wastes quota.
    """
    params: dict[str, str] = {"query": query, "type": "Latest"}
    if cursor:
        params["cursor"] = cursor

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        retry_after: str | None = None
        try:
            resp = requests.get(
                SOCIALDATA_SEARCH_URL, headers=_api_headers(), params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as e:
            last_error = e
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return _parse_search_response(resp)
            last_error = requests.HTTPError(f"HTTP {resp.status_code} from SocialData", response=resp)
            retry_after = resp.headers.get("Retry-After")

        if attempt < MAX_RETRIES:
            time.sleep(_backoff_delay(attempt, retry_after))

    raise RuntimeError(f"SocialData API request failed after {MAX_RETRIES} retries: {last_error}")


def _parse_search_response(resp: requests.Response) -> dict[str, Any]:
    """Validate the HTTP status and decode the JSON body of a search response."""
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"SocialData API request failed: {e}") from e
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError(f"Failed to parse SocialData API response: {e}") from e


def fetch_tweets(query: str, limit: int, max_pages: int = MAX_PAGES) -> tuple[list[dict], int, int]:
    """Fetch up to `limit` tweets for `query`, paginating via next_cursor.

    Returns (tweets[:limit], api_calls_made, raw_tweet_count). `raw_tweet_count`
    is the number of tweet objects actually returned by the API before slicing
    to `limit` — SocialData bills per tweet returned, not per tweet kept after
    our own truncation, so that is the number the cost guard must record.

    Stops early once `limit` is reached, the API returns no more results, or
    `max_pages` is hit (safety bound). Raises FetchError (carrying whatever
    was fetched so far) if a page request ultimately fails.
    """
    tweets: list[dict] = []
    cursor: str | None = None
    api_calls = 0

    for _ in range(max_pages):
        try:
            data = _search_page(query, cursor)
        except RuntimeError as e:
            raise FetchError(str(e), tweets, api_calls) from e
        api_calls += 1

        page_tweets = data.get("tweets") or []
        if not page_tweets:
            break
        tweets.extend(page_tweets)

        cursor = data.get("next_cursor")
        if not cursor or len(tweets) >= limit:
            break

    return tweets[:limit], api_calls, len(tweets)


def fetch_all_since(query: str, since_dt: datetime, max_pages: int = MAX_PAGES) -> tuple[list[dict], int, int, bool]:
    """Page through `query` (already scoped with a since:DATE operator),
    collecting every tweet newer than `since_dt`.

    Stops when a page contains a tweet at/before `since_dt` (the window has
    been fully covered), next_cursor is exhausted, or `max_pages` is hit.
    Returns (new_tweets, api_calls, raw_tweet_count, window_incomplete).
    `window_incomplete` is True only if `max_pages` was hit while the window
    still had not been fully covered — i.e. some new tweets may be missing.
    Raises FetchError (carrying partial results) if a page request fails.
    """
    new_tweets: list[dict] = []
    cursor: str | None = None
    api_calls = 0
    raw_tweet_count = 0
    window_incomplete = True

    for _ in range(max_pages):
        try:
            data = _search_page(query, cursor)
        except RuntimeError as e:
            raise FetchError(str(e), new_tweets, api_calls) from e
        api_calls += 1

        page_tweets = data.get("tweets") or []
        if not page_tweets:
            window_incomplete = False
            break
        raw_tweet_count += len(page_tweets)

        page_has_older_tweet = False
        for tweet in page_tweets:
            created_dt = _parse_utc_datetime(tweet.get("tweet_created_at") or "")
            if created_dt is not None and created_dt > since_dt:
                new_tweets.append(tweet)
            else:
                page_has_older_tweet = True

        cursor = data.get("next_cursor")
        if page_has_older_tweet or not cursor:
            window_incomplete = False
            break

    return new_tweets, api_calls, raw_tweet_count, window_incomplete


# --- Query building ---


def build_query(raw_query: str, args: argparse.Namespace) -> str:
    """Append convenience-flag operators to the raw query string.

    Duplicate-operator handling: if `raw_query` already contains an operator
    equivalent to one of the flags, this function simply appends the flag's
    operator too — it does not detect or remove duplicates. X's own query
    parser is left to resolve the resulting duplicate.
    """
    parts = [raw_query]
    if args.from_user:
        parts.append(f"from:{args.from_user}")
    if args.min_faves is not None:
        parts.append(f"min_faves:{args.min_faves}")
    if args.since:
        parts.append(f"since:{args.since}")
    if args.until:
        parts.append(f"until:{args.until}")
    if args.lang:
        parts.append(f"lang:{args.lang}")
    if args.no_replies:
        parts.append("-filter:replies")
    return " ".join(parts)


def engagement_score(tweet: dict) -> float:
    """Composite engagement score for a RAW API tweet dict (favorite_count etc.).

    Not for normalized dicts (favs etc.) — unknown keys score 0.
    """
    total = 0.0
    for field, weight in ENGAGEMENT_WEIGHTS.items():
        value = tweet.get(field)
        if isinstance(value, (int, float)) and value > 0:
            total += weight * math.log1p(value)
    return total


def sort_tweets(tweets: list[dict], sort_mode: str) -> list[dict]:
    """Sort tweets by composite engagement score (see engagement_score) or leave API order (recent)."""
    if sort_mode == "engagement":
        return sorted(tweets, key=engagement_score, reverse=True)
    return tweets


# --- Record normalization / rendering ---


@dataclass
class NormalizedTweet:
    """Reduced tweet record used for --format json output."""

    id: str
    url: str
    author: str
    created_at: str  # JST ISO8601
    favs: int
    retweets: int
    views: int
    text: str
    replies: int
    quotes: int
    bookmarks: int
    score: float


def normalize_tweet(tweet: dict) -> NormalizedTweet:
    """Reduce a raw SocialData tweet object to the fields required by --format json."""
    user = tweet.get("user") or {}
    tweet_id = tweet.get("id_str") or ""
    handle = user.get("screen_name") or ""
    dt_jst = to_jst(tweet.get("tweet_created_at") or "")
    return NormalizedTweet(
        id=tweet_id,
        url=f"https://x.com/{handle}/status/{tweet_id}",
        author=handle,
        created_at=dt_jst.isoformat() if dt_jst else (tweet.get("tweet_created_at") or ""),
        favs=tweet.get("favorite_count") or 0,
        retweets=tweet.get("retweet_count") or 0,
        views=tweet.get("views_count") or 0,
        text=tweet.get("full_text") or tweet.get("text") or "",
        replies=tweet.get("reply_count") or 0,
        quotes=tweet.get("quote_count") or 0,
        bookmarks=tweet.get("bookmark_count") or 0,
        score=round(engagement_score(tweet), 2),
    )


def format_md_entry(index: int, tweet: dict) -> str:
    """Render one tweet as a 3-line markdown digest entry."""
    user = tweet.get("user") or {}
    handle = user.get("screen_name") or ""
    tweet_id = tweet.get("id_str") or ""
    favs = tweet.get("favorite_count") or 0
    retweets = tweet.get("retweet_count") or 0
    bookmarks = tweet.get("bookmark_count") or 0
    replies = tweet.get("reply_count") or 0
    score = engagement_score(tweet)
    text = tweet.get("full_text") or tweet.get("text") or ""
    # " ".join(text.split()) collapses every Unicode whitespace/line-break
    # character (not just \r\n/\n/\r) into single spaces — full_text can
    # contain U+2028 etc., which the old replace-chain left in place.
    text_flat = " ".join(text.split())
    text_preview = text_flat[:TEXT_PREVIEW_LENGTH]

    dt_jst = to_jst(tweet.get("tweet_created_at") or "")
    time_str = f"{dt_jst.month}/{dt_jst.day} {dt_jst.strftime('%H:%M')} JST" if dt_jst else "unknown time"

    url = f"https://x.com/{handle}/status/{tweet_id}"
    header = f"{index}. @{handle} ({time_str}) ♥{favs} RT{retweets} 🔖{bookmarks} 💬{replies} ⚡{score:.1f}"
    return f"{header}\n   {text_preview}\n   {url}"


def render(tweets: list[dict], fmt: str) -> None:
    """Print tweets to stdout in the requested format."""
    if fmt == "md":
        if not tweets:
            print("(no results)")
        for i, tweet in enumerate(tweets, 1):
            print(format_md_entry(i, tweet))
    elif fmt == "json":
        compact = [asdict(normalize_tweet(t)) for t in tweets]
        print(json.dumps(compact, ensure_ascii=False, indent=2))
    elif fmt == "full":
        print(json.dumps(tweets, ensure_ascii=False, indent=2))


def save_raw_output(prefix: str, tweets: list[dict], **meta: Any) -> Path:
    """Save all raw tweet data for this run to output/<prefix>-YYYYMMDD-HHMMSS.json."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUTPUT_DIR / f"{prefix}-{timestamp}.json"
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), **meta, "tweets": tweets}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


# --- Atomic JSON persistence ---


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to `path` atomically via a temp file + os.replace.

    Prevents a crash or concurrent run mid-write from leaving a truncated
    or corrupted ledger / watch-state file behind.
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


# --- Cost guard (shared by search & watch) ---


def _monthly_cap() -> int:
    """Resolve the monthly tweet cap from XQ_MONTHLY_CAP, falling back to the
    default (with a stderr warning) if the env var is set but not a valid int."""
    raw = os.environ.get("XQ_MONTHLY_CAP")
    if raw is None:
        return DEFAULT_MONTHLY_TWEET_CAP
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARNING: XQ_MONTHLY_CAP='{raw}' is not a valid integer; "
            f"falling back to default {DEFAULT_MONTHLY_TWEET_CAP}.",
            file=sys.stderr,
        )
        return DEFAULT_MONTHLY_TWEET_CAP


def _usage_ledger_path() -> Path:
    month_str = datetime.now(timezone.utc).strftime("%Y-%m")
    return LOGS_DIR / f"usage-{month_str}.json"


def load_usage_ledger() -> dict[str, Any]:
    """Load the current month's usage ledger, or an empty one if absent/corrupt."""
    path = _usage_ledger_path()
    empty: dict[str, Any] = {"tweets_fetched": 0, "api_calls": 0, "updated_at": ""}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return empty
    return {**empty, **data}


def save_usage_ledger(ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(_usage_ledger_path(), ledger)


def record_usage(tweets_fetched: int, api_calls: int) -> dict[str, Any]:
    """Add this run's usage to the monthly ledger and persist it."""
    ledger = load_usage_ledger()
    ledger["tweets_fetched"] = (ledger.get("tweets_fetched") or 0) + tweets_fetched
    ledger["api_calls"] = (ledger.get("api_calls") or 0) + api_calls
    save_usage_ledger(ledger)
    return ledger


def check_cost_guard(force: bool) -> None:
    """Block execution if the monthly tweet cap is already exceeded.

    Uses usage recorded from prior runs (this run's own fetch has not
    happened yet). Warns at >=80% usage; exits(1) at/over the cap unless
    `force` is set, in which case it warns and continues.
    """
    ledger = load_usage_ledger()
    used = ledger.get("tweets_fetched") or 0
    cap = _monthly_cap()

    if used >= cap:
        if not force:
            print(
                f"ERROR: monthly tweet cap reached ({used}/{cap} tweets used this month). "
                "Use --force to proceed anyway.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"WARNING: proceeding past monthly cap with --force ({used}/{cap} tweets used).", file=sys.stderr)
    elif used >= cap * USAGE_WARNING_RATIO:
        print(f"WARNING: {used}/{cap} tweets used this month ({used / cap:.0%}).", file=sys.stderr)


# --- watch.yaml / watch-state.json helpers ---


def load_watch_config() -> tuple[dict[str, Any], list[Any], list[Any]]:
    """Load watch.yaml, returning (defaults, handles, searches).

    `searches` defaults to [] when the key is absent/empty, so watch.yaml
    files without a `searches:` section keep working unchanged.
    """
    if not WATCH_FILE.exists():
        raise RuntimeError(f"watch.yaml not found at {WATCH_FILE}")
    try:
        data = yaml.safe_load(WATCH_FILE.read_text()) or {}
    except yaml.YAMLError as e:
        raise RuntimeError(f"Failed to parse watch.yaml: {e}") from e
    return data.get("defaults") or {}, data.get("handles") or [], data.get("searches") or []


def load_watch_state() -> dict[str, Any]:
    if not WATCH_STATE_FILE.exists():
        return {}
    try:
        return json.loads(WATCH_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_watch_state(state: dict[str, Any]) -> None:
    _atomic_write_json(WATCH_STATE_FILE, state)


def _fetch_new_since(
    context_label: str, base_query: str, last_checked: str | None, max_pages: int = MAX_PAGES
) -> tuple[list[dict], int, int]:
    """Shared engine behind fetch_new_for_handle/fetch_new_for_search.

    Resolves the watch window (via _resolve_since), composes
    `{base_query} since:DATE`, and pages through fetch_all_since until the
    window is fully covered — not just until a display limit is hit, so no
    new tweet is silently skipped.

    Returns (new_tweets, api_calls, raw_tweet_count). Emits a stderr warning
    (without raising) if max_pages was hit before the window was covered.
    """
    since_dt = _resolve_since(last_checked)
    query = f"{base_query} since:{since_dt.strftime('%Y-%m-%d')}"
    new_tweets, api_calls, raw_tweet_count, window_incomplete = fetch_all_since(query, since_dt, max_pages)

    if window_incomplete:
        print(
            f"WARNING: {context_label} — hit MAX_PAGES ({max_pages}) before reaching {since_dt.date()}; "
            "some new posts may be missing this run. last_checked will still advance.",
            file=sys.stderr,
        )
    return new_tweets, api_calls, raw_tweet_count


def fetch_new_for_handle(handle: str, last_checked: str | None, max_pages: int = MAX_PAGES) -> tuple[list[dict], int, int]:
    """Fetch ALL tweets from `handle` newer than last_checked (or last 7 days
    on first run). See _fetch_new_since for the shared pagination behavior."""
    return _fetch_new_since(f"@{handle}", f"from:{handle}", last_checked, max_pages)


def fetch_new_for_search(
    name: str, query: str, last_checked: str | None, max_pages: int = MAX_PAGES
) -> tuple[list[dict], int, int]:
    """Fetch ALL tweets matching a watch.yaml saved search (`query`) newer
    than last_checked (or last 7 days on first run). Same full-window
    pagination as fetch_new_for_handle — see _fetch_new_since."""
    return _fetch_new_since(f"search:{name}", query, last_checked, max_pages)


def _filter_displayed(tweets: list[dict], limit: int, seen_ids: set[str]) -> list[dict]:
    """Cross-section dedup: drop tweets already displayed by an earlier digest
    section, then claim the IDs that will actually be shown (top `limit`) so
    later sections skip them and other content fills their slots.

    Tweets hidden behind the overflow cut are NOT claimed — they may still
    surface in a later section. `tweets` must already be in display order.
    A tweet without id_str is never claimed nor dropped (fail-open: it can
    still appear in multiple sections).
    """
    fresh = [t for t in tweets if (t.get("id_str") or "") not in seen_ids]
    seen_ids.update(tid for t in fresh[:limit] if (tid := t.get("id_str") or ""))
    return fresh


def _render_digest_section(heading: str, new_tweets: list[dict], limit: int) -> str:
    """Render one digest section: `heading` + up to `limit` entries (already
    in the desired display order), then an overflow summary line if truncated."""
    if not new_tweets:
        return f"{heading}\n新着なし"

    shown = new_tweets[:limit]
    entries = "\n".join(format_md_entry(i, t) for i, t in enumerate(shown, 1))
    overflow = len(new_tweets) - len(shown)
    if overflow > 0:
        entries += f"\n   …ほか {overflow}件（output/のJSON参照）"
    return f"{heading}\n{entries}"


def _render_watch_section(handle: str, new_tweets: list[dict], limit: int, seen_ids: set[str]) -> str:
    """Render one handle's markdown section, truncated to `limit` entries."""
    fresh = _filter_displayed(new_tweets, limit, seen_ids)
    return _render_digest_section(f"## @{handle}", fresh, limit)


def _search_heading(name: str, note: str | None) -> str:
    """Build a saved-search digest heading: '## 🔍 {name}', plus ' — {note}' if given."""
    return f"## 🔍 {name}" + (f" — {note}" if note else "")


def _render_search_section(name: str, note: str | None, new_tweets: list[dict], limit: int, seen_ids: set[str]) -> str:
    """Render one saved search's digest section: engagement-sorted, deduped
    against earlier sections, then truncated to `limit` entries with an
    overflow summary line."""
    ranked = sort_tweets(new_tweets, "engagement")
    fresh = _filter_displayed(ranked, limit, seen_ids)
    return _render_digest_section(_search_heading(name, note), fresh, limit)


SECTION_RETRY_BACKOFF_SECONDS = 60
_retry_backoff_spent = False


def _fetch_with_retry(fetch_fn):
    """Retry one section fetch after a transient failure (2026-08-25: a local
    DNS blip took out six consecutive handle sections in one run; the data was
    recoverable seconds later). The backoff sleep is global-once per run — the
    first failure waits SECTION_RETRY_BACKOFF_SECONDS, later sections retry
    immediately — so an outage never adds more than one minute of wall clock.

    Usage from the failed attempt (already billed by SocialData) is folded
    into the retry's result, or into the re-raised FetchError, so the ledger
    never loses billed pages. The failed attempt's partial tweets are dropped:
    the retry re-fetches the same window (last_checked has not advanced).
    """
    global _retry_backoff_spent
    try:
        return fetch_fn()
    except FetchError as first:
        if not _retry_backoff_spent:
            print(
                f"WARNING: section fetch failed ({first}); retrying once after "
                f"{SECTION_RETRY_BACKOFF_SECONDS}s backoff.",
                file=sys.stderr,
            )
            time.sleep(SECTION_RETRY_BACKOFF_SECONDS)
            _retry_backoff_spent = True
        try:
            tweets, api_calls, raw_count = fetch_fn()
            return tweets, api_calls + first.api_calls, raw_count + first.raw_tweet_count
        except FetchError as second:
            second.api_calls += first.api_calls
            second.raw_tweet_count += first.raw_tweet_count
            raise


def _watch_handle(
    handle: str, last_checked: str | None, limit: int, seen_ids: set[str]
) -> tuple[str, list[dict], int, int, bool]:
    """Process one watch.yaml handle: fetch new tweets and render its digest section.

    Returns (section_markdown, new_tweets, api_calls, raw_tweet_count, succeeded).
    Catches every exception (not just RuntimeError/FetchError) so that one
    handle's failure never aborts the rest of the run, the state save, or
    usage recording. `succeeded` is False on any failure, so the caller can
    skip advancing that handle's last_checked timestamp (retry next run).
    """
    try:
        new_tweets, api_calls, raw_tweet_count = _fetch_with_retry(
            lambda: fetch_new_for_handle(handle, last_checked)
        )
    except FetchError as e:
        return f"## @{handle}\n[error] {e}", [], e.api_calls, e.raw_tweet_count, False
    except Exception as e:  # isolate unexpected failures to this handle only
        return f"## @{handle}\n[error] unexpected failure: {e}", [], 0, 0, False

    section = _render_watch_section(handle, new_tweets, limit, seen_ids)
    return section, new_tweets, api_calls, raw_tweet_count, True


def _watch_search(
    name: str, query: str, note: str | None, last_checked: str | None, limit: int, seen_ids: set[str]
) -> tuple[str, list[dict], int, int, bool]:
    """Process one watch.yaml saved search: fetch new tweets and render its digest section.

    Mirrors _watch_handle's exception isolation: any failure yields an
    [error] section instead of raising, so one search never aborts the rest
    of the run, the state save, or usage recording. `succeeded` is False on
    any failure, so the caller skips advancing that search's last_checked.
    """
    try:
        new_tweets, api_calls, raw_tweet_count = _fetch_with_retry(
            lambda: fetch_new_for_search(name, query, last_checked)
        )
    except FetchError as e:
        return f"{_search_heading(name, note)}\n[error] {e}", [], e.api_calls, e.raw_tweet_count, False
    except Exception as e:  # isolate unexpected failures to this search only
        return f"{_search_heading(name, note)}\n[error] unexpected failure: {e}", [], 0, 0, False

    section = _render_search_section(name, note, new_tweets, limit, seen_ids)
    return section, new_tweets, api_calls, raw_tweet_count, True


# --- Subcommands ---


def _resolve_search_limit(requested: int) -> int:
    """Clamp --limit to [1, MAX_LIMIT], warning on stderr whenever it clamps."""
    if requested > MAX_LIMIT:
        print(f"WARNING: --limit {requested} exceeds max {MAX_LIMIT}; clamping to {MAX_LIMIT}.", file=sys.stderr)
        return MAX_LIMIT
    if requested < 1:
        print(f"WARNING: --limit {requested} is below minimum 1; clamping to 1.", file=sys.stderr)
        return 1
    return requested


def cmd_search(args: argparse.Namespace) -> None:
    """Run `xq.py search`."""
    check_cost_guard(args.force)

    limit = _resolve_search_limit(args.limit)
    query = build_query(args.query, args)

    try:
        tweets, api_calls, raw_tweet_count = fetch_tweets(query, limit)
    except FetchError as e:
        record_usage(e.raw_tweet_count, e.api_calls)  # don't lose partial (already-billed) usage
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    tweets = sort_tweets(tweets, args.sort)
    record_usage(raw_tweet_count, api_calls)

    output_path = save_raw_output("xq", tweets, query=query)
    render(tweets, args.format)
    print(f"saved: {output_path}")


def _run_handle_watches(
    handles: list[Any], limit: int, state: dict[str, Any], seen_ids: set[str]
) -> tuple[list[str], list[dict], int, int]:
    """Process every watch.yaml handle entry, updating `state` in place.

    Returns (sections, new_tweets, api_calls, raw_tweet_count) aggregated
    across all handles. Skips (with a stderr warning) any entry with a
    missing/empty 'handle' instead of raising KeyError.
    """
    sections: list[str] = []
    all_tweets: list[dict] = []
    total_api_calls = 0
    total_raw_tweet_count = 0

    for entry in handles:
        handle = entry.get("handle") if isinstance(entry, dict) else None
        if not handle:
            print(f"WARNING: skipping watch.yaml handle entry with missing/empty 'handle': {entry!r}", file=sys.stderr)
            continue

        last_checked = (state.get(handle) or {}).get("last_checked")
        section, new_tweets, api_calls, raw_tweet_count, succeeded = _watch_handle(handle, last_checked, limit, seen_ids)
        sections.append(section)
        total_api_calls += api_calls
        total_raw_tweet_count += raw_tweet_count
        all_tweets.extend(new_tweets)
        if succeeded:
            state[handle] = {"last_checked": datetime.now(timezone.utc).isoformat()}

    return sections, all_tweets, total_api_calls, total_raw_tweet_count


def _run_search_watches(
    searches: list[Any], limit: int, state: dict[str, Any], seen_ids: set[str]
) -> tuple[list[str], list[dict], int, int]:
    """Process every watch.yaml search entry, updating `state` in place
    (under the `search:{name}` key, namespaced apart from handle keys so the
    two never collide).

    Returns (sections, new_tweets, api_calls, raw_tweet_count) aggregated
    across all searches. Skips (with a stderr warning) any entry missing
    'name' or 'query' instead of raising KeyError.
    """
    sections: list[str] = []
    all_tweets: list[dict] = []
    total_api_calls = 0
    total_raw_tweet_count = 0

    for entry in searches:
        name = entry.get("name") if isinstance(entry, dict) else None
        query = entry.get("query") if isinstance(entry, dict) else None
        if not name or not query:
            print(f"WARNING: skipping watch.yaml search entry with missing 'name'/'query': {entry!r}", file=sys.stderr)
            continue
        note = entry.get("note") if isinstance(entry, dict) else None

        state_key = f"search:{name}"
        last_checked = (state.get(state_key) or {}).get("last_checked")
        section, new_tweets, api_calls, raw_tweet_count, succeeded = _watch_search(name, query, note, last_checked, limit, seen_ids)
        sections.append(section)
        total_api_calls += api_calls
        total_raw_tweet_count += raw_tweet_count
        all_tweets.extend(new_tweets)
        if succeeded:
            state[state_key] = {"last_checked": datetime.now(timezone.utc).isoformat()}

    return sections, all_tweets, total_api_calls, total_raw_tweet_count


def cmd_watch(args: argparse.Namespace) -> None:
    """Run `xq.py watch`: handles first, then saved searches, into one digest."""
    check_cost_guard(args.force)

    try:
        defaults, handles, searches = load_watch_config()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    limit_per_handle = defaults.get("limit_per_handle") or DEFAULT_WATCH_LIMIT_PER_HANDLE
    limit_per_search = defaults.get("limit_per_search") or DEFAULT_WATCH_LIMIT_PER_SEARCH
    state = load_watch_state()

    # Shared across handles -> searches so a tweet is displayed at most once
    # per digest (first section wins; watch.yaml order = priority order).
    seen_ids: set[str] = set()
    handle_sections, handle_tweets, handle_calls, handle_raw = _run_handle_watches(handles, limit_per_handle, state, seen_ids)
    search_sections, search_tweets, search_calls, search_raw = _run_search_watches(searches, limit_per_search, state, seen_ids)

    all_tweets = handle_tweets + search_tweets
    record_usage(handle_raw + search_raw, handle_calls + search_calls)
    save_watch_state(state)
    output_path = save_raw_output("xq-watch", all_tweets)

    if handle_sections and search_sections:
        sections = handle_sections + ["---"] + search_sections
    else:
        sections = handle_sections + search_sections

    if args.format == "json":
        compact = [asdict(normalize_tweet(t)) for t in all_tweets]
        print(json.dumps(compact, ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(sections))

    print(f"saved: {output_path}")


def cmd_usage(args: argparse.Namespace) -> None:
    """Run `xq.py usage`."""
    ledger = load_usage_ledger()
    tweets = ledger.get("tweets_fetched") or 0
    calls = ledger.get("api_calls") or 0
    cost_usd = tweets * COST_PER_TWEET_USD
    cap = _monthly_cap()
    pct_used = (tweets / cap * 100) if cap else 0.0
    month_str = datetime.now(timezone.utc).strftime("%Y-%m")

    print(f"Usage for {month_str}:")
    print(f"  Tweets fetched : {tweets}")
    print(f"  API calls      : {calls}")
    print(f"  Estimated cost : ${cost_usd:.4f}")
    print(f"  Monthly cap    : {cap} tweets ({pct_used:.1f}% used)")
    print(f"  Updated at     : {ledger.get('updated_at') or 'never'}")


# --- Argument parsing ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xq.py",
        description="Generic X (Twitter) search CLI backed by the SocialData API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser(
        "search",
        help="Search X with a raw query plus convenience flags",
        description=(
            "Search X with a raw query. X search operators (min_faves:100, "
            "from:foo, lang:ja, ...) can be written directly in the query. "
            "Convenience flags below are appended as additional operators; "
            "if the raw query already contains an equivalent operator, X's "
            "own query parser resolves the duplicate (this CLI does not "
            "deduplicate or take precedence)."
        ),
    )
    search_parser.add_argument("query", type=str, help="Raw X search query; operators allowed")
    search_parser.add_argument("--from-user", type=str, default=None, help="Append from:HANDLE")
    search_parser.add_argument("--min-faves", type=int, default=None, help="Append min_faves:N")
    search_parser.add_argument("--since", type=str, default=None, help="Append since:YYYY-MM-DD")
    search_parser.add_argument("--until", type=str, default=None, help="Append until:YYYY-MM-DD")
    search_parser.add_argument("--lang", type=str, default=None, help="Append lang:CODE")
    search_parser.add_argument("--no-replies", action="store_true", help="Append -filter:replies")
    search_parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"Max tweets to fetch (default {DEFAULT_LIMIT}, max {MAX_LIMIT})"
    )
    search_parser.add_argument(
        "--sort",
        choices=["engagement", "recent"],
        default="recent",
        help="engagement: 複合エンゲージメントスコア降順（見た/いいね/RT/引用/保存を加重合算）。recent: 取得順（デフォルト）",
    )
    search_parser.add_argument("--format", choices=["md", "json", "full"], default="md")
    search_parser.add_argument("--force", action="store_true", help="Bypass the monthly cost guard")
    search_parser.set_defaults(func=cmd_search)

    watch_parser = subparsers.add_parser("watch", help="Check watch.yaml handles for new posts")
    watch_parser.add_argument("--format", choices=["md", "json"], default="md")
    watch_parser.add_argument("--force", action="store_true", help="Bypass the monthly cost guard")
    watch_parser.set_defaults(func=cmd_watch)

    usage_parser = subparsers.add_parser("usage", help="Show this month's usage ledger and estimated cost")
    usage_parser.set_defaults(func=cmd_usage)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
