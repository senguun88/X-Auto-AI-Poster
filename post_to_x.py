import os
import random
from datetime import datetime
import hashlib
import json
import pytz
import re

import tweepy
from google import genai
from google.genai.types import HttpOptions

# ---------------- SETTINGS ----------------
MAX_CHARS = 260

# Run schedule can still be frequent; posting is controlled by POST_CHANCE
POST_CHANCE = 0.25          # hourly schedule -> ~5–6 posts/day average

TIMEZONE = "US/Mountain"
QUIET_START = 23            # 11 PM
QUIET_END = 6               # 6 AM

# State files (persisted via GitHub Actions cache in the YAML below)
STATE_DIR = ".bot_state"
DAILY_POSTS_FILE = os.path.join(STATE_DIR, "daily_posts.json")

# ---------------- HUMOR PROMPT (EVERGREEN) ----------------
PROMPT_BASE = (
    "Write ONE funny, high-engagement X post.\n\n"
    "Tone:\n"
    "- Relatable, clever, punchy.\n"
    "- Clean humor (no harassment, no slurs, no punching down).\n"
    "- No politics.\n\n"
    "Hard rules:\n"
    "- Evergreen only (no news, no current events, no dates, no numbers tied to real-world events).\n"
    "- Do NOT mention specific BTC/ETH prices.\n"
    "- No emojis.\n"
    "- No hashtags.\n"
    "- No questions.\n"
    "- Under 220 characters.\n"
    "- Output only the post text.\n\n"
    "Good topics:\n"
    "- Work meetings, procrastination, money habits, tech/AI confusion, gym motivation, adulting, parenting.\n"
    "- Light crypto vibes allowed but only as feelings/vibes (no price levels).\n\n"
    "Make it sound like a human wrote it."
)

# ---------------- HELPERS ----------------
def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)

def load_daily_posts():
    ensure_state_dir()
    if not os.path.exists(DAILY_POSTS_FILE):
        return {}
    with open(DAILY_POSTS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_daily_posts(data: dict):
    ensure_state_dir()
    with open(DAILY_POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def text_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def today_key(tzname: str) -> str:
    tz = pytz.timezone(tzname)
    return datetime.now(tz).strftime("%Y-%m-%d")

def basic_safety_block(text: str) -> bool:
    """
    Simple local filter to avoid obvious risky topics.
    """
    t = text.lower()
    politics = ["election", "trump", "biden", "democrat", "republican", "congress", "senate"]
    if any(w in t for w in politics):
        return True
    harassment = ["kill yourself", "kys", "go die"]
    if any(w in t for w in harassment):
        return True
    return False

def looks_like_btc_price_anchor(text: str) -> bool:
    """
    Blocks posts that mention BTC/Bitcoin + a specific large price level which can go stale fast.
    Examples blocked: "BTC above $69k", "BTC at 90000", "$90,000 Bitcoin"
    """
    t = text.lower()
    if ("btc" not in t) and ("bitcoin" not in t):
        return False

    pats = [
        r"\$\s*\d{2,3}\s*[kK]\b",            # $90k
        r"\b\d{2,3}\s*[kK]\b",               # 90k
        r"\$\s*\d{1,3}(?:,\d{3})+\b",        # $90,000
        r"\b\d{5,6}\b",                      # 90000
    ]
    return any(re.search(p, text) for p in pats)

# ---------------- POST DECISION ----------------
tz = pytz.timezone(TIMEZONE)
now = datetime.now(tz)
hour = now.hour

event_name = os.getenv("GITHUB_EVENT_NAME", "")  # 'workflow_dispatch' when run manually
is_manual = (event_name == "workflow_dispatch")

# Quiet hours only apply to scheduled runs
if not is_manual:
    if hour >= QUIET_START or hour < QUIET_END:
        print(f"SKIP: Quiet hours ({hour}:00 {TIMEZONE})")
        raise SystemExit(0)

    r = random.random()
    if r > POST_CHANCE:
        print(f"SKIP: Random skip r={r:.3f} > chance={POST_CHANCE}")
        raise SystemExit(0)

print("POST: Proceeding (humor-only)")

# ---------------- X CLIENT (API v2) ----------------
client_x = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

# ---------------- AI HUMOR MODE ----------------
def generate_ai_text(avoid_texts: list[str]) -> str:
    client_ai = genai.Client(http_options=HttpOptions(api_version="v1"))

    avoid_block = ""
    if avoid_texts:
        recent = avoid_texts[-5:]
        avoid_block = (
            "\nExtra rule:\n"
            "Do NOT repeat the same joke/topic as any of these posts already made today:\n"
            + "\n".join([f"- {t}" for t in recent])
            + "\nPick a different angle.\n"
        )

    prompt = PROMPT_BASE + avoid_block

    # Generate multiple candidates
    candidates = []
    for _ in range(5):
        resp = client_ai.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = (resp.text or "").strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            candidates.append(text[:240])

    # Local filters
    filtered = []
    for c in candidates:
        if basic_safety_block(c):
            continue
        if looks_like_btc_price_anchor(c):
            continue
        filtered.append(c[:220])

    if not filtered:
        return ""

    # Ask model to pick best (JSON only)
    scoring_prompt = (
        "Pick the single best X post for engagement.\n"
        "Return ONLY JSON: {\"best_index\": <int>}.\n"
        "Reject anything that is political, mean, or needs current events.\n\n"
        "Candidates:\n" + "\n".join([f"{i}. {c}" for i, c in enumerate(filtered)])
    )

    pick = client_ai.models.generate_content(
        model="gemini-2.5-flash",
        contents=scoring_prompt,
    )
    raw = (pick.text or "").strip()

    try:
        data = json.loads(raw)
        idx = int(data.get("best_index", 0))
        if 0 <= idx < len(filtered):
            return filtered[idx][:MAX_CHARS]
    except Exception:
        pass

    return filtered[0][:MAX_CHARS]

def post_ai_text():
    day = today_key(TIMEZONE)
    data = load_daily_posts()
    todays = data.get(day, [])

    # Generate up to 4 tries if duplicates / blocked content
    for attempt in range(4):
        text = generate_ai_text([p["text"] for p in todays])
        if not text:
            print("AI: Empty output, regenerating...")
            continue

        # Final guardrails
        if basic_safety_block(text):
            print("AI: Blocked by safety filter, regenerating...")
            continue
        if looks_like_btc_price_anchor(text):
            print("AI: Blocked BTC price anchor, regenerating...")
            continue

        h = text_hash(text)
        if any(p.get("hash") == h for p in todays):
            print(f"AI: Duplicate text hash (attempt {attempt+1}), regenerating...")
            continue

        print("Generated text:", text)

        try:
            client_x.create_tweet(text=text, user_auth=True)
            print("Posted to X ✅")

            # Save to daily history
            todays.append({"hash": h, "text": text, "ts": now.isoformat()})
            data[day] = todays
            save_daily_posts(data)
            return

        except tweepy.Forbidden as e:
            r = getattr(e, "response", None)
            if r is not None:
                print("X STATUS:", r.status_code)
                print("X BODY:", r.text)
            else:
                print("X Forbidden 403:", str(e))

            print("SKIP: X API blocked this request (likely plan/permissions).")
            raise SystemExit(0)

    print("AI: Could not generate a safe non-duplicate post after retries. Skipping.")
    raise SystemExit(0)

# ---------------- MAIN: humor only ----------------
post_ai_text()
