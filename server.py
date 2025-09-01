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

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ---------- ENV VARS ----------
DEFAULT_QTY = int(os.environ.get("DEFAULT_QTY", "35"))
ZERODHA_API_KEY = os.environ.get("ZERODHA_API_KEY")
ZERODHA_ACCESS_TOKEN = os.environ.get("ZERODHA_ACCESS_TOKEN")
TEST_MODE = os.environ.get("TEST_MODE", "True") == "True"

app = Flask(__name__)

kite = KiteConnect(api_key=ZERODHA_API_KEY)
kite.set_access_token(ZERODHA_ACCESS_TOKEN)

# ---------- FAKE POSITIONS (TEST MODE) ----------
fake_positions = {}  # store {symbol: qty} in TEST_MODE

def log_positions(final=False):
    """Logs current positions (both test/live)."""
    positions = get_current_positions()
    if not positions:
        msg = "✅ Final positions: None" if final else "📌 Current positions: None"
        logging.info(msg)
    else:
        pretty = [f"{p['tradingsymbol']} x {p['quantity']}" for p in positions]
        msg = f"✅ Final positions: {', '.join(pretty)}" if final else f"📌 Current positions: {', '.join(pretty)}"
        logging.info(msg)

# ---------- SAFE FUNCTIONS ----------
def safe_ltp(symbol):
    for attempt in range(5):
        try:
            return kite.ltp([symbol])[symbol]["last_price"]
        except Exception as e:
            logging.warning(f"⚠️ LTP retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("❌ LTP fetch failed after 2 attempts")

def place_order(symbol, qty=DEFAULT_QTY, transaction_type="BUY"):
    """Unified order placing with retries"""
    for attempt in range(5):
        try:
            kite.place_order(
                variety="regular",
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=qty,
                order_type="MARKET",
                product="NRML"
            )
            logging.info(f"✅ Order success: {transaction_type} {symbol} x {qty}")
            return True
        except Exception as e:
            logging.warning(f"⚠️ Order retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("❌ Order failed after 2 attempts")

def exit_position(symbol, qty=DEFAULT_QTY):
    """Exit a position safely (opposite side order)."""
    return place_order(symbol, qty, transaction_type="SELL")

# ---------- HELPER FUNCTIONS ----------
def is_market_open():
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_time = now.time()
    return time(9, 15) <= current_time <= time(15, 30)

def get_monthly_expiry():
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
    strike = int(spot_price / step) * step if option_type == "CE" else (int(spot_price / step) + 1) * step
    expiry = get_monthly_expiry()
    return f"BANKNIFTY{expiry}{strike}{option_type}"

def get_current_positions():
    if TEST_MODE:
        return [{"tradingsymbol": sym, "quantity": qty} for sym, qty in fake_positions.items()]
    try:
        positions = kite.positions()["net"]
        return [p for p in positions if p["quantity"] != 0]
    except Exception as e:
        logging.warning(f"⚠️ Could not fetch positions: {e}")
        return []

# ---------- TEST MODE HELPERS ----------
@app.route('/reset_positions', methods=['GET'])
def reset_positions():
    if TEST_MODE:
        fake_positions.clear()
        log_positions(final=True)
        return jsonify({"status": "reset", "positions": fake_positions})
    return jsonify({"status": "error", "reason": "Not in TEST_MODE"})

@app.route('/remove_position', methods=['GET'])
def remove_position():
    if TEST_MODE:
        sym = request.args.get("symbol")
        if sym in fake_positions:
            del fake_positions[sym]
            log_positions(final=True)
            return jsonify({"status": "removed", "symbol": sym, "positions": fake_positions})
        return jsonify({"status": "not_found", "positions": fake_positions})
    return jsonify({"status": "error", "reason": "Not in TEST_MODE"})

@app.route('/view_positions', methods=['GET'])
def view_positions():
    try:
        return jsonify({"positions": get_current_positions()})
    except Exception as e:
        logging.error(f"❌ View positions error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# ---------- HEALTH CHECK ----------
@app.route('/', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Bot is running"}), 200

# ---------- MAIN ROUTE ----------
last_flip_time = None

@app.route('/webhook', methods=['POST'])
def webhook():
    global last_flip_time
    try:
        if not is_market_open():
            return jsonify({"status": "rejected", "reason": "Outside market hours"})

        data = request.get_json(silent=True)
        if not data:
            raw = request.data.decode('utf-8', errors='replace')
            logging.warning(f"⚠️ Raw webhook body (not valid JSON): {raw}")
            return jsonify({"status": "error", "reason": "invalid JSON", "raw": raw})

        option_type = data.get("type")  # "CE" or "PE"
        qty = int(data.get("qty", DEFAULT_QTY))
        logging.info(f"📩 Received {option_type} Alert")

        spot = safe_ltp("NSE:NIFTY BANK")
        main_symbol = get_option_symbol(spot, option_type)
        opposite_type = "PE" if option_type == "CE" else "CE"
        positions = get_current_positions()
        opposite_symbol = None
        for p in positions:
            if p["tradingsymbol"].endswith(opposite_type):
                opposite_symbol = p["tradingsymbol"]
                break
 


        if last_flip_time and (datetime.now() - last_flip_time).total_seconds() < 2:
            logging.info("⏳ Flip cooldown active → ignoring this alert")
            log_positions(final=True)
            return jsonify({"status": "skipped", "reason": "flip cooldown"})

        if not positions:
            logging.info(f"🆕 Flat → Entering {option_type} @ {main_symbol} (qty: {qty})")
            if TEST_MODE:
                fake_positions[main_symbol] = qty
                log_positions(final=True)
                return jsonify({"status": "test", "entry": main_symbol, "positions": fake_positions})
            # Live order
            place_order(main_symbol, qty, "BUY")
            log_positions(final=True)
            return jsonify({"status": "success", "entry": main_symbol})

        if any(p["tradingsymbol"].endswith(option_type) for p in positions):
            logging.info(f"⏩ Already holding a {option_type} position → skipping new {main_symbol}")
            log_positions(final=True)
            return jsonify({"status": "skipped", "reason": f"Already in {option_type}"})


        logging.info(f"🔄 Switching: Exit {opposite_type} → Enter {option_type} @ {main_symbol} (qty: {qty})")

        if TEST_MODE:
            if opposite_symbol in fake_positions:
                del fake_positions[opposite_symbol]
            fake_positions[main_symbol] = qty
            last_flip_time = datetime.now()
            log_positions(final=True)
            return jsonify({"status": "test", "flip": {"exit": opposite_symbol, "enter": main_symbol}, "positions": fake_positions})

        # Live flip
        exit_position(opposite_symbol, qty)
        place_order(main_symbol, qty, "BUY")
        last_flip_time = datetime.now()
        log_positions(final=True)
        return jsonify({"status": "success", "flip": {"exit": opposite_symbol, "enter": main_symbol}})

    except Exception as e:
        logging.error(f"❌ Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# ---------- START SERVER ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
