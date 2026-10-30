#!/usr/bin/env bash
# Project-local tools in .tools/ (gitignored; no brew, nothing system-wide).
#   scripts/tools.sh                (make tools)       kubeconform, terraform, tflint
#   scripts/tools.sh agentcore      (make tools-agentcore)   + the AgentCore CLI (npm, v0.8)
# Every binary comes from its vendor's release page and is checked against the sha256
# pinned below (copied from the vendor's published checksum file). Versions: docs/versions.md.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

KUBECONFORM_VERSION=v0.7.0
TERRAFORM_VERSION=1.17.0
TFLINT_VERSION=v0.64.0
AGENTCORE_CLI_VERSION=1.0.1

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
mkdir -p .tools/bin
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

fetch() {  # url sum file
  curl -sSfL -o "$tmp/$3" "$1"
  echo "$2  $tmp/$3" | shasum -a 256 -c - >/dev/null
}

# --- kubeconform (v0.9) ---
asset="kubeconform-$os-$arch.tar.gz"
case "$asset" in
  kubeconform-darwin-arm64.tar.gz) sum=b5d32b2cb77f9c781c976b20a85e2d0bc8f9184d5d1cfe665a2f31a19f99eeb9 ;;
  kubeconform-darwin-amd64.tar.gz) sum=c6771cc894d82e1b12f35ee797dcda1f7da6a3787aa30902a15c264056dd40d4 ;;
  kubeconform-linux-amd64.tar.gz)  sum=c31518ddd122663b3f3aa874cfe8178cb0988de944f29c74a0b9260920d115d3 ;;
  kubeconform-linux-arm64.tar.gz)  sum=cc907ccf9e3c34523f0f32b69745265e0a6908ca85b92f41931d4537860eb83c ;;
  *) echo "tools: no pinned kubeconform for $asset" >&2; exit 1 ;;
esac
if [[ -x .tools/bin/kubeconform ]] && .tools/bin/kubeconform -v | grep -q "$KUBECONFORM_VERSION"; then
  echo "tools: kubeconform $KUBECONFORM_VERSION present"
else
  fetch "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM_VERSION/$asset" "$sum" "$asset"
  tar -xzf "$tmp/$asset" -C "$tmp" kubeconform
  mv "$tmp/kubeconform" .tools/bin/kubeconform
  echo "tools: kubeconform $KUBECONFORM_VERSION -> .tools/bin (sha256 ok)"
fi

# --- terraform (v0.8; releases.hashicorp.com terraform_<v>_SHA256SUMS) ---
asset="terraform_${TERRAFORM_VERSION}_${os}_${arch}.zip"
case "$asset" in
  *_darwin_arm64.zip) sum=7472a54cca911d46c0e399a5ebbce44444912d0d4dad7495f37f1fa51e7ca6b7 ;;
  *_darwin_amd64.zip) sum=3773397524196e4ebbf7167e2b9fb478dff1962d555d649f616ba97ca446a143 ;;
  *_linux_amd64.zip)  sum=c168cab7c637a8769fdb90089ddd19de701bc6ff8aca64dc2e2185fe46f59f67 ;;
  *_linux_arm64.zip)  sum=f32478134ffc6d710eec474bbd316652cdddc0f4e336973a3745e8d3a3309cdf ;;
  *) echo "tools: no pinned terraform for $asset" >&2; exit 1 ;;
esac
if [[ -x .tools/bin/terraform ]] && .tools/bin/terraform version | head -1 | grep -q "v$TERRAFORM_VERSION\$"; then
  echo "tools: terraform $TERRAFORM_VERSION present"
else
  fetch "https://releases.hashicorp.com/terraform/$TERRAFORM_VERSION/$asset" "$sum" "$asset"
  unzip -oq "$tmp/$asset" terraform -d "$tmp"
  mv "$tmp/terraform" .tools/bin/terraform
  echo "tools: terraform $TERRAFORM_VERSION -> .tools/bin (sha256 ok)"
fi

# --- tflint (v0.8; GitHub release checksums.txt) ---
asset="tflint_${os}_${arch}.zip"
case "$asset" in
  tflint_darwin_arm64.zip) sum=2496e9cb3d24992d553b45e7c87a0fdc9449ca975233876247a9bfeda857e6c0 ;;
  tflint_darwin_amd64.zip) sum=0f3a9fd17526014646a2dfc3f9122f7b4161abe3d6b0f0f03f9014483ddf4d19 ;;
  tflint_linux_amd64.zip)  sum=cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537 ;;
  tflint_linux_arm64.zip)  sum=560da89aacf59389d4eb029730dd5b109b7288096c32f2726a0d9e783a5ea8eb ;;
  *) echo "tools: no pinned tflint for $asset" >&2; exit 1 ;;
esac
if [[ -x .tools/bin/tflint ]] && .tools/bin/tflint --version | head -1 | grep -q "${TFLINT_VERSION#v}"; then
  echo "tools: tflint $TFLINT_VERSION present"
else
  fetch "https://github.com/terraform-linters/tflint/releases/download/$TFLINT_VERSION/$asset" "$sum" "$asset"
  unzip -oq "$tmp/$asset" tflint -d "$tmp"
  mv "$tmp/tflint" .tools/bin/tflint
  echo "tools: tflint $TFLINT_VERSION -> .tools/bin (sha256 ok)"
fi

# --- AgentCore CLI (v0.8; only on request: needs Node >= 20.12, ~750 npm packages) ---
# Exact version into .tools/agentcore-cli (npm checks each tarball's registry integrity).
# Run it through scripts/agentcore.sh, which keeps its config in the repo and telemetry off.
if [[ "${1:-}" == agentcore ]]; then
  dir=.tools/agentcore-cli
  if [[ -x "$dir/node_modules/.bin/agentcore" ]] &&
     [[ "$(AGENTCORE_TELEMETRY_DISABLED=1 "$dir/node_modules/.bin/agentcore" --version 2>/dev/null | tail -1)" == "$AGENTCORE_CLI_VERSION" ]]; then
    echo "tools: @aws/agentcore $AGENTCORE_CLI_VERSION present"
  else
    mkdir -p "$dir"
    npm install --prefix "$dir" --no-fund --no-audit --save-exact "@aws/agentcore@$AGENTCORE_CLI_VERSION" >/dev/null
    echo "tools: @aws/agentcore $AGENTCORE_CLI_VERSION -> $dir"
  fi
fi
