"""
Volatility Filter Module for Kalshi Consecutive Streak Bot.

Calculates realized volatility over the previous 4 hours using the 16 completed 15-minute Kalshi market settlement prices
immediately preceding a streak trigger (no look-ahead).
If realized volatility < threshold_pct (e.g. 0.13%), the signal is skipped.
"""

import os
import csv
import re
import glob
import time
import statistics
try:
    import requests
except ImportError:
    requests = None
from typing import Tuple, List, Optional, Dict

from lib.kalshi_env import rest_base_url
from lib.kalshi_market import series_ticker_for


def calculate_realized_volatility(returns: List[float]) -> float:
    """
    Computes sample standard deviation of 15m candle returns.
    Returns value as a decimal (e.g. 0.0013 for 0.13%).
    """
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns)


_returns_cache: dict = {}  # in-process cache (fast layer)
_CACHE_TTL_SEC = 60        # shared cache TTL — covers adjacent 15m decision windows

# File-based shared cache so all bot processes (cap1/2/3/4, BTC/ETH) share one API result.
# Located next to this file so it is accessible to all processes regardless of CWD.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".vol_cache")


def _shared_cache_path(asset_key: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"vol_cache_{asset_key}.json")


def _read_shared_cache(asset_key: str, count: int, now_ts: int) -> "List[float] | None":
    """Try reading a valid shared cache entry written by another process."""
    import json
    path = _shared_cache_path(asset_key)
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cached_ts = data.get("ts", 0)
        cached_count = data.get("count", 0)
        cached_returns = data.get("returns", [])
        if now_ts - cached_ts < _CACHE_TTL_SEC and cached_count == count and cached_returns:
            return cached_returns
    except Exception:
        pass
    return None


def _write_shared_cache(asset_key: str, count: int, now_ts: int, returns: "List[float]") -> None:
    """Write fetched returns to the shared file cache."""
    import json
    path = _shared_cache_path(asset_key)
    lock_path = path + ".lock"
    try:
        # Simple lock: skip write if another process is already writing
        if os.path.exists(lock_path) and now_ts - os.path.getmtime(lock_path) < 2:
            return
        with open(lock_path, "w") as lf:
            lf.write(str(os.getpid()))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": now_ts, "count": count, "returns": returns}, f)
    except Exception:
        pass
    finally:
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception:
            pass


def fetch_kalshi_15m_returns(asset: str = "BTC", count: int = 16) -> List[float]:
    """
    Fetches historical settlement prices for completed 15-minute Kalshi markets.
    Uses a two-layer cache to prevent 429 errors when multiple bot processes fire simultaneously:
      1. In-process memory cache (fastest, per-process)
      2. File-based shared cache (shared across all bot processes on this machine)
    Retries up to 3 times with exponential backoff on 429 responses.
    """
    import random

    asset_key = asset.upper()
    now_ts = int(time.time())

    # Layer 1: in-process cache
    if asset_key in _returns_cache:
        cached_ts, cached_count, cached_returns = _returns_cache[asset_key]
        if now_ts - cached_ts < _CACHE_TTL_SEC and cached_count == count:
            return cached_returns

    # Layer 2: shared file cache (written by whichever process fetched first)
    shared = _read_shared_cache(asset_key, count, now_ts)
    if shared is not None:
        _returns_cache[asset_key] = (now_ts, count, shared)
        return shared

    series_ticker = series_ticker_for(asset, 15)
    base_url = rest_base_url()
    url = f"{base_url}/markets"
    params = {"series_ticker": series_ticker, "max_close_ts": now_ts, "limit": count + 10}

    if requests is None:
        raise ImportError("The 'requests' package is required to fetch live Kalshi 15m returns. Please install requests.")

    # Retry with exponential backoff on 429 (rate limit) errors
    max_retries = 3
    backoff = 1.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", backoff))
                jitter = random.uniform(0.1, 0.5)
                sleep_sec = min(retry_after + jitter, 5.0)
                time.sleep(sleep_sec)
                backoff = min(backoff * 2, 4.0)
                last_exc = Exception(f"429 Client Error: Too Many Requests (attempt {attempt + 1}/{max_retries})")
                # Check if another process succeeded while we were waiting
                shared = _read_shared_cache(asset_key, count, now_ts)
                if shared is not None:
                    _returns_cache[asset_key] = (now_ts, count, shared)
                    return shared
                continue
            resp.raise_for_status()
            break
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(backoff + random.uniform(0.1, 0.3))
                backoff = min(backoff * 2, 4.0)
    else:
        raise last_exc  # Re-raise after exhausting all retries

    markets = resp.json().get("markets", [])

    valid_markets = []
    for m in markets:
        exp_val = m.get("expiration_value")
        if exp_val:
            try:
                val = float(exp_val)
                if val > 0:
                    valid_markets.append((m.get("close_time", ""), val))
            except (ValueError, TypeError):
                pass

    valid_markets.sort(key=lambda x: x[0])
    if len(valid_markets) < 2:
        return []

    prices = [val for _, val in valid_markets[-(count + 1):]]
    returns = [(prices[i] / prices[i - 1]) - 1.0 for i in range(1, len(prices))]

    # Write to both cache layers
    _returns_cache[asset_key] = (now_ts, count, returns)
    _write_shared_cache(asset_key, count, now_ts, returns)
    return returns


def fetch_binance_15m_returns(
    asset: str = "BTC",
    count: int = 16,
    timeout_sec: float = 4.0,
) -> List[float]:
    """
    Fetches completed 15-minute candle returns for `asset` directly from Binance public REST API.
    Calculates returns from the last `count` completed 15m candles preceding current beat:
      return_i = (close_i / open_i) - 1.0
    Uses dual-layer caching (in-process memory + shared file cache) to prevent redundant API queries.
    Gracefully falls back to fetch_kalshi_15m_returns if Binance endpoints are unreachable.
    """
    import urllib.request
    import json

    asset_key = asset.upper()
    cache_key = f"BINANCE_{asset_key}"
    now_ts = int(time.time())

    # Layer 1: in-process cache
    if cache_key in _returns_cache:
        cached_ts, cached_count, cached_returns = _returns_cache[cache_key]
        if now_ts - cached_ts < _CACHE_TTL_SEC and cached_count == count:
            return cached_returns

    # Layer 2: shared file cache
    shared = _read_shared_cache(cache_key, count, now_ts)
    if shared is not None:
        _returns_cache[cache_key] = (now_ts, count, shared)
        return shared

    symbol = f"{asset_key}USDT"
    current_beat_ts = (now_ts // 900) * 900
    # The last completed 15m candle ends 1 millisecond before current_beat_ts * 1000
    end_ms = (current_beat_ts * 1000) - 1

    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&endTime={end_ms}&limit={count}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=15m&endTime={end_ms}&limit={count}",
    ]

    last_exc = None
    data = None
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    raw_data = json.loads(resp.read().decode())
                if raw_data and len(raw_data) >= count:
                    data = raw_data[-count:]
                    break
            except Exception as e:
                last_exc = e
                time.sleep(0.2)
        if data is not None:
            break

    if not data or len(data) < count:
        # Fallback to Kalshi 15m returns if Binance fails
        try:
            kalshi_returns = fetch_kalshi_15m_returns(asset=asset, count=count)
            if len(kalshi_returns) >= count:
                return kalshi_returns
        except Exception:
            pass
        if last_exc:
            raise last_exc
        return []

    returns = []
    for k in data:
        o = float(k[1])
        c = float(k[4])
        if o > 0:
            returns.append((c / o) - 1.0)

    if len(returns) == count:
        _returns_cache[cache_key] = (now_ts, count, returns)
        _write_shared_cache(cache_key, count, now_ts, returns)

    return returns


def check_volatility_filter(
    asset: str = "BTC",
    enabled: bool = True,
    threshold_pct: float = 0.13,
    lookback_candles: int = 16,
    source: str = "binance",
) -> Tuple[bool, float, str]:
    """
    Evaluates whether a trigger should be skipped due to low volatility calculated from 15m historical data.
    Default source is 'binance' (pure Binance data).
    
    Returns:
        (should_skip: bool, vol_pct: float, log_message: str)
    """
    if not enabled:
        return False, 0.0, "[VOL FILTER] Disabled in config. Trading signal unconditionally."

    source_clean = str(source).strip().lower() if source else "binance"
    is_binance = (source_clean == "binance")
    source_label = "Binance" if is_binance else "Kalshi"

    try:
        if is_binance:
            returns = fetch_binance_15m_returns(asset=asset, count=lookback_candles)
        else:
            returns = fetch_kalshi_15m_returns(asset=asset, count=lookback_candles)

        if len(returns) < lookback_candles:
            return False, 0.0, f"[VOL FILTER WARNING] Insufficient {source_label} data points ({len(returns)}/{lookback_candles}). Allowing trade."

        vol = calculate_realized_volatility(returns)
        vol_pct = vol * 100.0

        if vol_pct < threshold_pct:
            msg = (
                f"[VOL FILTER: SKIP] 4H Realized Volatility ({source_label}) = {vol_pct:.4f}% (< {threshold_pct:.2f}% threshold). "
                f"Market is in low-volatility drift/chop. Signal SKIPPED."
            )
            return True, vol_pct, msg
        else:
            msg = (
                f"[VOL FILTER: TRADE] 4H Realized Volatility ({source_label}) = {vol_pct:.4f}% (>= {threshold_pct:.2f}% threshold). "
                f"Sufficient volatility confirmed. Signal ACCEPTED."
            )
            return False, vol_pct, msg

    except Exception as e:
        # Failsafe: if external API fails, log warning and allow normal trading (do not crash bot)
        return False, 0.0, f"[VOL FILTER ERROR] Could not fetch {source_label} historical market data ({e}). Allowing trade as fallback."


def load_historical_candles(
    csv_path: Optional[str] = None,
    asset: str = "BTC",
    search_dir: str = r"c:\Users\Jeremy\Documents\projects\collect_data\kalshi",
    source: str = "binance",
) -> Dict[int, Dict]:
    """
    Loads 15-minute historical candle data (open/close) keyed by beat_start_ts.
    If csv_path is not specified, auto-detects based on source ('binance' or 'kalshi').
    Default is 'binance' for pure Binance historical data.
    """
    candidates = []
    if csv_path and os.path.exists(csv_path):
        candidates = [csv_path]
    else:
        asset_lower = asset.lower()
        if source and str(source).strip().lower() == "binance":
            binance_candidates = [
                os.path.join(os.path.dirname(search_dir), f"binance_{asset_lower}_15min_historical.csv"),
                os.path.join(search_dir, f"binance_{asset_lower}_15min_historical.csv"),
            ]
            for bp in binance_candidates:
                if os.path.exists(bp) and os.path.getsize(bp) > 10000:
                    candidates.append(bp)
                    break

        if not candidates:
            patterns = [
                os.path.join(search_dir, f"{asset_lower}_15min_historical_*merged*.csv"),
                os.path.join(search_dir, f"KALSHI_{asset_lower}_15min_historical_*.csv"),
                os.path.join(search_dir, f"{asset_lower}_15min_historical_*.csv"),
            ]
            for pat in patterns:
                found = glob.glob(pat)
                if found:
                    found.sort(key=lambda p: os.path.getsize(p), reverse=True)
                    candidates.extend(found)
                    break

    if not candidates:
        return {}

    target_file = candidates[0]
    candles = {}
    with open(target_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            po = r.get("price_to_beat", r.get("open", "")).replace(",", "").strip()
            pf = r.get("final_price", r.get("close", "")).replace(",", "").strip()
            hi_str = r.get("high", "").replace(",", "").strip()
            lo_str = r.get("low", "").replace(",", "").strip()
            ts_str = r.get("beat_start_ts", "").strip()
            if po and pf and ts_str:
                try:
                    ts = int(ts_str)
                    candles[ts] = {
                        "open": float(po),
                        "close": float(pf),
                        "high": float(hi_str) if hi_str else None,
                        "low": float(lo_str) if lo_str else None,
                        "time_utc": r.get("beat_start_time_utc", ""),
                        "outcome": r.get("outcome", "")
                    }
                except ValueError:
                    pass

    # Seamlessly supplement with Binance historical candles if available and recent data is missing
    binance_path = os.path.join(os.path.dirname(search_dir), f"binance_{asset.lower()}_15min_historical.csv")
    if os.path.exists(binance_path) and os.path.abspath(binance_path) != os.path.abspath(target_file):
        try:
            with open(binance_path, "r", encoding="utf-8") as f:
                b_reader = csv.DictReader(f)
                for r in b_reader:
                    ts_str = r.get("beat_start_ts", "").strip()
                    if ts_str:
                        try:
                            b_ts = int(ts_str)
                            if b_ts not in candles:
                                po = r.get("price_to_beat", r.get("open", "")).replace(",", "").strip()
                                pf = r.get("final_price", r.get("close", "")).replace(",", "").strip()
                                hi_str = r.get("high", "").replace(",", "").strip()
                                lo_str = r.get("low", "").replace(",", "").strip()
                                if po and pf:
                                    candles[b_ts] = {
                                        "open": float(po),
                                        "close": float(pf),
                                        "high": float(hi_str) if hi_str else None,
                                        "low": float(lo_str) if lo_str else None,
                                        "time_utc": r.get("beat_start_time_utc", ""),
                                        "outcome": r.get("outcome", "")
                                    }
                        except ValueError:
                            pass
        except Exception:
            pass

    return candles


def download_binance_15m_candles(
    symbol: str = "BTCUSDT",
    start_ts: int = 0,
    end_ts: int = 0,
    output_path: Optional[str] = None,
) -> Dict[int, Dict]:
    """
    Downloads 15m historical candles from Binance public REST API.
    Saves to output_path if specified and returns candles dict indexed by ts.
    """
    import urllib.request
    import json
    from datetime import datetime, timezone
    
    current_start_ms = start_ts * 1000
    end_ms = end_ts * 1000
    all_candles = []
    
    while current_start_ms < end_ms:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&startTime={current_start_ms}&endTime={end_ms}&limit=1000"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
        except Exception:
            time.sleep(1)
            continue
            
        if not data:
            break
            
        for k in data:
            open_ts = int(k[0] // 1000)
            open_px = float(k[1])
            high_px = float(k[2])
            low_px = float(k[3])
            close_px = float(k[4])
            vol = float(k[5])
            time_str = datetime.fromtimestamp(open_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            all_candles.append({
                "beat_start_ts": open_ts,
                "beat_start_time_utc": time_str,
                "price_to_beat": open_px,
                "final_price": close_px,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "delta": abs(close_px - open_px),
                "volume": vol
            })
            
        last_ms = data[-1][0]
        if last_ms <= current_start_ms:
            break
        current_start_ms = last_ms + (15 * 60 * 1000)
        time.sleep(0.1)
        
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        combined_map = {}
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            try:
                with open(output_path, "r", encoding="utf-8") as f_in:
                    r = csv.DictReader(f_in)
                    for row in r:
                        if "beat_start_ts" in row and row["beat_start_ts"]:
                            combined_map[int(row["beat_start_ts"])] = row
            except Exception:
                pass
        for c in all_candles:
            combined_map[c["beat_start_ts"]] = c

        fieldnames = ["beat_start_ts", "beat_start_time_utc", "price_to_beat", "final_price", "open", "high", "low", "close", "delta", "volume"]
        sorted_rows = [combined_map[k] for k in sorted(combined_map.keys())]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(sorted_rows)

        return {int(r["beat_start_ts"]): r for r in sorted_rows}

    return {c["beat_start_ts"]: c for c in all_candles}


def get_historical_volatility(
    beat_start_ts: int,
    candles_by_ts: Dict[int, Dict],
    lookback_candles: int = 16,
) -> Tuple[bool, float]:
    """
    Calculates 4H realized volatility using `lookback_candles` (default 16) completed 15m candles
    immediately preceding `beat_start_ts` (900 seconds per candle, no look-ahead).
    Returns (is_valid: bool, vol_pct: float).
    """
    if not candles_by_ts:
        return False, 0.0

    returns = []
    for i in range(lookback_candles, 0, -1):
        c_ts = beat_start_ts - (i * 900)
        if c_ts in candles_by_ts:
            c = candles_by_ts[c_ts]
            if c["open"] > 0:
                returns.append((c["close"] / c["open"]) - 1.0)
            else:
                return False, 0.0
        else:
            return False, 0.0

    if len(returns) == lookback_candles:
        vol = calculate_realized_volatility(returns)
        return True, vol * 100.0

    return False, 0.0


def get_prior_beat_delta(
    beat_start_ts: int,
    candles_by_ts: Dict[int, Dict],
    metric: str = "range",
) -> Tuple[bool, float]:
    """
    Returns (is_valid: bool, delta_usd: float) for the 15m candle immediately
    preceding beat_start_ts (beat_start_ts - 900).
    metric:
      'range': high - low (if available, else abs(close - open))
      'body': abs(close - open)
    """
    if not candles_by_ts:
        return False, 0.0
    prev_ts = beat_start_ts - 900
    if prev_ts in candles_by_ts:
        c = candles_by_ts[prev_ts]
        if metric == "range" and c.get("high") is not None and c.get("low") is not None:
            return True, float(c["high"]) - float(c["low"])
        return True, abs(c["close"] - c["open"])
    return False, 0.0


def fetch_binance_15m_delta_range(
    asset: str = "BTC",
    beat_start_ts: Optional[int] = None,
    timeout_sec: float = 4.0,
) -> Tuple[bool, float, float, float, float, str]:
    """
    Fetches the 15-minute candle from Binance public REST API for the beat starting at beat_start_ts.
    If beat_start_ts is None, uses the current 15-minute beat start timestamp: (now // 900) * 900.
    Returns: (is_valid, delta_range, delta_body, high, low, log_message)
    where delta_range = high - low, and delta_body = abs(close - open).
    """
    import urllib.request
    import json
    import time

    if beat_start_ts is None:
        beat_start_ts = (int(time.time()) // 900) * 900

    symbol = f"{asset.upper()}USDT"
    start_ms = beat_start_ts * 1000

    # Primary and fallback endpoints
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&startTime={start_ms}&limit=2",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=15m&startTime={start_ms}&limit=2",
    ]

    last_err = None
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = json.loads(resp.read().decode())
                if data and len(data) > 0:
                    k = data[0]
                    open_px = float(k[1])
                    high_px = float(k[2])
                    low_px = float(k[3])
                    close_px = float(k[4])
                    delta_range = high_px - low_px
                    delta_body = abs(close_px - open_px)
                    msg = (
                        f"Binance {symbol} 15m candle [{beat_start_ts}] "
                        f"High: ${high_px:,.2f} | Low: ${low_px:,.2f} -> "
                        f"Range: ${delta_range:.2f} | Body: ${delta_body:.2f}"
                    )
                    return True, delta_range, delta_body, high_px, low_px, msg
            except Exception as e:
                last_err = e
                time.sleep(0.2)

    return False, 0.0, 0.0, 0.0, 0.0, f"Failed to fetch Binance 15m candle ({last_err})"


def parse_asymmetric_config(d: Optional[Dict]) -> Optional[Dict]:
    """
    Extracts and normalizes asymmetric streak move and candle 4 range filter settings
    from a dictionary, accepting dedicated 'asymmetric_filter' dict, nested 'asymmetric' object,
    boolean flags, or direct keys.
    """
    if not isinstance(d, dict):
        return None
    asym = d.get("asymmetric")
    if asym is True or isinstance(asym, dict) or "up_streak_move" in d or "up_move" in d or "asymmetric_filter" in d:
        target = asym if isinstance(asym, dict) else (d.get("asymmetric_filter") if isinstance(d.get("asymmetric_filter"), dict) else d)
        up_move = target.get("up_streak_move", target.get("up_move", target.get("up_min_streak_move", 350.0)))
        up_c4 = target.get("up_c4_range", target.get("up_range", target.get("up_min_candle4_range", 150.0)))
        down_move = target.get("down_streak_move", target.get("down_move", target.get("down_min_streak_move", 380.0)))
        down_c4 = target.get("down_c4_range", target.get("down_range", target.get("down_min_candle4_range", 220.0)))
        metric = target.get("move_metric", target.get("metric", "displacement"))
        return {
            "enabled": bool(target.get("enabled", d.get("enabled", True))),
            "up_streak_move": float(up_move),
            "up_c4_range": float(up_c4),
            "down_streak_move": float(down_move),
            "down_c4_range": float(down_c4),
            "move_metric": str(metric).lower(),
            "source": str(target.get("source", d.get("source", "binance"))),
        }
    return None


def fetch_binance_15m_streak_candles(
    asset: str = "BTC",
    current_beat_start_ts: Optional[int] = None,
    streak_count: int = 4,
    timeout_sec: float = 4.0,
) -> Tuple[bool, List[Dict], str]:
    """
    Fetches the last N completed 15m candles preceding current_beat_start_ts from Binance.
    Returns: (is_valid, candles_list, log_message)
    where candles_list has elements: {"open": float, "high": float, "low": float, "close": float, "ts": int}
    """
    import urllib.request
    import json
    import time

    if current_beat_start_ts is None:
        current_beat_start_ts = (int(time.time()) // 900) * 900

    symbol = f"{asset.upper()}USDT"
    start_ts = current_beat_start_ts - (streak_count * 900)
    start_ms = start_ts * 1000

    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&startTime={start_ms}&limit={streak_count + 1}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=15m&startTime={start_ms}&limit={streak_count + 1}",
    ]

    last_err = None
    for url in urls:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = json.loads(resp.read().decode())
                if data and len(data) >= streak_count:
                    candles = []
                    for k in data[:streak_count]:
                        candles.append({
                            "ts": int(k[0]) // 1000,
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                        })
                    c1, c4 = candles[0], candles[-1]
                    disp = abs(c4["close"] - c1["open"])
                    c4_rng = c4["high"] - c4["low"]
                    msg = (
                        f"Binance {symbol} {streak_count}-candle streak [{candles[0]['ts']} -> {candles[-1]['ts']}]: "
                        f"Disp: ${disp:,.2f} | C4 Range: ${c4_rng:,.2f}"
                    )
                    return True, candles, msg
            except Exception as e:
                last_err = e
                time.sleep(0.2)

    return False, [], f"Failed to fetch Binance {streak_count} streak candles ({last_err})"



def parse_dynamic_filter_rules(rules_val: Any) -> list[dict]:
    """
    Parses dynamic compound filter rules from JSON list/dict, JSON string, or shorthand string.
    Examples:
      - [{"vol_threshold_pct": 0.14, "delta_max_range": 220, "range_max": 500}, {"vol_threshold_pct": 0.21, "delta_max_range": 150}]
      - 'vol>=0.14,range>=220 | vol>=0.21,range>=150'
      - 'vol>=0.18, range>=10 & range<=500'
      - 'vol>=0.18, vol<=0.30, range>=230, range<=500'
      - '0.14,220 | 0.21,150'
    """
    if not rules_val:
        return []
    
    if isinstance(rules_val, str):
        s = rules_val.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                rules_val = json.loads(s)
            except Exception:
                pass
        else:
            # Handle accidental slash typo like 0/18 -> 0.18
            s_clean = re.sub(r'(\d+)/(\d+)', r'\1.\2', s)
            rule_tokens = [t.strip() for t in re.split(r'[|;]', s_clean) if t.strip()]
            parsed_rules = []
            for token in rule_tokens:
                rule_dict = {}
                # Split parts by either comma or ampersand
                parts = [p.strip() for p in re.split(r'[,&]', token) if p.strip()]
                for p in parts:
                    # 1. Check range between e.g. 10 <= range <= 500
                    m_between = re.search(r'([0-9.]+)\s*(?:<=|<)\s*(?:range|rng|delta_max_range|r)\s*(?:<=|<)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_between:
                        rule_dict["delta_max_range"] = float(m_between.group(1))
                        rule_dict["range_max"] = float(m_between.group(2))
                        continue

                    # 2. Check range ceiling e.g. range <= 500, max_range = 500
                    m_rng_ceil = re.search(r'\b(?:max_range|range_max|range_ceil(?:ing)?)\s*(?:<=|<|:|=)\s*([0-9.]+)', p, re.IGNORECASE) or \
                                 re.search(r'\b(?:range|rng|delta_max_range|r)\s*(?:<=|<)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_rng_ceil:
                        rule_dict["range_max"] = float(m_rng_ceil.group(1))
                        continue

                    # 3. Check range floor (min) e.g. range >= 10, range = 230
                    m_rng_min = re.search(r'\b(?:min_range|range_min|range_floor)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE) or \
                                re.search(r'\b(?:range|rng|delta_max_range|r)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_rng_min:
                        rule_dict["delta_max_range"] = float(m_rng_min.group(1))
                        continue

                    # 4. Check vol ceiling e.g. vol <= 0.30, max_vol = 0.30
                    m_vol_ceil = re.search(r'\b(?:max_vol|vol_max|vol_ceil(?:ing)?)\s*(?:<=|<|:|=)\s*([0-9.]+)', p, re.IGNORECASE) or \
                                 re.search(r'\b(?:vol|volatility|v)\s*(?:<=|<)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_vol_ceil:
                        rule_dict["vol_max_pct"] = float(m_vol_ceil.group(1))
                        continue

                    # 5. Check vol floor (min) e.g. vol >= 0.18, vol = 0.18
                    m_vol_min = re.search(r'\b(?:min_vol|vol_min|vol_floor)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE) or \
                                re.search(r'\b(?:vol|volatility|v)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_vol_min:
                        rule_dict["vol_threshold_pct"] = float(m_vol_min.group(1))
                        continue

                    # 6. Check body ceiling e.g. body <= 100
                    m_bdy_ceil = re.search(r'\b(?:max_body|body_max|body_ceil(?:ing)?)\s*(?:<=|<|:|=)\s*([0-9.]+)', p, re.IGNORECASE) or \
                                 re.search(r'\b(?:body|bdy|delta_threshold|b)\s*(?:<=|<)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_bdy_ceil:
                        rule_dict["body_max"] = float(m_bdy_ceil.group(1))
                        continue

                    # 7. Check body floor (min) e.g. body >= 90
                    m_bdy_min = re.search(r'\b(?:min_body|body_min|body_floor)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE) or \
                                re.search(r'\b(?:body|bdy|delta_threshold|b)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_bdy_min:
                        rule_dict["delta_threshold"] = float(m_bdy_min.group(1))
                        continue

                    # 8. Check displacement / streak move floor e.g. disp >= 450, displacement >= 450
                    m_disp_min = re.search(r'\b(?:disp(?:lacement)?|streak_move|move)\s*(?:>=|>|:|=)\s*([0-9.]+)', p, re.IGNORECASE)
                    if m_disp_min:
                        rule_dict["disp_min"] = float(m_disp_min.group(1))
                        continue

                    # Fallback for plain numbers like "0.14,220"
                    try:
                        val = float(p)
                        if "vol_threshold_pct" not in rule_dict and val < 1.0:
                            rule_dict["vol_threshold_pct"] = val
                        elif "delta_max_range" not in rule_dict:
                            rule_dict["delta_max_range"] = val
                    except ValueError:
                        pass
                if rule_dict:
                    parsed_rules.append(rule_dict)
            return parsed_rules

    if isinstance(rules_val, dict):
        if "rules" in rules_val:
            rules_val = rules_val["rules"]
        elif "dynamic_or" in rules_val:
            rules_val = rules_val["dynamic_or"]
        else:
            rules_val = [rules_val]

    normalized = []
    if isinstance(rules_val, list):
        for r in rules_val:
            if isinstance(r, dict):
                norm = {}
                v = r.get("vol_threshold_pct", r.get("threshold_pct", r.get("vol_threshold", r.get("vol"))))
                if v is not None:
                    norm["vol_threshold_pct"] = float(v)
                v_max = r.get("vol_max_pct", r.get("vol_max", r.get("max_vol", r.get("vol_ceiling"))))
                if v_max is not None:
                    norm["vol_max_pct"] = float(v_max)

                lb = r.get("lookback_candles", r.get("lookback"))
                if lb is not None:
                    norm["lookback_candles"] = int(lb)

                rng = r.get("delta_max_range", r.get("range_threshold", r.get("range", r.get("range_min"))))
                if rng is not None:
                    norm["delta_max_range"] = float(rng)
                rng_max = r.get("range_max", r.get("delta_range_max", r.get("max_range", r.get("range_ceiling"))))
                if rng_max is not None:
                    norm["range_max"] = float(rng_max)

                bdy = r.get("delta_threshold", r.get("body_threshold", r.get("body", r.get("body_min"))))
                if bdy is not None:
                    norm["delta_threshold"] = float(bdy)
                bdy_max = r.get("body_max", r.get("delta_body_max", r.get("max_body", r.get("body_ceiling"))))
                if bdy_max is not None:
                    norm["body_max"] = float(bdy_max)

                disp = r.get("disp_min", r.get("disp", r.get("displacement", r.get("streak_move"))))
                if disp is not None:
                    norm["disp_min"] = float(disp)

                logic = r.get("delta_logic", r.get("logic", "and"))
                norm["delta_logic"] = str(logic).lower()
                normalized.append(norm)
    return normalized


def evaluate_dynamic_rules(
    dynamic_rules: list[dict],
    vol_pct: float | None,
    delta_rng: float | None,
    delta_body: float | None,
    disp_val: float | None = None,
) -> tuple[bool, str, list[str]]:
    """
    Evaluates compound OR rules against current real-time Vol and Delta.
    Supports both floors (>=) and ceilings (<=) for Range, Vol, and Body.
    Returns: (passed: bool, match_summary: str, rule_statuses: list[str])
    """
    rule_statuses = []
    for idx, rule in enumerate(dynamic_rules, start=1):
        r_vol = rule.get("vol_threshold_pct", rule.get("threshold_pct"))
        r_vol_max = rule.get("vol_max_pct")
        r_rng = rule.get("delta_max_range", rule.get("range_threshold"))
        r_rng_max = rule.get("range_max")
        r_bdy = rule.get("delta_threshold", rule.get("body_threshold"))
        r_bdy_max = rule.get("body_max")
        r_logic = str(rule.get("delta_logic", rule.get("logic", "and"))).lower()

        vol_ok = True
        vol_desc_parts = []
        if r_vol is not None or r_vol_max is not None:
            v_min_ok = (r_vol is None) or (vol_pct is not None and vol_pct >= float(r_vol))
            v_max_ok = (r_vol_max is None) or (vol_pct is not None and vol_pct <= float(r_vol_max))
            vol_ok = v_min_ok and v_max_ok

            if r_vol is not None:
                vol_desc_parts.append(
                    f"V {vol_pct:.4f}% >= {float(r_vol):.2f}%" if v_min_ok else (
                        f"V {vol_pct:.4f}% < {float(r_vol):.2f}%" if vol_pct is not None else f"V N/A < {float(r_vol):.2f}%"
                    )
                )
            if r_vol_max is not None:
                vol_desc_parts.append(
                    f"V {vol_pct:.4f}% <= {float(r_vol_max):.2f}%" if v_max_ok else (
                        f"V {vol_pct:.4f}% > {float(r_vol_max):.2f}%" if vol_pct is not None else f"V N/A > {float(r_vol_max):.2f}%"
                    )
                )

        delta_ok = True
        delta_desc_parts = []
        if r_rng is not None or r_rng_max is not None or r_bdy is not None or r_bdy_max is not None:
            rng_min_ok = (r_rng is None) or (delta_rng is not None and delta_rng >= float(r_rng))
            rng_max_ok = (r_rng_max is None) or (delta_rng is not None and delta_rng <= float(r_rng_max))
            rng_ok = rng_min_ok and rng_max_ok

            bdy_min_ok = (r_bdy is None) or (delta_body is not None and delta_body >= float(r_bdy))
            bdy_max_ok = (r_bdy_max is None) or (delta_body is not None and delta_body <= float(r_bdy_max))
            bdy_ok = bdy_min_ok and bdy_max_ok

            if r_rng is not None:
                delta_desc_parts.append(
                    f"R ${delta_rng:.2f} >= ${float(r_rng):g}" if rng_min_ok else (
                        f"R ${delta_rng:.2f} < ${float(r_rng):g}" if delta_rng is not None else f"R N/A < ${float(r_rng):g}"
                    )
                )
            if r_rng_max is not None:
                delta_desc_parts.append(
                    f"R ${delta_rng:.2f} <= ${float(r_rng_max):g}" if rng_max_ok else (
                        f"R ${delta_rng:.2f} > ${float(r_rng_max):g}" if delta_rng is not None else f"R N/A > ${float(r_rng_max):g}"
                    )
                )

            if r_bdy is not None:
                delta_desc_parts.append(
                    f"B ${delta_body:.2f} >= ${float(r_bdy):g}" if bdy_min_ok else (
                        f"B ${delta_body:.2f} < ${float(r_bdy):g}" if delta_body is not None else f"B N/A < ${float(r_bdy):g}"
                    )
                )
            if r_bdy_max is not None:
                delta_desc_parts.append(
                    f"B ${delta_body:.2f} <= ${float(r_bdy_max):g}" if bdy_max_ok else (
                        f"B ${delta_body:.2f} > ${float(r_bdy_max):g}" if delta_body is not None else f"B N/A > ${float(r_bdy_max):g}"
                    )
                )

            if r_logic == "or":
                delta_ok = (rng_ok or bdy_ok)
            else:
                delta_ok = (rng_ok and bdy_ok)

        disp_ok = True
        disp_desc_parts = []
        r_disp = rule.get("disp_min", rule.get("disp", rule.get("displacement")))
        if r_disp is not None:
            disp_ok = (disp_val is not None and disp_val >= float(r_disp))
            disp_desc_parts.append(
                f"Disp ${disp_val:.2f} >= ${float(r_disp):g}" if disp_ok else (
                    f"Disp ${disp_val:.2f} < ${float(r_disp):g}" if disp_val is not None else f"Disp N/A < ${float(r_disp):g}"
                )
            )

        rule_ok = vol_ok and delta_ok and disp_ok
        status_parts = []
        if vol_desc_parts:
            status_parts.extend(vol_desc_parts)
        if delta_desc_parts:
            status_parts.extend(delta_desc_parts)
        if disp_desc_parts:
            status_parts.extend(disp_desc_parts)
        status_str = f"Filter {idx} ({' & '.join(status_parts)}): {'PASS' if rule_ok else 'FAIL'}"
        rule_statuses.append(status_str)

        if rule_ok:
            return True, f"Tier {idx} ({' & '.join(status_parts)})", rule_statuses

    return False, "Failed all dynamic tiers", rule_statuses
