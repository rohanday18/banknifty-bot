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
    if TEST_MODE:
        logging.info(f"✅ Final positions: {fake_positions if fake_positions else 'None'}")

# ---------- SAFE FUNCTIONS ----------
def safe_ltp(symbol):
    for attempt in range(2):
        try:
            return kite.ltp([symbol])[symbol]["last_price"]
        except Exception as e:
            print(f"⚠️ LTP retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("❌ LTP fetch failed after 2 attempts")

def safe_place_order(**kwargs):
    for attempt in range(2):
        try:
            order_id = kite.place_order(**kwargs)
            return order_id
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
    today = datetime.today()
    year, month = today.year, today.month

    def last_thursday(y, m):
        last_day = calendar.monthrange(y, m)[1]
        d = datetime(y, m, last_day)
        while d.weekday() != 3:
            d -= timedelta(days=1)
        return d

    expiry = last_thursday(year, month)
    if (expiry - today).days < 5:  
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
        return [p for p in positions if p["quantity"] != 0]
    except Exception as e:
        print(f"⚠️ Could not fetch positions: {e}")
        return []

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
            print(f"⚠️ Raw webhook body (not valid JSON): {raw}")
            return jsonify({"status": "error", "reason": "invalid JSON", "raw": raw})

        action = data.get("action", "BUY")
        option_type = data.get("type")
        qty = int(data.get("qty", 35))

        print(f"📨 Received {option_type} Alert")

        spot = safe_ltp("NSE:NIFTY BANK")
        main_symbol = get_option_symbol(spot, option_type)
        opposite_type = "PE" if option_type == "CE" else "CE"
        opposite_symbol = get_option_symbol(spot, opposite_type)

        positions = get_current_positions()

        if last_flip_time and (datetime.now() - last_flip_time).total_seconds() < 2:
            print("⏳ Flip cooldown active → ignoring this alert")
            return jsonify({"status": "skipped", "reason": "flip cooldown"})

        # ---------- If flat ----------
        if not positions:
            print(f"🟦 Flat → Entering {option_type} @ {main_symbol} (qty: {qty})")
            if TEST_MODE:
                fake_positions.append(main_symbol)
                log_fake_positions()
            else:
                safe_place_order(
                    tradingsymbol=main_symbol,
                    transaction_type=kite.TRANSACTION_TYPE_BUY,
                    quantity=qty,
                    order_type=kite.ORDER_TYPE_MARKET,
                    product=kite.PRODUCT_NRML,
                    exchange=kite.EXCHANGE_NFO
                )
                logging.info(f"✅ LIVE BUY {main_symbol} x {qty}")
            return jsonify({"status": "success", "entry": main_symbol})

        # ---------- If already in same side ----------
        if any(p["tradingsymbol"].endswith(option_type) for p in positions):
            print(f"⏩ Already in {option_type} → skipping duplicate entry")
            return jsonify({"status": "skipped", "reason": f"Already in {option_type}"})

        # ---------- Flip positions ----------
        print(f"🔄 Flip → EXIT {opposite_symbol}, ENTER {main_symbol}")
        if TEST_MODE:
            if opposite_symbol in fake_positions:
                fake_positions.remove(opposite_symbol)
            fake_positions.append(main_symbol)
            log_fake_positions()
            last_flip_time = datetime.now()
        else:
            # exit opposite
            safe_place_order(
                tradingsymbol=opposite_symbol,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_NRML,
                exchange=kite.EXCHANGE_NFO
            )
            logging.info(f"✅ LIVE EXIT {opposite_symbol} x {qty}")
            # enter new
            safe_place_order(
                tradingsymbol=main_symbol,
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_NRML,
                exchange=kite.EXCHANGE_NFO
            )
            logging.info(f"✅ LIVE BUY {main_symbol} x {qty}")
            last_flip_time = datetime.now()

        return jsonify({"status": "success", "flip": {"exit": opposite_symbol, "enter": main_symbol}})

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# ---------- START SERVER ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
