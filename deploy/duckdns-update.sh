#!/usr/bin/env bash
# Point a DuckDNS subdomain at this EC2 instance's public IP.
#
# One-time setup (5 min):
#   1. Go to https://www.duckdns.org and sign in (Google/GitHub).
#   2. Create subdomain "spandan-demo" → you get spandan-demo.duckdns.org
#   3. Copy your token from the DuckDNS dashboard.
#   4. Add to ~/spandan/.env on EC2:
#        DUCKDNS_TOKEN=<your-token>
#        DUCKDNS_DOMAIN=spandan-demo
#        PUBLIC_BASE_URL=http://spandan-demo.duckdns.org
#   5. Run: ./deploy/duckdns-update.sh
#   6. Update REPLY_BASE_URL and MAILPIT_UI_URL to use PUBLIC_BASE_URL,
#      then: sudo systemctl restart spandan
#
# Re-run after EC2 stop/start (IP changes) or add to cron.

set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[ -f .env ] && source .env

TOKEN="${DUCKDNS_TOKEN:-}"
DOMAIN="${DUCKDNS_DOMAIN:-spandan-demo}"
IP="${1:-$(curl -s https://checkip.amazonaws.com | tr -d '[:space:]')}"

if [ -z "$TOKEN" ]; then
    echo "Set DUCKDNS_TOKEN in .env (get it from https://www.duckdns.org)"
    exit 1
fi

echo "==> Pointing ${DOMAIN}.duckdns.org → ${IP}"
curl -s "https://www.duckdns.org/update?domains=${DOMAIN}&token=${TOKEN}&ip=${IP}"
echo
echo "Done. Test: curl -sI http://${DOMAIN}.duckdns.org/_stcore/health"
