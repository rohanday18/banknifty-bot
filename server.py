import os
import json
import logging
import time
from flask import Flask, request, jsonify
from kiteconnect import KiteConnect

# ========================
# Flask app
# ========================
app = Flask(__name__)

# ========================
# Logging setup
# ========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ========================
# Config
# ========================
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
API_KEY = os.environ.get("API_KEY")
API_SECRET = os.environ.get("API_SECRET")

kite = None
if not TEST_MODE:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

# Hardcoded quantity (can later make env-based)
QTY = int(os.environ.get("QTY", "35"))

# Track current side (CE / PE / None)
current_position = None


# ========================
# Helpers
# ========================
def log_action(msg, level="info"):
    if level == "error":
        logging.error(msg)
    else:
        logging.info(msg)


def place_order(tradingsymbol, side, qty=QTY, retries=2):
    """Places order with retry logic"""
    global current_position

    if TEST_MODE:
        log_action(f"[TEST] {side.upper()} {tradingsymbol} x {qty}")
        time.sleep(1)
        return True

    for attempt in range(1, retries + 1):
        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=tradingsymbol,
                transaction_type=kite.TRANSACTION_TYPE_BUY if side == "BUY" else kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET
            )
            log_action(f"✅ Order success: {side} {tradingsymbol} x {qty} (ID: {order_id})")
            return True
        except Exception as e:
            log_action(f"⚠️ Attempt {attempt} failed: {e}", "error")
            time.sleep(attempt)  # wait 1s, then 2s before retry

    log_action(f"❌ Order failed after {retries} attempts for {tradingsymbol}", "error")
    return False


def exit_position(tradingsymbol, qty=QTY):
    """Exit current position"""
    if TEST_MODE:
        log_action(f"[TEST] EXIT {tradingsymbol} x {qty}")
        time.sleep(1)
        return True

    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=tradingsymbol,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET
        )
        log_action(f"✅ Exit success: {tradingsymbol} x {qty} (ID: {order_id})")
        return True
    except Exception as e:
        log_action(f"❌ Exit failed: {e}", "error")
        return False


# ========================
# Routes
# ========================
@app.route("/", methods=["GET"])
def home():
    """Health check route"""
    return jsonify({"status": "running", "mode": "TEST" if TEST_MODE else "LIVE"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle alerts from TradingView"""
    global current_position

    data = request.get_json()
    log_action(f"📩 Webhook: {data}")

    if not data or "type" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    signal = data["type"].upper()
    if signal not in ["CE", "PE"]:
        return jsonify({"error": "Invalid signal"}), 400

    if current_position == signal:
        log_action(f"🔁 Already in {signal}, skipping")
        return jsonify({"status": "skipped"}), 200

    tradingsymbol = "BANKNIFTY25AUG" + signal

    if current_position and current_position != signal:
        exit_symbol = "BANKNIFTY25AUG" + current_position
        log_action(f"🚪 Exiting {current_position} → switching to {signal}")
        exit_position(exit_symbol, QTY)

    log_action(f"➡️ Entering {signal} → {tradingsymbol} (qty={QTY})")
    success = place_order(tradingsymbol, "BUY", QTY)

    if success:
        current_position = signal

    return jsonify({"status": "ok", "success": success}), 200


@app.route("/view_positions", methods=["GET"])
def view_positions():
    """View current positions"""
    global current_position

    if TEST_MODE:
        log_action(f"[TEST] Positions: {current_position}")
        return jsonify({"positions": current_position})

    try:
        positions = kite.positions()
        net = positions["net"]
        active = [p for p in net if p["quantity"] != 0]
        log_action(f"✅ Live positions: {active}")
        return jsonify({"positions": active})
    except Exception as e:
        log_action(f"⚠️ Fetch positions failed: {e}", "error")
        return jsonify({"error": str(e)}), 500


# ========================
# Main
# ========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
