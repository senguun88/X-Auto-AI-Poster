import os
import random
import json
import hashlib
from datetime import datetime, timezone
import pytz

import tweepy
from google import genai
from google.genai.types import HttpOptions

# ---------------- SETTINGS ----------------
TIMEZONE = os.getenv("TIMEZONE", "US/Mountain")
QUIET_START = int(os.getenv("QUIET_START", "23"))
QUIET_END = int(os.getenv("QUIET_END", "6"))

RUN_CHANCE = float(os.getenv("POST_RUN_CHANCE", "0.35"))
MAX_CHARS = int(os.getenv("MAX_CHARS", "260"))

STATE_DIR = ".bot_state"
STATE_JSON = os.path.join(STATE_DIR, "original_state.json")

POST_PROMPT = (
    "Write ONE short X post about crypto + macro.\n\n"
    "Rules:\n"
    "- 1–2 sentences.\n"
    "- Factual tone, no hype.\n"
    "- No hashtags, no emojis.\n"
    "- No links.\n"
    "- No price targets or specific levels like $90k.\n"
    "- Stay under 260 characters.\n"
    "- If nothing important happened, post a brief neutral market note.\n\n"
    "Output ONLY the post text."
)

# ---------------- HELPERS ----------------
def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)

def load_state():
    ensure_state_dir()
    if not os.path.exists(STATE_JSON):
        return {}
    try:
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    ensure_state_dir()
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f)

def today_key_local():
    tz = pytz.timezone(TIMEZONE)
    return datetime.now(tz).strftime("%Y-%m-%d")

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def clamp_text(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s[:n].strip()

def should_skip_now(is_manual: bool) -> None:
    if is_manual:
        return
    tz = pytz.timezone(TIMEZONE)
    hour = datetime.now(tz).hour
    if hour >= QUIET_START or hour < QUIET_END:
        print(f"SKIP: Quiet hours ({hour}:00 {TIMEZONE})")
        raise SystemExit(0)

    r = random.random()
    if r > RUN_CHANCE:
        print(f"SKIP: Random skip r={r:.3f} > chance={RUN_CHANCE}")
        raise SystemExit(0)

def is_duplicate_today(state: dict, post_text: str) -> bool:
    day = today_key_local()
    key = f"hashes_{day}"
    hashes = set(state.get(key, []) or [])
    return sha1(post_text.lower()) in hashes

def remember_post(state: dict, post_text: str):
    day = today_key_local()
    key = f"hashes_{day}"
    hashes = list(state.get(key, []) or [])
    hashes.append(sha1(post_text.lower()))
    state[key] = hashes[-50:]
    state["last_post_day"] = day
    state["last_post_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

def ai_client():
    return genai.Client(http_options=HttpOptions(api_version="v1"))

# ---------------- MAIN ----------------
event_name = os.getenv("GITHUB_EVENT_NAME", "")
is_manual = (event_name == "workflow_dispatch")

should_skip_now(is_manual)

state = load_state()

print("RUN: Proceeding (original post)")

# 1) Generate text
try:
    c = ai_client()
    resp = c.models.generate_content(model="gemini-2.5-flash", contents=POST_PROMPT)
    post_text = clamp_text(resp.text, MAX_CHARS)
except Exception as e:
    print("AI ERROR:", repr(e))
    raise SystemExit(0)

if not post_text or len(post_text) < 20:
    print("SKIP: generated post too short/empty.")
    raise SystemExit(0)

if is_duplicate_today(state, post_text):
    print("SKIP: duplicate content today (hash match).")
    raise SystemExit(0)

# 2) Post via OAuth1 + tweepy.API (most reliable)
api_key = os.getenv("X_API_KEY")
api_secret = os.getenv("X_API_SECRET")
access_token = os.getenv("X_ACCESS_TOKEN")
access_secret = os.getenv("X_ACCESS_SECRET")

missing = [k for k, v in {
    "X_API_KEY": api_key,
    "X_API_SECRET": api_secret,
    "X_ACCESS_TOKEN": access_token,
    "X_ACCESS_SECRET": access_secret,
}.items() if not v]

if missing:
    print("ERROR: Missing secrets:", ", ".join(missing))
    raise SystemExit(1)

try:
    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    api = tweepy.API(auth, wait_on_rate_limit=False)

    status = api.update_status(status=post_text)
    remember_post(state, post_text)
    print("POSTED ✅ id=", getattr(status, "id", None))
    print("TEXT:", post_text)

except tweepy.Forbidden as e:
    # Tweepy v4 puts server response in e.api_messages/e.response sometimes
    print("X Forbidden 403")
    try:
        r = getattr(e, "response", None)
        if r is not None:
            print("X STATUS:", r.status_code)
            print("X BODY:", (r.text or "")[:1200])
    except Exception:
        pass
    print("DETAIL:", str(e)[:500])
    raise SystemExit(0)

except tweepy.TooManyRequests:
    print("RATE LIMIT: X write rate limit hit. Skipping.")
    raise SystemExit(0)

except Exception as e:
    print("X POST ERROR:", repr(e))
    raise SystemExit(0)
