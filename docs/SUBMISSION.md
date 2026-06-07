# Spandan — Hackathon submission pack

Copy-paste for judges, WhatsApp, and submission forms.

---

## Live demo

| | Link |
|---|---|
| **Live demo (Bitly — share on WhatsApp)** | **https://bit.ly/4vCaAQ4** |
| **Dashboard (direct)** | http://98.84.159.117 |
| **Email inbox (MailPit)** | http://98.84.159.117/mail |
| **Source code** | https://github.com/madhuri-gande/spandan |
| **Pitch deck** | [docs/Spandan_Hackathon_Pitch.pptx](Spandan_Hackathon_Pitch.pptx) |

**Login:** `coordinator` · password shared privately with judges.

---

## WhatsApp / short link

**https://bit.ly/4vCaAQ4** → opens the live coordinator dashboard.

Works on any phone with internet. MailPit is reachable from the dashboard header or at http://98.84.159.117/mail.

---

## QR codes (in pitch deck)

Scan from the **Live demo** slide of `Spandan_Hackathon_Pitch.pptx` (17 slides, finalist edition), or use:

- `docs/assets/qr-dashboard.png` → dashboard
- `docs/assets/qr-mailpit.png` → MailPit inbox
- `docs/assets/qr-github.png` → GitHub repo
- `docs/assets/qr-bitly.png` → https://bit.ly/4vCaAQ4 (WhatsApp share)

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
