"""Shared Kalshi API environment URLs (production vs demo)."""

from __future__ import annotations

import os

PROD_REST_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_REST_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


def use_demo_environment() -> bool:
    return os.environ.get("KALSHI_USE_DEMO", "0").strip().lower() in ("1", "true", "yes")


def rest_base_url() -> str:
    return DEMO_REST_BASE_URL if use_demo_environment() else PROD_REST_BASE_URL


def ws_url() -> str:
    return DEMO_WS_URL if use_demo_environment() else PROD_WS_URL
