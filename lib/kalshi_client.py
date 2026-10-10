"""
Kalshi Trade API trading client for the lookback fade bot.

Setup (.env):
  KALSHI_API_KEY_ID=your-api-key-id
  KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi-key.key
  # or KALSHI_PRIVATE_KEY=<pem string>
  KALSHI_USE_DEMO=0   # set to 1 for demo environment
"""

from __future__ import annotations

import os
import random
import time
import uuid
from typing import Any

import httpx
from dotenv import load_dotenv

from lib.kalshi_auth import build_auth_headers, load_private_key_from_env
from lib.kalshi_env import rest_base_url
from lib.kalshi_market import (
    kalshi_book_side_for_side,
    kalshi_price_for_side,
    parse_token_id,
    side_from_outcome,
)

load_dotenv()


class _KalshiClobCompat:
    """Thin wrapper so bot_live can call client._client.get_order()."""

    def __init__(self, client: "KalshiClient"):
        self._client = client

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._client.get_order_normalized(order_id)

    def get_trades(self, _params: Any = None) -> list[dict[str, Any]]:
        return []


class KalshiClient:
    def __init__(
        self,
        api_key_id: str | None = None,
        private_key: Any | None = None,
        base_url: str | None = None,
    ):
        self._api_key_id = api_key_id or os.environ["KALSHI_API_KEY_ID"]
        self._private_key = private_key or load_private_key_from_env()
        self._base_url = base_url or rest_base_url()
        self._http = httpx.Client(
            http2=True,
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60.0),
        )
        self._client = _KalshiClobCompat(self)
        self._print_balance()

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = build_auth_headers(
            self._private_key,
            self._api_key_id,
            method,
            self._base_url,
            path,
        )
        url = self._base_url + path
        resp = self._http.request(method, url, headers=headers, json=json_body, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"Kalshi API {method} {path} failed ({resp.status_code}): {resp.text}")
        if not resp.content:
            return {}
        return resp.json()

    def _print_balance(self):
        try:
            data = self._request("GET", "/portfolio/balance")
            balance = float(data.get("balance_dollars", 0))
            portfolio = data.get("portfolio_value", 0) / 100.0
            print(f"Kalshi API Key: {self._api_key_id[:8]}...")
            print(f"Cash Balance: ${balance:.2f}")
            print(f"Portfolio Value: ${portfolio:.2f}")
        except Exception as exc:
            print(f"⚠️  Could not fetch Kalshi balance: {exc}")

    def get_ok(self) -> dict[str, Any]:
        return self._request("GET", "/exchange/status")

    def get_usdc_balance(self) -> float:
        try:
            data = self._request("GET", "/portfolio/balance")
            return float(data.get("balance_dollars", 0))
        except Exception:
            return 0.0

    def get_portfolio_value(self) -> float:
        try:
            data = self._request("GET", "/portfolio/balance")
            return data.get("portfolio_value", 0) / 100.0
        except Exception:
            return 0.0

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        """Fetch open/resting portfolio orders from Kalshi API."""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        try:
            resp = self._request("GET", "/portfolio/orders", params=params)
            orders = resp.get("orders", [])
            return orders if isinstance(orders, list) else []
        except Exception as e:
            print(f"         [WARNING] Error querying portfolio orders: {e}")
            return []

    def get_order_normalized(self, order_id: str) -> dict[str, Any]:
        """Map Kalshi order fields to the shape expected by bot_live helpers."""
        raw = self.get_order(order_id)
        order = raw.get("order", raw)
        fill_count = float(order.get("fill_count_fp", 0))
        remaining = float(order.get("remaining_count_fp", 0))
        initial = float(order.get("initial_count_fp", 0))
        status = str(order.get("status", "")).lower()

        if status == "executed":
            pm_status = "matched"
        elif status == "resting":
            pm_status = "partially_matched" if fill_count > 0 else "live"
        elif status == "canceled":
            pm_status = "partially_matched" if fill_count > 0 else "canceled"
        else:
            pm_status = status

        outcome = str(order.get("outcome_side", order.get("side", "yes"))).lower()
        yes_price = float(order.get("yes_price_dollars", 0))
        no_price = float(order.get("no_price_dollars", 0))
        display_price = yes_price if outcome == "yes" else no_price

        taker_cost = float(order.get("taker_fill_cost_dollars", 0))
        maker_cost = float(order.get("maker_fill_cost_dollars", 0))
        total_cost = taker_cost + maker_cost
        avg_price = display_price
        if fill_count > 0 and total_cost > 0:
            avg_price = total_cost / fill_count

        ticker = order.get("ticker", "")
        token_id = f"{ticker}|{outcome}"

        return {
            "status": pm_status,
            "original_size": initial,
            "size_matched": fill_count,
            "price": display_price,
            "avg_price": avg_price,
            "token_id": token_id if 'token_id' in locals() else "",
            "associate_trades": [],
        }

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str | None = None,
        post_only: bool = False,
        action: str = "buy",
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        market_ticker, outcome = parse_token_id(token_id)
        trade_side = side_from_outcome(outcome) if side is None else side
        if action == "sell":
            book_side = "ask" if trade_side == "Up" else "bid"
        else:
            book_side = kalshi_book_side_for_side(trade_side)
        kalshi_price = kalshi_price_for_side(trade_side, price)

        cid = client_order_id if client_order_id else str(uuid.uuid4())

        body = {
            "ticker": market_ticker,
            "client_order_id": cid,
            "action": action,
            "side": book_side,
            "count": f"{size:.2f}",
            "price": f"{kalshi_price:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "cancel_order_on_pause": False,
            "reduce_only": False,
            "exchange_index": -1,
        }
        resp = self._request("POST", "/portfolio/events/orders", json_body=body)
        order_id = resp.get("order_id")
        return {
            "success": bool(order_id),
            "orderID": order_id,
            "client_order_id": cid,
            "fill_count": resp.get("fill_count"),
            "remaining_count": resp.get("remaining_count"),
            "average_fill_price": resp.get("average_fill_price"),
        }

    def buy_outcome(self, token_id: str, price: float, shares: float):
        return self.place_limit_order(token_id, price, shares, post_only=False)

    def sell_outcome_maker(self, token_id: str, price: float, shares: float) -> dict[str, Any]:
        """Place a post-only limit SELL order to lock in take-profit."""
        return self.place_limit_order(
            token_id=token_id,
            price=price,
            size=shares,
            post_only=True,
            action="sell",
            reduce_only=False,
        )

    def buy_outcome_ioc(self, token_id: str, price: float, shares: float):
        return self.place_ioc_order(token_id, price, shares)

    def buy_outcome_fak(self, token_id: str, price: float, usdc_amount: float):
        shares = usdc_amount / price if price > 0 else 0
        return self.place_ioc_order(token_id, price, shares)

    def place_ioc_order(self, token_id: str, price: float, shares: float) -> dict[str, Any]:
        market_ticker, outcome = parse_token_id(token_id)
        trade_side = side_from_outcome(outcome)
        book_side = kalshi_book_side_for_side(trade_side)
        kalshi_price = kalshi_price_for_side(trade_side, price)

        body = {
            "ticker": market_ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": book_side,
            "count": f"{shares:.2f}",
            "price": f"{kalshi_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": False,
            "exchange_index": -1,
        }
        resp = self._request("POST", "/portfolio/events/orders", json_body=body)
        order_id = resp.get("order_id")
        return {
            "success": bool(order_id),
            "orderID": order_id,
            "fill_count": resp.get("fill_count"),
            "remaining_count": resp.get("remaining_count"),
            "average_fill_price": resp.get("average_fill_price"),
        }

    def cancel_order(self, order_id: str, exchange_index: int = 2, market_ticker: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"exchange_index": exchange_index}
        if market_ticker:
            params["market_ticker"] = market_ticker.upper()
        try:
            return self._request("DELETE", f"/portfolio/events/orders/{order_id}", params=params)
        except RuntimeError as err:
            err_str = str(err).lower()
            if "404" in err_str or "not_found" in err_str or "not found" in err_str:
                return {"success": True, "already_canceled": True, "status": "not_found"}
            raise

    def is_warmed_up(self, token_id: str) -> bool:
        return True

    async def warmup_market(self, _event_ticker: str):
        return None

    def get_market(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker.upper()}")

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker.upper()}/orderbook")


if __name__ == "__main__":
    client = KalshiClient()
    print(client.get_ok())
