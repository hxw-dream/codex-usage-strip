#!/usr/bin/env python3
"""Usage/balance data sources for the Codex usage widget.

Auto-detects the active provider from Codex's config.toml on every fetch:

  - no custom model_provider  -> ChatGPT subscription usage
      (chatgpt.com/backend-api/codex/usage, token from auth.json)
  - custom model_provider     -> relay balance via the generic one-api
      protocol (/v1/dashboard/billing/subscription + /usage), falling back to
      DeepSeek's native /user/balance when the relay doesn't implement it.
      When the active base_url can't serve billing at all (e.g. a local chat
      proxy), the same-name cc-switch entry is used as the billing source.

Returns a normalized dict; UI branches on the "mode" field:
  chatgpt: {mode, plan_type, primary_window, secondary_window, credits_balance, ...}
  relay:   {mode, provider, protocol, currency, balance, total, used, used_ratio, ...}
  any failure raises, or {"error": ...} is returned by fetch_usage_safe-style callers.
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def _http_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ------------------------------------------------------------------ chatgpt
def fetch_chatgpt() -> dict:
    with open(os.path.join(_codex_home(), "auth.json"), encoding="utf-8") as f:
        auth = json.load(f)
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise RuntimeError("auth.json has no ChatGPT access_token")

    data = _http_json(USAGE_URL, {
        "Authorization": "Bearer " + token,
        "chatgpt-account-id": tokens.get("account_id") or "",
        "User-Agent": "codex_cli_rs",
    })

    rl = data.get("rate_limit") or {}
    primary = rl.get("primary_window") or {}
    secondary = rl.get("secondary_window")
    credits = data.get("credits") or {}

    def window(w: dict) -> dict:
        return {
            "used_percent": w.get("used_percent"),
            "limit_window_seconds": w.get("limit_window_seconds"),
            "reset_after_seconds": w.get("reset_after_seconds"),
        }

    return {
        "mode": "chatgpt",
        "plan_type": data.get("plan_type"),
        "email": data.get("email"),
        "limit_reached": rl.get("limit_reached"),
        "primary_window": window(primary) if primary else None,
        "secondary_window": window(secondary) if secondary else None,
        "credits_balance": credits.get("balance"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ detection
def detect_provider(codex_home: str = None):
    """('chatgpt', None) or ('relay', {name, base_url, api_key})."""
    home = codex_home or _codex_home()
    try:
        import tomllib
        with open(os.path.join(home, "config.toml"), "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return "chatgpt", None
    except Exception:
        return "chatgpt", None

    name = cfg.get("model_provider")
    prov = (cfg.get("model_providers") or {}).get(name)
    if not name or not isinstance(prov, dict) or not prov.get("base_url"):
        return "chatgpt", None

    base = prov["base_url"].rstrip("/")
    # key resolution follows Codex/cc-switch conventions, newest first:
    # cc-switch v4 writes experimental_bearer_token into the provider block;
    # env_key providers read a env var; apikey-mode auth.json holds the rest
    key = prov.get("experimental_bearer_token") or ""
    if not key:
        env_key = prov.get("env_key")
        if env_key and os.environ.get(env_key):
            key = os.environ[env_key]
    if not key:
        try:
            with open(os.path.join(home, "auth.json"), encoding="utf-8") as f:
                key = json.load(f).get("OPENAI_API_KEY") or ""
        except Exception:
            key = ""
    return "relay", {"name": prov.get("name") or name, "base_url": base, "api_key": key}


# ------------------------------------------------------------------ one-api
def _api_root(base: str) -> str:
    return base if base.endswith("/v1") else base + "/v1"


def fetch_oneapi(base: str, key: str) -> dict:
    """Generic one-api/new-api/voapi billing endpoints (values in USD)."""
    hdr = {"Authorization": "Bearer " + key}
    root = _api_root(base)
    sub = _http_json(root + "/dashboard/billing/subscription", hdr)
    total = float(sub.get("hard_limit_usd") or 0)
    end = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    usg = _http_json(
        root + f"/dashboard/billing/usage?start_date=2020-01-01&end_date={end}", hdr)
    used = float(usg.get("total_usage") or 0) / 100.0  # endpoint returns cents
    balance = total - used
    return {
        "protocol": "one-api",
        "currency": "USD",
        "balance": round(balance, 4),
        "total": total,
        "used": round(used, 4),
        "used_ratio": (used / total) if total > 0 else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ deepseek
def fetch_deepseek(base: str, key: str) -> dict:
    """DeepSeek native balance endpoint (values in CNY)."""
    data = _http_json(base + "/user/balance", {"Authorization": "Bearer " + key})
    infos = data.get("balance_infos") or []
    info = next((i for i in infos if str(i.get("currency", "")).upper() == "CNY"),
                infos[0] if infos else {})
    balance = float(info.get("total_balance") or 0)
    return {
        "protocol": "deepseek",
        "currency": "CNY" if str(info.get("currency", "")).upper() == "CNY" else "USD",
        "balance": balance,
        "total": None,
        "used": None,
        "used_ratio": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ custom /v1/usage
def fetch_custom_usage(base: str, key: str) -> dict:
    """Custom /v1/usage endpoint (SAIL-style), same shape cc-switch reads:
    remaining ?? quota.remaining ?? balance; unit ?? quota.unit ?? USD."""
    url = (base + "/usage") if base.endswith("/v1") else (base + "/v1/usage")
    data = _http_json(url, {"Authorization": "Bearer " + key})
    quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
    balance = None
    for src in (data, quota):
        if src.get("remaining") is not None:
            balance = float(src["remaining"])
            break
    if balance is None:
        balance = float(data.get("balance") or 0)
    unit = str(data.get("unit") or quota.get("unit") or "USD").upper()
    today_cost = None
    du = data.get("daily_usage")
    if isinstance(du, list):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for e in du:
            if isinstance(e, dict) and e.get("date") == today and e.get("cost") is not None:
                today_cost = float(e["cost"])
    return {
        "protocol": "custom-usage",
        "currency": "CNY" if unit == "CNY" else "USD",
        "balance": round(balance, 4),
        "total": None,
        "used": None,
        "used_ratio": None,
        "today_cost": today_cost,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_relay(info: dict) -> dict:
    base, key = info["base_url"], info["api_key"]
    if not key:
        raise RuntimeError("custom provider has no API key (bearer token/env_key/auth.json)")
    chain = ((fetch_deepseek, fetch_oneapi, fetch_custom_usage)
             if "deepseek.com" in base
             else (fetch_oneapi, fetch_custom_usage, fetch_deepseek))
    last = None
    for fn in chain:
        try:
            return fn(base, key)
        except Exception as e:  # noqa: BLE001 - try next adapter
            last = e
    raise RuntimeError(f"relay balance endpoints unavailable ({last})")


# ------------------------------------------------------------------ cc-switch (read-only)
CCS_DB = os.path.join(os.path.expanduser("~"), ".cc-switch", "cc-switch.db")


def ccs_relay_providers() -> list:
    """Read-only snapshot of cc-switch codex providers that carry an API key.

    Never writes the DB; safe to read while CCS runs. Official (oauth) entries
    are excluded — their refresh tokens must not be used by us (rotation would
    break CCS's own tokens); official usage is served from auth.json instead.
    """
    import re
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CCS_DB.replace(chr(92), '/')}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT name, settings_config, meta FROM providers WHERE app_type='codex'"
        ).fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for name, sc, meta in rows:
        try:
            j = json.loads(sc)
            m = json.loads(meta) if meta else {}
        except Exception:
            continue
        key = (j.get("auth") or {}).get("OPENAI_API_KEY") or ""
        base_m = re.search(r'base_url\s*=\s*"([^"]+)"', j.get("config") or "")
        if not key or not base_m:
            continue                      # oauth/official entry without a key
        base = base_m.group(1).rstrip("/")
        script_m = re.search(r'url:\s*"\{\{baseUrl\}\}([^"]+)"', (m.get("usage_script") or {}).get("code", ""))
        out.append({"name": name, "base_url": base, "api_key": key,
                    "script_path": script_m.group(1) if script_m else None})
    return out


def fetch_named(name: str) -> dict:
    """Fetch balance/usage for a provider by its cc-switch display name.

    'OpenAI Official' (or anything not found in CCS with a key) falls back to
    the active auth.json tokens — only valid while official mode is active.
    """
    if name in ("OpenAI Official", "official"):
        u = fetch_chatgpt()
        u["provider"] = "OpenAI Official"
        return u
    for p in ccs_relay_providers():
        if p["name"] == name:
            base, key = p["base_url"], p["api_key"]
            if "deepseek.com" in base:
                u = fetch_deepseek(base, key)
            elif p["script_path"]:
                # same /v1/usage protocol; reuse the full parser (today_cost etc.)
                u = fetch_custom_usage(base, key)
            else:
                u = fetch_relay({"name": name, "base_url": base, "api_key": key})
            u["mode"] = "relay" if u.get("protocol") else u.get("mode")
            u["provider"] = name
            return u
    raise RuntimeError(f"provider {name!r} not found in cc-switch (or has no key)")


# ------------------------------------------------------------------ dispatch
def fetch_usage() -> dict:
    kind, info = detect_provider()
    if kind == "relay":
        try:
            u = fetch_relay(info)
        except Exception:
            # The active provider may route through a local proxy that forwards
            # chats but not billing endpoints; the same-name cc-switch entry
            # holds the real billing base_url and key — resolve through it.
            u = _fetch_relay_via_ccs(info.get("name"))
            if u is None:
                raise
        u["mode"] = "relay"
        u["provider"] = info["name"]
        return u
    return fetch_chatgpt()


def _fetch_relay_via_ccs(name):
    """Balance for the active provider via its same-name cc-switch entry.

    None when no entry carries that name, or that entry fails too (the
    caller then re-raises the original error)."""
    if not name:
        return None
    for p in ccs_relay_providers():
        if p["name"] == name:
            try:
                return fetch_named(name)
            except Exception:
                return None
    return None
