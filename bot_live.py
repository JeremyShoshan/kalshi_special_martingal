import os
import re
import atexit
import argparse
import builtins
import asyncio
import httpx
import websockets
import uuid
import json
import time
from datetime import datetime, timezone
from typing import Any
class MarketNotFoundError(Exception):
    """Raised when Kalshi API returns 404 market_not_found."""
    pass


def get_consecutive_streak(outcomes: list[str]) -> tuple[int, str | None]:
    """Return (streak_length, streak_side) from the tail of Up/Down outcomes."""
    streak_side: str | None = None
    streak_len = 0
    for out in reversed(outcomes):
        if out not in ("Up", "Down"):
            continue
        if streak_side is None:
            streak_side = out
            streak_len = 1
        elif out == streak_side:
            streak_len += 1
        else:
            break
    return streak_len, streak_side


DEFAULT_PATTERN = ["FADE", "FOLLOW", "FADE"]


def normalize_pattern(pattern: Any) -> list[str]:
    """Parse/validate a campaign pattern (list or 'FADE,FADE,FOLLOW' string)."""
    if pattern is None:
        return list(DEFAULT_PATTERN)
    if isinstance(pattern, str):
        pattern = [p for p in re.split(r"[,\s\-]+", pattern) if p]
    out = [str(p).strip().upper() for p in pattern]
    if not out or any(p not in ("FADE", "FOLLOW") for p in out):
        raise ValueError(f"Invalid pattern {pattern!r}: use only FADE / FOLLOW")
    return out


def get_campaign_bet(
    campaign_step: int,
    campaign_trigger_dir: str,
    pattern: list[str] | None = None,
    stake_multipliers: list[float] | None = None,
    base_stake_usd: float = 10.0,
    max_cap: int = 3,
) -> tuple[str, str, float]:
    """Return (action_type, bet_side, stake_usd) for a campaign step."""
    pattern = normalize_pattern(pattern)
    if stake_multipliers is None:
        stake_multipliers = [1.0, 2.0, 4.0]
    step = max(0, min(campaign_step, max_cap - 1))
    mult = stake_multipliers[step] if step < len(stake_multipliers) else (2.0 ** step)
    stake = round(base_stake_usd * mult, 2)
    action_type = pattern[step] if step < len(pattern) else pattern[-1]
    fade_side = "Down" if campaign_trigger_dir == "Up" else "Up"
    bet_side = campaign_trigger_dir if action_type == "FOLLOW" else fade_side
    return action_type, bet_side, stake


def check_special_martingale_trigger(
    outcomes: list[str],
    consecutive_streak_count: int = 2,
    in_campaign: bool = False,
    campaign_step: int = 0,
    campaign_trigger_dir: str | None = None,
    max_cap: int = 3,
    stake_multipliers: list[float] | None = None,
    base_stake_usd: float = 10.0,
    pattern: list[str] | None = None,
) -> tuple[bool, str | None, str | None, float, int, str | None]:
    """
    Evaluates Special Martingale Pattern C: Trend Transition Engine.
    
    Pattern sequence:
    - Step 0 (Step 1/3): FADE the 2-candle streak (Stake: 1.0x).
      Exploits the 52.4% mean-reversion rate of 2-candle streaks in 15m BTC.
    - Step 1 (Step 2/3): FOLLOW the streak direction (Stake: 2.0x).
      If Step 0 lost, the streak reached 3 consecutive candles. Trend confirmed! Ride it.
    - Step 2 (Step 3/3): FADE the streak direction (Stake: 4.0x).
      If Step 1 lost, the streak exhausted at candle 4. Fade exhaustion.
    Pattern is configurable via ``pattern`` (default FADE, FOLLOW, FADE).
    e.g. ["FADE", "FADE", "FOLLOW"] = Streak-3 Double-Fade Breakout:
    fade the streak twice, then follow it if it keeps extending.
        
    Returns:
        (triggered, bet_side, action_type, stake_usd, streak_len, streak_side)
    """
    if stake_multipliers is None:
        stake_multipliers = [1.0, 2.0, 4.0]
    pattern = normalize_pattern(pattern)

    streak_len, streak_side = get_consecutive_streak(outcomes)

    if in_campaign and campaign_trigger_dir is not None:
        action_type, bet_side, stake = get_campaign_bet(
            campaign_step, campaign_trigger_dir, pattern,
            stake_multipliers, base_stake_usd, max_cap,
        )
        return True, bet_side, action_type, stake, streak_len, streak_side

    # Not currently in active campaign: trigger only on a fresh streak of exactly N
    if streak_len == consecutive_streak_count and streak_side is not None:
        action_type, bet_side, stake = get_campaign_bet(
            0, streak_side, pattern, stake_multipliers, base_stake_usd, max_cap,
        )
        return True, bet_side, action_type, stake, streak_len, streak_side

    return False, None, None, 0.0, streak_len, streak_side


def check_streak_trigger(
    outcomes: list[str],
    consecutive_streak_count: int,
    cap: int | None = None,
) -> tuple[bool, str | None, int, str | None]:
    """Compatibility wrapper for legacy streak triggers."""
    streak_len, streak_side = get_consecutive_streak(outcomes)
    if streak_len >= consecutive_streak_count and streak_side is not None:
        if cap is not None and cap > 0 and streak_len >= consecutive_streak_count + cap:
            return False, None, streak_len, streak_side
        fade_side = "Down" if streak_side == "Up" else "Up"
        return True, fade_side, streak_len, streak_side
    return False, None, streak_len, streak_side

# Prepend timestamp to prints
_orig_print = builtins.print

_log_file_handle = None
_log_file_size = 0
_log_file_index = 0
_log_file_path = None

def write_to_log(message, end="\n"):
    global _log_file_handle, _log_file_size, _log_file_index, _log_file_path
    if not _log_file_path:
        return

    text = message + end
    encoded_text = text.encode("utf-8")
    text_len = len(encoded_text)

    # 5MB in bytes
    max_size = 5 * 1024 * 1024

    if _log_file_handle is None or (_log_file_size + text_len > max_size):
        if _log_file_handle:
            try:
                _log_file_handle.close()
            except Exception:
                pass
            _log_file_handle = None

        _log_file_index += 1
        if _log_file_index == 1:
            file_path = _log_file_path
        else:
            base_path, ext = os.path.splitext(_log_file_path)
            file_path = f"{base_path}_{_log_file_index}{ext}"

        _log_file_handle = open(file_path, "ab")
        _log_file_size = 0

    _log_file_handle.write(encoded_text)
    _log_file_handle.flush()
    _log_file_size += text_len

def cleanup_log():
    global _log_file_handle
    if _log_file_handle:
        try:
            _log_file_handle.close()
        except Exception:
            pass

atexit.register(cleanup_log)

def custom_print(*args, **kwargs):
    dt = datetime.now()
    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int(dt.microsecond / 1000):03d}"
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = sep.join(str(arg) for arg in args)

    leading_newlines = 0
    while message.startswith("\n"):
        leading_newlines += 1
        message = message[1:]

    lines = message.split("\n")
    formatted_lines = []
    for i, line in enumerate(lines):
        if i == len(lines) - 1 and not line:
            formatted_lines.append("")
        else:
            formatted_lines.append(f"[{ts_str}] {line}")

    final_message = ("\n" * leading_newlines) + "\n".join(formatted_lines)

    # Write to rotating file log in real mode
    write_to_log(final_message, end)

    kwargs_to_pass = {k: v for k, v in kwargs.items() if k not in ("sep", "end")}
    try:
        _orig_print(final_message, end=end, **kwargs_to_pass)
    except UnicodeEncodeError:
        safe_message = final_message.encode("ascii", errors="replace").decode("ascii")
        _orig_print(safe_message, end=end, **kwargs_to_pass)

builtins.print = custom_print

from lib.trade_bot import (
    TradeBot,
    BotMode,
    utc_beat_start_ts,
    calculate_dynamic_stake,
)
from lib.kalshi_data_fetch import KalshiDataFetcher
from lib.kalshi_client import KalshiClient
from lib.kalshi_auth import load_private_key_from_env, sign_pss_text
from lib.kalshi_env import rest_base_url, ws_url
from lib.kalshi_market import parse_token_id, token_id_for_side
from lib.volatility_filter import (
    check_volatility_filter,
    fetch_binance_15m_delta_range,
    parse_asymmetric_config,
    fetch_binance_15m_streak_candles,
    parse_dynamic_filter_rules,
    evaluate_dynamic_rules,
)

def send_telegram_notification(message: str, parse_mode: str | None = None):
    """
    Imports and calls the telegram notification utility in the notification workspace.
    """
    try:
        import sys
        notification_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "notification"))
        if notification_path not in sys.path:
            sys.path.append(notification_path)
        from send_notification import send_telegram_message
        send_telegram_message(message, parse_mode=parse_mode)
    except Exception as e:
        print(f"[TELEGRAM ERROR] Failed to send notification: {e}")

def format_masked_amount(val: float, sign: bool = False) -> str:
    """
    Mask traces for Telegram: divide amount by 1,000 and display with up to 3 decimal places
    (trimming trailing zeros, e.g. $2,500 -> $2.5, $192,754 -> $192.754, $3,300 -> $3.3).
    """
    d = val / 1000.0
    if sign:
        s = f"{d:+.3f}"
        sign_char = s[0]
        num_part = s[1:].rstrip("0").rstrip(".")
        if not num_part:
            num_part = "0"
        return f"{sign_char}{num_part}"
    else:
        s = f"{d:.3f}".rstrip("0").rstrip(".")
        return s if s and s != "-" else "0"

async def get_portfolio_value(client: KalshiClient | None) -> float:
    """Fetch total portfolio value (cash + positions) from Kalshi."""
    if client is None:
        return 0.0
    try:
        return await asyncio.to_thread(client.get_portfolio_value)
    except Exception as e:
        print(f"         [WARNING] Failed to fetch portfolio value: {e}")
        return 0.0

# Keep strong references to background tasks to prevent garbage collection
background_tasks = set()

# Kalshi WebSocket market data variables
ws_book_cache = {}  # token_id -> { "best_bid": float, "best_ask": float, "last_updated": float }
subscribed_assets = set()
_kalshi_ws_auth: dict[str, Any] | None = None
_http_async_client = httpx.AsyncClient(
    limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=10.0),
    timeout=3.0
)
resubscribe_event = asyncio.Event()

def _build_kalshi_ws_headers() -> dict[str, str]:
    global _kalshi_ws_auth
    if _kalshi_ws_auth is None:
        private_key = load_private_key_from_env()
        api_key_id = os.environ["KALSHI_API_KEY_ID"]
        timestamp = str(int(time.time() * 1000))
        sign_path = "/trade-api/ws/v2"
        msg_string = timestamp + "GET" + sign_path
        signature = sign_pss_text(private_key, msg_string)
        _kalshi_ws_auth = {
            "KALSHI-ACCESS-KEY": api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }
    return _kalshi_ws_auth


def _update_ws_cache_from_yes_prices(market_ticker: str, yes_bid: float, yes_ask: float):
    up_token = token_id_for_side(market_ticker, "Up")
    down_token = token_id_for_side(market_ticker, "Down")
    now = time.time()
    ws_book_cache[up_token] = {
        "best_bid": yes_bid,
        "best_ask": yes_ask,
        "last_updated": now,
    }
    ws_book_cache[down_token] = {
        "best_bid": 1.0 - yes_ask,
        "best_ask": 1.0 - yes_bid,
        "last_updated": now,
    }


async def kalshi_ws_listener_loop():
    """Background loop that connects to the Kalshi WebSocket and maintains the best_bid_ask cache."""
    global subscribed_assets, _kalshi_ws_auth
    ws_msg_id = 1

    while True:
        try:
            while not subscribed_assets:
                await asyncio.sleep(0.1)

            _kalshi_ws_auth = None
            headers = _build_kalshi_ws_headers()
            print("🔌 Connecting to Kalshi WebSocket...")
            async with websockets.connect(ws_url(), additional_headers=headers, ping_interval=20, ping_timeout=None) as ws:
                print("✅ Connected to Kalshi WebSocket")

                async def sender():
                    nonlocal ws_msg_id
                    while True:
                        await resubscribe_event.wait()
                        resubscribe_event.clear()
                        if subscribed_assets:
                            market_tickers = sorted({parse_token_id(tid)[0] for tid in subscribed_assets})
                            sub_msg = {
                                "id": ws_msg_id,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["ticker"],
                                    "market_tickers": market_tickers,
                                },
                            }
                            ws_msg_id += 1
                            await ws.send(json.dumps(sub_msg))
                            print(f"📡 Subscribed to Kalshi tickers: {market_tickers}")

                sender_task = asyncio.create_task(sender())

                if subscribed_assets:
                    resubscribe_event.set()

                try:
                    while True:
                        msg_str = await ws.recv()
                        if not msg_str or not msg_str.strip():
                            continue

                        try:
                            data = json.loads(msg_str)
                        except json.JSONDecodeError:
                            continue

                        if data.get("type") == "ticker":
                            msg = data.get("msg", {})
                            market_ticker = msg.get("market_ticker")
                            yes_bid = msg.get("yes_bid_dollars")
                            yes_ask = msg.get("yes_ask_dollars")
                            if market_ticker and yes_bid is not None and yes_ask is not None:
                                _update_ws_cache_from_yes_prices(
                                    market_ticker,
                                    float(yes_bid),
                                    float(yes_ask),
                                )
                finally:
                    sender_task.cancel()
        except Exception as e:
            err_name = type(e).__name__
            print(f"         [WS DISCONNECT] Kalshi WS connection closed ({err_name}: {e}). Reconnecting in 1.5s...")
            _kalshi_ws_auth = None
            ws_book_cache.clear()
            await asyncio.sleep(1.5)

def clean_book_prices(bid: float | None, ask: float | None) -> tuple[float, float]:
    b = float(bid) if bid is not None and bid >= 0 else None
    a = float(ask) if ask is not None and ask >= 0 else None

    if b is not None and a is not None:
        return b, a
    elif a is not None:
        return 0.00, a
    elif b is not None:
        return b, 1.00
    else:
        return 0.50, 0.50

async def get_token_book_prices(fetcher: KalshiDataFetcher, token_id: str) -> tuple[float, float]:
    """Get the current best bid and best ask for a token ID from WS cache, falling back to REST orderbook."""
    now = time.time()
    if token_id in ws_book_cache:
        cache_entry = ws_book_cache[token_id]
        stale_sec = now - cache_entry["last_updated"]
        if stale_sec <= 15.0:
            return clean_book_prices(cache_entry.get("best_bid"), cache_entry.get("best_ask"))

    # Fallback to REST GET /markets/{ticker}/orderbook
    try:
        market_ticker, outcome = parse_token_id(token_id)
        side = "Up" if outcome == "yes" else "Down"
        resp = await _http_async_client.get(
            f"{fetcher.base_url}/markets/{market_ticker}/orderbook"
        )
        if resp.status_code == 200:
            book = resp.json()
            best_bid, best_ask = KalshiDataFetcher.best_prices_from_orderbook(book, side)
            return clean_book_prices(best_bid, best_ask)
        elif resp.status_code == 429:
            print(f"         [RATE LIMIT BACKOFF] HTTP 429 fetching orderbook for {market_ticker}. Backing off 3.0s...")
            await asyncio.sleep(3.0)
        else:
            print(f"         [WARNING] Error fetching Kalshi orderbook for {market_ticker} (HTTP {resp.status_code})")
    except Exception as e:
        if "429" in str(e):
            print(f"         [RATE LIMIT BACKOFF] HTTP 429 fetching orderbook for {token_id[:16]}. Backing off 3.0s...")
            await asyncio.sleep(3.0)
        else:
            print(f"         [WARNING] Error fetching Kalshi orderbook for {token_id[:16]} via REST: {e}")

    if token_id in ws_book_cache:
        cache_entry = ws_book_cache[token_id]
        return clean_book_prices(cache_entry.get("best_bid"), cache_entry.get("best_ask"))

    return 0.50, 0.50



def adjust_shares_to_minimum(shares: float, price: float, label: str) -> float:
    min_shares = max(1.0, 2.0 / price) if price > 0 else 1.0
    if shares < min_shares:
        print(f"         [{label}] Calculated contracts ({shares:.4f}) is below Kalshi minimum of {min_shares:.1f}. Scaling up to {min_shares:.4f} contracts.")
        return min_shares
    return shares

async def cancel_bot_order(
    mode: BotMode,
    client: KalshiClient | None,
    order_id: str,
    market_ticker_or_token: str | None = None,
) -> None:
    """Cancel a limit order on Kalshi. Handles dry_run correctly."""
    if mode == BotMode.DRY_RUN:
        print(f"         [DRY RUN] Would cancel order: {order_id[:8]}...")
        return

    if client is not None:
        try:
            ticker = None
            if market_ticker_or_token:
                ticker = parse_token_id(market_ticker_or_token)[0] if "|" in market_ticker_or_token else market_ticker_or_token
            res = await asyncio.to_thread(client.cancel_order, order_id, exchange_index=2, market_ticker=ticker)
            if isinstance(res, dict) and res.get("status") == "not_found":
                print(f"         [INFO] Cancel skipped for order {order_id[:8]}: order is no longer resting (already filled or canceled).")
        except Exception as e:
            err_msg = str(e).lower()
            if "404" in err_msg or "not_found" in err_msg or "not found" in err_msg:
                print(f"         [INFO] Cancel skipped for order {order_id[:8]}: order is no longer resting (already filled or canceled).")
            else:
                raise e

async def cancel_remaining_fade_orders(mode: BotMode, client: KalshiClient | None, fade_orders: dict, current_beat_start: int):
    """Cancel all future pre-placed orders in fade_orders (i.e. those starting after current_beat_start)."""
    for beat_ts, ord_info in list(fade_orders.items()):
        if beat_ts > current_beat_start:
            order_id = ord_info.get("order_id")
            if order_id:
                try:
                    if mode == BotMode.REAL:
                        print(f"         [CANCEL FADE ORDER] Canceling future pre-placed order {order_id[:8]} for beat {beat_ts}...")
                    await cancel_bot_order(mode, client, order_id)
                except Exception as e:
                    print(f"         [WARNING] Failed to cancel future order {order_id[:8]}: {e}")

async def connection_keepalive_loop(client: KalshiClient):
    """Periodically ping the Kalshi API to keep the TCP/TLS connection warm."""
    while True:
        try:
            await asyncio.to_thread(client.get_ok)
        except Exception:
            pass
        await asyncio.sleep(15)

async def check_order_fill(
    mode: BotMode,
    client: KalshiClient | None,
    order_id: str,
    original_shares: float,
    fallback_price: float,
    known_filled_shares: float = 0.0,
    known_avg_price: float | None = None
) -> tuple[float, str, float]:
    """Check order status, fill size, and actual average fill price with network retry and local fill preservation."""
    if mode == BotMode.DRY_RUN:
        if known_filled_shares > 0:
            status = "matched" if known_filled_shares >= (original_shares - 0.01) else "partially_matched"
            return known_filled_shares, status, known_avg_price if known_avg_price else fallback_price
        return 0.0, "canceled", fallback_price

    assert client is not None
    last_err = None
    for attempt in range(3):
        try:
            resp = await asyncio.to_thread(client._client.get_order, order_id)

            if not isinstance(resp, dict):
                raise ValueError(f"get_order returned invalid response: {resp} (type: {type(resp)})")

            status = resp.get("status", "").lower()
            original_size = float(resp.get("original_size", 0))
            size_matched = float(resp.get("size_matched", 0))
            remaining_size = original_size - size_matched
            actual_avg_price = float(resp.get("avg_price", fallback_price))

            print(f"         [ORDER CHECK] Order {order_id[:8]}... Status: {status} | Size: {remaining_size:.4f} | Matched: {size_matched:.4f} | Avg Price: {actual_avg_price:.4f}")

            if size_matched > 0:
                if status == "matched":
                    print(f"         [FILL SUCCESS] Order {order_id[:8]}... fully filled: {size_matched:.4f} shares")
                else:
                    print(f"         [PARTIAL FILL] Order {order_id[:8]}... partially filled: {size_matched:.4f}/{original_size:.4f} shares")

            if status in ("live", "partially_matched", "open", "resting"):
                try:
                    print(f"         [CANCEL] Canceling unfilled portion of order {order_id[:8]}...")
                    await asyncio.to_thread(client.cancel_order, order_id)

                    final_status = status
                    final_matched = size_matched
                    final_actual_avg_price = actual_avg_price

                    # Poll until terminal confirmation or fill stabilization (up to 2.0 seconds)
                    for poll_attempt in range(1, 11):
                        await asyncio.sleep(0.18)
                        try:
                            final_resp = await asyncio.to_thread(client._client.get_order, order_id)
                            if not isinstance(final_resp, dict):
                                continue

                            cur_status = final_resp.get("status", "").lower()
                            cur_matched = float(final_resp.get("size_matched", 0))
                            cur_avg_price = float(final_resp.get("avg_price", actual_avg_price))

                            if cur_matched > final_matched:
                                final_matched = cur_matched
                                final_actual_avg_price = cur_avg_price
                            final_status = cur_status

                            # Terminal statuses: 'canceled', 'matched', 'executed'
                            if final_status in ("canceled", "matched", "executed"):
                                break
                            elif final_status == "partially_matched" and final_matched > 0 and poll_attempt >= 2:
                                # Order was partially matched and cancel processed
                                break
                        except Exception as poll_err:
                            err_msg = str(poll_err).lower()
                            if "404" in err_msg or "not_found" in err_msg or "not found" in err_msg:
                                final_status = "canceled"
                                break

                    # Protect against stale API reads: never report lower than already known fills
                    if known_filled_shares > final_matched:
                        final_matched = known_filled_shares
                        if known_avg_price:
                            final_actual_avg_price = known_avg_price

                    print(f"         [POST-CANCEL CHECK] Order {order_id[:8]}... Final Status: {final_status} | Final Matched: {final_matched:.4f} | Final Avg Price: {final_actual_avg_price:.4f}")

                    if final_matched > size_matched:
                        additional = final_matched - size_matched
                        print(f"         [FILL UPDATE] Order {order_id[:8]}... filled an additional {additional:.4f} contracts during cancellation (Total filled: {final_matched:.4f}/{original_size:.4f} contracts)")
                    elif final_status == "matched" and status != "matched":
                        print(f"         [FILL SUCCESS] Order {order_id[:8]}... fully filled: {final_matched:.4f} contracts")

                    return final_matched, final_status, final_actual_avg_price
                except Exception as cancel_err:
                    print(f"         [WARNING] Cancel order failed: {cancel_err}")
                    final_status = status
                    final_matched = max(size_matched, known_filled_shares)
                    final_actual_avg_price = actual_avg_price
                    for poll_attempt in range(1, 5):
                        await asyncio.sleep(0.2)
                        try:
                            final_resp = await asyncio.to_thread(client._client.get_order, order_id)
                            if isinstance(final_resp, dict):
                                final_status = final_resp.get("status", "").lower()
                                cur_matched = float(final_resp.get("size_matched", 0))
                                if cur_matched > final_matched:
                                    final_matched = cur_matched
                                final_actual_avg_price = float(final_resp.get("avg_price", actual_avg_price))
                                if final_status in ("canceled", "matched", "executed"):
                                    break
                        except Exception:
                            pass
                    print(f"         [POST-CANCEL ERROR CHECK] Order {order_id[:8]}... status after cancel failure: {final_status} | Matched: {final_matched:.4f}")
                    return final_matched, final_status, final_actual_avg_price

            return size_matched, status, actual_avg_price
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(0.3)

    print(f"         [WARNING] Error querying order {order_id} after 3 attempts: {last_err}")
    # Preserve local fill count if WebSocket or previous poll already captured fills
    if known_filled_shares > 0:
        eff_price = known_avg_price if known_avg_price is not None else fallback_price
        matched_status = "matched" if known_filled_shares >= (original_shares - 0.01) else "partially_matched"
        print(f"         [FILL PRESERVATION] Preserving known local fill: {known_filled_shares:.4f} shares at {eff_price:.4f} despite REST API error")
        return known_filled_shares, matched_status, eff_price
    return 0.0, "unknown", fallback_price



async def check_existing_bot_order(
    client: KalshiClient | None,
    token_id: str,
    client_order_id: str | None = None,
) -> str | None:
    """Check if an open/resting order already exists on Kalshi for token_id/client_order_id. Returns order_id or None."""
    if not client:
        return None

    def _fetch():
        try:
            market_ticker, _ = parse_token_id(token_id)
            orders = client.get_orders(ticker=market_ticker)
            for o in orders:
                oid = o.get("order_id")
                cid = o.get("client_order_id")
                st = str(o.get("status", "")).lower()
                if client_order_id and cid == client_order_id:
                    if st in ("resting", "executed"):
                        return oid
            return None
        except Exception as e:
            print(f"         [WARNING] Could not check existing orders for {token_id[:8]}: {e}")
            return None

    return await asyncio.to_thread(_fetch)


async def place_bot_order(
    mode: BotMode,
    client: KalshiClient | None,
    token_id: str,
    price: float,
    shares: float,
    side: str,
    post_only: bool = True,
    client_order_id: str | None = None,
) -> str | None:
    """Place a limit buy order on Kalshi. Returns order_id or None."""

    if mode == BotMode.DRY_RUN:
        mock_id = f"dry_run_{int(time.time())}"
        cid_str = f" (client_order_id={client_order_id[:8]}...)" if client_order_id else ""
        print(f"         [DRY RUN] Would place Limit Buy order: {shares:.4f} shares of {side} ({token_id[:8]}...) at {price:.2f} (post-only={post_only}){cid_str}")
        return mock_id

    assert client is not None
    cid_log = f" (client_order_id={client_order_id[:8]}...)" if client_order_id else ""
    print(f"         [ORDER SUBMIT] placing Limit Buy: {shares:.4f} shares of {side} ({token_id[:8]}...) at {price:.2f} (post-only={post_only}){cid_log}")
    try:
        resp = await asyncio.to_thread(
            client.place_limit_order,
            token_id=token_id,
            price=price,
            size=shares,
            side=side,
            post_only=post_only,
            client_order_id=client_order_id,
        )
        if resp is None:
            print(f"         [ERROR] Order placement response was None")
            return None
        success = resp.get("success", False)
        order_id = resp.get("orderID")
        if not success or not order_id:
            err_msg = str(resp.get("error", resp))
            if "market_not_found" in err_msg.lower() or "404" in err_msg.lower():
                raise MarketNotFoundError(f"Market for {token_id} not found on Kalshi exchange (404)")
            print(f"         [ERROR] Order placement rejected by Kalshi: {resp}")
            return None
        print(f"         [ORDER SUCCESS] Order placed. ID: {order_id}")
        return order_id
    except MarketNotFoundError:
        raise
    except Exception as e:
        err_str = str(e).lower()
        if "market_not_found" in err_str or "404" in err_str:
            raise MarketNotFoundError(f"Market for {token_id} not found on Kalshi exchange (404)") from e
        if "429" in err_str or "too many requests" in err_str:
            print(f"         [RATE LIMIT BACKOFF] HTTP 429 during order placement. Backing off 5.0s...")
            await asyncio.sleep(5.0)

        # Recover order if Kalshi placed it despite socket/network exception
        if client_order_id and mode == BotMode.REAL:
            recovered_id = await check_existing_bot_order(client, token_id, client_order_id)
            if recovered_id:
                print(f"         [RECOVERED ORDER] Found active order ID {recovered_id} on Kalshi for client_order_id {client_order_id[:8]}... despite exception ({e})")
                return recovered_id

        print(f"         [ERROR] Failed to place order: {e}")
        return None

async def place_bot_sell_order(
    mode: BotMode,
    client: KalshiClient | None,
    token_id: str,
    price: float,
    shares: float,
    side: str,
    post_only: bool = True
) -> str | None:
    """Place a post-only limit sell order on Kalshi (take-profit maker). Returns order_id or None."""
    if mode == BotMode.DRY_RUN:
        mock_id = f"dry_run_tp_{int(time.time())}"
        print(f"         [DRY RUN] Would place Limit Sell (Take-Profit): {shares:.4f} shares of {side} ({token_id[:8]}...) at {price:.2f} (post-only={post_only})")
        return mock_id

    assert client is not None
    print(f"         [TAKE-PROFIT SUBMIT] placing Limit Sell: {shares:.4f} shares of {side} ({token_id[:8]}...) at {price:.2f} (post-only={post_only})")
    try:
        resp = await asyncio.to_thread(
            client.place_limit_order,
            token_id=token_id,
            price=price,
            size=shares,
            side=side,
            post_only=post_only,
            action="sell",
            reduce_only=False,
        )
        if resp is None:
            print(f"         [ERROR] TP Order placement response was None")
            return None
        success = resp.get("success", False)
        order_id = resp.get("orderID")
        if not success or not order_id:
            print(f"         [ERROR] TP Order placement rejected by Kalshi: {resp}")
            return None
        print(f"         [TP ORDER SUCCESS] Take-Profit Limit Sell placed. ID: {order_id}")
        return order_id
    except Exception as e:
        print(f"         [ERROR] Failed to place TP order: {e}")
        return None

async def place_bot_ioc_order(
    mode: BotMode,
    client: KalshiClient | None,
    token_id: str,
    price: float,
    shares: float,
    side: str
) -> str | None:
    """Place an IOC limit buy order on Kalshi. Returns order_id or None."""
    if mode == BotMode.DRY_RUN:
        mock_id = f"dry_run_ioc_{int(time.time())}"
        print(f"         [DRY RUN] Would place IOC Taker order: {shares:.4f} shares of {side} ({token_id[:8]}...) up to {price:.2f}")
        return mock_id

    assert client is not None
    usdc_amount = round(shares * price, 2)
    print(f"         [ORDER SUBMIT] placing IOC Taker (FAK): spend up to ${usdc_amount:.2f} USDC to buy {side} ({token_id[:8]}...) at max price {price:.2f}")
    try:
        resp = await asyncio.to_thread(
            client.buy_outcome_fak,
            token_id=token_id,
            price=price,
            usdc_amount=usdc_amount
        )
        if resp is None:
            print(f"         [ERROR] IOC Order placement response was None")
            return None
        success = resp.get("success", False)
        order_id = resp.get("orderID")
        if not success or not order_id:
            print(f"         [ERROR] IOC Order placement rejected by Kalshi: {resp}")
            return None
        print(f"         [ORDER SUCCESS] IOC Order placed. ID: {order_id}")
        return order_id
    except Exception as e:
        print(f"         [ERROR] Failed to place IOC order: {e}")
        return None

class OrderVerifyResult:
    """Result returned by verify_order_in_book with backward-compatible boolean evaluation."""
    def __init__(
        self,
        verified: bool,
        size_matched: float = 0.0,
        avg_price: float = 0.0,
        status: str = "",
        order_dict: dict[str, Any] | None = None,
    ):
        self.verified = bool(verified)
        self.size_matched = float(size_matched or 0.0)
        self.avg_price = float(avg_price or 0.0)
        self.status = str(status or "")
        self.order_dict = order_dict or {}

    def __bool__(self) -> bool:
        return self.verified

    def __repr__(self) -> str:
        return (
            f"OrderVerifyResult(verified={self.verified}, "
            f"size_matched={self.size_matched:.4f}, avg_price={self.avg_price:.4f}, "
            f"status='{self.status}')"
        )

async def verify_order_in_book(
    mode: BotMode,
    client: KalshiClient | None,
    order_id: str,
) -> OrderVerifyResult:
    """Verify order is active in the order book and capture any immediate fills."""
    if mode == BotMode.DRY_RUN:
        print(f"         [VERIFIED] [DRY RUN] Order {order_id} verified in order book.")
        return OrderVerifyResult(True, size_matched=0.0, avg_price=0.0, status="resting")

    assert client is not None

    max_verify_attempts = 25
    backoff = 0.15
    for attempt in range(1, max_verify_attempts + 1):
        await asyncio.sleep(backoff)
        backoff = min(1.5, backoff * 1.5)
        try:
            resp: Any = await asyncio.to_thread(client._client.get_order, order_id)
            if isinstance(resp, str):
                try:
                    resp = json.loads(resp)
                except Exception:
                    pass

            if resp is None or not isinstance(resp, dict):
                if attempt < max_verify_attempts:
                    print(f"         [WARNING] Order query returned invalid response for {order_id[:8]}...: {resp} (attempt {attempt}/{max_verify_attempts}). Retrying...")
                    continue
                else:
                    print(f"         [WARNING] Order query returned invalid response for {order_id[:8]}...: {resp} after {max_verify_attempts} attempts.")
                    break

            order_data = resp.get("order", resp) if isinstance(resp.get("order"), dict) else resp
            status = order_data.get("status", "").lower()
            size_matched = float(order_data.get("size_matched", 0))
            avg_price_val = order_data.get("avg_price") or order_data.get("avgPrice") or order_data.get("averagePrice") or order_data.get("average_price") or 0.0
            avg_price = float(avg_price_val)

            if status in ("live", "open", "resting", "matched", "partially_matched"):
                matched_info = f" (matched: {size_matched:.4f} @ ${avg_price:.4f})" if size_matched > 0 else ""
                print(f"         [VERIFIED] Order {order_id[:8]}... successfully found in book with status: {status}{matched_info}")
                return OrderVerifyResult(True, size_matched=size_matched, avg_price=avg_price, status=status, order_dict=order_data)
            if status == "canceled" and size_matched > 0:
                print(
                    f"         [VERIFIED] Order {order_id[:8]}... canceled with "
                    f"{size_matched:.4f} contracts filled @ ${avg_price:.4f} (IOC/partial)"
                )
                return OrderVerifyResult(True, size_matched=size_matched, avg_price=avg_price, status=status, order_dict=order_data)
            print(f"         [WARNING] Order status not active in book: {status}")
            break
        except Exception as e:
            if attempt < max_verify_attempts:
                print(f"         [WARNING] Could not verify order {order_id} in book (attempt {attempt}/{max_verify_attempts}): {e}. Retrying...")
            else:
                print(f"         [WARNING] Could not verify order {order_id} in book: {e}")
                break

    # If verification failed, explicitly cancel the order to avoid untracked filled execution.
    try:
        if mode == BotMode.REAL:
            print(f"         [CANCEL] Verification failed. Canceling order {order_id[:8]}...")
        await cancel_bot_order(mode, client, order_id)
    except Exception as cancel_err:
        print(f"         [WARNING] Cancel order failed during verification cleanup: {cancel_err}")
    return OrderVerifyResult(False)

async def initialize_historical_outcomes_cache(
    fetcher: KalshiDataFetcher,
    asset: str = "BTC",
    history_len: int = 50,
    beat_duration_min: int = 5
) -> tuple[dict[int, str], int, int]:
    """
    Fetch recent beats to initialize the historical outcomes cache and return
    initial UP and DOWN counts.
    """
    print(f"⏳ Initializing historical outcomes cache from past {history_len} beats...")
    now = int(time.time())
    current_beat_start = utc_beat_start_ts(now, beat_duration_min)

    # We query the past `history_len` beats before current_beat_start
    beat_duration_sec = beat_duration_min * 60
    timestamps = [current_beat_start - beat_duration_sec * i for i in range(history_len, 0, -1)]
    outcomes = await fetch_historical_outcomes(fetcher, timestamps)

    # Filter out None values
    valid_outcomes = {ts: out for ts, out in outcomes.items() if out in ("Up", "Down")}

    count_up = list(valid_outcomes.values()).count("Up")
    count_down = list(valid_outcomes.values()).count("Down")

    print(f"✅ Initialized outcomes cache: {len(valid_outcomes)} resolved beats | UP = {count_up} | DOWN = {count_down}")
    return valid_outcomes, count_up, count_down

async def fetch_historical_outcomes(
    fetcher: KalshiDataFetcher,
    timestamps: list[int],
) -> dict[int, str | None]:
    """
    Fetch Kalshi market results for a list of beat start timestamps.
    Returns a dict mapping timestamp -> outcome ('Up', 'Down', or None).
    """
    results: dict[int, str | None] = {ts: None for ts in timestamps}
    if not timestamps:
        return results

    beat_duration_sec = fetcher.beat_duration_min * 60
    min_close = min(timestamps) + beat_duration_sec - 60
    max_close = max(timestamps) + beat_duration_sec + 60

    try:
        resp = await _http_async_client.get(
            f"{fetcher.base_url}/markets",
            params={
                "series_ticker": fetcher.series_ticker,
                "min_close_ts": min_close,
                "max_close_ts": max_close,
                "limit": max(50, len(timestamps) * 2),
            },
            timeout=10,
        )
        if resp.status_code == 200:
            markets = resp.json().get("markets", [])
            for ts in timestamps:
                target_close = ts + beat_duration_sec
                for m in markets:
                    close_str = m.get("close_time")
                    if close_str:
                        m_close_ts = int(datetime.fromisoformat(close_str.replace("Z", "+00:00")).timestamp())
                        if abs(m_close_ts - target_close) <= 60:
                            outcome = KalshiDataFetcher.outcome_from_market(m)
                            if outcome:
                                results[ts] = outcome
                                break
    except Exception as e:
        print(f"         [WARNING] Bulk market query failed: {e}")

    # Fallback for any timestamps not resolved by bulk query
    unresolved = [ts for ts, out in results.items() if out is None]
    if unresolved:
        async def fetch_one(ts: int) -> tuple[int, str | None]:
            try:
                market_ticker = fetcher.resolve_market_ticker(ts)
                resp = await _http_async_client.get(
                    f"{fetcher.base_url}/markets/{market_ticker}",
                    timeout=5,
                )
                if resp.status_code == 200:
                    market = resp.json().get("market", {})
                    return ts, KalshiDataFetcher.outcome_from_market(market)
            except Exception:
                pass
            return ts, None

        sem = asyncio.Semaphore(5)
        async def bounded_fetch(ts: int) -> tuple[int, str | None]:
            async with sem:
                return await fetch_one(ts)

        fetched = await asyncio.gather(*(bounded_fetch(ts) for ts in unresolved))
        for ts, outcome in fetched:
            if outcome:
                results[ts] = outcome

    return results

def simulate_state_from_history(
    outcomes: list[tuple[int, str]],  # List of (timestamp, outcome) sorted chronologically
    history_len: int = 50,
    consecutive_streak_count: int = 2,
    cap: int | None = None,
    max_cap: int = 3,
    stake_multipliers: list[float] | None = None,
    base_stake_usd: float = 10.0,
    pattern: list[str] | None = None,
) -> dict:
    """
    Computes lookback counts and campaign state from the most recent history_len resolved outcomes.
    Simulates the ALGO 3 Special Martingale progression through historical outcomes.
    """
    if stake_multipliers is None:
        stake_multipliers = [1.0, 2.0, 4.0]
    pattern = normalize_pattern(pattern)
    if cap is not None and cap > 0:
        max_cap = cap

    resolved = [out for ts, out in outcomes if out in ("Up", "Down")]
    recent = resolved[-history_len:] if history_len else resolved

    count_up = recent.count("Up")
    count_down = recent.count("Down")

    in_camp = False
    step = 0
    t_dir = None
    streak_cnt = 0
    curr_s = None

    for act in recent:
        if act == curr_s:
            streak_cnt += 1
        else:
            curr_s = act
            streak_cnt = 1

        if not in_camp:
            if streak_cnt >= consecutive_streak_count:
                in_camp = True
                step = 0
                t_dir = curr_s
                # Triggered on this candle; next candle will be Step 0 resolution
                continue
        else:
            _, bet_side, _ = get_campaign_bet(
                step, t_dir, pattern, stake_multipliers, base_stake_usd, max_cap
            )

            if act == bet_side:
                # Won!
                in_camp = False
                step = 0
                t_dir = None
            else:
                # Lost step
                step += 1
                if step >= max_cap:
                    # Hard cap reached
                    in_camp = False
                    step = 0
                    t_dir = None

    streak_len, streak_side = get_consecutive_streak(recent)
    triggered, bet_side, action_type, stake, _, _ = check_special_martingale_trigger(
        recent,
        consecutive_streak_count=consecutive_streak_count,
        in_campaign=in_camp,
        campaign_step=step,
        campaign_trigger_dir=t_dir,
        max_cap=max_cap,
        stake_multipliers=stake_multipliers,
        base_stake_usd=base_stake_usd,
        pattern=pattern,
    )

    return {
        "lookback_up": count_up,
        "lookback_down": count_down,
        "streak_len": streak_len,
        "streak_side": streak_side,
        "in_fade_window": triggered,
        "fade_active_side": bet_side,
        "fade_window_remaining_beats": 1 if triggered else 0,
        "in_campaign": in_camp or triggered,
        "campaign_step": step,
        "campaign_trigger_dir": t_dir if in_camp else (streak_side if triggered else None),
        "action_type": action_type,
        "bet_side": bet_side,
        "stake_usd": stake,
        "triggered": triggered,
    }



def calculate_order_price_with_bank(
    best_bid: float | None,
    price_savings_bank: float = 0.0,
    enable_savings_bank: bool = False,
    base_cap: float = 0.50,
    best_ask: float | None = None
) -> tuple[float, float]:
    """
    Returns (calculated_order_price, credit_used_from_bank).
    - If best_bid is None: returns (base_cap, 0.0)
    - If best_ask and best_bid have a spread >= 0.02:
        target_price is placed between them (undercutting Ask by 1c, capped at base_cap).
    - Otherwise target_price = best_bid.
    - Strictly caps target_price at (best_ask - 0.01) to prevent post_only cross rejections.
    - Caps target_price at base_cap, expandable with price_savings_bank credit if enabled.
    """
    if best_bid is None:
        return base_cap, 0.0

    target_price = best_bid
    if best_ask is not None:
        spread = round(best_ask - best_bid, 4)
        if spread >= 0.0199:
            target_price = min(base_cap, round(best_ask - 0.01, 2))
            target_price = max(target_price, round(best_bid + 0.01, 2))

        # Guarantee post-only buy order never equals or exceeds best_ask
        if best_ask > 0.01:
            target_price = min(target_price, round(best_ask - 0.01, 2))
            target_price = max(0.01, target_price)

    if not enable_savings_bank or target_price <= base_cap:
        return round(min(target_price, base_cap), 4), 0.0

    needed = round(target_price - base_cap, 4)
    credit_used = round(min(price_savings_bank, needed), 4)
    max_price_cap = round(base_cap + credit_used, 4)
    order_price = round(min(target_price, max_price_cap), 4)
    return order_price, credit_used


async def run_live_bot(
    mode: BotMode,
    strategy_name: str = "ALGO 3 - Special Martingale",
    max_transactions: int | None = None,
    decisive_up: float = 0.90,
    decisive_down: float = 0.10,
    seconds_before_close: int = 15,
    history_beats_count: int = 50,
    consecutive_streak_count: int = 2,
    base_stake_usd: float = 10.0,
    stake_multipliers: list[float] | None = None,
    max_cap: int = 3,
    cap: int | None = None,
    fade_stake_usd: float | None = None,
    fade_profit_target_usd: float | None = None,
    notifications_enabled: bool = True,
    asset: str = "BTC",
    stop_maker_sec: int | None = 240,
    min_maker_sec: int = 0,
    taker_limit_price: float = 0.55,
    beat_duration_min: int = 15,
    active_order_adjusted_enabled: bool = True,
    enable_price_savings_bank: bool = False,
    max_maker_price: float = 0.52,
    marketable_limit: bool = True,
    state_file: str | None = None,
    reset_state: bool = False,
    dynamic_staking_config: dict | None = None,
    volatility_filter_config: dict | None = None,
    take_profit_config: dict | None = None,
    delta_filter_config: dict | None = None,
    dynamic_filter_config: dict | None = None,
    pattern: list[str] | None = None,
    sync_corrections: bool = False,
):
    global subscribed_assets
    if fade_stake_usd is not None and fade_stake_usd > 0:
        base_stake_usd = fade_stake_usd
    if cap is not None and cap > 0:
        max_cap = cap
    if stake_multipliers is None:
        stake_multipliers = [1.0, 2.0, 4.0]
    pattern = normalize_pattern(pattern)
    if len(pattern) < max_cap or len(stake_multipliers) < max_cap:
        print(f"⚠️ WARNING: pattern ({len(pattern)}) / stake_multipliers ({len(stake_multipliers)}) shorter than max_cap ({max_cap}); missing steps reuse last action / 2^step stake.")
    if not min_maker_sec or min_maker_sec <= 0:
        print("⚠️ WARNING: min_maker_sec = 0 places campaign orders at beat start, before the previous beat settles (step may be stale). Recommended: min_maker_sec >= 15.")

    asset_emoji = "₿" if asset == "BTC" else "⟠" if asset == "ETH" else "🪙"
    starting_balance = 1000.0 if mode == BotMode.DRY_RUN else 0.0
    bot = TradeBot(base_stake_usd, starting_balance=starting_balance)
    fetcher = KalshiDataFetcher(asset=asset, beat_duration_min=beat_duration_min)

    dynamic_staking_enabled = False
    dyn_rolling_window = 8
    dyn_target_wr = 0.515
    dyn_alpha = 1.5
    dyn_min_stake = None
    dyn_max_stake = None
    dyn_max_consec_loss = 3
    if isinstance(dynamic_staking_config, dict) and dynamic_staking_config.get("enabled", False):
        dynamic_staking_enabled = True
        dyn_rolling_window = int(dynamic_staking_config.get("rolling_window_triggers", 8))
        dyn_target_wr = float(dynamic_staking_config.get("target_win_rate", 0.515))
        dyn_alpha = float(dynamic_staking_config.get("sensitivity_alpha", 1.5))
        dyn_min_stake = float(dynamic_staking_config["min_stake_usd"]) if dynamic_staking_config.get("min_stake_usd") is not None else None
        dyn_max_stake = float(dynamic_staking_config["max_stake_usd"]) if dynamic_staking_config.get("max_stake_usd") is not None else None
        dyn_max_consec_loss = int(dynamic_staking_config.get("max_consecutive_loss_cap", 3)) if dynamic_staking_config.get("max_consecutive_loss_cap") is not None else None

    vol_filter_enabled = False
    vol_threshold_pct = 0.13
    vol_lookback_candles = 16
    vol_source = "binance"
    if isinstance(volatility_filter_config, dict) and volatility_filter_config.get("enabled", False):
        vol_filter_enabled = True
        vol_threshold_pct = float(volatility_filter_config.get("threshold_pct", 0.13))
        vol_lookback_candles = int(volatility_filter_config.get("lookback_candles", 16))
        vol_source = str(volatility_filter_config.get("source", "binance")).lower()

    # Dynamic Compound OR Filter Setup
    dyn_filter_enabled = False
    dynamic_rules = []
    dyn_vol_source = "binance"
    if isinstance(dynamic_filter_config, dict) and dynamic_filter_config.get('enabled', False):
        dyn_filter_enabled = True
        dynamic_rules = dynamic_filter_config.get('rules', [])
        dyn_vol_source = str(dynamic_filter_config.get('source', vol_source)).lower()
    # Minimum vol threshold across all dynamic tiers (for log display)
    dyn_min_vol_pct = min((r.get('vol_threshold_pct', 0.0) for r in dynamic_rules), default=0.0) if dynamic_rules else 0.0

    delta_filter_enabled = False
    delta_max_range = 230.0
    delta_ceiling_range = None
    delta_threshold = None
    delta_source = "binance"
    delta_asymmetric = None
    if isinstance(delta_filter_config, dict) and delta_filter_config.get("enabled", False):
        delta_filter_enabled = True
        delta_asymmetric = delta_filter_config.get("asymmetric") or parse_asymmetric_config(delta_filter_config)
        delta_max_range = float(delta_filter_config.get("delta_max_range", delta_filter_config.get("threshold", 230.0))) if (delta_filter_config.get("delta_max_range") is not None or delta_filter_config.get("threshold") is not None) else None
        delta_ceiling_range = float(delta_filter_config.get("range_max", delta_filter_config.get("delta_range_max", delta_filter_config.get("delta_ceiling_range")))) if (delta_filter_config.get("range_max") is not None or delta_filter_config.get("delta_range_max") is not None or delta_filter_config.get("delta_ceiling_range") is not None) else None
        delta_threshold = float(delta_filter_config["delta_threshold"]) if delta_filter_config.get("delta_threshold") is not None else None
        delta_source = str(delta_filter_config.get("source", "binance"))

    tp_enabled = False
    tp_target_pct = 0.14
    if isinstance(take_profit_config, dict) and take_profit_config.get("enabled", False):
        tp_enabled = True
        tp_target_pct = float(take_profit_config.get("target_pct", 0.14))

    has_creds = "KALSHI_API_KEY_ID" in os.environ and (
        "KALSHI_PRIVATE_KEY_PATH" in os.environ or "KALSHI_PRIVATE_KEY" in os.environ
    )
    client = KalshiClient() if (mode == BotMode.REAL or (mode == BotMode.DRY_RUN and has_creds)) else None

    if mode == BotMode.REAL and client is None:
        raise RuntimeError(
            "REAL mode requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH "
            "(or KALSHI_PRIVATE_KEY) in .env"
        )
    print(f"Live Bot Started (Kalshi Consecutive Streak Fade Bot)")
    print(f"Strategy Name: {strategy_name}")
    print(f"Mode: {mode.value}")
    print(f"Asset: {asset}")
    print(f"Kalshi series: {fetcher.series_ticker} | API: {fetcher.base_url}")
    print(f"Verify series ticker on kalshi.com before live trading.")
    if mode == BotMode.DRY_RUN:
        print(f"Starting USDC Balance (DRY RUN): ${starting_balance:.2f}")
    print(f"Decisive Close Thresholds: UP > {decisive_up:.2f} | DOWN < {decisive_down:.2f}")
    print(f"Decision window: {seconds_before_close}s before close")
    cap_info_print = f" | Cap: {cap} bets max per streak" if cap is not None else " | Cap: None (uncapped)"
    print(f"Strategy: Lookback {history_beats_count} beats | Consecutive streak trigger: {consecutive_streak_count}{cap_info_print}")
    pattern_desc = " -> ".join(f"{a}@{(stake_multipliers[i] if i < len(stake_multipliers) else 2.0 ** i):g}x" for i, a in enumerate(pattern[:max_cap]))
    print(f"Campaign Pattern: {pattern_desc} | Sync corrections: {'ON' if sync_corrections else 'OFF'}")

    profit_target_str = f">= ${fade_profit_target_usd:.2f}" if fade_profit_target_usd is not None else "None"
    print(f"Trading Parameters: flat ${fade_stake_usd:.2f} base stake per trigger (Profit Target: {profit_target_str})")
    if dynamic_staking_enabled:
        min_s_str = f"${dyn_min_stake:.2f}" if dyn_min_stake else f"${fade_stake_usd*0.5:.2f}"
        max_s_str = f"${dyn_max_stake:.2f}" if dyn_max_stake else f"${fade_stake_usd*1.5:.2f}"
        print(f"Dynamic Staking: ENABLED | Window: {dyn_rolling_window} triggers | Target WR: {dyn_target_wr*100:.1f}% | Alpha: {dyn_alpha} | Bounds: [{min_s_str}, {max_s_str}] | Loss Cap: {dyn_max_consec_loss}")
    else:
        print(f"Dynamic Staking: DISABLED (Flat ${fade_stake_usd:.2f})")
    if dyn_filter_enabled:
        rule_desc_parts = []
        for r in dynamic_rules:
            parts = []
            if r.get('vol_threshold_pct') is not None:
                parts.append(f"V>={r['vol_threshold_pct']:.2f}%")
            if r.get('vol_max_pct') is not None:
                parts.append(f"V<={r['vol_max_pct']:.2f}%")
            if r.get('delta_max_range') is not None:
                parts.append(f"R>=${r['delta_max_range']:g}")
            if r.get('range_max') is not None:
                parts.append(f"R<=${r['range_max']:g}")
            if r.get('delta_threshold') is not None:
                parts.append(f"B>=${r['delta_threshold']:g}")
            if r.get('body_max') is not None:
                parts.append(f"B<=${r['body_max']:g}")
            rule_desc_parts.append(f"({' & '.join(parts)})")
        rule_desc = ' OR '.join(rule_desc_parts)
        print(f"Dynamic Compound Filter: ENABLED | Vol Source: {dyn_vol_source.upper()} | Rules: {rule_desc}")
    else:
        if vol_filter_enabled:
            print(f"Volatility Filter: ENABLED (Source: {vol_source.upper()}) | Skip Threshold: < {vol_threshold_pct:.2f}% | Lookback: {vol_lookback_candles} candles (4H)")
        else:
            print(f"Volatility Filter: DISABLED")
        if delta_filter_enabled:
            if delta_asymmetric:
                metric = delta_asymmetric.get("move_metric", "displacement")
                print(f"BTC Delta Filter: ENABLED (ASYMMETRIC {metric.upper()}) | Up: Move >= ${delta_asymmetric['up_streak_move']:.0f} & C4 Range >= ${delta_asymmetric['up_c4_range']:.0f} | Down: Move >= ${delta_asymmetric['down_streak_move']:.0f} & C4 Range >= ${delta_asymmetric['down_c4_range']:.0f} | Source: {delta_source.upper()}")
            else:
                conds = []
                if delta_max_range is not None:
                    conds.append(f"Range >= ${delta_max_range:.2f}")
                if delta_ceiling_range is not None:
                    conds.append(f"Range <= ${delta_ceiling_range:.2f}")
                if delta_threshold is not None:
                    conds.append(f"Body >= ${delta_threshold:.2f}")
                cond_str = " AND ".join(conds) if conds else f"Range >= ${delta_max_range:.2f}"
                print(f"BTC Delta Filter: ENABLED | Dual Conditions: {cond_str} | Source: {delta_source.upper()}")
        else:
            print(f"BTC Delta Filter: DISABLED")
    if tp_enabled:
        print(f"Take Profit (Maker): ENABLED | Target: +{tp_target_pct*100:.1f}% limit sell upon buy fill")
    else:
        print(f"Take Profit (Maker): DISABLED (Hold to expiration)")
    maker_win_str = f"{min_maker_sec}s - {stop_maker_sec}s" if min_maker_sec > 0 else f"{stop_maker_sec}s"
    print(f"Maker Window: {maker_win_str} | Taker Limit Price: {taker_limit_price} | Price Adjust: {active_order_adjusted_enabled} | Price Savings Bank: {enable_price_savings_bank} | Max Maker Price: {max_maker_price} | Marketable Limit: {marketable_limit}")

    if client is not None:
        ok = await asyncio.to_thread(client.get_ok)
        print(f"Kalshi connection: {ok}")

        task = asyncio.create_task(connection_keepalive_loop(client))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

        ws_task = asyncio.create_task(kalshi_ws_listener_loop())
        background_tasks.add(ws_task)
        ws_task.add_done_callback(background_tasks.discard)

    bot.print_header()

    effective_history_len = history_beats_count
    # Historical Outcomes Cache Initialization
    historical_outcomes_cache, lookback_up_count, lookback_down_count = await initialize_historical_outcomes_cache(
        fetcher, asset=asset, history_len=effective_history_len, beat_duration_min=beat_duration_min
    )

    # Special Martingale Campaign Variables (ALGO 3)
    in_campaign = False
    campaign_step = 0
    campaign_trigger_dir: str | None = None
    campaign_num = 0
    campaign_pnl = 0.0
    campaign_action_type: str | None = None

    # Fade window variables
    price_savings_bank = 0.0
    in_fade_window = False
    fade_window_remaining_beats = 0
    fade_active_side = None
    fade_session_profit = 0.0
    session_fills_count = 0
    session_total_fill_time = 0.0
    fade_session_num = 0
    sessions_with_fills = set()
    fade_session_total_beats = 1
    fade_orders = {}
    loaded_fade_beats = set()
    current_fade_stake_usd = round(base_stake_usd * stake_multipliers[0], 2)

    active_order_id = None
    active_order_token_id = None
    active_order_side = None
    active_order_stake = None
    active_order_shares = None
    active_order_price = None
    active_order_target_bid = None
    active_order_beat_ts = None
    active_order_fill_time = None
    active_order_last_adjust_time = None
    active_order_last_fill_time = None
    active_order_cancel_checked = False
    active_order_adjusted = False
    active_order_replacement_pending = False
    active_order_replacement_shares = 0.0
    active_order_cross_retries = 0
    active_order_used_bank_credit = 0.0
    is_startup_evaluation = True

    active_order_tp_id = None
    active_order_tp_price = None
    active_order_tp_shares = 0.0
    active_order_tp_placed = False
    active_order_tp_filled = False
    active_order_tp_fill_time = None

    active_order_filled_shares = 0.0
    active_order_filled_val = 0.0
    chased_filled_shares = 0.0
    chased_filled_val = 0.0
    chase_attempts = 0
    last_chase_check_time = 0
    last_fill_check_time = 0
    last_processed_beat_ts = None
    last_synced_beat_ts = None
    last_sync_attempt_time = 0
    pending_trades = {}
    pending_skipped_trades = {}
    pending_filter_skipped_trades = {}

    loaded_state_from_file = False
    if state_file and reset_state:
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
                print(f"🧹 [RESET] Deleted existing state file {state_file} to start fresh.")
            except Exception as reset_err:
                print(f"⚠️ [WARNING] Failed to delete state file {state_file} during reset: {reset_err}")
        if os.path.exists(state_file + ".tmp"):
            try:
                os.remove(state_file + ".tmp")
            except Exception:
                pass

    if state_file and not os.path.exists(state_file) and not reset_state:
        if os.path.exists("state.json") and state_file != "state.json":
            try:
                import shutil
                shutil.copy("state.json", state_file)
                print(f"📋 [STATE MIGRATION] Initialized {state_file} from state.json")
            except Exception as mig_err:
                print(f"⚠️ [WARNING] Failed to copy state.json to {state_file}: {mig_err}")

    if state_file and os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state_data = json.load(f)
            
            # Load BotState
            bs = state_data.get("bot_state", {})
            bot.state.trade_num = bs.get("trade_num", bot.state.trade_num)
            bot.state.wins = bs.get("wins", bot.state.wins)
            bot.state.losses = bs.get("losses", bot.state.losses)
            bot.state.total_pnl = bs.get("total_pnl", bot.state.total_pnl)
            bot.state.total_fees = bs.get("total_fees", bot.state.total_fees)
            bot.state.total_gross_pnl = bs.get("total_gross_pnl", bot.state.total_gross_pnl)
            bot.state.win_pnl_total = bs.get("win_pnl_total", bot.state.win_pnl_total)
            bot.state.loss_pnl_total = bs.get("loss_pnl_total", bot.state.loss_pnl_total)
            bot.state.current_balance = bs.get("current_balance", bot.state.current_balance)
            bot.state.total_fill_price_sum = bs.get("total_fill_price_sum", bot.state.total_fill_price_sum)
            bot.state.total_stake = bs.get("total_stake", bot.state.total_stake)
            bot.state.win_stake_total = bs.get("win_stake_total", bot.state.win_stake_total)
            bot.state.loss_stake_total = bs.get("loss_stake_total", bot.state.loss_stake_total)
            bot.state.skipped_count = bs.get("skipped_count", bot.state.skipped_count)
            bot.state.skipped_wins = bs.get("skipped_wins", bot.state.skipped_wins)
            bot.state.skipped_losses = bs.get("skipped_losses", bot.state.skipped_losses)
            bot.state.filter_skipped_count = bs.get("filter_skipped_count", bot.state.filter_skipped_count)
            bot.state.filter_skipped_wins = bs.get("filter_skipped_wins", bot.state.filter_skipped_wins)
            bot.state.filter_skipped_losses = bs.get("filter_skipped_losses", bot.state.filter_skipped_losses)
            bot.state.trigger_history = [bool(x) for x in bs.get("trigger_history", [])]

            # Load Session variables
            sess = state_data.get("session", {})
            price_savings_bank = sess.get("price_savings_bank", price_savings_bank)
            # Load Special Martingale Campaign
            camp = state_data.get("campaign", {})
            in_campaign = camp.get("in_campaign", in_campaign)
            campaign_step = camp.get("campaign_step", campaign_step)
            campaign_trigger_dir = camp.get("campaign_trigger_dir", campaign_trigger_dir)
            campaign_num = camp.get("campaign_num", campaign_num)
            campaign_pnl = camp.get("campaign_pnl", campaign_pnl)
            campaign_action_type = camp.get("campaign_action_type", campaign_action_type)
            bot.state.campaigns = camp.get("campaigns", bot.state.campaigns)
            bot.state.campaign_wins = camp.get("campaign_wins", bot.state.campaign_wins)
            bot.state.campaign_losses = camp.get("campaign_losses", bot.state.campaign_losses)

            in_fade_window = in_campaign
            fade_window_remaining_beats = sess.get("fade_window_remaining_beats", 1 if in_campaign else 0)
            fade_active_side = sess.get("fade_active_side", fade_active_side)
            fade_session_profit = sess.get("fade_session_profit", fade_session_profit)
            session_fills_count = sess.get("session_fills_count", session_fills_count)
            session_total_fill_time = sess.get("session_total_fill_time", session_total_fill_time)
            fade_session_num = campaign_num if campaign_num > 0 else sess.get("fade_session_num", fade_session_num)
            sessions_with_fills = set(sess.get("sessions_with_fills", []))
            fade_session_total_beats = sess.get("fade_session_total_beats", fade_session_total_beats)
            current_fade_stake_usd = sess.get("current_fade_stake_usd", current_fade_stake_usd)
            if in_campaign and campaign_trigger_dir in ("Up", "Down"):
                # Re-derive bet from the configured pattern (state may come from an older pattern)
                campaign_action_type, fade_active_side, current_fade_stake_usd = get_campaign_bet(
                    campaign_step, campaign_trigger_dir, pattern, stake_multipliers, base_stake_usd, max_cap
                )

            # Load Pending lists
            pending_trades = {int(k): v for k, v in state_data.get("pending_trades", {}).items()}
            pending_skipped_trades = {int(k): v for k, v in state_data.get("pending_skipped_trades", {}).items()}
            pending_filter_skipped_trades = {int(k): v for k, v in state_data.get("pending_filter_skipped_trades", {}).items()}

            # Load Active order variables
            ao = state_data.get("active_order", {})
            active_order_id = ao.get("active_order_id", active_order_id)
            active_order_token_id = ao.get("active_order_token_id", active_order_token_id)
            active_order_side = ao.get("active_order_side", active_order_side)
            active_order_stake = ao.get("active_order_stake", active_order_stake)
            active_order_shares = ao.get("active_order_shares", active_order_shares)
            active_order_price = ao.get("active_order_price", active_order_price)
            active_order_target_bid = ao.get("active_order_target_bid", active_order_target_bid)
            active_order_beat_ts = ao.get("active_order_beat_ts", active_order_beat_ts)
            active_order_fill_time = ao.get("active_order_fill_time", active_order_fill_time)
            active_order_last_adjust_time = ao.get("active_order_last_adjust_time", active_order_last_adjust_time)
            active_order_last_fill_time = ao.get("active_order_last_fill_time", active_order_last_fill_time)
            active_order_cancel_checked = ao.get("active_order_cancel_checked", active_order_cancel_checked)
            active_order_adjusted = ao.get("active_order_adjusted", active_order_adjusted)
            active_order_replacement_pending = ao.get("active_order_replacement_pending", active_order_replacement_pending)
            active_order_replacement_shares = ao.get("active_order_replacement_shares", active_order_replacement_shares)
            active_order_used_bank_credit = ao.get("active_order_used_bank_credit", active_order_used_bank_credit)
            active_order_tp_id = ao.get("active_order_tp_id", active_order_tp_id)
            active_order_tp_price = ao.get("active_order_tp_price", active_order_tp_price)
            active_order_tp_shares = ao.get("active_order_tp_shares", active_order_tp_shares)
            active_order_tp_placed = ao.get("active_order_tp_placed", active_order_tp_placed)
            active_order_tp_filled = ao.get("active_order_tp_filled", active_order_tp_filled)
            active_order_tp_fill_time = ao.get("active_order_tp_fill_time", active_order_tp_fill_time)
            active_order_filled_shares = ao.get("active_order_filled_shares", active_order_filled_shares)
            active_order_filled_val = ao.get("active_order_filled_val", active_order_filled_val)

            # Load Sync variables
            sync_v = state_data.get("sync", {})
            last_synced_beat_ts = sync_v.get("last_synced_beat_ts", last_synced_beat_ts)

            loaded_state_from_file = True
            print(f"✅ Loaded state from {state_file}")
            print(f"   Stats: Trades: {bot.state.trade_num} | Wins: {bot.state.wins} | Losses: {bot.state.losses} | PnL: ${bot.state.total_pnl:+.4f}")
            print(f"   Filter_Skips: {bot.state.filter_skipped_count} | Wins_FS: {bot.state.filter_skipped_wins} | Losses_FS: {bot.state.filter_skipped_losses}")
            camp_wr_val = bot.state.campaign_winrate
            print(f"   Special Martingale: In Campaign={in_campaign} | Step={campaign_step+1}/{max_cap} ({campaign_action_type}) | Dir={campaign_trigger_dir} | Running Camp PnL: ${campaign_pnl:+.2f}")
            print(f"   Campaigns: {bot.state.campaigns} | Wins: {bot.state.campaign_wins} | Losses: {bot.state.campaign_losses} (WR: {camp_wr_val:.1f}%)")
            print(f"   Fade Window: {in_fade_window} ({fade_active_side}), remaining beats: {fade_window_remaining_beats}")
        except Exception as e:
            print(f"⚠️ [WARNING] Failed to load state from {state_file}: {e}. Starting with clean state.")

    def save_state():
        if not state_file:
            return
        try:
            state_data = {
                "bot_state": {
                    "trade_num": bot.state.trade_num,
                    "wins": bot.state.wins,
                    "losses": bot.state.losses,
                    "total_pnl": bot.state.total_pnl,
                    "total_fees": bot.state.total_fees,
                    "total_gross_pnl": bot.state.total_gross_pnl,
                    "win_pnl_total": bot.state.win_pnl_total,
                    "loss_pnl_total": bot.state.loss_pnl_total,
                    "current_balance": bot.state.current_balance,
                    "total_fill_price_sum": bot.state.total_fill_price_sum,
                    "total_stake": bot.state.total_stake,
                    "win_stake_total": bot.state.win_stake_total,
                    "loss_stake_total": bot.state.loss_stake_total,
                    "skipped_count": bot.state.skipped_count,
                    "skipped_wins": bot.state.skipped_wins,
                    "skipped_losses": bot.state.skipped_losses,
                    "filter_skipped_count": bot.state.filter_skipped_count,
                    "filter_skipped_wins": bot.state.filter_skipped_wins,
                    "filter_skipped_losses": bot.state.filter_skipped_losses,
                    "trigger_history": bot.state.trigger_history,
                },
                "campaign": {
                    "in_campaign": in_campaign,
                    "campaign_step": campaign_step,
                    "campaign_trigger_dir": campaign_trigger_dir,
                    "campaign_num": campaign_num,
                    "campaign_pnl": campaign_pnl,
                    "campaign_action_type": campaign_action_type,
                    "campaigns": bot.state.campaigns,
                    "campaign_wins": bot.state.campaign_wins,
                    "campaign_losses": bot.state.campaign_losses,
                    "base_stake_usd": base_stake_usd,
                    "stake_multipliers": stake_multipliers,
                    "max_cap": max_cap,
                    "pattern": pattern,
                },
                "session": {
                    "price_savings_bank": price_savings_bank,
                    "in_fade_window": in_fade_window,
                    "fade_window_remaining_beats": fade_window_remaining_beats,
                    "fade_active_side": fade_active_side,
                    "fade_session_profit": fade_session_profit,
                    "session_fills_count": session_fills_count,
                    "session_total_fill_time": session_total_fill_time,
                    "fade_session_num": fade_session_num,
                    "sessions_with_fills": list(sessions_with_fills),
                    "fade_session_total_beats": fade_session_total_beats,
                    "current_fade_stake_usd": current_fade_stake_usd,
                },
                "pending_trades": {str(k): v for k, v in pending_trades.items()},
                "pending_skipped_trades": {str(k): v for k, v in pending_skipped_trades.items()},
                "pending_filter_skipped_trades": {str(k): v for k, v in pending_filter_skipped_trades.items()},
                "active_order": {
                    "active_order_id": active_order_id,
                    "active_order_token_id": active_order_token_id,
                    "active_order_side": active_order_side,
                    "active_order_stake": active_order_stake,
                    "active_order_shares": active_order_shares,
                    "active_order_price": active_order_price,
                    "active_order_target_bid": active_order_target_bid,
                    "active_order_beat_ts": active_order_beat_ts,
                    "active_order_fill_time": active_order_fill_time,
                    "active_order_last_adjust_time": active_order_last_adjust_time,
                    "active_order_last_fill_time": active_order_last_fill_time,
                    "active_order_cancel_checked": active_order_cancel_checked,
                    "active_order_adjusted": active_order_adjusted,
                    "active_order_replacement_pending": active_order_replacement_pending,
                    "active_order_replacement_shares": active_order_replacement_shares,
                    "active_order_used_bank_credit": active_order_used_bank_credit,
                    "active_order_tp_id": active_order_tp_id,
                    "active_order_tp_price": active_order_tp_price,
                    "active_order_tp_shares": active_order_tp_shares,
                    "active_order_tp_placed": active_order_tp_placed,
                    "active_order_tp_filled": active_order_tp_filled,
                    "active_order_tp_fill_time": active_order_tp_fill_time,
                    "active_order_filled_shares": active_order_filled_shares,
                    "active_order_filled_val": active_order_filled_val,
                },
                "sync": {
                    "last_synced_beat_ts": last_synced_beat_ts,
                },
                "config": {
                    "strategy_name": strategy_name,
                    "mode": mode.value,
                    "asset": asset,
                    "consecutive_streak_count": consecutive_streak_count,
                    "cap": cap,
                    "fade_stake_usd": fade_stake_usd,
                    "fade_profit_target_usd": fade_profit_target_usd,
                    "max_transactions": max_transactions,
                    "decisive_up": decisive_up,
                    "decisive_down": decisive_down,
                    "seconds_before_close": seconds_before_close,
                    "history_beats_count": history_beats_count,
                    "beat_duration_min": beat_duration_min,
                    "stop_maker_sec": stop_maker_sec,
                    "min_maker_sec": min_maker_sec,
                    "max_maker_price": max_maker_price,
                    "marketable_limit": marketable_limit,
                    "taker_limit_price": taker_limit_price,
                    "active_order_adjusted": active_order_adjusted_enabled,
                    "enable_price_savings_bank": enable_price_savings_bank,
                    "notifications": notifications_enabled
                }
            }
            temp_file = state_file + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(state_data, f, indent=2)
            for _attempt in range(3):
                try:
                    os.replace(temp_file, state_file)
                    break
                except PermissionError:
                    time.sleep(0.05)
            else:
                os.replace(temp_file, state_file)
        except Exception as e:
            print(f"         [WARNING] Failed to save state to {state_file}: {e}")

    # Seed the trigger state immediately at startup based on history if not loaded from state file
    if loaded_state_from_file:
        print(f"         [STARTUP] Resumed state from state file. Skipping startup trigger check from history.")
    else:
        startup_outcomes = []
        for ts in sorted(historical_outcomes_cache.keys()):
            out = historical_outcomes_cache.get(ts)
            if out in ("Up", "Down"):
                startup_outcomes.append(out)
        startup_outcomes = startup_outcomes[-effective_history_len:]

        startup_triggered, startup_fade_side, startup_action_type, startup_stake_usd, startup_streak_len, startup_streak_side = check_special_martingale_trigger(
            startup_outcomes,
            consecutive_streak_count=consecutive_streak_count,
            in_campaign=in_campaign,
            campaign_step=campaign_step,
            campaign_trigger_dir=campaign_trigger_dir,
            max_cap=max_cap,
            stake_multipliers=stake_multipliers,
            base_stake_usd=base_stake_usd,
            pattern=pattern,
        )

        startup_last_streak = startup_outcomes[-consecutive_streak_count:]
        print(f"         [STARTUP LAST {consecutive_streak_count} BEATS] {startup_last_streak}")

        if startup_triggered:
            skip_startup_by_dyn = False
            if dyn_filter_enabled and dynamic_rules:
                _, s_vol_pct, s_vol_msg = check_volatility_filter(
                    asset=asset, enabled=True, threshold_pct=dyn_min_vol_pct, lookback_candles=vol_lookback_candles, source=dyn_vol_source
                )
                sorted_cache_ts = sorted(historical_outcomes_cache.keys())
                last_closed_beat_ts = sorted_cache_ts[-1] if sorted_cache_ts else utc_beat_start_ts(int(time.time()), beat_duration_min) - (beat_duration_min * 60)
                d_valid, d_rng, d_body, b_hi, b_lo, d_msg = fetch_binance_15m_delta_range(
                    asset=asset, beat_start_ts=last_closed_beat_ts
                )
                if not d_valid:
                    print(f'         [STARTUP DYNAMIC FILTER: SKIP] Binance Network Issue ({d_msg}). Skipping startup trade.')
                    skip_startup_by_dyn = True
                else:
                    passed_dyn, match_desc, statuses = evaluate_dynamic_rules(dynamic_rules, s_vol_pct, d_rng, d_body)
                    print(f'         [STARTUP DYNAMIC FILTER] {match_desc}')
                    if not passed_dyn:
                        skip_startup_by_dyn = True
                        print(f'         [STARTUP DYNAMIC FILTER: SKIP] {match_desc}. Skipping startup trade.')
            skip_startup_by_vol = False
            if not dyn_filter_enabled and vol_filter_enabled:
                should_skip, vol_pct, vol_msg = check_volatility_filter(
                    asset=asset,
                    enabled=True,
                    threshold_pct=vol_threshold_pct,
                    lookback_candles=vol_lookback_candles,
                    source=vol_source,
                )
                print(f"         {vol_msg}")
                if should_skip:
                    skip_startup_by_vol = True
                    print(f"         [STARTUP VOL FILTER: SKIP] 4H Volatility {vol_pct:.4f}% < {vol_threshold_pct:.2f}%. Skipping startup trade.")

            skip_startup_by_delta = False
            if not dyn_filter_enabled and delta_filter_enabled:
                if delta_asymmetric:
                    s_valid, s_candles, s_msg = fetch_binance_15m_streak_candles(
                        asset=asset, current_beat_start_ts=current_beat_start, streak_count=consecutive_streak_count
                    )
                    print(f"         [STARTUP ASYMMETRIC DELTA FILTER] {s_msg}")
                    if not s_valid or len(s_candles) < consecutive_streak_count:
                        skip_startup_by_delta = True
                        print(f"         [STARTUP DELTA FILTER: SKIP] Binance Network Issue ({s_msg}). Skipping startup trade.")
                    else:
                        c1, c4 = s_candles[0], s_candles[-1]
                        c4_rng = c4["high"] - c4["low"]
                        move_type = delta_asymmetric.get("move_metric", "displacement")
                        streak_move = (max(c["high"] for c in s_candles) - min(c["low"] for c in s_candles)) if move_type == "range" else abs(c4["close"] - c1["open"])
                        req_move = delta_asymmetric["up_streak_move"] if startup_streak_side == "Up" else delta_asymmetric["down_streak_move"]
                        req_rng = delta_asymmetric["up_c4_range"] if startup_streak_side == "Up" else delta_asymmetric["down_c4_range"]
                        if (streak_move < req_move) or (c4_rng < req_rng):
                            skip_startup_by_delta = True
                            print(f"         [STARTUP ASYMMETRIC FILTER: SKIP] Move ${streak_move:.2f} < ${req_move:.2f} or Range ${c4_rng:.2f} < ${req_rng:.2f}. Skipping startup trade.")
                else:
                    sorted_cache_ts = sorted(historical_outcomes_cache.keys())
                    if sorted_cache_ts:
                        last_closed_beat_ts = sorted_cache_ts[-1]
                    else:
                        curr_start = utc_beat_start_ts(int(time.time()), beat_duration_min)
                        last_closed_beat_ts = curr_start - (beat_duration_min * 60)
                    d_valid, d_rng, d_body, b_hi, b_lo, d_msg = fetch_binance_15m_delta_range(
                        asset=asset, beat_start_ts=last_closed_beat_ts
                    )
                    print(f"         [STARTUP DELTA FILTER] {d_msg}")
                    if not d_valid:
                        skip_startup_by_delta = True
                        print(f"         [STARTUP DELTA FILTER: SKIP] Binance Network Issue ({d_msg}). Skipping startup trade.")
                        if notifications_enabled:
                            msg = (
                                f"⚠️ POLY - BINANCE NETWORK ISSUE (Startup)\n"
                                f"🆔 {strategy_name}\n"
                                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"❌ Binance Network Issue: {d_msg}\n"
                                f"Action: SKIPPED startup bet due to Binance Network Issue."
                            )
                            send_telegram_notification(msg)
                    else:
                        is_rng_min_fail = (delta_max_range is not None) and (d_rng < delta_max_range)
                        is_rng_max_fail = (delta_ceiling_range is not None) and (d_rng > delta_ceiling_range)
                        is_rng_fail = is_rng_min_fail or is_rng_max_fail
                        is_body_fail = (delta_threshold is not None) and (d_body < delta_threshold)
                        if is_rng_fail or is_body_fail:
                            skip_startup_by_delta = True
                            reasons = []
                            if is_rng_min_fail:
                                reasons.append(f"Range ${d_rng:.2f} < ${delta_max_range:.2f}")
                            if is_rng_max_fail:
                                reasons.append(f"Range ${d_rng:.2f} > ${delta_ceiling_range:.2f}")
                            if is_body_fail:
                                reasons.append(f"Body ${d_body:.2f} < ${delta_threshold:.2f}")
                            print(f"         [STARTUP DELTA FILTER: SKIP] {' & '.join(reasons)}. Skipping startup trade.")

            if not skip_startup_by_dyn and not skip_startup_by_vol and not skip_startup_by_delta:
                if not in_campaign:
                    in_campaign = True
                    campaign_step = 0
                    campaign_trigger_dir = startup_streak_side
                    campaign_num += 1
                    campaign_pnl = 0.0

                campaign_action_type = startup_action_type
                in_fade_window = True
                current_fade_stake_usd = startup_stake_usd
                fade_active_side = startup_fade_side
                fade_session_total_beats = 1
                fade_window_remaining_beats = 1
                step_tag = f"[STEP {campaign_step+1}/{max_cap} ({campaign_action_type})]"
                print(
                    f"         [STARTUP TRIGGER] Special Martingale Campaign #{campaign_num} {step_tag} triggered on {startup_streak_len} consecutive {startup_streak_side}. "
                    f"Stake = ${current_fade_stake_usd:.2f}. Bet on {fade_active_side}."
                )
            else:
                streak_info = f"{startup_streak_len} {startup_streak_side}" if startup_streak_side else "none"
                cap_info = f", cap: {cap}" if cap else ""
                filter_reasons = []
                if skip_startup_by_vol:
                    filter_reasons.append("volatility")
                if skip_startup_by_delta:
                    filter_reasons.append("delta range")
                if skip_startup_by_dyn:
                    filter_reasons.append('dynamic filter')
                print(f"         [STARTUP] Filter active ({' + '.join(filter_reasons)}) ({streak_info}). Trade suppressed.")
        else:
            streak_info = f"{startup_streak_len} {startup_streak_side}" if startup_streak_side else "none"
            cap_info = f", cap: {cap}" if cap else ""
            print(
                f"         [STARTUP] Streak check not met ({streak_info}, need {consecutive_streak_count}{cap_info}). Skipping current beat."
            )
        
        # Save initial state
        save_state()

    if enable_price_savings_bank:
        print(f"Price Savings Bank: Enabled (Initial balance: ${price_savings_bank:.4f})")

    beat_duration_sec = beat_duration_min * 60

    async def subscription_maintenance_loop():
        while True:
            try:
                now_t = int(time.time())
                curr_start = utc_beat_start_ts(now_t, beat_duration_min)
                current_tokens = fetcher.get_token_ids_for_beat(curr_start)
                next_tokens = fetcher.get_token_ids_for_beat(curr_start + beat_duration_sec)
                all_tokens = set(current_tokens.values()) | set(next_tokens.values())

                global subscribed_assets
                if all_tokens != subscribed_assets:
                    subscribed_assets = all_tokens
                    resubscribe_event.set()

                    if client is not None:
                        cond_id = fetcher.get_current_condition_id()
                        if cond_id:
                            needs_warmup = any(not client.is_warmed_up(tid) for tid in all_tokens)
                            if needs_warmup:
                                task = asyncio.create_task(client.warmup_market(cond_id))
                                background_tasks.add(task)
                                task.add_done_callback(background_tasks.discard)
            except Exception:
                pass
            await asyncio.sleep(5)

    sub_task = asyncio.create_task(subscription_maintenance_loop())
    background_tasks.add(sub_task)
    sub_task.add_done_callback(background_tasks.discard)

    last_printed_best_ask = None
    last_printed_token_id = None
    last_loop_beat_ts = None
    delayed_placement_beat_ts = None
    delayed_wait_log_beat_ts = None

    async def place_beat_order(target_beat_start: int, target_side: str, target_stake: float):
        nonlocal active_order_id, active_order_token_id, active_order_side, active_order_stake
        nonlocal active_order_shares, active_order_price, active_order_target_bid, active_order_beat_ts
        nonlocal active_order_fill_time, active_order_last_fill_time, active_order_last_adjust_time
        nonlocal active_order_replacement_pending, active_order_replacement_shares
        nonlocal active_order_used_bank_credit, chase_attempts, active_order_cancel_checked
        nonlocal active_order_filled_shares, active_order_filled_val, chased_filled_shares
        nonlocal chased_filled_val, price_savings_bank

        try:
            max_order_attempts = 40
            order_id = None
            final_price = max_maker_price
            final_shares = round(target_stake / max_maker_price, 4)
            final_credit_used = 0.0

            for attempt in range(1, max_order_attempts + 1):
                try:
                    target_tokens = fetcher.get_token_ids_for_beat(target_beat_start, force_refresh=(attempt > 1))
                    target_token_id = target_tokens.get(target_side)
                except Exception as token_err:
                    print(f"         [WARNING] Attempt {attempt}/{max_order_attempts}: Could not resolve token ID: {token_err}")
                    target_token_id = None

                if not target_token_id:
                    if attempt < max_order_attempts:
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        print(f"         [ERROR] Next beat token ID for side {target_side} not found. Skipping order placement.")
                        break

                # Deterministic tagged client_order_id for this bot instance and beat
                bot_instance_tag = f"{strategy_name}_cap{cap}_streak{consecutive_streak_count}"
                cid_ns = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
                base_client_order_id = str(uuid.uuid5(cid_ns, f"{bot_instance_tag}:{target_beat_start}:{target_side}"))

                # Unique client_order_id per attempt to prevent 409 order_already_exists on retries
                if attempt == 1:
                    beat_client_order_id = base_client_order_id
                else:
                    beat_client_order_id = str(uuid.uuid5(cid_ns, f"{bot_instance_tag}:{target_beat_start}:{target_side}:att{attempt}"))

                # Pre-retry check: if attempt > 1, check if order is already resting or executed on Kalshi
                if attempt > 1 and mode == BotMode.REAL and client and target_token_id:
                    existing_order_id = await check_existing_bot_order(client, target_token_id, base_client_order_id)
                    if not existing_order_id and attempt > 2:
                        prev_cid = str(uuid.uuid5(cid_ns, f"{bot_instance_tag}:{target_beat_start}:{target_side}:att{attempt - 1}"))
                        existing_order_id = await check_existing_bot_order(client, target_token_id, prev_cid)

                    if existing_order_id:
                        print(f"         [RETRY CHECK] Existing order found on Kalshi orderbook (ID: {existing_order_id}). Adopting order instead of re-betting!")
                        order_id = existing_order_id
                        break

                target_bid, target_ask = await get_token_book_prices(fetcher, target_token_id)
                print(f"         [NEXT BOOK CHECK] Beat {target_side} book (attempt {attempt}/{max_order_attempts}): Best Bid: {target_bid:.2f} | Best Ask: {target_ask:.2f}")

                if marketable_limit:
                    # Marketable Limit Order: Always place directly at max_maker_price (post_only=False).
                    # If best_ask <= max_maker_price, Kalshi executes instantly as taker (capturing price improvement).
                    # Any unfilled shares automatically rest at max_maker_price as top priority maker.
                    credit_used = 0.0
                    if enable_price_savings_bank and price_savings_bank > 0:
                        max_boost = min(price_savings_bank, 0.05)
                        next_price = round(max_maker_price + max_boost, 4)
                        credit_used = max_boost
                    else:
                        next_price = max_maker_price
                    is_post_only = False
                    ask_str = f"{target_ask:.4f}" if target_ask is not None else "N/A"
                    print(f"         [MARKETABLE LIMIT] Placing GTC limit buy at ${next_price:.4f} (post_only=False, max_maker_price=${max_maker_price:.4f}). "
                          f"Best Ask: {ask_str}. Will fill as taker if ask <= {next_price:.4f}, remainder rests as maker.")
                else:
                    # Legacy Post-Only maker calculation
                    next_price, credit_used = calculate_order_price_with_bank(
                        target_bid, price_savings_bank, enable_price_savings_bank, best_ask=target_ask, base_cap=max_maker_price
                    )
                    is_post_only = True
                    if target_ask is not None and (target_ask - target_bid) >= 0.0199 and next_price > target_bid:
                        print(f"         [SPREAD LOGIC] Best Bid ({target_bid:.2f}) & Best Ask ({target_ask:.2f}) spread is >= 0.02. Calculated order price: ${next_price:.2f}.")

                next_shares = round(target_stake / next_price, 4)
                next_shares = adjust_shares_to_minimum(next_shares, next_price, "NEXT PLACE CHECK")

                try:
                    order_id = await place_bot_order(
                        mode, client, target_token_id, next_price, next_shares, target_side, post_only=is_post_only, client_order_id=beat_client_order_id
                    )
                except MarketNotFoundError as mnf_err:
                    print(f"         [MARKET NOT LISTED YET] Attempt {attempt}/{max_order_attempts}: Market not open on Kalshi yet ({mnf_err}).")
                    fetcher.invalidate_token_cache(target_beat_start)
                    order_id = None

                if order_id:
                    final_price = next_price
                    final_shares = next_shares
                    final_credit_used = credit_used
                    if credit_used > 0:
                        price_savings_bank = round(price_savings_bank - credit_used, 4)
                        print(f"         [SAVINGS BANK] Applied ${credit_used:.4f} bank credit for order price {next_price:.4f} (best bid: {target_bid:.4f}). Remaining Bank Balance: ${price_savings_bank:.4f}")
                    break
                else:
                    if attempt < max_order_attempts:
                        delay = 0.5
                        print(f"         [RETRY] Failed to place order (attempt {attempt}/{max_order_attempts}). Retrying in {delay:.1f}s...")
                        await asyncio.sleep(delay)
                    else:
                        print(f"         [ERROR] Failed to place order after {max_order_attempts} attempts.")

            if order_id:
                verified = await verify_order_in_book(mode, client, order_id)
                if verified:
                    active_order_id = order_id
                    active_order_token_id = target_token_id
                    active_order_side = target_side
                    active_order_stake = target_stake
                    active_order_shares = final_shares
                    active_order_price = final_price
                    active_order_target_bid = final_price
                    active_order_beat_ts = target_beat_start

                    active_order_fill_time = None
                    active_order_last_fill_time = None
                    active_order_last_adjust_time = int(time.time())
                    active_order_replacement_pending = False
                    active_order_replacement_shares = 0.0
                    active_order_used_bank_credit = final_credit_used
                    chase_attempts = 0
                    active_order_cancel_checked = False

                    # Check for immediate fills (common in marketable limit orders)
                    init_matched = float(verified.size_matched) if hasattr(verified, "size_matched") else 0.0
                    init_avg_price = float(verified.avg_price) if hasattr(verified, "avg_price") and verified.avg_price > 0 else final_price

                    if mode == BotMode.DRY_RUN and marketable_limit:
                        # Simulate immediate taker execution in DRY RUN if best_ask <= final_price
                        if target_ask is not None and target_ask <= final_price:
                            init_matched = final_shares
                            init_avg_price = target_ask
                            active_order_fill_time = int(time.time())
                            active_order_last_fill_time = active_order_fill_time
                            print(f"         [DRY RUN IMMEDIATE FILL] Best Ask (${target_ask:.4f}) <= Limit Price (${final_price:.4f}). "
                                  f"Filled {final_shares:.4f} shares as taker at ${target_ask:.4f} (savings: ${(final_price - target_ask)*final_shares:.2f})")

                    if init_matched > 0:
                        active_order_filled_shares = init_matched
                        active_order_filled_val = init_matched * init_avg_price
                        active_order_last_fill_time = int(time.time())
                        if verified.status == "matched" or init_matched >= (final_shares - 0.01):
                            active_order_fill_time = int(time.time())
                            print(f"         [IMMEDIATE FILL] Marketable Limit Order {order_id[:8]}... 100% matched as taker: {init_matched:.4f} shares at avg ${init_avg_price:.4f}")
                        else:
                            print(f"         [IMMEDIATE PARTIAL FILL] Marketable Limit Order {order_id[:8]}... partially matched as taker: {init_matched:.4f}/{final_shares:.4f} shares at avg ${init_avg_price:.4f}. Remaining rests as maker.")
                    else:
                        active_order_filled_shares = 0.0
                        active_order_filled_val = 0.0
                        if marketable_limit:
                            print(f"         [RESTING AS MAKER] Marketable Limit Order {order_id[:8]}... resting in book at ${final_price:.4f}")

                    chased_filled_shares = 0.0
                    chased_filled_val = 0.0
                else:
                    if final_credit_used > 0:
                        price_savings_bank = round(price_savings_bank + final_credit_used, 4)
                    print(f"         [WARNING] Order {order_id} failed verification. Not tracked as active order.")
            else:
                print("         [ERROR] Order placement failed.")
        except Exception as order_err:
            print(f"         [ERROR] Order preparation failed: {order_err}")

    # Save the startup beat start timestamp to prevent placement on startup
    startup_beat_ts = utc_beat_start_ts(int(time.time()), beat_duration_min)

    while True:
        try:
            now = int(time.time())
            current_beat_start = utc_beat_start_ts(now, beat_duration_min)

            if last_loop_beat_ts != current_beat_start:
                last_loop_beat_ts = current_beat_start
                last_printed_best_ask = None
                last_printed_token_id = None
                delayed_placement_beat_ts = None

            elapsed_in_beat = now - current_beat_start

            # --- Beat Synchronization ---
            if elapsed_in_beat >= 4 and last_synced_beat_ts != current_beat_start:
                if now - last_sync_attempt_time >= 2:
                    last_sync_attempt_time = now
                    # Fetch outcomes of the last effective_history_len beats
                    sync_timestamps = [current_beat_start - beat_duration_sec * i for i in range(effective_history_len, 0, -1)]
                    sync_outcomes = await fetch_historical_outcomes(fetcher, sync_timestamps)
                    historical_outcomes_cache.update({ts: out for ts, out in sync_outcomes.items() if out in ("Up", "Down")})

                    # Check if the just-closed beat outcome is resolved
                    last_beat_outcome = sync_outcomes.get(current_beat_start - beat_duration_sec)
                    resolved_outcomes_list = []
                    if last_beat_outcome is not None:
                        # Retrieve all resolved outcomes in chronological order
                        for ts in sync_timestamps:
                            outcome = historical_outcomes_cache.get(ts)
                            if outcome is not None:
                                resolved_outcomes_list.append((ts, outcome))
                        if len(resolved_outcomes_list) < effective_history_len:
                            print(f"         [SYNC PENDING] Sync returned incomplete history ({len(resolved_outcomes_list)}/{effective_history_len} beats resolved). Retrying...")

                    if last_beat_outcome is not None and len(resolved_outcomes_list) >= min(effective_history_len, consecutive_streak_count + (cap or 0) + 1):
                        # Run simulation to compute correct state
                        sim = simulate_state_from_history(
                            resolved_outcomes_list,
                            history_len=effective_history_len,
                            consecutive_streak_count=consecutive_streak_count,
                            cap=cap,
                            max_cap=max_cap,
                            stake_multipliers=stake_multipliers,
                            base_stake_usd=base_stake_usd,
                            pattern=pattern,
                        )



                        # False Trigger / Mismatch Check
                        true_in_fade = sim["in_fade_window"]
                        # Legacy history-based corrections are opt-in (sync_corrections). The campaign
                        # state machine is driven by settled results of the bot's own trades/skips.
                        is_history_complete = sync_corrections and (len(resolved_outcomes_list) == effective_history_len)
                        if is_history_complete and in_fade_window and not true_in_fade:
                            print(f"         [FALSE TRIGGER DETECTED] Bot is in fade window but history simulation says it should NOT be! Keeping order active and continuing trade...")
                            if notifications_enabled:
                                side_emoji = "⬆️ UP" if fade_active_side == "Up" else "⬇️ DOWN"
                                ft_msg = (
                                    f"🚨 POLY - FALSE TRIGGER - #{fade_session_num}\n"
                                    f"🆔 {strategy_name}\n"
                                    f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                    f"🎯 Bet: {side_emoji} (${format_masked_amount(current_fade_stake_usd)})\n"
                                    f"📊 Settled History: Up: {sim['lookback_up']} | Down: {sim['lookback_down']}\n"
                                    f"⚠️ Detail: Beat prediction differed from settled history. Keeping order active and continuing trade..."
                                )
                                send_telegram_notification(ft_msg)

                        elif is_history_complete and in_fade_window and true_in_fade and sim["fade_active_side"] != fade_active_side:
                            print(f"         [FADE SIDE MISMATCH] Bot active side {fade_active_side} != true active side {sim['fade_active_side']}. Canceling orders and updating...")
                            if notifications_enabled:
                                curr_side_emoji = "⬆️ UP" if fade_active_side == "Up" else "⬇️ DOWN"
                                true_side_emoji = "⬆️ UP" if sim['fade_active_side'] == "Up" else "⬇️ DOWN"
                                ft_msg = (
                                    f"🚨 POLY - FADE SIDE MISMATCH - #{fade_session_num}\n"
                                    f"🆔 Strategy: {strategy_name} - {asset}\n"
                                    f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                    f"🎯 Current Side: {curr_side_emoji} ➡️ True Side: {true_side_emoji}\n"
                                    f"📊 Settled History: Up: {sim['lookback_up']} | Down: {sim['lookback_down']}\n"
                                    f"⚠️ Detail: Bot side mismatch with settled history. Canceling orders and correcting..."
                                )
                                send_telegram_notification(ft_msg)

                            if active_order_id:
                                try:
                                    await cancel_bot_order(mode, client, active_order_id)
                                except Exception as e:
                                    print(f"         [WARNING] Failed to cancel active order {active_order_id[:8]}: {e}")
                                active_order_id = None
                            active_order_cancel_checked = False

                            for beat_ts, ord_info in list(fade_orders.items()):
                                order_id = ord_info.get("order_id")
                                if order_id:
                                    try:
                                        await cancel_bot_order(mode, client, order_id)
                                    except Exception as e:
                                        print(f"         [WARNING] Failed to cancel order {order_id[:8]}: {e}")

                            fade_orders.clear()
                            active_order_id = None
                            active_order_token_id = None
                            active_order_side = None
                            active_order_stake = None
                            active_order_shares = None
                            active_order_price = None
                            active_order_target_bid = None
                            active_order_beat_ts = None
                            active_order_filled_shares = 0.0
                            active_order_filled_val = 0.0
                            chased_filled_shares = 0.0
                            chased_filled_val = 0.0

                            fade_active_side = sim["fade_active_side"]
                            # Keep remaining beats in an active session; only correct the side.

                            # Place order for corrected side (Maker-only)
                            try:
                                token_id = fetcher.get_token_ids_for_beat(current_beat_start).get(fade_active_side)
                                if token_id:
                                    order_id = None
                                    if current_beat_start == startup_beat_ts:
                                        print(f"         [STARTUP] Skipping corrected side placement for startup beat {current_beat_start}.")
                                    else:
                                        best_bid, best_ask = await get_token_book_prices(fetcher, token_id)
                                        if marketable_limit:
                                            price = max_maker_price
                                            credit_used = 0.0
                                            is_post_only = False
                                        else:
                                            price, credit_used = calculate_order_price_with_bank(
                                                best_bid, price_savings_bank, enable_price_savings_bank, best_ask=best_ask, base_cap=max_maker_price
                                            )
                                            is_post_only = True
                                        if dynamic_staking_enabled:
                                            current_fade_stake_usd, _, _, _, _ = calculate_dynamic_stake(
                                                base_stake=fade_stake_usd,
                                                trigger_history=bot.state.trigger_history,
                                                rolling_window=dyn_rolling_window,
                                                target_win_rate=dyn_target_wr,
                                                alpha=dyn_alpha,
                                                min_stake=dyn_min_stake,
                                                max_stake=dyn_max_stake,
                                                max_consecutive_loss_cap=dyn_max_consec_loss,
                                            )
                                        else:
                                            current_fade_stake_usd = fade_stake_usd
                                        shares = round(current_fade_stake_usd / price, 4)
                                        shares = adjust_shares_to_minimum(shares, price, "SYNC CORRECTION PLACE")
                                        order_id = await place_bot_order(
                                            mode, client, token_id, price, shares, fade_active_side, post_only=is_post_only
                                        )
                                    if order_id:
                                        verified = await verify_order_in_book(mode, client, order_id)
                                        if verified:
                                            if credit_used > 0:
                                                price_savings_bank = round(price_savings_bank - credit_used, 4)
                                                print(f"         [SAVINGS BANK] Applied ${credit_used:.4f} bank credit for order price {price:.4f}. Remaining Bank Balance: ${price_savings_bank:.4f}")
                                            active_order_id = order_id
                                            active_order_token_id = token_id
                                            active_order_side = fade_active_side
                                            active_order_stake = current_fade_stake_usd
                                            active_order_shares = shares
                                            active_order_price = price
                                            active_order_beat_ts = current_beat_start
                                            active_order_cancel_checked = False
                                            active_order_adjusted = False
                                            active_order_replacement_pending = False
                                            active_order_replacement_shares = 0.0
                                            active_order_used_bank_credit = credit_used
                                            init_matched = float(verified.size_matched) if hasattr(verified, "size_matched") else 0.0
                                            init_avg_price = float(verified.avg_price) if hasattr(verified, "avg_price") and verified.avg_price > 0 else price
                                            if init_matched > 0:
                                                active_order_filled_shares = init_matched
                                                active_order_filled_val = init_matched * init_avg_price
                                                active_order_last_fill_time = int(time.time())
                                                if verified.status == "matched" or init_matched >= (shares - 0.01):
                                                    active_order_fill_time = int(time.time())
                                            print(f"         [SYNC CORRECTION PLACE SUCCESS] Placed order for correct side: {order_id[:8]}...")
                            except Exception as ex:
                                print(f"         [ERROR] Correct side placement failed: {ex}")

                        elif is_history_complete and not in_fade_window and true_in_fade:
                            skip_sync_by_vol = False
                            if vol_filter_enabled:
                                should_skip, vol_pct, vol_msg = check_volatility_filter(
                                    asset=asset,
                                    enabled=True,
                                    threshold_pct=vol_threshold_pct,
                                    lookback_candles=vol_lookback_candles,
                                    source=vol_source,
                                )
                                print(f"         {vol_msg}")
                                if should_skip:
                                    skip_sync_by_vol = True
                                    print(f"         [VOL FILTER: SKIP] Sync trigger suppressed due to low volatility ({vol_pct:.4f}% < {vol_threshold_pct:.2f}%).")

                            skip_sync_by_delta = False
                            if delta_filter_enabled:
                                last_closed_beat_ts = current_beat_start - (beat_duration_min * 60)
                                d_valid, d_rng, d_body, b_hi, b_lo, d_msg = fetch_binance_15m_delta_range(
                                    asset=asset, beat_start_ts=last_closed_beat_ts
                                )
                                print(f"         [SYNC DELTA FILTER] {d_msg}")
                                if not d_valid:
                                    skip_sync_by_delta = True
                                    print(f"         [DELTA FILTER: SKIP] Sync trigger suppressed due to Binance Network Issue ({d_msg}).")
                                else:
                                    is_rng_min_fail = (delta_max_range is not None) and (d_rng < delta_max_range)
                                    is_range_max_fail = (delta_ceiling_range is not None) and (d_rng > delta_ceiling_range)
                                    is_rng_fail = is_rng_min_fail or is_range_max_fail
                                    is_body_fail = (delta_threshold is not None) and (d_body < delta_threshold)
                                    if is_rng_fail or is_body_fail:
                                        skip_sync_by_delta = True
                                        reasons = []
                                        if is_rng_min_fail:
                                            reasons.append(f"Range ${d_rng:.2f} < ${delta_max_range:.2f}")
                                        if is_range_max_fail:
                                            reasons.append(f"Range ${d_rng:.2f} > ${delta_ceiling_range:.2f}")
                                        if is_body_fail:
                                            reasons.append(f"Body ${d_body:.2f} < ${delta_threshold:.2f}")
                                        print(f"         [DELTA FILTER: SKIP] Sync trigger suppressed due to {' & '.join(reasons)}.")

                            skip_sync_by_dynamic = False
                            if dyn_filter_enabled and dynamic_rules:
                                _, sync_vol_pct, sync_vol_msg = check_volatility_filter(
                                    asset=asset, enabled=True, threshold_pct=dyn_min_vol_pct, lookback_candles=vol_lookback_candles, source=dyn_vol_source
                                )
                                print(f"         [SYNC DYNAMIC FILTER VOL] {sync_vol_msg}")
                                sd_valid, sd_rng, sd_body, _, _, sd_msg = fetch_binance_15m_delta_range(
                                    asset=asset, beat_start_ts=current_beat_start
                                )
                                print(f"         [SYNC DYNAMIC FILTER DELTA] {sd_msg}")
                                if not sd_valid:
                                    skip_sync_by_dynamic = True
                                    print(f"         [DYNAMIC FILTER: SKIP] Sync trigger suppressed due to Binance Network Issue ({sd_msg}).")
                                else:
                                    passed_dyn, match_desc, statuses = evaluate_dynamic_rules(dynamic_rules, sync_vol_pct, sd_rng, sd_body)
                                    if not passed_dyn:
                                        skip_sync_by_dynamic = True
                                        print(f"         [DYNAMIC FILTER: SKIP] Sync trigger suppressed. {match_desc}.")
                                        for st in statuses:
                                            print(f"           • {st}")
                                    else:
                                        print(f"         [DYNAMIC FILTER: PASS] Sync allowed. {match_desc}.")

                            if not skip_sync_by_vol and not skip_sync_by_delta and not skip_sync_by_dynamic:
                                print(f"         [MISSING TRIGGER DETECTED] Bot is NOT in fade window but history sync says it should be! Entering fade window...")
                                in_fade_window = True
                                fade_active_side = sim["fade_active_side"]
                                fade_session_total_beats = 1
                                fade_window_remaining_beats = 1


                            if dynamic_staking_enabled:
                                current_fade_stake_usd, _, _, _, _ = calculate_dynamic_stake(
                                    base_stake=fade_stake_usd,
                                    trigger_history=bot.state.trigger_history,
                                    rolling_window=dyn_rolling_window,
                                    target_win_rate=dyn_target_wr,
                                    alpha=dyn_alpha,
                                    min_stake=dyn_min_stake,
                                    max_stake=dyn_max_stake,
                                    max_consecutive_loss_cap=dyn_max_consec_loss,
                                )
                            else:
                                current_fade_stake_usd = fade_stake_usd

                            # Place order for current beat (Maker-only)
                            try:
                                if (min_maker_sec or 0) > 0 and elapsed_in_beat < min_maker_sec:
                                    print(f"         [SYNC MISS] Delayed maker configured (min_maker_sec = {min_maker_sec}s). Will place order once beat elapsed >= {min_maker_sec}s.")
                                else:
                                    token_id = fetcher.get_token_ids_for_beat(current_beat_start).get(fade_active_side)
                                    if token_id:
                                        order_id = None
                                        if current_beat_start == startup_beat_ts:
                                            print(f"         [STARTUP] Skipping missing order placement for startup beat {current_beat_start}.")
                                        else:
                                            best_bid, best_ask = await get_token_book_prices(fetcher, token_id)
                                            if marketable_limit:
                                                price = max_maker_price
                                                credit_used = 0.0
                                                is_post_only = False
                                            else:
                                                price, credit_used = calculate_order_price_with_bank(
                                                    best_bid, price_savings_bank, enable_price_savings_bank, best_ask=best_ask, base_cap=max_maker_price
                                                )
                                                is_post_only = True
                                            shares = round(current_fade_stake_usd / price, 4)
                                            shares = adjust_shares_to_minimum(shares, price, "SYNC MISS PLACE")
                                            order_id = await place_bot_order(
                                                mode, client, token_id, price, shares, fade_active_side, post_only=is_post_only
                                            )
                                        if order_id:
                                            verified = await verify_order_in_book(mode, client, order_id)
                                            if verified:
                                                if credit_used > 0:
                                                    price_savings_bank = round(price_savings_bank - credit_used, 4)
                                                    print(f"         [SAVINGS BANK] Applied ${credit_used:.4f} bank credit for order price {price:.4f}. Remaining Bank Balance: ${price_savings_bank:.4f}")
                                                active_order_id = order_id
                                                active_order_token_id = token_id
                                                active_order_side = fade_active_side
                                                active_order_stake = current_fade_stake_usd
                                                active_order_shares = shares
                                                active_order_price = price
                                                active_order_target_bid = price
                                                active_order_beat_ts = current_beat_start
                                                active_order_cancel_checked = False
                                                active_order_adjusted = False
                                                active_order_replacement_pending = False
                                                active_order_replacement_shares = 0.0
                                                active_order_used_bank_credit = credit_used
                                                init_matched = float(verified.size_matched) if hasattr(verified, "size_matched") else 0.0
                                                init_avg_price = float(verified.avg_price) if hasattr(verified, "avg_price") and verified.avg_price > 0 else price
                                                if init_matched > 0:
                                                    active_order_filled_shares = init_matched
                                                    active_order_filled_val = init_matched * init_avg_price
                                                    active_order_last_fill_time = int(time.time())
                                                    if verified.status == "matched" or init_matched >= (shares - 0.01):
                                                        active_order_fill_time = int(time.time())
                                                print(f"         [SYNC MISS PLACE SUCCESS] Placed missing order: {order_id[:8]}...")
                            except Exception as ex:
                                print(f"         [ERROR] Missing placement failed: {ex}")

                        # Update lookback counts & trigger state with simulated truth
                        lookback_up_count = sim["lookback_up"]
                        lookback_down_count = sim["lookback_down"]

                        sync_last_n = [out for ts, out in resolved_outcomes_list][-consecutive_streak_count:]
                        streak_info_str = f"{sim['streak_len']} {sim['streak_side']}" if sim['streak_side'] else "none"
                        print(f"         [SYNC SUCCESS] State synchronized: Last {consecutive_streak_count} beats {sync_last_n} (Streak: {streak_info_str}) | Trade = {in_fade_window} ({fade_active_side})")

                        last_synced_beat_ts = current_beat_start

                        # Evaluate and record pending trades once their beats resolve
                        for p_beat_ts in list(pending_trades.keys()):
                            trade_outcome = sync_outcomes.get(p_beat_ts)
                            if trade_outcome is not None:
                                p_trade = pending_trades[p_beat_ts]
                                tp_hit = p_trade.get("tp_hit", False)
                                if tp_hit:
                                    won = True
                                    result_str = "WIN (TP)"
                                    exit_price = p_trade.get("tp_sell_price", 0.50)
                                    proceeds = p_trade["shares"] * exit_price
                                    pnl = proceeds - p_trade["cost"]
                                else:
                                    won = (trade_outcome == p_trade["side"])
                                    result_str = "WIN" if won else "LOSS"
                                    exit_price = 1.00 if won else 0.00
                                    proceeds = p_trade["shares"] * exit_price
                                    pnl = proceeds - p_trade["cost"]

                                trade = {
                                    "signal_ts": p_beat_ts,
                                    "side": p_trade["side"],
                                    "buy_local_ts": time.time() - p_trade["trigger_seconds"],
                                    "sell_local_ts": time.time(),
                                    "entry_ask": p_trade["avg_price"],
                                    "exit_bid": exit_price,
                                    "entry_fee": 0.0,
                                    "exit_fee": 0.0,
                                    "total_fees": 0.0,
                                    "gross_pnl": round(pnl, 4),
                                    "pnl": round(pnl, 4),
                                    "cost": round(p_trade["cost"], 4),
                                    "shares": p_trade["shares"],
                                    "result": result_str,
                                    "won": won,
                                }
                                bot.record_trade(trade)

                                if enable_price_savings_bank:
                                    used_credit = p_trade.get("used_bank_credit", 0.0)
                                    if won:
                                        avg_buy_price = p_trade["avg_price"]
                                        if avg_buy_price < max_maker_price:
                                            saved_diff = round(max_maker_price - avg_buy_price, 4)
                                            price_savings_bank = round(price_savings_bank + saved_diff, 4)
                                            print(f"         [SAVINGS BANK] Trade WON at {avg_buy_price:.4f} (< {max_maker_price:.2f})! Saved +${saved_diff:.4f} -> Total Bank Credit: ${price_savings_bank:.4f}")
                                    else:
                                        if used_credit > 0:
                                            price_savings_bank = round(price_savings_bank + used_credit, 4)
                                            print(f"         [SAVINGS BANK] Trade LOST. Refunded ${used_credit:.4f} used credit back to bank. Total Bank Credit: ${price_savings_bank:.4f}")

                                # Special Martingale Campaign State Transition
                                p_camp_step = p_trade.get("campaign_step", campaign_step)
                                p_camp_dir = p_trade.get("campaign_trigger_dir", campaign_trigger_dir)
                                p_camp_act = p_trade.get("action_type") or get_campaign_bet(p_camp_step, p_camp_dir or "Up", pattern, stake_multipliers, base_stake_usd, max_cap)[0]

                                campaign_pnl += pnl
                                if won:
                                    bot.record_campaign_result(True)
                                    camp_status_str = f"🎉 CAMPAIGN #{campaign_num} WON on Step {p_camp_step+1}/{max_cap} ({p_camp_act})!"
                                    print(f"         [CAMPAIGN WON] {camp_status_str} Profit: ${campaign_pnl:+.2f}")
                                    in_campaign = False
                                    campaign_step = 0
                                    campaign_trigger_dir = None
                                    campaign_pnl = 0.0
                                    in_fade_window = False
                                    fade_active_side = None
                                    campaign_action_type = None
                                else:
                                    p_camp_step += 1
                                    if p_camp_step < max_cap and p_camp_dir in ("Up", "Down"):
                                        in_campaign = True
                                        campaign_step = p_camp_step
                                        campaign_trigger_dir = p_camp_dir
                                        next_act, next_side, next_stake = get_campaign_bet(
                                            campaign_step, campaign_trigger_dir, pattern, stake_multipliers, base_stake_usd, max_cap
                                        )
                                        campaign_action_type = next_act
                                        fade_active_side = next_side
                                        current_fade_stake_usd = next_stake
                                        camp_status_str = f"⚠️ Step {p_camp_step}/{max_cap} Lost ➡️ Advancing to Step {p_camp_step+1}/{max_cap} ({next_act} {next_side}, ${next_stake:.2f})"
                                        print(f"         [CAMPAIGN STEP ADVANCE] {camp_status_str}")
                                        in_fade_window = True
                                    else:
                                        bot.record_campaign_result(False)
                                        camp_status_str = f"🚨 HARD CAP {max_cap} REACHED! Campaign #{campaign_num} LOST (-${abs(campaign_pnl):.2f}). Sequence reset."
                                        print(f"         [CAMPAIGN HARD STOP] {camp_status_str}")
                                        in_campaign = False
                                        campaign_step = 0
                                        campaign_trigger_dir = None
                                        campaign_pnl = 0.0
                                        in_fade_window = False
                                        fade_active_side = None
                                        campaign_action_type = None

                                fill_time_val = p_trade.get("fill_time")
                                time_to_fill_str = "N/A"
                                avg_fill_time_str = "N/A"
                                if fill_time_val:
                                    time_to_fill = max(0, fill_time_val - p_beat_ts)
                                    session_fills_count += 1
                                    session_total_fill_time += time_to_fill
                                    time_to_fill_str = f"{time_to_fill:.1f}s"
                                    avg_fill_time_str = f"{session_total_fill_time / session_fills_count:.1f}s"

                                s_num = p_trade.get("session_num", fade_session_num)
                                sessions_with_fills.add(s_num)
                                print(f"         [EVALUATION] Previous trade on {p_trade['side']} filled {p_trade['shares']:.4f} shares (Avg Price: {p_trade['avg_price']:.4f}). Beat outcome: {trade_outcome} | Result: {result_str}")
                                print(f"         [SESSION & TOTAL] Session: #{s_num} | Target Best Bid: {p_trade['target_bid']:.4f} | Final Buy Price: {p_trade['avg_price']:.4f} | Session Profit: ${fade_session_profit:+.2f} | Total Profit: ${bot.state.total_pnl:+.2f}")
                                print(f"         [STATS] Trade Fill Time: {time_to_fill_str} | Average Time-to-Fill: {avg_fill_time_str} ({session_fills_count} fills)")

                                # Fetch USDC and portfolio balance
                                usdc_bal = 0.0
                                portfolio_val = 0.0
                                if client is not None:
                                    try:
                                        usdc_bal = await asyncio.to_thread(client.get_usdc_balance)
                                        portfolio_val = await get_portfolio_value(client)
                                        positions_val = max(0.0, portfolio_val - usdc_bal)
                                        print(f"Cash LIVE Balance: ${usdc_bal:.2f} | Positions Value: ${positions_val:.2f} | Portfolio Value: ${portfolio_val:.2f}")
                                    except Exception as bal_err:
                                        print(f"USDC LIVE Balance: $0.00 (Error: {bal_err})")
                                        portfolio_val = usdc_bal
                                else:
                                    usdc_bal = bot.state.current_balance
                                    portfolio_val = usdc_bal
                                    print(f"USDC DRY-RUN Balance: ${usdc_bal:.2f} (Mock)")

                                if notifications_enabled:
                                    fill_time_val = p_trade.get("fill_time") or p_trade.get("last_fill_time")
                                    fill_time_str = datetime.fromtimestamp(fill_time_val).strftime('%H:%M:%S') if fill_time_val else "N/A"
                                    win_emoji = "🎯" if tp_hit else ("🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪"))
                                    result_word = "WIN (TP)" if tp_hit else ("WIN" if won else ("LOSS" if pnl < 0 else "TIE"))
                                    winrate_trade_val = (100.0 * bot.state.wins / bot.state.trade_num) if bot.state.trade_num else 0.0
                                    winrate_dollar_val = (100.0 * bot.state.win_stake_total / bot.state.total_stake) if bot.state.total_stake else 0.0
                                    total_winrate_val = bot.state.total_winrate
                                    avg_price_val = (bot.state.total_fill_price_sum / bot.state.trade_num) if bot.state.trade_num else 0.0

                                    camp_wr_val = bot.state.campaign_winrate
                                    step_info = f"[STEP {p_camp_step+1}/{max_cap}]"
                                    msg = (
                                        f"✅ SPECIAL MARTINGALE - TRADE CLOSED - #{s_num} {step_info}\n"
                                        f"🆔 {strategy_name}\n"
                                        f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                        f"💰 Stake: ${format_masked_amount(p_trade['cost'])}\n"
                                        f"🎯 Filled Price: ${p_trade['avg_price']:.4f} ({fill_time_str})\n"
                                        f"{win_emoji} Result: {result_word} ({format_masked_amount(pnl, sign=True)})\n"
                                        f"🎯 Campaign Status: {camp_status_str}\n"
                                        f"📈 Campaigns: {bot.state.campaigns} | Wins: {bot.state.campaign_wins} | Losses: {bot.state.campaign_losses} (WR: {camp_wr_val:.1f}%)\n"
                                        f"📊 Trades: {bot.state.trade_num} | Wins: {bot.state.wins} | Losses: {bot.state.losses}\n"
                                        f"⚠️ Skips: {bot.state.skipped_count} | Win_S: {bot.state.skipped_wins} | Loss_S: {bot.state.skipped_losses}\n"
                                        f"📈 Winrate: {winrate_trade_val:.2f}% (Trade) | {winrate_dollar_val:.2f}% (Dollars)\n"
                                        f"🌐 Total Winrate (include skips): {total_winrate_val:.2f}%\n"
                                        f"🎯 Avg Price: {avg_price_val:.4f}\n"
                                        f"💵 Total P&L: {format_masked_amount(bot.state.total_pnl, sign=True)}\n"
                                        f"💳 Balance: ${format_masked_amount(usdc_bal)}"
                                    )
                                    send_telegram_notification(msg)

                                del pending_trades[p_beat_ts]

                        # Evaluate and record pending skipped trades once their beats resolve
                        for p_beat_ts in list(pending_skipped_trades.keys()):
                            trade_outcome = sync_outcomes.get(p_beat_ts)
                            if trade_outcome is not None:
                                p_skip = pending_skipped_trades[p_beat_ts]
                                skip_side = p_skip["side"]
                                s_num = p_skip["session_num"]

                                won_skip = (trade_outcome == skip_side)
                                bot.record_skipped_trade(won_skip)

                                skip_result_word = "WIN" if won_skip else "LOSS"

                                # Special Martingale Unfilled Handling:
                                p_camp_step = p_skip.get("campaign_step", campaign_step)
                                p_camp_dir = p_skip.get("campaign_trigger_dir", campaign_trigger_dir)
                                p_camp_act = p_skip.get("action_type") or get_campaign_bet(p_camp_step, p_camp_dir or "Up", pattern, stake_multipliers, base_stake_usd, max_cap)[0]

                                if p_camp_dir not in ("Up", "Down") or (p_camp_step == 0 and trade_outcome != p_camp_dir):
                                    # Streak reversed while unfilled on Step 0: cleanly reset to Step 0 with $0 loss!
                                    in_campaign = False
                                    campaign_step = 0
                                    campaign_trigger_dir = None
                                    in_fade_window = False
                                    fade_active_side = None
                                    campaign_action_type = None
                                    skip_action_desc = "Streak broke while unfilled on Step 1. Cleanly reset to Step 1 ($0 loss)."
                                else:
                                    # Streak continued or Step > 0: PRESERVE STEP!
                                    in_campaign = True
                                    campaign_step = p_camp_step
                                    campaign_trigger_dir = p_camp_dir
                                    campaign_action_type, fade_active_side, current_fade_stake_usd = get_campaign_bet(
                                        campaign_step, campaign_trigger_dir, pattern, stake_multipliers, base_stake_usd, max_cap
                                    )
                                    in_fade_window = True
                                    skip_action_desc = f"Preserving Step {p_camp_step+1}/{max_cap} ({campaign_action_type} {fade_active_side}) with stake ${current_fade_stake_usd:.2f} for next beat."

                                print(f"         [SKIPPED EVALUATION] Unfilled session #{s_num} on {skip_side} beat outcome: {trade_outcome} -> Result: {skip_result_word} (UNFILLED - $0 risked)")
                                print(f"         [SKIPPED ACTION] {skip_action_desc}")
                                print(f"         [SKIPPED STATS] Total Skipped: {bot.state.skipped_count} | Wins_Skip: {bot.state.skipped_wins} | Losses_skip: {bot.state.skipped_losses}")

                                if notifications_enabled:
                                    total_winrate_val = bot.state.total_winrate
                                    camp_wr_val = bot.state.campaign_winrate
                                    step_info = f"[STEP {p_camp_step+1}/{max_cap}]"
                                    msg = (
                                        f"🚨 SPECIAL MARTINGALE - ORDER UNFILLED (SKIPPED) - #{s_num} {step_info}\n"
                                        f"🆔 {strategy_name}\n"
                                        f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                        f"❌ Result: {skip_result_word} (UNFILLED - $0 risked)\n"
                                        f"🔄 Action: {skip_action_desc}\n"
                                        f"📈 Campaigns: {bot.state.campaigns} | Wins: {bot.state.campaign_wins} | Losses: {bot.state.campaign_losses} (WR: {camp_wr_val:.1f}%)\n"
                                        f"⚠️ Skips: {bot.state.skipped_count} | Win_S: {bot.state.skipped_wins} | Loss_S: {bot.state.skipped_losses}\n"
                                        f"🌐 Total Winrate (include skips): {total_winrate_val:.2f}%"
                                    )
                                    send_telegram_notification(msg)

                                del pending_skipped_trades[p_beat_ts]

                        # Evaluate and record pending Filter_Skip trades once their candidate beats resolve
                        for p_beat_ts in list(pending_filter_skipped_trades.keys()):
                            trade_outcome = sync_outcomes.get(p_beat_ts)
                            if trade_outcome is not None:
                                p_fs = pending_filter_skipped_trades[p_beat_ts]
                                fs_side = p_fs["side"]
                                fs_num = p_fs.get("session_num", 1)
                                fs_delta_range = p_fs.get("delta_range", 0.0)
                                fs_delta_body = p_fs.get("delta_body", 0.0)

                                won_fs = (trade_outcome == fs_side)
                                bot.record_filter_skipped_trade(won_fs)

                                fs_result_word = "WIN" if won_fs else "LOSS"
                                fs_total = bot.state.filter_skipped_count
                                fs_wr = (100.0 * bot.state.filter_skipped_wins / fs_total) if fs_total > 0 else 0.0

                                print(f"         [FILTER_SKIP EVALUATION] Filter_Skip #{fs_num} on {fs_side} (15m Range: ${fs_delta_range:.2f} | Body: ${fs_delta_body:.2f}) resolved: {trade_outcome} -> Result: {fs_result_word} (FILTER_SKIP)")
                                print(f"         [FILTER_SKIP STATS] Total Filter_Skips: {bot.state.filter_skipped_count} | Wins: {bot.state.filter_skipped_wins} | Losses: {bot.state.filter_skipped_losses} (WR: {fs_wr:.1f}%)")

                                if notifications_enabled:
                                    cand_side_abbr = "U" if fs_side == "Up" else ("D" if fs_side == "Down" else fs_side)
                                    outcome_abbr = "U" if trade_outcome == "Up" else ("D" if trade_outcome == "Down" else trade_outcome)
                                    fs_vol = p_fs.get('vol_pct')
                                    fs_body = p_fs.get('delta_body')
                                    c_parts = []
                                    if fs_delta_range > 0:
                                        c_parts.append(f"R: ${fs_delta_range:.1f}")
                                    if fs_body is not None:
                                        c_parts.append(f"B: ${fs_body:.1f}")
                                    if fs_vol is not None:
                                        c_parts.append(f"V: {fs_vol:.4f}%")
                                    cond_line = f"📊 Conditions: {' | '.join(c_parts)}\n" if c_parts else ''
                                    step_info = f"[STEP {campaign_step+1}/{max_cap}]"
                                    msg = (
                                        f"🚨 SPECIAL MARTINGALE - FILTER_SKIP RESOLUTION - #{fs_num} {step_info}\n"
                                        f"🆔 {strategy_name}\n"
                                        f"📅 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                        f"🎯 Candidate Bet: {cand_side_abbr} | Beat Outcome: {outcome_abbr}\n"
                                        f'{cond_line}'
                                        f"❌ Result: {fs_result_word} (FILTER_SKIP)\n"
                                        f"🔄 Action: Filter_Skip resolved ($0 risked). Step preserved.\n"
                                        f"⚠️ Filter_Skips: {bot.state.filter_skipped_count} | Win_FS: {bot.state.filter_skipped_wins} | Loss_FS: {bot.state.filter_skipped_losses}\n"
                                        f"🌐 Filter_Skip Winrate: {fs_wr:.2f}%"
                                    )
                                    send_telegram_notification(msg)

                                del pending_filter_skipped_trades[p_beat_ts]

                        # Load pre-placed order details at the start of the beat
                        if in_fade_window:
                            beat_ts = current_beat_start
                            if beat_ts in fade_orders:
                                ord_info = fade_orders[beat_ts]
                                active_order_id = ord_info["order_id"]
                                active_order_token_id = ord_info["token_id"]
                                active_order_side = ord_info["side"]
                                active_order_stake = ord_info["stake"]
                                active_order_shares = ord_info["shares"]
                                active_order_price = ord_info["price"]
                                active_order_target_bid = ord_info["price"]
                                active_order_beat_ts = beat_ts
                                active_order_filled_shares = 0.0
                                active_order_filled_val = 0.0
                                chased_filled_shares = 0.0
                                chased_filled_val = 0.0
                                chase_attempts = 0
                                last_chase_check_time = 0
                                active_order_cancel_checked = False
                                active_order_adjusted = False
                                print(f"         [FADE RUN] Restored pre-placed order {active_order_id[:8]}... for current beat {beat_ts}")

            # Dynamic WebSocket subscriptions maintenance (now handled by background loop)
            pass

            # Periodic fill checking (every 3-5 seconds) during the active beat
            if (active_order_id is not None
                and active_order_beat_ts == current_beat_start
                and elapsed_in_beat < (beat_duration_sec - seconds_before_close - 5)
                and now - last_fill_check_time >= 3):

                last_fill_check_time = now
                try:
                    if mode == BotMode.DRY_RUN:
                        if active_order_filled_shares < active_order_shares:
                            b_bid, b_ask = await get_token_book_prices(fetcher, active_order_token_id)
                            if active_order_id.startswith("dry_run_ioc"):
                                if b_ask is not None and b_ask <= active_order_price:
                                    active_order_filled_shares = active_order_shares
                                    active_order_filled_val = active_order_shares * b_ask
                                    active_order_fill_time = now
                                    print(f"         [FILL SUCCESS] [DRY RUN IOC] Live Ask (${b_ask:.2f}) <= Taker Limit (${active_order_price:.2f}). Filled {active_order_filled_shares:.4f} shares at ${b_ask:.2f}")
                                else:
                                    print(f"         [DRY RUN IOC EXPIRED] Best Ask ({b_ask}) > Taker Limit (${active_order_price:.2f}). IOC order expired with 0 fills.")
                                    active_order_id = None
                            else:
                                # Regular maker order fill check: Fill only if best_ask <= active_order_price
                                if b_ask is not None and b_ask <= active_order_price:
                                    active_order_filled_shares = active_order_shares
                                    active_order_filled_val = active_order_shares * active_order_price
                                    active_order_fill_time = now
                                    print(f"         [FILL SUCCESS] [DRY RUN MAKER] Live Ask (${b_ask:.2f}) <= Maker Price (${active_order_price:.2f}). Order {active_order_id[:8]}... fully filled: {active_order_filled_shares:.4f} shares at {active_order_price:.2f}")
                    elif client is not None:
                        resp = await asyncio.to_thread(client.get_order_normalized, active_order_id)
                        if isinstance(resp, dict):
                            status = resp.get("status", "").lower()
                            size_matched = float(resp.get("size_matched", 0))
                            original_size = float(resp.get("original_size", 0))

                            avg_price_val = resp.get("avg_price") or resp.get("avgPrice") or resp.get("averagePrice") or resp.get("average_price")
                            actual_price = float(avg_price_val) if avg_price_val else active_order_price

                            if size_matched > active_order_filled_shares:
                                new_fill = size_matched - active_order_filled_shares
                                active_order_filled_shares = size_matched
                                active_order_filled_val = size_matched * actual_price
                                active_order_last_fill_time = now
                                if status == "matched":
                                    active_order_fill_time = now
                                    print(f"         [FILL SUCCESS] Order {active_order_id[:8]}... fully filled: {size_matched:.4f} shares")
                                else:
                                    print(f"         [PARTIAL FILL] Order {active_order_id[:8]}... partially filled: {size_matched:.4f}/{original_size:.4f} shares (+{new_fill:.4f} shares)")

                    # Trigger Take-Profit limit sell placement upon buy fill
                    if tp_enabled and not active_order_tp_placed and active_order_filled_shares > 0:
                        avg_fill = (active_order_filled_val / active_order_filled_shares) if active_order_filled_shares > 0 else active_order_price
                        active_order_tp_price = min(0.99, max(round(avg_fill + 0.01, 2), round(avg_fill * (1.0 + tp_target_pct), 2)))
                        active_order_tp_shares = active_order_filled_shares
                        active_order_tp_id = await place_bot_sell_order(
                            mode, client, active_order_token_id, active_order_tp_price, active_order_tp_shares, active_order_side
                        )
                        if active_order_tp_id:
                            active_order_tp_placed = True
                            print(f"         [TAKE-PROFIT ORDER PLACED] Limit Sell posted at ${active_order_tp_price:.2f} for {active_order_tp_shares:.4f} shares (+{tp_target_pct*100:.1f}%) | ID: {active_order_tp_id[:8]}...")

                    # Monitor Take-Profit Order Fill if resting
                    if active_order_tp_placed and not active_order_tp_filled and active_order_tp_id is not None:
                        try:
                            if mode == BotMode.DRY_RUN:
                                b_bid, b_ask = await get_token_book_prices(fetcher, active_order_token_id)
                                if b_bid is not None and b_bid >= active_order_tp_price:
                                    active_order_tp_filled = True
                                    active_order_tp_fill_time = now
                                    net_tp_pnl = (active_order_tp_price * active_order_tp_shares) - active_order_filled_val
                                    print(f"         🎯 [TAKE-PROFIT FILLED] [DRY RUN] Market Bid (${b_bid:.2f}) >= Sell Target (${active_order_tp_price:.2f}). Locked in +${net_tp_pnl:.2f} profit (+{tp_target_pct*100:.1f}%)!")
                            elif client is not None:
                                tp_resp = await asyncio.to_thread(client.get_order_normalized, active_order_tp_id)
                                if isinstance(tp_resp, dict):
                                    tp_status = tp_resp.get("status", "").lower()
                                    tp_matched = float(tp_resp.get("size_matched", 0))
                                    if tp_status == "matched" or (tp_matched >= active_order_tp_shares and active_order_tp_shares > 0):
                                        active_order_tp_filled = True
                                        active_order_tp_fill_time = now
                                        net_tp_pnl = (active_order_tp_price * active_order_tp_shares) - active_order_filled_val
                                        print(f"         🎯 [TAKE-PROFIT FILLED] [REAL] Limit Sell executed on Kalshi! Locked in +${net_tp_pnl:.2f} profit (+{tp_target_pct*100:.1f}%)!")
                        except Exception as tp_check_err:
                            print(f"         [WARNING] TP order fill check failed: {tp_check_err}")
                except Exception as fill_check_err:
                    print(f"         [WARNING] Periodic fill check failed: {fill_check_err}")

            # Delayed Maker Order Placement (when min_maker_sec > 0)
            delayed_window_end = stop_maker_sec if stop_maker_sec is not None else (beat_duration_sec - seconds_before_close - 5)
            prev_beat_ts_for_entry = current_beat_start - beat_duration_sec
            prev_beat_unresolved = (prev_beat_ts_for_entry in pending_trades) or (prev_beat_ts_for_entry in pending_skipped_trades)
            if (min_maker_sec is not None and min_maker_sec > 0
                and in_fade_window
                and fade_active_side is not None
                and active_order_id is None
                and delayed_placement_beat_ts != current_beat_start
                and elapsed_in_beat >= min_maker_sec
                and prev_beat_unresolved):
                # Campaign step depends on the previous beat's settled result: wait for the sync.
                if elapsed_in_beat >= delayed_window_end:
                    delayed_placement_beat_ts = current_beat_start
                    print(f"         [DELAYED ENTRY SKIPPED] Previous beat {prev_beat_ts_for_entry} still unsettled at {elapsed_in_beat}s (>= {delayed_window_end}s). No order this beat.")
                elif delayed_wait_log_beat_ts != current_beat_start:
                    delayed_wait_log_beat_ts = current_beat_start
                    print(f"         [DELAYED ENTRY WAIT] Waiting for previous beat {prev_beat_ts_for_entry} to settle before placing campaign order...")
            elif (min_maker_sec is not None and min_maker_sec > 0
                and in_fade_window
                and fade_active_side is not None
                and active_order_id is None
                and delayed_placement_beat_ts != current_beat_start
                and elapsed_in_beat >= min_maker_sec
                and elapsed_in_beat < delayed_window_end):

                delayed_placement_beat_ts = current_beat_start
                step_lbl = f"Step {campaign_step+1}/{max_cap} {campaign_action_type} " if in_campaign else ""
                print(f"         [DELAYED ENTRY] Beat elapsed: {elapsed_in_beat}s >= {min_maker_sec}s. Submitting delayed maker order {step_lbl}for {fade_active_side} (${current_fade_stake_usd:.2f})...")
                await place_beat_order(current_beat_start, fade_active_side, current_fade_stake_usd)

            # Post-only cancellation & taker order placement check
            if (active_order_id is not None
                and active_order_beat_ts == current_beat_start
                and stop_maker_sec is not None
                and stop_maker_sec > 0
                and elapsed_in_beat >= stop_maker_sec
                and elapsed_in_beat < (beat_duration_sec - seconds_before_close - 5)
                and not active_order_cancel_checked
                and active_order_shares is not None
                and active_order_price is not None
                and (active_order_shares - active_order_filled_shares) >= 1.0):

                active_order_cancel_checked = True
                try:
                    print(f"         [POST-ONLY CANCEL CHECK] Beat elapsed: {elapsed_in_beat}s >= {stop_maker_sec}s. Checking/canceling open maker order {active_order_id[:8]}...")
                    maker_filled_shares, maker_status, maker_avg_price = await check_order_fill(
                        mode, client, active_order_id, active_order_shares, active_order_price,
                        known_filled_shares=active_order_filled_shares,
                        known_avg_price=(active_order_filled_val / active_order_filled_shares) if active_order_filled_shares > 0 else active_order_price
                    )
                    if maker_filled_shares > active_order_filled_shares:
                        active_order_last_fill_time = int(time.time())

                    # Cancel the open maker order to prevent late decaying fills
                    try:
                        await cancel_bot_order(mode, client, active_order_id, active_order_token_id)
                        print(f"         [CANCEL SUCCESS] Successfully cancelled open maker order {active_order_id[:8]}.")
                    except Exception as c_err:
                        print(f"         [WARNING] Failed to cancel expired maker order: {c_err}")

                    # Store what was filled on the maker order
                    chased_filled_shares += maker_filled_shares
                    chased_filled_val += (maker_filled_shares * maker_avg_price)

                    maker_spent = maker_filled_shares * maker_avg_price
                    remaining_stake = active_order_stake - maker_spent
                    remaining_shares = active_order_shares - maker_filled_shares

                    print(f"         [POST-ONLY CANCEL RESULT] Maker filled: {maker_filled_shares:.4f}/{active_order_shares:.4f} shares | Spent: ${maker_spent:.2f}/${active_order_stake:.2f}")

                    if tp_enabled and not active_order_tp_placed and maker_filled_shares > 0:
                        avg_fill = (maker_spent / maker_filled_shares) if maker_filled_shares > 0 else active_order_price
                        active_order_tp_price = min(0.99, max(round(avg_fill + 0.01, 2), round(avg_fill * (1.0 + tp_target_pct), 2)))
                        active_order_tp_shares = maker_filled_shares
                        active_order_tp_id = await place_bot_sell_order(
                            mode, client, active_order_token_id, active_order_tp_price, active_order_tp_shares, active_order_side
                        )
                        if active_order_tp_id:
                            active_order_tp_placed = True
                            print(f"         [TAKE-PROFIT ORDER PLACED] Limit Sell posted at ${active_order_tp_price:.2f} for {active_order_tp_shares:.4f} shares (+{tp_target_pct*100:.1f}%) | ID: {active_order_tp_id[:8]}...")

                    if maker_status == "unknown":
                        print(f"         [WARNING] Maker order status is unknown due to API failure. Skipping taker order placement to prevent double-buying.")
                        chased_filled_shares = 0.0
                        chased_filled_val = 0.0
                    elif remaining_shares >= 1.0 and remaining_stake >= 1.00 and taker_limit_price is not None and taker_limit_price > 0:
                        taker_shares = remaining_stake / taker_limit_price
                        taker_shares = min(remaining_shares, taker_shares)

                        # Adjust to minimum contract size required by Kalshi
                        taker_shares = adjust_shares_to_minimum(taker_shares, taker_limit_price, "IOC TAKER")

                        print(f"         [IOC TAKER PLACE] Placing IOC Taker order (Last Chance): {taker_shares:.4f} shares up to {taker_limit_price:.2f}")

                        taker_order_id = await place_bot_ioc_order(
                            mode, client, active_order_token_id, taker_limit_price, taker_shares, active_order_side
                        )

                        if taker_order_id:
                            # Switch active tracking to the new IOC order
                            active_order_id = taker_order_id
                            active_order_shares = taker_shares
                            active_order_price = taker_limit_price
                            active_order_target_bid = taker_limit_price
                            active_order_filled_shares = 0.0
                            active_order_filled_val = 0.0
                            active_order_fill_time = None
                            active_order_last_fill_time = None

                            if mode == BotMode.DRY_RUN:
                                # In Dry Run, check live taker liquidity immediately
                                b_bid, b_ask = await get_token_book_prices(fetcher, active_order_token_id)
                                if b_ask is not None and b_ask <= taker_limit_price:
                                    active_order_filled_shares = taker_shares
                                    active_order_filled_val = taker_shares * b_ask
                                    active_order_fill_time = int(time.time())
                                    print(f"         [FILL SUCCESS] [DRY RUN IOC] Filled immediately at live ask: ${b_ask:.2f} ({taker_shares:.4f} shares)")
                                else:
                                    print(f"         [DRY RUN IOC EXPIRED] Best Ask ({b_ask}) > Taker Limit (${taker_limit_price:.2f}). 0 shares filled.")
                                    active_order_id = None
                            else:
                                print(f"         [IOC TAKER SUCCESS] Now tracking IOC order {active_order_id[:8]}... as active.")
                        else:
                            print(f"         [WARNING] IOC Taker order placement failed or no liquidity <= {taker_limit_price:.2f}.")
                            chased_filled_shares = 0.0
                            chased_filled_val = 0.0
                    else:
                        if maker_filled_shares >= active_order_shares or remaining_stake < 1.0:
                            print(f"         [POST-ONLY CANCEL RESULT] Maker order fully filled or stake spent. No taker order needed.")
                        else:
                            print(f"         [POST-ONLY CANCEL RESULT] Maker order timed out unfilled ({elapsed_in_beat}s >= {stop_maker_sec}s). {maker_filled_shares:.4f} shares filled. No taker order configured.")
                        chased_filled_shares = 0.0
                        chased_filled_val = 0.0
                except Exception as cancel_err:
                    print(f"         [WARNING] Post-only cancel & taker placement failed: {cancel_err}")

            # Retry logic for replacement orders that failed to place or verify
            if (active_order_replacement_pending
                and active_order_beat_ts == current_beat_start
                and elapsed_in_beat < (beat_duration_sec - seconds_before_close - 5)):
                try:
                    # Check if stop_maker_sec elapsed OR 5 consecutive post-only cross retries occurred
                    if (stop_maker_sec is not None and elapsed_in_beat >= stop_maker_sec) or active_order_cross_retries >= 5:
                        if taker_limit_price is not None and taker_limit_price > 0:
                            reason = f"Beat elapsed {elapsed_in_beat}s >= {stop_maker_sec}s" if (stop_maker_sec is not None and elapsed_in_beat >= stop_maker_sec) else "5 consecutive maker post-only cross rejections"
                            print(f"         [IOC TAKER TRANSITION] {reason}. Transitioning replacement order to IOC Taker at {taker_limit_price:.2f}...")
                            active_order_replacement_pending = False
                            active_order_cross_retries = 0
                            if active_order_stake and active_order_stake >= 1.0:
                                ioc_shares = adjust_shares_to_minimum(round(active_order_stake / taker_limit_price, 4), taker_limit_price, "IOC TAKER")
                                ioc_order_id = await place_bot_ioc_order(mode, client, active_order_token_id, taker_limit_price, ioc_shares, active_order_side)
                                if ioc_order_id:
                                    active_order_id = ioc_order_id
                                    active_order_shares = ioc_shares
                                    active_order_price = taker_limit_price
                                    active_order_target_bid = taker_limit_price
                                    active_order_filled_shares = 0.0
                                    active_order_filled_val = 0.0
                                    active_order_fill_time = None
                                    active_order_last_fill_time = None
                                    print(f"         [IOC TAKER SUCCESS] Now tracking IOC order {active_order_id[:8]}... as active.")
                                else:
                                    print(f"         [WARNING] IOC Taker order placement failed for replacement.")
                        else:
                            active_order_replacement_pending = False
                            active_order_cross_retries = 0
                    else:
                        best_bid, best_ask = await get_token_book_prices(fetcher, active_order_token_id)
                        new_price, credit_used = calculate_order_price_with_bank(
                            best_bid, price_savings_bank, enable_price_savings_bank, best_ask=best_ask, base_cap=max_maker_price
                        )
                        remaining_stake = active_order_stake if active_order_stake and active_order_stake >= 1.0 else (active_order_replacement_shares * max_maker_price)
                        new_shares = round(remaining_stake / new_price, 4)
                        new_shares = adjust_shares_to_minimum(new_shares, new_price, "PRICE ADJUST RETRY")
                        
                        print(f"         [PRICE ADJUST RETRY] Attempting maker replacement: {new_shares:.4f} shares of {active_order_side} at {new_price:.4f} (best bid: {best_bid}, best ask: {best_ask}) (Attempt {active_order_cross_retries + 1}/5)...")
                        
                        new_order_id = await place_bot_order(
                            mode, client, active_order_token_id, new_price, new_shares, active_order_side, post_only=True
                        )
                        
                        if new_order_id:
                            verified = await verify_order_in_book(mode, client, new_order_id)
                            if verified:
                                active_order_id = new_order_id
                                active_order_shares = new_shares
                                active_order_price = new_price
                                active_order_target_bid = new_price
                                active_order_filled_shares = 0.0
                                active_order_filled_val = 0.0
                                active_order_fill_time = None
                                active_order_last_fill_time = None
                                active_order_replacement_pending = False
                                active_order_replacement_shares = 0.0
                                active_order_cross_retries = 0
                                if credit_used > 0:
                                    price_savings_bank = round(price_savings_bank - credit_used, 4)
                                    active_order_used_bank_credit += credit_used
                                print(f"         [PRICE ADJUST RETRY SUCCESS] Successfully placed and verified replacement order {new_order_id[:8]} at {new_price:.4f}")
                            else:
                                active_order_cross_retries += 1
                                print(f"         [WARNING] Retry replacement order failed verification (Attempt {active_order_cross_retries}/5). Will retry in 1s...")
                                await asyncio.sleep(1.0)
                        else:
                            active_order_cross_retries += 1
                            print(f"         [WARNING] Retry replacement order placement failed / crossed (Attempt {active_order_cross_retries}/5). Will retry in 1s...")
                            await asyncio.sleep(1.0)
                except Exception as retry_err:
                    active_order_cross_retries += 1
                    print(f"         [WARNING] Retry replacement order failed: {retry_err} (Attempt {active_order_cross_retries}/5). Will retry in 1s...")
                    await asyncio.sleep(1.0)

            # Maker price adjustment logic (track every 5s if price is smaller than max_maker_price)
            if (active_order_id is not None
                and active_order_beat_ts == current_beat_start
                and elapsed_in_beat < (beat_duration_sec - seconds_before_close - 5)
                and active_order_adjusted_enabled
                and active_order_price is not None
                and active_order_price < max_maker_price):

                now_t = time.time()
                if active_order_last_adjust_time is None or (now_t - active_order_last_adjust_time) >= 5.0:
                    active_order_last_adjust_time = now_t

                    if active_order_shares is not None and (active_order_shares - active_order_filled_shares) >= 1.0:
                        try:
                            # 1. Fetch current book bid/ask
                            best_bid, best_ask = await get_token_book_prices(fetcher, active_order_token_id)
                            if best_bid is not None:
                                new_price, credit_used = calculate_order_price_with_bank(
                                    best_bid, price_savings_bank, enable_price_savings_bank, best_ask=best_ask, base_cap=max_maker_price
                                )
                                if new_price > active_order_price:
                                    print(f"         [PRICE ADJUST] Beat elapsed: {elapsed_in_beat}s. Open maker order {active_order_id[:8]} at {active_order_price:.4f} is unfilled and new target is {new_price:.4f}. Adjusting...")

                                    # 2. Query order status one last time to capture any partial fills before canceling
                                    maker_filled_shares, maker_status, maker_avg_price = await check_order_fill(
                                        mode, client, active_order_id, active_order_shares, active_order_price,
                                        known_filled_shares=active_order_filled_shares,
                                        known_avg_price=(active_order_filled_val / active_order_filled_shares) if active_order_filled_shares > 0 else active_order_price
                                    )
                                    if maker_filled_shares > active_order_filled_shares:
                                        active_order_last_fill_time = int(time.time())

                                    # Cancel old order if not already confirmed terminal by check_order_fill
                                    if maker_status not in ("canceled", "matched", "executed"):
                                        try:
                                            await cancel_bot_order(mode, client, active_order_id, active_order_token_id)
                                            print(f"         [PRICE ADJUST] Cancelled old maker order {active_order_id[:8]}")
                                        except Exception as cancel_err:
                                            print(f"         [WARNING] Failed to cancel old maker order for adjustment: {cancel_err}")
                                    else:
                                        print(f"         [PRICE ADJUST] Old maker order {active_order_id[:8]} confirmed {maker_status} (fills: {maker_filled_shares:.4f}).")

                                    # Accumulate partial fills if any
                                    chased_filled_shares = chased_filled_shares + maker_filled_shares
                                    chased_filled_val = chased_filled_val + (maker_filled_shares * maker_avg_price)

                                    # Calculate remaining stake
                                    spent = maker_filled_shares * maker_avg_price
                                    remaining_stake = max(0.0, active_order_stake - spent)
                                    active_order_stake = remaining_stake

                                    print(f"         [PRICE ADJUST BUDGET] Order {active_order_id[:8]} filled: {maker_filled_shares:.4f} shares (${spent:.2f}). Remaining Budget: ${remaining_stake:.2f} (Target Price: {new_price:.4f})")

                                    if remaining_stake >= 1.00:
                                        new_shares = round(remaining_stake / new_price, 4)
                                        new_shares = adjust_shares_to_minimum(new_shares, new_price, "PRICE ADJUST ADJUSTMENT")

                                        print(f"         [PRICE ADJUST] Placing replacement order: {new_shares:.4f} shares of {active_order_side} at {new_price:.4f} (post-only=True)")

                                        new_order_id = await place_bot_order(
                                            mode, client, active_order_token_id, new_price, new_shares, active_order_side, post_only=True
                                        )

                                        if new_order_id:
                                            verified = await verify_order_in_book(mode, client, new_order_id)
                                            if verified:
                                                # Update active tracking to the new replacement order
                                                active_order_id = new_order_id
                                                active_order_shares = new_shares
                                                active_order_price = new_price
                                                active_order_target_bid = new_price
                                                active_order_filled_shares = 0.0
                                                active_order_filled_val = 0.0
                                                active_order_fill_time = None
                                                if credit_used > 0:
                                                    price_savings_bank = round(price_savings_bank - credit_used, 4)
                                                    active_order_used_bank_credit += credit_used
                                                print(f"         [PRICE ADJUST SUCCESS] Successfully placed and verified replacement order {new_order_id[:8]} at {new_price:.4f}")
                                            else:
                                                print(f"         [WARNING] Replacement order {new_order_id} failed verification. Setting pending retry.")
                                                active_order_id = None
                                                active_order_replacement_pending = True
                                                active_order_replacement_shares = new_shares
                                        else:
                                            print(f"         [WARNING] Replacement order placement failed. Setting pending retry.")
                                            active_order_id = None
                                            active_order_replacement_pending = True
                                            active_order_replacement_shares = new_shares
                                    else:
                                        print(f"         [PRICE ADJUST] Maker order was mostly filled. No replacement needed.")
                                        active_order_id = None
                        except Exception as adjust_err:
                            print(f"         [WARNING] Maker price adjustment check failed: {adjust_err}")


            # Print best ask of the active token from stop_maker_sec until the end of the beat (before decision window)
            monitored_token_id = active_order_token_id
            monitored_side = active_order_side
            if monitored_token_id is None and in_fade_window and fade_active_side:
                try:
                    monitored_tokens = fetcher.get_token_ids_for_beat(current_beat_start)
                    monitored_token_id = monitored_tokens.get(fade_active_side)
                    monitored_side = fade_active_side
                except Exception:
                    pass

            if (monitored_token_id is not None
                and stop_maker_sec is not None
                and elapsed_in_beat >= stop_maker_sec
                and elapsed_in_beat < (beat_duration_sec - seconds_before_close)):
                try:
                    _, best_ask_val = await get_token_book_prices(fetcher, monitored_token_id)
                    if best_ask_val != last_printed_best_ask or monitored_token_id != last_printed_token_id:
                        last_printed_best_ask = best_ask_val
                        last_printed_token_id = monitored_token_id
                        #print(f"         [BOOK INFO] Beat elapsed: {elapsed_in_beat}s | Active side: {monitored_side} | Best Ask: {best_ask_val:.4f}")
                except Exception as e:
                    print(f"         [WARNING] Failed to fetch book info: {e}")

            # Decision window before beat close
            trigger_seconds = beat_duration_sec - seconds_before_close
            if elapsed_in_beat >= trigger_seconds and last_processed_beat_ts != current_beat_start:
                last_processed_beat_ts = current_beat_start
                print(f"\n⏰ Beat Close Decision Window: Entered at {elapsed_in_beat}s elapsed. Current beat start: {current_beat_start}")

                # Check few seconds before each beat start, if the best bid is more close to max_maker_price than the already order, adjust
                next_beat_ts = current_beat_start + beat_duration_sec
                if in_fade_window and next_beat_ts in fade_orders:
                    ord_info = fade_orders[next_beat_ts]
                    token_id = ord_info["token_id"]
                    old_price = ord_info["price"]

                    next_bid, next_ask = await get_token_book_prices(fetcher, token_id)
                    if next_bid is not None:
                        old_credit = ord_info.get("used_bank_credit", 0.0)
                        temp_bank = price_savings_bank + old_credit
                        new_price, new_credit = calculate_order_price_with_bank(
                            next_bid, temp_bank, enable_price_savings_bank, base_cap=max_maker_price
                        )
                        if new_price > old_price + 0.001:
                            print(f"         [PRE-PLACEMENT ADJUSTMENT] Next beat best bid {next_bid:.4f} (adjusted: {new_price:.4f}) is closer to {max_maker_price:.2f} than pre-placed price {old_price:.4f}. Adjusting...")
                            old_order_id = ord_info["order_id"]

                            # 1. Cancel old order
                            try:
                                await cancel_bot_order(mode, client, old_order_id)
                            except Exception as e:
                                print(f"         [WARNING] Failed to cancel old pre-placed order: {e}")

                            # 2. Place new order
                            new_shares = round(current_fade_stake_usd / new_price, 4)
                            new_shares = adjust_shares_to_minimum(new_shares, new_price, "PRE-PLACEMENT ADJUSTMENT")
                            new_order_id = await place_bot_order(
                                mode, client, token_id, new_price, new_shares, fade_active_side, post_only=True
                            )

                            # 3. Verify and update
                            if new_order_id:
                                verified = await verify_order_in_book(mode, client, new_order_id)
                                if verified:
                                    price_savings_bank = round(temp_bank - new_credit, 4)
                                    if new_credit > 0:
                                        print(f"         [SAVINGS BANK] Using ${new_credit:.4f} bank credit for adjusted price {new_price:.4f}. Remaining Bank Balance: ${price_savings_bank:.4f}")
                                    fade_orders[next_beat_ts].update({
                                        "order_id": new_order_id,
                                        "price": new_price,
                                        "shares": new_shares,
                                        "target_bid": new_price,
                                        "used_bank_credit": new_credit,
                                    })
                                    print(f"         [PRE-PLACEMENT ADJUST SUCCESS] Pre-placed order for beat {next_beat_ts} successfully adjusted to price {new_price:.4f}")
                                else:
                                    print(f"         [WARNING] Adjusted order failed verification. Will retry placement at beat start.")
                                    price_savings_bank = round(temp_bank, 4)
                                    fade_orders.pop(next_beat_ts, None)
                            else:
                                print(f"         [WARNING] Adjusted order placement failed. Will retry placement at beat start.")
                                price_savings_bank = round(temp_bank, 4)
                                fade_orders.pop(next_beat_ts, None)

                # Fetch current beat's UP price to make evaluations/decisions
                current_tokens = fetcher.get_token_ids_for_beat(current_beat_start)
                up_token_id = current_tokens.get("Up")
                if not up_token_id:
                    print("         [ERROR] Current beat UP token ID not found. Skipping decision.")
                    continue

                # Loop check every second until the price is decisive or beat ends
                is_decisive = True
                current_close_side = None
                best_bid = 0.50
                best_ask = 0.50

                while True:
                    now_time = int(time.time())
                    current_utc_start = utc_beat_start_ts(now_time, beat_duration_min)
                    elapsed_now = now_time - current_utc_start

                    if elapsed_now >= (beat_duration_sec - 2) or current_utc_start != current_beat_start:
                        break

                    best_bid, best_ask = await get_token_book_prices(fetcher, up_token_id)
                    current_up_price = best_bid

                    # Get active/monitored token's best ask if applicable
                    active_ask_str = ""
                    monitored_token_id = active_order_token_id
                    monitored_side = active_order_side
                    if monitored_token_id is None and in_fade_window and fade_active_side:
                        try:
                            monitored_tokens = fetcher.get_token_ids_for_beat(current_beat_start)
                            monitored_token_id = monitored_tokens.get(fade_active_side)
                            monitored_side = fade_active_side
                        except Exception:
                            pass

                    if monitored_token_id is not None:
                        if monitored_token_id == up_token_id:
                            active_ask_str = f" | Active ({monitored_side}) Best Ask: {best_ask:.4f}"
                        else:
                            try:
                                _, a_ask = await get_token_book_prices(fetcher, monitored_token_id)
                                active_ask_str = f" | Active ({monitored_side}) Best Ask: {a_ask:.4f}"
                            except Exception:
                                pass

                    if current_up_price > decisive_up:
                        current_close_side = "Up"
                        print(f"         [PRICE CHECK] Decisive close detected (UP > {decisive_up:.2f}): Best Bid: {best_bid:.2f} | Best Ask: {best_ask:.2f}{active_ask_str} at {elapsed_now}s elapsed.")
                        break
                    elif current_up_price < decisive_down:
                        current_close_side = "Down"
                        print(f"         [PRICE CHECK] Decisive close detected (DOWN < {decisive_down:.2f}): Best Bid: {best_bid:.2f} | Best Ask: {best_ask:.2f}{active_ask_str} at {elapsed_now}s elapsed.")
                        break
                    else:
                        print(f"         [PRICE CHECK] Price not decisive yet: Best Bid: {best_bid:.2f} | Best Ask: {best_ask:.2f}{active_ask_str} at {elapsed_now}s elapsed. Retrying in 1s...")


                        await asyncio.sleep(1.0)

                if current_close_side is None:
                    is_decisive = False
                    if best_bid >= 0.50:
                        current_close_side = "Up"
                    else:
                        current_close_side = "Down"
                    print(f"         [DECISION] Beat close was not decisive. Fallback 50/50: {current_close_side}")

                # Load pre-placed order details if available and not yet loaded
                if current_beat_start in fade_orders and current_beat_start not in loaded_fade_beats:
                    loaded_fade_beats.add(current_beat_start)
                    ord_info = fade_orders[current_beat_start]
                    active_order_id = ord_info["order_id"]
                    active_order_token_id = ord_info["token_id"]
                    active_order_side = ord_info["side"]
                    active_order_stake = ord_info["stake"]
                    active_order_shares = ord_info["shares"]
                    active_order_price = ord_info["price"]
                    active_order_target_bid = ord_info["target_bid"]
                    active_order_beat_ts = current_beat_start
                    active_order_filled_shares = ord_info["filled_shares"]
                    active_order_filled_val = ord_info["filled_val"]
                    active_order_fill_time = None
                    active_order_last_fill_time = None
                    active_order_cancel_checked = False
                    active_order_adjusted = False
                    active_order_replacement_pending = False
                    active_order_replacement_shares = 0.0
                    print(f"         [FADE RUN] Loaded pre-placed order {active_order_id[:8]} for current beat {current_beat_start}")

                # --- STEP 1: Evaluate Previous Position ---
                current_filled_shares = 0.0
                actual_avg_price = active_order_price if active_order_price is not None else 0.0
                if active_order_id is not None:
                    assert active_order_shares is not None
                    assert active_order_price is not None
                    if active_order_filled_shares >= active_order_shares and active_order_shares > 0:
                        current_filled_shares = active_order_filled_shares
                        if active_order_filled_shares > 0:
                            actual_avg_price = active_order_filled_val / active_order_filled_shares
                        else:
                            actual_avg_price = active_order_price
                        print(f"         [ORDER CHECK] Order {active_order_id[:8]}... already marked fully filled locally ({current_filled_shares:.4f} shares). Skipping API query.")
                    else:
                        current_filled_shares, order_status, actual_avg_price = await check_order_fill(
                            mode, client, active_order_id, active_order_shares, active_order_price,
                            known_filled_shares=active_order_filled_shares,
                            known_avg_price=(active_order_filled_val / active_order_filled_shares) if active_order_filled_shares > 0 else active_order_price
                        )
                        if current_filled_shares > active_order_filled_shares:
                            active_order_last_fill_time = int(time.time())
                        if order_status == "matched" and active_order_fill_time is None:
                            active_order_fill_time = int(time.time())

                total_filled_shares = chased_filled_shares + current_filled_shares
                actual_cost = chased_filled_val + (current_filled_shares * actual_avg_price)

                trade_occurred = total_filled_shares > 0

                if trade_occurred:
                    sessions_with_fills.add(fade_session_num)
                    assert active_order_beat_ts is not None
                    assert active_order_side is not None
                    average_price = actual_cost / total_filled_shares
                    target_bid_val = active_order_target_bid if active_order_target_bid is not None else average_price

                    if active_order_tp_placed and not active_order_tp_filled and client is not None and active_order_tp_id is not None:
                        try:
                            tp_resp = await asyncio.to_thread(client.get_order_normalized, active_order_tp_id)
                            if isinstance(tp_resp, dict):
                                tp_status = tp_resp.get("status", "").lower()
                                tp_matched = float(tp_resp.get("size_matched", 0))
                                if tp_status in ("matched", "executed") or (tp_matched >= active_order_tp_shares and active_order_tp_shares > 0):
                                    active_order_tp_filled = True
                        except Exception:
                            pass
                        if not active_order_tp_filled:
                            try:
                                await asyncio.to_thread(client.cancel_order, active_order_tp_id)
                            except Exception:
                                pass

                    pending_trades[active_order_beat_ts] = {
                        "beat_ts": active_order_beat_ts,
                        "side": active_order_side,
                        "cost": actual_cost,
                        "shares": total_filled_shares,
                        "avg_price": average_price,
                        "target_bid": target_bid_val,
                        "in_fade_window": in_fade_window,
                        "trigger_seconds": trigger_seconds,
                        "fill_time": active_order_fill_time,
                        "last_fill_time": active_order_last_fill_time,
                        "session_num": fade_session_num,
                        "bet_num": campaign_step + 1,
                        "total_beats": max_cap,
                        "used_bank_credit": active_order_used_bank_credit,
                        "tp_hit": active_order_tp_filled,
                        "tp_sell_price": active_order_tp_price,
                        "is_campaign": True,
                        "campaign_num": campaign_num,
                        "campaign_step": campaign_step,
                        "campaign_trigger_dir": campaign_trigger_dir,
                        "action_type": campaign_action_type,
                    }
                    if active_order_tp_filled:
                        print(f"         [PENDING EVALUATION] Trade occurred on {active_order_side} (filled {total_filled_shares:.4f} shares) with TAKE-PROFIT FILLED at ${active_order_tp_price:.2f}! Locked profit: +${(active_order_tp_price * total_filled_shares) - actual_cost:.2f}")
                    else:
                        print(f"         [PENDING EVALUATION] Trade occurred on {active_order_side} (filled {total_filled_shares:.4f} shares). Deferring evaluation until beat resolves...")
                else:
                    if active_order_id is not None:
                        print(f"         [NO FILL] Order {active_order_id[:8]}... did not get filled.")
                        if active_order_used_bank_credit > 0 and enable_price_savings_bank:
                            price_savings_bank = round(price_savings_bank + active_order_used_bank_credit, 4)
                            print(f"         [SAVINGS BANK] Refunded ${active_order_used_bank_credit:.4f} credit back to bank (no fill). Total Bank Credit: ${price_savings_bank:.4f}")
                    else:
                        print("         [IDLE BEAT] No order was placed for this beat.")

                    if in_fade_window and not is_startup_evaluation:
                        skip_beat_ts = active_order_beat_ts if active_order_beat_ts is not None else current_beat_start
                        skip_side = active_order_side if active_order_side is not None else fade_active_side
                        if skip_beat_ts not in pending_trades and skip_beat_ts not in pending_skipped_trades:
                            pending_skipped_trades[skip_beat_ts] = {
                                "session_num": fade_session_num,
                                "side": skip_side,
                                "stake": active_order_stake or current_fade_stake_usd,
                                "is_campaign": True,
                                "campaign_num": campaign_num,
                                "campaign_step": campaign_step,
                                "campaign_trigger_dir": campaign_trigger_dir,
                                "action_type": campaign_action_type,
                            }
                            print(f"         [PENDING SKIP EVALUATION] Session #{fade_session_num} on {skip_side} had 0 filled shares. Deferring skipped evaluation until beat resolves...")

                # Clear active order variables (ready for next beat)
                active_order_id = None
                active_order_token_id = None
                active_order_side = None
                active_order_stake = None
                active_order_shares = None
                active_order_price = None
                active_order_target_bid = None
                active_order_beat_ts = None
                active_order_filled_shares = 0.0
                active_order_filled_val = 0.0
                chased_filled_shares = 0.0
                chased_filled_val = 0.0
                active_order_fill_time = None
                active_order_last_fill_time = None
                active_order_cancel_checked = False
                active_order_adjusted = False
                active_order_replacement_pending = False
                active_order_replacement_shares = 0.0
                active_order_cross_retries = 0
                active_order_used_bank_credit = 0.0
                active_order_tp_id = None
                active_order_tp_price = None
                active_order_tp_shares = 0.0
                active_order_tp_placed = False
                active_order_tp_filled = False
                active_order_tp_fill_time = None

                # --- STEP 2: Update lookback counts & fade state ---
                # Build timestamps in strict chronological order (oldest to newest: 24 beats ago ... 1 beat ago)
                required_ts = [current_beat_start - beat_duration_sec * i for i in range(effective_history_len - 1, 0, -1)]
                missing_ts = [ts for ts in required_ts if ts not in historical_outcomes_cache]
                if missing_ts:
                    try:
                        fetched = await fetch_historical_outcomes(fetcher, missing_ts)
                        historical_outcomes_cache.update({ts: out for ts, out in fetched.items() if out in ("Up", "Down")})
                    except Exception as ex:
                        print(f"         [WARNING] Failed to fetch missing historical outcomes in decision window: {ex}")

                recent_outcomes = []
                for ts in required_ts:
                    out = historical_outcomes_cache.get(ts)
                    if out in ("Up", "Down"):
                        recent_outcomes.append(out)
                if current_close_side in ("Up", "Down"):
                    recent_outcomes.append(current_close_side)

                lookback_up_count = recent_outcomes.count("Up")
                lookback_down_count = recent_outcomes.count("Down")

                streak_triggered, streak_fade_side, streak_action_type, streak_stake_usd, streak_len, streak_side = check_special_martingale_trigger(
                    outcomes=recent_outcomes,
                    consecutive_streak_count=consecutive_streak_count,
                    in_campaign=in_campaign,
                    campaign_step=campaign_step,
                    campaign_trigger_dir=campaign_trigger_dir,
                    max_cap=max_cap,
                    stake_multipliers=stake_multipliers,
                    base_stake_usd=base_stake_usd,
                    pattern=pattern,
                )

                last_n_outcomes = recent_outcomes[-consecutive_streak_count:]
                streak_up_count = last_n_outcomes.count("Up")
                streak_down_count = last_n_outcomes.count("Down")
                print(f"         [DECISION LAST {consecutive_streak_count} BEATS] {last_n_outcomes}")
                streak_desc = f"{streak_len} consecutive {streak_side}" if streak_side else "no streak"
                cap_desc = f", cap: {cap} bets" if cap else ""
                print(f"         [DECISION STREAK] {streak_desc} (need {consecutive_streak_count} to trigger{cap_desc})")

                decision_str = ""
                if in_fade_window:
                    next_beats_left = fade_window_remaining_beats - 1
                    target_profit = fade_profit_target_usd
                    if target_profit is not None and fade_session_profit >= target_profit:
                        decision_str = f"Profit target of >= ${target_profit:.2f} met. Stopping fade early."
                    elif next_beats_left <= 0:
                        decision_str = "Fade window completed. Stopping fade."
                    else:
                        decision_str = f"Active fade window in progress (Side: {fade_active_side}, {next_beats_left} beats remaining). Continuing."
                else:
                    if streak_triggered:
                        bet_num_in_streak = streak_len - consecutive_streak_count + 1
                        cap_suffix = f" (Bet #{bet_num_in_streak}/{cap})" if cap else ""
                        decision_str = f"Trigger condition met! {streak_len} consecutive {streak_side}. Starting fade on {streak_fade_side}{cap_suffix}."
                    elif cap is not None and cap > 0 and streak_len >= consecutive_streak_count + cap:
                        decision_str = f"Streak cap reached ({streak_len} >= {consecutive_streak_count + cap}). Streak not broken yet. Waiting for opposite outcome."
                    else:
                        decision_str = f"Trigger condition NOT met. Waiting."

                print(f"         [DECISION] {decision_str}")

                if in_fade_window:
                    fade_window_remaining_beats -= 1
                    target_profit = fade_profit_target_usd
                    if target_profit is not None and fade_session_profit >= target_profit:
                        in_fade_window = False
                        fade_active_side = None
                        await cancel_remaining_fade_orders(mode, client, fade_orders, current_beat_start)
                        fade_orders.clear()
                        print(f"         [FADE PROFIT TARGET MET] Profit target of >= ${target_profit:.2f} met (Current Session: ${fade_session_profit:.2f}). Stopping fade window early and returning to wait mode.")
                    elif fade_window_remaining_beats <= 0:
                        in_fade_window = False
                        fade_active_side = None
                        await cancel_remaining_fade_orders(mode, client, fade_orders, current_beat_start)
                        fade_orders.clear()
                        print(f"         [FADE WINDOW END] Lookback trade completed.")

                # Check for new trigger
                if not in_fade_window:
                    if streak_triggered and streak_fade_side is not None:
                        vol_notif_str = ""
                        vol_console_str = ""
                        delta_notif_str = ""
                        delta_console_str = ""

                        if in_campaign:
                            vol_failed = False
                            delta_failed = False
                            print(f"         [SPECIAL MARTINGALE CAMPAIGN] Step {campaign_step+1}/{max_cap} active ({streak_action_type}). Filter bypassed for recovery. (Provisional: next step/side is finalized once this beat settles.)")
                        elif dyn_filter_enabled and dynamic_rules:
                            _, curr_vol_pct, vol_msg = check_volatility_filter(
                                asset=asset, enabled=True, threshold_pct=dyn_min_vol_pct, lookback_candles=vol_lookback_candles, source=dyn_vol_source
                            )
                            print(f'         {vol_msg}')

                            d_valid, d_rng, d_body, b_hi, b_lo, d_msg = fetch_binance_15m_delta_range(
                                asset=asset, beat_start_ts=current_beat_start
                            )
                            print(f'         [DELTA FILTER] {d_msg}')

                            if not d_valid:
                                print(f'         [DYNAMIC FILTER ERROR] Binance not available ({d_msg}). Skipping bet due to Binance Network Issue.')
                                if notifications_enabled:
                                    cap_notif = f' (Bet {streak_len - consecutive_streak_count + 1}/{cap})' if cap else ''
                                    streak_char = 'U' if streak_side == 'Up' else 'D'
                                    cand_side_abbr = 'U' if streak_fade_side == 'Up' else 'D'
                                    msg = (
                                        f'⚠️ POLY - BINANCE NETWORK ISSUE{cap_notif}\n'
                                        f'🆔 {strategy_name}\n'
                                        f'📅 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
                                        f'📊 Streak: {streak_len}{streak_char}\n'
                                        f'🎯 Candidate Bet: {cand_side_abbr}(SKIPPED)\n'
                                        f'Action: SKIPPED bet due to Binance Network Issue.'
                                    )
                                    send_telegram_notification(msg)
                                is_startup_evaluation = False
                                continue

                            passed_dyn, match_desc, statuses = evaluate_dynamic_rules(dynamic_rules, curr_vol_pct, d_rng, d_body)

                            if not passed_dyn:
                                next_beat_ts = current_beat_start + beat_duration_sec
                                filter_skip_session_num = bot.state.filter_skipped_count + len(pending_filter_skipped_trades) + 1
                                pending_filter_skipped_trades[next_beat_ts] = {
                                    'side': streak_fade_side,
                                    'session_num': filter_skip_session_num,
                                    'streak_len': streak_len,
                                    'streak_side': streak_side,
                                    'delta_range': d_rng,
                                    'delta_body': d_body,
                                    'vol_pct': curr_vol_pct,
                                }
                                print(f'         [DYNAMIC FILTER: FAIL] {match_desc}. Signal SKIPPED.')
                                for st in statuses:
                                    print(f'           • {st}')

                                if notifications_enabled:
                                    cap_notif = f' (Bet {streak_len - consecutive_streak_count + 1}/{cap})' if cap else ''
                                    streak_char = 'U' if streak_side == 'Up' else 'D'
                                    cand_side_abbr = 'U' if streak_fade_side == 'Up' else 'D'
                                    tiers_display = '\n'.join([f'• {st}' for st in statuses])
                                    msg = (
                                        f'🛡️ POLY - FILTER_SKIP - #{filter_skip_session_num}{cap_notif}\n'
                                        f'🆔 {strategy_name}\n'
                                        f'📅 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
                                        f'📊 Streak: {streak_len}{streak_char}\n'
                                        f'⚠️ Dynamic OR Filter: FAIL\n'
                                        f'{tiers_display}\n'
                                        f'🎯 Candidate Bet: {cand_side_abbr}(SKIPPED)\n'
                                        f'Action: Filter_Skip applied. Order NOT placed.\n'
                                        f'Total Filter_Skips: {bot.state.filter_skipped_count}'
                                    )
                                    send_telegram_notification(msg)
                                save_state()
                                is_startup_evaluation = False
                                continue
                            else:
                                print(f'         [DYNAMIC FILTER: PASS] {match_desc}. Signal ACCEPTED.')
                                vol_notif_str = f'✅ Dynamic Filter: PASS ({match_desc})\n'
                                vol_console_str = f' [Dynamic: {match_desc}]'
                        else:
    
                            # ── Run ALL enabled filters unconditionally ──────────────
                            vol_failed = False
                            vol_pct = 0.0
                            vol_fail_line = ""
    
                            # 4H Volatility Filter Check
                            if vol_filter_enabled:
                                should_skip_vol, vol_pct, vol_msg = check_volatility_filter(
                                    asset=asset,
                                    enabled=True,
                                    threshold_pct=vol_threshold_pct,
                                    lookback_candles=vol_lookback_candles,
                                    source=vol_source,
                                )
                                print(f"         {vol_msg}")
                                if should_skip_vol:
                                    vol_failed = True
                                    vol_fail_line = f"⚠️ V: {vol_pct:.4f}% (Req >= {vol_threshold_pct:.2f}%)\n"
                                    print(f"         [VOL FILTER: FAIL] 4H Volatility {vol_pct:.4f}% < {vol_threshold_pct:.2f}%.")
                                else:
                                    vol_notif_str = f"📈 V: {vol_pct:.4f}% (Req >= {vol_threshold_pct:.2f}%)\n"
                                    vol_console_str = f" [Vol: {vol_pct:.4f}% (Req >= {vol_threshold_pct:.2f}%)]"
    
                            delta_failed = False
                            delta_fail_lines = ""
                            delta_pass_filter_lines = ""
    
                            # Delta Filter Check (Binance Real-Time 15m Range & Body)
                            if delta_filter_enabled:
                                if delta_asymmetric:
                                    s_valid, s_candles, s_msg = fetch_binance_15m_streak_candles(
                                        asset=asset, current_beat_start_ts=current_beat_start, streak_count=consecutive_streak_count
                                    )
                                    print(f"         [ASYMMETRIC DELTA FILTER] {s_msg}")
                                    if not s_valid or len(s_candles) < consecutive_streak_count:
                                        print(f"         [DELTA FILTER ERROR] Binance not available ({s_msg}). Skipping bet.")
                                        is_startup_evaluation = False
                                        continue
    
                                    c1, c4 = s_candles[0], s_candles[-1]
                                    c4_rng = c4["high"] - c4["low"]
                                    move_type = delta_asymmetric.get("move_metric", "displacement")
                                    streak_move = (max(c["high"] for c in s_candles) - min(c["low"] for c in s_candles)) if move_type == "range" else abs(c4["close"] - c1["open"])
    
                                    req_move = delta_asymmetric["up_streak_move"] if streak_side == "Up" else delta_asymmetric["down_streak_move"]
                                    req_rng = delta_asymmetric["up_c4_range"] if streak_side == "Up" else delta_asymmetric["down_c4_range"]
    
                                    is_move_fail = (streak_move < req_move)
                                    is_rng_fail = (c4_rng < req_rng)
    
                                    if is_move_fail or is_rng_fail:
                                        delta_failed = True
                                        asym_reasons = []
                                        if is_move_fail:
                                            asym_reasons.append(f"Streak Move ${streak_move:.2f} < ${req_move:.2f}")
                                        if is_rng_fail:
                                            asym_reasons.append(f"Candle 4 Range ${c4_rng:.2f} < ${req_rng:.2f}")
                                        print(f"         [ASYMMETRIC FILTER: FAIL] {' & '.join(asym_reasons)}.")
                                        delta_fail_lines = "".join(
                                            [f"⚠️ Move: ${streak_move:.2f} (Req >= ${req_move:.2f})\n" if is_move_fail else "",
                                             f"⚠️ R: ${c4_rng:.2f} (Req >= ${req_rng:.2f})\n" if is_rng_fail else ""]
                                        )
                                    else:
                                        asym_pass_parts = [f"Move: ${streak_move:.2f} (Req >= ${req_move:.2f})", f"R: ${c4_rng:.2f} (Req >= ${req_rng:.2f})"]
                                        print(f"         [ASYMMETRIC FILTER: PASS] {' AND '.join(asym_pass_parts)}. Signal ACCEPTED.")
                                        delta_notif_str = f"📐 {' | '.join(asym_pass_parts)}\n"
                                        delta_console_str = f" [Asym: {' | '.join(asym_pass_parts)}]"
                                        delta_pass_filter_lines = "".join(
                                            [f"📈 Move: ${streak_move:.2f} (Req >= ${req_move:.2f})\n",
                                             f"📈 R: ${c4_rng:.2f} (Req >= ${req_rng:.2f})\n"]
                                        )
                                else:
                                    delta_valid, delta_rng, delta_body, b_high, b_low, delta_msg = fetch_binance_15m_delta_range(
                                        asset=asset, beat_start_ts=current_beat_start
                                    )
                                    print(f"         [DELTA FILTER] {delta_msg}")
                                    if not delta_valid:
                                        print(f"         [DELTA FILTER ERROR] Binance not available ({delta_msg}). Skipping bet due to Binance Network Issue.")
                                        if notifications_enabled:
                                            cap_notif = f" (Bet {streak_len - consecutive_streak_count + 1}/{cap})" if cap else ""
                                            streak_char = "U" if streak_side == "Up" else "D"
                                            cand_side_abbr = "U" if streak_fade_side == "Up" else "D"
                                            msg = (
                                                f"⚠️ POLY - BINANCE NETWORK ISSUE{cap_notif}\n"
                                                f"🆔 {strategy_name}\n"
                                                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                                f"📊 Streak: {streak_len}{streak_char}\n"
                                                f"{vol_notif_str}"
                                                f"🎯 Candidate Bet: {cand_side_abbr}(SKIPPED)\n"
                                                f"❌ Binance Network Issue: Could not fetch real-time 15m candle.\n"
                                                f"Action: SKIPPED bet due to Binance Network Issue."
                                            )
                                            send_telegram_notification(msg)
                                        is_startup_evaluation = False
                                        continue

                                    is_range_min_fail = (delta_max_range is not None) and (delta_rng < delta_max_range)
                                    is_range_max_fail = (delta_ceiling_range is not None) and (delta_rng > delta_ceiling_range)
                                    is_range_fail = is_range_min_fail or is_range_max_fail
                                    is_body_fail = (delta_threshold is not None) and (delta_body < delta_threshold)

                                    if is_range_fail or is_body_fail:
                                        delta_failed = True
                                        fail_reasons = []
                                        if is_range_min_fail:
                                            fail_reasons.append(f"Range ${delta_rng:.2f} < ${delta_max_range:.2f}")
                                            delta_fail_lines += f"⚠️ R: ${delta_rng:.2f} (Req: >= ${delta_max_range:.2f})\n"
                                        if is_range_max_fail:
                                            fail_reasons.append(f"Range ${delta_rng:.2f} > ${delta_ceiling_range:.2f}")
                                            delta_fail_lines += f"⚠️ R: ${delta_rng:.2f} (Req: <= ${delta_ceiling_range:.2f})\n"
                                        if is_body_fail:
                                            fail_reasons.append(f"Body ${delta_body:.2f} < ${delta_threshold:.2f}")
                                            delta_fail_lines += f"⚠️ B: ${delta_body:.2f} (Req: >= ${delta_threshold:.2f})\n"
                                        print(f"         [DELTA FILTER: FAIL] {' & '.join(fail_reasons)}.")
                                    else:
                                        pass_parts = []
                                        pass_filter_parts = []
                                        if delta_max_range is not None:
                                            pass_parts.append(f"R: ${delta_rng:.2f} (Req >= ${delta_max_range:.2f})")
                                            pass_filter_parts.append(f"📈 R: ${delta_rng:.2f} (Req >= ${delta_max_range:.2f})\n")
                                        else:
                                            pass_parts.append(f"R: ${delta_rng:.2f}")
                                        if delta_threshold is not None:
                                            pass_parts.append(f"B: ${delta_body:.2f} (Req >= ${delta_threshold:.2f})")
                                            if delta_threshold > 0:
                                                pass_filter_parts.append(f"📈 B: ${delta_body:.2f} (Req >= ${delta_threshold:.2f})\n")
                                        elif delta_body is not None:
                                            pass_parts.append(f"B: ${delta_body:.2f}")
                                        print(f"         [DELTA FILTER: PASS] {' AND '.join(pass_parts)}. Signal ACCEPTED.")
                                        delta_notif_str = f"📐 {' | '.join(pass_parts)}\n"
                                        delta_console_str = f" [Delta: {' | '.join(pass_parts)}]"
                                        delta_pass_filter_lines = "".join(pass_filter_parts)
    
                            # ── Single decision point ──────────────────────────────
                            if vol_failed or delta_failed:
                                next_beat_ts = current_beat_start + beat_duration_sec
                                filter_skip_session_num = bot.state.filter_skipped_count + len(pending_filter_skipped_trades) + 1
                                pending_filter_skipped_trades[next_beat_ts] = {
                                    "side": streak_fade_side,
                                    "session_num": filter_skip_session_num,
                                    "streak_len": streak_len,
                                    "streak_side": streak_side,
                                    "delta_range": delta_rng,
                                    "delta_body": delta_body,
                                }
                                console_reasons = []
                                if vol_failed:
                                    console_reasons.append(f"Vol {vol_pct:.4f}% < {vol_threshold_pct:.2f}%")
                                if delta_failed:
                                    console_reasons.append("Delta fail")
                                print(f"         [FILTER_SKIP] {' & '.join(console_reasons)}. Signal SKIPPED.")
                                if notifications_enabled:
                                    cap_notif = f" (Bet {streak_len - consecutive_streak_count + 1}/{cap})" if cap else ""
                                    streak_char = "U" if streak_side == "Up" else "D"
                                    cand_side_abbr = "U" if streak_fade_side == "Up" else "D"
                                    delta_filter_display = delta_fail_lines if delta_failed else delta_pass_filter_lines
                                    msg = (
                                        f"🛡️ POLY - FILTER_SKIP - #{filter_skip_session_num}{cap_notif}\n"
                                        f"🆔 {strategy_name}\n"
                                        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                        f"📊 Streak: {streak_len}{streak_char}\n"
                                        f"{vol_fail_line if vol_failed else vol_notif_str}"
                                        f"{delta_filter_display}"
                                        f"🎯 Candidate Bet: {cand_side_abbr}(SKIPPED)\n"
                                        f"Action: Filter_Skip applied. Order NOT placed.\n"
                                        f"Total Filter_Skips: {bot.state.filter_skipped_count}"
                                    )
                                    send_telegram_notification(msg)
                                save_state()
                                is_startup_evaluation = False
                                continue
    
                        if not in_campaign:
                            in_campaign = True
                            campaign_step = 0
                            campaign_trigger_dir = streak_side
                            campaign_num += 1
                            campaign_pnl = 0.0

                        campaign_action_type = streak_action_type
                        current_fade_stake_usd = streak_stake_usd
                        fade_active_side = streak_fade_side
                        in_fade_window = True
                        fade_session_total_beats = 1
                        fade_window_remaining_beats = 1
                        fade_session_num = campaign_num

                        step_info_str = f"[STEP {campaign_step+1}/{max_cap} ({campaign_action_type})]"
                        print(
                            f"         [TRIGGER] Special Martingale Campaign #{campaign_num} {step_info_str} on {streak_len} consecutive {streak_side}. "
                            f"Bet on {fade_active_side} with stake ${current_fade_stake_usd:.2f}{vol_console_str}{delta_console_str}."
                        )
                        if notifications_enabled:
                            up_arrow = "⬆️" if fade_active_side == "Up" else "⬇️"
                            side_letter = "U" if fade_active_side == "Up" else "D"
                            winrate_trade_val = (100.0 * bot.state.wins / bot.state.trade_num) if bot.state.trade_num else 0.0
                            winrate_dollar_val = (100.0 * bot.state.win_stake_total / bot.state.total_stake) if bot.state.total_stake else 0.0
                            total_winrate_val = bot.state.total_winrate
                            avg_price_val = (bot.state.total_fill_price_sum / bot.state.trade_num) if bot.state.trade_num else 0.0
                            camp_wr_val = bot.state.campaign_winrate
                            msg = (
                                f'⚡ SPECIAL MARTINGALE - TRIGGER - Campaign #{campaign_num} {step_info_str}\n'
                                f'🆔 {strategy_name}\n'
                                f'📅 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
                                f'👉 Bet: {up_arrow} {side_letter} (${format_masked_amount(current_fade_stake_usd)}) {step_info_str}\n'
                                f'📊 Streak: {streak_len}{"U" if streak_side == "Up" else "D"} (Trigger: {consecutive_streak_count})\n'
                                f"{vol_notif_str}"
                                f"{delta_notif_str}"
                                f"📈 Campaigns: {bot.state.campaigns} | Wins: {bot.state.campaign_wins} | Losses: {bot.state.campaign_losses} (WR: {camp_wr_val:.1f}%)\n"
                                f"📊 Trades: {bot.state.trade_num} | Wins: {bot.state.wins} | Losses: {bot.state.losses}\n"
                                f"⚠️ Skips: {bot.state.skipped_count} (Unfilled) | Filter_Skips: {bot.state.filter_skipped_count}\n"
                                f"📈 Winrate: {winrate_trade_val:.2f}% (Trade) | {winrate_dollar_val:.2f}% (Dollars)\n"
                                f"🌐 Total Winrate (include skips): {total_winrate_val:.2f}%\n"
                                f"🎯 Avg Price: {avg_price_val:.4f}\n"
                                f"💵 Total P&L: {format_masked_amount(bot.state.total_pnl, sign=True)}"
                            )
                            send_telegram_notification(msg)

                is_startup_evaluation = False

                # Check if max transactions reached
                if max_transactions is not None and bot.state.trade_num >= max_transactions:
                    print(f"Maximum transactions ({max_transactions}) reached. Stopping bot.")
                    bot.print_summary()
                    return



                # --- STEP 3: Place Order for Next Beat ---
                if current_close_side is not None:
                    if in_fade_window:
                        assert fade_active_side is not None
                        next_side = fade_active_side
                        next_stake = current_fade_stake_usd
                        print(f"         [FADE RUN] In fade window ({fade_window_remaining_beats} beats remaining). Side: {next_side} | Stake: ${next_stake:.2f}")

                        next_beat_start = current_beat_start + beat_duration_sec
                        if next_beat_start in fade_orders:
                            print(f"         [FADE RUN] Order for next beat {next_beat_start} is already pre-placed. Skipping placement.")
                        elif (min_maker_sec or 0) > 0:
                            print(f"         [ORDER TIMING] Delayed maker entry configured (min_maker_sec = {min_maker_sec}s). Will place order at elapsed >= {min_maker_sec}s into beat {next_beat_start}.")
                        else:
                            print(f"         [PREPARING] Next beat starts at {next_beat_start} (UTC: {datetime.fromtimestamp(next_beat_start, tz=timezone.utc)})")

                            # Wait until exact beat start timestamp
                            target_place_ts = float(next_beat_start)
                            now_ts = time.time()
                            wait_time = target_place_ts - now_ts
                            if wait_time > 0:
                                target_time_str = datetime.fromtimestamp(target_place_ts, tz=timezone.utc).strftime('%H:%M:%S')
                                print(f"         [ORDER TIMING] Waiting {wait_time:.2f}s until beat start ({target_time_str} UTC) to submit maker order...")
                                await asyncio.sleep(wait_time)

                            await place_beat_order(next_beat_start, next_side, next_stake)
                    else:
                        print("         [WAITING] No active fade window. Skipping order placement (Fade-Only strategy).")
                else:
                    print("         [ERROR] current_close_side is None. Skipping order placement.")

        except Exception as loop_err:
            print(f"         [LOOP ERROR] {loop_err}")

        save_state()
        await asyncio.sleep(1.0)

def nullable_float(val: Any) -> float | None:
    if val is None or str(val).strip().lower() in ("null", "none", ""):
        return None
    return float(val)

def nullable_int(val: Any) -> int | None:
    if val is None or str(val).strip().lower() in ("null", "none", ""):
        return None
    return int(val)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Kalshi window-fade bot")
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to external JSON configuration file (default: config.json)"
    )
    parser.add_argument(
        "--mode",
        choices=[BotMode.DRY_RUN.value, BotMode.REAL.value],
        help="dry_run: simulate trades; real: place orders via Kalshi API",
    )
    parser.add_argument(
        "--max-transactions",
        type=nullable_int,
        help="Maximum number of trades before stopping the bot.",
    )
    parser.add_argument(
        "--decisive-up",
        type=float,
        help="UP price threshold to decide if the beat is going to close UP",
    )
    parser.add_argument(
        "--decisive-down",
        type=float,
        help="UP price threshold to decide if the beat is going to close DOWN",
    )
    parser.add_argument(
        "--seconds-before-close",
        type=int,
        help="Number of seconds before the beat close to decide and order",
    )

    parser.add_argument(
        "--history-beats-count",
        type=int,
        help="Number of last beats' decisions to look back on (default: 50)",
    )
    parser.add_argument(
        "--consecutive-streak-count",
        type=int,
        help="Number of consecutive Up or Down outcomes required to trigger (default: 7)",
    )
    parser.add_argument(
        "--base-stake-usd",
        type=float,
        default=None,
        help="Base stake size in USD for Step 1 (default: 10.0)",
    )
    parser.add_argument(
        "--stake-multipliers",
        type=str,
        default=None,
        help="Comma-separated stake multipliers for progression (default: '1,2,4')",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        help="Comma-separated campaign pattern of FADE/FOLLOW per step (default: 'FADE,FOLLOW,FADE')",
    )
    parser.add_argument(
        "--sync-corrections",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable legacy history-based sync corrections (FALSE TRIGGER / SIDE MISMATCH / MISSING TRIGGER). Default: off",
    )
    parser.add_argument(
        "--max-cap",
        type=nullable_int,
        default=None,
        help="Maximum martingale steps before cutting loss and resetting (default: 3)",
    )
    parser.add_argument(
        "--fade-stake-usd",
        type=float,
        help="Alias for base stake size in USD",
    )
    parser.add_argument(
        "--fade-profit-target-usd",
        type=nullable_float,
        help="Profit target in USD to stop the session of profit (default: 30.0)",
    )
    parser.add_argument(
        "--notifications",
        action="store_true",
        dest="notifications",
        default=None,
        help="Enable Telegram notifications on closed transactions (default: True or from config)"
    )
    parser.add_argument(
        "--no-notifications",
        action="store_false",
        dest="notifications",
        help="Disable Telegram notifications on closed transactions"
    )
    parser.add_argument(
        "--asset",
        type=str,
        help="Asset symbol to trade (BTC, ETH, SOL) (default: BTC)",
    )
    parser.add_argument(
        "--stop-maker-sec",
        type=int,
        help="Seconds from beat start to cancel open post_only maker order (default: 45)",
    )
    parser.add_argument(
        "--min-maker-sec",
        type=int,
        default=None,
        help="Seconds from beat start to delay placing maker order (e.g. 151, default: 0)",
    )
    parser.add_argument(
        "--taker-limit-price",
        type=nullable_float,
        help="Max limit price for taker IOC orders (default: 0.55)",
    )
    parser.add_argument(
        "--beat-duration-min",
        type=int,
        help="Duration of each beat in minutes (default: 5)",
    )
    parser.add_argument(
        "--active-order-adjusted",
        action="store_true",
        dest="active_order_adjusted",
        default=None,
        help="Enable automated maker price adjustments to max-maker-price (default: True)",
    )
    parser.add_argument(
        "--no-active-order-adjusted",
        action="store_false",
        dest="active_order_adjusted",
        help="Disable automated maker price adjustments to max-maker-price",
    )
    parser.add_argument(
        "--enable-price-savings-bank",
        action="store_true",
        dest="enable_price_savings_bank",
        default=None,
        help="Enable Price Savings Bank (adjust entry price above max-maker-price using WIN savings)",
    )
    parser.add_argument(
        "--max-maker-price",
        type=float,
        help="Maximum limit price for maker limit orders (default: 0.50)",
    )
    parser.add_argument(
        "--marketable-limit",
        action="store_true",
        dest="marketable_limit",
        default=None,
        help="Enable Marketable Limit Order strategy (GTC limit at max_maker_price, post_only=False). Default: True"
    )
    parser.add_argument(
        "--no-marketable-limit",
        "--post-only",
        action="store_false",
        dest="marketable_limit",
        help="Disable Marketable Limit Order strategy and enforce legacy post-only orders"
    )
    parser.add_argument(
        "--disable-price-savings-bank",
        action="store_false",
        dest="enable_price_savings_bank",
        help="Disable Price Savings Bank",
    )
    parser.add_argument(
        "--strategy-name",
        type=str,
        help="Strategy name identifier for the bot instance (default: 'Consecutive Streak' or from config)",
    )
    parser.add_argument(
        "--cap",
        type=nullable_int,
        default=None,
        help="Maximum number of bets per streak before waiting for next streak (default: None)",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        help="Path to file where bot state is saved and restored (default: None)",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        dest="reset_state",
        default=None,
        help="Ignore and delete existing state file to start fresh"
     )
    parser.add_argument(
        "--no-reset-state",
        action="store_false",
        dest="reset_state",
        help="Do not reset existing state file"
    )
    parser.add_argument(
        "--enable-volatility-filter",
        action="store_true",
        dest="volatility_filter",
        default=None,
        help="Enable 4H realized volatility filter (skip trades if vol < threshold)",
    )
    parser.add_argument(
        "--disable-volatility-filter",
        action="store_false",
        dest="volatility_filter",
        help="Disable 4H realized volatility filter",
    )
    parser.add_argument(
        "--volatility-threshold",
        type=float,
        default=None,
        help="4H realized volatility skip threshold in percent (default: 0.13 or from config)",
    )
    parser.add_argument(
        "--volatility-lookback",
        type=int,
        default=None,
        help="Lookback candles for volatility filter (default: 16 candles = 4H)",
    )
    parser.add_argument(
        "--enable-dynamic-staking",
        action="store_true",
        dest="enable_dynamic_staking",
        default=None,
        help="Enable dynamic stake sizing based on rolling win rate",
    )
    parser.add_argument(
        "--disable-dynamic-staking",
        action="store_true",
        dest="disable_dynamic_staking",
        default=None,
        help="Disable dynamic stake sizing",
    )
    parser.add_argument(
        "--dynamic-window",
        type=int,
        default=None,
        help="Rolling window size in triggers for dynamic staking (default: 8 or from config)",
    )
    parser.add_argument(
        "--dynamic-alpha",
        type=float,
        default=None,
        help="Sensitivity alpha for dynamic staking (default: 1.5 or from config)",
    )
    parser.add_argument(
        "--dynamic-target-wr",
        type=float,
        default=None,
        help="Target baseline win rate for dynamic staking (default: 0.519 or from config)",
    )
    parser.add_argument(
        "--dynamic-min-stake",
        type=float,
        default=None,
        help="Minimum allowed dynamic stake in USD (default: None)",
    )
    parser.add_argument(
        "--dynamic-max-stake",
        type=float,
        default=None,
        help="Maximum allowed dynamic stake in USD (default: None)",
    )
    parser.add_argument(
        "--dynamic-max-loss-cap",
        type=int,
        default=None,
        help="Maximum consecutive losses before capping stake (default: 3 or from config)",
    )
    parser.add_argument(
        "--enable-take-profit",
        action="store_true",
        dest="take_profit",
        default=None,
        help="Enable Maker Take-Profit (Stop-Profit) limit sell order placement on fill",
    )
    parser.add_argument(
        "--disable-take-profit",
        action="store_false",
        dest="take_profit",
        help="Disable Maker Take-Profit",
    )
    parser.add_argument(
        "--take-profit-pct",
        type=float,
        default=None,
        help="Take-profit target percentage (e.g. 0.14 for 14%%)",
    )
    parser.add_argument(
        "--enable-delta-filter",
        action="store_true",
        dest="delta_filter",
        default=None,
        help="Enable real-time Binance 15m BTC delta filter (skip trades if delta range < threshold)",
    )
    parser.add_argument(
        "--disable-delta-filter",
        action="store_false",
        dest="delta_filter",
        help="Disable real-time Binance 15m BTC delta filter",
    )
    parser.add_argument(
        "--delta-max-range",
        type=float,
        default=None,
        help="Prior 15m BTC candle full range threshold (High - Low) in USD (default: 230.0 or from config)",
    )
    parser.add_argument(
        "--delta-threshold",
        type=float,
        default=None,
        help="Optional prior 15m BTC candle body threshold (|Close - Open|) in USD",
    )
    parser.add_argument(
        "--delta-source",
        type=str,
        default=None,
        help="Data source for delta filter (default: 'binance')",
    )

    parser.add_argument(
        "--delta-ceiling-range",
        "--delta-range-max",
        "--range-ceiling",
        type=float,
        default=None,
        help="Optional ceiling/max prior 15m BTC candle range in USD (e.g. 500.0)",
    )
    parser.add_argument(
        '--filter-rules',
        type=str,
        default=None,
        help="Custom compound filter rules as shorthand or JSON (e.g. 'vol>=0.16,range>=10 & range<=500 | vol>=0.30,range>=5')",
    )
    parser.add_argument(
        '--dynamic-or',
        action='store_true',
        default=False,
        help='Enable 2-tier Dynamic OR compound filter (e.g. Vol>=0.14pct and Range>=$220 OR Vol>=0.21pct and Range>=$150)',
    )
    parser.add_argument(
        '--dynamic-vol-tier',
        type=float,
        default=0.21,
        help='Second tier volatility threshold in percent for --dynamic-or (default: 0.21)',
    )
    parser.add_argument(
        '--dynamic-range-tier',
        type=float,
        default=150.0,
        help='Second tier 15m candle range in USD for --dynamic-or (default: 150.0)',
    )
    parser.add_argument(
        '--dynamic-body-tier',
        type=float,
        default=None,
        help='Second tier candle body in USD for --dynamic-or (optional)',
    )
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()

    # 1. Load config file
    config_path = args.config
    config_data: dict[str, Any] = {}
    try:
        with open(config_path, "r") as f:
            config_data = json.load(f)
    except FileNotFoundError:
        print(f"⚠️ Configuration file {config_path} not found. Using default values.")
        config_data = {
            "strategy_name": "Consecutive Streak",
            "mode": "dry_run",
            "max_transactions": 1000,
            "decisive_up": 0.90,
            "decisive_down": 0.10,
            "seconds_before_close": 15,
            "fade_stake_usd": 10.0,
            "fade_profit_target_usd": None,
            "notifications": True,
            "asset": "BTC",
            "consecutive_streak_count": 7,
            "cap": None,
            "stop_maker_sec": 45,
            "min_maker_sec": 0,
            "taker_limit_price": 0.55,
            "active_order_adjusted": True,
            "enable_price_savings_bank": True,
            "max_maker_price": 0.50,
            "state_file": None,
            "reset_state": False,
        }

    # 2. Merge command-line arguments (they override config file values)
    strategy_name = str(
        args.strategy_name
        if args.strategy_name is not None
        else config_data.get("strategy_name", config_data.get("session_id", "Consecutive Streak"))
    )
    mode_val = args.mode if args.mode is not None else config_data.get("mode", "dry_run")
    mode = BotMode(mode_val)

    max_tx = args.max_transactions if args.max_transactions is not None else config_data.get("max_transactions")
    decisive_up = args.decisive_up if args.decisive_up is not None else config_data.get("decisive_up", 0.90)
    decisive_down = args.decisive_down if args.decisive_down is not None else config_data.get("decisive_down", 0.10)
    seconds_before_close = args.seconds_before_close if args.seconds_before_close is not None else config_data.get("seconds_before_close", 30)
    history_beats_count = args.history_beats_count if args.history_beats_count is not None else config_data.get("history_beats_count", 50)
    consecutive_streak_count = (
        args.consecutive_streak_count
        if args.consecutive_streak_count is not None
        else config_data.get("consecutive_streak_count", 2)
    )
    max_cap = (
        args.max_cap
        if getattr(args, "max_cap", None) is not None
        else (args.cap if args.cap is not None else config_data.get("max_cap", config_data.get("cap", 3)))
    )
    cap = max_cap
    base_stake_usd = (
        args.base_stake_usd
        if getattr(args, "base_stake_usd", None) is not None
        else (args.fade_stake_usd if args.fade_stake_usd is not None else config_data.get("base_stake_usd", config_data.get("fade_stake_usd", 10.0)))
    )
    fade_stake_usd = base_stake_usd

    stake_mult_cfg = (
        args.stake_multipliers
        if getattr(args, "stake_multipliers", None) is not None
        else config_data.get("stake_multipliers", [1.0, 2.0, 4.0])
    )
    if isinstance(stake_mult_cfg, str):
        stake_multipliers = [float(x.strip()) for x in stake_mult_cfg.split(",") if x.strip()]
    elif isinstance(stake_mult_cfg, list):
        stake_multipliers = [float(x) for x in stake_mult_cfg]
    else:
        stake_multipliers = [1.0, 2.0, 4.0]
    pattern_cfg = args.pattern if getattr(args, "pattern", None) is not None else config_data.get("pattern")
    campaign_pattern = normalize_pattern(pattern_cfg)
    sync_corrections = (
        args.sync_corrections
        if getattr(args, "sync_corrections", None) is not None
        else bool(config_data.get("sync_corrections", False))
    )
    fade_profit_target_usd = args.fade_profit_target_usd if args.fade_profit_target_usd is not None else config_data.get("fade_profit_target_usd")
    notifications_enabled = args.notifications if args.notifications is not None else config_data.get("notifications", True)
    asset_val = args.asset if args.asset is not None else config_data.get("asset", "BTC")
    asset = asset_val.upper()
    stop_maker_sec = args.stop_maker_sec if args.stop_maker_sec is not None else config_data.get("stop_maker_sec", 45)
    min_maker_sec = (
        args.min_maker_sec
        if args.min_maker_sec is not None
        else config_data.get("min_maker_sec", config_data.get("start_maker_sec", 0))
    )
    taker_limit_price = args.taker_limit_price if args.taker_limit_price is not None else config_data.get("taker_limit_price", 0.55)
    beat_duration_min = args.beat_duration_min if args.beat_duration_min is not None else config_data.get("beat_duration_min", 5)
    active_order_adjusted_enabled = args.active_order_adjusted if args.active_order_adjusted is not None else config_data.get("active_order_adjusted", True)
    enable_price_savings_bank = args.enable_price_savings_bank if args.enable_price_savings_bank is not None else config_data.get("enable_price_savings_bank", True)
    max_maker_price = args.max_maker_price if args.max_maker_price is not None else config_data.get("max_maker_price", 0.50)
    marketable_limit = args.marketable_limit if args.marketable_limit is not None else config_data.get("marketable_limit", True)
    state_file = args.state_file if args.state_file is not None else config_data.get("state_file")
    if not state_file:
        clean_strat = re.sub(r'[^a-zA-Z0-9_.\-]', '_', strategy_name).strip('_')
        clean_strat = re.sub(r'_+', '_', clean_strat)
        mode_prefix = "dry_run_" if mode == BotMode.DRY_RUN else ""
        state_file = f"state_{mode_prefix}{asset}_{clean_strat}.json"
    reset_state = args.reset_state if args.reset_state is not None else config_data.get("reset_state", False)
    raw_dynamic = config_data.get("dynamic_staking")
    if isinstance(raw_dynamic, dict):
        dynamic_staking_config = dict(raw_dynamic)
    else:
        dynamic_staking_config = {
            "enabled": False,
            "rolling_window_triggers": 8,
            "target_win_rate": 0.519,
            "sensitivity_alpha": 1.5,
            "min_stake_usd": None,
            "max_stake_usd": None,
            "max_consecutive_loss_cap": 3,
        }

    if args.enable_dynamic_staking:
        dynamic_staking_config["enabled"] = True
    elif args.disable_dynamic_staking:
        dynamic_staking_config["enabled"] = False

    if args.dynamic_window is not None:
        dynamic_staking_config["rolling_window_triggers"] = args.dynamic_window
    if args.dynamic_alpha is not None:
        dynamic_staking_config["sensitivity_alpha"] = args.dynamic_alpha
    if args.dynamic_target_wr is not None:
        dynamic_staking_config["target_win_rate"] = args.dynamic_target_wr
    if args.dynamic_min_stake is not None:
        dynamic_staking_config["min_stake_usd"] = args.dynamic_min_stake
    if args.dynamic_max_stake is not None:
        dynamic_staking_config["max_stake_usd"] = args.dynamic_max_stake
    if args.dynamic_max_loss_cap is not None:
        dynamic_staking_config["max_consecutive_loss_cap"] = args.dynamic_max_loss_cap

    vol_filter_cfg = config_data.get("volatility_filter", {})
    if not isinstance(vol_filter_cfg, dict):
        vol_filter_cfg = {"enabled": bool(vol_filter_cfg)}
    vol_enabled = args.volatility_filter if args.volatility_filter is not None else vol_filter_cfg.get("enabled", False)
    vol_threshold = args.volatility_threshold if args.volatility_threshold is not None else vol_filter_cfg.get("threshold_pct", 0.13)
    vol_lookback = args.volatility_lookback if args.volatility_lookback is not None else vol_filter_cfg.get("lookback_candles", 16)
    volatility_filter_config = {
        "enabled": vol_enabled,
        "threshold_pct": vol_threshold,
        "lookback_candles": vol_lookback,
    }

    tp_cfg = config_data.get("take_profit", {})
    if not isinstance(tp_cfg, dict):
        tp_cfg = {"enabled": bool(tp_cfg)}
    tp_enabled = args.take_profit if args.take_profit is not None else tp_cfg.get("enabled", False)
    tp_target_pct = args.take_profit_pct if args.take_profit_pct is not None else tp_cfg.get("target_pct", 0.14)
    take_profit_config = {
        "enabled": tp_enabled,
        "target_pct": tp_target_pct,
    }

    delta_filter_cfg = config_data.get("delta_filter", {})
    if not isinstance(delta_filter_cfg, dict):
        delta_filter_cfg = {"enabled": bool(delta_filter_cfg)}
    delta_enabled = args.delta_filter if args.delta_filter is not None else delta_filter_cfg.get("enabled", False)
    delta_max_range = args.delta_max_range if args.delta_max_range is not None else delta_filter_cfg.get("delta_max_range", delta_filter_cfg.get("threshold", 230.0))
    delta_ceiling_range = args.delta_ceiling_range if args.delta_ceiling_range is not None else delta_filter_cfg.get("range_max", delta_filter_cfg.get("delta_range_max", delta_filter_cfg.get("delta_ceiling_range")))
    delta_threshold = args.delta_threshold if args.delta_threshold is not None else delta_filter_cfg.get("delta_threshold", None)
    delta_source = args.delta_source if args.delta_source is not None else delta_filter_cfg.get("source", "binance")
    asym_parsed = parse_asymmetric_config(delta_filter_cfg)
    delta_filter_config = {
        "enabled": delta_enabled,
        "delta_max_range": delta_max_range,
        "range_max": delta_ceiling_range,
        "delta_threshold": delta_threshold,
        "source": delta_source,
        "asymmetric": asym_parsed,
    }

    # Resolve Dynamic Compound OR Filter
    dynamic_filter_config = None
    if args.dynamic_or or args.filter_rules:
        if args.filter_rules:
            rules = parse_dynamic_filter_rules(args.filter_rules)
        else:
            r1_vol = args.volatility_threshold if args.volatility_threshold is not None else 0.14
            r1_range = args.delta_max_range if args.delta_max_range is not None else 220.0
            r1_body = getattr(args, 'delta_threshold', None)
            r2_vol = args.dynamic_vol_tier
            r2_range = args.dynamic_range_tier
            r2_body = getattr(args, 'dynamic_body_tier', None)

            rule1 = {'vol_threshold_pct': r1_vol, 'delta_max_range': r1_range}
            if r1_body is not None:
                rule1['delta_threshold'] = r1_body

            rule2 = {'vol_threshold_pct': r2_vol, 'delta_max_range': r2_range}
            if r2_body is not None:
                rule2['delta_threshold'] = r2_body

            rules = [rule1, rule2]
        if rules:
            if delta_ceiling_range is not None:
                for r in rules:
                    if 'range_max' not in r:
                        r['range_max'] = float(delta_ceiling_range)
            dynamic_filter_config = {'enabled': True, 'rules': rules}
    elif 'dynamic_filter' in config_data or 'filter_rules' in config_data:
        raw_dyn = config_data.get('dynamic_filter', config_data.get('filter_rules'))
        if isinstance(raw_dyn, dict) and raw_dyn.get('enabled', True):
            rules = parse_dynamic_filter_rules(raw_dyn.get('rules', raw_dyn))
            if rules:
                dynamic_filter_config = {'enabled': True, 'rules': rules}
        elif isinstance(raw_dyn, list):
            rules = parse_dynamic_filter_rules(raw_dyn)
            if rules:
                dynamic_filter_config = {'enabled': True, 'rules': rules}
    elif 'delta_filter' in config_data and isinstance(config_data['delta_filter'], dict) and 'dynamic_or' in config_data['delta_filter']:
        rules = parse_dynamic_filter_rules(config_data['delta_filter']['dynamic_or'])
        if rules:
            dynamic_filter_config = {'enabled': True, 'rules': rules}

    if delta_ceiling_range is not None and dynamic_filter_config and 'rules' in dynamic_filter_config:
        for r in dynamic_filter_config['rules']:
            if 'range_max' not in r:
                r['range_max'] = float(delta_ceiling_range)

    # Always log to file for all modes (both dry_run and real) to prevent infinite log growth
    starting_date_str = datetime.now().strftime("%Y-%m-%d")
    date_folder = os.path.join("logs", starting_date_str)
    os.makedirs(date_folder, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode_prefix = "dry_run_" if mode == BotMode.DRY_RUN else ""
    pid = os.getpid()
    log_filename = f"{mode_prefix}{asset}_{timestamp_str}_{pid}.log"
    _log_file_path = os.path.join(date_folder, log_filename)
    print(f"Logging to file: {_log_file_path}")

    try:
        asyncio.run(run_live_bot(
            mode=mode,
            strategy_name=strategy_name,
            max_transactions=max_tx,
            decisive_up=decisive_up,
            decisive_down=decisive_down,
            seconds_before_close=seconds_before_close,
            history_beats_count=history_beats_count,
            consecutive_streak_count=consecutive_streak_count,
            cap=cap,
            fade_stake_usd=fade_stake_usd,
            fade_profit_target_usd=fade_profit_target_usd,
            notifications_enabled=notifications_enabled,
            asset=asset,
            base_stake_usd=base_stake_usd,
            stake_multipliers=stake_multipliers,
            max_cap=max_cap,
            stop_maker_sec=stop_maker_sec,
            min_maker_sec=min_maker_sec,
            taker_limit_price=taker_limit_price,
            beat_duration_min=beat_duration_min,
            active_order_adjusted_enabled=active_order_adjusted_enabled,
            enable_price_savings_bank=enable_price_savings_bank,
            max_maker_price=max_maker_price,
            marketable_limit=marketable_limit,
            state_file=state_file,
            reset_state=reset_state,
            dynamic_staking_config=dynamic_staking_config,
            volatility_filter_config=volatility_filter_config,
            take_profit_config=take_profit_config,
            delta_filter_config=delta_filter_config,
            pattern=campaign_pattern,
            sync_corrections=sync_corrections,
            dynamic_filter_config=dynamic_filter_config,
        ))
    except KeyboardInterrupt:
        print("Bot stopped by user.")
