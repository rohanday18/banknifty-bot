from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
import os
ZERODHA_API_KEY = os.environ.get("ZERODHA_API_KEY")
ZERODHA_ACCESS_TOKEN = os.environ.get("ZERODHA_ACCESS_TOKEN")
TEST_MODE = os.environ.get("TEST_MODE", "True") == "True"
from datetime import datetime, time, timedelta
import calendar
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

kite = KiteConnect(api_key=ZERODHA_API_KEY)
kite.set_access_token(ZERODHA_ACCESS_TOKEN)

def safe_place_order(**kwargs):
    for attempt in range(2):  # try up to 2 times
        try:
            kite.place_order(**kwargs)
            return True
        except Exception as e:
            print(f"⚠️ Retry attempt {attempt + 1} failed: {e}")
            time.sleep(1)
    raise Exception("❌ Order failed after 2 attempts")

def safe_ltp(instrument):
    for attempt in range(3):
        try:
            return kite.ltp([instrument])[instrument]["last_price"]
        except Exception as e:
            print(f"⚠️ LTP retry {attempt + 1} failed: {e}")
            time.sleep(1)
    raise Exception("❌ Failed to fetch LTP after 3 attempts.")

def is_market_open():
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    current_time = now.time()
    print(f"🕒 IST Time Now: {current_time}")  # optional: to debug
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

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if not is_market_open():
            return jsonify({"status": "rejected", "reason": "Outside market hours"})

        data = request.get_json()
        print(f"Received webhook payload: {data}")
        action = data.get("action")  # BUY or SELL (not used yet)
        option_type = data.get("type")  # "CE" or "PE"
        qty = int(data.get("qty", 105))

        spot = safe_ltp("NSE:NIFTY BANK")
        main_symbol = get_option_symbol(spot, option_type)

        # Define opposite type and symbol
        opposite_type = "PE" if option_type == "CE" else "CE"
        opposite_symbol = get_option_symbol(spot, opposite_type)

        if TEST_MODE:
             print(f"[TEST MODE] EXIT {opposite_symbol} x {qty}")
             print("[TEST MODE] Waiting 2 seconds...")
             print(f"[TEST MODE] BUY {main_symbol} x {qty}")
             return jsonify({"status": "test", "exit": opposite_symbol, "enter": main_symbol})

        # Step 1: Exit opposite leg
        safe_place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=opposite_symbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )
        logging.info(f"✅ Exited: {opposite_symbol}")

        # Step 2: Wait 2 seconds
        import time
        time.sleep(2)

        # Step 3: Enter new leg
        safe_place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=main_symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_NRML
        )
        logging.info(f"✅ Bought: {main_symbol}")

        return jsonify({"status": "success", "entered": main_symbol, "exited": opposite_symbol})

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
