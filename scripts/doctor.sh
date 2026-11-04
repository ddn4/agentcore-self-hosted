#!/usr/bin/env bash
# Check the workstation against docs/versions.md (plan section 3.1, v0.1 doctor list).
# Exit non-zero if a REQUIRED check fails. Optional checks only warn.
#   DOCTOR_SKIP_AWS=1   skip the AWS identity check (Bedrock credentials; CI never has them)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

fail=0
ok()   { printf '  ok    %-22s %s\n' "$1" "$2"; }
warn() { printf '  WARN  %-22s %s\n' "$1" "$2"; }
bad()  { printf '  FAIL  %-22s %s\n' "$1" "$2"; fail=1; }

# version_ge A B: true if A >= B (dotted numeric)
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }

pin() { grep -E "^\| *$1 *\|" docs/versions.md | head -1 | awk -F'|' '{gsub(/ /,"",$3); print $3}'; }

echo "doctor: required"
want_cli="$(pin 'Temporal CLI')"
if command -v temporal >/dev/null; then
  v="$(temporal --version | sed -E 's/temporal version ([^ ]+).*/\1/')"
  [[ "$v" == "$want_cli" ]] && ok temporal "$(temporal --version)" || bad temporal "have $v, want $want_cli"
else bad temporal "not installed (want $want_cli)"; fi

if command -v uv >/dev/null; then ok uv "$(uv --version)"; else bad uv "not installed"; fi

py="$(uv run --no-sync python -c 'import platform;print(platform.python_version())' 2>/dev/null || true)"
[[ "$py" == 3.12.* ]] && ok python "$py (uv)" || bad python "want 3.12.x via uv, have '${py:-none}' (uv python install 3.12)"

tio="$(uv run --no-sync python -c 'import importlib.metadata as m;print(m.version("temporalio"))' 2>/dev/null || true)"
if [[ -n "$tio" ]] && version_ge "$tio" 1.34.0; then ok temporalio "$tio (>= 1.34.0)"; else bad temporalio "want >= 1.34.0, have '${tio:-none}' (make setup)"; fi
uv run --no-sync python -c 'from temporalio.strands_agents import StrandsPlugin' 2>/dev/null \
  && ok strands-plugin "temporalio.strands_agents importable" || bad strands-plugin "temporalio.strands_agents not importable"

echo "doctor: later tags (warn only at v0.1)"
command -v kind    >/dev/null && ok kind "$(kind version | awk '{print $2}')" || warn kind "not installed (v0.9)"
command -v helm    >/dev/null && ok helm "$(helm version --short)" || warn helm "not installed (v0.9)"
command -v kubectl >/dev/null && ok kubectl "$(kubectl version --client 2>/dev/null | head -1 | awk '{print $3}')" || warn kubectl "not installed (v0.9)"
command -v docker  >/dev/null && { DOCKER_CONTEXT=colima-agentcore docker info >/dev/null 2>&1 \
  && ok docker "context colima-agentcore reachable" || warn docker "context colima-agentcore not running (needed from v0.9)"; } \
  || warn docker "not installed"
command -v gitleaks >/dev/null && ok gitleaks "$(gitleaks version)" || warn gitleaks "not installed: scrub-check uses grep only (CI runs gitleaks)"

echo "doctor: AWS"
if [[ "${DOCTOR_SKIP_AWS:-0}" == "1" ]]; then
  warn aws "skipped (DOCTOR_SKIP_AWS=1)"
elif ! command -v aws >/dev/null; then
  bad aws "AWS CLI v2 not installed"
else
  region="${AWS_REGION:-us-east-1}"
  if arn="$(aws sts get-caller-identity --region "$region" --query Arn --output text 2>/dev/null)"; then
    ok aws "identity $(printf "%s" "$arn" | scripts/redact.sh | sed -E "s#(assumed-role/[^/]*)/.*#\1/<session>#; s#AWSReservedSSO_([A-Za-z]+)_[0-9a-f]+#AWSReservedSSO_\1_<id>#") in $region"
  else
    bad aws "no valid credentials (aws sso login)"
  fi
fi

[[ $fail -eq 0 ]] && echo "doctor: OK" || echo "doctor: FAILED"
exit $fail
