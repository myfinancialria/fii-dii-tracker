"""
Automated Fyers daily login using TOTP. Verbose error mode — prints the
exact response body when any step fails, so endpoint drift is debuggable.

Requires in .env:
    FYERS_CLIENT_ID, FYERS_SECRET, FYERS_REDIRECT
    FYERS_FY_ID, FYERS_PIN, FYERS_TOTP_KEY
"""
import os
import sys
import base64
import requests
import pyotp
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

load_dotenv()

CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
SECRET    = os.getenv("FYERS_SECRET")
REDIRECT  = os.getenv("FYERS_REDIRECT")
FY_ID     = (os.getenv("FYERS_FY_ID") or "").strip().upper()
PIN       = (os.getenv("FYERS_PIN") or "").strip()
TOTP_KEY  = (os.getenv("FYERS_TOTP_KEY") or "").strip().replace(" ", "")

for name, val in [("FYERS_FY_ID", FY_ID), ("FYERS_PIN", PIN), ("FYERS_TOTP_KEY", TOTP_KEY)]:
    if not val:
        sys.exit(f"Missing {name} in .env")

def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()

DEFAULT_HEADERS = {
    "Content-Type":    "application/json",
    "Accept":          "application/json",
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin":          "https://login.fyers.in",
    "Referer":         "https://login.fyers.in/",
    "Accept-Language": "en-US,en;q=0.9",
}

def post(label, url, payload, extra_headers=None):
    """POST with verbose error reporting."""
    headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
    print(f"\n[{label}] POST {url}")
    print(f"  payload keys: {list(payload.keys())}")
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    print(f"  status: {r.status_code}")
    try:
        body = r.json()
    except Exception:
        body = r.text
    if r.status_code >= 400:
        print(f"  RESPONSE BODY: {body}")
        sys.exit(f"[{label}] failed (HTTP {r.status_code}).")
    return r, body

BASE = "https://api-t2.fyers.in/vagator/v2"

# Step 1: trigger login flow with fy_id
print(f"Logging in as FY_ID: {FY_ID}  (length={len(FY_ID)})")
print(f"PIN length: {len(PIN)}")
print(f"TOTP_KEY length: {len(TOTP_KEY)}")

# Sanity-check the TOTP key is valid Base32
try:
    test_totp = pyotp.TOTP(TOTP_KEY).now()
    print(f"TOTP_KEY decodes OK. Current 6-digit code: {test_totp}")
except Exception as e:
    sys.exit(f"FYERS_TOTP_KEY is not valid Base32: {e}")

r1, body1 = post("send_login_otp", f"{BASE}/send_login_otp_v2",
                 {"fy_id": b64(FY_ID), "app_id": "2"})
req_key = body1.get("request_key")
if not req_key:
    sys.exit(f"No request_key in response. Body: {body1}")

# Step 2: verify TOTP
totp = pyotp.TOTP(TOTP_KEY).now()
r2, body2 = post("verify_otp", f"{BASE}/verify_otp",
                 {"request_key": req_key, "otp": totp})
req_key2 = body2.get("request_key")
if not req_key2:
    sys.exit(f"No request_key after verify_otp. Body: {body2}")

# Step 3: verify PIN -> get short-lived bearer
r3, body3 = post("verify_pin", f"{BASE}/verify_pin_v2",
                 {"request_key": req_key2,
                  "identity_type": "pin",
                  "identifier": b64(PIN)})
short_token = body3.get("data", {}).get("access_token")
if not short_token:
    sys.exit(f"No access_token after verify_pin. Body: {body3}")

# Step 4: exchange for auth_code
APP_ID, APP_TYPE = CLIENT_ID.split("-")
r4, body4 = post("get_auth_code", "https://api-t1.fyers.in/api/v3/token",
                 {
                    "fyers_id":      FY_ID,
                    "app_id":        APP_ID,
                    "redirect_uri":  REDIRECT,
                    "appType":       APP_TYPE,
                    "code_challenge": "",
                    "state":         "sample_state",
                    "scope":         "",
                    "nonce":         "",
                    "response_type": "code",
                    "create_cookie": True,
                 },
                 extra_headers={"Authorization": f"Bearer {short_token}"})

url_field = body4.get("Url") or body4.get("url")
if not url_field:
    sys.exit(f"No 'Url' in response. Body: {body4}")
auth_code = parse_qs(urlparse(url_field).query).get("auth_code", [None])[0]
if not auth_code:
    sys.exit(f"No auth_code in URL: {url_field}")

# Step 5: exchange auth_code for the real access_token
print("\n[exchange] generating access token...")
session = fyersModel.SessionModel(
    client_id=CLIENT_ID, secret_key=SECRET, redirect_uri=REDIRECT,
    response_type="code", grant_type="authorization_code",
)
session.set_token(auth_code)
resp = session.generate_token()

if "access_token" not in resp:
    sys.exit(f"Token generation failed: {resp}")

with open("access_token.txt", "w") as f:
    f.write(resp["access_token"])

print("\n✅ Auto-login successful. Token written to access_token.txt")
