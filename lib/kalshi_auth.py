"""RSA-PSS request signing for the Kalshi Trade API."""

from __future__ import annotations

import base64
import datetime
import os
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key_from_file(path: str):
    with open(path, "rb") as key_file:
        return serialization.load_pem_private_key(
            key_file.read(),
            password=None,
            backend=default_backend(),
        )


def load_private_key_from_env() -> object:
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem:
        return serialization.load_pem_private_key(
            pem.encode("utf-8"),
            password=None,
            backend=default_backend(),
        )
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not path:
        raise KeyError("Set KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH in .env")
    return load_private_key_from_file(path)


def sign_pss_text(private_key, text: str) -> str:
    message = text.encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def signing_path(base_url: str, path: str) -> str:
    """Full URL path used in the signature payload (no query string)."""
    return urlparse(base_url + path).path


def build_auth_headers(
    private_key,
    api_key_id: str,
    method: str,
    base_url: str,
    path: str,
) -> dict[str, str]:
    timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
    sign_path = signing_path(base_url, path.split("?")[0])
    msg_string = timestamp + method.upper() + sign_path
    signature = sign_pss_text(private_key, msg_string)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }
