"""HTML and plain-text email templates for donor outreach.

The templates are language-agnostic in structure but render localized
labels and CTAs based on the donor's preferred_language. Patient/donor
context cards use universal short labels that read well in any script.
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Optional


# Localized labels keyed by language. English fallback for anything else.
_L: dict[str, dict[str, str]] = {
    "english": {
        "title": "Urgent blood donation request",
        "patient_details": "Patient details",
        "patient": "Patient",
        "blood": "Blood group",
        "hospital": "Hospital",
        "needed_by": "Needed by",
        "urgency": "Urgency",
        "your_history": "Why we're asking you",
        "last_donation": "Last donation",
        "total_donations": "Total donations",
        "match": "Blood match",
        "eligibility": "Eligibility",
        "yes_btn": "YES, count me in",
        "no_btn": "Can't make it",
        "or_reply": "Or simply reply to this email with YES or NO.",
        "footer": "Spandan AI Coordinator · Blood Warriors · Hyderabad, India",
        "opt_out": "This message was sent because you are a registered donor. Reply STOP to opt out.",
        "high_urgency": "HIGH",
        "perfect_match": "Perfect match",
        "eligible": "Eligible",
        "not_eligible": "Not currently eligible",
    },
    "telugu": {
        "title": "అత్యవసర రక్తదానం అభ్యర్థన",
        "patient_details": "రోగి వివరాలు",
        "patient": "రోగి",
        "blood": "రక్త వర్గం",
        "hospital": "ఆసుపత్రి",
        "needed_by": "అవసర తేదీ",
        "urgency": "ప్రాధాన్యత",
        "your_history": "మిమ్మల్ని ఎందుకు అడుగుతున్నాం",
        "last_donation": "చివరి దానం",
        "total_donations": "మొత్తం దానాలు",
        "match": "రక్త వర్గ సరిపోలిక",
        "eligibility": "అర్హత",
        "yes_btn": "అవును, నేను సహాయం చేస్తాను",
        "no_btn": "ఈసారి కుదరదు",
        "or_reply": "లేదా ఈ ఇమెయిల్‌కు అవును లేదా కాదు అని ప్రత్యుత్తరం ఇవ్వండి.",
        "footer": "స్పందన AI కోఆర్డినేటర్ · బ్లడ్ వారియర్స్ · హైదరాబాద్",
        "opt_out": "మీరు నమోదు చేసుకున్న దాత కాబట్టి ఈ సందేశం పంపబడింది. ఉపసంహరించుకోవడానికి STOP అని ప్రత్యుత్తరం ఇవ్వండి.",
        "high_urgency": "అధికం",
        "perfect_match": "సరిగ్గా సరిపోతుంది",
        "eligible": "అర్హులు",
        "not_eligible": "ప్రస్తుతం అర్హులు కాదు",
    },
    "hindi": {
        "title": "तत्काल रक्तदान अनुरोध",
        "patient_details": "रोगी विवरण",
        "patient": "रोगी",
        "blood": "रक्त समूह",
        "hospital": "अस्पताल",
        "needed_by": "आवश्यकता तिथि",
        "urgency": "अत्यावश्यकता",
        "your_history": "हम आपसे क्यों पूछ रहे हैं",
        "last_donation": "पिछला दान",
        "total_donations": "कुल दान",
        "match": "रक्त समूह मिलान",
        "eligibility": "पात्रता",
        "yes_btn": "हाँ, मैं तैयार हूँ",
        "no_btn": "इस बार नहीं",
        "or_reply": "या इस ईमेल का उत्तर हाँ या नहीं में दें।",
        "footer": "स्पंदन AI समन्वयक · ब्लड वॉरियर्स · हैदराबाद, भारत",
        "opt_out": "यह संदेश इसलिए भेजा गया क्योंकि आप पंजीकृत दाता हैं। ऑप्ट आउट के लिए STOP जवाब दें।",
        "high_urgency": "उच्च",
        "perfect_match": "पूर्ण मिलान",
        "eligible": "पात्र",
        "not_eligible": "वर्तमान में पात्र नहीं",
    },
    "tamil": {
        "title": "அவசர இரத்த தான வேண்டுகோள்",
        "patient_details": "நோயாளி விவரங்கள்",
        "patient": "நோயாளி",
        "blood": "இரத்த வகை",
        "hospital": "மருத்துவமனை",
        "needed_by": "தேவைப்படும் தேதி",
        "urgency": "அவசர நிலை",
        "your_history": "உங்களை ஏன் கேட்கிறோம்",
        "last_donation": "கடைசி தானம்",
        "total_donations": "மொத்த தானங்கள்",
        "match": "இரத்த வகை பொருத்தம்",
        "eligibility": "தகுதி",
        "yes_btn": "ஆம், நான் தயார்",
        "no_btn": "இந்த முறை முடியாது",
        "or_reply": "அல்லது இந்த மின்னஞ்சலுக்கு ஆம் அல்லது இல்லை என்று பதிலளிக்கவும்.",
        "footer": "ஸ்பந்தன் AI ஒருங்கிணைப்பாளர் · ப்ளட் வாரியர்ஸ் · ஹைதராபாத்",
        "opt_out": "நீங்கள் பதிவு செய்த தானியாக இருப்பதால் இந்த செய்தி அனுப்பப்பட்டது. வெளியேற STOP என பதிலளிக்கவும்.",
        "high_urgency": "உயர்",
        "perfect_match": "சரியான பொருத்தம்",
        "eligible": "தகுதியானவர்",
        "not_eligible": "தற்போது தகுதியில்லை",
    },
}


def _labels(language: str) -> dict[str, str]:
    return _L.get((language or "english").lower(), _L["english"])


def _format_date(s: Optional[str]) -> str:
    if not s:
        return "—"
    s = str(s)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%A, %B %-d %Y")
    except Exception:
        return s


def _urgency_label(bridge: dict, lab: dict) -> str:
    try:
        from datetime import date
        next_str = bridge.get("expected_next_transfusion_date") or ""
        if next_str:
            d = datetime.strptime(str(next_str)[:10], "%Y-%m-%d").date()
            delta = (d - date.today()).days
            if delta <= 1:
                return f"{lab['high_urgency']} ({delta}d)"
            return f"+{delta}d"
    except Exception:
        pass
    return lab["high_urgency"]


def build_html_email(
    donor_name: str,
    body_text: str,
    bridge: dict,
    language: str,
    reply_links: dict,
    msg_type: str = "outreach",
) -> str:
    lab = _labels(language)
    patient_name = bridge.get("patient_name") or "—"
    patient_age = bridge.get("patient_age") or ""
    blood = bridge.get("blood_group") or "—"
    hospital = bridge.get("hospital") or "—"
    needed_by = _format_date(bridge.get("expected_next_transfusion_date"))
    urgency = _urgency_label(bridge, lab)

    body_html = html.escape(body_text).replace("\n", "<br>")
    age_str = f" ({patient_age} years)" if patient_age else ""

    yes = reply_links.get("yes", "#")
    no = reply_links.get("no", "#")

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>{html.escape(lab['title'])}</title>
</head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table cellpadding="0" cellspacing="0" width="100%" style="background:#f4f5f7;padding:24px 0;">
<tr><td align="center">
  <table cellpadding="0" cellspacing="0" width="600" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
    <tr>
      <td style="background:linear-gradient(135deg,#c0273f 0%,#8a1c2c 100%);padding:24px 32px;color:#fff;">
        <div style="font-size:13px;letter-spacing:1.5px;opacity:0.9;">SPANDAN · BLOOD WARRIORS</div>
        <div style="font-size:22px;font-weight:600;margin-top:6px;">{html.escape(lab['title'])}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:28px 32px 8px 32px;color:#1a1a1a;font-size:15px;line-height:1.6;">
        {body_html}
      </td>
    </tr>
    <tr>
      <td style="padding:8px 32px;">
        <div style="background:#f8f9fb;border:1px solid #e7e9ed;border-radius:10px;padding:16px 18px;margin-top:8px;">
          <div style="font-size:12px;color:#7a7f87;letter-spacing:1px;font-weight:600;margin-bottom:10px;">{html.escape(lab['patient_details']).upper()}</div>
          <table cellpadding="0" cellspacing="0" width="100%" style="font-size:14px;color:#1a1a1a;">
            <tr><td style="padding:4px 0;color:#7a7f87;width:130px;">{html.escape(lab['patient'])}</td><td style="padding:4px 0;font-weight:500;">{html.escape(patient_name)}{html.escape(age_str)}</td></tr>
            <tr><td style="padding:4px 0;color:#7a7f87;">{html.escape(lab['blood'])}</td><td style="padding:4px 0;font-weight:500;color:#c0273f;">{html.escape(blood)}</td></tr>
            <tr><td style="padding:4px 0;color:#7a7f87;">{html.escape(lab['hospital'])}</td><td style="padding:4px 0;font-weight:500;">{html.escape(hospital)}</td></tr>
            <tr><td style="padding:4px 0;color:#7a7f87;">{html.escape(lab['needed_by'])}</td><td style="padding:4px 0;font-weight:500;">{html.escape(needed_by)}</td></tr>
            <tr><td style="padding:4px 0;color:#7a7f87;">{html.escape(lab['urgency'])}</td><td style="padding:4px 0;font-weight:600;color:#c0273f;">{html.escape(urgency)}</td></tr>
          </table>
        </div>
      </td>
    </tr>
    <tr>
      <td style="padding:24px 32px 8px 32px;text-align:center;">
        <a href="{html.escape(yes)}" style="display:inline-block;padding:14px 28px;background:#0a8f3f;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;font-size:15px;margin:4px;">✓ {html.escape(lab['yes_btn'])}</a>
      </td>
    </tr>
    <tr>
      <td style="padding:0 32px 20px 32px;text-align:center;">
        <a href="{html.escape(no)}" style="display:inline-block;padding:11px 22px;background:#f4f5f7;color:#444;text-decoration:none;border-radius:8px;font-weight:500;font-size:14px;margin:4px;border:1px solid #d8dadf;">✗ {html.escape(lab['no_btn'])}</a>
      </td>
    </tr>
    <tr>
      <td style="padding:0 32px 20px 32px;text-align:center;font-size:12px;color:#9aa0a8;">
        {html.escape(lab['or_reply'])}
      </td>
    </tr>
    <tr>
      <td style="background:#f8f9fb;padding:16px 32px;text-align:center;font-size:11px;color:#9aa0a8;border-top:1px solid #e7e9ed;">
        {html.escape(lab['footer'])}<br>
        <span style="opacity:0.7;">{html.escape(lab['opt_out'])}</span>
      </td>
    </tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


def build_plain_email(
    donor_name: str,
    body_text: str,
    bridge: dict,
    language: str,
    reply_links: dict,
    msg_type: str = "outreach",
) -> str:
    lab = _labels(language)
    patient_name = bridge.get("patient_name") or "—"
    patient_age = bridge.get("patient_age") or ""
    blood = bridge.get("blood_group") or "—"
    hospital = bridge.get("hospital") or "—"
    needed_by = _format_date(bridge.get("expected_next_transfusion_date"))
    urgency = _urgency_label(bridge, lab)

    age_str = f" ({patient_age} years)" if patient_age else ""

    return (
        f"=== SPANDAN · BLOOD WARRIORS ===\n"
        f"{lab['title']}\n\n"
        f"{body_text}\n\n"
        f"--- {lab['patient_details']} ---\n"
        f"  {lab['patient']:<20} {patient_name}{age_str}\n"
        f"  {lab['blood']:<20} {blood}\n"
        f"  {lab['hospital']:<20} {hospital}\n"
        f"  {lab['needed_by']:<20} {needed_by}\n"
        f"  {lab['urgency']:<20} {urgency}\n\n"
        f"  {lab['yes_btn']}: {reply_links.get('yes', '')}\n"
        f"  {lab['no_btn']}: {reply_links.get('no', '')}\n\n"
        f"{lab['or_reply']}\n\n"
        f"---\n"
        f"{lab['footer']}\n"
        f"{lab['opt_out']}\n"
    )
