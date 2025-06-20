import time
import requests
import numpy as np
import pandas as pd
from binance.exceptions import BinanceAPIException

def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"Telegram alert failed: {e}")

def get_all_usdt_symbols(client):
    try:
        info = client.futures_exchange_info()
        # Exclude BTCUSDT itself (we want to trade other pairs)
        return [
            s['symbol'] for s in info['symbols']
            if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING' and s['symbol'] != 'BTCUSDT'
        ]
    except Exception as e:
        print(f"Error fetching symbols: {e}")
        return []

def calculate_order_quantity(client, symbol, usdt_to_trade, price):
    try:
        info = client.futures_exchange_info()
        symbol_info = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
        if not symbol_info:
            return 0
        step_size = float([
            f['stepSize'] for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'
        ][0])
        quantity = usdt_to_trade / price
        precision = int(round(-np.log10(step_size)))
        quantity = np.floor(quantity * (10 ** precision)) / (10 ** precision)
        return round(quantity, precision)
    except Exception as e:
        print(f"Error calculating quantity: {e}")
        return 0

def place_order_with_trailing(client, symbol, quantity, entry_price, tg_token, tg_chat_id):
    # Mirror Market: Only trade if coin is strongly correlated with BTCUSDT
    try:
        # Get last 50 closes for BTCUSDT and symbol
        leader_klines = client.futures_klines(symbol='BTCUSDT', interval='15m', limit=50)
        leader_df = pd.DataFrame(leader_klines, columns=[
            "open_time","open","high","low","close","volume","close_time","quote_asset_volume",
            "number_of_trades","taker_buy_base","taker_buy_quote","ignore"
        ])
        leader_close = leader_df["close"].astype(float).values

        klines = client.futures_klines(symbol=symbol, interval='15m', limit=50)
        df = pd.DataFrame(klines, columns=[
            "open_time","open","high","low","close","volume","close_time","quote_asset_volume",
            "number_of_trades","taker_buy_base","taker_buy_quote","ignore"
        ])
        target_close = df["close"].astype(float).values

        if len(target_close) == 50 and len(leader_close) == 50:
            corr = np.corrcoef(target_close, leader_close)[0, 1]
        else:
            corr = 0

        if corr < 0.85:
            return {"skipped": True, "reason": "Mirror correlation < 0.85"}

        # Trailing SL/TP logic (1:2 RR default, 1:3 if ADX>30 or strong momentum)
        df = df.astype({"open": float,"high": float,"low": float,"close": float,"volume": float})
        df["tr"] = np.maximum.reduce([
            df["high"] - df["low"],
            np.abs(df["high"] - df["close"].shift(1)),
            np.abs(df["low"] - df["close"].shift(1)),
        ])
        df["+dm"] = np.where(
            (df["high"] - df["high"].shift(1)) > (df["low"].shift(1) - df["low"]),
            np.maximum(df["high"] - df["high"].shift(1), 0), 0
        )
        df["-dm"] = np.where(
            (df["low"].shift(1) - df["low"]) > (df["high"] - df["high"].shift(1)),
            np.maximum(df["low"].shift(1) - df["low"], 0), 0
        )
        period = 14
        df["+di"] = 100 * (df["+dm"].rolling(window=period).sum() / df["tr"].rolling(window=period).sum())
        df["-di"] = 100 * (df["-dm"].rolling(window=period).sum() / df["tr"].rolling(window=period).sum())
        df["dx"] = 100 * np.abs(df["+di"] - df["-di"]) / (df["+di"] + df["-di"])
        df["adx"] = df["dx"].rolling(window=period).mean()
        adx = df["adx"].iloc[-1]
        momentum = (df["close"].iloc[-1] - df["close"].iloc[-4]) / df["close"].iloc[-4]
        if adx > 30 or momentum > 0.03:
            TRAIL_STOP_PERCENT = 0.01   # 1% SL
            TRAIL_PROFIT_PERCENT = 0.03 # 3% TP
            rr_label = "1:3"
        else:
            TRAIL_STOP_PERCENT = 0.01   # 1% SL
            TRAIL_PROFIT_PERCENT = 0.02 # 2% TP
            rr_label = "1:2"
        POLL_INTERVAL = 15
        order = client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=quantity)
        fills = order.get("avgFillPrice")
        if fills:
            entry_filled = float(fills)
        else:
            entry_filled = float(client.futures_get_order(symbol=symbol, orderId=order["orderId"])["avgFillPrice"])
        msg = (
            f"🟢 *TRADE OPENED*\n"
            f"Symbol: `{symbol}`\n"
            f"Side: `BUY`\n"
            f"Entry: `${entry_filled:.4f}`\n"
            f"Quantity: `{quantity}`\n"
            f"Risk:Reward: `{rr_label}`\n"
            f"Trailing SL: `{TRAIL_STOP_PERCENT*100:.2f}%`\n"
            f"Trailing TP: `{TRAIL_PROFIT_PERCENT*100:.2f}%`\n"
        )
        send_telegram_message(tg_token, tg_chat_id, msg)
    except BinanceAPIException as e:
        send_telegram_message(tg_token, tg_chat_id, f"❌ Trade failed: {e}")
        return {"error": str(e)}
    except Exception as e:
        send_telegram_message(tg_token, tg_chat_id, f"❌ Trade failed: {e}")
        return {"error": str(e)}

    highest = entry_filled
    stop_price = entry_filled * (1 - TRAIL_STOP_PERCENT)
    target_price = entry_filled * (1 + TRAIL_PROFIT_PERCENT)
    closed = False
    last_price = entry_filled

    while not closed:
        try:
            last_price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
            if last_price > highest:
                highest = last_price
                stop_price = highest * (1 - TRAIL_STOP_PERCENT)
            if last_price >= target_price:
                if rr_label == "1:3":
                    target_price = last_price * (1 + TRAIL_PROFIT_PERCENT)
                else:
                    client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=quantity, reduceOnly=True)
                    msg = (
                        f"🟢 *TRADE CLOSED (TP)*\n"
                        f"Symbol: `{symbol}`\n"
                        f"Exit: `${last_price:.4f}`\n"
                        f"Peak: `${highest:.4f}`\n"
                        f"TP: `${target_price:.4f}`\n"
                        f"Trailing TP Hit."
                    )
                    send_telegram_message(tg_token, tg_chat_id, msg)
                    closed = True
            if last_price <= stop_price and not closed:
                client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=quantity, reduceOnly=True)
                msg = (
                    f"🔴 *TRADE CLOSED (SL)*\n"
                    f"Symbol: `{symbol}`\n"
                    f"Exit: `${last_price:.4f}`\n"
                    f"Peak: `${highest:.4f}`\n"
                    f"Stop: `${stop_price:.4f}`\n"
                    f"Trailing SL Hit."
                )
                send_telegram_message(tg_token, tg_chat_id, msg)
                closed = True
            time.sleep(POLL_INTERVAL)
        except BinanceAPIException as e:
            send_telegram_message(tg_token, tg_chat_id, f"❌ Polling failed: {e}")
            break
        except Exception as e:
            send_telegram_message(tg_token, tg_chat_id, f"❌ Polling failed: {e}")
            break
    return {
        "symbol": symbol,
        "entry": entry_filled,
        "quantity": quantity,
        "exit": last_price,
        "peak": highest,
        "stop": stop_price,
        "closed": closed,
    }