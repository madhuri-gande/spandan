# Spandan — Architecture

Simple overview of the live system at **http://98.84.159.117** (EC2 + nginx).

---

## 1. System overview

```mermaid
flowchart TB
    subgraph Users
        COORD[Coordinator<br/>browser]
        DONOR[Donor<br/>email / magic link]
    end

    subgraph EC2["EC2 t3.small · nginx :80"]
        NGX[nginx]
        ST[Streamlit :8501<br/>Home · Coordinator · Reply]
        MP[MailPit<br/>SMTP :1025 · UI /mail]
        AG[Agent thread<br/>agent.py]
        DEL[Delivery<br/>delivery.py]
    end

    subgraph AWS["AWS us-east-1"]
        DDB[(DynamoDB<br/>donors · bridges · messages · donations)]
        BR[Bedrock<br/>Claude Haiku 4.5]
    end

    COORD -->|login dashboard| NGX
    DONOR -->|YES / NO link| NGX
    NGX --> ST
    NGX -->|/mail| MP

    ST --> AG
    AG --> DDB
    AG --> BR
    AG --> DEL
    DEL -->|SMTP| MP
    DEL --> DDB
    ST --> DDB
```

---

## 2. Autonomous agent loop

```mermaid
flowchart LR
    A[Forecast<br/>90-day pipeline] --> B[Rank donors<br/>Logistic Regression]
    B --> C[Outreach<br/>Bedrock multilingual]
    C --> D[Email<br/>MailPit / SES]
    D --> E{Donor reply<br/>YES / NO}
    E -->|YES| F[Confirm donation<br/>DynamoDB]
    E -->|NO / timeout| G[Next donor<br/>sequential]
    G --> B
    F --> H[Remind · advance cadence]
```

**Modes**
- **Normal** — one donor at a time per patient (`DONOR_WAIT_SECONDS`)
- **Surge** — emergency parallel blast to top-ranked donors

---

## 3. Email & reply flow

```mermaid
sequenceDiagram
    participant A as Agent
    participant D as DynamoDB
    participant B as Bedrock
    participant M as MailPit
    participant U as Donor browser

    A->>B: generate_outreach()
    B-->>A: Telugu / Hindi / Tamil / English text
    A->>D: write outbound message
    A->>M: SMTP HTML email + YES/NO links
    U->>M: open inbox
    U->>U: click YES
    U->>D: /Reply page writes inbound message
    A->>B: classify_intent()
    A->>D: confirm donation
```

---

## 4. ML & rules (who to contact)

| Step | Type | Module |
|------|------|--------|
| When is blood needed? | Cadence math | `forecasting.py` |
| Blood type match? | Rules | `ranking.py` COMPATIBILITY |
| Who responds best? | ML — Logistic Regression | `ranking.py` |
| What to say? | LLM — Bedrock Haiku | `bedrock_chat.py` |

---

## 5. DynamoDB tables

| Table | Purpose |
|-------|---------|
| `donors` | Donor profile, ML features, skip_score |
| `bridges` | Patient, cadence, donor_pool |
| `messages` | Inbound / outbound emails |
| `donations` | Confirmed donations |
| `agent_log` | Audit trail of every agent action |

---

## 6. Deploy path

```
GitHub repo → EC2 clone → ec2-setup.sh
  → train ranking model → load Dataset.csv → DynamoDB
  → nginx :80 → systemd spandan.service → run-stack.sh
```

Live URLs: **Dashboard** http://98.84.159.117 · **MailPit** http://98.84.159.117/mail · **Bitly** https://bit.ly/4vCaAQ4
