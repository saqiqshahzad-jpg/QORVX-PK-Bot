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
import tempfile

# ═══ SESSION STATE & DEDUP STORES ═══
AGENCY_KEYWORDS = ["al-madina", "dha-estates", "qorvx"]
PK_LOCATION_KEYWORDS = ["karachi", "lahore", "islamabad", "dha", "bahria", "clifton", "gulshan", "rawalpindi", "peshawar", "multan"]
USER_SESSIONS = {}      # key: "tenant_id:phone" -> {bhk, budget, location, purpose, market, language, agency_tag}
PROCESSED_MSG_IDS = {}   # message_id -> timestamp, auto-cleaned after 5 min

# --- MASTER SYSTEM PROMPT ---
# Defined below

# ---------------------------------------------

# 🚨 SECURE CONFIGURATION OVERLAY
load_dotenv()

from supabase import create_client, Client
url: str = os.environ.get("SUPABASE_URL", "")
key: str = os.environ.get("SUPABASE_KEY", "")
# Allow it to run even if missing in dev environment
if url and key:
    supabase: Client = create_client(url, key)
else:
    supabase = None

app = FastAPI()

# =========================================================================================
# 👑 PRODUCTION ENVIRONMENT VARIABLES LOCK
# =========================================================================================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

MY_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "ALAAUDIN_SECRET_TOKEN")

client = Groq(api_key=GROQ_API_KEY, max_retries=0)
MODEL_ID = "openai/gpt-oss-20b"
FALLBACK_MODEL = "openai/gpt-oss-20b"

# =========================================================================================
# 🎙️ AUDIO MESSAGE PROCESSING (Meta Download + OpenAI Whisper)
# =========================================================================================
def download_whatsapp_audio(media_id: str, token: str):
    """Fetches media URL from Meta and downloads the raw audio file to a temp file."""
    try:
        # Step 1: Get Media URL from Meta
        url_req = requests.get(f"https://graph.facebook.com/v25.0/{media_id}", headers={"Authorization": f"Bearer {token}"})
        if url_req.status_code != 200:
            logger.error(f"Meta Media URL fetch failed: {url_req.status_code} | {url_req.text}")
            return None
        
        media_url = url_req.json().get("url")
        if not media_url:
            logger.error("Meta returned no URL for media_id.")
            return None
            
        # Step 2: Download actual file
        media_req = requests.get(media_url, headers={"Authorization": f"Bearer {token}"})
        if media_req.status_code == 200:
            # Save to a temporary file
            temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
            with open(temp_audio.name, "wb") as f:
                f.write(media_req.content)
            return temp_audio.name
        else:
            logger.error(f"Meta audio download failed: {media_req.status_code}")
    except Exception as e:
        logger.error(f"Exception downloading audio: {e}")
    return None

def transcribe_audio_groq(file_path: str):
    """Transcribes audio file using Groq's fast and free Whisper API."""
    try:
        client = Groq() # Automatically uses the existing GROQ_API_KEY from environment
        with open(file_path, "rb") as file:
            transcription = client.audio.translations.create(
                file=(file_path, file.read()),
                model="whisper-large-v3",
                response_format="json"
            )
        
        # Bulletproof extraction logic
        if isinstance(transcription, str):
            msg_body = transcription
        elif isinstance(transcription, dict):
            msg_body = transcription.get("text", "")
        else:
            msg_body = getattr(transcription, "text", str(transcription))
            
        return msg_body.strip()
    except Exception as e:
        logger.error(f"Groq Whisper Error: {e}")
        raise e

def robust_chat_completion(messages_array, temperature, max_tokens):
    try:
        return client.chat.completions.create(
            model=MODEL_ID,
            temperature=temperature,
            max_tokens=512,
            messages=messages_array
        )
    except Exception as e:
        logger.info(f"Primary LLM failed ({str(e)}), trying fallback...")
        try:
            return client.chat.completions.create(
                model=FALLBACK_MODEL,
                temperature=temperature,
                max_tokens=512,
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

def get_user_session(phone_number: str, tenant_id: str):
    if not supabase:
        return None
    try:
        response = supabase.table('user_sessions').select('session_data').eq('phone_number', phone_number).eq('tenant_id', tenant_id).execute()
        if response.data:
            return response.data[0]['session_data']
        return None
    except Exception as e:
        logger.error(f"🚨 Supabase session fetch error: {str(e)}")
    return None

def update_user_session(phone_number: str, session_data: dict, tenant_id: str):
    if not supabase:
        return
    try:
        supabase.table('user_sessions').upsert({
            'phone_number': phone_number,
            'tenant_id': tenant_id,
            'session_data': session_data
        }).execute()
    except Exception as e:
        logger.error(f"🚨 Supabase session upsert error: {str(e)}")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# =========================================================================================
# 🚀 MULTI-TENANT GOOGLE WORKSPACE CLIENT & FREE SHEET CRM
# =========================================================================================
class GoogleSpreadsheetClient:
    def __init__(self, tenant_id: str, booking_sheet_name: str, property_sheet_name: str):
        self.tenant_id = tenant_id
        self.booking_sheet_name = "BookingSlot"
        self.property_sheet_name = "QORVX_PK_Master_Database"
        self.scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            raise ValueError("GOOGLE_CREDENTIALS environment variable is not set")
        creds_dict = json.loads(creds_json)
        self.gc = gspread.service_account_from_dict(creds_dict, scopes=self.scope)

    def get_booking_sheet(self):
        return self.gc.open(self.booking_sheet_name).sheet1

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

    def append_urgent_lead(self, phone: str, user_message: str):
        try:
            sh = self.gc.open(self.property_sheet_name)
            try:
                worksheet = sh.worksheet("Urgent_Leads")
            except gspread.exceptions.WorksheetNotFound:
                worksheet = sh.add_worksheet(title="Urgent_Leads", rows="1000", cols="3")
                worksheet.append_row(["Timestamp", "Phone", "User Message"])
            
            pk_time = pytz.timezone('Asia/Karachi')
            timestamp = datetime.datetime.now(pk_time).strftime("%Y-%m-%d %H:%M:%S")
            worksheet.append_row([timestamp, phone, user_message])
        except Exception as e:
            logger.error(f"🚨 Google Sheet Urgent Lead Append Crash: {str(e)}")

AGENCY_PROFILES_CACHE = {}

def get_agency_profile(agency_tag: str, tenant_id: str, property_sheet_name: str) -> dict:
    if not agency_tag:
        return {}
    if agency_tag in AGENCY_PROFILES_CACHE:
        return AGENCY_PROFILES_CACHE[agency_tag]
    try:
        workspace = GoogleSpreadsheetClient(tenant_id, "BookingSlot", property_sheet_name)
        sh = workspace.gc.open(workspace.property_sheet_name)
        try:
            worksheet = sh.worksheet("Agency_Profiles")
        except Exception:
            AGENCY_PROFILES_CACHE[agency_tag] = {}
            return {}
        records = worksheet.get_all_records()
        for row in records:
            if str(row.get("Agency_Tag", "")).strip() == agency_tag:
                AGENCY_PROFILES_CACHE[agency_tag] = row
                return row
    except Exception as e:
        logger.error(f"🚨 Failed to fetch Agency_Profiles: {e}")
    AGENCY_PROFILES_CACHE[agency_tag] = {}
    return {}

@lru_cache(maxsize=10)
def get_agency_tags(tenant_id: str, booking_sheet_name: str, property_sheet_name: str, cache_buster: int):
    try:
        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
        sheet = workspace.get_property_sheet()
        records = sheet.get_all_records()
        df = pd.DataFrame(records)
        agency_col = next((col for col in df.columns if str(col).strip().lower() == "agency_tag"), None)
        if agency_col:
            return df[agency_col].dropna().astype(str).unique().tolist()
        return []
    except Exception as e:
        logger.error(f"Error fetching agency tags: {e}")
        return []


# =========================================================================================
# 🚀 CORE ENGINE UTILITIES & DYNAMIC APPEND BOOKING ENGINE
# =========================================================================================
def handle_calendar_booking(date_req: str, time_req: str, phone: str, tenant_id: str, booking_sheet_name: str, property_sheet_name: str):
    try:
        logger.info(f"Initializing GoogleSpreadsheetClient for calendar booking. Tenant: {tenant_id}")
        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
        
        logger.info(f"Opening booking sheet: {booking_sheet_name}")
        sheet = workspace.gc.open(booking_sheet_name).sheet1
        
        logger.info("Fetching all values from booking sheet to check if empty...")
        if not sheet.get_all_values():
            logger.info("Booking sheet is empty. Appending header row...")
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
        
        logger.info("Fetching all records from booking sheet...")
        records = sheet.get_all_records()
        logger.info(f"Successfully fetched {len(records)} records from booking sheet.")
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
            logger.info(f"Appending new booking row for Date: {date_req}, Time: {matched_slot.upper()}")
            sheet.append_row([date_req, matched_slot.upper(), "Pending Client", phone, "Pending Email", "Booked 🚫"])
            logger.info("Booking row appended successfully.")
            return {"status": "success", "slot": matched_slot.upper()}
            
    except Exception as e:
        logger.error(f"🚨 Booking Engine Crash: {str(e)}")
        return {"status": "error"}

def query_property_database(listing_type: str, bhk: int, city_society: str, budget: int, tenant_id: str, booking_sheet_name: str, property_sheet_name: str, agency_tag: str = None, property_type: str = None):
    try:
        logger.info(f"Initializing GoogleSpreadsheetClient for tenant: {tenant_id}, booking_sheet: {booking_sheet_name}, property_sheet: {property_sheet_name}")
        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
        
        logger.info("Fetching property sheet...")
        sheet = workspace.get_property_sheet()
        import pandas as pd
        
        logger.info("Fetching all records from the property sheet...")
        records = sheet.get_all_records()
        logger.info(f"Successfully fetched {len(records)} records from Google Sheet.")
        df = pd.DataFrame(records)
        
        if df.empty:
            return []

        # Helper to get column case-insensitively
        def get_col(name):
            return next((c for c in df.columns if str(c).strip().lower() == name.lower()), None)

        # 2. STRICT Agency Tag Isolation
        if agency_tag:
            active_tag = agency_tag.strip().lower()
            agency_col = get_col("agency_tag")
            if agency_col:
                df = df[df[agency_col].astype(str).str.strip().str.lower() == active_tag]
                logger.info(f"After Agency Filter '{active_tag}': {len(df)} properties left.")
        
        if df.empty:
            return []

        # 3. DUAL-COLUMN Location Filter (Check both City and Society_Area)
        if city_society:
            loc = city_society.strip().lower()
            city_col = get_col("city")
            society_col = get_col("society_area")
            
            mask_city = df[city_col].astype(str).str.lower().str.contains(loc, na=False) if city_col else pd.Series(False, index=df.index)
            mask_society = df[society_col].astype(str).str.lower().str.contains(loc, na=False) if society_col else pd.Series(False, index=df.index)
            
            df = df[mask_city | mask_society]
            logger.info(f"After Location Filter: {len(df)} properties left.")

        # After location filter, strictly filter property type
        if property_type:
            prop_type_col = get_col("property_type")
            if prop_type_col:
                df = df[df[prop_type_col].astype(str).str.lower() == property_type.strip().lower()]
                logger.info(f"After Property Type Filter ({property_type}): {len(df)} properties left.")

        # 4. Exact Numeric BHK Filter
        if bhk:
            bhk_col = get_col("bhk")
            if bhk_col:
                df = df[pd.to_numeric(df[bhk_col], errors='coerce') == int(bhk)]
                logger.info(f"After BHK Filter: {len(df)} properties left.")

        # 5. SOFT Purpose Filter (Don't drop rows if AI guessed wrong, just try to match)
        if listing_type:
            purpose = listing_type.strip().lower()
            if purpose == "buy": purpose = "sale"
            listing_col = get_col("listing_type")
            if listing_col:
                temp_df = df[df[listing_col].astype(str).str.lower().str.contains(purpose, na=False)]
                # Only apply purpose filter if it actually yields results; otherwise, show what's available
                if not temp_df.empty:
                    df = temp_df
                else:
                    logger.warning(f"Purpose '{purpose}' yielded 0 matches. Bypassing purpose filter to show available inventory.")

        # 6. Budget Filter
        if budget > 0:
            budget_col = get_col("demand_pkr")
            if budget_col:
                try:
                    max_budget = float(budget)
                    # If budget was mistakenly set to <= 100 (e.g. 1 or 2 PKR), ignore the filter to prevent false sold-outs
                    if max_budget > 100 and max_budget < 900000000:
                        df_demand = pd.to_numeric(df[budget_col], errors='coerce')
                        df = df[(df_demand <= max_budget) | (df_demand.isna())]
                    logger.info(f"After Budget Filter: {len(df)} properties left.")
                except Exception as e:
                    logger.warning(f"Bypassing budget filter: {e}")

        logger.info(f"Final filtered properties ready to send: {len(df)}")

        if not df.empty: 
            return df.to_dict(orient="records")
        return []
    except Exception as e: 
        logger.error(f"🚨 Property DB Query Crash: {str(e)}")
        return []

# =========================================================================================
# 📲 DYNAMIC TENANT WHATSAPP ROUTING
# =========================================================================================
def send_property_media_sequence(to_number: str, prop: dict, tenant_id: str, access_token: str):
    
    # 1. Collect all valid Image URLs
    image_keys = [k for k in prop.keys() if "image" in str(k).lower()]
    image_urls = []
    for k in sorted(image_keys):
        url = str(prop.get(k, "")).strip()
        if url.startswith("http") and url != "N/A":
            image_urls.append(url)

    # 2. Collect Video URL
    video_url = str(prop.get("video") or prop.get("Video") or "").strip()
    if video_url and video_url != "N/A" and video_url.startswith("http"):
        if "dropbox.com" in video_url:
            import re
            # Replace dl=0 with raw=1 for direct streaming
            video_url = re.sub(r'[?&]dl=[01]', '', video_url)
            video_url += "?raw=1" if "?" not in video_url else "&raw=1"
        logger.info(f"Dispatching direct video stream: {video_url}")

    # 3. Dispatch Images (NO CAPTIONS)
    for idx, img_url in enumerate(image_urls):
        send_whatsapp_media(tenant_id, to_number, img_url, "image", access_token, caption="")
        time.sleep(0.5) # Prevent Meta rate-limit drops

    # 4. Dispatch Video
    if video_url and video_url != "N/A" and video_url.startswith("http"):
        send_whatsapp_media(tenant_id, to_number, video_url, "video", access_token, caption="🎥 Property Walkthrough Video")

def send_whatsapp_quick_reply_buttons(to_number: str, body_text: str, tenant_id: str, access_token: str):
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": body_text
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "btn_cheaper",
                            "title": "Sasti Option 📉"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "btn_next",
                            "title": "Koi Aur Option 🔄"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "btn_visit",
                            "title": "Visit Schedule 📅"
                        }
                    }
                ]
            }
        }
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"📤 [SEND QUICK REPLIES] Status: {res.status_code} | Body: {res.text[:200]}")
        return res
    except Exception as e:
        logger.error(f"🚨 [SEND QUICK REPLIES CRASH] {str(e)}")
        return None

def send_whatsapp_text(tenant_id: str, to_number: str, text_body: str, whatsapp_token: str):
    # 🚨 BULLETPROOF FAILSAFE: Never send empty body to Meta API (prevents 400 Bad Request crash)
    if not text_body or not str(text_body).strip():
        logger.error(f"CRITICAL: Attempted to send empty message to Meta API for {to_number}. Injecting fallback.")
        text_body = "Janab, main aapki nayi requirement process kar raha hoon. Barah-e-karam batayein, aap is waqt property ke liye konsa shehar ya area dekh rahe hain? 📍"
    
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {
            "preview_url": True,
            "body": text_body
        }
    }
    try: 
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"📤 [SEND TEXT] To: {to_number} | Status: {res.status_code} | Body: {res.text[:200]}")
        return res
    except Exception as e: 
        logger.error(f"🚨 [SEND TEXT CRASH] To: {to_number} | Error: {str(e)}")
        return None

def send_whatsapp_media(tenant_id: str, to_number: str, media_url: str, media_type: str, whatsapp_token: str, caption: str = None):
    """
    Sends media to WhatsApp. If a video exceeds the 16MB limit, it automatically retries sending it as a 100MB-limit Document.
    """
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    
    # Do not pass empty captions to Meta to avoid payload formatting errors
    media_payload = {"link": media_url}
    if caption and caption.strip():
        media_payload["caption"] = caption

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": media_type,
        media_type: media_payload
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"🎬 [META MEDIA RESPONSE] Status: {res.status_code} | Body: {res.text}")
        
        try:
            response_data = res.json()
        except Exception:
            response_data = {}
            
        if res.status_code != 200 or "error" in response_data:
            error_msg = str(response_data.get("error", response_data))
            logger.error(f"Meta Media Upload Failed ({media_type}): {error_msg}")
            
            is_size_error = "exceeds maximum allowed size" in error_msg.lower()
            
            # --- SMART HACK: Try Video as Document (100MB Limit) ---
            if media_type == "video" and is_size_error:
                logger.info("Video exceeds 16MB. Retrying as a Document payload...")
                doc_obj = {"link": media_url, "filename": "Property_Walkthrough.mp4"}
                if caption and caption.strip():
                    doc_obj["caption"] = caption
                    
                doc_payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to_number,
                    "type": "document",
                    "document": doc_obj
                }
                doc_resp = requests.post(url, headers=headers, json=doc_payload)
                if doc_resp.status_code == 200 and "error" not in doc_resp.json():
                    logger.info("Document hack successful! Video sent as file.")
                    return True
                else:
                    logger.error(f"Document fallback also failed: {doc_resp.json()}")
            
            # --- IMAGE SIZE FAILSAFE: Catch oversized images (>5MB Meta limit) ---
            if media_type == "image" and is_size_error:
                logger.warning(f"Image exceeds Meta 5MB limit. Falling back to text link: {media_url}")
            
            # --- FINAL FALLBACK: Send as Text Link (for ALL oversized/failed media) ---
            logger.info("Executing Text Link Fallback.")
            if caption:
                fallback_text = f"📎 *{caption}*\n\nJanab, yeh file bari hone ki wajah se direct load nahi hui, is link par click karke dekh lein:\n👉 {media_url}"
            else:
                fallback_text = f"📎 Janab, yeh media file bari hone ki wajah se load nahi hui, is link par dekh lein:\n👉 {media_url}"
                
            send_whatsapp_text(tenant_id, to_number, fallback_text, whatsapp_token)
            return False
            
        return True
    except Exception as e:
        logger.error(f"Exception in send_whatsapp_media: {e}")
        return False

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

def send_menu_buttons(to_number: str, tenant_id: str, whatsapp_token: str):
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Janab, aap property ke sath kya karna chahte hain? Neeche option select karein 👇"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "intent_buy", "title": "Kharidni Hai"}},
                    {"type": "reply", "reply": {"id": "intent_rent", "title": "Rent Par Leni"}},
                    {"type": "reply", "reply": {"id": "intent_sell", "title": "Bechni Hai"}}
                ]
            }
        }
    }
    try: 
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code != 200:
            logger.error(f"Failed to send interactive buttons: {response.text}")
    except Exception as e: 
        logger.error(f"Error sending interactive buttons: {e}")

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

def format_pkr_currency(value):
    """Safely formats raw numbers into PKR comma format with Lakh/Crore readable strings."""
    if value is None or str(value).strip().upper() == "N/A":
        return "N/A"
    try:
        # Clean string from any existing commas or spaces
        clean_val = str(value).replace(',', '').replace(' ', '').strip()
        num = float(clean_val)
        if num.is_integer():
            num = int(num)
        
        # Add standard comma formatting
        formatted_num = f"{num:,}"
        
        # Generate local readable format (Lakh/Crore)
        if num >= 10000000:
            readable = f"{num / 10000000:g} Crore"
        elif num >= 100000:
            readable = f"{num / 100000:g} Lakh"
        elif num >= 1000:
            readable = f"{num / 1000:g} Thousand"
        else:
            readable = str(num)
            
        return f"{formatted_num} ({readable})"
    except Exception:
        return str(value) # Fallback to original string if not parseable

# ═══════════════════════════════════════════════════════════════
# 🧠 SESSION STATE ENGINE (ANTI-AMNESIA CORE)
# ═══════════════════════════════════════════════════════════════

def extract_and_update_session(session: dict, msg_body: str, chat_history: list, tenant_id: str, tenant_config: dict) -> dict:
    """Updates the passed session dictionary with extracted variables."""
    msg_lower = msg_body.lower().strip()

    # ── AGENCY TAG detection (Dynamic) ──
    cache_buster = int(time.time() // 300) # 5-minute cache
    unique_tags = get_agency_tags(tenant_id, tenant_config.get("booking_sheet_name"), tenant_config.get("property_sheet_name"), cache_buster)
    
    for tag in unique_tags:
        clean_tag = tag.strip().lower()
        if clean_tag and (clean_tag in msg_lower or clean_tag.replace("_", " ") in msg_lower):
            session["agency_tag"] = tag.strip()
            logger.info(f"Dynamically locked agency_tag: {session['agency_tag']}")
            break

    # ── Detect market & language ──
    detected = detect_market(msg_body, chat_history)
    session["market"] = detected["market"]
    session["language"] = detected["language"]

    # 1. Purpose Extraction
    if any(k in msg_lower for k in ["buy", "kharidna", "purchase", "khareedna"]):
        session["purpose"] = "buy"
    elif any(k in msg_lower for k in ["rent", "kiraya", "rent pr", "rent par"]):
        session["purpose"] = "rent"

    # 2. Property Type Extraction
    if any(k in msg_lower for k in ["plot", "zameen"]):
        session["property_type"] = "plot"
        session["bhk"] = 0 # Plots don't have BHK
    elif any(k in msg_lower for k in ["flat", "apartment"]):
        session["property_type"] = "flat"
    elif any(k in msg_lower for k in ["house", "makan", "bangla", "villa", "ghar"]):
        session["property_type"] = "house"

    # ── Find last AI message for context ──
    last_ai_content = ""
    if chat_history:
        for row in reversed(chat_history):
            if row["role"] == "assistant":
                last_ai_content = row["content"].lower()
                break

    # ── BHK detection ──
    bhk_match = re.search(r'\b([1-9]|10)\s*(?:bhk|bed|bedroom|bad room|room)?\b', msg_lower)
    if bhk_match and not session.get("bhk"):
        # If message is just a number like "1" or "2", it's BHK
        session["bhk"] = int(bhk_match.group(1))
        logger.info(f"Locked BHK: {session['bhk']}")

    # ── LOCATION detection ──
    if not session.get("location"):
        for kw in PK_LOCATION_KEYWORDS:
            if kw in msg_lower:
                session["location"] = kw.title()
                break
        # Context: AI asked for location and user gave short answer
        if not session.get("location") and len(msg_body.split()) <= 3:
            if any(kw in last_ai_content for kw in ["location", "city", "area", "jagah", "shehar", "ilaq", "kidhar", "preferred location"]):
                if not msg_lower.strip().isdigit():
                    session["location"] = msg_body.strip().title()

    # ── BUDGET detection ──
    if not session.get("budget"):
        # Budget Extraction (Requires Lakh, Crore, Thousand, K, or numbers >= 1000)
        budget_keywords = ["lakh", "lac", "crore", "cr", "hazar", "k", "budget", "pkr", "rs"]
        has_budget_context = any(k in msg_lower for k in budget_keywords)
        
        # Extract large numeric values
        digits = re.findall(r'\b\d+\b', msg_lower)
        if digits:
            val = int(digits[0])
            if has_budget_context or val > 1000:
                extracted_budget = normalize_budget(msg_body)
                if extracted_budget >= 1000:
                    session["budget"] = extracted_budget
                    logger.info(f"Locked Budget: {session['budget']}")

    return session

def build_state_aware_prompt(session: dict, base_prompt: str) -> str:
    """Inject current session state into the LLM system prompt."""
    collected = []
    missing = []

    if session.get("purpose"): collected.append(f"Listing Type: {session['purpose']}")
    else: missing.append("Listing_Type (Buy or Rent)")

    if session.get("bhk"): collected.append(f"BHK: {session['bhk']}")
    else: missing.append("BHK (Number of bedrooms / Size)")

    if session.get("location"): collected.append(f"City/Society: {session['location']}")
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
    return all([session.get("purpose"), session.get("bhk"), session.get("location"), session.get("budget")])

def get_master_system_prompt(session: dict) -> str:
    current_agency = session.get("agency_tag")
    formatted_agency = str(current_agency).replace("_", " ").title() if current_agency else "Our Real Estate Agency"

    return f"""Identity: Aap {formatted_agency} ke smart, warm aur professional AI Real Estate Consultant hain.

CORE CONVERSATIONAL GUIDELINES:
1. GENDER-NEUTRAL RESPECT: Strictly NEVER use 'Sir', 'Madam', or 'Bhai'. Address the user respectfully using gender-neutral words like 'Janab', 'Mohtaram', or simply 'Aap'.
2. NATURAL GREETING (MESSAGE #1): Agar user ne sirf Salam, greeting, ya agency ka naam liya hai, toh foran interrogation shuru mat karein (do NOT immediately ask 'buy karna hai ya rent' ya BHK). Pehle warm greeting dein aur poochein:
   - Example: "Walaikum Assalam! Ji janab, {formatted_agency} mein khush-amdeed. Main aapki kya madad kar sakta hoon? 🏡✨"
3. LET USER LEAD: Jab user khud bataye ke usay property chahiye ya kya talaash kar raha hai, tab natural andaz mein agle sawalat poochein.
4. STRICT AGENCY LOYALTY: Aap sirf aur sirf '{formatted_agency}' ko represent karte hain. Aapka kaam sirf apni agency ki properties recommend karna hai.
5. LANGUAGE: 100% Polite, warm, aur natural Roman Urdu.
6. The "Zero-Silence" Rule: Always end your message with a gentle, relevant question to keep the conversation moving. Never leave a dead-end response.

7. The Core State Machine (4-Step Qualification)
Your main objective is to collect these exactly 4 details before searching the database:
- Listing_Type: (Buy / Rent)
- City / Location: (e.g., Lahore, Karachi, DHA)
- BHK: (Number of bedrooms / Size)
- Budget: (In Lakh/Crore)

Strict Rules:
- Until ALL 4 details are collected, politely ask ONLY for the missing details (AFTER the user has stated intent).
- CRITICAL NORMALIZATION RULE: When parsing the budget, ALWAYS convert textual currency ("50 lakh", "1.5 crore", "50k") into pure integers (e.g., 5000000, 15000000, 50000). Never output strings for budget.
- CRITICAL MEMORY RULE (State Retention): You MUST preserve and carry forward any parameters (location, bhk, budget, purpose) that the user provided in previous turns. ONLY update a parameter if the user explicitly changes it. DO NOT set previously acquired parameters to null just because they are missing from the user's newest message. Build upon the existing state.
- CRITICAL FORMATTING (English/Numeric Normalization): No matter what language or script the user inputs (Urdu script, Roman Urdu, etc.), you MUST translate and save ALL extracted JSON parameter values in standard English spelling and standard numeric digits. Examples: If the user says 'اسلام آباد', save "location": "Islamabad". If the user says 'ایک لاکھ بیس ہزار', save "budget": 120000. If the user says 'فلیٹ', save "property_type": "flat". If the user says 'لاہور', save "location": "Lahore". If the user says 'پانچ کروڑ', save "budget": 50000000. NEVER save Urdu script (Arabic alphabet) inside the JSON parameter values. All city names, property types, and numeric values MUST be in English.
- When ALL 4 variables are collected, YOU MUST OUTPUT EXACTLY THIS JSON FORMAT ON A NEW LINE:
[PROPERTY_SEARCH: {{"bhk":<int>,"budget":<int>,"location":"<str>","purpose":"buy"|"rent"}}]
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
    active_sessions = []
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

                            # ═══ DEDUP BYPASS FOR FIRST-TIME USERS & MENU ═══
                            peek_body = ""
                            if message.get("type") == "text":
                                peek_body = message.get("text", {}).get("body", "").strip().lower()
                                
                            session = get_user_session(from_number, tenant_id)
                            is_first_time = (session is None)
                            is_menu = (peek_body == "menu")

                            # ═══ DEDUP: Skip if Meta retried this message ═══
                            msg_id = message.get("id", "")
                            if msg_id:
                                now = time.time()
                                if msg_id in PROCESSED_MSG_IDS and not (is_first_time or is_menu):
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

                            # --- HANDLE AUDIO (VOICE NOTES) ---
                            elif message.get("type") == "audio":
                                audio_id = message["audio"]["id"]
                                logger.info(f"🎙️ Audio message received. ID: {audio_id}")
                                
                                audio_file_path = download_whatsapp_audio(audio_id, whatsapp_token)
                                if audio_file_path:
                                    try:
                                        msg_body = transcribe_audio_groq(audio_file_path)
                                        logger.info(f"🎙️ Groq Transcription Success: {msg_body}")
                                    except Exception as e:
                                        logger.error(f"🎙️ Groq Transcription Error: {e}")
                                        reply_text = "Maazrat janab, internet connection ya background shor ki wajah se main aapki aawaz theek se sun nahi paya. Barah-e-karam apna paigham text mein likh kar bhej dein. 📝"
                                        send_whatsapp_text(tenant_id, from_number, reply_text, whatsapp_token)
                                        save_supabase_message(from_number, "user", "[Voice Note - Transcription Failed]", tenant_id)
                                        save_supabase_message(from_number, "assistant", reply_text, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)
                                    finally:
                                        # Clean up temp file to save disk space
                                        if audio_file_path and os.path.exists(audio_file_path):
                                            os.remove(audio_file_path)
                                else:
                                    logger.error("Failed to download audio media from Meta.")
                                    reply_text = "Maazrat janab, aapki voice note load nahi ho saki. Barah-e-karam dobara bhejein ya text mein likh dein. 📝"
                                    send_whatsapp_text(tenant_id, from_number, reply_text, whatsapp_token)
                                    save_supabase_message(from_number, "user", "[Voice Note - Download Failed]", tenant_id)
                                    save_supabase_message(from_number, "assistant", reply_text, tenant_id)
                                    return PlainTextResponse(content="OK", status_code=200)

                            # --- HANDLE NON-TEXT MEDIA (DARK PSYCHOLOGY DEMO LOCK) ---
                            elif message.get("type") not in ["text", "interactive"]:
                                msg_type = message.get("type", "media")
                                media_type_map = {
                                    "image": "tasveer",
                                    "document": "document",
                                    "video": "video",
                                    "sticker": "sticker"
                                }
                                media_name = media_type_map.get(msg_type, msg_type)
                                
                                dark_psychology_msg = f"Arre wah, seedha {media_name}? Lekin ek choti si rukawat hai, yeh demo version hai, isliye live image-scanning ka feature abhi restricted rakha gaya hai taake server load na barhe.\n\nAsli version mein AI khud tasveer parh kar rate bata deta hai. Batayein, filhal text mein koi property search karni hai?"
                                
                                logger.info(f"🔒 Triggered Dark Psychology Demo Lock for type: {msg_type}")
                                save_supabase_message(from_number, "user", f"[{msg_type.upper()} RECEIVED]", tenant_id)
                                save_supabase_message(from_number, "assistant", dark_psychology_msg, tenant_id)
                                send_whatsapp_text(tenant_id, from_number, dark_psychology_msg, whatsapp_token)
                                return PlainTextResponse(content="OK", status_code=200)

                            if msg_body:

                                db_history = get_supabase_history(from_number, tenant_id)
                                last_ai_msg = db_history[-1]["content"] if db_history else ""
                                
                                msg_clean = msg_body.lower().strip()
                                
                                # --- INTELLIGENT ROUTER & SUPABASE PERSISTENCE ---
                                # session is already fetched above for DEDUP check

                                if session is None:
                                    logger.info("New user detected. Sending Menu.")
                                    session = {"purpose": None, "bhk": None, "location": None, "budget": None, "agency_tag": None, "state": None, "intent": None, "greeting_done": True, "chat_history": []}
                                    
                                    # --- EXTRACT AGENCY TAG FROM INITIAL MESSAGE ---
                                    # Pattern: "mujhe [Agency_Name] ki properties"
                                    import re
                                    match = re.search(r"mujhe\s+(.+?)\s+ki properties", msg_body, re.IGNORECASE)
                                    if match:
                                        session["agency_tag"] = match.group(1).strip()
                                        logger.info(f"Extracted agency_tag from regex: {session['agency_tag']}")
                                    else:
                                        cache_buster = int(time.time() // 300)
                                        unique_tags = get_agency_tags(tenant_id, tenant_config.get("booking_sheet_name"), tenant_config.get("property_sheet_name"), cache_buster)
                                        for tag in unique_tags:
                                            clean_tag = tag.strip().lower()
                                            if clean_tag and (clean_tag in msg_clean or clean_tag.replace("_", " ") in msg_clean):
                                                session["agency_tag"] = tag.strip()
                                                logger.info(f"Dynamically locked agency_tag on first message: {session['agency_tag']}")
                                                break
                                            
                                    active_sessions.append((from_number, session, tenant_id))
                                    
                                    if session.get("agency_tag"):
                                        agency_name = str(session["agency_tag"]).replace("_", " ").title()
                                    else:
                                        agency_name = str(tenant_config.get("agency_tag", "Real Estate Agency")).replace("_", " ").title()

                                        
                                    send_whatsapp_text(tenant_id, from_number, f"Walaikum Assalam! {agency_name} mein khush-amdeed. ✨", whatsapp_token)
                                    send_menu_buttons(from_number, tenant_id, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # Register session for auto-save in finally block
                                active_sessions.append((from_number, session, tenant_id))

                                # =========================================================================================
                                # 🔘 INTERACTIVE BUTTON FAST-TRACK ROUTER
                                # =========================================================================================
                                if btn_id:
                                    logger.info(f"User clicked interactive button: {btn_id}")
                                    
                                    if btn_id == "intent_buy":
                                        session["purpose"] = "buy"
                                        ai_response = "Behtareen! Janab, aap kis shehar ya specific society mein property dekhna chahte hain? 📍"
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", "Clicked Kharidni Hai", tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif btn_id == "intent_rent":
                                        session["purpose"] = "rent"
                                        ai_response = "Zaroor! Janab, aap rent ke liye kis shehar ya area mein option dekh rahe hain? 📍"
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", "Clicked Rent Par Leni", tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif btn_id == "intent_sell":
                                        session["purpose"] = "sell"
                                        session["state"] = "ASKING_SELL_TYPE"
                                        ai_response = "Zabardast janab! 🤝 Aap kya bechna chahte hain? (Misaal ke taur par: Ghar, Flat, Plot, ya Commercial) 🏡"
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", "Clicked Bechni Hai", tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif btn_id == "btn_cheaper":
                                        session["state"] = None 
                                        session["budget"] = None
                                        session["seen_properties"] = []
                                        ai_response = "Janab, bilkul! Main aapko is se kam price mein options dikhata hoon. Barah-e-karam apna naya approximate budget bata dein? 📉"
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", "Clicked Sasti Option", tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif btn_id == "btn_next":
                                        session["state"] = None
                                        session["active_property"] = None
                                        ai_response = f"Zaroor janab! Main aapko isi criteria mein agli behtareen property nikal kar deta hoon... 🔍"
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", "Clicked Koi Aur Option", tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif btn_id == "btn_visit":
                                        session["state"] = "SCHEDULING_VISIT"
                                        ai_response = "Behtareen! Is property ka physical visit arrange karne ke liye, barah-e-karam apna Pura Naam aur Phone Number share kardein taake hamara agent aapse rabta kar le. 📅🤝"
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", "Clicked Visit Schedule", tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                if msg_clean == "menu":
                                    logger.info("User requested menu. Clearing context.")
                                    if "archived_intents" not in session:
                                        session["archived_intents"] = []
                                    if session.get("purpose") or session.get("property_type"):
                                        session["archived_intents"].append({
                                            "property_type": session.get("property_type"),
                                            "budget": session.get("budget"),
                                            "bhk": session.get("bhk"),
                                            "location": session.get("location"),
                                            "purpose": session.get("purpose")
                                        })
                                    session["state"] = None
                                    session["intent"] = None
                                    session["purpose"] = None
                                    session["active_property"] = None
                                    send_menu_buttons(from_number, tenant_id, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)
                                    
                                is_greeting = any(word in msg_clean for word in ["salam", "hello", "hi", "assalam", "hy"])
                                if is_greeting and session.get("purpose"):
                                    logger.info("Returning user greeted. Sending contextual prompt.")
                                    RETURNING_PROMPT = f"""User is returning. Past context: {session}.
User message: "{msg_body}"
Action: Greet them back politely. Acknowledge their past interest naturally. Ask if they want to continue with that or see the 'Menu' for other options. Keep it short and professional in Roman Urdu."""
                                    completion = client.chat.completions.create(
                                        model=MODEL_ID,
                                        messages=[{"role": "system", "content": RETURNING_PROMPT}],
                                        temperature=0.3,
                                        max_tokens=512
                                    )
                                    ai_response = completion.choices[0].message.content
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # Initialize chat history if empty
                                if "chat_history" not in session:
                                    session["chat_history"] = []
                                    
                                # Append current user message
                                session["chat_history"].append({"role": "user", "content": msg_body})
                                
                                # Ensure memory doesn't exceed the last 6 turns (to save tokens)
                                if len(session["chat_history"]) > 6:
                                    session["chat_history"] = session["chat_history"][-6:]

                                # --- 1. INTENT SHIFT DETECTOR ---
                                new_prop_type = None
                                if any(k in msg_clean for k in ["plot", "zameen"]): new_prop_type = "plot"
                                elif any(k in msg_clean for k in ["flat", "apartment"]): new_prop_type = "flat"
                                elif any(k in msg_clean for k in ["house", "makan", "bangla"]): new_prop_type = "house"

                                current_prop_type = session.get("property_type")
                                
                                # If the user changed their mind about the property type mid-conversation
                                if new_prop_type and current_prop_type and new_prop_type != current_prop_type:
                                    logger.info(f"Intent Shift: {current_prop_type} -> {new_prop_type}. Wiping conflicting session data.")
                                    if "archived_intents" not in session:
                                        session["archived_intents"] = []
                                    session["archived_intents"].append({
                                        "property_type": current_prop_type,
                                        "budget": session.get("budget"),
                                        "bhk": session.get("bhk"),
                                        "location": session.get("location"),
                                        "purpose": session.get("purpose")
                                    })
                                    session["property_type"] = new_prop_type
                                    session["budget"] = None  # New property means new budget needed
                                    session["bhk"] = None
                                    session["location"] = None  # Force re-qualification of location
                                    
                                    # 🚨 CRITICAL FIX: Completely unlock the Q&A state!
                                    session["state"] = None
                                    session["active_property"] = None
                                    session["seen_properties"] = []  # Fresh inventory for new property type
                                    logger.info("Cleared INSPECTING_PROPERTY state + active_property + seen_properties. Routing back to fresh qualification.")
                                    
                                    # Ask LLM to naturally acknowledge the shift
                                    shift_prompt = f"User was looking for {current_prop_type}, but just said: '{msg_body}'. Acknowledge the change politely in Roman Urdu, and ask if they want to BUY or RENT this new {new_prop_type}."
                                    
                                    system_msg = {"role": "system", "content": shift_prompt}
                                    llm_messages = [system_msg] + session.get("chat_history", [])
                                    
                                    completion = robust_chat_completion(llm_messages, 0.3, 100)
                                    ai_response = completion.choices[0].message.content
                                    
                                    # CRITICAL FAILSAFE: Never send empty string to Meta API
                                    if not ai_response or not str(ai_response).strip():
                                        logger.warning("LLM returned empty string on intent shift. Failsafe triggered.")
                                        ai_response = f"Zaroor janab, bilkul! Aap {new_prop_type} dekhna chahte hain — behtareen! Yeh aap kharidna chahte hain ya rent par lena chahte hain? 🏡"
                                    
                                    session["chat_history"].append({"role": "assistant", "content": ai_response})
                                    
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # ═══ SESSION STATE: Extract & persist parameters ═══
                                # ── Update Core Session Context ──
                                session = extract_and_update_session(session, msg_body, db_history, tenant_id, tenant_config)
                                logger.info(f"🧠 [SESSION] {from_number}: bhk={session.get('bhk')} loc={session.get('location')} purpose={session.get('purpose')} budget={session.get('budget')} market={session.get('market')}")

                                # =========================================================================================
                                # ➕ MORE OPTIONS: Sell + Book Strategy (Overflow from 3-button limit)
                                # =========================================================================================
                                if msg_body == "More Options ➕":
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
                                        logger.info(f"Initializing GoogleSpreadsheetClient to append lead. Tenant: {tenant_id}")
                                        workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
                                        
                                        logger.info(f"Appending lead record to Google Sheet for phone: {from_number}, email: {client_email}")
                                        workspace.append_lead_record(from_number, extracted_name, client_email, target_prop_id, "PK")
                                        logger.info("Successfully appended lead record to Google Sheet.")
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
                                    session["state"] = None
                                    session["purpose"] = None
                                    session["location"] = None
                                    session["bhk"] = None
                                    session["budget"] = None
                                    session["seen_properties"] = []  # Full reset
                                    ai_response = "Understood. We continuously update our off-market assets. State any new parameters whenever you are ready. 🏛️"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif msg_body == "Lower Budget 💰":
                                    session["state"] = None
                                    session["budget"] = None
                                    session["seen_properties"] = []  # New budget = fresh inventory
                                    ai_response = "No problem at all. Please state your revised maximum budget, BHK count, and target area to re-query the network. 🔍"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)
                                # 🔥 MAKHAN LOGIC: Area Change Interceptor (DUAL-MARKET AWARE)
                                elif msg_body == "Change Area 📍":
                                    session["state"] = None
                                    session["location"] = None
                                    session["budget"] = None
                                    session["seen_properties"] = []  # New area = fresh inventory
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
                                    
                                    # --- SELLER PIPELINE ---
                                    if session.get("state") == "ASKING_SELL_TYPE":
                                        p_type = msg_body.lower()
                                        session["sell_property_type"] = msg_body.strip()
                                        session["state"] = "COLLECTING_SELLER_DETAILS"
                                        
                                        # Tailor the one-shot question based on property type
                                        if any(word in p_type for word in ["flat", "ghar", "house", "bangla", "apartment"]):
                                            ai_response = "Behtareen! Janab, agent ko achi deal nikalne ke liye bas ek hi message mein yeh details bhej dein:\n\n📍 Location / Society\n📏 Size (Marla/Sq Yd)\n🛏️ Rooms & Bathrooms\n💰 Demand (Price)\n👤 Aapka Pura Naam"
                                        else:
                                            # Plots/Commercial don't need beds/baths
                                            ai_response = "Behtareen! Janab, agent ko achi deal nikalne ke liye bas ek hi message mein yeh details bhej dein:\n\n📍 Location / Society\n📏 Size (Marla/Sq Yd)\n💰 Demand (Price)\n👤 Aapka Pura Naam"
                                            
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                        save_supabase_message(from_number, "user", msg_body, tenant_id)
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif session.get("state") == "COLLECTING_SELLER_DETAILS":
                                        logger.info("Parsing seller details via LLM JSON mode.")
                                        
                                        EXTRACT_PROMPT = f"""
                                        Extract the following real estate details from the user's message. 
                                        User Message: "{msg_body}"
                                        
                                        Respond ONLY in strictly valid JSON format with these exact keys:
                                        "Name" (str), "Location" (str), "Size" (str), "Features" (str - extract beds/baths if any, else "N/A"), "Demand" (int).
                                        
                                        CRITICAL NUMBER NORMALIZATION RULE:
                                        - Demand (Budget/Price): ALWAYS convert to a pure integer. (e.g., "50 lakh" -> 5000000, "1.5 crore" -> 15000000, "50k" -> 50000). Never output strings like "50 Lakh".
                                        - Size: Format clearly with the unit (e.g., "1 Kanal", "20 Marla").
                                        
                                        If a value is missing, output "N/A" (or 0 for integers).
                                        """
                                        
                                        extracted_data = {}
                                        try:
                                            completion = client.chat.completions.create(
                                                model=MODEL_ID, 
                                                messages=[{"role": "user", "content": EXTRACT_PROMPT}],
                                                response_format={"type": "json_object"},
                                                temperature=0.1,
                                                max_tokens=512
                                            )
                                            import json
                                            extracted_data = json.loads(completion.choices[0].message.content)
                                            
                                            # --- GOOGLE SHEET APPEND LOGIC ---
                                            import datetime
                                            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                            lead_row = [
                                                str(current_time), 
                                                str(from_number), 
                                                str(extracted_data.get('Name', 'N/A')), 
                                                str(session.get('sell_property_type', 'N/A')), 
                                                str(extracted_data.get('Location', 'N/A')), 
                                                str(extracted_data.get('Size', 'N/A')), 
                                                str(extracted_data.get('Features', 'N/A')), 
                                                str(extracted_data.get('Demand', 'N/A'))
                                            ]
                                            
                                            workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
                                            sheet = workspace.gc.open(property_sheet_name).worksheet("Seller_Leads")
                                            sheet.append_row(lead_row)
                                            logger.info(f"Appended Seller Lead to Sheet: {extracted_data}")
                                        except Exception as e:
                                            logger.error(f"Failed to parse or append seller lead to sheet: {e}")

                                        # Clear state and confirm
                                        session["state"] = None
                                        session["purpose"] = None
                                        
                                        confirm_msg = f"Shukriya {extracted_data.get('Name', 'janab')}! Aapki property ({session.get('sell_property_type', 'property')}) ki details hamare system mein save ho gayi hain. Hamara agent jald aapse rabta karega. 🤝✨"
                                        send_whatsapp_text(tenant_id, from_number, confirm_msg, whatsapp_token)
                                        save_supabase_message(from_number, "user", msg_body, tenant_id)
                                        save_supabase_message(from_number, "assistant", confirm_msg, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)
                                        
                                    # --- STATE 3: BOOKING VISIT (LEAD CAPTURE) ---
                                    if session.get("state") == "BOOKING_VISIT":
                                        import re
                                        from datetime import datetime
                                        
                                        # 1. Extract Email
                                        email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', msg_body)
                                        email = email_match.group(0) if email_match else "N/A"
                                        
                                        # 2. Extract Name cleanly (strip email and punctuation)
                                        name = msg_body.replace(email, "").replace(",", "").replace("/", "").strip()
                                        if not name:
                                            name = "Client"
                                            
                                        # 3. Context Data
                                        active_prop = session.get("active_property", {})
                                        property_id = active_prop.get("Property_ID", active_prop.get("property_id", "Prop_Unknown"))
                                        phone_number = str(from_number)
                                        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        
                                        # 4. Strict Column Order: [Col A: Property_ID, Col B: Name, Col C: Email, Col D: Phone_Number, Col E: Date_Time]
                                        lead_row = [str(property_id), str(name), str(email), str(phone_number), str(current_time)]
                                        
                                        try:
                                            workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet_name, property_sheet_name)
                                            sheet = workspace.gc.open(property_sheet_name).worksheet("Leads")
                                            sheet.append_row(lead_row)
                                            logger.info(f"Lead Row Appended: {lead_row}")
                                        except Exception as e:
                                            logger.error(f"Failed to append row to Leads sheet: {e}")
                                            
                                        session["state"] = "INSPECTING_PROPERTY"
                                        
                                        reply_text = "Bohat shukriya janab! ✅ Aapki details hamare paas mehfooz ho gayi hain aur agent ko forward kar di gayi hain. Hamara numainda jald hi aap se rabta karega. 🏡✨\n\nKya aapko is property ke baare mein kuch aur janna hai?"
                                        send_whatsapp_text(tenant_id, from_number, reply_text, whatsapp_token)
                                        save_supabase_message(from_number, "user", msg_body, tenant_id)
                                        save_supabase_message(from_number, "assistant", reply_text, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    # --- STATE 1: POST-SEARCH GROUNDED Q&A ---
                                    if session.get("state") == "INSPECTING_PROPERTY":
                                        active_prop = session.get("active_property", {})
                                        
                                        # Check if user clicked a reset/search button
                                        if "sasti" in msg_body.lower() or "koi aur" in msg_body.lower():
                                            session["state"] = None  # Reset state to allow new search
                                        else:
                                            # Fallback Lead Capture Routing
                                            email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', msg_body)
                                            if email_match:
                                                client_email = email_match.group(0)
                                                client_name = "Client"  # Simple fallback
                                                prop_id = session.get("active_property", {}).get("Property_ID", "Unknown")
                                                workspace = GoogleWorkspaceManager(tenant_id, booking_sheet_name, property_sheet_name)
                                                workspace.append_lead_record(from_number, client_name, client_email, prop_id, "PK")
                                                
                                                confirm_msg = "Shukriya Janab! Aapki details agent ko bhej di gayi hain, wo jald aapse rabta karenge. 🤝"
                                                send_whatsapp_text(tenant_id, from_number, confirm_msg, whatsapp_token)
                                                save_supabase_message(from_number, "user", msg_body, tenant_id)
                                                save_supabase_message(from_number, "assistant", confirm_msg, tenant_id)
                                                
                                                session["state"] = None
                                                return PlainTextResponse(content="OK", status_code=200)

                                            logger.info("Handling follow-up property question using active property context.")
                                            
                                            # Format active property data cleanly for LLM context (HIDDEN from user)
                                            demand_raw = active_prop.get('Demand_PKR', 'N/A')
                                            demand_formatted = format_pkr_currency(demand_raw)
                                            
                                            prop_context = f"""
Property Type: {active_prop.get('Property_Type', 'N/A')}
Location: {active_prop.get('Society_Area', 'N/A')}, {active_prop.get('City', 'N/A')} ({active_prop.get('Phase_Block', '')})
Price / Demand: PKR {demand_formatted}
Size: {active_prop.get('Size', 'N/A')}
BHK / Rooms: {active_prop.get('BHK', 'N/A')}
Description: {active_prop.get('Description', 'N/A')}
Amenities: {active_prop.get('Amenities', 'N/A')}
"""
                                            
                                            QNA_PROMPT = f"""Identity: Aap {str(session.get('agency_tag', 'Real Estate Agency')).replace('_', ' ').title()} ke smart, friendly aur polite consultant hain.

BACKGROUND CONTEXT (KNOWLEDGE BASE ONLY - DO NOT DUMP THIS TO THE USER):
{prop_context}

USER'S MESSAGE: "{msg_body}"

STRICT RULES FOR YOUR RESPONSE:
1. SMALL TALK / GREETINGS: Agar user ka message sirf "hello", "hi", "ji", "kia hua", ya koi casual filler hai, toh bilkul natural aur short jawab dein. DO NOT repeat the property details. (Example: "Ji janab, main yahan hoon. Boliye, is property ke hawale se aap kya janna chahte hain?"). 
2. ANSWER DIRECTLY: Agar user property ka koi detail (price, rooms, parking, kitchen) pooche, toh sirf us akele sawal ka short aur direct jawab dein BACKGROUND CONTEXT se.
3. ZERO HALLUCINATIONS: Agar koi baat BACKGROUND CONTEXT mein nahi hai, toh politely maazrat karein aur kahein: "Janab, is detail ke liye main agent se baat karwa deta hoon, barah-e-karam apna Name aur Email share kardein."
4. GENDER-NEUTRAL: Always use 'Janab' or 'Aap'. NEVER use 'Sir' or 'Bhai'.
5. TONE: 100% Conversational and natural Roman Urdu. Like a helpful human, not a robot.
6. OFF-TOPIC RECOVERY: Agar user koi aisi baat kare jo property se related nahi hai, toh politely uski baat ka short jawab dein aur aakhir mein add karein: "Waise janab, jo property maine aapko abhi dikhayi hai, kya aap uska visit schedule karna chahenge?"
"""
                                            # Build System Prompt
                                            system_msg = {"role": "system", "content": QNA_PROMPT}
                                            
                                            # Combine System Prompt + Chat History
                                            llm_messages = [system_msg] + session.get("chat_history", [])
                                            
                                            # Call LLM with full context
                                            completion = robust_chat_completion(llm_messages, 0.3, 200)
                                            ai_response = completion.choices[0].message.content
                                            
                                            # CRITICAL FAILSAFE: Never send empty string to Meta API
                                            if not ai_response or not str(ai_response).strip():
                                                logger.warning("LLM returned empty string in Q&A state. Failsafe triggered.")
                                                ai_response = "Janab, main abhi samajh nahi saka. Barah-e-karam apna sawal dobara likh dein ya batayein main kya madad kar sakta hoon? 🏡"
                                                
                                            # Save Assistant's response to memory
                                            session["chat_history"].append({"role": "assistant", "content": ai_response})
                                                
                                            send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                            save_supabase_message(from_number, "user", msg_body, tenant_id)
                                            save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                            return PlainTextResponse(content="OK", status_code=200)

                                    # --- STATE 2: INITIAL SEARCH / QUALIFICATION ---
                                    is_plot = session.get("property_type") == "plot"
                                    has_bhk = True if is_plot else bool(session.get("bhk"))
                                    
                                    if session.get("state") != "INSPECTING_PROPERTY" and session.get("purpose") and session.get("property_type") and session.get("location") and has_bhk and session.get("budget"):
                                        logger.info("All 5 funnel parameters satisfied. Executing database query.")
                                        
                                        results = query_property_database(
                                            listing_type=session["purpose"],
                                            bhk=session["bhk"],
                                            city_society=session["location"],
                                            budget=session["budget"],
                                            tenant_id=tenant_id,
                                            booking_sheet_name=booking_sheet_name,
                                            property_sheet_name=property_sheet_name,
                                            agency_tag=session.get("agency_tag"),
                                            property_type=session.get("property_type")
                                        )
                                        
                                        # ═══════════════════════════════════════════════════════════════
                                        # 🏠 PROPERTY DISPATCH WITH PAGINATION (SEEN TRACKING)
                                        # ═══════════════════════════════════════════════════════════════
                                        if results and len(results) > 0:
                                            seen_props = session.get("seen_properties", [])
                                            
                                            # Filter out properties the user has already seen in this session
                                            available_props = [p for p in results if str(p.get("Property_ID", p.get("Demand_PKR", id(p)))) not in seen_props]
                                            logger.info(f"Pagination: {len(results)} total results, {len(seen_props)} seen, {len(available_props)} available to show.")
                                            
                                            if available_props:
                                                # Take the NEXT unseen property
                                                active_prop = available_props[0]
                                                
                                                # Mark this property as seen
                                                prop_id_key = str(active_prop.get("Property_ID", active_prop.get("Demand_PKR", id(active_prop))))
                                                seen_props.append(prop_id_key)
                                                session["seen_properties"] = seen_props
                                                
                                                intro_text = "Janab, yeh rahi aapki match karti hui property details! 🏡✨"
                                                send_whatsapp_text(tenant_id, from_number, intro_text, whatsapp_token)
                                                full_ai_text = intro_text + "\n\n"
                                                
                                                # Flexible helper to get key regardless of casing
                                                prop = active_prop
                                                def get_val(key_name, default="N/A"):
                                                    for k, v in prop.items():
                                                        if str(k).strip().lower() == key_name.lower() and v:
                                                            return v
                                                    return default

                                                ptype = str(get_val("Property_Type", "House")).title()
                                                city = str(get_val("City", "")).title()
                                                society = str(get_val("Society_Area", "")).title()
                                                phase = str(get_val("Phase_Block", ""))
                                                size = str(get_val("Size", ""))
                                                bhk_val = get_val("BHK", "N/A")
                                                demand_raw = get_val("Demand_PKR", "N/A")
                                                demand_formatted = format_pkr_currency(demand_raw)

                                                location_str = f"{society}, {city}" if society else city
                                                if phase and phase != "N/A":
                                                    location_str += f" ({phase})"

                                                prop_msg = f"📍 *{ptype} - {location_str}*\n"
                                                prop_msg += f"▫️ *Size:* {size}\n"
                                                prop_msg += f"▫️ *BHK / Rooms:* {bhk_val}\n"
                                                prop_msg += f"▫️ *Demand:* PKR {demand_formatted}\n"
                                                
                                                remaining_count = len(available_props) - 1
                                                if remaining_count > 0:
                                                    prop_msg += f"\n_({remaining_count} mazeed option{'s' if remaining_count > 1 else ''} available)_\n"
                                                
                                                full_ai_text += prop_msg
                                                
                                                # Send the property summary FIRST as text
                                                send_whatsapp_text(tenant_id, from_number, prop_msg, whatsapp_token)
                                                
                                                # Step 2: Pre-Media Notification Text
                                                send_whatsapp_text(tenant_id, from_number, "Janab, main is property ki kuch tasaveer bhej raha hoon, barah-e-karam thora intezar farmayen... 📸", whatsapp_token)
                                                
                                                # Step 3: Dispatch Multi-Media Sequence (All Images + Video)
                                                send_property_media_sequence(from_number, prop, tenant_id, whatsapp_token)
                                                
                                                # Add delay to allow Meta servers to process and deliver media before sending lightweight buttons
                                                time.sleep(4)
                                                
                                                # Step 4: Interactive Action Buttons
                                                outro_msg = "Kya aap is property ka visit schedule karna chahte hain? 🤝"
                                                send_whatsapp_quick_reply_buttons(from_number, outro_msg, tenant_id, whatsapp_token)
                                                full_ai_text += outro_msg
                                                ai_response = full_ai_text
                                                
                                                # Save active context
                                                session["active_property"] = active_prop
                                                session["state"] = "INSPECTING_PROPERTY"
                                                logger.info(f"Property dispatched (seen: {len(seen_props)}). State transitioned to INSPECTING_PROPERTY.")
                                                
                                                save_supabase_message(from_number, "user", msg_body, tenant_id)
                                                save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                                return PlainTextResponse(content="OK", status_code=200)
                                            else:
                                                # All matching inventory has been shown
                                                session["state"] = None
                                                session["seen_properties"] = []  # Reset for next search cycle
                                                ai_response = f"Janab, is criteria ke mutabiq hamari tamam inventory aapko dikhayi ja chuki hai. Kya main aapka budget ya area thora change karun taake mazeed options mil sakein? 🔄"
                                        else:
                                            formatted_budget = format_pkr_currency(session.get('budget', ''))
                                            ai_response = f"Janab, filhal PKR {formatted_budget} ke budget mein {session.get('location', '')} mein hamari inventory sold out hai. 🏢"
                                            
                                    else:
                                        # Reset any accidental default
                                        if not session.get("purpose") or session.get("purpose") not in ["buy", "rent"]:
                                            session["purpose"] = None

                                        # ═══════════════════════════════════════════════════════════════
                                        # 🧠 DYNAMIC LLM-POWERED QUALIFICATION (INTENT-SHIFT AWARE)
                                        # ═══════════════════════════════════════════════════════════════
                                        if session.get("state") != "INSPECTING_PROPERTY":
                                            logger.info(f"Funnel Incomplete. Using Dynamic LLM Qualification Prompt.")
                                            
                                            raw_agency_tag = session.get("agency_tag") or tenant_config.get("agency_tag", "Real Estate Agency")
                                            agency_name = str(raw_agency_tag).replace("_", " ").title()
                                            
                                            # Fetch dynamic profile
                                            prop_sheet = tenant_config.get("property_sheet_name")
                                            agency_profile = get_agency_profile(raw_agency_tag, tenant_id, prop_sheet)
                                            
                                            # Prepare real-time session snapshot for the AI
                                            current_state = f"""
        - Purpose (Buy/Rent): {session.get('purpose', 'Not specified')}
        - Property Type: {session.get('property_type', 'Not specified')}
        - Location: {session.get('location', 'Not specified')}
        - BHK / Rooms: {session.get('bhk', 'Not specified')}
        - Budget: {format_currency(session['budget']) if session.get('budget') else 'Not specified'}
                                            """

                                            DYNAMIC_PROMPT = f"""Identity: Aap {agency_name} ke smart, empathetic aur highly professional consultant hain.

User's Current Message: "{msg_body}"
Current Extracted Criteria:
{current_state}

CORE TRAINING FOR CONVERSATION & INTENT SHIFTS:
1. ADAPTABILITY (NO HARDCODING): If the user changes their mind mid-conversation (e.g., switches from Plot to House, Rent to Buy, or mentions a completely new requirement like "Bangla", "Flat", "Kothi"), GRACEFULLY ACCEPT the new context. Acknowledge their choice naturally.
2. ACTIVE CHAT RULE (STRICT): If the user changes their requirement (Intent Shift) during an ongoing conversation, DO NOT greet them again (NO 'Assalam-o-Alaikum', NO 'Welcome back').
3. SMOOTH TRANSITION (STRICT): Acknowledge the change instantly and casually in Roman Urdu. (e.g., 'Koi masla nahi janab, hum flat ke bajaye plot dekh lete hain. Barah-e-karam batayein plot kis shehar mein chahiye?')
4. NO OVER-EXPLAINING (STRICT): Do not say 'Mujhe yaad hai aap pehle X dhoond rahe the'. Just smoothly transition to asking the missing parameters (Location, Budget, etc.) for the new property type.
5. IDENTIFY MISSING GAPS: Look at the 'Current Extracted Criteria'. Your only goal is to dynamically figure out what is STILL MISSING (out of Purpose, Property Type, Location, Size/BHK, and Budget) and politely ask the user for the NEXT MISSING one. Ask for ONE thing at a time.
6. CONVERSATIONAL FLOW: Do NOT repeat introductory greetings (like "Walaikum Assalam") if they just clicked an option button or are continuing a chat. Jump straight to the next question.
7. GENDER-NEUTRAL STRICTNESS: Always use 'Janab' or 'Aap'. Strictly NEVER use 'Sir', 'Madam', or 'Bhai'.
8. LANGUAGE: 100% Natural Roman Urdu. Be conversational, not a robot. Keep it short — ONE question, ONE sentence, ONE emoji.
9. BHK SKIP FOR PLOTS: If the property type is 'plot', do NOT ask for BHK/rooms — skip directly to budget.
10. BUDGET PHRASING: If purpose is 'rent', ask for "monthly rent budget". If purpose is 'buy', ask for "total purchase budget".
11. CRITICAL: NEVER return an empty response. Always guide the user to the next step.

STRICT ANTI-HALLUCINATION RULES:
12. ZERO ASSUMPTIONS: NEVER assume, guess, or make up missing parameters (like BHK, Property Type, Budget, or Location). If the user hasn't explicitly mentioned a parameter in their CURRENT request, you must keep it as `null` in the JSON.
13. STEP-BY-STEP QUESTIONING: If a parameter is `null`, ask for it explicitly. Do not combine missing parameters with assumed values. (e.g., If property type is missing, say 'Aap kis type ki property dekh rahe hain? Flat, Plot, ya Ghar?')
14. CLEAN SLATE ON NEW INTENT: When the user clicks a new interactive button (like 'Kharidni Hai'), their active search parameters (bhk, location, budget, property_type) must be treated as completely blank/null unless they explicitly type them again.
15. INTENT SHIFT WIPE: CRITICAL: If the user changes their primary property requirement mid-chat (e.g., switching from Plot to Flat, or from Buy to Rent), you MUST IMMEDIATELY SET the previous `budget` and `bhk` to `null`. DO NOT carry over old budgets to a new property type. Treat it as a fresh search and gracefully ask the user for their new budget and requirements for this newly requested property.
16. AGENT ESCALATION: CRITICAL: If the user explicitly asks to speak to an agent, a human, asks for a phone call, or seems highly frustrated (e.g., 'agent', 'call', 'insaan', 'baat karni hai'), YOU MUST IMMEDIATELY STOP ASKING ABOUT PROPERTIES. Respond EXACTLY with this empathetic Roman Urdu message: 'Janab, main ne aap ki request apne senior agent ko forward kar di hai. Wo thori dair mein aap se direct rabta kar lenge. Tab tak, barah-e-karam apna sawal ya masla yahan likh dein taake main unhe update kar sakun. 📞🤝' Set all property parameters to null and do not attempt to sell or search for properties in this specific response.
17. FULL CATALOG / PDF REQUESTS: If the user asks for a complete list, catalog, or PDF of all properties (e.g., "saari properties", "list bhej do", "pdf"), DO NOT attempt to list multiple properties. Respond EXACTLY with: 'Janab, hamare paas inventory rozana update hoti rehti hai. Aap bas apni pasandida location aur budget batayein, main best options yahan screen par dikha deta hoon! 😊'
18. SPAM / GIBBERISH / TYPOS: If the user sends a random string of characters (like "asdfgh"), only emojis, or an empty message, DO NOT try to parse it. Respond politely with: "Janab, main aapki baat samajh nahi paya, barah-e-karam property ke hawale se apna sawal poochein."
19. DOUBLE INTENT (MIXED REQUESTS): If the user asks to do two things at once (e.g., "Mujhe plot lena hai aur flat bechna hai"), ALWAYS prioritize the FIRST task mentioned. Acknowledge both but steer the conversation to solve the first one first. (e.g., "Zaroor! Pehle hum aapke naye plot ki details save kar lete hain, phir flat ki baat karenge. Plot ke liye aapka budget kya hai?")
20. OUT OF SYLLABUS (OFF-TOPIC): If the user asks questions unrelated to real estate, properties, or our agency (e.g., weather, politics, general AI questions), STRICTLY REFUSE TO ANSWER. Respond politely: "Janab, main ek Real Estate Assistant hoon. Main sirf properties aur real estate ke hawale se aapki rehnumai kar sakta hoon. Batayein, aap kis type ki property dekh rahe hain?"
"""
                                            if agency_profile:
                                                address = agency_profile.get("Address", "N/A")
                                                phone = agency_profile.get("Phone", "N/A")
                                                email = agency_profile.get("Email", "N/A")
                                                about_us = agency_profile.get("About_Us", "N/A")
                                                DYNAMIC_PROMPT += f"\nCRITICAL CONTEXT: You are currently representing the agency '{agency_name}'. If the user asks about our office, owner, or contact info, use ONLY these details: Address: {address}, Phone: {phone}, Email: {email}. About us: {about_us}."

                                            completion = robust_chat_completion([{"role": "system", "content": DYNAMIC_PROMPT}], 0.3, 150)
                                            ai_response = completion.choices[0].message.content
                                            
                                            # Intercept Urgent Escalation
                                            if ai_response and "senior agent ko forward" in ai_response.lower():
                                                try:
                                                    booking_sheet = tenant_config.get("booking_sheet_name")
                                                    prop_sheet = tenant_config.get("property_sheet_name")
                                                    urgent_workspace = GoogleSpreadsheetClient(tenant_id, booking_sheet, prop_sheet)
                                                    urgent_workspace.append_urgent_lead(phone=from_number, user_message=msg_body)
                                                    
                                                    # Clear state
                                                    session["state"] = None
                                                    session["purpose"] = None
                                                    session["property_type"] = None
                                                    session["location"] = None
                                                    session["budget"] = None
                                                    session["bhk"] = None
                                                    logger.info(f"Escalation triggered for {from_number}. Saved to Urgent_Leads.")
                                                except Exception as e:
                                                    logger.error(f"Failed to route urgent lead: {e}")
                                            
                                            # CRITICAL FAILSAFE: Never send empty string to Meta API
                                            if not ai_response or not str(ai_response).strip():
                                                logger.warning("LLM returned empty string during qualification. Failsafe triggered to prevent Meta API Crash.")
                                                ai_response = "Maazrat janab, main aapki nayi requirement process kar raha hoon. Barah-e-karam batayein, aap is property ke liye kis shehar ya area ko prefer karenge? 📍"
                                            
                                            send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                            save_supabase_message(from_number, "user", msg_body, tenant_id)
                                            save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                            session["chat_history"].append({"role": "assistant", "content": ai_response})
                                            return PlainTextResponse(content="OK", status_code=200)

                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    if ai_response and "I am scanning our off-market registries" not in ai_response and "processing your luxury portfolio request" not in ai_response:
                                        save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                        
                                    # CRITICAL FAILSAFE: Never send empty string to Meta API
                                    if not ai_response or not str(ai_response).strip():
                                        logger.warning("LLM returned empty string at final dispatch. Failsafe triggered to prevent Meta API Crash.")
                                        ai_response = "Maazrat janab, main aapki nayi requirement process kar raha hoon. Barah-e-karam batayein, aap is property ke liye kis shehar ya area ko prefer karenge? 📍"
                                    
                                    if whatsapp_token and ai_response: 
                                        send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    
        return PlainTextResponse(content="OK", status_code=200)
    except Exception as e:
        print(f"🚨 [BACKGROUND TASK ERROR] {str(e)}", flush=True)
        logger.exception(f"🚨 Webhook Parse Crash: {str(e)}")
        return PlainTextResponse(content="OK", status_code=200)
    finally:
        for phone, session_data, tenant in active_sessions:
            try:
                if session_data is not None:
                    update_user_session(phone, session_data, tenant)
            except Exception as e:
                logger.error(f"🚨 Failed to save session in finally block: {str(e)}")