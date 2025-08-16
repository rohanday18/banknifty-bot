import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
from flask import Flask, request, jsonify 
from kiteconnect import KiteConnect
import os
from datetime import datetime, time, timedelta
import calendar
import logging
import time as time_module

# Environment vars
ZERODHA_API_KEY = os.environ.get("ZERODHA_API_KEY")
ZERODHA_ACCESS_TOKEN = os.environ.get("ZERODHA_ACCESS_TOKEN")
TEST_MODE = os.environ.get("TEST_MODE", "True") == "True"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

kite = KiteConnect(api_key=ZERODHA_API_KEY)
kite.set_access_token(ZERODHA_ACCESS_TOKEN)

# ---------- FAKE POSITION STORAGE FOR TEST MODE ----------
fake_positions = []  # store tradingsymbols in TEST_MODE

def log_fake_positions():
    """Logs the current fake positions in TEST_MODE"""
    if TEST_MODE:
        logging.info(f"📌 Fake positions now: {fake_positions}")

# ---------- SAFE FUNCTIONS ----------
def safe_ltp(symbol):
    """Retry LTP fetch up to 2 times"""
    for attempt in range(2):
        try:
            return kite.ltp([symbol])[symbol]["last_price"]
        except Exception as e:
            print(f"⚠️ LTP retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("❌ LTP fetch failed after 2 attempts")

def safe_place_order(**kwargs):
    """Retry order up to 2 times"""
    for attempt in range(2):
        try:
            kite.place_order(**kwargs)
            return True
        except Exception as e:
            print(f"⚠️ Order retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("❌ Order failed after 2 attempts")

# ---------- HELPER FUNCTIONS ----------
def is_market_open():
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_time = now.time()
    return time(9, 15) <= current_time <= time(15, 30)

def get_monthly_expiry():
    """Get current or next month expiry (skip current if <5 days remain)."""
    today = datetime.today()
    year, month = today.year, today.month

    def last_thursday(y, m):
        last_day = calendar.monthrange(y, m)[1]
        d = datetime(y, m, last_day)
        while d.weekday() != 3:
            d -= timedelta(days=1)
        return d

    expiry = last_thursday(year, month)
    if (expiry - today).days < 5:  # switch to next month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        expiry = last_thursday(year, month)

    return expiry.strftime("%y%b").upper()

def get_option_symbol(spot_price, option_type):
    step = 100
    if option_type == "CE":
        strike = int(spot_price / step) * step
    else:
        strike = (int(spot_price / step) + 1) * step
    expiry = get_monthly_expiry()
    return f"BANKNIFTY{expiry}{strike}{option_type}"

def get_current_positions():
    if TEST_MODE:
        return [{"tradingsymbol": sym, "quantity": 1} for sym in fake_positions]
    try:
        positions = kite.positions()["net"]
        active = [p for p in positions if p["quantity"] != 0]
        return active
    except Exception as e:
        print(f"⚠️ Could not fetch positions: {e}")
        return []

# ---------- TEST MODE HELPERS ----------
@app.route('/reset_positions', methods=['GET'])
def reset_positions():
    if TEST_MODE:
        fake_positions.clear()
        log_fake_positions()
        return jsonify({"status": "reset", "positions": fake_positions})
    return jsonify({"status": "error", "reason": "Not in TEST_MODE"})

@app.route('/remove_position', methods=['GET'])
def remove_position():
    if TEST_MODE:
        sym = request.args.get("symbol")
        if sym in fake_positions:
            fake_positions.remove(sym)
            log_fake_positions()
            return jsonify({"status": "removed", "symbol": sym, "positions": fake_positions})
        return jsonify({"status": "not_found", "positions": fake_positions})
    return jsonify({"status": "error", "reason": "Not in TEST_MODE"})

@app.route('/view_positions', methods=['GET'])
def view_positions():
    return jsonify({"positions": fake_positions if TEST_MODE else get_current_positions()})

# ---------- MAIN ROUTE ----------
last_flip_time = None  # store last flip time globally

@app.route('/webhook', methods=['POST'])
def webhook():
    global last_flip_time
    try:
        if not is_market_open():
            return jsonify({"status": "rejected", "reason": "Outside market hours"})

        # ✅ Try parsing JSON, else fallback to raw logging
        data = request.get_json(silent=True)
        if not data:
            raw = request.data.decode('utf-8', errors='replace')
            print(f"⚠️ Raw webhook body (not valid JSON): {raw}")
            return jsonify({"status": "error", "reason": "invalid JSON", "raw": raw})

        print(f"📩 Received webhook payload: {data}")
        option_type = data.get("type")  # "CE" or "PE"
        qty = int(data.get("qty", 105))

        spot = safe_ltp("NSE:NIFTY BANK")
        main_symbol = get_option_symbol(spot, option_type)
        opposite_type = "PE" if option_type == "CE" else "CE"
        opposite_symbol = get_option_symbol(spot, opposite_type)

        positions = get_current_positions()

        # Cooldown check — prevent flip within 2 sec
        if last_flip_time and (datetime.now() - last_flip_time).total_seconds() < 2:
            print("⏳ Flip cooldown active → ignoring this alert")
            return jsonify({"status": "skipped", "reason": "flip cooldown"})

        # ---------- If flat ----------
        if not positions:
            print(f"🆕 No open positions → taking {option_type} entry")
            if TEST_MODE:
                fake_positions.append(main_symbol)
                log_fake_positions()
                print(f"[TEST] BUY {main_symbol} x {qty}")
                return jsonify({"status": "test", "entry": main_symbol, "positions": fake_positions})
            # Live order logic here...
            return jsonify({"status": "success", "entry": main_symbol})

        # ---------- If already in same side ----------
        if any(p["tradingsymbol"].endswith(option_type) for p in positions):
            print(f"⏩ Already in {option_type} → skipping duplicate entry")
            return jsonify({"status": "skipped", "reason": f"Already in {option_type}"})

        # ---------- Flip positions ----------
        if TEST_MODE:
            if opposite_symbol in fake_positions:
                fake_positions.remove(opposite_symbol)
            fake_positions.append(main_symbol)
            log_fake_positions()
            last_flip_time = datetime.now()
            print(f"[TEST] EXIT {opposite_symbol} x {qty}")
            print("[TEST] Waiting 2 sec...")
            print(f"[TEST] BUY {main_symbol} x {qty}")
            return jsonify({"status": "test", "flip": {"exit": opposite_symbol, "enter": main_symbol}, "positions": fake_positions})

        # Live flip logic...
        return jsonify({"status": "success", "flip": {"exit": opposite_symbol, "enter": main_symbol}})

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# ---------- START SERVER ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
