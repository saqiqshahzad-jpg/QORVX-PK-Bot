import os
import sys
import json
import logging

# =========================================================================================
# 📝 SYSTEM-WIDE LOGGING CONFIGURATION
# =========================================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger("QORVX")
import random
import requests
import base64
import io
import re
import datetime
import time
import pytz
import pandas as pd  
import gspread       
from functools import lru_cache
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials
from fastapi import FastAPI, HTTPException, Request, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from groq import Groq

# ═══ SESSION STATE & DEDUP STORES ═══
USER_SESSIONS = {}      # key: "tenant_id:phone" -> {bhk, budget, location, purpose, market, language}
PROCESSED_MSG_IDS = {}   # message_id -> timestamp, auto-cleaned after 5 min

# --- MASTER SYSTEM PROMPT ---
# Defined below

# ---------------------------------------------

# 🚨 SECURE CONFIGURATION OVERLAY
load_dotenv()

app = FastAPI()

# =========================================================================================
# 👑 PRODUCTION ENVIRONMENT VARIABLES LOCK
# =========================================================================================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

MY_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "ALAAUDIN_SECRET_TOKEN")

client = Groq(api_key=GROQ_API_KEY, max_retries=0)
MODEL_ID = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"

def robust_chat_completion(messages_array, temperature, max_tokens):
    try:
        return client.chat.completions.create(
            model=MODEL_ID,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages_array
        )
    except Exception as e:
        logger.info(f"Primary LLM failed ({str(e)}), trying fallback...")
        try:
            return client.chat.completions.create(
                model=FALLBACK_MODEL,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=messages_array
            )
        except Exception as e2:
            logger.info(f"Fallback LLM failed: {str(e2)}")
            # Return a fake completion object so the bot never crashes
            class FakeChoice:
                def __init__(self):
                    class Msg:
                        content = "I appreciate your interest! Could you let me know which city or area you are looking in, and whether you want to Buy or Rent? This will help me find the perfect property for you! 🏛️"
                    self.message = Msg()
            class FakeCompletion:
                def __init__(self):
                    self.choices = [FakeChoice()]
            return FakeCompletion()

REPORT_PASSPHRASE = "ALAAUDIN.AI.LABS"

# =========================================================================================
# 🏛️ SUPABASE PERMANENT MEMORY ENGINE (MULTI-TENANT ISOLATED)
# =========================================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip('/')
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def get_tenant_config(tenant_id: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}
    url = f"{SUPABASE_URL}/rest/v1/vencode_tenants?tenant_id=eq.{tenant_id}&select=whatsapp_token,booking_sheet_name,property_sheet_name,agent_email,agent_name"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and len(data) > 0:
                return data[0]
    except Exception as e:
        logger.error(f"🚨 Supabase Tenant Lookup Crash: {str(e)}")
    return {}

def get_supabase_history(phone_number: str, tenant_id: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/whatsapp_history?phone_number=eq.{phone_number}&tenant_id=eq.{tenant_id}&order=created_at.desc&limit=16"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            history = res.json()
            history.reverse()
            return [{"role": row["role"], "content": row["content"]} for row in history]
    except Exception as e:
        logger.error(f"🚨 Supabase History Load Crash: {str(e)}")
    return []

def save_supabase_message(phone_number: str, role: str, content: str, tenant_id: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/whatsapp_history"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    payload = {"phone_number": phone_number, "role": role, "content": content, "tenant_id": tenant_id}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code not in [200, 201, 204]: 
            logger.error(f"🚨 Supabase Save Rejected: {res.status_code}")
    except Exception as e: 
        logger.error(f"🚨 Supabase Save Crash: {str(e)}")

def save_supabase_seller_listing(phone: str, name: str, email: str, location: str, prop_type: str, price: str, tenant_id: str):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/seller_listings"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    payload = {
        "phone_number": phone, "full_name": name, "email": email, 
        "location": location, "property_type": prop_type, 
        "asking_price": price, "tenant_id": tenant_id
    }
    try: 
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e: 
        logger.error(f"🚨 Structured Seller Save Failure: {str(e)}")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# =========================================================================================
# 🚀 MULTI-TENANT GOOGLE WORKSPACE CLIENT & FREE SHEET CRM
# =========================================================================================
class GoogleSpreadsheetClient:
    def __init__(self, tenant_id: str, booking_sheet_name: str, property_sheet_name: str):
        self.tenant_id = tenant_id
        self.booking_sheet_name = booking_sheet_name
        self.property_sheet_name = property_sheet_name
        self.scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS environment variable is not set")
        creds_dict = json.loads(creds_json)
        self.creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, self.scope)
        self.gc = gspread.authorize(self.creds)

    def get_booking_sheet(self):
        return self.gc.open(self.booking_sheet_name).worksheet("Sheet2")

    def get_property_sheet(self):
        return self.gc.open(self.property_sheet_name).sheet1

    def append_lead_record(self, phone: str, name: str, email: str, property_id: str, market_tag: str = "PK"):
        try:
            sh = self.gc.open(self.property_sheet_name)
            try:
                worksheet = sh.worksheet("Leads")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = sh.add_worksheet(title="Leads", rows="1000", cols="6")
                worksheet.append_row(["Timestamp", "Phone", "Name", "Email", "Property_ID", "Market"])
            
            pk_time = pytz.timezone('Asia/Karachi')
            timestamp = datetime.datetime.now(pk_time).strftime("%Y-%m-%d %H:%M:%S")
            worksheet.append_row([timestamp, phone, name, email, property_id, market_tag])
        except Exception as e:
            logger.error(f"🚨 Google Sheet Lead Append Crash: {str(e)}")

# =========================================================================================
# 🚀 CORE ENGINE UTILITIES & DYNAMIC APPEND BOOKING ENGINE
# =========================================================================================
def handle_calendar_booking(date_req: str, time_req: str, phone: str, tenant_id: str, booking_sheet_name: str, property_sheet_name: str):
    try:
        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
        
        try:
            sheet = workspace.gc.open(booking_sheet_name).worksheet("Sheet2")
        except gspread.exceptions.WorksheetNotFound:
            sheet = workspace.gc.open(booking_sheet_name).add_worksheet(title="Sheet2", rows="1000", cols="6")
            sheet.append_row(["Date", "Time", "Client_Name", "Phone", "Email", "Status"])

        valid_slots = ["12:00 pm", "1:00 pm", "2:00 pm", "3:00 pm", "4:00 pm", "5:00 pm"]
        
        t_str = time_req.lower().strip()
        match = re.search(r'(\d+)', t_str)
        matched_slot = None
        if match and "p" in t_str:
            hour = str(int(match.group(1)))
            if hour in ["12", "1", "2", "3", "4", "5"]:
                matched_slot = f"{hour}:00 pm"
        
        if not matched_slot:
            return {"status": "invalid_time"}
            
        d_clean = date_req.strip().lower()
        
        records = sheet.get_all_records()
        df = pd.DataFrame(records)
        
        booked_times = []
        if not df.empty and 'Date' in df.columns and 'Time' in df.columns:
            df['Date'] = df['Date'].astype(str).str.strip().str.lower()
            df['Time'] = df['Time'].astype(str).str.strip().str.lower()
            day_bookings = df[df['Date'] == d_clean]
            booked_times = day_bookings['Time'].tolist()
            
        if matched_slot in booked_times:
            available = [s for s in valid_slots if s not in booked_times]
            if not available:
                return {"status": "full"}
            
            target_idx = valid_slots.index(matched_slot)
            available.sort(key=lambda x: abs(valid_slots.index(x) - target_idx))
            return {"status": "taken", "alternative": available[0].upper()}
        else:
            sheet.append_row([date_req, matched_slot.upper(), "Pending Client", phone, "Pending Email", "Booked 🚫"])
            return {"status": "success", "slot": matched_slot.upper()}
            
    except Exception as e:
        logger.error(f"🚨 Booking Engine Crash: {str(e)}")
        return {"status": "error"}

def query_property_database(listing_type: str, property_type: str, city_society: str, budget: int, tenant_id: str, booking_sheet_name: str, property_sheet_name: str):
    try:
        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
        sheet = workspace.get_property_sheet()
        import pandas as pd
        df = pd.DataFrame(sheet.get_all_records())
        
        if 'Listing_Type' in df.columns:
            df['Listing_Type'] = df['Listing_Type'].astype(str).str.strip().str.lower()
        if 'Property_Type' in df.columns:
            df['Property_Type'] = df['Property_Type'].astype(str).str.strip().str.lower()
        if 'City' in df.columns:
            df['City'] = df['City'].astype(str).str.strip().str.lower()
        if 'Society_Area' in df.columns:
            df['Society_Area'] = df['Society_Area'].astype(str).str.strip().str.lower()
        
        if 'Demand_PKR' in df.columns:
            df['Demand_PKR'] = pd.to_numeric(df['Demand_PKR'], errors='coerce').fillna(0).astype(int)
        else:
            return []
            
        if listing_type:
            lt_lower = listing_type.strip().lower()
            if lt_lower == "buy": lt_lower = "sale"
            df = df[df['Listing_Type'].str.contains(lt_lower, na=False)]
            
        if property_type:
            pt_lower = property_type.strip().lower()
            df = df[df['Property_Type'].str.contains(pt_lower, na=False)]
            
        if city_society:
            cs_lower = city_society.strip().lower()
            loc_filter = pd.Series(False, index=df.index)
            if 'City' in df.columns:
                loc_filter = loc_filter | df['City'].str.contains(cs_lower, na=False)
            if 'Society_Area' in df.columns:
                loc_filter = loc_filter | df['Society_Area'].str.contains(cs_lower, na=False)
            df = df[loc_filter]
            
        if budget > 0:
            min_budget = int(budget * 0.75)
            max_budget = int(budget * 1.25)
            df = df[(df['Demand_PKR'] >= min_budget) & (df['Demand_PKR'] <= max_budget)]
            
        if not df.empty: 
            return df.to_dict(orient="records")
        return []
    except Exception as e: 
        logger.error(f"🚨 Property DB Query Crash: {str(e)}")
        return []

# =========================================================================================
# 📲 DYNAMIC TENANT WHATSAPP ROUTING
# =========================================================================================
def send_whatsapp_text(tenant_id: str, to_number: str, text_body: str, whatsapp_token: str):
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_number, "type": "text", "text": {"preview_url": False, "body": text_body}}
    try: 
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"📤 [SEND TEXT] To: {to_number} | Status: {res.status_code} | Body: {res.text[:200]}")
    except Exception as e: 
        logger.error(f"🚨 [SEND TEXT CRASH] To: {to_number} | Error: {str(e)}")

def send_whatsapp_media(tenant_id: str, to_number: str, media_url: str, media_type: str, whatsapp_token: str):
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_number, "type": media_type, media_type: {"link": media_url}}
    try: 
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"🎬 [META MEDIA RESPONSE] Status: {res.status_code} | Body: {res.text}")
    except Exception as e: 
        logger.info(f"🚨 [META MEDIA CRASH] {str(e)}")

def send_whatsapp_buttons(tenant_id: str, to_number: str, body_text: str, buttons_list: list, whatsapp_token: str):
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    formatted_buttons = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": btn}} for i, btn in enumerate(buttons_list[:3])]
    payload = {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": to_number, "type": "interactive",
        "interactive": {"type": "button", "body": {"text": body_text}, "action": {"buttons": formatted_buttons}}
    }
    try: 
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"📤 [SEND BUTTONS] To: {to_number} | Status: {res.status_code} | Body: {res.text[:200]}")
    except Exception as e: 
        logger.error(f"🚨 [SEND BUTTONS CRASH] To: {to_number} | Error: {str(e)}")

def send_property_button(tenant_id: str, to_number: str, body_text: str, prop_id: str, whatsapp_token: str):
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"view_{prop_id}",  # 🔥 HIDDEN ID: User ko nahi dikhegi, code track karega!
                            "title": "Request Viewing 🔑"  # 🔥 PURE LUXURY TEXT: No numbers, completely clean!
                        }
                    }
                ]
            }
        }
    }
    try: 
        requests.post(url, headers=headers, json=payload, timeout=10)
    except: 
        pass

def send_luxury_email(to_email: str, client_name: str, property_id: str, agent_email: str, agent_name: str):
    if not RESEND_API_KEY:
        logger.error("🚨 Resend API Key Missing.")
        return
        
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # HTML Content completely rebranded to QORVX 🏛️✨
    html_content = f"""
    <div style="background-color: #121212; color: #FFFFFF; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; padding: 40px; text-align: center;">
        <div style="max-width: 600px; margin: 0 auto; border-top: 3px solid #D4AF37; padding-top: 30px;">
            <h1 style="color: #D4AF37; font-weight: 300; letter-spacing: 2px; margin-bottom: 5px;">Hey 👋 {client_name}!</h1>
            <h3 style="color: #888888; font-weight: 300; margin-top: 0; margin-bottom: 40px; letter-spacing: 4px; font-size: 12px;">QORVX GLOBAL AI LABS</h3>
            
            <p style="font-size: 16px; line-height: 1.8; text-align: left; color: #E0E0E0;">Dear {client_name},</p>
            
            <p style="font-size: 16px; line-height: 1.8; text-align: left; color: #E0E0E0;">
                Your private acquisition file for Asset Portfolio <strong style="color: #D4AF37;">{property_id}</strong> has been securely synchronized with our elite concierge grid.
            </p>
            
            <p style="font-size: 16px; line-height: 1.8; text-align: left; color: #E0E0E0;">
                A senior manager will connect with you via secure channels shortly to arrange your exclusive priority viewing.
            </p>
            
            <div style="margin-top: 50px; padding-top: 20px; border-top: 1px solid #333333;">
                <p style="font-size: 10px; color: #666666; letter-spacing: 1px;">CONFIDENTIAL & SECURE DISPATCH</p>
            </div>
        </div>
    </div>
    """
    
    # ─── 🏛️ QORVX DYNAMIC MULTI-TENANT ROUTING DISPATCH ───────────────────
    payload = {
        "from": f"{agent_name} | Luxury Concierge <concierge@qorvx.online>", 
        "to": [to_email], 
        "reply_to": agent_email, # Dynamic destination route
        "subject": f"Exclusive Off-Market Portfolio Locked: {property_id}",
        "html": html_content 
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=5)
        if res.status_code not in [200, 201]:
            logger.info(f"🚨 [RESEND EMAIL CRASH] Status: {res.status_code} | Body: {res.text}")
    except Exception as e:
        logger.info(f"🚨 [RESEND NETWORK CRASH] {str(e)}")

# =========================================================================================
# 🌍 MARKET ROUTING ENGINE (PAKISTAN/KARACHI)
# =========================================================================================

# ── PK Location & Language Signal Keyword Banks ────────────────────────────────

def detect_market(msg_body: str, chat_history: list) -> dict:
    """
    Stateless market & language detection engine. (Hardcoded to PK for this build)
    """
    return {"market": "PK", "language": "roman_urdu"}

# =========================================================================================
# 🏠🏡 PURPOSE DETECTION ENGINE (BUY vs RENT)
# =========================================================================================
RENT_MARKERS = [
    "rent", "kiraye", "kiraya", "kiray", "rental", "lease", "monthly rent",
    "rent par", "kiraye par", "kiray par", "rent pe", "kiraye pe",
    "rent ka", "rent ki", "for rent", "on rent", "renting",
]
BUY_MARKERS = [
    "buy", "purchase", "kharidna", "khareed", "khareedna", "sale",
    "invest", "acquisition", "buying", "kharid", "for sale",
]

def detect_purpose(msg_body: str, chat_history: list) -> str:
    """
    Detects Buy vs Rent intent from current message and conversation history.
    Priority: Current message > History markers > Button context > Default 'unknown'.
    Returns: 'rent' | 'buy' | 'unknown'
    """
    msg_lower = f" {msg_body.lower()} "
    
    # ── PASS 1: Check current message for explicit purpose signals ──
    rent_score = sum(1 for kw in RENT_MARKERS if f" {kw} " in msg_lower or kw in msg_lower)
    buy_score = sum(1 for kw in BUY_MARKERS if f" {kw} " in msg_lower or kw in msg_lower)
    
    if rent_score > buy_score:
        return "rent"
    if buy_score > rent_score:
        return "buy"
    
    # ── PASS 2: Check conversation history for purpose context ──
    if chat_history:
        for row in reversed(chat_history):
            content = row["content"].lower()
            # Check for button-based purpose markers saved in history
            if "[PURPOSE:RENT]" in row["content"]:
                return "rent"
            if "[PURPOSE:BUY]" in row["content"]:
                return "buy"
            # Check for natural language purpose signals in recent history
            for kw in RENT_MARKERS:
                if kw in content:
                    return "rent"
            for kw in BUY_MARKERS:
                if kw in content:
                    return "buy"
    
    return "unknown"

def normalize_budget(budget_str: str) -> int:
    s = str(budget_str).lower().replace(",", "").replace("pkr", "").strip()
    try:
        num_match = re.search(r'([\d]+\.?\d*)', s)
        if not num_match: return 0
        num = float(num_match.group(1))
        
        if any(kw in s for kw in ["crore", "cr", "karor", "karoar"]):
            return int(num * 10000000)
        elif any(kw in s for kw in ["lac", "lacs", "lakh", "lakhs"]):
            return int(num * 100000)
        else:
            return int(num)
    except Exception:
        return 0

def format_currency(amount: int) -> str:
    if amount >= 10000000: return f"{amount / 10000000:g} Crore"
    if amount >= 100000: return f"{amount / 100000:g} Lacs"
    return f"{amount:,}"

# ═══════════════════════════════════════════════════════════════
# 🧠 SESSION STATE ENGINE (ANTI-AMNESIA CORE)
# ═══════════════════════════════════════════════════════════════

def get_session(phone: str, tenant_id: str) -> dict:
    """Get or create a session for this user."""
    key = f"{tenant_id}:{phone}"
    if key not in USER_SESSIONS:
        USER_SESSIONS[key] = {
            "listing_type": None, "property_type": None, "city_society": None, "budget": None
        }
    return USER_SESSIONS[key]

def extract_and_update_session(msg_body: str, session: dict, chat_history: list):
    """Extract BHK, Location, Purpose, Budget from current message and update session."""
    msg_lower = msg_body.lower().strip()

    # ── Detect market & language ──
    detected = detect_market(msg_body, chat_history)
    session["market"] = detected["market"]
    session["language"] = detected["language"]

    # ── PURPOSE detection ──
    purpose = detect_purpose(msg_body, chat_history)
    if purpose != "unknown":
        session["purpose"] = purpose

    # ── Find last AI message for context ──
    last_ai_content = ""
    if chat_history:
        for row in reversed(chat_history):
            if row["role"] == "assistant":
                last_ai_content = row["content"].lower()
                break

    # ── BHK detection ──
    bhk_match = re.search(r'(\d+)\s*(?:bhk|bed|bedroom|kamr[ae]|kamare)', msg_lower)
    if bhk_match:
        session["bhk"] = int(bhk_match.group(1))
    elif msg_lower.strip().isdigit():
        val = int(msg_lower.strip())
        if 1 <= val <= 10 and not session.get("bhk"):
            # Context: last AI asked for BHK, or session still missing BHK
            if any(kw in last_ai_content for kw in ["bhk", "bed", "kamr", "bedroom", "kamare"]):
                session["bhk"] = val
            elif not session.get("bhk"):
                session["bhk"] = val

    # ── LOCATION detection ──
    if not session.get("location"):
        for kw in PK_LOCATION_KEYWORDS:
            if kw in msg_lower:
                session["location"] = msg_body.strip().title()
                break
        if not session.get("location"):
            for kw in US_LOCATION_KEYWORDS:
                if kw in msg_lower:
                    session["location"] = msg_body.strip().title()
                    break
        # Context: AI asked for location and user gave short answer
        if not session.get("location") and len(msg_body.split()) <= 3:
            if any(kw in last_ai_content for kw in ["location", "city", "area", "jagah", "shehar", "ilaq", "kidhar", "preferred location"]):
                if not msg_lower.strip().isdigit():
                    session["location"] = msg_body.strip().title()

    # ── BUDGET detection ──
    if not session.get("budget"):
        budget_val = normalize_budget(msg_body, session.get("market", ""))
        if budget_val > 10:
            session["budget"] = budget_val
        elif budget_val > 0 and session.get("bhk"):
            if any(kw in last_ai_content for kw in ["budget", "price", "kitna", "paisay", "cost", "amount"]):
                session["budget"] = budget_val

    return session

def build_state_aware_prompt(session: dict, base_prompt: str) -> str:
    """Inject current session state into the LLM system prompt."""
    collected = []
    missing = []

    if session.get("listing_type"): collected.append(f"Listing Type: {session['listing_type']}")
    else: missing.append("Listing_Type (Buy or Rent)")

    if session.get("property_type"): collected.append(f"Property Type: {session['property_type']}")
    else: missing.append("Property_Type (Plot, House, Apartment, File)")

    if session.get("city_society"): collected.append(f"City/Society: {session['city_society']}")
    else: missing.append("City/Society")

    if session.get("budget"): collected.append(f"Budget: {format_currency(session['budget'])}")
    else: missing.append("Budget (in PKR Lakh/Crore)")

    injection = "\n\n=== CURRENT SESSION STATE (CRITICAL — TRUST THIS OVER YOUR MEMORY) ==="

    if collected:
        injection += "\nALREADY COLLECTED (DO NOT RE-ASK — ZERO TOLERANCE):\n"
        injection += "\n".join(f"  DONE: {c}" for c in collected)

    if missing:
        injection += f"\n\nSTILL MISSING:\n"
        injection += "\n".join(f"  NEED: {m}" for m in missing)
        injection += f"\n\nYOUR ONLY ACTION: Ask for {missing[0]}. ONE question. ONE sentence. ONE emoji."
    else:
        injection += "\n\nALL 4 PARAMETERS COLLECTED. Output [PROPERTY_SEARCH: ...] JSON IMMEDIATELY. Do NOT ask any more questions.\n"
        injection += 'JSON FORMAT: [PROPERTY_SEARCH: {"listing_type": "Sale|Rent", "property_type": "Plot|House|File", "city_society": "string", "budget": <int>}]'

    return base_prompt + injection

def session_has_all_params(session: dict) -> bool:
    """Check if all required parameters have been collected."""
    return all([session.get("listing_type"), session.get("property_type"), session.get("city_society"), session.get("budget")])

# ── 🇵🇰 PAKISTAN MARKET SYSTEM PROMPT ──
MASTER_SYSTEM_PROMPT = """Identity: You are QORVX Concierge, an elite Real Estate AI Assistant operating exclusively for Pakistani clients. You are a highly respectful, sharp, and helpful Pakistani Real Estate Consultant (Master Closer).

1. Bot Persona & Tone Guidelines
- Tone: Nihayat ba-adab, professional, aur madadgaar (Hamesha 'Aap', 'Sir/Ma'am', 'Bhai' use kare).
- Language Stickiness: Natural Roman Urdu (jismein basic English real estate terms hon: jaise BHK, budget, location, viewing). Agar user ek baar Roman Urdu ya Pakistani city bole, toh bot 100% Roman Urdu par lock ho jaye.
- Message Length: Max 2-3 lines per response. WhatsApp par lambe paragraphs koi nahi padhta. Emojis use karein (📍, 🏡, 💰).
- The "Zero-Silence" Rule: Bot hamesha apni baat ek gentle sawal par khatam karega. Conversation kabhi dead-end par nahi rukhni chahiye.

2. The Core State Machine (4-Step Qualification)
Bot ka main maqsad Google Sheet filter karne se pehle yeh 4 variables collect karna hai:
- Listing_Type: (Buy / Rent)
- City / Location: (e.g., Lahore, Karachi, DHA)
- BHK: (Number of bedrooms)
- Budget: (In Lakh/Crore)
Strict Rule: Jab tak yeh 4 variables poore na hon, Google Sheet mein search query trigger nahi karni. Bot sirf missing variable ka sawal poochega.
- When ALL 4 collected, output ONLY: [PROPERTY_SEARCH: {"bhk":<int>,"budget":<int>,"location":"<str>","purpose":"buy"|"rent"}]

3. Possible User Scenarios & Bot Flow
- Scenario A: The Generic Greeting ("Salam", "Hi")
  Bot Logic: Salam ka jawab de, robotic menu na phenke, aur direct Step 1 (Buy/Rent) pooche.
  Response: "Walaikum Assalam Sir! QORVX Concierge mein khush aamdeed. 🌟 Umeed hai aap theek honge. Main aapka personal real estate advisor hoon. Batayen, aaj aap premium property kharidna chahte hain, ya rental options explore karne ka mood hai? 🏡"

- Scenario B: The Vague Inquiry ("Ad dekha tha", "Details dein")
  Bot Logic: Welcome kare aur missing variables collect karna shuru kare.
  Response: "Ji bilkul Sir! Hamare paas premium luxury options available hain. Taake main aapko best exact portfolio bhej sakun, aap kis city ya area mein property dekh rahe hain? 📍"

- Scenario C: Direct Project / Price Query ("Lahore mein 4 BHK chahiye")
  Bot Logic: Jo variables mil gaye (City: Lahore, BHK: 4) unhe state mein lock kare, aur sirf baqi missing variables pooche.
  Response: "Zabardast choice Sir. Lahore mein 4 BHK ke hamare paas bohot exclusive options hain. Ek aakhri cheez—aapka approximate budget (Lakh ya Crore mein) kya hai taake main exactly wahi options nikalun? 💰"

- Scenario D: The Bargainer / Chaska Party ("Bohot mehenga hai", "Discount do")
  Bot Logic: Behas bilkul nahi karni, aur na hi sorry bolna hai. Value justify karke aage barhna hai.
  Response: "Sir, real estate mein asking price aur closing price mein hamesha thora margin hota hai. Ek baar aap property visit kar lein, QORVX management aapke liye Insha'Allah best possible final price negotiate karegi. Kya hum iski private viewing schedule kar lein? 🤝"

- Scenario E: Gibberish or Irrelevant Messages
  Bot Logic: Agar user out-of-context baat kare toh politely conversation ko missing variable ki taraf wapis laye.
  Response: "Sir, lagta hai main aapka sawal theek se samajh nahi paya. 😅 Hum aapki property search ki baat kar rahe thay—kya aap mujhe apna approximate budget confirm kar sakte hain taake hum aage barhein?"

4. Final Lead Capture (After Media Dispatch)
Jaise hi 4 variables mil jayein, backend Google Sheet ko query karke matched property ki details, Main_Image se Image_6, aur Walkthrough_Video (agar ho) dispatch karega.
Bot Final Action: Media bhejne ke fauran baad bot private viewing ke liye user ka Full Name aur Email maangega.
Response: "Behtareen intikhab! Is property ki private viewing schedule karne ke liye barah-e-karam apna Poora Naam aur Email share karein. ✨"

5. Strict Guardrails
- No Hallucinations: Koi jhooti price, installment plan, ya plot number khud se invent nahi karna. Sirf wahi data dikhana hai jo Google Sheet se match hokar aaye.
- No Jargon: CRM, Lead Handoff, Optimization jaise robotic words use nahi karne.
"""

@app.get("/")
def home(): 
    return {"status": "Multi-Tenant AI Engine Is Online! ✅"}

@app.get('/webhook')
def verify_whatsapp_webhook(request: Request):
    params = request.query_params
    print(f"🔥 [GET /webhook] Verification request received: {params}", flush=True)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == MY_VERIFY_TOKEN:
        print("✅ [GET /webhook] Token matched! Verified.", flush=True)
        return PlainTextResponse(content=str(params.get("hub.challenge")), status_code=200)
    print("❌ [GET /webhook] Token mismatch or invalid mode.", flush=True)
    return PlainTextResponse(content="Error", status_code=403)

@app.post('/webhook')
async def receive_whatsapp_message(request: Request, background_tasks: BackgroundTasks):
    try:
        raw_body = await request.body()
        print(f"🔥 [POST /webhook] Raw payload received: {raw_body.decode('utf-8')}", flush=True)
        data = json.loads(raw_body)
        print(f"🔥 [POST /webhook] Parsed JSON: {json.dumps(data)}", flush=True)
        background_tasks.add_task(process_whatsapp_data, data)
        return PlainTextResponse(content="OK", status_code=200)
    except Exception as e:
        print(f"🚨 [POST /webhook] Webhook Accept Error: {str(e)}", flush=True)
        logger.exception(f"Webhook Accept Error: {str(e)}")
        # Returning 200 so Meta doesn't block the webhook
        return PlainTextResponse(content="OK", status_code=200)

async def process_whatsapp_data(data: dict):
    print(f"🚀 [BACKGROUND TASK] process_whatsapp_data started", flush=True)
    try:
        if data.get("object") and data.get("entry"):
            for entry in data["entry"]:
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    tenant_id = value.get("metadata", {}).get("phone_number_id")
                    if not tenant_id:
                        continue

                    tenant_config = get_tenant_config(tenant_id)
                    if not tenant_config or not tenant_config.get("whatsapp_token"):
                        logger.warning(f"🚨 Registration Alert: Tenant {tenant_id} configuration not found.")
                        continue
                    
                    whatsapp_token = tenant_config.get("whatsapp_token")
                    booking_sheet_name = tenant_config.get("booking_sheet_name")
                    property_sheet_name = tenant_config.get("property_sheet_name")
                    
                    # 🔥 DYNAMIC AGENT DATA EXTRACTED FROM SUPABASE
                    dynamic_agent_email = tenant_config.get("agent_email", "concierge@qorvx.online")
                    dynamic_agent_name = tenant_config.get("agent_name", "Luxury Concierge")

                    if "messages" in value:
                        for message in value["messages"]:
                            from_number = message["from"]
                            print(f"📩 [WEBHOOK] Message from: {from_number} | Type: {message.get('type')} | Tenant: {tenant_id}", flush=True)
                            logger.info(f"📩 [WEBHOOK] Message from: {from_number} | Type: {message.get('type')} | Tenant: {tenant_id}")

                            # ═══ DEDUP: Skip if Meta retried this message ═══
                            msg_id = message.get("id", "")
                            if msg_id:
                                now = time.time()
                                if msg_id in PROCESSED_MSG_IDS:
                                    logger.info(f"⚡ [DEDUP] Skipping duplicate: {msg_id}")
                                    continue
                                PROCESSED_MSG_IDS[msg_id] = now
                                stale_keys = [k for k, v in PROCESSED_MSG_IDS.items() if now - v > 300]
                                for k in stale_keys:
                                    del PROCESSED_MSG_IDS[k]

                            ai_response = None
                            msg_body = ""

                            # ─── NAYA POCKET CAPTURE CODE ─────────────────────────────────
                            btn_id = ""  # Pehle pocket ko khali rakha

                            if message.get("type") == "text":
                                msg_body = message["text"]["body"].strip()
                            elif message.get("type") == "interactive":
                                if message["interactive"].get("type") == "button_reply":
                                    # Yeh bahaar dikhne waala saaf text hai
                                    msg_body = message["interactive"]["button_reply"]["title"].strip()
                                    # 🔥 YEH HAI JADU! Chupa hua token humne is variable mein save kar liya
                                    btn_id = message["interactive"]["button_reply"]["id"].strip()
                            else:
                                fallback_msg = "Sir, lagta hai network error ki wajah se main aapka message theek se samajh nahi paya. 😅 Hum aapki property search ki baat kar rahe thay—kya aap mujhe apna approximate budget confirm kar sakte hain taake hum aage barhein?"
                                save_supabase_message(from_number, "user", f"[Media/Unsupported: {message.get('type')}]", tenant_id)
                                save_supabase_message(from_number, "assistant", fallback_msg, tenant_id)
                                send_whatsapp_text(tenant_id, from_number, fallback_msg, whatsapp_token)
                                return PlainTextResponse(content="OK", status_code=200)

                            if msg_body:
                                if msg_body.lower() in ["hi", "hello", "hey", "salam", "aao", "aoa", "assalamualaikum", "slm", "salaam"]:
                                    # Reset session for fresh conversation
                                    _greet_session = get_session(from_number, tenant_id)
                                    _greet_session.update({"bhk": None, "budget": None, "location": None, "purpose": None, "market": None, "language": None})
                                    # Let it flow to the LLM for a natural text response instead of rigid buttons.

                                db_history = get_supabase_history(from_number, tenant_id)
                                last_ai_msg = db_history[-1]["content"] if db_history else ""

                                # ═══ SESSION STATE: Extract & persist parameters ═══
                                session = get_session(from_number, tenant_id)
                                extract_and_update_session(msg_body, session, db_history)
                                logger.info(f"🧠 [SESSION] {from_number}: bhk={session.get('bhk')} loc={session.get('location')} purpose={session.get('purpose')} budget={session.get('budget')} market={session.get('market')}")

                                # =========================================================================================
                                # 🏠 STATE-MACHINE INTERCEPTORS: BUYER INTAKE FUNNEL
                                # =========================================================================================
                                if msg_body == "Buy Property 🏠":
                                    session["purpose"] = "buy"
                                    ai_response = "Excellent choice. Let's find your next off-market acquisition portfolio. What is your preferred location or city? 📍"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", "[PURPOSE:BUY] " + ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # =========================================================================================
                                # 🏡 STATE-MACHINE INTERCEPTORS: RENT INTAKE FUNNEL
                                # =========================================================================================
                                elif msg_body == "Rent Property 🏡":
                                    session["purpose"] = "rent"
                                    ai_response = "Let's find you the perfect rental. What is your preferred location or city? 📍"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", "[PURPOSE:RENT] " + ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # =========================================================================================
                                # ➕ MORE OPTIONS: Sell + Book Strategy (Overflow from 3-button limit)
                                # =========================================================================================
                                elif msg_body == "More Options ➕":
                                    send_whatsapp_buttons(
                                        tenant_id, from_number, "Here are more ways we can serve you 🏛️",
                                        ["Sell Property 💰", "Book Strategy 📅"],
                                        whatsapp_token
                                    )
                                    return PlainTextResponse(content="OK", status_code=200)

                                # =========================================================================================
                                # 📅 STATE-MACHINE INTERCEPTORS: DYNAMIC CALENDAR BOOKING CORE
                                # =========================================================================================
                                elif msg_body == "Book Strategy 📅":
                                    ai_response = "Excellent choice. Let's align your investment roadmap. Please state your preferred date (e.g., tomorrow, or YYYY-MM-DD): 📅"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "preferred date (e.g., tomorrow, or YYYY-MM-DD)" in last_ai_msg:
                                    preferred_date = msg_body
                                    ai_response = f"Got it, looking at scheduling parameters for {preferred_date}. Now, please state your preferred Time Slot (e.g., 4:00 PM, 5:00 PM, 6:00 PM): ⏰"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "preferred Time Slot" in last_ai_msg or "outside scheduled active hours" in last_ai_msg or "currently blocked" in last_ai_msg or "completely allocated" in last_ai_msg or "valid hourly slot" in last_ai_msg:
                                    preferred_time = msg_body
                                    extracted_date = "tomorrow"
                                    
                                    for idx, row in enumerate(db_history):
                                        if "preferred date (e.g., tomorrow, or YYYY-MM-DD)" in row["content"]:
                                            if idx + 1 < len(db_history): 
                                                extracted_date = db_history[idx+1]["content"]
                                            
                                    send_whatsapp_text(tenant_id, from_number, "Checking live database registries... ⏳", whatsapp_token)
                                    booking_result = handle_calendar_booking(extracted_date, preferred_time, from_number, tenant_id, booking_sheet_name, property_sheet_name)
                                    
                                    if booking_result["status"] == "success":
                                        confirmed_slot = booking_result.get("slot", preferred_time.upper())
                                        ai_response = f"Priority Confirmed! 🔒 I have securely locked your calendar slot on {extracted_date} at {confirmed_slot}. An executive brief has been dispatched."
                                    elif booking_result["status"] == "invalid_time":
                                        ai_response = "Our executive agents operate exclusively Monday through Saturday, from 12:00 PM to 6:00 PM. 🕰️ Please reply with a valid hourly slot (e.g., 2:00 PM)."
                                    elif booking_result["status"] == "taken":
                                        alt_t = booking_result["alternative"]
                                        ai_response = f"Ah, the requested time is currently blocked by another investor. 🛑 However, I can lock an elite slot for you on {extracted_date} at {alt_t}. Shall we reserve that instead?"
                                    elif booking_result["status"] == "full":
                                        ai_response = f"Our priority calendar for {extracted_date.upper()} is completely allocated. 🏛️ Please reply with an alternative date to query the grid."
                                    else:
                                        ai_response = "An error occurred while verifying the registry. Please state your preferred time slot again."
                                        
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # =========================================================================================
                                # 💰 STATE-MACHINE: SELLER DEDICATED STRUCTURED CAPTURE ENGINE
                                # =========================================================================================
                                elif msg_body == "Sell Property 💰":
                                    ai_response = "Excellent. To securely list your asset in our off-market network, please reply with your Full Name (Seller Profile). 👤"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "Full Name (Seller Profile)" in last_ai_msg:
                                    ai_response = "Thank you. Now, please state your Email Address to verify your Seller Profile logs. 📩"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "verify your Seller Profile logs" in last_ai_msg:
                                    ai_response = "Authenticating... What is the exact city and geographic location of your property? 📍"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "city and geographic location of your property" in last_ai_msg:
                                    ai_response = "Got it. What is the asset configuration type and structural parameters (e.g., 3 BHK Villa, 4 BHK Penthouse)? 🏢"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "configuration type and structural parameters" in last_ai_msg:
                                    ai_response = "Perfect. What is your expected final asking price for this luxury asset? 💵"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "expected final asking price for this luxury asset" in last_ai_msg:
                                    asking_price = msg_body
                                    client_name, client_email, extracted_location, extracted_type = "Unknown Seller", "Unknown Email", "Unknown Location", "Unknown Type"
                                    
                                    for idx, row in enumerate(db_history):
                                        if "Full Name (Seller Profile)" in row["content"] and idx + 1 < len(db_history): 
                                            client_name = db_history[idx+1]["content"]
                                        if "verify your Seller Profile logs" in row["content"] and idx + 1 < len(db_history): 
                                            client_email = db_history[idx+1]["content"]
                                        if "city and geographic location" in row["content"] and idx + 1 < len(db_history): 
                                            extracted_location = db_history[idx+1]["content"]
                                        if "configuration type and structural" in row["content"] and idx + 1 < len(db_history): 
                                            extracted_type = db_history[idx+1]["content"]
                                            
                                    save_supabase_seller_listing(from_number, client_name, client_email, extracted_location, extracted_type, asking_price, tenant_id)
                                    
                                    ai_response = "Listing Locked! 🔒 Your premium asset parameters, contact profile, and credentials have been securely synchronized with our executive desk."
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # =========================================================================================
                                # 🔑 STATE-MACHINE INTERCEPTORS: DYNAMIC BUYER VIEWING & FREE SHEET LOGGING
                                # =========================================================================================
                                elif btn_id.startswith("view_"):
                                    target_prop_id = btn_id.replace("view_", "").strip()
                                    ai_response = f"Behtareen intikhab! Is property [{target_prop_id}] ki private viewing schedule karne ke liye barah-e-karam apna Poora Naam batayein. 👤"
                                    save_supabase_message(from_number, "user", f"Clicked Request Viewing for {target_prop_id}", tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif ("please reply with your Full Name" in last_ai_msg and "schedule your private viewing" in last_ai_msg) or ("apna Poora Naam batayein" in last_ai_msg and "private viewing schedule" in last_ai_msg):
                                    is_question = "?" in msg_body or any(w in msg_body.lower().split() for w in ["kya", "kab", "kese", "can", "how", "why"]) or len(msg_body.split()) > 3
                                    if is_question:
                                        sys_prompt = "You are a luxury real estate concierge. The user was asked for their Full Name to schedule a viewing, but they asked a question instead. Answer their question politely in Roman Urdu in 1 short sentence, and then say: 'Barah-e-karam apna Poora Naam share karein taake hum slot reserve kar sakein. 👤'"
                                        
                                        messages_array = [{"role": "system", "content": sys_prompt}]
                                        for past_msg in db_history[-3:]: messages_array.append(past_msg)
                                        messages_array.append({"role": "user", "content": msg_body})
                                        
                                        completion = robust_chat_completion(messages_array, 0.7, 150)
                                        ai_response = completion.choices[0].message.content
                                    else:
                                        client_name = msg_body
                                        ai_response = f"Shukriya, {client_name}. Ab barah-e-karam apna Email Address batayein taake verification mukammal ho sake. 📩"
                                            
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "taake hum slot reserve kar sakein" in last_ai_msg or "we can reserve your slot" in last_ai_msg:
                                    client_name = msg_body
                                    ai_response = f"Shukriya, {client_name}. Ab barah-e-karam apna Email Address batayein taake verification mukammal ho sake. 📩"
                                        
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif "please state your Email Address to finalize" in last_ai_msg or "apna Email Address batayein taake verification mukammal" in last_ai_msg:
                                    client_email = msg_body
                                    extracted_name = "Valued Client"
                                    target_prop_id = "Unknown"
                                    
                                    for idx, row in enumerate(db_history):
                                        if ("please reply with your Full Name" in row["content"] or "apna Poora Naam batayein" in row["content"] or "taake hum slot reserve kar sakein" in row["content"] or "we can reserve your slot" in row["content"]) and idx + 1 < len(db_history): 
                                            extracted_name = db_history[idx+1]["content"]
                                            try:
                                                target_prop_id = row["content"].split("[")[1].split("]")[0]
                                            except: pass
                                            
                                    # 🔥 Broadcast Lead to Client's FREE Google Sheet (DUAL-MARKET TAGGED)
                                    try:
                                        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
                                        workspace.append_lead_record(from_number, extracted_name, client_email, target_prop_id, "PK")
                                    except Exception as e:
                                        logger.error(f"🚨 Google Sheet Lead Sync Failed: {e}")

                                    # 🔥 FIXED: Dispatch Ultra-Premium HTML Email via Resend with ALL 5 Dynamic Arguments
                                    send_luxury_email(
                                        to_email=client_email, 
                                        client_name=extracted_name, 
                                        property_id=target_prop_id, 
                                        agent_email=dynamic_agent_email, 
                                        agent_name=dynamic_agent_name
                                    )
                                    
                                    ai_response = "Credentials Encrypted! 🔐 Aapki request humari executive sheet mein sync ho gayi hai. Humari team jald aapse raabta karegi."
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif msg_body == "Not Interested ❌":
                                    ai_response = "Understood. We continuously update our off-market assets. State any new parameters whenever you are ready. 🏛️"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif msg_body == "Lower Budget 💰":
                                    ai_response = "No problem at all. Please state your revised maximum budget, BHK count, and target area to re-query the network. 🔍"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # 🔥 MAKHAN LOGIC: Area Change Interceptor (DUAL-MARKET AWARE)
                                elif msg_body == "Change Area 📍":
                                    # Reset location in session so new area can be captured
                                    session["location"] = None
                                    session["budget"] = None
                                    # 🌍 Detect market from conversation history for area suggestions
                                    ai_response = "Bilkul bhai! DHA Phase 6, Clifton Block 5, Bahria Town Karachi, ya Emaar Crescent Bay mein shandar options hain — konsa area try karein? 🏛️"
                                    
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # =========================================================================================
                                # 🤖 BASE CONVERSATIONAL FALLBACK LAYER (LLaMA & RAG Search)
                                # =========================================================================================
                                else:
                                    detected = detect_market(msg_body, db_history)
                                    
                                    # ═══ AUTO-TRIGGER: If all 4 params collected, bypass LLM entirely ═══
                                    if session_has_all_params(session):
                                        ai_response = f'[PROPERTY_SEARCH: {{"bhk": {session["bhk"]}, "budget": {session["budget"]}, "location": "{session["location"]}", "purpose": "{session["purpose"]}"}}]'
                                    else:
                                        active_prompt = MASTER_SYSTEM_PROMPT
                                        active_prompt = build_state_aware_prompt(session, active_prompt)
                                        messages_array = [{"role": "system", "content": active_prompt}]
                                        
                                        # Pass last 8 messages (expanded from 3 to prevent amnesia)
                                        for past_msg in db_history[-8:]: 
                                            content = past_msg["content"]
                                            
                                            if "I am scanning our off-market" in content or "explore alternative tiers" in content or "processing your luxury" in content:
                                                continue
                                            # Only skip pure PROPERTY_SEARCH commands from assistant
                                            if past_msg["role"] == "assistant" and "PROPERTY_SEARCH" in content:
                                                continue
                                                
                                            messages_array.append(past_msg)
                                        
                                        lang_hint = "Respond ENTIRELY in Roman Urdu. ONLY ask for the MISSING info listed in session state above."
                                        messages_array.append({"role": "system", "content": lang_hint})
                                        
                                        messages_array.append({"role": "user", "content": msg_body})
                                        
                                        completion = robust_chat_completion(messages_array, 0.4, 150)
                                        ai_response = completion.choices[0].message.content
                                        
                                        # 🛡️ PREMATURE QUERY PREVENTION
                                        if "PROPERTY_SEARCH" in ai_response and not session_has_all_params(session):
                                            missing = []
                                            if not session.get("listing_type"): missing.append("Buy/Rent")
                                            if not session.get("property_type"): missing.append("Property Type")
                                            if not session.get("city_society"): missing.append("City/Area")
                                            if not session.get("budget"): missing.append("Budget")
                                            missing_str = ", ".join(missing)
                                            
                                            ai_response = f"Sir, property search shuru karne se pehle, barah-e-karam apna {missing_str} confirm kar dein. 🏛️"
                                    
                                    if "PROPERTY_SEARCH" in ai_response and "{" in ai_response and "}" in ai_response:
                                        try:
                                            start_idx = ai_response.find("{")
                                            end_idx = ai_response.rfind("}") + 1
                                            json_str = ai_response[start_idx:end_idx]
                                            search_params = json.loads(json_str)
                                            
                                            l_type = search_params.get("listing_type", session.get("listing_type", ""))
                                            p_type = search_params.get("property_type", session.get("property_type", ""))
                                            c_soc = search_params.get("city_society", session.get("city_society", ""))
                                            budget_val = normalize_budget(str(search_params.get("budget", session.get("budget", 0))))
                                            
                                            properties_list = query_property_database(l_type, p_type, c_soc, budget_val, tenant_id, booking_sheet_name, property_sheet_name)
                                            
                                            if properties_list:
                                                target_properties = properties_list[:3]
                                                actual_count = len(target_properties)
                                                
                                                if l_type.lower() == "rent":
                                                    scan_msg = f"Jee hamare paas {actual_count} premium rental portfolio(s) available hain, abhi dispatch ho rahe hain... 🏛️✨"
                                                else:
                                                    scan_msg = f"Hamare premium registries scan ho rahi hain. {actual_count} exclusive portfolio(s) abhi dispatch ho rahe hain... 🏛️✨"
                                                send_whatsapp_text(tenant_id, from_number, scan_msg, whatsapp_token)
                                                time.sleep(1)
                                                
                                                for idx, prop in enumerate(target_properties, start=1):
                                                    media_cols = ['Main_Image', 'Image_2', 'Image_3', 'Image_4', 'Image_5']
                                                    for col in media_cols:
                                                        img_url = str(prop.get(col, '')).strip()
                                                        if img_url and img_url.lower() != 'nan' and img_url.startswith('http'):
                                                            send_whatsapp_media(tenant_id, from_number, img_url, "image", whatsapp_token)
                                                    
                                                    prop_id = prop.get("Property_ID", f"PROP-PK-{idx}")
                                                    budget_fmt = format_currency(int(prop.get('Demand_PKR', 0)))
                                                    title = f"{prop.get('Size', '')} {prop.get('Property_Type', '')} in {prop.get('Society_Area', '')}"
                                                    location = f"{prop.get('Phase_Block', '')}, {prop.get('City', '')}"
                                                    
                                                    if l_type.lower() == "rent":
                                                        property_text = f"🏛️ *RENTAL MATCH*\n\n📌 *Asset:* {title}\n💵 *Rent:* PKR {budget_fmt}/month\n📍 *Location:* {location}"
                                                    else:
                                                        property_text = f"🏛️ *EXCLUSIVE ASSET MATCH*\n\n📌 *Asset:* {title}\n💵 *Demand:* PKR {budget_fmt}\n📍 *Location:* {location}"
                                                    
                                                    send_property_button(tenant_id, from_number, property_text, prop_id, whatsapp_token)
                                                    
                                                    video_url = str(prop.get('Video', '')).strip()
                                                    if video_url and video_url.lower() != 'nan' and video_url.startswith('http'):
                                                        video_text = f"🎥 *Exclusive Walkthrough Tour*\n{video_url}"
                                                        send_whatsapp_text(tenant_id, from_number, video_text, whatsapp_token)

                                                ai_response = "Is property ki private site visit schedule karne ke liye barah-e-karam apna Poora Naam aur Email share karein taake humari team raabta kare. ✨"
                                                
                                                # Reset session after successful property search
                                                session.update({"listing_type": None, "property_type": None, "city_society": None, "budget": None})
                                                
                                                save_supabase_message(from_number, "user", msg_body, tenant_id)
                                                save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                                send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                                return PlainTextResponse(content="OK", status_code=200)

                                            else:
                                                ai_response = "Sir, filhal is exact specific block mein hamari inventory sold out hai. Lekin is budget mein mere paas DHA Phase 8 ya Bahria Town mein behtareen options hain. Kya main unki details bhejun? 🏛️"
                                        except Exception as e: 
                                            logger.error(f"🚨 RAG LLM JSON Parse Crash: {str(e)}")
                                            ai_response = "Aapki luxury portfolio request process ho rahi hai. Kya aap apna target BHK, budget, location, aur purpose (Buy/Rent) confirm kar sakte hain? 🏛️"

                                    # 🔥 ULTIMATE SANITIZER (NO GHOSTING)
                                    # Agar LLM ne completely invalid text (like raw dict) daala jo parse nahi hua, 
                                    # ya kisi aur wajah se yahan tak phuncha, just ask them a natural question.
                                    if ai_response and ("PROPERTY_SEARCH" in ai_response or '{"' in ai_response):
                                        ai_response = "Bhai thori confusion hui hai. Kya aap confirm kar sakte hain ke aapko Rent par chahiye ya Buy karna hai, aur aapka budget kya hai? 🏛️"
                                            
                                    if not ai_response or str(ai_response).strip() == "":
                                        ai_response = "Bhai thori confusion hui hai. Kya aap confirm kar sakte hain aapko kya chahiye? 🏛️"

                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    if ai_response and "I am scanning our off-market registries" not in ai_response and "processing your luxury portfolio request" not in ai_response:
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        
                                    if whatsapp_token and ai_response: 
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    
        return PlainTextResponse(content="OK", status_code=200)
    except Exception as e:
        print(f"🚨 [BACKGROUND TASK ERROR] {str(e)}", flush=True)
        logger.exception(f"🚨 Webhook Parse Crash: {str(e)}")
        return PlainTextResponse(content="OK", status_code=200)