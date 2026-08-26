# xq.py — Generic X Search CLI

A thin CLI over the SocialData API (fully usage-based pricing, $0.0002/tweet,
no subscription) for ad-hoc X/Twitter search, account watching, and usage
tracking. Reuses the `.env` (`SOCIALDATA_API_KEY`) and directory conventions
already used by `search.py` / `config.py` in this project.

## Setup

```bash
cd /Users/fuma/dev/ai-setup/x-search
pip install -r requirements.txt   # requests, pyyaml, python-dotenv — already required by search.py
cp .env.example .env               # if not already done, then add your SOCIALDATA_API_KEY
```

## Usage examples

```bash
# 1. Raw query with X search operators written directly
python3 xq.py search "claude code min_faves:200 -filter:replies" --limit 10

# 2. Convenience flags composed onto the raw query
python3 xq.py search "test" --from-user AnthropicAI --min-faves 50 \
  --since 2026-08-01 --no-replies --limit 5 --format json

# 3. Sort by engagement instead of recency
python3 xq.py search "CLAUDE.md tips" --sort engagement --limit 10

# 4. Full raw API response (for downstream tooling / debugging)
python3 xq.py search "claude code skills" --limit 5 --format full

# 5. Watch list — new posts since last run, for all handles in watch.yaml
python3 xq.py watch

# Check this month's usage / estimated cost
python3 xq.py usage
```

## Search operator cheat sheet (usable directly in the raw query)

| Operator | Meaning |
|---|---|
| `from:handle` | Posts by a specific account |
| `min_faves:N` / `min_retweets:N` | Minimum likes / retweets |
| `since:YYYY-MM-DD` / `until:YYYY-MM-DD` | Date range |
| `lang:ja` / `lang:en` | Language filter |
| `-filter:replies` | Exclude replies |
| `filter:links` / `url:example.com` | Has a link / links to a specific URL |
| `"exact phrase"` / `A OR B` / `-word` | Exact match / OR / exclude |

Convenience flags (`--from-user`, `--min-faves`, `--since`, `--until`,
`--lang`, `--no-replies`) simply append the equivalent operator to the raw
query. If the raw query already contains an equivalent operator, this CLI
does not deduplicate — X's own query parser resolves the duplicate.

## Output

- `--format md` (default): compact digest to stdout —
  `N. @handle (M/D HH:MM JST) ♥123 RT45 🔖12 💬8 ⚡31.4` + text preview (160
  chars, newlines flattened to spaces) + tweet URL. `♥`=favorites,
  `RT`=retweets, `🔖`=bookmarks, `💬`=replies, `⚡`=the composite engagement
  score (see "Engagement scoring" below) rounded to 1 decimal.
- `--format json`: reduced JSON per tweet (`id`, `url`, `author`,
  `created_at` in JST, `favs`, `retweets`, `views`, `replies`, `quotes`,
  `bookmarks`, `score`, full `text`).
- `--format full`: raw API response objects, unmodified.
- Regardless of format, **all** fetched raw tweets for the run are saved to
  `output/xq-YYYYMMDD-HHMMSS.json` (or `output/xq-watch-YYYYMMDD-HHMMSS.json`
  for `watch`), and the path is printed as the last line (`saved: <path>`).
  Read the digest on stdout; only open the saved JSON via a subagent if you
  need to dig into full-text/raw fields.

## Cost guard

- Every run adds to a monthly ledger at `logs/usage-YYYY-MM.json`
  (`tweets_fetched`, `api_calls`, `updated_at`).
- Monthly cap: 50,000 tweets (≈ $10), overridable via `XQ_MONTHLY_CAP`.
- Before fetching, if the month's usage is already at/over the cap, the run
  exits with status 1 (use `--force` to proceed anyway). At ≥80% usage, a
  warning is printed to stderr but the run continues.
- Check current usage any time with `python3 xq.py usage`.

## Engagement scoring (`--sort engagement`)

`--sort engagement` (and the `## 🔍 ` saved-search sections in `xq.py watch`
digests, which are always engagement-sorted) rank tweets by a composite
score — a weighted sum of `log1p(count)` across several engagement signals,
computed by `engagement_score()`:

```python
ENGAGEMENT_WEIGHTS: dict[str, float] = {
    "bookmark_count": 4.0,  # saves = read-later value, strongest catch-up signal
    "retweet_count":  2.0,  # endorsement / spread
    "quote_count":    1.5,  # spread with commentary; spikes on controversy, below RT
    "favorite_count": 1.0,  # weak baseline signal
    "reply_count":    0.5,  # spikes hardest on outrage/gossip, deliberately damped
    "views_count":    0.1,  # denominator-ish, tie-breaker
}
```

Why `log1p` and not raw counts: a linear weighted sum lets one viral outlier
(a political/gossip post with 10k+ favorites) dominate every ranking by a
wide margin. `log1p` compresses that gap so posts with a healthier signal
mix — especially bookmarks, the strongest "worth reading" proxy — can
compete with raw favorite/retweet counts instead of being permanently
buried by them.

Why bookmarks are weighted highest and replies are damped: a bookmark means
someone wanted to come back and actually read the post — the closest proxy
this API exposes to "worth catching up on." Replies, by contrast, spike
hardest on outrage, gossip, and political posts (people arguing in the
quote/reply thread), so they're weighted low on purpose rather than treated
as a genuine value signal.

Why `followers_count` is **not** in the score: including it would bias the
ranking toward big accounts regardless of whether a given post is actually
good — a large account's mediocre post would still outrank a small
account's excellent one. It stays in the raw saved JSON
(`output/xq-watch-*.json` → `tweet.user.followers_count`) for anyone who
wants to build a different metric from it, just not in this default score.

**Recalibrating the weights**: if the ranking starts feeling off (e.g. a
low-value post keeps landing near the top, or a clearly valuable one keeps
sinking), pick a recent `output/xq-watch-*.json`, sort it a few different
ways and eyeball the result:

```bash
python3 -c "
import json, xq
tweets = json.load(open('output/xq-watch-YYYYMMDD-HHMMSS.json'))['tweets']
for t in sorted(tweets, key=xq.engagement_score, reverse=True)[:20]:
    print(round(xq.engagement_score(t), 1), t['user']['screen_name'], t.get('full_text', '')[:60])
"
```

If the ordering looks wrong, adjust the relevant weight in
`ENGAGEMENT_WEIGHTS` (the one constant in `xq.py`) and re-run the snippet
against the same saved JSON until the ranking looks right. There's no
separate config file for this by design — one constant, one place to tune.

## LLM relevance filter (`scripts/x-watch-filter.py`)

The composite score above re-ranks posts by *engagement quality*, but it
can't tell a well-engaged political gossip post from a well-engaged AI
post — engagement scoring and topical relevance are separate problems, so
they're solved by separate layers. `scripts/x-watch-filter.py` is the
second layer: an LLM relevance judge that runs **after** `xq.py watch`
writes the digest and **before** the Slack notifier reads it.

Pipeline order (see `scripts/x-watch-cron.sh`):

```
xq.py watch  →  x-watch-filter.py  →  notify (osascript, if new posts)  →  x-watch-slack.py
```

- **Scope**: only `## 🔍 ` saved-search sections are judged. Handle
  sections (curated accounts you already chose to follow) always pass
  through untouched.
- Only entries actually displayed in the digest are judged — the trailing
  `…ほか N件` overflow line (posts beyond `limit_per_search`) is never sent
  to the judge and passes through unchanged.
- Because of that, `defaults.limit_per_search` in `watch.yaml` was raised
  from 15 to 25 when this filter was introduced, so more pre-filter
  candidates are shown (and therefore judged) per search.
- **Judgment**: each post is batched (≤50 per `claude -p --model haiku`
  call) and judged against three "worth catching up on" axes —
  A) AI構築 (agents/Claude Code/MCP/prompt design/model news/dev practice),
  B) ビジネス (automation/efficiency/AI-adoption case studies), and
  C) 人生・人間関係 (career & working style / learning & habit-building /
  partner relationships) — with an explicit exclusion list (politics,
  gossip, outrage-bait, celebrity news, incidents/accidents, war/
  propaganda, anti-AI sentiment wars, contentless reactions).
- **Bait guard (pre-LLM, deterministic)**: before the LLM judge runs,
  entries with `favs >= BAIT_GUARD_MIN_FAVES` (300) and
  `bookmarks / favs < BAIT_GUARD_MIN_BOOKMARK_RATIO` (0.03) are DROPped
  outright — decisively, without spending a judge call on them. Only
  applies where the digest's new header format exposed a bookmark count
  (`🔖`); old-format entries (`bookmarks` unparsed → `None`) are untouched.
  Rationale: on healthy content a meaningful share of people who favorite a
  post also bookmark it to come back to; a post racking up likes without
  that follow-through is a classic engagement-bait shape. Thresholds were
  calibrated on 2026-08-19/20 probes — the noise cluster sat at a 1.5-2.6%
  bookmark ratio versus a 4.8% floor for legitimate posts. Guard drops are
  tagged in the sidecar with `"bait-guard: 🔖率X%（♥favs/🔖bookmarks）"` so
  they're distinguishable from LLM-judged drops, and they count toward the
  digest's exclusion total the same way.
- **GitHub repo enrichment (pre-LLM)**: for any entry linking a
  `github.com/{owner}/{repo}` URL (subpaths like `/blob/`, `/issues/N`,
  `/pull/N`, `/releases/...` are truncated to the repo root; gist.github.com
  and the marketplace/sponsors/topics top-level paths are excluded), the
  script fetches repo metadata — stars, forks, an "age since published"
  velocity (`stars / age_days`, 1 decimal), days since the last push, and
  whether the repo is archived — via `gh api repos/{owner}/{repo}` (falling
  back to the unauthenticated GitHub REST API if `gh` isn't installed or
  fails, or once one `gh` call has already failed this run — after that, gh
  is skipped for the rest of the run and every remaining lookup goes
  straight to `requests`). This happens *before* the LLM judge and feeds
  it: the metrics are appended to the post text the judge sees (`[GH:
  owner/repo ⭐N 公開Nd 勢いN/d forkN pushNd前]`, with a trailing ` ARCHIVED`
  token appended only when the repo is archived), so an active,
  well-starred repo reads differently to the judge than a dead or
  newly-registered one. Kept entries that resolved a repo get a `⭐` line
  rendered directly under their URL line in the digest (`   ⭐ owner/repo
  N★（公開Nd・N/day・fork N・push Nd前）`, `・ARCHIVED` appended when
  archived); this line never matches `x_watch_lib.ENTRY_RE` or the cron
  notification grep, so it's invisible to the Slack parser and the "N new
  post(s)" notification count. Enrichment only ever runs on entries that
  survived the bait guard and reached the LLM judge — whether the judge
  then marked them KEEP or DROP — so the sidecar's `github: {repo, stars,
  age_days, velocity, archived}` object only ever appears on entries the
  LLM judge dropped; a bait-guard drop never carries one, since it was
  never enriched in the first place. Repos are memoized per run (a repo
  linked from multiple entries is fetched once) and total unique lookups
  are capped at `MAX_GITHUB_LOOKUPS_PER_RUN` (20, for unauthenticated
  GitHub API rate-limit safety — 60 req/hr) as well as at
  `GITHUB_ENRICH_BUDGET_SECONDS` (60, wall-clock) — this means **a
  scheduled watch run makes outbound calls to the GitHub API daily**
  whenever the digest contains repo links. Fail-open throughout: `gh`
  missing, a network error, a non-2xx response, a timeout, unparseable
  `created_at`/`pushed_at`, or any other unexpected error building one
  repo's metadata all just skip enrichment for that entry (no repo line,
  no judge-text addition, no sidecar `github` field) rather than affecting
  the rest of the run.
- **Fail-open by design**: any ambiguity resolves to KEEP. A missing or
  unparseable verdict line keeps that post; a failed/timed-out batch call
  keeps the entire batch; any unexpected exception in the script leaves the
  digest byte-for-byte unchanged and exits 0. `x-watch-cron.sh` also calls
  it with `|| true` as a second line of defense — this filter can reduce
  signal-to-noise, but it must never be able to break a scheduled run or
  silently eat a digest.
- **Audit trail**: every dropped post (id/url/author/section/favs/score/
  reason/text preview, plus a `github` object when the post linked a repo)
  is written to `output/excluded-YYYYMMDD.json`, even when nothing was
  dropped (empty list). Review this periodically — especially in the first
  week after a watch.yaml query change — to catch false drops. It's a plain
  file under `output/` (gitignored), never copied into the Obsidian vault,
  so it doesn't pollute the cc-evolve digest input.
- **Dry run**: `python3 scripts/x-watch-filter.py output/watch-YYYYMMDD.md --dry-run`
  prints a KEEP/DROP table plus a summary count and writes nothing (no
  digest rewrite, no sidecar) — use this to sanity-check a prompt or weight
  change before letting it touch a real digest.

## Watching accounts & searches

- Edit `watch.yaml` to add/remove handles (`handles: [{handle, note}]`) and
  saved keyword searches (`searches: [{name, query, note}]`), plus the
  default per-entry digest size (`defaults.limit_per_handle`,
  `defaults.limit_per_search`). An entry missing its required key
  (`handle` for handles; `name` or `query` for searches) is skipped with a
  stderr warning, not a crash. `searches:` is optional — a watch.yaml without
  it behaves exactly as before (handles only).
- `xq.py watch` runs all handles first, then all saved searches, into one
  digest: a handles section, a `---` separator (only when both groups are
  non-empty), then a searches section.
  - Handles fetch `from:HANDLE`; searches use each entry's `query` as-is.
    Both compose `{query} since:DATE` and page through **every** post newer
    than that entry's `last_checked` — it does not stop at
    `limit_per_handle`/`limit_per_search`, so no new post is silently
    dropped just because an entry was unusually active. Those limits only
    bound how many are *shown* in the markdown digest; anything beyond that
    is summarized as `…ほか N件（output/のJSON参照）`, and the full set is
    still in the saved `output/xq-watch-*.json`.
  - Handles display in API (recency) order; searches display sorted by
    composite engagement score descending (see "Engagement scoring" above),
    with heading `## 🔍 {name}` (` — {note}` appended if given).
  - **Cross-section dedup (2026-08-24)**: a tweet is displayed at most once
    per digest. Handles render first and claim their displayed IDs, then
    searches in watch.yaml order — so when the same tweet matches several
    queries, the first (most specific) section keeps it and later sections
    promote their next-ranked tweets into the freed slots. Only *displayed*
    entries claim IDs: a tweet hidden behind one section's overflow cut can
    still appear in a later section. The saved `output/xq-watch-*.json` is
    NOT deduplicated (it archives everything fetched, per query).
  - State is tracked per entry in `logs/watch-state.json`: handles under
    their handle string, searches under `search:{name}` (namespaced so the
    two never collide). First run (no state for that entry) looks back 7
    days; a corrupted/unparseable `last_checked` also falls back to 7 days,
    with a stderr warning, instead of crashing.
- Pagination for one entry stops when a page contains a tweet at/before the
  window start, `next_cursor` runs out, or the `MAX_PAGES` safety bound (10)
  is hit. Hitting `MAX_PAGES` prints a stderr warning that some new posts may
  be missing this run, but still advances `last_checked` (accepts the small
  loss rather than getting stuck retrying the same entry forever).
- One handle's or search's failure (fetch error or any unexpected exception)
  never aborts the rest of the run — it renders as an `[error]` line in that
  entry's own section, and the run still saves state/usage for everything
  else.
- A daily automated run (08:47 JST) can be installed via
  `launchd/com.fuma.x-watch.plist.template` (see the comment at the top of
  that file for the `launchctl bootstrap` install command — registration is
  a manual, human step, not automatic) plus `scripts/x-watch-cron.sh`, which
  runs `xq.py watch`, applies `scripts/x-watch-filter.py` in place (see "LLM
  relevance filter" above), fires a macOS notification when the digest
  contains new posts, then posts the Slack summary.

## Troubleshooting

- Python-level errors from a scheduled `watch` run (exceptions, tracebacks,
  the retry/fallback warnings above): check `logs/x-watch-cron.log`.
- launchd/bash-level errors (script failed to start, wrong path, `python3`
  not found, etc.): check `logs/x-watch-launchd.err.log`.
