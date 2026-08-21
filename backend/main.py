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
AGENCY_KEYWORDS = ["al-madina", "dha-estates", "qorvx"]
PK_LOCATION_KEYWORDS = ["karachi", "lahore", "islamabad", "dha", "bahria", "clifton", "gulshan", "rawalpindi", "peshawar", "multan"]
USER_SESSIONS = {}      # key: "tenant_id:phone" -> {bhk, budget, location, purpose, market, language, agency_tag}
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
MODEL_ID = "openai/gpt-oss-20b"
FALLBACK_MODEL = "openai/gpt-oss-20b"

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

def query_property_database(listing_type: str, bhk: int, city_society: str, budget: int, tenant_id: str, booking_sheet_name: str, property_sheet_name: str, agency_tag: str = None):
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
    import time
    
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

    # 3. Dispatch Images
    for idx, img_url in enumerate(image_urls):
        caption = f"Photo {idx+1}/{len(image_urls)}" if idx > 0 else f"📸 Main View: {prop.get('Society_Area', '')}"
        send_whatsapp_media(tenant_id, to_number, img_url, "image", access_token, caption=caption)
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
    Sends media to WhatsApp. If Meta rejects the media (e.g., file too large),
    it gracefully falls back to sending the URL as a text message.
    """
    url = f"https://graph.facebook.com/v25.0/{tenant_id}/messages"
    headers = {"Authorization": f"Bearer {whatsapp_token}", "Content-Type": "application/json"}
    media_payload = {"link": media_url}
    if caption:
        media_payload["caption"] = caption
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_number, "type": media_type, media_type: media_payload}
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        logger.info(f"🎬 [META MEDIA RESPONSE] Status: {res.status_code} | Body: {res.text}")
        
        try:
            response_data = res.json()
        except Exception:
            response_data = {}
            
        # Check if Meta API threw an error (HTTP 400+)
        if res.status_code != 200 or "error" in response_data:
            logger.error(f"Meta Media Upload Failed ({media_type}): {response_data.get('error', response_data)}")
            
            # --- SMART FALLBACK: Send as Text Link ---
            cap_text = caption if caption else "Media File"
            fallback_text = f"📎 *{cap_text}*\n\nJanab, yeh file size mein bari hone ki wajah se WhatsApp par direct load nahi ho saki. Barah-e-karam is link par click karke direct dekh lein:\n👉 {media_url}"
            
            logger.info("Executing Text Fallback for oversized media.")
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

def get_session(phone: str, tenant_id: str) -> dict:
    """Get or create a session for this user."""
    key = f"{tenant_id}:{phone}"
    if key not in USER_SESSIONS:
        USER_SESSIONS[key] = {
            "purpose": None, "bhk": None, "location": None, "budget": None, "agency_tag": None
        }
    return USER_SESSIONS[key]

def extract_and_update_session(msg_body: str, phone: str, chat_history: list, tenant_id: str, tenant_config: dict) -> dict:
    """Updates global session with extracted variables."""
    session_id = f"{tenant_id}:{phone}"
    if session_id not in USER_SESSIONS:
        USER_SESSIONS[session_id] = {}
    session = USER_SESSIONS[session_id]
    
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

                                db_history = get_supabase_history(from_number, tenant_id)
                                last_ai_msg = db_history[-1]["content"] if db_history else ""
                                
                                # --- INTENT SHIFT & SEARCH RESET DETECTOR ---
                                msg_lower = msg_body.lower()
                                property_type_keywords = ["plot", "house", "makan", "flat", "apartment", "bangla"]
                                action_keywords = ["kharidna", "buy", "rent", "bechna", "chaihe", "chahiye"]
                                
                                _sess_check = get_session(from_number, tenant_id)
                                if any(pt in msg_lower for pt in property_type_keywords) and any(ak in msg_lower for ak in action_keywords):
                                    logger.info("New search intent detected. Clearing previous session parameters.")
                                    _sess_check["state"] = None
                                    _sess_check["active_property"] = None
                                    _sess_check["bhk"] = None
                                    _sess_check["budget"] = None
                                    _sess_check["purpose"] = "buy" if any(b in msg_lower for b in ["buy", "kharidna", "plot"]) else "rent"
                                    
                                    # Extract property type
                                    for pt in ["plot", "house", "flat", "apartment"]:
                                        if pt in msg_lower:
                                            _sess_check["property_type"] = pt
                                            break

                                # ═══ SESSION STATE: Extract & persist parameters ═══
                                # ── Update Core Session Context ──
                                session = extract_and_update_session(msg_body, from_number, db_history, tenant_id, tenant_config)
                                logger.info(f"🧠 [SESSION] {from_number}: bhk={session.get('bhk')} loc={session.get('location')} purpose={session.get('purpose')} budget={session.get('budget')} market={session.get('market')}")

                                # --- NEW HARD RESET & GREETING BLOCK ---
                                msg_lower = msg_body.lower()
                                is_greeting = any(word in msg_lower for word in ["salam", "assalam", "hello", "hi", "hey"])
                                is_new_inquiry = "properties dekhni hain" in msg_lower or "property dekhni hai" in msg_lower
                                
                                if is_greeting or is_new_inquiry:
                                    logger.info("New conversation trigger detected. Performing Hard Session Reset.")
                                    
                                    # Clear all previous funnel filters and states
                                    session["purpose"] = None
                                    session["property_type"] = None
                                    session["location"] = None
                                    session["bhk"] = None
                                    session["budget"] = None
                                    session["state"] = None
                                    session["active_property"] = None
                                    session["greeting_done"] = True  # Mark greeting as done for this new loop
                                    
                                    # Generate Dynamic Agency Greeting
                                    raw_agency_tag = session.get("agency_tag", "Hamari Agency")
                                    agency_name = str(raw_agency_tag).replace("_", " ").title()
                                    
                                    greeting_text = f"Walaikum Assalam! {agency_name} mein khush-amdeed. ✨\n\nJanab, aap property kharidna chahte hain ya rent par lena chahte hain? 🏡"
                                    
                                    send_whatsapp_text(tenant_id, from_number, greeting_text, whatsapp_token)
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", greeting_text, tenant_id)
                                    return PlainTextResponse(content="OK", status_code=200)

                                # Check if user clicked an interactive quick reply button
                                interactive = message.get("interactive", {})
                                if interactive.get("type") == "button_reply":
                                    button_id = interactive["button_reply"]["id"]
                                    button_title = interactive["button_reply"]["title"]
                                    logger.info(f"Interactive Button Clicked: {button_id} ({button_title})")
                                    
                                    if button_id == "btn_cheaper":
                                        # Reduce budget by 20% and clear active state
                                        current_budget = float(session.get("budget", 10000000))
                                        session["budget"] = current_budget * 0.8
                                        session["state"] = None  # Exit inspecting state
                                        session["active_property"] = None
                                        
                                        # Format new budget to show user
                                        new_budget_str = format_pkr_currency(session["budget"])
                                        reply_text = f"Ji Janab, main ne aapka budget thora kam set kar diya hai (Approx. PKR {new_budget_str}). Hum is naye budget mein options dhoondhte hain. Kya main search shuru karun? 🔍"
                                        send_whatsapp_text(tenant_id, from_number, reply_text, whatsapp_token)
                                        save_supabase_message(from_number, "user", f"Clicked Sasti Option", tenant_id)
                                        save_supabase_message(from_number, "assistant", reply_text, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif button_id == "btn_next":
                                        # Clear active state and ask for criteria adjustment
                                        session["state"] = None
                                        session["active_property"] = None
                                        reply_text = "Koi masla nahi janab! 🔄 Aap mazeed options ke liye apni requirement (jaise location, rooms ya budget) mein kya tabdeeli karna chahenge?"
                                        send_whatsapp_text(tenant_id, from_number, reply_text, whatsapp_token)
                                        save_supabase_message(from_number, "user", f"Clicked Koi Aur Option", tenant_id)
                                        save_supabase_message(from_number, "assistant", reply_text, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

                                    elif button_id == "btn_visit":
                                        # Transition to Lead Collection State
                                        session["state"] = "BOOKING_VISIT"
                                        reply_text = "Zabardast janab! 🤝 Is property ka visit schedule karne ke liye barah-e-karam apna *Poora Naam* aur *Email* share kardein taake hamara agent aapse rabta kare."
                                        send_whatsapp_text(tenant_id, from_number, reply_text, whatsapp_token)
                                        save_supabase_message(from_number, "user", f"Clicked Visit Schedule", tenant_id)
                                        save_supabase_message(from_number, "assistant", reply_text, tenant_id)
                                        return PlainTextResponse(content="OK", status_code=200)

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
                                    ai_response = "Understood. We continuously update our off-market assets. State any new parameters whenever you are ready. 🏛️"
                                    save_supabase_message(from_number, "user", msg_body, tenant_id)
                                    save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                    send_whatsapp_text(tenant_id, from_number, ai_response, whatsapp_token)
                                    return PlainTextResponse(content="OK", status_code=200)

                                elif msg_body == "Lower Budget 💰":
                                    session["state"] = None
                                    session["budget"] = None
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
"""
                                            # Call LLM for direct answer
                                            completion = robust_chat_completion([{"role": "system", "content": QNA_PROMPT}], 0.3, 200)
                                            ai_response = completion.choices[0].message.content
                                            if ai_response is None:
                                                ai_response = "Janab, main abhi samajh nahi saka."
                                                
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
                                            agency_tag=session.get("agency_tag")
                                        )
                                        
                                        # HARD-WIRE THE RESPONSE:
                                        if results and len(results) > 0:
                                            intro_text = "Sir, yeh rahi aapki match karti hui property details! 🏡✨"
                                            send_whatsapp_text(tenant_id, from_number, intro_text, whatsapp_token)
                                            full_ai_text = intro_text + "\n\n"
                                            
                                            for idx, prop in enumerate(results):
                                                # Flexible helper to get key regardless of casing
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
                                                img_url = get_val("Main_Image", "")

                                                location_str = f"{society}, {city}" if society else city
                                                if phase and phase != "N/A":
                                                    location_str += f" ({phase})"

                                                prop_msg = f"📍 *{idx+1}. {ptype} - {location_str}*\n"
                                                prop_msg += f"▫️ *Size:* {size}\n"
                                                prop_msg += f"▫️ *BHK / Rooms:* {bhk_val}\n"
                                                prop_msg += f"▫️ *Demand:* PKR {demand_formatted}\n"
                                                
                                                full_ai_text += prop_msg
                                                
                                                # Send the property summary FIRST as text
                                                send_whatsapp_text(tenant_id, from_number, prop_msg, whatsapp_token)
                                                
                                                # Dispatch Multi-Media Sequence (All Images + Video)
                                                send_property_media_sequence(from_number, prop, tenant_id, whatsapp_token)
                                                
                                                full_ai_text += "\n"
                                            
                                            outro_msg = "Kya aap in properties ka visit schedule karna chahte hain? 🤝"
                                            send_whatsapp_quick_reply_buttons(from_number, outro_msg, tenant_id, whatsapp_token)
                                            full_ai_text += outro_msg
                                            ai_response = full_ai_text
                                            
                                            # Save active context
                                            session["active_property"] = results[0]
                                            session["state"] = "INSPECTING_PROPERTY"
                                            logger.info("Property dispatched. State transitioned to INSPECTING_PROPERTY.")
                                            
                                            save_supabase_message(from_number, "user", msg_body, tenant_id)
                                            save_supabase_message(from_number, "assistant", ai_response, tenant_id)
                                            return PlainTextResponse(content="OK", status_code=200)
                                        else:
                                            formatted_budget = format_pkr_currency(session.get('budget', ''))
                                            ai_response = f"Janab, filhal PKR {formatted_budget} ke budget mein {session.get('location', '')} mein hamari inventory sold out hai. 🏢"
                                            
                                    else:
                                        # Reset any accidental default
                                        if not session.get("purpose") or session.get("purpose") not in ["buy", "rent"]:
                                            session["purpose"] = None

                                        # Determine missing parameter in STRICT sequential priority
                                        missing_param_prompt = None
                                        
                                        # STEP 1: PURPOSE (Buy vs Rent)
                                        if not session.get("purpose"):
                                            missing_param_prompt = "Janab, aap property kharidna chahte hain ya rent par lena chahte hain? 🏡"
                                            
                                        # STEP 2: PROPERTY TYPE (House vs Flat vs Plot)
                                        elif not session.get("property_type"):
                                            missing_param_prompt = "Aapko kis type ki property chahiye? (House, Flat ya Plot)? 🏢"
                                            
                                        # STEP 3: LOCATION (City / Society)
                                        elif not session.get("location"):
                                            missing_param_prompt = "Aap kis shehar ya specific society mein property dekhna chahte hain? 📍"
                                            
                                        # STEP 4: BHK / ROOMS (Only required for House/Flat, SKIP for Plot)
                                        elif session.get("property_type") in ["house", "flat", "apartment"] and not session.get("bhk"):
                                            missing_param_prompt = "Aapko kitne rooms ya BHK ki requirement hai? 🛏️"
                                            
                                        # STEP 5: BUDGET (Dynamic phrasing based on Purpose)
                                        elif not session.get("budget"):
                                            if session.get("purpose") == "rent":
                                                missing_param_prompt = "Aapka approximate monthly rent ka budget kitna hai? 💰"
                                            else:
                                                missing_param_prompt = "Aapka approximate total purchase budget kitna hai? 💰"

                                        # If qualification is still incomplete and we are not in Q&A state, prompt the user
                                        if missing_param_prompt and session.get("state") != "INSPECTING_PROPERTY":
                                            logger.info(f"Funnel Incomplete. Dispatching sequential prompt.")
                                            # Just send the prompt directly, because the initial greeting is handled above
                                            send_whatsapp_text(tenant_id, from_number, missing_param_prompt, whatsapp_token)
                                            save_supabase_message(from_number, "user", msg_body, tenant_id)
                                            save_supabase_message(from_number, "assistant", missing_param_prompt, tenant_id)
                                            return PlainTextResponse(content="OK", status_code=200)

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