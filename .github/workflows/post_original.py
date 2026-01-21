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
QUIET_START = int(os.getenv("QUIET_START", "23"))  # 11 PM
QUIET_END = int(os.getenv("QUIET_END", "6"))       # 6 AM

RUN_CHANCE = float(os.getenv("POST_RUN_CHANCE", "0.35"))  # adjust to taste
MAX_CHARS = int(os.getenv("MAX_CHARS", "260"))

STATE_DIR = ".bot_state"
STATE_JSON = os.path.join(STATE_DIR, "original_state.json")

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

def ai_client():
    return genai.Client(http_options=HttpOptions(api_version="v1"))

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

def already_posted_today(state: dict) -> bool:
    # optional: if you only want 1 post/day, enforce here
    # return state.get("last_post_day") == today_key_local()
    return False

def is_duplicate_today(state: dict, post_text: str) -> bool:
    day = today_key_local()
    key = f"hashes_{day}"
    hashes = set(state.get(key, []) or [])
    h = sha1(post_text.lower())
    return h in hashes

def remember_post(state: dict, post_text: str):
    day = today_key_local()
    key = f"hashes_{day}"
    hashes = list(state.get(key, []) or [])
    hashes.append(sha1(post_text.lower()))
    # keep last 50/day
    hashes = hashes[-50:]
    state[key] = hashes
    state["last_post_day"] = day
    state["last_post_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

# ---------------- PROMPT ----------------
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

# ---------------- MAIN ----------------
event_name = os.getenv("GITHUB_EVENT_NAME", "")
is_manual = (event_name == "workflow_dispatch")

should_skip_now(is_manual)

state = load_state()
if already_posted_today(state):
    print("SKIP: already posted today (daily limit).")
    raise SystemExit(0)

print("RUN: Proceeding (original post)")
c = ai_client()

try:
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

# X write client (OAuth 1.0a)
client_write = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

try:
    client_write.create_tweet(text=post_text, user_auth=True)
    remember_post(state, post_text)
    print("POSTED ✅:", post_text)
except tweepy.TooManyRequests:
    print("RATE LIMIT: X write rate limit hit. Skipping.")
    raise SystemExit(0)
except tweepy.Forbidden as e:
    r = getattr(e, "response", None)
    if r is not None:
        print("X STATUS:", r.status_code)
        print("X BODY:", r.text[:1000])
    else:
        print("X Forbidden 403:", str(e))
    raise SystemExit(0)
except Exception as e:
    print("X POST ERROR:", repr(e))
    raise SystemExit(0)
