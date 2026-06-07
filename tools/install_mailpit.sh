#!/usr/bin/env bash
# Spandan — MailPit installer.
#
# Detects the host OS / arch and downloads the matching MailPit binary
# (https://github.com/axllent/mailpit/releases) into ./tools/mailpit.
# Runs idempotently — re-execute any time to refresh.
#
# Usage:
#   ./tools/install_mailpit.sh                # install pinned version
#   MAILPIT_VERSION=v1.30.1 ./tools/install_mailpit.sh

set -euo pipefail

MAILPIT_VERSION="${MAILPIT_VERSION:-v1.30.1}"

cd "$(dirname "$0")"

uname_s="$(uname -s)"
uname_m="$(uname -m)"

case "$uname_s" in
    Darwin)  os="darwin" ;;
    Linux)   os="linux" ;;
    *) echo "Unsupported OS: $uname_s. Manual download: https://github.com/axllent/mailpit/releases"; exit 1 ;;
esac

case "$uname_m" in
    x86_64|amd64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) echo "Unsupported arch: $uname_m"; exit 1 ;;
esac

asset="mailpit-${os}-${arch}.tar.gz"
url="https://github.com/axllent/mailpit/releases/download/${MAILPIT_VERSION}/${asset}"

echo "==> Downloading MailPit ${MAILPIT_VERSION} (${os}/${arch}) ..."
echo "    $url"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tmp/$asset"
elif command -v wget >/dev/null 2>&1; then
    wget -q "$url" -O "$tmp/$asset"
else
    echo "Need curl or wget on PATH."; exit 1
fi

tar -xzf "$tmp/$asset" -C "$tmp"
mv "$tmp/mailpit" ./mailpit
chmod +x ./mailpit

echo "==> Installed: $(pwd)/mailpit"
./mailpit version || true
