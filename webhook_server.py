"""
Coastal Cultures Fermentation Club — Webhook Server
Handles: JOIN signups, STOP opt-outs, menu orders with inventory tracking,
         auto sold-out replies, and drop auto-close when all items gone.
"""

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import json
import os
import re

app = Flask(__name__)

# ─────────────────────────────────────────
# 🔧 YOUR SETTINGS
# ─────────────────────────────────────────
import os, base64, tempfile

GOOGLE_SHEET_NAME  = "Coastal Cultures"
INVITE_SHEET_NAME  = "Coastal Cultures - Invite List CK"
CONTACTS_FILE      = "contacts.json"
INVENTORY_FILE     = "inventory.json"

def get_creds_file():
    env_creds = os.environ.get("GOOGLE_CREDS_BASE64")
    if env_creds:
        creds_data = base64.b64decode(env_creds).decode("utf-8")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(creds_data)
        tmp.flush()
        return tmp.name
    return "google_creds.json"

GOOGLE_CREDS_FILE = get_creds_file()

# ─────────────────────────────────────────
# 🛒 INVENTORY — update before each drop
# Set quantity to 0 to hide an item from the menu
# ─────────────────────────────────────────
DEFAULT_INVENTORY = {
    "drop_number": 1,            # Increments each drop — creates new sheet tab
    "drop_open": True,           # Set to False to manually close the drop
    "total_cap": 30,             # Max total jars for the whole drop
    "total_sold": 0,             # Tracks total jars sold (auto-updated)
    "items": {
        "1": {
            "name": "Traditional Sauerkraut (16oz)",
            "price": 12,
            "qty_available": 20,
            "qty_sold": 0,
            "keywords": ["sauerkraut", "kraut", "cabbage", "1"]
        },
        "2": {
            "name": "Kimchi (16oz)",
            "price": 12,
            "qty_available": 0,   # 0 = coming soon / hidden
            "qty_sold": 0,
            "keywords": ["kimchi", "kim chi", "kimchee", "2"]
        },
        "3": {
            "name": "Hot Sauce (5oz)",
            "price": 8,
            "qty_available": 0,   # 0 = coming soon / hidden
            "qty_sold": 0,
            "keywords": ["hot sauce", "hotsauce", "sauce", "hot", "3"]
        },
        "4": {
            "name": "Kombucha (16oz)",
            "price": 8,
            "qty_available": 0,   # 0 = coming soon / hidden
            "qty_sold": 0,
            "keywords": ["kombucha", "booch", "4"]
        }
    }
}

PENDING_SIGNUPS = {}

# ─────────────────────────────────────────
# 📦 Inventory helpers
# ─────────────────────────────────────────
def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        save_inventory(DEFAULT_INVENTORY)
        return DEFAULT_INVENTORY
    with open(INVENTORY_FILE, "r") as f:
        return json.load(f)

def save_inventory(inv):
    with open(INVENTORY_FILE, "w") as f:
        json.dump(inv, f, indent=2)

def get_available_items():
    """Return only items that are in stock."""
    inv = load_inventory()
    available = {}
    for key, item in inv["items"].items():
        remaining = item["qty_available"] - item["qty_sold"]
        if remaining > 0:
            available[key] = {**item, "remaining": remaining}
    return available

def drop_is_open():
    inv = load_inventory()
    if not inv.get("drop_open", False):
        return False
    available = get_available_items()
    total_remaining = inv["total_cap"] - inv["total_sold"]
    return len(available) > 0 and total_remaining > 0

def build_menu_text():
    """Build the current available menu as a text string."""
    available = get_available_items()
    inv = load_inventory()
    total_remaining = inv["total_cap"] - inv["total_sold"]

    if not available or total_remaining <= 0:
        return None  # Drop is closed

    lines = []
    for key, item in sorted(available.items()):
        remaining = item["remaining"]
        lines.append(f"{key} - {item['name']} — ${item['price']} ({remaining} left)")

    return "\n".join(lines)

def deduct_inventory(parsed_order):
    """
    parsed_order: list of (item_key, qty) tuples
    Returns: (approved, rejected, messages)
      approved = list of (key, qty, name, price) that went through
      rejected = list of (key, qty, name) that were over stock
    """
    inv = load_inventory()
    approved = []
    rejected = []

    for key, qty in parsed_order:
        item = inv["items"].get(key)
        if not item:
            continue

        remaining = item["qty_available"] - item["qty_sold"]
        total_remaining = inv["total_cap"] - inv["total_sold"]

        # Cap qty to what's actually available
        can_fill = min(qty, remaining, total_remaining)

        if can_fill <= 0:
            rejected.append((key, qty, item["name"]))
        else:
            if can_fill < qty:
                rejected.append((key, qty - can_fill, item["name"]))

            # Deduct
            inv["items"][key]["qty_sold"] += can_fill
            inv["total_sold"] += can_fill
            approved.append((key, can_fill, item["name"], item["price"]))

    save_inventory(inv)
    return approved, rejected

# ─────────────────────────────────────────
# 📂 Contact helpers
# ─────────────────────────────────────────
def load_contacts():
    if not os.path.exists(CONTACTS_FILE):
        return []
    with open(CONTACTS_FILE, "r") as f:
        return json.load(f)

def save_contacts(contacts):
    with open(CONTACTS_FILE, "w") as f:
        json.dump(contacts, f, indent=2)

def find_contact(phone):
    for c in load_contacts():
        if c["phone"] == phone:
            return c
    return None

def add_contact(name, phone):
    contacts = load_contacts()
    contacts.append({
        "name": name,
        "phone": phone,
        "joined": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "active": True
    })
    save_contacts(contacts)

def deactivate_contact(phone):
    contacts = load_contacts()
    for c in contacts:
        if c["phone"] == phone:
            c["active"] = False
    save_contacts(contacts)

# ─────────────────────────────────────────
# 📊 Google Sheets helpers
# ─────────────────────────────────────────
def get_book():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME)

def get_or_create_tab(book, title, headers):
    try:
        return book.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        sheet = book.add_worksheet(title=title, rows=1000, cols=10)
        sheet.append_row(headers)
        return sheet

def get_drop_number():
    inv = load_inventory()
    return inv.get("drop_number", 1)

def get_drop_tab_name():
    return f"Drop {get_drop_number()} Orders"

def log_new_member(name, phone):
    try:
        book    = get_book()
        members = get_or_create_tab(book, "Members", ["Joined", "Name", "Phone", "Status"])
        members.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name, phone, "Active"])
        print(f"  📊 Member logged: {name}")
    except Exception as e:
        print(f"  ❌ Sheet error (member): {e}")

def log_opt_out(name, phone):
    try:
        book    = get_book()
        members = get_or_create_tab(book, "Members", ["Joined", "Name", "Phone", "Status"])
        cell    = members.find(phone)
        if cell:
            members.update_cell(cell.row, 4, "Opted Out")
        print(f"  📊 Opt-out logged: {name}")
    except Exception as e:
        print(f"  ❌ Sheet error (opt-out): {e}")

def log_order(name, phone, raw_reply, approved, total):
    try:
        book      = get_book()
        tab_name  = get_drop_tab_name()
        orders    = get_or_create_tab(book, tab_name,    ["Timestamp", "Name", "Phone", "Raw Reply", "Items Ordered", "Total $"])
        inventory = get_or_create_tab(book, "Inventory", ["Timestamp", "Event", "Item", "Qty", "Notes"])
        items_str = ", ".join(f"{qty}x {nm}" for _, qty, nm, _ in approved)
        orders.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            name, phone, raw_reply, items_str, f"${total}"
        ])
        for key, qty, nm, _ in approved:
            inv       = load_inventory()
            remaining = inv["items"][key]["qty_available"] - inv["items"][key]["qty_sold"]
            inventory.append_row([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                f"SOLD (Drop {get_drop_number()})", nm, qty, f"{remaining} remaining"
            ])
        print(f"  📊 Order logged to {tab_name}: {name} → {items_str} (${total})")
    except Exception as e:
        print(f"  ❌ Sheet error (order): {e}")

def log_drop_closed():
    try:
        book      = get_book()
        inventory = get_or_create_tab(book, "Inventory", ["Timestamp", "Event", "Item", "Qty", "Notes"])
        inventory.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            f"DROP {get_drop_number()} CLOSED", "All items", "-", "Sold out!"
        ])
    except Exception as e:
        print(f"  ❌ Sheet error (drop closed): {e}")

def log_message(name, phone, message):
    try:
        book     = get_book()
        messages = get_or_create_tab(book, "Messages", ["Timestamp", "Name", "Phone", "Message"])
        messages.append_row([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name, phone, message])
        print(f"  📊 Message logged: {name} → {message}")
    except Exception as e:
        print(f"  ❌ Sheet error (message): {e}")

# ─────────────────────────────────────────
# 🔍 Smart order parser
# ─────────────────────────────────────────
WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10
}

def word_to_num(word):
    word = word.lower().strip()
    if word in WORD_NUMBERS:
        return WORD_NUMBERS[word]
    try:
        return int(word)
    except ValueError:
        return None

def parse_order(reply_text):
    """Returns list of (item_key, qty) tuples."""
    text  = reply_text.lower()
    found = {}
    inv   = load_inventory()

    # Build keyword map from current inventory
    keyword_map = {}
    for key, item in inv["items"].items():
        for kw in item.get("keywords", []):
            keyword_map[kw] = key

    # Strategy 1: item_num:qty or item_num x qty
    for m in re.finditer(r'\b([1-4])\s*[x:]\s*(\d+)', text):
        key, qty = m.group(1), int(m.group(2))
        found[key] = found.get(key, 0) + qty

    # Strategy 2: qty x item_num
    for m in re.finditer(r'\b(\d+)\s*x\s*([1-4])\b', text):
        qty, key = int(m.group(1)), m.group(2)
        if key not in found:
            found[key] = found.get(key, 0) + qty

    # Strategy 3: qty + keyword
    for kw, key in keyword_map.items():
        pattern = r'(\b(?:' + '|'.join(WORD_NUMBERS.keys()) + r'|\d+)\b)\s+' + re.escape(kw)
        for m in re.finditer(pattern, text):
            qty = word_to_num(m.group(1))
            if qty and key not in found:
                found[key] = found.get(key, 0) + qty

    # Strategy 4: keyword + qty
    for kw, key in keyword_map.items():
        pattern = re.escape(kw) + r'\s*[x(]?\s*(\d+)'
        for m in re.finditer(pattern, text):
            qty = int(m.group(1))
            if key not in found:
                found[key] = found.get(key, 0) + qty

    # Strategy 5: bare keyword (qty = 1)
    for kw, key in keyword_map.items():
        if re.search(r'\b' + re.escape(kw) + r'\b', text) and key not in found:
            found[key] = 1

    # Strategy 6: bare item number (qty = 1)
    for m in re.finditer(r'\b([1-4])\b', text):
        key = m.group(1)
        if key not in found:
            found[key] = 1

    return [(k, v) for k, v in found.items()]

def looks_like_order(reply_text):
    text = reply_text.lower()
    inv  = load_inventory()
    has_number = bool(re.search(r'\b[1-4]\b', text))
    has_keyword = any(
        kw in text
        for item in inv["items"].values()
        for kw in item.get("keywords", [])
    )
    return has_number or has_keyword

# ─────────────────────────────────────────
# 📥 Main webhook
# ─────────────────────────────────────────
@app.route("/sms", methods=["POST"])
def incoming_sms():
    from_number = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()
    body_upper  = body.upper()

    resp    = MessagingResponse()
    contact = find_contact(from_number)
    name    = contact["name"] if contact else "there"

    print(f"\n📩 From {from_number} ({name}): {body}")

    # ── 1. JOIN ──────────────────────────────
    if body_upper == "JOIN":
        if contact and contact.get("active"):
            resp.message(f"Hey {name}! You're already on the Coastal Cultures list 🥬 We'll text you on the next drop!")
        else:
            PENDING_SIGNUPS[from_number] = "awaiting_name"
            resp.message(
                "Welcome to Coastal Cultures! 🥬\n\n"
                "We warmly invite you to join our growing community of culture, health and resilience.\n\n"
                "What's your full name?"
            )

    # ── 2. Awaiting name ─────────────────────
    elif from_number in PENDING_SIGNUPS and PENDING_SIGNUPS[from_number] == "awaiting_name":
        new_name = body.strip().title()
        del PENDING_SIGNUPS[from_number]
        add_contact(new_name, from_number)
        log_new_member(new_name, from_number)
        update_invite_status(from_number)
        resp.message(
            f"You're in, {new_name}! ✅\n\n"
            "By joining you agree to our club terms & conditions:\n"
            "coastalcultures.club/legal.html#terms\n\n"
            "We'll text you when the next drop is ready. "
            "Reply STOP anytime to leave the list.\n\n"
            "— The Brine Surgeons 🥬"
        )

    # ── 3. STOP ──────────────────────────────
    elif body_upper in ["STOP", "UNSUBSCRIBE", "QUIT", "LEAVE"]:
        deactivate_contact(from_number)
        log_opt_out(name, from_number)
        resp.message(
            f"You've been removed from Coastal Cultures, {name}. "
            "Sorry to see you go! Text JOIN anytime to come back 🥬"
        )

    # ── 4. Order ─────────────────────────────
    elif looks_like_order(body) and contact and contact.get("active"):

        # Drop closed entirely?
        if not drop_is_open():
            resp.message(
                f"Hey {name} — this drop is now sold out! 🥬\n\n"
                "We'll text you when the next batch is ready."
            )
            return str(resp)

        parsed = parse_order(body)
        if not parsed:
            resp.message(
                f"Hi {name}, we got your message but couldn't read your order.\n\n"
                "Try replying like:\n"
                "  • '2 sauerkraut'\n"
                "  • Just the number: '1'"
            )
            return str(resp)

        # Deduct from inventory
        approved, rejected = deduct_inventory(parsed)

        if not approved:
            # Everything they wanted is sold out
            menu = build_menu_text()
            if menu:
                resp.message(
                    f"Sorry {name}, those items are sold out! 😔\n\n"
                    f"Still available:\n{menu}\n\n"
                    "Reply with a new order!"
                )
            else:
                resp.message(
                    f"Sorry {name} — this drop is completely sold out! 🥬\n\n"
                    "We'll text you when the next batch is ready."
                )
            return str(resp)

        # Calculate total
        total = sum(qty * price for _, qty, _, price in approved)

        # Log the order
        log_order(name, from_number, body, approved, total)

        # Build confirmation
        items_list = "\n".join(f"  • {qty}x {nm} — ${qty * price}" for _, qty, nm, price in approved)
        reply = (
            f"Got it, {name}! ✅\n\n"
            f"Your order:\n{items_list}\n"
            f"  💰 Total: ${total}\n\n"
        )

        # Partial fill — some items sold out
        if rejected:
            sold_out_names = ", ".join(nm for _, _, nm in rejected)
            reply += f"⚠️ Sold out: {sold_out_names}\n\n"

        reply += "We'll confirm pickup details soon — thanks! 🥬"

        # Check if drop just closed
        if not drop_is_open():
            log_drop_closed()
            reply += "\n\n🎉 That was the last of this batch — drop is now closed!"
            print("  🔒 Drop is now sold out and closed!")

        resp.message(reply)

    # ── 5. General message ───────────────────
    else:
        log_message(name, from_number, body)
        if contact and contact.get("active"):
            resp.message(
                f"Hey {name}! 👋 We got your message and will get back to you soon.\n\n"
                "— Coastal Cultures 🥬"
            )
        else:
            resp.message(
                "Hey! 👋 Text JOIN to sign up for Coastal Cultures fermentation drops 🥬"
            )

    return str(resp)

# ─────────────────────────────────────────
# ▶️ Run
# ─────────────────────────────────────────
if __name__ == "__main__":
    # Print current inventory status on startup
    inv = load_inventory()
    print("\n🥬 Coastal Cultures webhook server running...")
    print("📡 Listening at http://localhost:5000/sms")
    print("\n📦 Current inventory:")
    for key, item in inv["items"].items():
        remaining = item["qty_available"] - item["qty_sold"]
        status = f"{remaining} remaining" if remaining > 0 else "SOLD OUT"
        print(f"   {key}. {item['name']} — {status}")
    total_remaining = inv["total_cap"] - inv["total_sold"]
    print(f"\n   Total cap: {inv['total_cap']} | Sold: {inv['total_sold']} | Remaining: {total_remaining}")
    print(f"   Drop status: {'OPEN 🟢' if drop_is_open() else 'CLOSED 🔴'}")
    print("\n💡 Keep this running during drops!\n")
    app.run(port=5000, debug=True)

def update_invite_status(phone):
    """Auto-update invite sheet to Joined when someone texts JOIN."""
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet  = client.open(INVITE_SHEET_NAME).sheet1
        rows   = sheet.get_all_records()

        for i, row in enumerate(rows, start=2):
            sheet_phone = str(row.get("Phone", "")).strip()
            if sheet_phone == phone:
                sheet.update_cell(i, 6, "Joined")  # Column F = Status
                print(f"  📋 Invite sheet updated to Joined for {phone}")
                return
        print(f"  ℹ️ {phone} not found in invite sheet — may have joined organically")
    except Exception as e:
        print(f"  ⚠️ Could not update invite sheet: {e}")


