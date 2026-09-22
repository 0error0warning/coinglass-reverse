#!/usr/bin/env python3
"""
CoinGlass API Response Decryption — Pure Python, no browser, no API key.

Reverse-engineered from CoinGlass webpack module 12471 (CryptoJS AES-ECB).

Algorithm:
  Layer 1: AES-128-ECB(user_token, Key0)   → Gzip(actual_key)
  Layer 2: Gunzip                          → 16-char hex actual key
  Layer 3: AES-128-ECB(encrypted_body, key) → Gzip(JSON)
  Layer 4: Gunzip                          → plain JSON

Key derivation (v=1, universal since 2025):
  Key0 = base64(url_path)[:16]

Old v=55/66/77 constants (deprecated, no longer in use):
  v=55 → base64("170b070da9654622")[:16]
  v=66 → base64("d6537d845a964081")[:16]
  v=77 → base64("863f08689c97435b")[:16]

Usage:
  from decrypt import fetch_and_decrypt
  data = fetch_and_decrypt("https://capi.coinglass.com/api/spot/rsi/list")
"""

import json
import gzip
import base64
import time
from typing import Any, Dict
from urllib.parse import urlparse

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


# Historical key constants (found in webpack module 12471).
# Modern CoinGlass (2025+) uses v=1 universally — these are kept for
# backward compatibility with old archive data.
_KEY_TABLE = {
    "55": "170b070da9654622",
    "66": "d6537d845a964081",
    "77": "863f08689c97435b",
}


def _derive_key0(
    v: str,
    url: str = "",
    *,
    cache_ts: str = "",
    time_header: str = "",
) -> str:
    """Derive the first-layer decryption key from the `v` response header.

    CoinGlass rotates `v` on a daily cycle (webpack module 12471, function Xt):
      v=0 → base64(request header cache-ts-v2)[:16]
      v=1 → base64(url_path)[:16]
      v=2 → base64(response header time)[:16]
      v=55/66/77 → base64(legacy constant)[:16]
    """
    if v == "0":
        if not cache_ts:
            raise ValueError("v=0 requires cache_ts (request header cache-ts-v2)")
        constant = cache_ts
    elif v == "1":
        constant = urlparse(url).path or url.split("?")[0]
    elif v == "2":
        if not time_header:
            raise ValueError("v=2 requires time_header (response header time)")
        constant = time_header
    else:
        constant = _KEY_TABLE.get(v)
        if constant is None:
            raise ValueError(f"Unknown v={v}, known: 0,1,2 + {list(_KEY_TABLE)}")
    return base64.b64encode(constant.encode()).decode()[:16]


class CoinGlassError(ValueError):
    """Safe boundary error. Never include request URLs or signed parameters."""

    def __init__(self, category, code, message):
        self.category = category
        self.code = code
        super().__init__(f'{category}: {message}')


def check_business(value):
    if isinstance(value, dict):
        code = value.get('code')
        if value.get('success') is False or (code is not None and str(code) not in ('0', '200')):
            category = {'40000': 'authentication', '40003': 'permission', '50001': 'rate_limit'}.get(str(code), 'business')
            safe_code = code if isinstance(code, (int, str)) and str(code).isdigit() else 'business_error'
            raise CoinGlassError(category, safe_code, 'API rejected the request')
    return value


def decrypt(
    encrypted_body: str,
    user_token_b64: str,
    v: str,
    url: str = "",
    *,
    cache_ts: str = "",
    time_header: str = "",
) -> Any:
    """
    Decrypt a CoinGlass encrypted API response.

    Args:
        encrypted_body: Raw HTTP response body (JSON with "data" field).
        user_token_b64: Value of the 'user' response header.
        v: Value of the 'v' response header.
        url: API URL (needed when v="1").
        cache_ts: Request header cache-ts-v2 (needed when v="0").
        time_header: Response header time (needed when v="2").

    Returns:
        Decrypted JSON as Python dict.
    """
    try:
        outer = check_business(json.loads(encrypted_body))
        payload = base64.b64decode(outer["data"], validate=True)
        token = base64.b64decode(user_token_b64, validate=True)
        key0 = _derive_key0(str(v), url, cache_ts=cache_ts, time_header=time_header)
        step1 = unpad(AES.new(key0.encode(), AES.MODE_ECB).decrypt(token), 16)
        actual_key = gzip.decompress(step1).decode()
        step2 = unpad(AES.new(actual_key.encode(), AES.MODE_ECB).decrypt(payload), 16)
        result = check_business(json.loads(gzip.decompress(step2).decode()))
        if not isinstance(result, (dict, list)):
            raise CoinGlassError('schema', 'invalid_payload', 'Expected object or array')
        return result
    except CoinGlassError:
        raise
    except Exception:
        raise CoinGlassError('decrypt', 'invalid_ciphertext', 'Response decryption failed') from None


def fetch_and_decrypt(url: str, params: dict = None, timeout: int = 30) -> Any:
    """Fetch an encrypted CoinGlass API endpoint and return the decrypted data.

    Args:
        url: Full API URL (e.g. https://capi.coinglass.com/api/spot/rsi/list)
        params: Optional query parameters as a dict.
        timeout: Request timeout in seconds (default 30).

    Returns:
        Decrypted JSON as a Python dict or list.

    Raises:
        ValueError: If the response is missing the required encryption headers.
        requests.HTTPError: On non-200 HTTP status.
    """
    import requests

    cache_ts = str(int(time.time() * 1000))
    try:
        resp = requests.get(url, params=params or {}, timeout=timeout, headers={
            'Accept': 'application/json', 'cache-ts-v2': cache_ts,
            'encryption': 'true', 'language': 'en',
            'Origin': 'https://www.coinglass.com',
            'Referer': 'https://www.coinglass.com/',
            'User-Agent': 'Mozilla/5.0',
        })
        resp.raise_for_status()
    except requests.RequestException:
        raise CoinGlassError('transport', 'http_failure', 'HTTP request failed') from None
    headers = {k.lower(): v for k, v in resp.headers.items()}
    user, version = headers.get('user'), headers.get('v')
    if ('user' in headers) != ('v' in headers) or (('user' in headers) and (not user or version in (None, ''))):
        raise CoinGlassError('decrypt', 'missing_header', 'Incomplete encryption headers')
    if user is not None and version is not None:
        return decrypt(resp.text, user, version, url, cache_ts=cache_ts,
                       time_header=headers.get('time', ''))
    try:
        value = check_business(resp.json())
    except CoinGlassError:
        raise
    except Exception:
        raise CoinGlassError('schema', 'invalid_json', 'Invalid JSON response') from None
    if not isinstance(value, (dict, list)):
        raise CoinGlassError('schema', 'invalid_payload', 'Expected object or array')
    if isinstance(value, dict) and (isinstance(value.get('data'), str) or value.get('encryption') in (True, 'true')):
        raise CoinGlassError('decrypt', 'missing_headers', 'Encrypted payload without headers')
    return value
