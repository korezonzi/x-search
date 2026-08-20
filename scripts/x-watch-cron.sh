#!/usr/bin/env bash
# x-watch-cron.sh — daily X watch runner, invoked by launchd
# (see launchd/com.fuma.x-watch.plist.template for the schedule).
#
# Runs `xq.py watch`, writes the digest to output/watch-YYYYMMDD.md, applies
# the LLM relevance filter (x-watch-filter.py), appends stderr to
# logs/x-watch-cron.log, and fires a macOS notification if the digest
# contains at least one new post.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
X_SEARCH_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# launchd runs with a minimal PATH, so `command -v python3` may resolve to the
# system interpreter that lacks `requests`. Probe known locations and pick the
# first interpreter that can actually import our dependency.
PYTHON_BIN=""
for cand in /usr/local/bin/python3 /opt/homebrew/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
    "$(command -v python3 || true)"; do
  if [[ -n "${cand}" && -x "${cand}" ]] && "${cand}" -c 'import requests' 2>/dev/null; then
    PYTHON_BIN="${cand}"
    break
  fi
done
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [error] no python3 with 'requests' found" >&2
  exit 1
fi

cd "${X_SEARCH_DIR}"
mkdir -p output logs

OUTPUT_FILE="output/watch-$(date +%Y%m%d).md"

"${PYTHON_BIN}" xq.py watch --format md >"${OUTPUT_FILE}" 2>>logs/x-watch-cron.log

# Apply the LLM relevance filter in place (drops low-value posts from the
# "## 🔍 " search sections; handles pass through untouched). Fail-open by
# design — on any internal failure it leaves the digest unchanged and exits
# 0 — and `|| true` here is a second line of defense so a filter crash can
# never abort the watch run.
"${PYTHON_BIN}" "${SCRIPT_DIR}/x-watch-filter.py" "${OUTPUT_FILE}" 2>>logs/x-watch-cron.log || true

# Notify only when the digest contains at least one new-post entry
# (format_md_entry lines look like "1. @handle (...) ...").
if grep -qE '^[0-9]+\. @' "${OUTPUT_FILE}" 2>/dev/null; then
  NEW_COUNT=$(grep -cE '^[0-9]+\. @' "${OUTPUT_FILE}")
  osascript -e "display notification \"${NEW_COUNT} new post(s) — ${OUTPUT_FILE}\" with title \"X Watch\"" || true
fi

# Post a Slack summary (optional — skips silently when no webhook is configured;
# never fails the watch run).
"${PYTHON_BIN}" "${SCRIPT_DIR}/x-watch-slack.py" "${OUTPUT_FILE}" 2>>logs/x-watch-cron.log || true
