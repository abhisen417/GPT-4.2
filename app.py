import os
from flask import Flask, jsonify
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException
from utils import (
    get_all_usdt_symbols,
    calculate_order_quantity,
    place_order_with_trailing,
)

load_dotenv()

app = Flask(__name__)

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRADE_AMOUNT_INR = float(os.getenv("TRADE_AMOUNT_INR", 500))
INR_USDT_RATE = float(os.getenv("INR_USDT_RATE", 83))

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, testnet=USE_TESTNET)
if USE_TESTNET:
    client.API_URL = 'https://testnet.binance.vision/api'

@app.route('/')
def home():
    return jsonify({"status": "Binance Testnet Trading Bot is running with Mirror Market logic."})

@app.route('/run', methods=['GET'])
def run_trading_bot():
    try:
        usdt_pairs = get_all_usdt_symbols(client)
        if not usdt_pairs:
            return jsonify({"error": "No USDT pairs found."}), 400

        # Mirror Market check (BTC/USDT correlation)
        # Implement: For every USDT pair (except BTCUSDT), if correlation > 0.85, trade
        traded_symbols = []
        for symbol in usdt_pairs:
            price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
            usdt_to_trade = TRADE_AMOUNT_INR / INR_USDT_RATE
            quantity = calculate_order_quantity(client, symbol, usdt_to_trade, price)
            if quantity <= 0:
                continue
            order_info = place_order_with_trailing(
                client, symbol, quantity, price, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
            )
            traded_symbols.append({"symbol": symbol, "order": order_info})
        if not traded_symbols:
            return jsonify({"message": "No tradeable symbol found with mirror market filter."}), 200
        return jsonify({"success": True, "trades": traded_symbols})
    except BinanceAPIException as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)