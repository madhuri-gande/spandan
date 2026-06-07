#!/usr/bin/env bash
# Spandan — one-shot EC2 bootstrap.
#
# Runs on a fresh Amazon Linux 2023 / Ubuntu 22+ host. Installs system
# packages, creates a Python venv, fetches MailPit, trains the donor
# ranking model, and (optionally) installs a systemd unit so the stack
# auto-starts on boot.
#
# Usage:
#   sudo ./deploy/ec2-setup.sh                    # base install
#   sudo ./deploy/ec2-setup.sh --systemd          # base install + service
#
# Re-run safely — every step is idempotent.

set -euo pipefail

INSTALL_SYSTEMD=0
for arg in "$@"; do
    case "$arg" in
        --systemd) INSTALL_SYSTEMD=1 ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

echo "==> Installing OS packages ..."
if command -v dnf >/dev/null 2>&1; then           # Amazon Linux 2023 / Fedora
    sudo dnf -y install python3.11 python3.11-pip sqlite tar gzip curl git
elif command -v apt-get >/dev/null 2>&1; then     # Ubuntu / Debian
    sudo apt-get update -y
    sudo apt-get install -y python3.11 python3.11-venv python3-pip sqlite3 tar gzip curl git
else
    echo "Neither dnf nor apt-get found. Install python3.11 + sqlite3 + tar manually."
    exit 1
fi

echo "==> Creating Python venv ..."
if [ ! -d venv ]; then
    python3.11 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt

echo "==> Fetching MailPit ..."
./tools/install_mailpit.sh

echo "==> Training donor ranking model (one-time, ~5s) ..."
python services/ranking.py || {
    echo "    Warning: model training failed. The agent will still run, but"
    echo "    will fall back to random ranking until you re-train."
}

echo "==> Loading dataset into DynamoDB (idempotent) ..."
python data/load_dataset.py || {
    echo "    Warning: dataset load failed. Check AWS credentials / IAM role."
}

mkdir -p logs data

if [ "$INSTALL_SYSTEMD" = "1" ]; then
    echo "==> Installing systemd service ..."
    USER_NAME="${SUDO_USER:-$USER}"
    SERVICE_PATH=/etc/systemd/system/spandan.service
    sudo tee "$SERVICE_PATH" > /dev/null <<EOF
[Unit]
Description=Spandan Streamlit + MailPit stack
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_ROOT
EnvironmentFile=$PROJECT_ROOT/.env
ExecStart=$PROJECT_ROOT/run-stack.sh
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable spandan.service
    sudo systemctl restart spandan.service
    echo "    Service installed. Status: sudo systemctl status spandan"
fi

cat <<EOF

============================================================
Spandan EC2 setup complete.

  Project root:  $PROJECT_ROOT
  Python venv:   $PROJECT_ROOT/venv
  MailPit:       $PROJECT_ROOT/tools/mailpit

Next steps:
  1. Make sure $PROJECT_ROOT/.env exists. Copy from .env.example
     and fill in real values (or use IAM role + leave AWS keys blank).
  2. Open port 8501 in the EC2 Security Group (Streamlit dashboard).
  3. Start the stack:
       ./run-stack.sh                # foreground (for first-time test)
       sudo systemctl start spandan  # background (if you used --systemd)

Dashboard URL:
       http://<ec2-public-ip>:8501
============================================================
EOF
