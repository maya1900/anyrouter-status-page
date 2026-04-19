#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "Missing GITHUB_TOKEN" >&2
  exit 1
fi

OWNER="${GITHUB_OWNER:-maya1900}"
REPO="${GITHUB_REPO:-anyrouter-status-page}"
EVENT_TYPE="${GITHUB_EVENT_TYPE:-status-check}"

curl --fail-with-body \
  -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/${OWNER}/${REPO}/dispatches" \
  -d "$(printf '{"event_type":"%s"}' "${EVENT_TYPE}")"

echo
echo "repository_dispatch sent: ${OWNER}/${REPO} (${EVENT_TYPE})"
