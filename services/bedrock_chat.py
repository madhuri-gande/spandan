"""Amazon Bedrock wrapper for Spandan.

Uses Claude Haiku 4.5 (cross-region inference profile) for:
  * generate_outreach()      - multilingual donation request message
  * classify_intent()        - parse donor reply -> YES / NO / QUESTION
  * answer_donor_question()  - empathetic answer about donation in donor language
  * thank_you_note()         - post-donation thank-you in donor language

Every call is logged to a small in-memory token counter; downstream
callers may decide to cache responses in DynamoDB for cost control.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import boto3
from dotenv import load_dotenv

load_dotenv()

REGION = os.getenv("BEDROCK_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
GENERATION_MODEL_ID = os.getenv(
    "BEDROCK_GENERATION_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
CLASSIFICATION_MODEL_ID = os.getenv(
    "BEDROCK_CLASSIFICATION_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=REGION)
    return _client


def _invoke_claude(prompt: str, model_id: str, max_tokens: int = 400, system: Optional[str] = None) -> str:
    client = _get_client()
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
    )
    payload = json.loads(response["body"].read())
    parts = payload.get("content", [])
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return text.strip()


LANGUAGE_LABEL = {
    "telugu": "Telugu (తెలుగు)",
    "hindi": "Hindi (हिन्दी)",
    "tamil": "Tamil (தமிழ்)",
    "english": "English",
}

CTA_BY_LANGUAGE = {
    "telugu": "అవును లేదా కాదు అని ప్రత్యుత్తరం ఇవ్వండి",
    "hindi": "हाँ या नहीं में जवाब दें",
    "tamil": "ஆம் அல்லது இல்லை என்று பதிலளிக்கவும்",
    "english": "Reply YES or NO",
}


def generate_outreach(donor: dict, bridge: dict) -> str:
    """Compose a warm donation request in the donor's preferred language."""
    language = (donor.get("preferred_language") or "english").lower()
    lang_label = LANGUAGE_LABEL.get(language, language.title())
    cta = CTA_BY_LANGUAGE.get(language, CTA_BY_LANGUAGE["english"])
    donor_name = donor.get("name") or "Donor"
    donations = donor.get("donations_till_date", 0)
    patient_name = bridge.get("patient_name") or "a young patient"
    age = bridge.get("patient_age") or "young"
    blood_group = bridge.get("blood_group") or ""
    hospital = bridge.get("hospital") or "the hospital"

    # Language-aware constraints. The "no English words" rule only makes
    # sense for non-English languages; for English donors that contradicts
    # the rest of the prompt and Claude (rightly) refuses or hedges.
    if language == "english":
        script_rule = (
            "Write the message in clear, natural English."
        )
    else:
        script_rule = (
            f"Write the message ENTIRELY in {lang_label}, native script only — including the "
            f"call-to-action phrase. The donor's name and place names may stay in Latin script if needed, "
            f"but everything else must be in {lang_label}."
        )

    prompt = (
        f"You are Spandan, the AI coordinator for Blood Warriors (a thalassemia donor network). "
        f"Write a warm, brief donation request message to a donor.\n\n"
        f"{script_rule}\n"
        f"Maximum 3 sentences. End with this exact call-to-action phrase (verbatim): \"{cta}\"\n\n"
        f"DONOR: {donor_name} (has donated {donations} times before)\n"
        f"PATIENT: {patient_name}, age {age}, needs {blood_group} blood\n"
        f"HOSPITAL: {hospital}\n"
        f"Tone: warm, urgent but not alarming, gracious. "
        f"Output the message body only — no preamble, no quotation marks around it, no explanation."
    )
    return _invoke_claude(prompt, GENERATION_MODEL_ID, max_tokens=320)


def classify_intent(reply_text: str) -> str:
    """Classify a donor's reply as YES / NO / CANCEL / QUESTION. Returns one word.

    CANCEL specifically means: the donor previously said yes but is now
    backing out (sick, out of town, emergency). Used by the agent to
    auto-reschedule to the next-ranked donor when this happens.
    """
    if not reply_text or not reply_text.strip():
        return "UNKNOWN"
    prompt = (
        "A blood-donation coordinator is asking a donor to donate. "
        "The donor replied below (could be in any Indian language).\n\n"
        "Classify their intent as ONE of these four labels:\n"
        "  YES      - donor agrees / will donate\n"
        "  NO       - donor refuses outright (this request)\n"
        "  CANCEL   - donor previously confirmed but is now backing out "
        "(e.g. 'I'm sick', 'emergency', 'I can't come anymore', "
        "'I won't be able to make it', 'sorry, please find someone else')\n"
        "  QUESTION - donor is asking for more details (location, time, "
        "eligibility, etc.) before deciding\n\n"
        f"Donor reply: {reply_text}\n\n"
        "Respond with ONLY the single label word."
    )
    raw = _invoke_claude(prompt, CLASSIFICATION_MODEL_ID, max_tokens=10)
    upper = raw.strip().upper()
    for token in ("CANCEL", "QUESTION", "YES", "NO"):
        if token in upper:
            return token
    return "UNKNOWN"


def answer_donor_question(donor: dict, question: str, bridge: Optional[dict] = None) -> str:
    """Answer a donor's question (e.g., eligibility, side-effects) in their language."""
    language = (donor.get("preferred_language") or "english").lower()
    lang_label = LANGUAGE_LABEL.get(language, language.title())

    context = ""
    if bridge:
        context = (
            f"\nContext: Patient is {bridge.get('patient_name')}, "
            f"age {bridge.get('patient_age')}, needs {bridge.get('blood_group')} blood "
            f"at {bridge.get('hospital')}."
        )

    system = (
        "You are Spandan, the AI assistant for Blood Warriors, a thalassemia "
        "donor support network. Answer donor questions truthfully and warmly. "
        "If unsure, say so. Keep answers under 3 sentences. "
        "Cover topics like eligibility, donation safety, recovery, or process. "
        f"Respond in {lang_label} using the native script."
    )
    prompt = f"Donor question: {question}{context}"
    return _invoke_claude(prompt, GENERATION_MODEL_ID, max_tokens=280, system=system)


def thank_you_note(donor: dict, bridge: dict) -> str:
    """Compose a heartfelt post-donation thank-you in the donor's language."""
    language = (donor.get("preferred_language") or "english").lower()
    lang_label = LANGUAGE_LABEL.get(language, language.title())
    prompt = (
        f"Write a heartfelt thank-you message in {lang_label} (native script) to "
        f"{donor.get('name', 'a donor')} who just donated blood for "
        f"{bridge.get('patient_name', 'a patient')} (age {bridge.get('patient_age')}). "
        "2 sentences max. Warm and personal."
    )
    return _invoke_claude(prompt, GENERATION_MODEL_ID, max_tokens=200)


def thanks_already_covered(donor: dict, bridge: dict) -> str:
    """Polite message when a donor said YES but we already have enough confirmations.

    Used by the over-confirmation guard in surge mode (or any time multiple
    donors race to a YES). Goal: thank them sincerely, explain we already
    have the donations we need for this patient, and tell them we'll
    contact them for the next request.
    """
    language = (donor.get("preferred_language") or "english").lower()
    lang_label = LANGUAGE_LABEL.get(language, language.title())
    if language == "english":
        script_rule = "Write the message in clear, natural English."
    else:
        script_rule = (
            f"Write the message ENTIRELY in {lang_label}, native script only. "
            f"The donor's name and place names may stay in Latin script."
        )

    prompt = (
        f"You are Spandan, the AI coordinator for Blood Warriors.\n"
        f"{script_rule}\n"
        f"Maximum 3 sentences.\n\n"
        f"DONOR: {donor.get('name', 'a donor')} just replied YES to donate for "
        f"patient {bridge.get('patient_name','a thalassemia patient')} "
        f"({bridge.get('blood_group','')}). However, other donors have already "
        f"confirmed and the patient's needs are fully covered for this transfusion.\n\n"
        f"Write a warm, gracious message that:\n"
        f"  1. Thanks them sincerely for their willingness.\n"
        f"  2. Explains that the patient already has enough confirmed donors for "
        f"this round, so they don't need to come this time.\n"
        f"  3. Says we will reach out for the next request and that their "
        f"generosity makes the network stronger.\n\n"
        f"Output the message body only — no preamble, no quotation marks, no explanation."
    )
    return _invoke_claude(prompt, GENERATION_MODEL_ID, max_tokens=260)


def donation_reminder(donor: dict, bridge: dict, scheduled_date: str) -> str:
    """24h reconfirmation reminder for a donor who already said yes."""
    language = (donor.get("preferred_language") or "english").lower()
    lang_label = LANGUAGE_LABEL.get(language, language.title())
    cta = CTA_BY_LANGUAGE.get(language, CTA_BY_LANGUAGE["english"])
    prompt = (
        f"Write a brief, polite reminder in {lang_label} (native script only).\n"
        f"It's a 24-hour reminder to {donor.get('name', 'the donor')} who already "
        f"agreed to donate blood for {bridge.get('patient_name','the patient')} "
        f"({bridge.get('patient_age','')}, blood {bridge.get('blood_group','')}) "
        f"at {bridge.get('hospital','the hospital')}, scheduled {scheduled_date}.\n"
        f"Maximum 2 sentences. Confirm they are still able to come, and end with the "
        f"call-to-action phrase: '{cta}' (or its localized equivalent — e.g. 'Reply YES "
        f"if still confirmed, or NO if you cannot make it')."
    )
    return _invoke_claude(prompt, GENERATION_MODEL_ID, max_tokens=240)


if __name__ == "__main__":
    sample_donor = {
        "name": "Ravi K",
        "donations_till_date": 5,
        "preferred_language": "telugu",
    }
    sample_bridge = {
        "patient_name": "Aarav Reddy",
        "patient_age": 7,
        "blood_group": "B+",
        "hospital": "Apollo Hyderabad",
    }

    print("=== generate_outreach ===")
    msg = generate_outreach(sample_donor, sample_bridge)
    print(msg)

    print("\n=== classify_intent ===")
    print("YES test:", classify_intent("అవును, నేను చేస్తాను"))
    print("NO test:", classify_intent("Sorry I cannot today"))
    print("QUESTION test:", classify_intent("ఎక్కడ రావాలి?"))

    print("\n=== answer_donor_question ===")
    print(answer_donor_question(sample_donor, "ఎవరు దానం చేయవచ్చు?", sample_bridge))

    print("\n=== thank_you_note ===")
    print(thank_you_note(sample_donor, sample_bridge))
