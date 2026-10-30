#!/usr/bin/env bash
# Project-local tools in .tools/bin (gitignored; no brew, nothing system-wide).
#   scripts/tools.sh        (make tools)
# kubeconform v0.7.0 from its GitHub release, checked against pinned sha256 sums.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
VERSION=v0.7.0
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"; case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
asset="kubeconform-$os-$arch.tar.gz"
case "$asset" in
  kubeconform-darwin-arm64.tar.gz) sum=b5d32b2cb77f9c781c976b20a85e2d0bc8f9184d5d1cfe665a2f31a19f99eeb9 ;;
  kubeconform-darwin-amd64.tar.gz) sum=c6771cc894d82e1b12f35ee797dcda1f7da6a3787aa30902a15c264056dd40d4 ;;
  kubeconform-linux-amd64.tar.gz)  sum=c31518ddd122663b3f3aa874cfe8178cb0988de944f29c74a0b9260920d115d3 ;;
  kubeconform-linux-arm64.tar.gz)  sum=cc907ccf9e3c34523f0f32b69745265e0a6908ca85b92f41931d4537860eb83c ;;
  *) echo "tools: no pinned kubeconform for $asset" >&2; exit 1 ;;
esac
mkdir -p .tools/bin
if [[ -x .tools/bin/kubeconform ]] && .tools/bin/kubeconform -v | grep -q "$VERSION"; then
  echo "tools: kubeconform $VERSION present"; exit 0
fi
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
curl -sSfL -o "$tmp/$asset" "https://github.com/yannh/kubeconform/releases/download/$VERSION/$asset"
echo "$sum  $tmp/$asset" | shasum -a 256 -c - >/dev/null
tar -xzf "$tmp/$asset" -C "$tmp" kubeconform
mv "$tmp/kubeconform" .tools/bin/kubeconform
echo "tools: kubeconform $VERSION -> .tools/bin (sha256 ok)"
