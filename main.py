"""ChromeSignals Auto-Trade Bot — your personal signal executor.

Polls the ChromeSignals signal API and automatically executes trades
on YOUR Webull account. Your API keys stay on YOUR server — ChromeSignals
never sees them.

Required env vars:
    CHROMESIGNALS_API_KEY  — your API key from thechromesignals.com/app/autotrade
    WEBULL_APP_KEY         — your Webull developer app key
    WEBULL_APP_SECRET      — your Webull developer app secret
"""
import logging
import os
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
POLL_INTERVAL = 15

SIGNAL_API = "https://www.thechromesignals.com/api/signals/latest"
CONFIRM_API = "https://www.thechromesignals.com/api/signals/confirm"

# Settings synced from dashboard every poll cycle
risk_pct = 0.70
max_positions = 5


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

open_positions: dict[str, dict] = {}
last_signal_ts = ""
trade_count = 0
win_count = 0


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
# Execution confirmation
# ---------------------------------------------------------------------------

def confirm_execution(signal_type: str, ticker: str, fill_data: dict,
                      pnl_pct: float | None = None, exit_reason: str | None = None) -> None:
    """POST execution details back to ChromeSignals for tracking and notifications."""
    try:
        body = {
            "type": signal_type,
            "ticker": ticker,
            "price": fill_data.get("fill_price", 0),
            "shares": fill_data.get("shares", 0),
            "order_id": fill_data.get("order_id", ""),
        }
        if pnl_pct is not None:
            body["pnl_pct"] = round(pnl_pct, 2)
        if exit_reason is not None:
            body["exit_reason"] = exit_reason

        resp = requests.post(
            CONFIRM_API,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Confirmed %s %s to ChromeSignals", signal_type, ticker)
        else:
            logger.warning("Confirmation returned %d for %s %s", resp.status_code, signal_type, ticker)
    except Exception as e:
        logger.warning("Confirmation failed for %s %s: %s (non-blocking)", signal_type, ticker, e)


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

    if len(open_positions) >= max_positions:
        logger.info("Max positions (%d) reached, skipping %s", max_positions, ticker)
        return

    balance = get_webull_balance()
    allocation = balance * risk_pct
    if allocation < 10:
        logger.info("Insufficient funds for %s (available: $%.2f)", ticker, balance)
        return

    fill = place_buy(ticker, allocation)
    if not fill:
        logger.error("BUY %s FAILED", ticker)
        return

    open_positions[ticker] = {
        "entry_price": fill["fill_price"],
        "shares": fill["shares"],
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "hard_stop": fill["hard_stop"],
        "order_id": fill["order_id"],
    }
    trade_count += 1

    logger.info(
        "BUY EXECUTED — %s | %.4f shares @ $%.2f | Hard stop: $%.2f (-2%%) | Allocation: $%.2f",
        ticker, fill['shares'], fill['fill_price'], fill['hard_stop'], allocation
    )

    confirm_execution("entry", ticker, fill)


def handle_exit(signal: dict) -> None:
    global win_count
    ticker = signal.get("ticker", "")
    if not ticker:
        return

    if ticker not in open_positions:
        logger.info("No open position in %s, skipping exit", ticker)
        return

    pos = open_positions[ticker]

    fill = place_sell(ticker, pos["shares"])
    if not fill:
        logger.error("SELL %s FAILED — CHECK YOUR ACCOUNT", ticker)
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

    logger.info(
        "SELL EXECUTED — %s (%s) | %.4f shares @ $%.2f | P&L: %+.1f%% ($%+.2f) | %s | %d trades, %.0f%% WR",
        ticker, icon, pos['shares'], exit_price, pnl_pct, pnl_usd,
        signal.get('exit_reason', '?'), trade_count, wr
    )

    confirm_execution("exit", ticker, {"fill_price": exit_price, "shares": pos["shares"], "order_id": pos.get("order_id", "")}, pnl_pct=pnl_pct, exit_reason=signal.get("exit_reason", "unknown"))


def poll_signals() -> None:
    global last_signal_ts, risk_pct, max_positions

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

        # Apply settings synced from dashboard
        settings = data.get("settings")
        if settings:
            new_risk = settings.get("risk_pct")
            new_max = settings.get("max_positions")
            if new_risk is not None:
                new_risk_dec = float(new_risk) / 100.0
                if abs(new_risk_dec - risk_pct) > 0.001:
                    logger.info("Risk updated: %.0f%% -> %.0f%%", risk_pct * 100, new_risk_dec * 100)
                    risk_pct = new_risk_dec
            if new_max is not None:
                new_max = int(new_max)
                if new_max != max_positions:
                    logger.info("Max positions updated: %d -> %d", max_positions, new_max)
                    max_positions = new_max

        # Check for kill switch
        if data.get("kill"):
            logger.warning("KILL SWITCH ACTIVATED — closing all positions and stopping")
            for ticker in list(open_positions.keys()):
                pos = open_positions[ticker]
                fill = place_sell(ticker, pos["shares"])
                if fill:
                    exit_price = fill["fill_price"]
                    pnl_pct = ((exit_price / pos["entry_price"]) - 1) * 100
                    logger.info("KILL SELL — %s @ $%.2f (%+.1f%%)", ticker, exit_price, pnl_pct)
                    confirm_execution("exit", ticker, fill, pnl_pct=pnl_pct, exit_reason="kill_switch")
                    del open_positions[ticker]
                else:
                    logger.error("KILL SELL FAILED for %s — MANUAL INTERVENTION NEEDED", ticker)
            # Stop polling until resumed
            logger.warning("Bot stopped. Will check for resume every 60 seconds.")
            while True:
                time.sleep(60)
                try:
                    resp = requests.get(SIGNAL_API, headers={"X-API-Key": API_KEY}, params={"limit": "1"}, timeout=15)
                    if resp.status_code == 200 and not resp.json().get("kill"):
                        logger.info("RESUME signal received — restarting normal operation")
                        break
                except Exception:
                    pass
            return

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
        logger.error(
            "CHROMESIGNALS_API_KEY not set.\n"
            "  1. Get your key at thechromesignals.com/app/autotrade\n"
            "  2. Add it as an env var in Railway: CHROMESIGNALS_API_KEY=cs_live_xxx\n"
            "  3. Railway will auto-restart this service.\n\n"
            "Waiting 5 minutes before checking again..."
        )
        time.sleep(300)
        return

    if not WEBULL_APP_KEY or not WEBULL_APP_SECRET:
        logger.error(
            "WEBULL_APP_KEY and WEBULL_APP_SECRET not set.\n"
            "  Add your Webull developer credentials as env vars in Railway.\n"
            "  Waiting 5 minutes before checking again..."
        )
        time.sleep(300)
        return

    logger.info("ChromeSignals Bot starting (LIVE)")
    logger.info("Risk: %.0f%% | Max positions: %d | Poll: %ds (syncs from dashboard)", risk_pct * 100, max_positions, POLL_INTERVAL)

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
        elif resp.status_code == 401:
            logger.error(
                "API key rejected. Check CHROMESIGNALS_API_KEY.\n"
                "  Get your key at thechromesignals.com/app/autotrade\n"
                "  Waiting 5 minutes before retrying..."
            )
            time.sleep(300)
            return
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
    while True:
        main()
        logger.info("Restarting in 10 seconds...")
        time.sleep(10)
