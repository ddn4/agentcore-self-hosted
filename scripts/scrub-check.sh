#!/usr/bin/env bash
# Fail if tracked files (or, with --history, any commit) contain secrets,
# AWS account IDs, ARNs with account numbers, or AWS resource IDs.
# Uses gitleaks when installed, and always runs the grep-based scrub below.
# Usage: scripts/scrub-check.sh [--history]
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

mode="${1:-tree}"
status=0

# Patterns (extended regex). Keep in sync with scripts/redact.sh.
patterns=(
  'arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:[0-9]{12}:'                 # ARN with an account number
  '(^|[^0-9A-Za-z<._-])[0-9]{12}([^0-9A-Za-z>._-]|$)'               # bare 12-digit account ID
  '(^|[^0-9A-Za-z])(vpc|subnet|sg|eni|igw|rtb|vpce|eipalloc)-[0-9a-f]{8}([0-9a-f]{9})?([^0-9A-Za-z]|$)'  # AWS resource IDs
  '(AKIA|ASIA)[A-Z0-9]{16}'                                     # AWS access key IDs
  '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----'                           # private keys
  'runtime/[A-Za-z][A-Za-z0-9_]{0,47}-[A-Za-z0-9]{10}([^A-Za-z0-9]|$)'            # AgentCore runtime IDs
  'aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}'                  # secret keys
)
# v0.8: personal data and network locations (the budget email, the laptop's public IP for
# the EKS endpoint CIDR) must never be committed either. Matches on these lines are then
# filtered by the allowlist below (documentation and test placeholders, private ranges).
patterns+=(
  '[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'   # email address
  '(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)'                  # IPv4 address
)
regex="$(IFS='|'; echo "${patterns[*]}")"
# Hits that are only allowlisted emails or private/loopback/documentation IPs are fine.
allow_line() {
  local line="$1" rest
  rest="$(printf '%s' "$line" | sed -E \
    -e 's/[A-Za-z0-9._%+-]+@(example\.(com|org|invalid)|anthropic\.com|users\.noreply\.github\.com)//g' \
    -e 's/(^|[^0-9.])(127\.[0-9]+\.[0-9]+\.[0-9]+|0\.0\.0\.0|10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+|169\.254\.[0-9]+\.[0-9]+|255\.255\.[0-9]+\.[0-9]+)([^0-9.]|$)/\1\3/g')"
  ! printf '%s' "$rest" | grep -qE -- "$regex"
}

# Files that legitimately contain digit runs (lockfile hashes, this script's
# own patterns). Add paths here only with a reason.
excludes=(':!uv.lock' ':!scripts/scrub-check.sh' ':!scripts/redact.sh')

# Known-fake literals that are in already-committed history (never rewritten).
# --history only: the tree scan must still reject them if they come back.
#   AKIAOFFLINETEST00000  dummy credential in commit 08f8ba9 (test_wire_offline.py, v0.2);
#                         not a real key (contains 0, outside AWS's base32 alphabet).
history_allow=('AKIAOFFLINETEST00000')

if [[ "$mode" == "--history" ]]; then
  hits="$(git log -p --all --no-color --format=%B -- . "${excludes[@]}" | grep -nE -- "$regex" || true)"
  for lit in "${history_allow[@]}"; do
    hits="$(printf '%s\n' "$hits" | grep -vF -- "$lit" || true)"
  done
else
  hits="$(git grep -nIE -- "$regex" -- . "${excludes[@]}" || true)"
  # Untracked-but-not-ignored files would be committed next: scan them too.
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    more="$(grep -nIE -- "$regex" "$f" | sed "s|^|$f:|" || true)"
    [[ -n "$more" ]] && hits+=$'\n'"$more"
  done < <(git ls-files --others --exclude-standard -- . "${excludes[@]}")
fi
hits="$(printf '%s\n' "$hits" | sed '/^$/d' | while IFS= read -r l; do allow_line "$l" || printf '%s\n' "$l"; done)"

if [[ -n "$hits" ]]; then
  echo "scrub-check: FAILED. Possible account IDs, resource IDs or secrets:" >&2
  # Show where, but redact the match itself.
  printf '%s\n' "$hits" | scripts/redact.sh | sed 's/^/  /' >&2
  status=1
else
  echo "scrub-check: grep scrub clean ($mode)"
fi

if command -v gitleaks >/dev/null 2>&1; then
  if [[ "$mode" == "--history" ]]; then
    gitleaks git --no-banner --redact . || status=1
  else
    # Scan only what git would commit (tracked + untracked-not-ignored), not
    # .venv/.temporal/.certs: copy those files to a temp dir and scan that.
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    git ls-files -co --exclude-standard -z \
      | while IFS= read -r -d '' f; do [[ -f "$f" ]] && printf '%s\0' "$f"; done \
      | tar --null -T - -cf - | tar -xf - -C "$tmp"
    gitleaks dir --no-banner --redact "$tmp" || status=1
  fi
else
  echo "scrub-check: gitleaks not installed; grep scrub only (CI runs gitleaks)"
fi
exit $status
