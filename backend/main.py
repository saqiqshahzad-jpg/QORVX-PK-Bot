import os
import sys
import time
import json
import re
import logging
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from groq import Groq
from google.oauth2.service_account import Credentials
import gspread

# =========================================================================================
# CONFIGURATION & LOGGING
# =========================================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
logger = logging.getLogger("QORVX_PK")

app = FastAPI()

# ENV VARS (Assume configured in deployment)
MY_VERIFY_TOKEN = os.getenv("MY_VERIFY_TOKEN", "qorvx_pk_secret")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WHATSAPP_API_VERSION = "v25.0"

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

PROCESSED_MSG_IDS = {}

# =========================================================================================
# SUPABASE DATABASE LAYER
# =========================================================================================
def get_supabase_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}

def get_tenant_config(tenant_id: str):
    try:
        url = f"{SUPABASE_URL}/rest/v1/vencode_tenants?tenant_id=eq.{tenant_id}&select=*"
        res = requests.get(url, headers=get_supabase_headers(), timeout=10)
        logger.info(f"🔍 Supabase fetch status: {res.status_code} | response: {res.text}")
        if res.status_code == 200 and res.json():
            return res.json()[0]
    except Exception as e:
        logger.error(f"Supabase Tenant fetch failed: {e}")
    return {}

def get_user_session(phone: str, tenant_id: str):
    default_session = {
        "purpose": None, "property_type": None, "bhk": None, "location": None, 
        "budget": None, "state": None, "funnel_state": None, 
        "awaiting_confirmation": False, "search_confirmed": False, "chat_history": [], 
        "active_property": None, "archived_intents": [], "last_interaction": time.time()
    }
    try:
        url = f"{SUPABASE_URL}/rest/v1/user_sessions?phone_number=eq.{phone}&tenant_id=eq.{tenant_id}&select=*"
        res = requests.get(url, headers=get_supabase_headers(), timeout=10)
        if res.status_code == 200 and res.json():
            data = res.json()[0].get("session_data", {})
            return {**default_session, **data}
    except Exception as e:
        logger.error(f"Supabase Session fetch failed: {e}")
    return default_session

def save_user_session(phone: str, tenant_id: str, session: dict):
    session["last_interaction"] = time.time()
    payload = {"phone_number": phone, "tenant_id": tenant_id, "session_data": session}
    try:
        url = f"{SUPABASE_URL}/rest/v1/user_sessions"
        headers = get_supabase_headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Supabase Session save failed: {e}")

def save_chat_history(phone: str, tenant_id: str, role: str, content: str):
    payload = {"phone_number": phone, "tenant_id": tenant_id, "role": role, "content": content}
    try:
        url = f"{SUPABASE_URL}/rest/v1/whatsapp_history"
        requests.post(url, headers=get_supabase_headers(), json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Supabase History save failed: {e}")

# =========================================================================================
# WHATSAPP UTILS
# =========================================================================================
def send_whatsapp_text(tenant_id: str, phone: str, text: str, token: str):
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"WA Text Send Failed: {e}")

def send_whatsapp_image(tenant_id: str, phone: str, image_url: str, caption: str, token: str):
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption
        }
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"WA Image Send Failed: {e}")

def send_whatsapp_buttons(tenant_id: str, phone: str, text: str, buttons: list, token: str):
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    actions = []
    for btn in buttons:
        btn_id = btn.lower().replace(" ", "_").strip("🏠🏢🤝➕")
        actions.append({"type": "reply", "reply": {"id": f"btn_{btn_id}", "title": btn}})
    
    payload = {
        "messaging_product": "whatsapp", "to": phone, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": actions[:3]}
        }
    }
    requests.post(url, headers=headers, json=payload, timeout=10)

def download_audio_and_transcribe(audio_id: str, token: str):
    url = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/{audio_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200: return None
        media_url = res.json().get("url")
        media_res = requests.get(media_url, headers=headers, timeout=15)
        
        file_path = f"/tmp/{audio_id}.ogg"
        with open(file_path, "wb") as f:
            f.write(media_res.content)
            
        if groq_client:
            with open(file_path, "rb") as file:
                transcription = groq_client.audio.transcriptions.create(
                    file=(file_path, file.read()), model="whisper-large-v3", language="en"
                )
            return transcription.text
    except Exception as e:
        logger.error(f"Audio processing failed: {e}")
    return None

# =========================================================================================
# GOOGLE SHEETS CRM
# =========================================================================================
class GoogleSheetCRM:
    def __init__(self, sheet_id: str):
        self.sheet_id = sheet_id
        try:
            self.client = gspread.service_account()
            self.doc = self.client.open_by_key(sheet_id)
        except:
            self.client = None

    def append_lead(self, phone: str, name: str, email: str, prop_id: str):
        if not self.client: return False
        try:
            sheet = self.doc.worksheet("Leads")
            sheet.append_row([time.strftime("%Y-%m-%d %H:%M:%S"), phone, name, email, prop_id, "PK"])
            return True
        except:
            return False

    def book_strategy(self, phone: str, date: str, time_str: str):
        if not self.client: return False, "System error"
        try:
            sheet = self.doc.worksheet("BookingSlot")
            records = sheet.get_all_records()
            for r in records:
                if str(r.get("Date")) == date and str(r.get("Time")) == time_str:
                    return False, "Slot full"
            
            sheet.append_row([date, time_str, "", phone, "", "Reserved"])
            return True, "Success"
        except:
            return False, "System error"

    def search_properties(self, location, property_type, purpose, bhk=None, budget=None, limit=2):
        if not self.client:
            return self._mock_properties(location, property_type, bhk, budget)
        try:
            sheet = self.doc.worksheet("Properties")
            records = sheet.get_all_records()
            results = []
            for r in records:
                r_loc = str(r.get("Location", "")).lower()
                r_type = str(r.get("Type", "")).lower()
                r_purpose = str(r.get("Purpose", "")).lower()
                if location and location.lower() not in r_loc: continue
                if property_type and property_type.lower() not in r_type: continue
                if purpose and purpose.lower() not in r_purpose: continue
                results.append(r)
                if len(results) >= limit: break
            return results if results else self._mock_properties(location, property_type, bhk, budget)
        except:
            return self._mock_properties(location, property_type, bhk, budget)

    def _mock_properties(self, location, property_type, bhk, budget):
        loc = location or "Prime Location"
        ptype = (property_type or "Property").title()
        rooms = f"{bhk} BHK " if bhk else ""
        price = f"{budget/10000000:g} Crore" if budget else "Contact for Price"
        return [
            {"Title": f"Premium {rooms}{ptype}", "Location": loc, "Price": price, "Description": "Beautifully designed with modern amenities.", "ID": "PR-101", "Image": "https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?ixlib=rb-4.0.3&auto=format&fit=crop&w=800&q=80"},
            {"Title": f"Luxury {rooms}{ptype}", "Location": loc, "Price": price, "Description": "Spacious and well-ventilated with great view.", "ID": "PR-102", "Image": "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?ixlib=rb-4.0.3&auto=format&fit=crop&w=800&q=80"}
        ]

def format_search_confirmation(session):
    maqsad_map = {"buy": "Kharidna", "sell": "Bechna", "rent": "Rent"}
    maqsad = maqsad_map.get(session.get("purpose", ""), str(session.get("purpose", "-")).title())
    
    loc = str(session.get("location", "-")).title()
    ptype = str(session.get("property_type", "-")).title()
    
    bhk = "-"
    if ptype.lower() not in ["plot", "warehouse", "zameen", "-"]:
        bhk = f"{session.get('bhk')} Bedrooms" if session.get("bhk") else "-"
        
    budget = "-"
    if session.get("budget"):
        b_val = session["budget"]
        if b_val >= 10000000:
            budget = f"{b_val / 10000000:g} Crore"
        elif b_val >= 100000:
            budget = f"{b_val / 100000:g} Lakh"
        else:
            budget = str(b_val)
            
    text = f"Behtareen! Aapki search details yeh hain:\n\n"
    text += f"🎯 Maqsad: {maqsad}\n"
    text += f"📍 Location: {loc}\n"
    text += f"🏢 Property Type: {ptype}\n"
    text += f"🛏️ Bedrooms: {bhk}\n"
    text += f"💰 Budget: {budget}\n\n"
    text += "Kya aap in details ko confirm karte hain?"
    return text

def execute_property_search(session, tenant_config, wa_token, from_number, tenant_id, chat_hist):
    send_whatsapp_text(tenant_id, from_number, "Property search start kar di gayi hai... 🔍", wa_token)
    
    crm = GoogleSheetCRM(tenant_config.get("property_sheet_name", ""))
    properties = crm.search_properties(
        location=session.get("location"),
        property_type=session.get("property_type"),
        purpose=session.get("purpose"),
        bhk=session.get("bhk"),
        budget=session.get("budget")
    )
    
    if properties:
        for idx, p in enumerate(properties, 1):
            title = p.get('Title', f"Property {idx}")
            price = p.get('Price', 'N/A')
            loc = p.get('Location', session.get('location', 'N/A'))
            desc = p.get('Description', '')
            prop_id = p.get('ID', f"ID-{idx}")
            image_url = p.get('Image', '')
            
            caption = f"*{title}*\n📍 {loc}\n💰 {price}\n📝 {desc}\n🆔 (ID: {prop_id})"
            
            if image_url:
                send_whatsapp_image(tenant_id, from_number, image_url, caption, wa_token)
            else:
                send_whatsapp_text(tenant_id, from_number, caption, wa_token)
        
        after_msg = "Inmein se koi pasand aaya ya mazeed options dekhne hain? 👇"
        buttons = ["Sasta option 📉", "Koi aur option 🔄", "Visit karna 📅"]
        send_whatsapp_buttons(tenant_id, from_number, after_msg, buttons, wa_token)
        
        chat_hist.append({"role": "assistant", "content": f"Sent properties and options."})
    else:
        fail_msg = "Filhal is criteria ke mutabiq koi exact match nahi mila, lekin humari team is par kaam kar rahi hai aur jald aapko update karegi! ⏳"
        send_whatsapp_text(tenant_id, from_number, fail_msg, wa_token)
        send_whatsapp_buttons(tenant_id, from_number, "Kiya tabdeel karna chahte hain?", ["Change Location", "Change Budget", "Restart"], wa_token)
        chat_hist.append({"role": "assistant", "content": fail_msg})

# =========================================================================================
# NLP PARAMETER EXTRACTION
# =========================================================================================
def parse_south_asian_budget(text: str):
    text = text.lower().replace(",", "")
    match = re.search(r'([\d.]+)\s*(lac|lakh|crore|karor|cr|k|m|pkr)', text)
    if not match:
        digits = re.search(r'\b(\d{5,10})\b', text)
        return int(digits.group(1)) if digits else None
    
    val, unit = float(match.group(1)), match.group(2)
    if unit in ['lac', 'lakh']: return int(val * 100000)
    if unit in ['crore', 'karor', 'cr']: return int(val * 10000000)
    if unit == 'k': return int(val * 1000)
    if unit == 'm': return int(val * 1000000)
    return int(val)

def extract_bhk(text: str, prop_type: str):
    if prop_type in ["plot", "warehouse", "zameen"]: return None
    match = re.search(r'(\d+)\s*(bhk|bed|bedroom|br|beds)', text.lower())
    return int(match.group(1)) if match else None

def extract_location(text: str, last_ai: str):
    PK_LOCS = ["dha", "bahria", "clifton", "gulberg", "johar", "blue area", "f-11", "f-10"]
    for loc in PK_LOCS:
        if loc in text.lower(): return loc.title()
    
    if len(text.split()) <= 3 and any(kw in last_ai.lower() for kw in ["location", "city", "area"]):
        if not text.isdigit() and text.lower() not in ["buy", "rent", "yes", "no"]:
            return text.title()
    return None

# =========================================================================================
# LLM ENGINE
# =========================================================================================
PK_MASTER_PROMPT = """You are Qorvx PK Bot, a luxury real estate AI concierge for Pakistan.
OUTPUT ONLY JSON.

{
  "_thinking": "Internal logic",
  "intent": "search" | "qa" | "confirm_change" | "execute_search",
  "location": "string | null",
  "purpose": "buy" | "rent" | "sell" | null,
  "property_type": "house" | "flat" | "plot" | "warehouse" | null,
  "bhk": integer | null,
  "budget": integer | null,
  "reply_text": "Professional pure Pakistani Roman Urdu response"
}

RULES:
1. Iron Dome: If off-topic (politics, coding), intent="qa" and reply="Maazrat Janab 🙏... Mera kaam sirf property kharidne aur bechne tak mehdood hai. 🏢"
2. Contradiction: If user changes param, set intent="confirm_change".
3. Smart QA: Weave property loc/size into answer.
4. Marla Trap: Marla/Kanal/Sqft are LAND size. Do NOT put in `bhk`.
5. NEVER ask for information already provided.
6. Language: STRICTLY use pure Pakistani Roman Urdu (e.g., "Masla nahi", "Zabardast", "Bohot aala", "Sir/Madam"). DO NOT use Indian terms like "kripya", "dhanyawad", "namaste", "badhiya".
7. Emojis: ALWAYS include relevant emojis in your `reply_text` to make it engaging! ✨
"""

def chat_completion_fallback(messages: list):
    try:
        # Try Gemini (assuming OpenAI compatible endpoint or google genai)
        res = requests.post(f"https://generativelanguage.googleapis.com/v1beta/openai/chat/completions?key={GEMINI_API_KEY}", json={"model": "gemini-3.6-flash", "messages": messages, "temperature": 0.4}, timeout=15)
        if res.status_code == 200: return res.json()["choices"][0]["message"]["content"]
    except Exception as e: 
        logger.warning(f"Gemini skipped: {e}")
    
    if groq_client:
        # List of free Groq models to try one by one
        groq_models = [
            "qwen/qwen3.8-27b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
            "openai/gpt-oss-20b",
            "groq/compound"
        ]
        
        for model_name in groq_models:
            try:
                logger.info(f"🤖 Trying Groq model: {model_name}")
                comp = groq_client.chat.completions.create(model=model_name, messages=messages, temperature=0.4)
                return comp.choices[0].message.content
            except Exception as e:
                err_msg = str(e)
                if hasattr(e, 'response'):
                    err_msg += f" - {e.response.text}"
                logger.warning(f"⚠️ Groq model '{model_name}' failed: {err_msg}")
                continue
                
        logger.error("❌ All Groq models failed!")
    
    return "Janab, system par is waqt thora load hai... 10 second baad dobara bhejein."

# =========================================================================================
# WEBHOOK ENDPOINTS & DISPATCHER
# =========================================================================================
@app.get('/webhook')
def verify_webhook(request: Request):
    if request.query_params.get("hub.verify_token") == MY_VERIFY_TOKEN:
        return PlainTextResponse(content=str(request.query_params.get("hub.challenge")))
    return PlainTextResponse(content="Error", status_code=403)

@app.post('/webhook')
async def receive_webhook(request: Request, bg_tasks: BackgroundTasks):
    data = await request.json()
    bg_tasks.add_task(process_whatsapp_data, data)
    return PlainTextResponse(content="OK")

def process_whatsapp_data(data: dict):
    logger.info(f"🔔 Webhook data received: object={data.get('object')}, entries={len(data.get('entry', []))}")
    if not data.get("object") or not data.get("entry"):
        logger.warning("❌ No 'object' or 'entry' in webhook data — skipping")
        return
    
    for entry in data["entry"]:
        for change in entry.get("changes", []):
            val = change.get("value", {})
            tenant_id = val.get("metadata", {}).get("phone_number_id")
            if not tenant_id:
                logger.warning("❌ No tenant_id found in metadata — skipping")
                continue
            
            logger.info(f"🏢 Tenant ID: {tenant_id}")
            tenant_config = get_tenant_config(tenant_id)
            logger.info(f"🏢 Tenant config keys: {list(tenant_config.keys()) if tenant_config else 'EMPTY'}")
            wa_token = tenant_config.get("whatsapp_token")
            if not wa_token:
                logger.error(f"❌ No whatsapp_token found for tenant {tenant_id} — bot cannot reply!")
                continue
            
            msgs = val.get("messages", [])
            logger.info(f"📨 Messages count: {len(msgs)}")
            if not msgs:
                logger.info("ℹ️ No messages in this webhook (probably a status update)")
            
            for msg in msgs:
              try:
                from_number = msg["from"]
                msg_id = msg.get("id")
                logger.info(f"📩 Message from {from_number} | type={msg.get('type')} | id={msg_id}")
                
                # Dedup Engine
                now = time.time()
                if msg_id in PROCESSED_MSG_IDS:
                    msg_body = msg.get("text", {}).get("body", "").lower()
                    if msg_body != "menu": continue
                PROCESSED_MSG_IDS[msg_id] = now
                stale = [k for k, v in PROCESSED_MSG_IDS.items() if now - v > 300]
                for k in stale: del PROCESSED_MSG_IDS[k]
                
                msg_body, btn_id = "", ""
                msg_type = msg.get("type")
                
                if msg_type == "text":
                    msg_body = msg["text"]["body"].strip()
                elif msg_type == "interactive":
                    if msg["interactive"]["type"] == "button_reply":
                        msg_body = msg["interactive"]["button_reply"]["title"]
                        btn_id = msg["interactive"]["button_reply"]["id"]
                elif msg_type == "audio":
                    transcription = download_audio_and_transcribe(msg["audio"]["id"], wa_token)
                    if transcription: msg_body = transcription
                    else:
                        send_whatsapp_text(tenant_id, from_number, "Awaz clear nahi mili. Please type karein.", wa_token)
                        return
                else:
                    lock_msg = f"Arre wah, seedha {msg_type}? Lekin ek choti si rukawat hai, yeh demo version hai, isliye live media-scanning ka feature abhi restricted rakha gaya hai taake server load na barhe. Asli version mein AI khud tasveer parh kar rate bata deta hai. Batayein, filhal text mein koi property search karni hai?"
                    send_whatsapp_text(tenant_id, from_number, lock_msg, wa_token)
                    return
                
                if not msg_body: continue
                logger.info(f"💬 Processing: '{msg_body}' from {from_number}")
                
                session = get_user_session(from_number, tenant_id)
                chat_hist = session["chat_history"]
                
                # Initial Greeting or Returning User
                is_new_session = not chat_hist
                is_stale_session = now - session.get("last_interaction", now) > 86400 # 24 hours memory
                is_greeting = msg_body.lower() in ["hi", "hello", "salam", "assalam o alaikum", "menu", "start", "hey"]

                if (is_new_session or is_stale_session) and is_greeting:
                    msg = "Assalam o Alaikum! 🙏 Qorvx PK Bot mein khush amdeed. Main aapki property ke hawale se kaise madad kar sakta hoon? 👇"
                    send_whatsapp_buttons(tenant_id, from_number, msg, ["Kharidni hai 🏠", "Rent pr leni hai 🏢", "Bechni hai 🤝"], wa_token)
                    chat_hist.append({"role": "user", "content": msg_body})
                    chat_hist.append({"role": "assistant", "content": msg})
                    session["chat_history"] = chat_hist[-50:]
                    save_chat_history(from_number, tenant_id, "user", msg_body)
                    save_chat_history(from_number, tenant_id, "assistant", msg)
                    save_user_session(from_number, tenant_id, session)
                    return

                # Lead Capture Booking Engine
                if session.get("funnel_state") == "AWAITING_VISIT_INFO":
                    # Simplified verification 
                    if len(msg_body.split()) <= 4:
                        crm = GoogleSheetCRM(tenant_config.get("property_sheet_name"))
                        crm.append_lead(from_number, msg_body, "", session.get("active_property", "Unknown"))
                        session["funnel_state"] = "COMPLETED"
                        send_whatsapp_text(tenant_id, from_number, "Shukriya! Aapki details register ho gayi hain. Humari team jald raabta karegi.", wa_token)
                        save_user_session(from_number, tenant_id, session)
                        return

                # Interactive Fast-Track
                if btn_id:
                    ai_reply = ""
                    if "buy" in btn_id or "kharidni" in btn_id:
                        session["purpose"] = "buy"
                        ai_reply = "Zabardast! 🎉 Kis shehar ya area (Location) mein property dekh rahe hain? 📍"
                    elif "rent" in btn_id:
                        session["purpose"] = "rent"
                        ai_reply = "Theek hai! 👍 Kis location pe rent ke liye dekhna hai? 📍"
                    elif "sell" in btn_id or "bechni" in btn_id:
                        session["purpose"] = "sell"
                        session["state"] = "ASKING_SELL_TYPE"
                        ai_reply = "Aap kya bechna chahte hain? 🏡 (Ghar, Plot, Commercial?)"
                    elif "change" in btn_id:
                        session["search_confirmed"] = False
                        session["awaiting_confirmation"] = False
                        ai_reply = "Bilkul! Aap kya tabdeel karna chahte hain? 🔄 (Jaise: 'Budget 5 Crore' ya 'Location DHA')"
                    elif "confirm" in btn_id:
                        session["search_confirmed"] = True
                        logger.info(f"🔍 Starting property search for session: {session}")
                        execute_property_search(session, tenant_config, wa_token, from_number, tenant_id, chat_hist)
                        ai_reply = ""
                    elif "sasta" in btn_id:
                        if session.get("budget"): session["budget"] = int(session["budget"] * 0.8)
                        session["search_confirmed"] = True
                        logger.info(f"🔍 Starting sasta property search for session: {session}")
                        execute_property_search(session, tenant_config, wa_token, from_number, tenant_id, chat_hist)
                        ai_reply = ""
                    elif "aur" in btn_id:
                        session["search_confirmed"] = True
                        logger.info(f"🔍 Starting alternative property search for session: {session}")
                        execute_property_search(session, tenant_config, wa_token, from_number, tenant_id, chat_hist)
                        ai_reply = ""
                    elif "visit" in btn_id:
                        session["state"] = "SCHEDULING_VISIT"
                        session["funnel_state"] = "AWAITING_VISIT_INFO"
                        ai_reply = "Visit book karne ke liye apna Pura Naam likh kar bhejein: 📝"
                    
                    if ai_reply:
                        send_whatsapp_text(tenant_id, from_number, ai_reply, wa_token)
                        chat_hist.append({"role": "user", "content": msg_body})
                        chat_hist.append({"role": "assistant", "content": ai_reply})
                        session["chat_history"] = chat_hist[-50:]
                        save_chat_history(from_number, tenant_id, "user", msg_body)
                        save_chat_history(from_number, tenant_id, "assistant", ai_reply)
                    
                    save_user_session(from_number, tenant_id, session)
                    return

                # Verbal Confirmation Interceptor
                if session.get("awaiting_confirmation"):
                    if re.search(r'\b(yes|haan|theek|done)\b', msg_body.lower()):
                        session["search_confirmed"] = True
                        logger.info(f"🔍 Starting property search for session: {session}")
                        execute_property_search(session, tenant_config, wa_token, from_number, tenant_id, chat_hist)
                        save_user_session(from_number, tenant_id, session)
                        return
                    elif re.search(r'\b(change|badal|galat|no|nahi)\b', msg_body.lower()):
                        session["awaiting_confirmation"] = False
                        send_whatsapp_text(tenant_id, from_number, "Bilkul! Kya tabdeel karna chahte hain? 🔄 (Jaise: 'Budget 8 Crore' ya 'Location Bahria Town')", wa_token)
                        save_user_session(from_number, tenant_id, session)
                        return
                    else:
                        session["awaiting_confirmation"] = False

                # Intent Shift Detection (House -> Plot)
                if session["property_type"] == "house" and any(w in msg_body.lower() for w in ["plot", "zameen"]):
                    session["archived_intents"].append({"type": "house", "budget": session.get("budget"), "loc": session.get("location")})
                    session.update({"property_type": "plot", "budget": None, "bhk": None, "location": None, "state": None, "active_property": None})
                    send_whatsapp_text(tenant_id, from_number, "Note kar liya, aap ab plot dekhna chahte hain. Kya aap kharidna chahte hain ya bechna?", wa_token)
                    save_user_session(from_number, tenant_id, session)
                    return

                # NLP Extraction
                bhk = extract_bhk(msg_body, session.get("property_type"))
                if bhk: session["bhk"] = bhk
                
                budget = parse_south_asian_budget(msg_body)
                if budget: session["budget"] = budget
                
                last_ai = chat_hist[-1]["content"] if chat_hist else ""
                loc = extract_location(msg_body, last_ai)
                if loc: session["location"] = loc

                # LLM State
                logger.info(f"🤖 Calling LLM for {from_number}...")
                sys_prompt = PK_MASTER_PROMPT + f"\n\nCURRENT SESSION STATE: {json.dumps(session)}"
                messages = [{"role": "system", "content": sys_prompt}]
                messages.extend(chat_hist[-40:])
                messages.append({"role": "user", "content": msg_body})
                
                llm_res = chat_completion_fallback(messages)
                logger.info(f"✅ LLM Response received: {llm_res[:100]}...")
                
                # Parse LLM JSON
                ai_reply = llm_res
                if "{" in llm_res and "}" in llm_res:
                    try:
                        s_idx, e_idx = llm_res.find("{"), llm_res.rfind("}") + 1
                        parsed = json.loads(llm_res[s_idx:e_idx])
                        
                        for k in ["location", "purpose", "property_type", "bhk", "budget"]:
                            if parsed.get(k): session[k] = parsed[k]
                            
                        if parsed.get("intent") == "qa":
                            ai_reply = parsed.get("reply_text", llm_res)
                        elif parsed.get("intent") == "execute_search" or (all(session.get(k) for k in ["purpose", "location", "budget", "property_type"]) and parsed.get("intent") != "qa"):
                            session["awaiting_confirmation"] = True
                            ai_reply = format_search_confirmation(session)
                        else:
                            ai_reply = parsed.get("reply_text", llm_res)
                    except Exception as parse_err:
                        logger.warning(f"⚠️ JSON parse failed: {parse_err}")
                
                chat_hist.append({"role": "user", "content": msg_body})
                chat_hist.append({"role": "assistant", "content": ai_reply})
                session["chat_history"] = chat_hist[-50:]
                
                save_chat_history(from_number, tenant_id, "user", msg_body)
                save_chat_history(from_number, tenant_id, "assistant", ai_reply)
                save_user_session(from_number, tenant_id, session)
                
                logger.info(f"📤 Sending reply to {from_number}: {ai_reply[:80]}...")
                if session.get("awaiting_confirmation") and not session.get("search_confirmed"):
                    send_whatsapp_buttons(tenant_id, from_number, ai_reply, ["Confirm", "Change"], wa_token)
                else:
                    send_whatsapp_text(tenant_id, from_number, ai_reply, wa_token)

              except Exception as fatal_err:
                logger.error(f"💀 FATAL ERROR processing msg from {msg.get('from', 'unknown')}: {fatal_err}", exc_info=True)
                try:
                    send_whatsapp_text(tenant_id, msg.get('from', ''), "Maazrat! System mein thori si dikkat aa gayi. Dobara message bhejein.", wa_token)
                except:
                    logger.error("💀 Even fallback reply failed!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
