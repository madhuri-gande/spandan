#!/usr/bin/env python3
"""Generate Spandan hackathon pitch deck with QR codes.

Output:
  docs/Spandan_Hackathon_Pitch.pptx
  docs/assets/qr-dashboard.png
  docs/assets/qr-mailpit.png
  docs/assets/qr-github.png
"""
from __future__ import annotations

from pathlib import Path

import qrcode
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ASSETS = DOCS / "assets"

DEMO_URL = "http://98.84.159.117"
MAIL_URL = "http://98.84.159.117/mail"
GITHUB = "https://github.com/madhuri-gande/spandan"

RED = RGBColor(0xC8, 0x10, 0x2E)
DARK = RGBColor(0x22, 0x22, 0x2B)
GREY = RGBColor(0x5A, 0x60, 0x68)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_BG = RGBColor(0xF8, 0xF9, 0xFB)
PINK = RGBColor(0xFF, 0xDD, 0xDD)
SOFT_RED = RGBColor(0xFD, 0xE8, 0xEA)


def _qr(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(data, box_size=10, border=2)
    img.save(path)


def _set_slide_bg(slide, color: RGBColor) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _accent_bar(slide, *, top: float = 0, height: float = 0.12) -> None:
    bar = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, Inches(0), Inches(top), Inches(13.333), Inches(height)
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = RED
    bar.line.fill.background()


def _add_title(slide, title: str, subtitle: str = "", *, dark: bool = False) -> None:
    tx = slide.shapes.add_textbox(Inches(0.6), Inches(0.55), Inches(12.1), Inches(1.2))
    tf = tx.text_frame
    p = tf.paragraphs[0]
    p.text = title
    p.font.size = Pt(36)
    p.font.bold = True
    p.font.color.rgb = WHITE if dark else RED
    if subtitle:
        p2 = tf.add_paragraph()
        p2.text = subtitle
        p2.font.size = Pt(16)
        p2.font.color.rgb = GREY if not dark else RGBColor(0xDD, 0xDD, 0xDD)
        p2.space_before = Pt(6)


def _add_bullets(slide, items: list[str], top: float = 1.6) -> None:
    tx = slide.shapes.add_textbox(Inches(0.75), Inches(top), Inches(11.5), Inches(5.2))
    tf = tx.text_frame
    tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"▸  {item}"
        p.font.size = Pt(19)
        p.font.color.rgb = DARK
        p.space_after = Pt(10)


def _stat_card(slide, left: float, top: float, value: str, label: str) -> None:
    card = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top), Inches(3.8), Inches(1.55)
    )
    card.fill.solid()
    card.fill.fore_color.rgb = WHITE
    card.line.color.rgb = SOFT_RED
    card.line.width = Pt(1.5)
    tx = slide.shapes.add_textbox(Inches(left + 0.25), Inches(top + 0.2), Inches(3.3), Inches(1.2))
    tf = tx.text_frame
    p = tf.paragraphs[0]
    p.text = value
    p.font.size = Pt(32)
    p.font.bold = True
    p.font.color.rgb = RED
    p2 = tf.add_paragraph()
    p2.text = label
    p2.font.size = Pt(14)
    p2.font.color.rgb = GREY
    p2.space_before = Pt(4)


def build() -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    qr_dash = ASSETS / "qr-dashboard.png"
    qr_mail = ASSETS / "qr-mailpit.png"
    qr_gh = ASSETS / "qr-github.png"
    _qr(qr_dash, DEMO_URL)
    _qr(qr_mail, MAIL_URL)
    _qr(qr_gh, GITHUB)

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # 1 — Title
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, RED)
    _accent_bar(s, top=6.9, height=0.6)
    box = s.shapes.add_textbox(Inches(0.8), Inches(1.6), Inches(11.5), Inches(3.2))
    tf = box.text_frame
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.text = "Spandan"
    p.font.size = Pt(60)
    p.font.bold = True
    p.font.color.rgb = WHITE
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "Autonomous AI Blood Support Network"
    p2.font.size = Pt(26)
    p2.font.color.rgb = WHITE
    p2.alignment = PP_ALIGN.CENTER
    p2.space_before = Pt(14)
    p3 = tf.add_paragraph()
    p3.text = "Blood Warriors Hackathon · Team 066"
    p3.font.size = Pt(17)
    p3.font.color.rgb = PINK
    p3.alignment = PP_ALIGN.CENTER
    p3.space_before = Pt(22)
    p4 = tf.add_paragraph()
    p4.text = "स्पंदन · the heartbeat that keeps care in rhythm"
    p4.font.size = Pt(15)
    p4.font.color.rgb = PINK
    p4.alignment = PP_ALIGN.CENTER
    p4.space_before = Pt(8)

    # 2 — Impact at a glance
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "Impact at a glance")
    for i, (val, lbl) in enumerate([
        ("7,000+", "donor records in real dataset"),
        ("90 days", "forecast pipeline window"),
        ("4 languages", "Telugu · Hindi · Tamil · English"),
        ("24/7", "autonomous agent on AWS EC2"),
    ]):
        _stat_card(s, 0.75 + (i % 2) * 4.1, 1.65 + (i // 2) * 2.0, val, lbl)
    note = s.shapes.add_textbox(Inches(0.75), Inches(6.1), Inches(12), Inches(0.5))
    note.text_frame.paragraphs[0].text = (
        "Built on Blood Warriors data — not synthetic patients. Live demo running now."
    )
    note.text_frame.paragraphs[0].font.size = Pt(14)
    note.text_frame.paragraphs[0].font.color.rgb = GREY

    # 3 — Problem
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "The problem", "Manual coordination does not scale")
    _add_bullets(s, [
        "1,00,000+ Thalassemia patients need 500–700 transfusions in a lifetime",
        "Blood Warriors matches donors to patients — today mostly manual calls & spreadsheets",
        "Coordinators chase donors one-by-one; wrong language, timing, and fatigue",
        "Emergencies need instant parallel outreach — normal pacing is too slow",
        "As cities and partner hospitals grow, human nudging becomes the bottleneck",
    ])

    # 4 — Solution
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "Our solution — Spandan")
    _add_bullets(s, [
        "Always-on autonomous agent: forecast → rank → email → interpret reply → escalate",
        "One intelligent layer over real Blood Warriors dataset (7,000+ donor records)",
        "Sequential donor outreach — one patient, one donor at a time (no over-mobilization)",
        "Surge mode for emergencies — all eligible donors in parallel, one shot",
        "Coordinator dashboard for visibility — agent runs 24/7 without the UI open",
    ])

    # 5 — How it works (flow)
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "Autonomous loop")
    steps = [
        ("Forecast", "Roll transfusion cadence forward → 90-day pipeline"),
        ("Rank (ML)", "Logistic regression · blood-group filter · top donors"),
        ("Outreach", "Bedrock Haiku drafts multilingual HTML emails"),
        ("Reply", "Magic-link YES / NO → classify intent → confirm or next donor"),
        ("Escalate", "Timeout → next donor · post-YES reminder · emergency surge"),
    ]
    for i, (head, body) in enumerate(steps):
        y = 1.55 + i * 1.05
        badge = s.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(y), Inches(2.1), Inches(0.72)
        )
        badge.fill.solid()
        badge.fill.fore_color.rgb = RED if i % 2 == 0 else DARK
        badge.line.fill.background()
        btx = badge.text_frame
        btx.paragraphs[0].text = head
        btx.paragraphs[0].font.size = Pt(14)
        btx.paragraphs[0].font.bold = True
        btx.paragraphs[0].font.color.rgb = WHITE
        btx.paragraphs[0].alignment = PP_ALIGN.CENTER
        btx.vertical_anchor = MSO_ANCHOR.MIDDLE
        arrow = s.shapes.add_textbox(Inches(2.95), Inches(y + 0.1), Inches(0.4), Inches(0.5))
        arrow.text_frame.paragraphs[0].text = "→"
        arrow.text_frame.paragraphs[0].font.size = Pt(22)
        arrow.text_frame.paragraphs[0].font.color.rgb = RED
        body_tx = s.shapes.add_textbox(Inches(3.35), Inches(y + 0.12), Inches(9.2), Inches(0.6))
        body_tx.text_frame.paragraphs[0].text = body
        body_tx.text_frame.paragraphs[0].font.size = Pt(17)
        body_tx.text_frame.paragraphs[0].font.color.rgb = DARK

    # 6 — AI & ML
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "AI & ML components")
    _add_bullets(s, [
        "Amazon Bedrock · Claude Haiku 4.5 — multilingual outreach & YES/NO/CANCEL classification",
        "scikit-learn LogisticRegression — donor response probability (real dataset features)",
        "Per-patient forecasting — frequency_in_days rolled forward from historical records",
        "Conversational memory — DynamoDB messages + agent_log across cycles",
        "Failure learning — skip_score penalizes non-responsive donors in future ranking",
    ])

    # 7 — AWS
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "AWS architecture (live on EC2)")
    arch = s.shapes.add_textbox(Inches(0.7), Inches(1.45), Inches(12), Inches(5))
    tf = arch.text_frame
    tf.word_wrap = True
    diagram = """
┌──────────────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Streamlit dashboard │────▶│  agent.py       │────▶│  DynamoDB        │
│  Coordinator UI      │     │  background loop│     │  donors · bridges│
└──────────┬───────────┘     └────────┬────────┘     └──────────────────┘
           │                          │
           │                   ┌──────┴──────┐
           │                   ▼             ▼
           │            ┌────────────┐  ┌─────────────┐
           └───────────▶│ MailPit    │  │ Bedrock     │
                        │ /mail inbox│  │ Haiku 4.5   │
                        └────────────┘  └─────────────┘
nginx :80 · EC2 t3.small · IAM role · us-east-1 · systemd 24/7
"""
    tf.paragraphs[0].text = diagram.strip()
    tf.paragraphs[0].font.name = "Courier New"
    tf.paragraphs[0].font.size = Pt(13)
    tf.paragraphs[0].font.color.rgb = DARK

    # 8 — Live demo + QR (hero slide)
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, WHITE)
    _accent_bar(s)
    _add_title(s, "Live demo — scan to try")
    qr_specs = [
        (qr_dash, 0.85, "Dashboard", DEMO_URL),
        (qr_mail, 4.35, "MailPit inbox", MAIL_URL),
        (qr_gh, 7.85, "GitHub repo", GITHUB),
    ]
    for path, left, label, url in qr_specs:
        s.shapes.add_picture(str(path), Inches(left), Inches(1.75), width=Inches(2.35))
        cap = s.shapes.add_textbox(Inches(left - 0.1), Inches(4.25), Inches(2.6), Inches(1.1))
        ctf = cap.text_frame
        ctf.paragraphs[0].text = label
        ctf.paragraphs[0].font.size = Pt(13)
        ctf.paragraphs[0].font.bold = True
        ctf.paragraphs[0].font.color.rgb = RED
        ctf.paragraphs[0].alignment = PP_ALIGN.CENTER
        p2 = ctf.add_paragraph()
        p2.text = url.replace("https://", "").replace("http://", "")
        p2.font.size = Pt(9)
        p2.font.color.rgb = GREY
        p2.alignment = PP_ALIGN.CENTER

    side = s.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(10.5), Inches(1.65), Inches(2.5), Inches(4.8)
    )
    side.fill.solid()
    side.fill.fore_color.rgb = SOFT_RED
    side.line.fill.background()
    stx = s.shapes.add_textbox(Inches(10.65), Inches(1.85), Inches(2.2), Inches(4.4))
    stf = stx.text_frame
    stf.word_wrap = True
    for i, line in enumerate([
        "Login",
        "coordinator",
        "",
        "Demo flow",
        "1. Pipeline",
        "2. Email donor",
        "3. MailPit → YES",
        "4. KPI updates",
        "5. Surge mode",
    ]):
        p = stf.paragraphs[0] if i == 0 else stf.add_paragraph()
        p.text = line
        p.font.size = Pt(12)
        p.font.bold = line in ("Login", "Demo flow")
        p.font.color.rgb = RED if line in ("Login", "Demo flow") else DARK

    # 9 — Evaluation fit
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, LIGHT_BG)
    _accent_bar(s)
    _add_title(s, "Evaluation criteria mapping")
    criteria = [
        ("20%", "Practicality & scalability", "Real cadence forecasting, cooldowns, surge, SES-ready"),
        ("20%", "Innovation", "Autonomous state machine — not a chatbot wrapper"),
        ("20%", "AI component", "Bedrock multilingual + ML ranking on organiser dataset"),
        ("20%", "Real implementation", "Live emails, magic links, DynamoDB — not mock UI"),
        ("20%", "End-to-end deployment", "GitHub + EC2 + systemd + nginx + IAM role"),
    ]
    for i, (pct, head, body) in enumerate(criteria):
        y = 1.55 + i * 1.02
        pill = s.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.75), Inches(y), Inches(1.0), Inches(0.65)
        )
        pill.fill.solid()
        pill.fill.fore_color.rgb = RED
        pill.line.fill.background()
        pill.text_frame.paragraphs[0].text = pct
        pill.text_frame.paragraphs[0].font.size = Pt(13)
        pill.text_frame.paragraphs[0].font.bold = True
        pill.text_frame.paragraphs[0].font.color.rgb = WHITE
        pill.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        pill.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        htx = s.shapes.add_textbox(Inches(1.95), Inches(y), Inches(10.5), Inches(0.7))
        htx.text_frame.paragraphs[0].text = f"{head}  —  {body}"
        htx.text_frame.paragraphs[0].font.size = Pt(17)
        htx.text_frame.paragraphs[0].font.color.rgb = DARK

    # 10 — Thank you
    s = prs.slides.add_slide(blank)
    _set_slide_bg(s, RED)
    box = s.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(8.5), Inches(3))
    tf = box.text_frame
    p = tf.paragraphs[0]
    p.text = "Thank you"
    p.font.size = Pt(48)
    p.font.bold = True
    p.font.color.rgb = WHITE
    p2 = tf.add_paragraph()
    p2.text = f"Try now: {DEMO_URL}"
    p2.font.size = Pt(20)
    p2.font.color.rgb = WHITE
    p2.space_before = Pt(16)
    p3 = tf.add_paragraph()
    p3.text = GITHUB
    p3.font.size = Pt(15)
    p3.font.color.rgb = PINK
    p3.space_before = Pt(10)
    s.shapes.add_picture(str(qr_dash), Inches(10.2), Inches(2.0), width=Inches(2.2))
    scan = s.shapes.add_textbox(Inches(10.0), Inches(4.35), Inches(2.6), Inches(0.5))
    scan.text_frame.paragraphs[0].text = "Scan to open demo"
    scan.text_frame.paragraphs[0].font.size = Pt(12)
    scan.text_frame.paragraphs[0].font.color.rgb = PINK
    scan.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    out = DOCS / "Spandan_Hackathon_Pitch.pptx"
    prs.save(out)
    return out


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
