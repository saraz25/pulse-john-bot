import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI


# ============================================================
# CONFIG
# ============================================================

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

HIGHLEVEL_API_KEY = os.getenv("HIGHLEVEL_API_KEY")
HIGHLEVEL_LOCATION_ID = os.getenv("HIGHLEVEL_LOCATION_ID")
HIGHLEVEL_CALENDAR_ID = os.getenv("HIGHLEVEL_CALENDAR_ID")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set.")
if not HIGHLEVEL_API_KEY:
    raise RuntimeError("HIGHLEVEL_API_KEY is not set.")
if not HIGHLEVEL_CALENDAR_ID:
    raise RuntimeError("HIGHLEVEL_CALENDAR_ID is not set.")

client = OpenAI(api_key=OPENAI_API_KEY)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()

@app.get("/")
async def health():
    return {"status": "ok", "service": "Pulse John Bot"}


# ============================================================
# IN-MEMORY STATE (v1)
# ============================================================

conversations: dict[str, dict] = {}
booking_locks: dict[str, bool] = {}


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are John, a friendly and professional assistant for Pulse Car Detailing.
You speak UK English only and sound human, calm, and natural.

Rules:
- Never send the first message.
- Never confirm a booking as complete.
- You may say “I’ll get that booked in for you”.
- Final confirmation is sent only after the system books successfully.

Output strict JSON only:

{
  "reply": "string",
  "action": "none" | "ask_for_day" | "ask_for_time" | "book_callback",
  "preferred_date_iso": "YYYY-MM-DD or null",
  "preferred_time_of_day": "morning" | "afternoon" | null
}
"""


# ============================================================
# HELPERS
# ============================================================

def extract_contact(payload: dict):
    contact = payload.get("contact") or {}
    contact_id = (
        contact.get("id")
        or payload.get("contactId")
        or payload.get("contact_id")
    )

    contact["id"] = contact_id
    contact["phone"] = contact.get("phone") or payload.get("phone")
    contact["email"] = contact.get("email") or payload.get("email")
    contact["locationId"] = contact.get("locationId") or payload.get("locationId")

    return contact, contact_id


def extract_message(payload: dict):
    if isinstance(payload.get("message"), dict):
        return payload["message"].get("body")
    return payload.get("body") or payload.get("text")


def build_context(payload: dict):
    contact, _ = extract_contact(payload)
    msg = extract_message(payload)

    name = contact.get("firstName", "there")

    lines = [f"Customer name: {name}."]
    if msg:
        lines.append(f"Latest customer message: {msg}")
    else:
        lines.append("There is no customer message yet. DO NOT REPLY.")

    return "\n".join(lines)


def resolve_natural_date(text: str):
    if not text:
        return None

    today = datetime.now(ZoneInfo("Europe/London")).date()
    t = text.lower()

    if "today" in t:
        return today.isoformat()
    if "tomorrow" in t:
        return (today.replace(day=today.day + 1)).isoformat()

    return None


def get_available_slots(date_iso: str):
    resp = requests.get(
        "https://rest.gohighlevel.com/v1/appointments/slots",
        headers={"hl-api-key": HIGHLEVEL_API_KEY},
        params={
            "calendarId": HIGHLEVEL_CALENDAR_ID,
            "startDate": date_iso,
            "endDate": date_iso,
            "timezone": "Europe/London",
        },
        timeout=15,
    )

    if resp.status_code != 200:
        return []

    return resp.json().get("slots", [])


def pick_slot(slots, time_of_day):
    for slot in slots:
        dt = datetime.fromisoformat(slot)
        if time_of_day == "morning" and dt.hour < 12:
            return slot
        if time_of_day == "afternoon" and dt.hour >= 12:
            return slot
    return None


def book_appointment(contact, slot_iso):
    resp = requests.post(
        "https://rest.gohighlevel.com/v1/appointments/",
        headers={"hl-api-key": HIGHLEVEL_API_KEY},
        json={
            "locationId": contact.get("locationId") or HIGHLEVEL_LOCATION_ID,
            "calendarId": HIGHLEVEL_CALENDAR_ID,
            "selectedSlot": slot_iso,
            "selectedTimezone": "Europe/London",
            "name": contact.get("firstName", ""),
            "phone": contact.get("phone"),
            "email": contact.get("email"),
        },
        timeout=20,
    )
    return resp.status_code in (200, 201)


def send_reply(contact, text):
    if not text:
        return

    requests.post(
        "https://services.leadconnectorhq.com/conversations/messages",
        headers={
            "hl-api-key": HIGHLEVEL_API_KEY,
            "Authorization": f"Bearer {HIGHLEVEL_API_KEY}",
            "Version": "2021-07-28",
            "Content-Type": "application/json",
        },
        json={
            "locationId": contact.get("locationId") or HIGHLEVEL_LOCATION_ID,
            "contactId": contact.get("id"),
            "type": "SMS",
            "message": text,
            "source": "api",
        },
        timeout=15,
    )


def call_john(contact_id: str, context: str):
    history = conversations.get(contact_id, {}).get("history", [])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": context})

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.4,
    )

    data = json.loads(resp.choices[0].message.content)

    history.append({"role": "user", "content": context})
    history.append({"role": "assistant", "content": data.get("reply", "")})
    conversations[contact_id] = {"history": history}

    return data


# ============================================================
# WEBHOOK
# ============================================================

@app.post("/webhook/incoming")
async def webhook(request: Request):

    payload = await request.json()
    contact, contact_id = extract_contact(payload)

    if not contact_id:
        raise HTTPException(status_code=400, detail="Missing contactId")

    if conversations.get(contact_id, {}).get("booked"):
        return JSONResponse({"status": "already-booked"})

    context = build_context(payload)
    ai = call_john(contact_id, context)

    reply = ai.get("reply")
    action = ai.get("action")
    time_of_day = ai.get("preferred_time_of_day")

    date_iso = ai.get("preferred_date_iso") or resolve_natural_date(
        extract_message(payload)
    )

    if reply:
        send_reply(contact, reply)

    if action == "book_callback" and date_iso and time_of_day:

        if booking_locks.get(contact_id):
            return JSONResponse({"status": "locked"})

        booking_locks[contact_id] = True

        try:
            slots = get_available_slots(date_iso)
            slot = pick_slot(slots, time_of_day)

            if slot and book_appointment(contact, slot):
                conversations.setdefault(contact_id, {})["booked"] = True
                send_reply(contact, "All set 👍 one of the team will give you a call then.")
            else:
                send_reply(
                    contact,
                    "I can’t see availability at that time — would another day or time work?"
                )
        finally:
            booking_locks.pop(contact_id, None)

    return JSONResponse({"status": "ok"})
