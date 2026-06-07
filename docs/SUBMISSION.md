# Spandan — Hackathon submission pack

Copy-paste for judges, WhatsApp, and submission forms.

---

## Live demo

| | Link |
|---|---|
| **Dashboard** | http://98.84.159.117 |
| **Email inbox (MailPit)** | http://98.84.159.117/mail |
| **Source code** | https://github.com/madhuri-gande/spandan |
| **Pitch deck** | [docs/Spandan_Hackathon_Pitch.pptx](Spandan_Hackathon_Pitch.pptx) |

**Login:** `coordinator` · password shared privately with judges.

---

## WhatsApp / short links (Bitly)

If you created Bitly links, add them here and in the README:

| | Bitly (optional) | Direct URL |
|---|---|---|
| Dashboard | _paste your bit.ly link_ | http://98.84.159.117 |
| MailPit | _paste your bit.ly link_ | http://98.84.159.117/mail |

Bitly redirects to the direct URL — works on any phone with internet.

---

## QR codes (in pitch deck)

Scan from **slide 8** of `Spandan_Hackathon_Pitch.pptx`, or use:

- `docs/assets/qr-dashboard.png` → dashboard
- `docs/assets/qr-mailpit.png` → MailPit inbox
- `docs/assets/qr-github.png` → GitHub repo

---

## 60-second demo script

1. Open **http://98.84.159.117** → log in as coordinator.
2. Show **patient pipeline** (90-day forecast from real dataset).
3. Click **📧 Email next donor now** OR wait for autonomous agent.
4. Open **MailPit** (top-right button) → show multilingual HTML email.
5. Click **YES** in email → dashboard shows **CONFIRMED**.
6. (Optional) **Emergency surge** for one patient → parallel blast.

---

## AWS resources

- **EC2:** `t3.small` · `98.84.159.117` · Amazon Linux 2023
- **IAM role:** `SpandanEC2Role` (DynamoDB + Bedrock)
- **Region:** `us-east-1`
