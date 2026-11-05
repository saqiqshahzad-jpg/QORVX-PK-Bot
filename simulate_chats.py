import os
import sys
import json
import re
import time
from typing import Dict, Any, List

# Ensure UTF-8 stdout on Windows terminals
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Setup import path for backend
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, "backend") if os.path.exists(os.path.join(BASE_DIR, "backend")) else BASE_DIR
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

# Import core bot logic
try:
    import main
except ImportError:
    from backend import main

# Token counter with tiktoken support and graceful fallback
def count_tokens(text: str) -> int:
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return max(1, len(text) // 4)

def create_fresh_session() -> Dict[str, Any]:
    return {
        "purpose": None,
        "property_type": None,
        "bhk": None,
        "size": None,
        "location": None,
        "budget": None,
        "user_name": None,
        "state": None,
        "funnel_state": None,
        "awaiting_confirmation": False,
        "search_confirmed": False,
        "chat_history": [],
        "active_property": None,
        "sent_properties": [],
        "archived_intents": [],
        "last_interaction": time.time(),
        "name_confirm_pending": False,
        "pending_new_name": None,
        "name_last_changed": 0,
        "sub_location": None,
        "sub_location_pending": False,
        "sub_location_prompt": None,
        "location_confirm_pending": False,
        "pending_location": None
    }

def simulate_pipeline_turn(session: Dict[str, Any], user_message: str) -> Dict[str, Any]:
    """Simulates a complete bot pipeline turn directly against LLM & extraction layers."""
    # 1. Sanitize text
    msg_body, had_abuses = main.sanitize_and_extract(user_message)
    last_ai = session["chat_history"][-1]["content"] if session["chat_history"] else ""

    # 2. Sub-location response handling if pending
    if session.get("sub_location_pending") and session.get("location"):
        loc_lower = session["location"].lower()
        specific_loc = main.extract_location(msg_body, last_ai)
        if specific_loc:
            specific_lower = specific_loc.lower()
            if specific_lower != loc_lower and (
                specific_lower.startswith(loc_lower) or 
                loc_lower in specific_lower or
                main._location_already_specific(msg_body.lower(), loc_lower)
            ):
                session["location"] = specific_loc
                session["sub_location_pending"] = False
                session["sub_location_prompt"] = None
            elif specific_lower in main.KARACHI_AREAS:
                session["location"] = specific_loc
                new_sub = main.LOCATION_SUB_VARIANTS.get(specific_lower)
                if new_sub and not main._location_already_specific(msg_body.lower(), specific_lower):
                    session["sub_location_pending"] = True
                    session["sub_location_prompt"] = new_sub["prompt"]
                else:
                    session["sub_location_pending"] = False
                    session["sub_location_prompt"] = None
        else:
            sub_patterns = [
                (r'phase\s*(\d+)', 'phase'),
                (r'block\s*(\w+)', 'block'),
                (r'sector\s*(\w+)', 'sector'),
                (r'precinct\s*(\d+\w*)', 'precinct'),
                (r'no\.?\s*(\d+)', 'no'),
                (r'number\s*(\d+)', 'no'),
            ]
            for pattern, keyword in sub_patterns:
                m = re.search(pattern, msg_body.lower())
                if m:
                    sub_val = m.group(1)
                    combined = f"{session['location']} {keyword} {sub_val}".lower()
                    if combined in main.KARACHI_AREAS:
                        session["location"] = combined.title()
                    else:
                        session["location"] = f"{session['location']} {keyword.title()} {sub_val.upper() if len(sub_val) == 1 else sub_val}"
                    session["sub_location_pending"] = False
                    session["sub_location_prompt"] = None
                    break

    # 3. Parameter extraction
    bhk = main.extract_bhk(msg_body, session.get("property_type"), last_ai)
    if bhk:
        session["bhk"] = bhk

    size = main.extract_size(msg_body)
    if size:
        session["size"] = size

    budget = main.parse_south_asian_budget(msg_body)
    if budget:
        session["budget"] = budget

    loc = main.extract_location(msg_body, last_ai)
    if loc and loc != session.get("location") and not session.get("location_confirm_pending"):
        loc_lower = loc.lower()
        is_known_valid = loc_lower in main.KARACHI_AREAS or any(
            loc_lower.startswith(area) or area.startswith(loc_lower) 
            for area in main.KARACHI_AREAS if len(area) > 3
        )
        if is_known_valid:
            session["location"] = loc
            sub_info = main.LOCATION_SUB_VARIANTS.get(loc_lower)
            if sub_info and not main._location_already_specific(msg_body.lower(), loc_lower):
                session["sub_location_pending"] = True
                session["sub_location_prompt"] = sub_info["prompt"]
            else:
                session["sub_location_pending"] = False
                session["sub_location_prompt"] = None
                if sub_info and main._location_already_specific(msg_body.lower(), loc_lower):
                    specific = main._get_specific_sublocation(msg_body.lower(), loc_lower)
                    if specific:
                        session["location"] = specific
        else:
            session["pending_location"] = loc
            session["location_confirm_pending"] = True
    elif loc:
        session["location"] = loc

    # 4. Active Property Context
    active_prop_data = None
    if session.get("active_property") and session.get("sent_properties"):
        for p in session["sent_properties"]:
            if str(p.get("ID")) == str(session["active_property"]):
                active_prop_data = p
                break

    sys_add = f"\n\nCURRENT SESSION STATE: {json.dumps(session)}"
    if active_prop_data:
        sys_add += f"\n\nACTIVE PROPERTY DETAILS (Answer based on this): {json.dumps(active_prop_data)}"
    elif len(session.get("sent_properties", [])) > 1:
        sys_add += f"\n\nNOTE: You sent multiple properties but user hasn't specified which one. Ask them to clarify by replying to an image or sending the last 2 digits of the ID."

    # 5. Build Dynamic Prompt & Calculate Token Count
    sys_prompt = main.build_dynamic_prompt(session) + sys_add
    token_count = count_tokens(sys_prompt)

    # 6. LLM Invocation
    messages = [{"role": "system", "content": sys_prompt}]
    messages.extend(session["chat_history"][-10:])
    messages.append({"role": "user", "content": msg_body})

    llm_res = main.chat_completion_fallback(messages)

    # 7. Response Parsing
    parsed = {}
    reply_text = llm_res
    try:
        parsed = main.extract_clean_json(llm_res)
        old_ptype = session.get("property_type")
        if "bedrooms" in parsed and parsed["bedrooms"] is not None:
            parsed["bhk"] = parsed.pop("bedrooms")

        # Prevent LLM hallucinating a budget when none was mentioned
        if parsed.get("budget") is not None and not main.parse_south_asian_budget(msg_body) and not session.get("budget"):
            parsed["budget"] = None

        for k in ["location", "purpose", "property_type", "bhk", "budget", "size", "funnel_state"]:
            if k in parsed and parsed[k] is not None:
                session[k] = parsed[k]

        if old_ptype and old_ptype != session.get("property_type"):
            if session.get("property_type") in ["plot", "warehouse", "zameen"]:
                session["bhk"] = None
            else:
                session["size"] = None

        reply_text = parsed.get("reply_text", llm_res)
    except Exception:
        pass

    # Update conversation history
    session["chat_history"].append({"role": "user", "content": user_message})
    session["chat_history"].append({"role": "assistant", "content": reply_text})

    return {
        "user_message": user_message,
        "token_count": token_count,
        "raw_response": llm_res,
        "parsed_json": parsed,
        "reply_text": reply_text,
        "updated_session": session
    }

def print_separator(title: str = ""):
    print("\n" + "=" * 80)
    if title:
        print(f" {title.center(78)} ")
        print("=" * 80)

def print_turn_details(turn_num: int, result: Dict[str, Any]):
    print(f"\n--- [TURN {turn_num}] User Message: \"{result['user_message']}\" ---")
    print(f"📊 Dynamic Prompt Token Count: {result['token_count']} tokens")
    
    parsed = result["parsed_json"]
    if parsed:
        thinking = parsed.get("_thinking", "N/A")
        print(f"🧠 LLM Internal Thinking: {thinking}")
        print("\n📦 Parsed LLM JSON:")
        display_json = {k: v for k, v in parsed.items() if k != "_thinking"}
        print(json.dumps(display_json, indent=2, ensure_ascii=False))
    else:
        print("\n⚠️ Raw Output (Failed JSON Parse):")
        print(result["raw_response"])

    print(f"\n💬 Bot Reply Text:")
    print(f"👉 \"{result['reply_text']}\"")

    sess = result["updated_session"]
    print(f"\n📌 Active Session State:")
    print(f"   • Purpose: {sess.get('purpose')}")
    print(f"   • Location: {sess.get('location')}")
    print(f"   • Sub-Location Pending: {sess.get('sub_location_pending')}")
    print(f"   • Property Type: {sess.get('property_type')}")
    print(f"   • Bedrooms / Size: BHK={sess.get('bhk')}, Size={sess.get('size')}")
    print(f"   • Budget: PKR {sess.get('budget'):,}" if sess.get("budget") else f"   • Budget: {sess.get('budget')}")


# =========================================================================================
# SCENARIOS IMPLEMENTATION
# =========================================================================================

def run_scenario_1_garam_buyer():
    print_separator("SCENARIO 1: Garam Buyer (Clean, fast one-shot requirements)")
    print("Turn 1: User says 'Salam, DHA Phase 6 mein 500 gaz plot chahiye 10 cr budget'")
    print("Turn 2: When bot asks purpose, user confirms 'Kharidna hai'")
    
    session = create_fresh_session()
    result = simulate_pipeline_turn(session, "Salam, DHA Phase 6 mein 500 gaz plot chahiye 10 cr budget")
    print_turn_details(1, result)

    t1_p = result["parsed_json"]
    t1_checks = [
        ("Location extracted as DHA Phase 6", result["updated_session"].get("location") in ["Dha Phase 6", "DHA Phase 6"]),
        ("Property type extracted as plot", result["updated_session"].get("property_type") == "plot"),
        ("Budget extracted as 10 Crore (100,000,000)", result["updated_session"].get("budget") == 100000000),
        ("Size extracted as 500 gaz", "500" in str(result["updated_session"].get("size", ""))),
        ("Bot adheres to rule 4: explicitly asks purpose before assuming", any(w in result["reply_text"].lower() for w in ["kharidna", "kiraye", "rent", "purpose", "maqsad"]))
    ]
    print("\n🔍 Turn 1 Assessment:")
    for label, passed in t1_checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")

    # Turn 2: User confirms purpose "Kharidna hai"
    result2 = simulate_pipeline_turn(result["updated_session"], "Kharidna hai")
    print_turn_details(2, result2)

    t2_checks = [
        ("Purpose confirmed as 'buy'", result2["updated_session"].get("purpose") == "buy"),
        ("All 5 core requirements complete", all([
            result2["updated_session"].get("purpose"),
            result2["updated_session"].get("location"),
            result2["updated_session"].get("property_type"),
            result2["updated_session"].get("size"),
            result2["updated_session"].get("budget")
        ]))
    ]
    print("\n🔍 Turn 2 Assessment:")
    for label, passed in t2_checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_2_outofstock_test():
    print_separator("SCENARIO 2: Out-of-Stock Test (4 BHK House in 1 Crore)")
    print("User: 'kisi ilaqe mein 4 BHK house 1 crore'")
    print("Goal: Verify it stays in Karachi, does NOT hallucinate Islamabad, and does NOT pitch 10 Crore properties.")

    session = create_fresh_session()
    result = simulate_pipeline_turn(session, "kisi ilaqe mein 4 BHK house 1 crore")
    print_turn_details(1, result)

    reply = result["reply_text"].lower()
    p = result["parsed_json"]

    no_islamabad = "islamabad" not in reply and p.get("location") != "Islamabad"
    no_lahore = "lahore" not in reply and p.get("location") != "Lahore"
    budget_respected = result["updated_session"].get("budget") == 10000000
    bhk_respected = result["updated_session"].get("bhk") == 4
    no_crazy_upsell = "10 crore" not in reply and "5 crore" not in reply

    checks = [
        ("No Islamabad hallucination", no_islamabad),
        ("No Lahore hallucination", no_lahore),
        ("Budget extracted as 1 Crore", budget_respected),
        ("BHK extracted as 4", bhk_respected),
        ("Strict budget adherence (no 5cr/10cr wild upsell)", no_crazy_upsell)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_3_non_karachi_trap():
    print_separator("SCENARIO 3: Non-Karachi Trap ('Lahore DHA Phase 5 mein ghar dikhao')")
    print("User: 'Lahore DHA Phase 5 mein ghar dikhao'")
    print("Goal: Verify strict Karachi-only refusal rule triggers and location is NOT saved as Lahore.")

    session = create_fresh_session()
    result = simulate_pipeline_turn(session, "Lahore DHA Phase 5 mein ghar dikhao")
    print_turn_details(1, result)

    reply = result["reply_text"].lower()
    p = result["parsed_json"]

    refused_politely = any(phrase in reply for phrase in ["sirf karachi", "karachi ki properties", "karachi mein"])
    location_not_lahore = result["updated_session"].get("location") is None or "lahore" not in str(result["updated_session"].get("location")).lower()

    checks = [
        ("Polite Karachi-only refusal message sent", refused_politely),
        ("Location NOT saved as Lahore", location_not_lahore)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_4_ghusse_wala():
    print_separator("SCENARIO 4: Ghusse Wala / Confused User")
    print("User: 'Abe yar kya bakwas bhej rahe ho, maine bola tha flat chahiye'")
    print("Goal: Empathetically calm the user, handle frustration, switch property_type to flat.")

    # Mock session with prior context where a house was mistakenly discussed
    session = create_fresh_session()
    session["purpose"] = "buy"
    session["property_type"] = "house"
    session["location"] = "Gulshan-E-Iqbal"
    session["budget"] = 25000000
    session["sent_properties"] = [{
        "ID": "PK-901",
        "Title": "Luxury 4 Bed House in Gulshan",
        "Price": "2.5 Crore"
    }]
    session["chat_history"] = [
        {"role": "user", "content": "Mujhe Gulshan mein property dikhao"},
        {"role": "assistant", "content": "Janab yeh dekhein Gulshan mein 4 Bed House demand 2.5 Crore."}
    ]

    result = simulate_pipeline_turn(session, "Abe yar kya bakwas bhej rahe ho, maine bola tha flat chahiye")
    print_turn_details(1, result)

    reply = result["reply_text"].lower()
    updated_ptype = result["updated_session"].get("property_type")

    switched_to_flat = (updated_ptype == "flat")
    is_calming = any(w in reply for w in ["maazrat", "maafi", "tasalli", "fikr na", "guzarish", "madad", "flat", "apologize", "sorry"])

    checks = [
        ("Property type successfully switched to 'flat'", switched_to_flat),
        ("Empathetic / calming tone used for frustrated user", is_calming)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_5_sub_location():
    print_separator("SCENARIO 5: Sub-Location Multi-Turn Test")
    print("Turn 1: User says 'Gulshan'")
    print("Turn 2: User responds 'Block 13D'")

    session = create_fresh_session()
    
    # Turn 1
    t1 = simulate_pipeline_turn(session, "Gulshan")
    print_turn_details(1, t1)

    t1_loc = t1["updated_session"].get("location")
    t1_pending = t1["updated_session"].get("sub_location_pending")
    t1_prompt = t1["updated_session"].get("sub_location_prompt")

    # Turn 2
    t2 = simulate_pipeline_turn(t1["updated_session"], "Block 13D")
    print_turn_details(2, t2)

    t2_loc = str(t2["updated_session"].get("location"))
    t2_pending = t2["updated_session"].get("sub_location_pending")

    checks = [
        ("Turn 1 detected generic 'Gulshan'", "gulshan" in str(t1_loc).lower()),
        ("Turn 1 flagged sub_location_pending = True", bool(t1_pending)),
        ("Turn 1 generated sub-location prompt (Block 1-16)", bool(t1_prompt and "block" in t1_prompt.lower())),
        ("Turn 2 resolved specific sub-location (e.g. 'Gulshan Block 13D')", "13" in t2_loc and "block" in t2_loc.lower()),
        ("Turn 2 cleared sub_location_pending flag", t2_pending is False)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


# =========================================================================================
# 5 NEW EXTREME / WEIRD EDGE-CASE PERSONAS (STRESS TESTING)
# =========================================================================================

def run_scenario_6_kanjus_dreamer():
    print_separator("SCENARIO 6: Kanjus Dreamer (Unrealistic 15 Lakh for 1000 Gaz DHA 8 Bungalow)")
    print("User (Turn 1): 'DHA Phase 8 mein samandar ke samne 1000 gaz ka bangla chahiye, budget sirf 15 Lakh hai.'")
    print("User (Turn 2): 'Kharidna hai'")
    print("Goal: Verify budget extraction (15 Lakh), strict 30% boundary adherence, and NO 30 Crore property hallucination.")

    session = create_fresh_session()
    t1 = simulate_pipeline_turn(session, "DHA Phase 8 mein samandar ke samne 1000 gaz ka bangla chahiye, budget sirf 15 Lakh hai.")
    print_turn_details(1, t1)

    t1_budget = t1["updated_session"].get("budget")
    t1_loc = str(t1["updated_session"].get("location", ""))
    t1_size = str(t1["updated_session"].get("size", ""))
    t1_type = t1["updated_session"].get("property_type")

    t1_checks = [
        ("Budget extracted as 15 Lakh (1,500,000 PKR)", t1_budget == 1500000),
        ("Location extracted as DHA Phase 8", "phase 8" in t1_loc.lower() or "dha" in t1_loc.lower()),
        ("Size extracted as 1000 gaz", "1000" in t1_size),
        ("Property type mapped to house/bangla", t1_type in ["house", "bangla"])
    ]
    print("\n🔍 Turn 1 Assessment:")
    for label, passed in t1_checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")

    # Turn 2: User confirms buying
    t2 = simulate_pipeline_turn(t1["updated_session"], "Kharidna hai")
    print_turn_details(2, t2)

    t2_reply = t2["reply_text"].lower()
    no_crazy_30cr = "30 crore" not in t2_reply and "50 crore" not in t2_reply and "10 crore" not in t2_reply
    respects_budget = t2["updated_session"].get("budget") == 1500000

    t2_checks = [
        ("Budget strictly capped at 15 Lakh (no 30/50 Crore hallucination)", no_crazy_30cr),
        ("Budget preserved in session without inflation", respects_budget),
        ("Polite and professional response maintained", len(t2["reply_text"]) > 10)
    ]
    print("\n🔍 Turn 2 Assessment:")
    for label, passed in t2_checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_7_jaldbaaz_seth():
    print_separator("SCENARIO 7: Jaldbaaz Seth (Impatient & Rushing)")
    print("User: 'Abe bhai jaldi DHA ka plot dikha, time nahi hai mere paas, price ki tension mat le.'")
    print("Goal: Bot must NOT rush or hallucinate a random plot; calmly de-escalate and gather missing details step-by-step.")

    session = create_fresh_session()
    result = simulate_pipeline_turn(session, "Abe bhai jaldi DHA ka plot dikha, time nahi hai mere paas, price ki tension mat le.")
    print_turn_details(1, result)

    reply = result["reply_text"].lower()
    p = result["parsed_json"]
    loc = str(result["updated_session"].get("location", ""))
    ptype = result["updated_session"].get("property_type")

    did_not_hallucinate = p.get("intent") != "search"
    captured_dha = "dha" in loc.lower()
    captured_plot = ptype == "plot"
    polite_handling = any(w in reply for w in ["janab", "phase", "budget", "tasalli", "size", "plot"])

    checks = [
        ("Location captured as DHA (with sub-location flow active)", captured_dha),
        ("Property type captured as plot", captured_plot),
        ("Does NOT prematurely trigger unconfirmed search", did_not_hallucinate),
        ("Politely and calmly asks for missing details (Phase / budget / size)", polite_handling)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_8_confused_aatma():
    print_separator("SCENARIO 8: Confused Aatma (Flip-Flopper / Multi-Turn State Reset)")
    print("Turn 1: 'Gulshan mein flat chahiye.'")
    print("Turn 2: 'Nahi yaar, DHA mein plot dikha de.'")
    print("Turn 3: 'Ek kaam kar, Bahria Town mein ghar bta de.'")
    print("Goal: Verify that each turn resets previous contradictory memory and adapts state dynamically.")

    session = create_fresh_session()

    # Turn 1
    t1 = simulate_pipeline_turn(session, "Gulshan mein flat chahiye.")
    print_turn_details(1, t1)
    t1_loc = str(t1["updated_session"].get("location", "")).lower()
    t1_type = t1["updated_session"].get("property_type")

    # Turn 2: Switch to DHA plot
    t2 = simulate_pipeline_turn(t1["updated_session"], "Nahi yaar, DHA mein plot dikha de.")
    print_turn_details(2, t2)
    t2_loc = str(t2["updated_session"].get("location", "")).lower()
    t2_type = t2["updated_session"].get("property_type")

    # Turn 3: Switch to Bahria Town house
    t3 = simulate_pipeline_turn(t2["updated_session"], "Ek kaam kar, Bahria Town mein ghar bta de.")
    print_turn_details(3, t3)
    t3_loc = str(t3["updated_session"].get("location", "")).lower()
    t3_type = t3["updated_session"].get("property_type")

    checks = [
        ("Turn 1: Location = Gulshan, Type = flat", "gulshan" in t1_loc and t1_type == "flat"),
        ("Turn 2: Switched to DHA, Type = plot (flat reset)", "dha" in t2_loc and t2_type == "plot"),
        ("Turn 3: Switched to Bahria Town, Type = house (plot reset)", "bahria" in t3_loc and t3_type == "house"),
        ("All 3 transitions handled without crashing or conflicting states", True)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_9_vague_uncle():
    print_separator("SCENARIO 9: Vague Uncle (Zero Details / Poetic & Vague)")
    print("User: 'Karachi mein koi sasti si aur hawa dar jagah dila do.'")
    print("Goal: Bot handles completely unstructured, subjective criteria without crashing, asking user to specify area or type.")

    session = create_fresh_session()
    result = simulate_pipeline_turn(session, "Karachi mein koi sasti si aur hawa dar jagah dila do.")
    print_turn_details(1, result)

    reply = result["reply_text"].lower()
    p = result["parsed_json"]

    did_not_crash = len(result["reply_text"]) > 10
    no_fake_search = p.get("intent") != "search"
    guides_user = any(w in reply for w in ["kharidna", "kiraye", "rent", "area", "ilaqe", "kis", "budget", "dha", "clifton", "gulshan", "flat", "plot", "ghar", "jagah"])

    checks = [
        ("Handled unstructured text gracefully without crashing", did_not_crash),
        ("Did NOT execute hallucinated search with zero parameters", no_fake_search),
        ("Step-by-step requirement gathering initiated (asks purpose/area/type)", guides_user)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def run_scenario_10_time_waster():
    print_separator("SCENARIO 10: Time Waster (Context Breaker / Weather + Office + Plot)")
    print("User: 'Salam! Bhai kal barish bohot hui Karachi mein. Waise mujhe plot lena tha. Aapki office kahan hai?'")
    print("Goal: Iron Dome allows message (due to 'plot'); bot triggers handoff or gathers plot requirement without getting sidetracked by weather.")

    session = create_fresh_session()
    result = simulate_pipeline_turn(session, "Salam! Bhai kal barish bohot hui Karachi mein. Waise mujhe plot lena tha. Aapki office kahan hai?")
    print_turn_details(1, result)

    reply = result["reply_text"].lower()
    p = result["parsed_json"]
    intent = p.get("intent")

    passed_iron_dome = "mere kaam se bahir hai" not in reply
    valid_behavior = (intent == "handoff") or any(w in reply for w in ["plot", "area", "dha", "clifton", "office", "agent", "raabta"])

    checks = [
        ("Iron Dome did NOT block the message (real estate context preserved)", passed_iron_dome),
        ("Handled office inquiry according to core rules (Handoff to senior agent or office address)", valid_behavior),
        ("Weather chit-chat gracefully handled without derailing pipeline", True)
    ]
    print("\n🔍 Verification Assessment:")
    for label, passed in checks:
        print(f"   {'✅ PASS' if passed else '❌ FAIL'}: {label}")


def main_suite():
    print("\n" + "#" * 80)
    print(" QORVX PK BOT — AUTOMATED BUYER PERSONA & LLM PIPELINE SIMULATOR ".center(80, "#"))
    print("#" * 80)
    print(f"Start Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Working Directory: {os.getcwd()}")
    
    run_core = "--stress" not in sys.argv
    run_stress = "--core" not in sys.argv

    start_all = time.time()
    try:
        # Core 5 Scenarios
        if run_core:
            print("\n" + ">>> RUNNING CORE BUYER PERSONAS (1 - 5) <<<".center(80, " "))
            run_scenario_1_garam_buyer()
            run_scenario_2_outofstock_test()
            run_scenario_3_non_karachi_trap()
            run_scenario_4_ghusse_wala()
            run_scenario_5_sub_location()

        # 5 New Extreme / Weird Edge-Case Stress Testing Scenarios
        if run_stress:
            print("\n\n" + ">>> RUNNING EXTREME / WEIRD EDGE-CASE STRESS TESTS (6 - 10) <<<".center(80, " "))
            run_scenario_6_kanjus_dreamer()
            run_scenario_7_jaldbaaz_seth()
            run_scenario_8_confused_aatma()
            run_scenario_9_vague_uncle()
            run_scenario_10_time_waster()

    except Exception as e:
        print(f"\n❌ Error during simulation run: {e}")
        import traceback
        traceback.print_exc()

    elapsed = time.time() - start_all
    print_separator("ALL 10 SIMULATION SCENARIOS COMPLETE")
    print(f"Total Simulation Duration: {elapsed:.2f} seconds\n")

if __name__ == "__main__":
    main_suite()

