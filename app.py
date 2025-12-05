import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI


# ============================================================
#                      CONFIG
# ============================================================

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

HIGHLEVEL_API_KEY = os.getenv("HIGHLEVEL_API_KEY")
HIGHLEVEL_LOCATION_ID = os.getenv("HIGHLEVEL_LOCATION_ID")
HIGHLEVEL_CALENDAR_ID = os.getenv("HIGHLEVEL_CALENDAR_ID", "GJ6IHyj6TLnGTW1iwOsL")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set.")

if not HIGHLEVEL_API_KEY:
    raise RuntimeError("HIGHLEVEL_API_KEY is not set.")

if not HIGHLEVEL_LOCATION_ID:
    print("WARNING: HIGHLEVEL_LOCATION_ID is not set – calendar booking will fail.")

if not HIGHLEVEL_CALENDAR_ID:
    print("WARNING: HIGHLEVEL_CALENDAR_ID is not set – calendar booking will fail.")

client = OpenAI(api_key=OPENAI_API_KEY)

# ============================================================
#        THIS FIXES THE FASTAPI ERROR ON RENDER
# ============================================================
app = FastAPI()

@app.get("/")
async def health():
    return {"status": "ok", "message": "Pulse John Bot is running"}


# ============================================================
#     IN-MEMORY CONVERSATION MEMORY PER CONTACT
# ============================================================

conversations: dict[str, dict] = {}

# ============================================================
#                  JOHN SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are “John”, a friendly, professional virtual assistant for Pulse Car Detailing.

You act like a real human team member. Keep replies short (1–3 sentences).
Never give prices. Never use emojis except 👍 in follow-ups.

You must ALWAYS reply ONLY as a JSON object with:

{
  "reply": "string",
  "action": "none" | "ask_for_day" | "ask_for_time" | "book_callback",
  "preferred_date_iso": "YYYY-MM-DD or null",
  "preferred_time_of_day": "morning" | "afternoon" | "evening" | null
}

Never output anything outside this JSON.
"""


# ============================================================
#                   CONTEXT BUILDER
# ============================================================

def build_context_text(payload: dict) -> tuple[str, dict]:
    contact = payload.get("contact") or payload.get("contactDetails") or {}
    custom = payload.get("custom") or payload.get("customFields") or {}

    # ----- Extract latest customer message -----
    last_message = None
    if isinstance(payload.get("message"), str):
        last_message = payload["message"]
    elif isinstance(payload.get("body"), str):
        last_message = payload["body"]
    elif isinstance(payload.get("conversation"), dict):
        msg = payload["conversation"].get("message")
        if isinstance(msg, str):
            last_message = msg

    # ----- Contact Info -----
    first_name = contact.get("firstName") or "there"

    services = custom.get("services_interested_in") or custom.get("Services Interested In")
    colour = custom.get("vehicle_colour") or custom.get("Vehicle Colour")
    condition = custom.get("vehicle_condition") or custom.get("Vehicle Condition")
    make_model = custom.get("vehicle_make_model") or custom.get("Vehicle Make & Model")
    year = custom.get("vehicle_year") or custom.get("Vehicle Year")

    # ----- Build context text for LLM -----
    lines = []

    lines.append(
        f"Customer name: {first_name}. "
        f"Vehicle: {year or 'unknown year'} {make_model or 'unknown model'} in {colour or 'unknown colour'}."
    )

    if services:
        lines.append(f"Services selected: {services}.")
    if condition:
        lines.append(f"Vehicle condition from survey: {condition}.")

    if last_message:
        lines.append(f"Latest customer message: {last_message}")
    else:
        lines.append("No customer message yet. Start based on survey.")

    return "\n".join(lines), contact


# ============================================================
#           CALL JOHN (OPENAI) WITH STATE MEMORY
# ============================================================

def call_john(contact_id: str, context_text: str) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    history = conversations.get(contact_id, {}).get("history", [])
    messages.extend(history[-6:])  # last 3 turns
    messages.append({"role": "user", "content": context_text})

    completion = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.4,
    )

    content = completion.choices[0].message.content

    try:
        data = json.loads(content)
    except:
        data = {
            "reply": content,
            "action": "none",
            "preferred_date_iso": None,
            "preferred_time_of_day": None,
        }

    # Save conversation
    history.append({"role": "user", "content": context_text})
    history.append({"role": "assistant", "content": data.get("reply", "")})
    conversations[contact_id] = {"history": history}

    return data


# ============================================================
#         SEND MESSAGE BACK INTO HIGHLEVEL CHAT
# ============================================================

def send_reply_to_highlevel(contact: dict, reply: str):
    if not reply:
        return

    location_id = contact.get("locationId") or HIGHLEVEL_LOCATION_ID
    contact_id = contact.get("id") or contact.get("contactId")

    if not (location_id and contact_id):
        print("Missing location/contact – cannot reply")
        return

    url = "https://services.leadconnectorhq.com/conversations/messages"

    headers = {
        "Authorization": f"Bearer {HIGHLEVEL_API_KEY}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }

    body = {
        "locationId": location_id,
        "contactId": contact_id,
        "type": "SMS",
        "message": reply,
        "source": "api",
    }

    resp = requests.post(url, headers=headers, json=body, timeout=15)
    print("HL reply:", resp.status_code, resp.text[:300])


# ============================================================
#                   CREATE APPOINTMENT
# ============================================================

def create_callback_appointment(contact: dict, date_iso: str, time_of_day: str):
    if not (HIGHLEVEL_LOCATION_ID and HIGHLEVEL_CALENDAR_ID):
        print("Calendar not configured – skipping booking")
        return

    try:
        base_date = datetime.fromisoformat(date_iso)
    except:
        print("Invalid date from model:", date_iso)
        return

    if time_of_day == "morning":
        hour = 10
    elif time_of_day == "evening":
        hour = 18
    else:
        hour = 14

    dt_local = base_date.replace(
        hour=hour, minute=0, tzinfo=ZoneInfo("Europe/London")
    )

    name = (
        f"{contact.get('firstName','')} {contact.get('lastName','')}".strip()
        or "Pulse Customer"
    )

    payload = {
        "locationId": HIGHLEVEL_LOCATION_ID,
        "calendarId": HIGHLEVEL_CALENDAR_ID,
        "selectedSlot": dt_local.isoformat(),
        "selectedTimezone": "Europe/London",
        "name": name,
        "email": contact.get("email"),
        "phone": contact.get("phone"),
    }

    url = "https://rest.gohighlevel.com/v1/appointments/"
    headers = {"Authorization": f"Bearer {HIGHLEVEL_API_KEY}", "Content-Type": "application/json"}

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    print("Booking status:", resp.status_code, resp.text[:300])


# ============================================================
#               RENDER-FIXED WEBHOOK ENDPOINT
# ============================================================

@app.post("/webhook/incoming")
async def webhook_incoming(request: Request):
    try:
        payload = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    print("Incoming HL webhook:", json.dumps(payload)[:800])

    contact = payload.get("contact") or {}
    contact_id = contact.get("id") or contact.get("contactId")

    if not contact_id:
        raise HTTPException(status_code=400, detail="Missing contactId")

    context_text, contact = build_context_text(payload)
    ai = call_john(contact_id, context_text)

    reply = ai.get("reply")
    action = ai.get("action")
    date_iso = ai.get("preferred_date_iso")
    time_of_day = ai.get("preferred_time_of_day")

    if reply:
        send_reply_to_highlevel(contact, reply)

    if action == "book_callback" and date_iso and time_of_day:
        create_callback_appointment(contact, date_iso, time_of_day)

    return JSONResponse({"status": "ok"})
