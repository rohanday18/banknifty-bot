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
        logging.info(f"\ud83d\udccc Fake positions now: {fake_positions}")

# ---------- SAFE FUNCTIONS ----------
def safe_ltp(symbol):
    """Retry LTP fetch up to 2 times"""
    for attempt in range(2):
        try:
            return kite.ltp([symbol])[symbol]["last_price"]
        except Exception as e:
            print(f"\u26a0\ufe0f LTP retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("\u274c LTP fetch failed after 2 attempts")

def safe_place_order(**kwargs):
    """Retry order up to 2 times"""
    for attempt in range(2):
        try:
            kite.place_order(**kwargs)
            return True
        except Exception as e:
            print(f"\u26a0\ufe0f Order retry {attempt+1} failed: {e}")
            time_module.sleep(1)
    raise Exception("\u274c Order failed after 2 attempts")

# ---------- HELPER FUNCTIONS ----------
def is_market_open():
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_time = now.time()
    return time(9, 15) <= current_time <= time(15, 30)

def get_monthly_expiry():
    today = datetime.today()
    year, month = today.year, today.month
    last_day = calendar.monthrange(year, month)[1]
    expiry = datetime(year, month, last_day)
    while expiry.weekday() != 3:
        expiry -= timedelta(days=1)
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
        print(f"\u26a0\ufe0f Could not fetch positions: {e}")
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

        data = request.get_json()
        print(f"\ud83d\udce9 Received webhook payload: {data}")
        option_type = data.get("type")  # "CE" or "PE"
        qty = int(data.get("qty", 105))

        spot = safe_ltp("NSE:NIFTY BANK")
        main_symbol = get_option_symbol(spot, option_type)
        opposite_type = "PE" if option_type == "CE" else "CE"
        opposite_symbol = get_option_symbol(spot, opposite_type)

        positions = get_current_positions()

        # Cooldown check — prevent flipping back within 2 seconds
        if last_flip_time and (datetime.now() - last_flip_time).total_seconds() < 2:
            print("\u23f3 Flip cooldown active → ignoring this alert")
            return jsonify({"status": "skipped", "reason": "flip cooldown"})

        # ---------- If flat → take both CE and PE ----------
        if not positions:
            print("\ud83c\udd0f No open positions → taking both CE & PE entries")
            if TEST_MODE:
                fake_positions.append(main_symbol)
                fake_positions.append(opposite_symbol)
                log_fake_positions()
                print(f"[TEST] BUY {main_symbol} x {qty}")
                print(f"[TEST] BUY {opposite_symbol} x {qty}")
                return jsonify({"status": "test", "action": "both entries", "positions": fake_positions})
            safe_place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=main_symbol,
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_NRML
            )
            safe_place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=opposite_symbol,
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=kite.ORDER_TYPE_MARKET,
                product=kite.PRODUCT_NRML
            )
            return jsonify({"status": "success", "entries": [main_symbol, opposite_symbol]})

        # ---------- If in CE and CE Buy alert comes → skip ----------
        if any(p["tradingsymbol"].endswith("CE") and option_type == "CE" for p in positions):
            print("\u23e9 Already in CE → skipping duplicate CE entry")
            return jsonify({"status": "skipped", "reason": "Already in CE"})

        # ---------- If in PE and PE Buy alert comes → skip ----------
        if any(p["tradingsymbol"].endswith("PE") and option_type == "PE" for p in positions):
            print("\u23e9 Already in PE → skipping duplicate PE entry")
            return jsonify({"status": "skipped", "reason": "Already in PE"})

        # ---------- Flip positions ----------
        if TEST_MODE:
            if opposite_symbol in fake_positions:
                fake_positions.remove(opposite_symbol)
            fake_positions.append(main_symbol)
            log_fake_positions()
            last_flip_time = datetime.now()  # update flip time
            print(f"[TEST] EXIT {opposite_symbol} x {qty}")
            print("[TEST] Waiting 2 sec...")
            print(f"[TEST] BUY {main_symbol} x {qty}")
            return jsonify({"status": "test", "flip": {"exit": opposite_symbol, "enter": main_symbol}, "positions": fake_positions})

        safe_place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=opposite_symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )
        time_module.sleep(2)
        safe_place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=main_symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )
        last_flip_time = datetime.now()  # update flip time

        return jsonify({"status": "success", "flip": {"exit": opposite_symbol, "enter": main_symbol}})

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# ---------- START SERVER ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
