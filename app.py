import os
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, redirect, request, session
from supabase import create_client


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID")
OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET")
OLX_REDIRECT_URI = os.environ.get("OLX_REDIRECT_URI")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("Faltam SUPABASE_URL ou SUPABASE_SECRET_KEY.")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

OLX_AUTHORIZE_URL = "https://www.olx.pt/oauth/authorize/"
OLX_TOKEN_URL = "https://www.olx.pt/api/open/oauth/token"
OLX_API_BASE = "https://www.olx.pt/api/partner"

POLL_SECONDS = max(30, int(os.environ.get("POLL_SECONDS", "60")))
AUTO_POLL = os.environ.get("AUTO_POLL", "true").lower() in {"1", "true", "yes", "sim"}

INITIALIZED_SENTINEL = "__TC_CAR_PREMIUM_BOT_INITIALIZED__"
process_lock = threading.Lock()


def api_items(response):
    """Aceita tanto respostas OLX em lista como {'data': [...]}."""
    payload = response.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    return []


def get_latest_tokens():
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token, refresh_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not token_data.data:
        return None, None
    row = token_data.data[0]
    return row.get("access_token"), row.get("refresh_token")


def refresh_olx_token(refresh_token):
    if not refresh_token:
        return None, None

    response = requests.post(
        OLX_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": OLX_CLIENT_ID,
            "client_secret": OLX_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "scope": "v2 read write",
        },
        timeout=20,
    )

    if not response.ok:
        return None, None

    tokens = response.json()
    new_access_token = tokens.get("access_token")
    new_refresh_token = tokens.get("refresh_token", refresh_token)

    if not new_access_token:
        return None, None

    supabase.table("olx_tokens").insert(
        {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
        }
    ).execute()
