# Spandan · Demo Runbook

5-minute hackathon walkthrough. All inline tools/scripts assume you're at the repo root: `spandan/`.

## One-time setup

```bash
cd spandan
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# .env is already configured — review AUTH_COORDINATOR_PASSWORD before going live
```

## Start the stack (single command)

```bash
./run-stack.sh
```

This launches:
- **Streamlit dashboard** → http://localhost:8501
- **MailPit inbox**       → http://localhost:8025

Press `Ctrl-C` once to bring both down cleanly.

## Login

| Field | Value |
|---|---|
| URL | http://localhost:8501 → Coordinator Dashboard |
| Username | `coordinator` |
| Password | `spandan@2026` (see `.env`) |

Home page is public; Coordinator + Bridge View require login. Donor replies go through `/Reply` which is token-authenticated (no password).

## 5-minute live demo script

| Step | Click / do | What judges see |
|---|---|---|
| 1 | Open http://localhost:8501 | KPIs: 6,946 donors · 80 patient bridges · X messages · Y donations |
| 2 | Click **Coordinator Dashboard** + sign in | Branded login → Coordinator console |
| 3 | Open MailPit (http://localhost:8025) in a side window | Empty inbox waiting |
| 4 | Click **Run for ALL pending** | Agent processes every pending patient sequentially. Patient pipeline KPIs flip from Pending → Confirmed/Escalated. New emails appear in MailPit in real time, in different languages. |
| 5 | In MailPit, click any email | Beautiful HTML, multilingual greeting, patient details card, **YES** / **No** / **Question** buttons |
| 6 | Click **YES** in the email | Browser opens reply page → "Thank you, your donation is confirmed" → Confirmed donations KPI ticks up |
| 7 | Click any other email and choose **CAN'T MAKE IT** | Returns to "We'll reach out to another donor right away" → in Coordinator log you see `donation_cancelled` then `auto_reenqueued` then a new `outreach_sent` to the next-ranked donor automatically |
| 8 | Coordinator → expand "Urgency mode per patient" | Show Normal / Backup / Surge selector; pick a patient → switch to **surge** → run cycle; agent fires emails to all 8 donors in parallel without waiting |
| 9 | Click **Send 24h reminders** | Reminders go out for any donation scheduled in the next 24 h (in donor language) |
| 10 | Open **Bridge View** → pick any confirmed patient | Per-patient timeline: past transfusions, predicted next, donation history, full agent decision log audit trail, ranked donor pool |

## What's running where

| Service | Port | Purpose |
|---|---|---|
| Streamlit | 8501 | UI: Home, Coordinator, Bridge View, /Reply |
| MailPit SMTP | 1025 | All outbound emails sent by agent |
| MailPit web UI | 8025 | Gmail-style inbox view of every email the agent has sent |
| AWS DynamoDB | — | donors, bridges, messages, agent_log, donations |
| AWS Bedrock | — | Claude Haiku 4.5: outreach generation, intent classification, Q&A, reminders |
| AWS S3 | — | Dataset.csv + trained ML model pickles |

## Switch to production-grade delivery

In `.env`, change `DELIVERY_BACKEND=mailpit` to `DELIVERY_BACKEND=ses` and verify your SES sender domain. Same code path, real emails to real donors.

## Troubleshooting

**"localhost refused to connect"**
- Streamlit/MailPit died. Re-run `./run-stack.sh`.

**Email arrives but YES button does nothing**
- Make sure Streamlit is running on port 8501 (the magic-link domain) and DELIVERY_BACKEND is wired to a backend that the donor can actually click through (MailPit serves links inside the same machine). For real production, REPLY_BASE_URL must be a public HTTPS URL.

**KPIs all 0**
- DynamoDB tables empty. Run `python -m data.load_dataset --reset` once.

**`'numpy.float64' object has no attribute 'fillna'`**
- Stale Python bytecode. Run `find . -name __pycache__ -type d -exec rm -rf {} +` and restart Streamlit.

## Sample emails (offline preview)

Pre-rendered HTML samples sit in `logs/sample_emails/{telugu,hindi,tamil,english}.html`. Open any in a browser to see what donors receive.
