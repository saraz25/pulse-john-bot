import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI


# ============================================================
#             CONFIG
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

client = OpenAI(api_key=OPENAI_API_KEY)


# ============================================================
#             FASTAPI APP + HEALTH CHECK
# ============================================================

app = FastAPI()

@app.get("/")
async def health_get():
    return {"status": "ok", "message": "Pulse John Bot is running"}

@app.head("/")
async def health_head():
    return JSONResponse(status_code=200, content={})


# ============================================================
#          IN-MEMORY CONVERSATION MEMORY PER CONTACT
# ============================================================

conversations: dict[str, dict] = {}


# ============================================================
#             SYSTEM PROMPT FOR JOHN
# ============================================================

SYSTEM_PROMPT = """
You are “John”, a friendly, professional assistant for Pulse Car Detailing.
You ALWAYS reply in UK English and sound human, natural, and short.

================================================
CRITICAL COMPLIANCE RULE
================================================
You MUST NEVER send the first message.
If there is no customer message yet, return:

{
  "reply": "",
  "action": "none",
  "preferred_date_iso": null,
  "preferred_time_of_day": null
}

================================================
OUTPUT FORMAT (STRICT JSON)
================================================

{
  "reply": "short message",
  "action": "none" | "ask_for_day" | "ask_for_time" | "book_callback",
  "preferred_date_iso": "...",
  "preferred_time_of_day": "morning" | "afternoon" | "evening" | null
}

================================================
PERSONALITY
================================================
Short replies (1–3 sentences).
Warm, helpful, human.
No emojis except 👍.
No prices.
Never explain AI behaviour.

================================================
LOGIC
================================================
If price asked → say depends → suggest call.
If booking → ask day → ask time → then action=book_callback.
If unclear → ask short clarifying question.
If no reply → gentle follow-up.
"""


# ============================================================
#             CONTACT & PAYLOAD HELPERS
# ============================================================

def extract_contact_from_payload(payload: dict):
    contact = payload.get("contact") or payload.get("contactDetails") or {}
    if not isinstance(contact, dict):
        contact = {}

    contact_id = (
        contact.get("id")
        or payload.get("contact_id")
        or payload.get("contactId")
    )

    # Names
    for key in ["first_name", "firstName"]:
        if payload.get(key):
            contact["firstName"] = payload[key]

    for key in ["last_name", "lastName"]:
        if payload.get(key):
            contact["lastName"] = payload[key]

    # Location
    if not contact.get("locationId"):
        loc = payload.get("location")
        if isinstance(loc, dict) and loc.get("id"):
            contact["locationId"] = loc["id"]
        elif payload.get("locationId"):
            contact["locationId"] = payload["locationId"]

    # Phone
    contact["phone"] = (
        contact.get("phone")
        or payload.get("phone")
        or payload.get("phoneNumber")
        or payload.get("phone_number")
    )

    # Email
    contact["email"] = contact.get("email") or payload.get("email")

    return contact, contact_id



def extract_message(payload: dict):
    """Pull message text from any HL format."""
    keys = ["message", "body", "text"]

    for k in keys:
        if isinstance(payload.get(k), str):
            return payload[k]

    if isinstance(payload.get("conversation"), dict):
        msg = payload["conversation"].get("message")
        if isinstance(msg, str):
            return msg

    return None



def build_context_text(payload: dict):
    contact, _ = extract_contact_from_payload(payload)
    custom = (
        payload.get("custom")
        or payload.get("customFields")
        or payload.get("custom_fields")
        or {}
    )

    last_message = extract_message(payload)
    first_name = contact.get("firstName") or "there"

    # Vehicle data
    make_model = custom.get("Vehicle Make & Model") or custom.get("vehicle_make_model")
    year = custom.get("Vehicle Year") or custom.get("vehicle_year")
    colour = custom.get("Vehicle Colour") or custom.get("vehicle_colour")
    condition = custom.get("Vehicle Condition") or custom.get("vehicle_condition")
    services = custom.get("Services Interested In") or custom.get("services_interested_in")

    lines = []
    lines.append(f"Customer name: {first_name}.")
    lines.append(f"Vehicle: {year or 'unknown year'} {make_model or 'unknown model'} in {colour or 'unknown colour'}.")

    if services:
        lines.append(f"Services: {services}.")
    if condition:
        lines.append(f"Condition: {condition}.")

    if last_message:
        lines.append(f"Latest customer message: {last_message}")
    else:
        lines.append("There is no customer message yet. DO NOT REPLY.")

    return "\n".join(lines), contact


# ============================================================
#             CALL OPENAI
# ============================================================

def call_john(contact_id: str, context_text: str):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    history = conversations.get(contact_id, {}).get("history", [])
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": context_text})

    completion = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.4,
    )

    content = completion.choices[0].message.content

    try:
        ai = json.loads(content)
    except:
        print(f"ERROR: Failed to parse JSON from OpenAI. Raw content: {content[:100]}...")
        ai = {"reply": "", "action": "none", "preferred_date_iso": None, "preferred_time_of_day": None}

    # Update conversation history
    history.append({"role": "user", "content": context_text})
    history.append({"role": "assistant", "content": ai.get("reply", "")})
    conversations[contact_id] = {"history": history}

    print("AI OUTPUT:", ai)
    return ai


# ============================================================
#           SEND MESSAGE BACK INTO HIGHLEVEL (FIXED AUTHORIZATION)
# ============================================================

def send_reply_to_highlevel(contact: dict, reply: str):
    if not reply:
        return

    url = "https://services.leadconnectorhq.com/conversations/messages"

    headers = {
        "hl-api-key": HIGHLEVEL_API_KEY, 
        "Authorization": f"Bearer {HIGHLEVEL_API_KEY}", # CRITICAL FIX for 401 Error
        "Version": "2021-07-28", 
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    body = {
        "locationId": contact.get("locationId") or HIGHLEVEL_LOCATION_ID,
        "contactId": contact.get("id"),
        "type": "SMS",
        "message": reply,
        "source": "api",
    }

    print("Sending HL reply:", body)
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    
    # Check for success (200 or 201) before printing status
    if resp.status_code in [200, 201]:
        print(f"HL REPLY STATUS: {resp.status_code} - Message sent successfully.")
    else:
        print(f"HL REPLY STATUS: {resp.status_code} - ERROR: {resp.text[:500]}")
    


# ============================================================
#             CREATE CALLBACK APPOINTMENT
# ============================================================

def create_callback_appointment(contact: dict, date_iso: str, time_of_day: str):
    if not date_iso:
        return

    try:
        base_date = datetime.fromisoformat(date_iso)
    except:
        print("Invalid date:", date_iso)
        return

    hour = {"morning": 10, "afternoon": 14, "evening": 18}.get(time_of_day, 14)

    dt_local = base_date.replace(
        hour=hour, minute=0, second=0, microsecond=0,
        tzinfo=ZoneInfo("Europe/London")
    )

    payload = {
        "locationId": HIGHLEVEL_LOCATION_ID,
        "calendarId": HIGHLEVEL_CALENDAR_ID,
        "selectedSlot": dt_local.isoformat(),
        "selectedTimezone": "Europe/London",
        "name": (contact.get("firstName","") + " " + contact.get("lastName","")).strip(),
        "email": contact.get("email"),
        "phone": contact.get("phone"),
    }

    url = "https://rest.gohighlevel.com/v1/appointments/"
    headers = {
        "hl-api-key": HIGHLEVEL_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    print("APPOINTMENT STATUS:", resp.status_code, resp.text[:500])


# ============================================================
#             WEBHOOK ENDPOINT (PROACTIVE FIRST MESSAGE)
# ============================================================

@app.post("/webhook/incoming")
async def webhook_incoming(request: Request):

    try:
        payload = await request.json()
    except:
        payload = None

    if not payload:
        try:
            form = await request.form()
            payload = dict(form)
        except:
            raise HTTPException(status_code=400, detail="Invalid HL payload format")

    print("RAW HL PAYLOAD:", json.dumps(payload)[:800])

    contact, contact_id = extract_contact_from_payload(payload)
    if not contact_id:
        raise HTTPException(status_code=400, detail="Missing contactId/contact_id")

    contact["id"] = contact_id
    
    # ------------------------------------------------------------------
    # NEW LOGIC: Inject first question if no customer reply is found.
    # ------------------------------------------------------------------
    last_message = extract_message(payload)
    
    # Check if this is the absolute start of the conversation (no message, no history).
    if not last_message and not conversations.get(contact_id):
        
        # This is the question you want the bot to ask proactively
        forced_reply = "Great, could you tell me a little more about the condition of the vehicle?"
        
        # Send the forced reply to HighLevel immediately. This uses the fixed headers.
        send_reply_to_highlevel(contact, forced_reply)
        
        # Return a success response to the HL workflow.
        print("INFO: No customer message found. Sending forced first question.")
        return JSONResponse({
            "reply": forced_reply,
            "action": "none",
            "preferred_date_iso": None,
            "preferred_time_of_day": None
        })
    # ------------------------------------------------------------------

    # If a customer message is found, or history exists, proceed to call the AI as normal.
    context_text, contact = build_context_text(payload)

    ai = call_john(contact_id, context_text)

    reply = ai.get("reply")
    action = ai.get("action")
    date_iso = ai.get("preferred_date_iso")
    time_of_day = ai.get("preferred_time_of_day")

    # Send the AI's reply back to the contact via the HL API
    if reply:
        send_reply_to_highlevel(contact, reply)

    # Create the callback appointment if the action is set
    if action == "book_callback" and time_of_day:
        create_callback_appointment(contact, date_iso, time_of_day)

    # Return the final JSON response to the HighLevel workflow engine
    return JSONResponse({"status": "ok"})
