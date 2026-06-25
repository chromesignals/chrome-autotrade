"""ChromeSignals Auto-Trade Bot — your personal signal executor.

Polls the ChromeSignals signal API and automatically executes trades
on YOUR Webull account. Your API keys stay on YOUR server — ChromeSignals
never sees them.

Required env vars:
    CHROMESIGNALS_API_KEY  — your API key from thechromesignals.com/app/autotrade
    WEBULL_APP_KEY         — your Webull developer app key
    WEBULL_APP_SECRET      — your Webull developer app secret

Optional env vars:
    RISK_PCT               — % of available cash per trade (default: 70)
    MAX_POSITIONS          — max simultaneous positions (default: 5)
    POLL_INTERVAL          — seconds between signal checks (default: 15)
    TELEGRAM_BOT_TOKEN     — your personal Telegram bot for notifications
    TELEGRAM_CHAT_ID       — your Telegram chat ID for notifications
    DRY_RUN                — set to "true" to log signals without trading
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("chromesignals-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = os.getenv("CHROMESIGNALS_API_KEY", "")
WEBULL_APP_KEY = os.getenv("WEBULL_APP_KEY", "")
WEBULL_APP_SECRET = os.getenv("WEBULL_APP_SECRET", "")
RISK_PCT = float(os.getenv("RISK_PCT", "70")) / 100.0
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "15"))
DRY_RUN = os.getenv("DRY_RUN", "").lower() == "true"

SIGNAL_API = "https://www.thechromesignals.com/api/signals/latest"

TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

open_positions: dict[str, dict] = {}
last_signal_ts = ""
trade_count = 0
win_count = 0


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(message: str) -> None:
    logger.info(message)
    if TG_BOT_TOKEN and TG_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TG_CHAT_ID,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception:
            logger.warning("Telegram notification failed")


# ---------------------------------------------------------------------------
# Webull execution
# ---------------------------------------------------------------------------

def get_webull_balance() -> float:
    try:
        from webull import webull_openapi
        client = webull_openapi.APIClient(WEBULL_APP_KEY, WEBULL_APP_SECRET)
        account = client.account.get_account_balance()
        return float(account.get("usableCash", 0))
    except Exception as e:
        logger.error("Balance check failed: %s", e)
        return 0


def place_buy(ticker: str, allocation: float) -> dict | None:
    try:
        from webull import webull_openapi
        client = webull_openapi.APIClient(WEBULL_APP_KEY, WEBULL_APP_SECRET)

        quote = client.market.get_snapshot(ticker)
        price = float(quote.get("lastPrice", 0))
        if price <= 0:
            logger.error("Invalid price for %s: %s", ticker, price)
            return None

        shares = round(allocation / price, 4)
        if shares * price < 5:
            logger.warning("Order too small for %s: $%.2f", ticker, shares * price)
            return None

        result = client.order.place_order({
            "stock_order": {
                "symbol": ticker,
                "order_type": "MKT",
                "side": "BUY",
                "qty": str(shares),
                "entrust_type": "QTY",
                "time_in_force": "DAY",
                "support_trading_session": "CORE",
            }
        })

        fill_price = float(result.get("avgFilledPrice", price))
        filled_qty = float(result.get("filledQuantity", shares))
        order_id = result.get("orderId", "")

        # Place hard stop at -2%
        hard_stop = round(fill_price * 0.98, 2)
        try:
            client.order.place_order({
                "stock_order": {
                    "symbol": ticker,
                    "order_type": "STP",
                    "side": "SELL",
                    "qty": str(filled_qty),
                    "stop_price": str(hard_stop),
                    "entrust_type": "QTY",
                    "time_in_force": "GTC",
                    "support_trading_session": "CORE",
                }
            })
        except Exception:
            logger.warning("Failed to place hard stop for %s", ticker)

        return {
            "order_id": order_id,
            "fill_price": fill_price,
            "shares": filled_qty,
            "hard_stop": hard_stop,
        }
    except Exception as e:
        logger.error("BUY %s failed: %s", ticker, e)
        return None


def place_sell(ticker: str, shares: float) -> dict | None:
    try:
        from webull import webull_openapi
        client = webull_openapi.APIClient(WEBULL_APP_KEY, WEBULL_APP_SECRET)

        # Cancel any open stop orders first
        try:
            orders = client.order.get_open_orders()
            for order in (orders or []):
                if (order.get("symbol") == ticker and
                    order.get("orderType") == "STP" and
                    order.get("status") in ("Working", "Pending")):
                    client.order.cancel_order(order["orderId"])
        except Exception:
            logger.warning("Failed to cancel stop orders for %s", ticker)

        result = client.order.place_order({
            "stock_order": {
                "symbol": ticker,
                "order_type": "MKT",
                "side": "SELL",
                "qty": str(shares),
                "entrust_type": "QTY",
                "time_in_force": "DAY",
                "support_trading_session": "CORE",
            }
        })

        fill_price = float(result.get("avgFilledPrice", 0))
        return {"fill_price": fill_price}
    except Exception as e:
        logger.error("SELL %s failed: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# Signal processing
# ---------------------------------------------------------------------------

def handle_entry(signal: dict) -> None:
    global trade_count
    ticker = signal.get("ticker", "")
    if not ticker:
        return

    if ticker in open_positions:
        logger.info("Already holding %s, skipping entry", ticker)
        return

    if len(open_positions) >= MAX_POSITIONS:
        logger.info("Max positions (%d) reached, skipping %s", MAX_POSITIONS, ticker)
        return

    if DRY_RUN:
        notify(f"[DRY RUN] ENTRY signal: {ticker} @ ${signal.get('entry_price', '?')}")
        return

    balance = get_webull_balance()
    allocation = balance * RISK_PCT
    if allocation < 10:
        notify(f"Insufficient funds for {ticker} (available: ${balance:.2f})")
        return

    fill = place_buy(ticker, allocation)
    if not fill:
        notify(f"BUY {ticker} FAILED")
        return

    open_positions[ticker] = {
        "entry_price": fill["fill_price"],
        "shares": fill["shares"],
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "hard_stop": fill["hard_stop"],
        "order_id": fill["order_id"],
    }
    trade_count += 1

    notify(
        f"BUY EXECUTED — {ticker}\n"
        f"{fill['shares']:.4f} shares @ ${fill['fill_price']:.2f}\n"
        f"Hard stop: ${fill['hard_stop']:.2f} (-2%)\n"
        f"Allocation: ${allocation:.2f}"
    )


def handle_exit(signal: dict) -> None:
    global win_count
    ticker = signal.get("ticker", "")
    if not ticker:
        return

    if ticker not in open_positions:
        logger.info("No open position in %s, skipping exit", ticker)
        return

    pos = open_positions[ticker]

    if DRY_RUN:
        notify(f"[DRY RUN] EXIT signal: {ticker} — {signal.get('exit_reason', '?')}")
        return

    fill = place_sell(ticker, pos["shares"])
    if not fill:
        notify(f"SELL {ticker} FAILED — CHECK YOUR ACCOUNT")
        return

    exit_price = fill["fill_price"]
    entry_price = pos["entry_price"]
    pnl_pct = ((exit_price / entry_price) - 1) * 100 if entry_price > 0 else 0
    pnl_usd = pos["shares"] * (exit_price - entry_price)

    if pnl_usd > 0:
        win_count += 1

    del open_positions[ticker]

    icon = "WIN" if pnl_usd > 0 else "LOSS"
    wr = (win_count / trade_count * 100) if trade_count > 0 else 0

    notify(
        f"SELL EXECUTED — {ticker} ({icon})\n"
        f"{pos['shares']:.4f} shares @ ${exit_price:.2f}\n"
        f"P&L: {'+' if pnl_pct > 0 else ''}{pnl_pct:.1f}% (${pnl_usd:+.2f})\n"
        f"Reason: {signal.get('exit_reason', '?')}\n"
        f"Record: {trade_count} trades, {wr:.0f}% WR"
    )


def poll_signals() -> None:
    global last_signal_ts

    try:
        params = {"limit": "10"}
        if last_signal_ts:
            params["since"] = last_signal_ts

        resp = requests.get(
            SIGNAL_API,
            headers={"X-API-Key": API_KEY},
            params=params,
            timeout=15,
        )

        if resp.status_code == 401:
            logger.error("API key rejected. Check CHROMESIGNALS_API_KEY.")
            return
        if resp.status_code != 200:
            logger.warning("Signal API returned %d", resp.status_code)
            return

        data = resp.json()
        signals = data.get("signals", [])

        if not signals:
            return

        # Process in chronological order (oldest first)
        for signal in reversed(signals):
            ts = signal.get("timestamp", "")
            if ts <= last_signal_ts:
                continue

            signal_type = signal.get("type", "")
            ticker = signal.get("ticker", "?")
            logger.info("Signal: %s %s @ %s", signal_type, ticker, ts)

            if signal_type == "entry":
                handle_entry(signal)
            elif signal_type == "exit":
                handle_exit(signal)

            last_signal_ts = ts

    except requests.exceptions.ConnectionError:
        logger.warning("Signal API unreachable — will retry")
    except Exception:
        logger.exception("Signal poll failed")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    if not API_KEY:
        logger.error("CHROMESIGNALS_API_KEY not set. Get your key at thechromesignals.com/app/autotrade")
        sys.exit(1)

    if not WEBULL_APP_KEY or not WEBULL_APP_SECRET:
        if not DRY_RUN:
            logger.error("WEBULL_APP_KEY and WEBULL_APP_SECRET required (or set DRY_RUN=true)")
            sys.exit(1)

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    logger.info("ChromeSignals Bot starting (%s)", mode)
    logger.info("Risk: %.0f%% | Max positions: %d | Poll: %ds", RISK_PCT * 100, MAX_POSITIONS, POLL_INTERVAL)

    # Initial connection test
    try:
        resp = requests.get(
            SIGNAL_API,
            headers={"X-API-Key": API_KEY},
            params={"limit": "1"},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Signal API connected")
            notify(f"ChromeSignals Bot started ({mode})\nRisk: {RISK_PCT*100:.0f}% | Max: {MAX_POSITIONS} positions")
        elif resp.status_code == 401:
            logger.error("API key rejected. Check CHROMESIGNALS_API_KEY.")
            sys.exit(1)
        else:
            logger.warning("Signal API returned %d on startup", resp.status_code)
    except Exception as e:
        logger.warning("Could not reach signal API on startup: %s", e)

    while True:
        try:
            poll_signals()
        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except Exception:
            logger.exception("Unexpected error in main loop")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
