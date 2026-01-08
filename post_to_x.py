import os
import random
from datetime import datetime
import hashlib
import json
import pytz

import tweepy
from google import genai
from google.genai.types import HttpOptions

# ---------------- SETTINGS ----------------
MAX_CHARS = 260
POST_CHANCE = 0.25          # hourly schedule -> ~5–6 posts/day average
TIMEZONE = "US/Mountain"
QUIET_START = 23            # 11 PM
QUIET_END = 6               # 6 AM

# Mix in retweets sometimes (keeps the account looking human)
RETWEET_CHANCE = 0.00       # 20% retweet, 80% AI post

# Retweet filters (tune these)
RT_QUERY = '(bitcoin OR btc OR ethereum OR etf OR fed OR cpi OR jobs) -is:retweet lang:en'
RT_MIN_LIKES = 200
RT_MIN_RTS = 50
RT_MAX_RESULTS = 30

# State files (persisted via GitHub Actions cache in the YAML below)
STATE_DIR = ".bot_state"
RETWEETED_IDS_FILE = os.path.join(STATE_DIR, "retweeted_ids.txt")
DAILY_POSTS_FILE = os.path.join(STATE_DIR, "daily_posts.json")

PROMPT_BASE = (
    "Write ONE concise, high-impact X post about the single most important market-moving event from today.\n\n"

    "Tone & style:\n"
    "- Write like a Bloomberg/Reuters terminal headline rewritten for X\n"
    "- Lead with the surprise/key number or the outcome first\n"
    "- Short sentences. No fluff. No filler\n"
    "- Prefer numbers over adjectives\n"
    "- State the immediate market implication in one line (BTC/ETH, yields, USD, equities, oil, gold)\n\n"

    "Priority order:\n"
    "1) Major crypto news (Bitcoin, Ethereum, ETFs, regulation, exchange actions)\n"
    "2) If no major crypto news, choose ONE major non-crypto market mover:\n"
    "- Fed/CPI/jobs data\n"
    "- USD/yields, oil, gold\n"
    "- Major tech earnings or AI announcements\n\n"

    "Hard rules:\n"
    "- Factual only. No opinions. No predictions\n"
    "- No emojis\n"
    "- No hashtags\n"
    "- No questions\n"
    "- Avoid filler phrases like 'today', 'investors are watching', 'markets are reacting'\n"
    "- Target 180–220 characters (do not exceed 240)\n"
    "- Output only the post text, nothing else\n\n"

    "Format:\n"
    "Line 1: What happened (key fact/number)\n"
    "Line 2: Immediate impact (what moved + why it matters)\n"
)

# ---------------- HELPERS ----------------
def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)

def load_retweeted_ids():
    ensure_state_dir()
    if not os.path.exists(RETWEETED_IDS_FILE):
        return set()
    with open(RETWEETED_IDS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_retweeted_id(tweet_id: str):
    ensure_state_dir()
    with open(RETWEETED_IDS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{tweet_id}\n")

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

# ---------------- POST DECISION ----------------
tz = pytz.timezone(TIMEZONE)
now = datetime.now(tz)
hour = now.hour

event_name = os.getenv("GITHUB_EVENT_NAME", "")  # 'workflow_dispatch' when you run manually
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

print("POST: Proceeding")

# ---------------- X CLIENT (API v2) ----------------
client_x = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

# ---------------- RETWEET MODE ----------------
def try_retweet_important() -> bool:
    """
    Returns True if retweet succeeded, else False (fallback to AI post).
    """
    try:
        seen = load_retweeted_ids()

        resp = client_x.search_recent_tweets(
            query=RT_QUERY,
            max_results=min(RT_MAX_RESULTS, 100),
            tweet_fields=["public_metrics", "created_at"],
        )
        if not resp or not resp.data:
            print("RETWEET: No tweets found")
            return False

        candidates = []
        for t in resp.data:
            tid = str(t.id)
            if tid in seen:
                continue

            m = t.public_metrics or {}
            likes = m.get("like_count", 0)
            rts = m.get("retweet_count", 0)
            replies = m.get("reply_count", 0)

            if likes >= RT_MIN_LIKES and rts >= RT_MIN_RTS:
                score = likes + (2 * rts) + replies
                candidates.append((score, t))

        if not candidates:
            print("RETWEET: No tweets met thresholds")
            return False

        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][1]

        client_x.retweet(best.id, user_auth=True)
        save_retweeted_id(str(best.id))
        print(f"RETWEET: Retweeted {best.id} ✅")
        return True

    except tweepy.Forbidden as e:
        r = getattr(e, "response", None)
        if r is not None:
            print("RETWEET X STATUS:", r.status_code)
            print("RETWEET X BODY:", r.text)
        else:
            print("RETWEET Forbidden 403:", str(e))
        print("RETWEET: Blocked by plan/permissions. Fallback to AI post.")
        return False

    except Exception as e:
        print("RETWEET: Unexpected error:", repr(e))
        return False

# ---------------- AI POST MODE ----------------
def generate_ai_text(avoid_texts: list[str]) -> str:
    client_ai = genai.Client(http_options=HttpOptions(api_version="v1"))

    avoid_block = ""
    if avoid_texts:
        # Keep it short to avoid overprompting
        recent = avoid_texts[-5:]
        avoid_block = (
            "\nExtra rule:\n"
            "Do NOT repeat the same topic as any of these posts already made today:\n"
            + "\n".join([f"- {t}" for t in recent])
            + "\nPick a different major market-moving story from today.\n"
        )

    prompt = PROMPT_BASE + avoid_block

    resp = client_ai.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    text = (resp.text or "").strip().replace("\n", " ")
    return text[:MAX_CHARS]

def post_ai_text():
    day = today_key(TIMEZONE)
    data = load_daily_posts()
    todays = data.get(day, [])

    # Generate up to 3 tries if it repeats
    for attempt in range(3):
        text = generate_ai_text([p["text"] for p in todays])
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

    print("AI: Could not generate a non-duplicate post after retries. Skipping.")
    raise SystemExit(0)

# ---------------- MAIN: choose retweet vs AI ----------------
if random.random() < RETWEET_CHANCE:
    did = try_retweet_important()
    if not did:
        post_ai_text()
else:
    post_ai_text()
