from flask import Flask, request, jsonify
from kiteconnect import KiteConnect
from config import ZERODHA_API_KEY, ZERODHA_ACCESS_TOKEN, TEST_MODE
from datetime import datetime, time, timedelta
import calendar
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

kite = KiteConnect(api_key=ZERODHA_API_KEY)
kite.set_access_token(ZERODHA_ACCESS_TOKEN)

def is_market_open():
    now = datetime.now().time()
    return time(9, 15) <= now <= time(15, 30)

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
        side = data.get("side")
        option_type = data.get("option_type")
        qty = int(data.get("qty", 105))

        spot = kite.ltp("NSE:BANKNIFTY")["NSE:BANKNIFTY"]["last_price"]
        symbol = get_option_symbol(spot, option_type)

        if TEST_MODE:
            logging.info(f"[TEST MODE] {side} {symbol} x {qty}")
            return jsonify({"status": "test", "symbol": symbol, "qty": qty, "side": side})

        order = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_MIS
        )

        logging.info(f"Order placed: {order}")
        return jsonify({"status": "success", "order_id": order, "symbol": symbol})

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
