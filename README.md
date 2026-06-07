# Spandan — Autonomous Blood Support Network

An always-on AI agent that detects upcoming patient transfusions, ranks
donors with ML, drafts multilingual outreach via Amazon Bedrock, and
escalates to additional donors automatically — without human nudging.

> **Spandan** (स्पंदन) — Sanskrit for *heartbeat / pulse*. The system
> keeps the rhythm between patients who need blood and donors who can
> provide it.

---

## What it does

| Capability | Details |
|---|---|
| **Forecast demand** | For every patient (`bridge`), predict the next transfusion date by rolling their personal `frequency_in_days` cadence forward from `last_transfusion_date`. See `services/forecasting.py`. |
| **Rank donors with ML** | Logistic regression trained on the dataset (`services/ranking.py`). Features: calls-to-donations ratio, lifetime donations, eligibility, donor type (regular / one-time), call history. Output: probability of YES response. AUC ~0.7 on holdout. |
| **Sequential outreach** | One donor at a time per patient. The agent emails donor #1, waits `DONOR_WAIT_SECONDS`, and only emails donor #2 if no reply. Prevents over-mobilization. |
| **Multilingual messages** | Bedrock Claude Haiku 4.5 generates outreach + reminders in Telugu, Hindi, Tamil, English. Same model classifies replies (YES / NO / CANCEL). |
| **Magic-link replies** | Every email has signed YES / NO buttons that hit `/Reply` on the dashboard. No login needed for donors. |
| **Surge mode** | One-shot emergency button that emails every eligible donor for a patient simultaneously. Includes an "already covered" guard so over-confirmed donors get a polite thank-you. |
| **Auto reminders** | After a donor confirms YES, the agent automatically follows up with a reconfirm reminder `REMINDER_DELAY_SECONDS` later. |
| **Clock-skew tolerance** | Boto3's SigV4 signer is patched to detect `InvalidSignatureException` from drifted laptop clocks (Mac sleep/wake) and auto-correct. See `services/db.py`. |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Streamlit dashboard (app/) ── coordinator login             │
│   ├─ KPIs (donors, bridges, donations 24h)                  │
│   ├─ Patient pipeline (next 90 days)                        │
│   ├─ Manual actions (advance / process / email next)        │
│   └─ Surge expander (emergency parallel blast)              │
└──────────────┬──────────────────────────────────────────────┘
               │
┌──────────────▼─────────────┐    ┌────────────────────────┐
│ services/agent.py          │───▶│ AWS DynamoDB           │
│  - background thread       │    │ donors / bridges /     │
│  - per-patient state mach  │    │ messages / agent_log / │
│  - cooldowns + dedup       │    │ donations              │
└──────┬─────────┬───────────┘    └────────────────────────┘
       │         │
┌──────▼──┐  ┌───▼────────┐    ┌─────────────────────────┐
│ Bedrock │  │ MailPit /  │───▶│ Donor inbox (HTML email │
│ Claude  │  │ Amazon SES │    │ with magic YES/NO link) │
│ Haiku   │  └────────────┘    └─────────────────────────┘
└─────────┘
```

---

## Run locally

Prerequisites: macOS or Linux, Python 3.11, AWS account with Bedrock
+ DynamoDB access.

```bash
# 1. Clone + enter
git clone https://github.com/madhuri-gande/spandan.git
cd spandan

# 2. Python deps
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Local mail server (downloads ~25 MB MailPit binary)
./tools/install_mailpit.sh

# 4. Configure
cp .env.example .env
# edit .env — fill in AWS keys, set REPLY_TOKEN_SECRET, etc.

# 5. Load the patient + donor dataset into DynamoDB (one-time)
python data/load_dataset.py

# 6. Train donor ranking model (one-time, ~5 sec)
python services/ranking.py

# 7. Boot the stack
./run-stack.sh
```

Open:
- **Dashboard:** http://localhost:8501
- **MailPit inbox:** http://localhost:8025

---

## Deploy to EC2

### 1. Launch the instance

| Setting | Value |
|---|---|
| AMI | Amazon Linux 2023 (or Ubuntu 22.04) |
| Type | `t3.small` (2 vCPU, 2 GB) — fits comfortably |
| Storage | 20 GB gp3 |
| Security Group | inbound `22` (SSH from your IP), `80` (HTTP/nginx from `0.0.0.0/0`) |
| IAM Role | `SpandanEC2Role` with policies: `AmazonDynamoDBFullAccess`, `AmazonBedrockFullAccess`, optionally `AmazonS3FullAccess` if you use S3 for ML model checkpoints |

### 2. Bootstrap

```bash
# SSH in
ssh -i ~/.ssh/spandan.pem ec2-user@<public-ip>

# Clone
sudo dnf install -y git
git clone https://github.com/madhuri-gande/spandan.git
cd spandan

# Configure (use IAM role — leave AWS_ACCESS_KEY_ID blank)
cp .env.example .env
nano .env  # fill in REPLY_TOKEN_SECRET, AUTH_COORDINATOR_PASSWORD, etc.
#         # change REPLY_BASE_URL=http://<public-ip>/Reply

# One-shot install + start as systemd service
sudo ./deploy/ec2-setup.sh --systemd
```

### 3. Verify

```bash
sudo systemctl status spandan        # should show "active (running)"
sudo journalctl -u spandan -f        # tail logs
```

Visit `http://<public-ip>` — login with the password from your `.env`.

### 4. Optional hardening

- Put a reverse proxy (nginx / Caddy) in front for HTTPS via Let's Encrypt.
- Restrict the security group `80` source to specific IPs during the demo.
- Switch `DELIVERY_BACKEND=ses` and verify a sender domain in Amazon SES
  for real outbound email instead of MailPit.

---

## Project layout

```
spandan/
├── app/                       Streamlit pages (Coordinator, Reply)
├── services/
│   ├── agent.py               Autonomous orchestrator (background thread)
│   ├── forecasting.py         Predicts next-transfusion dates per patient
│   ├── ranking.py             ML donor scoring (LogisticRegression)
│   ├── bedrock_chat.py        Claude Haiku for messages + intent
│   ├── delivery.py            Pluggable SMTP / SES email backend
│   ├── db.py                  DynamoDB tables + clock-skew handling
│   ├── auth.py                Coordinator login (streamlit-authenticator)
│   ├── demo_seed.py           Demo data helpers (un/seed)
│   └── email_template.py      HTML + plain-text email templates
├── data/
│   ├── load_dataset.py        Bulk-loads Dataset.csv into DynamoDB
│   └── backfill_contacts.py   Adds emails to legacy donor rows
├── tools/
│   ├── install_mailpit.sh     Cross-platform MailPit downloader
│   └── seed_pipeline.py       CLI seeder (alt to dashboard button)
├── deploy/
│   └── ec2-setup.sh           One-shot EC2 bootstrap
├── run-stack.sh               Local dev launcher (Streamlit + MailPit)
├── requirements.txt
├── .env.example               Env template (NO secrets)
└── README.md                  ← you are here
```

---

## Tech stack

- **AWS:** DynamoDB (state), Bedrock (Claude Haiku 4.5), EC2 (compute), S3 (model artifacts), IAM Roles (auth)
- **Python:** Streamlit, boto3, pandas, scikit-learn
- **Local dev:** [MailPit](https://github.com/axllent/mailpit) catch-all SMTP

---

## Acknowledgements

Built for the Blood Warriors hackathon. Dataset provided by the
organizers; scrubbed of PII before commit.
