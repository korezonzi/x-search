"""Phase 1: Collect tweets/articles via SocialData API."""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from config import (
    CACHE_DIR,
    MAX_CALLS_PER_RUN,
    QUERIES_FILE,
    REQUEST_TIMEOUT_SECONDS,
    SOCIALDATA_ARTICLE_URL,
    SOCIALDATA_SEARCH_URL,
)
from models import TweetRecord

load_dotenv()

_call_count = 0

# --- Japanese detection ---


def is_japanese_author(profile_text: str) -> bool:
    """Return True if profile text contains Japanese characters."""
    return bool(re.search(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", profile_text))


# --- Cache helpers ---


def load_seen_ids() -> set[str]:
    """Load already-processed tweet IDs from cache."""
    seen_file = CACHE_DIR / "seen_ids.txt"
    if not seen_file.exists():
        return set()
    return set(seen_file.read_text().splitlines())


def save_seen_ids(ids: set[str]) -> None:
    """Persist tweet IDs to cache."""
    seen_file = CACHE_DIR / "seen_ids.txt"
    existing = load_seen_ids()
    updated = existing | ids
    seen_file.write_text("\n".join(sorted(updated)))


def save_records(records: list[TweetRecord], date_str: str) -> None:
    """Append records to daily JSONL cache file."""
    output_file = CACHE_DIR / f"{date_str}.jsonl"
    with output_file.open("a") as f:
        for rec in records:
            f.write(json.dumps(rec.__dict__) + "\n")


# --- API helpers ---


def _api_headers() -> dict[str, str]:
    key = os.environ.get("SOCIALDATA_API_KEY", "")
    if not key:
        raise RuntimeError("SOCIALDATA_API_KEY not set. Copy .env.example → .env and add your key.")
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _increment_call() -> None:
    global _call_count
    _call_count += 1
    if _call_count > MAX_CALLS_PER_RUN:
        raise RuntimeError(f"MAX_CALLS_PER_RUN ({MAX_CALLS_PER_RUN}) exceeded. Stopping.")


def get_article_detail(tweet_id: str) -> dict | None:
    """Fetch full article content for a given tweet ID."""
    _increment_call()
    url = f"{SOCIALDATA_ARTICLE_URL}/{tweet_id}"
    try:
        resp = requests.get(url, headers=_api_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        print(f"  [warn] article fetch failed {tweet_id}: {e}", file=sys.stderr)
        return None


def _search_page(query: str, next_cursor: str | None) -> dict:
    """Fetch one page of search results."""
    _increment_call()
    params: dict = {"query": query, "type": "Latest"}
    if next_cursor:
        params["cursor"] = next_cursor
    resp = requests.get(
        SOCIALDATA_SEARCH_URL,
        headers=_api_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return resp.json()


# --- Record builder ---


def _build_record(tweet: dict, tag: str, query_type: str, article_data: dict | None) -> TweetRecord:
    user = tweet.get("user", {})
    tweet_id = tweet.get("id_str", "")
    full_text = tweet.get("full_text", tweet.get("text", ""))

    if article_data:
        article_content = article_data.get("content", "") or article_data.get("full_text", "")
        full_text = article_content if article_content else full_text

    return TweetRecord(
        tweet_id=tweet_id,
        text=tweet.get("full_text", tweet.get("text", "")),
        full_text=full_text,
        author_username=user.get("screen_name", ""),
        author_name=user.get("name", ""),
        author_bio=user.get("description", ""),
        created_at=tweet.get("tweet_created_at", ""),
        likes=tweet.get("favorite_count", 0),
        retweets=tweet.get("retweet_count", 0),
        bookmarks=tweet.get("bookmark_count", 0),
        views=tweet.get("views_count", 0),
        is_article=query_type == "article",
        tag=tag,
        url=f"https://x.com/{user.get('screen_name', '')}/status/{tweet_id}",
    )


# --- Main search function ---


def search_articles(
    query: str,
    tag: str,
    query_type: str,
    since: datetime,
    until: datetime,
    seen_ids: set[str],
    ja_only: bool = False,
) -> list[TweetRecord]:
    """Fetch tweets matching query within [since, until] range."""
    records: list[TweetRecord] = []
    cursor: str | None = None

    while True:
        try:
            data = _search_page(query, cursor)
        except RuntimeError:
            raise
        except Exception as e:
            print(f"  [error] search failed: {e}", file=sys.stderr)
            break

        tweets = data.get("tweets", [])
        if not tweets:
            break

        for tweet in tweets:
            tweet_id = tweet.get("id_str", "")
            created_raw = tweet.get("tweet_created_at", "")

            # Parse date
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            # Stop if older than since
            if created_dt < since:
                return records

            # Skip if newer than until
            if created_dt > until:
                continue

            # Skip seen
            if tweet_id in seen_ids:
                continue

            user = tweet.get("user", {})
            bio = user.get("description", "") or ""

            # Japanese author filter
            if ja_only and not is_japanese_author(bio + user.get("name", "")):
                continue

            # Fetch article detail if needed
            article_data = None
            if query_type == "article":
                article_data = get_article_detail(tweet_id)

            rec = _build_record(tweet, tag, query_type, article_data)
            records.append(rec)
            seen_ids.add(tweet_id)

        cursor = data.get("next_cursor")
        if not cursor:
            break

    return records


# --- Entry point ---


def main(since: datetime, until: datetime) -> None:
    queries = yaml.safe_load(QUERIES_FILE.read_text())["queries"]
    seen_ids = load_seen_ids()
    date_str = until.strftime("%Y-%m-%d")
    all_records: list[TweetRecord] = []

    print(f"Collecting from {since.date()} to {until.date()} — {len(queries)} queries")

    for q in queries:
        tag: str = q["tag"]
        query: str = q["query"]
        query_type: str = q.get("type", "tweet")
        ja_only = tag.endswith("_ja")

        print(f"  [{tag}] {query[:60]}...")
        try:
            records = search_articles(
                query=query,
                tag=tag,
                query_type=query_type,
                since=since,
                until=until,
                seen_ids=seen_ids,
                ja_only=ja_only,
            )
        except RuntimeError as e:
            print(f"  [stop] {e}", file=sys.stderr)
            break

        print(f"    → {len(records)} new records")
        all_records.extend(records)

    save_records(all_records, date_str)
    save_seen_ids(seen_ids)

    print(f"\nDone. {len(all_records)} records saved → cache/{date_str}.jsonl")
    print(f"API calls used: {_call_count}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Collect Claude Code best practices from X")
    parser.add_argument("--since", type=str, help="Start date YYYY-MM-DD (default: 7 days ago)")
    parser.add_argument("--until", type=str, help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    tz = timezone.utc
    until_dt = (
        datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=tz)
        if args.until
        else datetime.now(tz)
    )
    since_dt = (
        datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=tz)
        if args.since
        else until_dt - timedelta(days=7)
    )

    main(since_dt, until_dt)
