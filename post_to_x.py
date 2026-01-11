import os
import random
from datetime import datetime, timezone, timedelta
import hashlib
import json
import pytz
import re

import tweepy
from google import genai
from google.genai.types import HttpOptions

# ============================================================
#  HUMOR REPLY BOT (AI picks latest + most engaged tweet)
#  - Replies (mostly) to high-engagement recent tweets
#  - AI selects best target among finalists
#  - AI writes a short witty reply (safe, non-cringe)
#  - Falls back to an original humor post if no targets found
#  - Avoids duplicates via local state
# ============================================================

# ---------------- SETTINGS ----------------
TIMEZONE = "US/Mountain"
QUIET_START = 23            # 11 PM
QUIET_END = 6               # 6 AM

POST_CHANCE = 0.35          # chance per scheduled run (set higher if you want more replies)
REPLY_CHANCE = 0.90         # 90% reply, 10% original (set 1.0 for replies only)

MAX_CHARS = 260
MAX_REPLY_CHARS = 200
MAX_ORIGINAL_CHARS = 220

# Only reply to tweets newer than this
MAX_TWEET_AGE_HOURS = 6

# Candidate selection limits
PER_ACCOUNT_MAX = 20        # tweets pulled per account (max 100)
CANDIDATE_POOL_LIMIT = 60   # total pool considered before AI
AI_FINALISTS = 12           # how many top tweets AI judges

# Engagement thresholds (lower these if you can’t find targets)
MIN_TARGET_LIKES = 30
MIN_TARGET_RTS = 5

# Target accounts (comma-separated handles WITHOUT @) set in GitHub Actions env:
# TARGET_ACCOUNTS="unusual_whales,CoinDesk,Cointelegraph,WatcherGuru,TheBlock__"
TARGET_ACCOUNTS = [a.strip().lstrip("@") for a in os.getenv("TARGET_ACCOUNTS", "").split(",") if a.strip()]

# ---------------- STATE ----------------
STATE_DIR = ".bot_state"
REPLIED_IDS_FILE = os.path.join(STATE_DIR, "replied_ids.txt")
DAILY_POSTS_FILE = os.path.join(STATE_DIR, "daily_posts.json")

# ---------------- PROMPTS ----------------
REPLY_PROMPT = (
    "Write ONE short reply to the tweet below.\n\n"
    "Goal:\n"
    "- Maximize likes and replies with witty/relatable humor.\n\n"
    "Hard rules:\n"
    "- Do NOT be mean. No insults. No politics. No tragedy.\n"
    "- Do NOT copy phrases from the tweet.\n"
    "- No emojis. No hashtags. No questions.\n"
    "- No price levels like $90k / 90,000 / etc.\n"
    "- Under 200 characters.\n"
    "- Output ONLY the reply text.\n\n"
    "Tweet to reply to:\n"
)

ORIGINAL_PROMPT = (
    "Write ONE funny, high-engagement X post.\n\n"
    "Tone:\n"
    "- Relatable, clever, punchy.\n"
    "- Clean humor. No politics.\n\n"
    "Hard rules:\n"
    "- Evergreen only (no news/current events/dates).\n"
    "- No emojis. No hashtags. No questions.\n"
    "- Under 220 characters.\n"
    "- Output only the post text.\n"
)

# ---------------- HELPERS ----------------
def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)

def load_replied_ids():
    ensure_state_dir()
    if not os.path.exists(REPLIED_IDS_FILE):
        return set()
    with open(REPLIED_IDS_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_replied_id(tweet_id: str):
    ensure_state_dir()
    with open(REPLIED_IDS_FILE, "a", encoding="utf-8") as f:
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

def utcnow():
    return datetime.now(timezone.utc)

def is_recent(dt: datetime, max_age_hours: int) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (utcnow() - dt) <= timedelta(hours=max_age_hours)

def engagement_score(m: dict) -> int:
    likes = m.get("like_count", 0)
    rts = m.get("retweet_count", 0)
    replies = m.get("reply_count", 0)
    quotes = m.get("quote_count", 0)
    # weight RTs + replies higher
    return likes + (3 * rts) + (2 * replies) + (2 * quotes)

def basic_safety_block(text: str) -> bool:
    t = text.lower()
    politics = ["election", "trump", "biden", "democrat", "republican", "congress", "senate"]
    if any(w in t for w in politics):
        return True
    harassment = ["kill yourself", "kys", "go die"]
    if any(w in t for w in harassment):
        return True
    return False

def looks_like_price_anchor(text: str) -> bool:
    t = text.lower()
    if any(k in t for k in ["btc", "bitcoin", "eth", "ethereum"]):
        pats = [
            r"\$\s*\d{2,3}\s*[kK]\b",            # $90k
            r"\b\d{2,3}\s*[kK]\b",               # 90k
            r"\$\s*\d{1,3}(?:,\d{3})+\b",        # $90,000
            r"\b\d{5,6}\b",                      # 90000
        ]
        return any(re.search(p, text) for p in pats)
    return False

def strip_quotes(text: str) -> str:
    return text.strip().strip('"').strip("'")

# ---------------- CLIENTS ----------------
client_x = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

def ai_client():
    return genai.Client(http_options=HttpOptions(api_version="v1"))

# ---------------- POST DECISION ----------------
tz = pytz.timezone(TIMEZONE)
now_local = datetime.now(tz)
hour = now_local.hour

event_name = os.getenv("GITHUB_EVENT_NAME", "")
is_manual = (event_name == "workflow_dispatch")

if not is_manual:
    if hour >= QUIET_START or hour < QUIET_END:
        print(f"SKIP: Quiet hours ({hour}:00 {TIMEZONE})")
        raise SystemExit(0)

    r = random.random()
    if r > POST_CHANCE:
        print(f"SKIP: Random skip r={r:.3f} > chance={POST_CHANCE}")
        raise SystemExit(0)

print("RUN: Proceeding")

# ---------------- AI: pick best tweet to reply to ----------------
def ai_pick_best_tweet(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None

    c = ai_client()
    finalists = candidates[:AI_FINALISTS]

    prompt = (
        "You are selecting ONE tweet to reply to for maximum engagement.\n"
        "Pick the tweet that is:\n"
        "- Recent\n"
        "- High engagement\n"
        "- Safe to reply to with light humor (no politics, no tragedy, no harassment)\n"
        "- Not too long / not too technical\n\n"
        "Return ONLY JSON: {\"best_index\": <int>, \"reason\": \"<short>\"}\n\n"
        "Tweets:\n"
    )

    for i, t in enumerate(finalists):
        created = t["created_at"].strftime("%Y-%m-%d %H:%M UTC") if t.get("created_at") else "unknown"
        prompt += (
            f"{i}. author={t.get('author','?')} created={created} score={t.get('score',0)}\n"
            f"   text={t.get('text','')}\n\n"
        )

    resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    raw = (resp.text or "").strip()

    try:
        data = json.loads(raw)
        idx = int(data.get("best_index", 0))
        if 0 <= idx < len(finalists):
            print("AI_PICK reason:", data.get("reason", ""))
            return finalists[idx]
    except Exception as e:
        print("AI_PICK: parse failed:", repr(e))

    return finalists[0]

def find_target_tweet() -> tuple[str, str] | None:
    if not TARGET_ACCOUNTS:
        print("CONFIG: TARGET_ACCOUNTS is empty. Set env TARGET_ACCOUNTS to comma-separated handles.")
        return None

    replied = load_replied_ids()
    pool = []

    for handle in TARGET_ACCOUNTS:
        query = f"from:{handle} -is:retweet -is:reply lang:en"
        try:
            resp = client_x.search_recent_tweets(
                query=query,
                max_results=min(PER_ACCOUNT_MAX, 100),
                tweet_fields=["public_metrics", "created_at"],
            )
            if not resp or not resp.data:
                continue

            for t in resp.data:
                tid = str(t.id)
                if tid in replied:
                    continue

                if not is_recent(t.created_at, MAX_TWEET_AGE_HOURS):
                    continue

                m = t.public_metrics or {}
                likes = m.get("like_count", 0)
                rts = m.get("retweet_count", 0)

                if likes < MIN_TARGET_LIKES or rts < MIN_TARGET_RTS:
                    continue

                txt = (getattr(t, "text", "") or "").strip()
                if len(txt) < 40:
                    continue

                pool.append({
                    "id": tid,
                    "text": txt[:500],
                    "created_at": t.created_at,
                    "metrics": m,
                    "score": engagement_score(m),
                    "author": handle
                })

        except Exception as e:
            print(f"SEARCH: failed for {handle}: {repr(e)}")
            continue

    if not pool:
        return None

    # Sort by engagement score (desc), then recency
    pool.sort(key=lambda x: (x["score"], x["created_at"]), reverse=True)
    pool = pool[:CANDIDATE_POOL_LIMIT]

    best = ai_pick_best_tweet(pool)
    if not best:
        return None

    print("AI picked tweet from:", best.get("author"), "score:", best.get("score"))
    return (best["id"], best["text"])

# ---------------- AI: generate reply ----------------
def generate_reply(tweet_text: str) -> str:
    c = ai_client()
    prompt = REPLY_PROMPT + tweet_text.strip()

    candidates = []
    for _ in range(5):
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = re.sub(r"\s+", " ", (resp.text or "").strip())
        text = strip_quotes(text)
        if text:
            candidates.append(text[:MAX_REPLY_CHARS])

    filtered = []
    for x in candidates:
        if basic_safety_block(x):
            continue
        if looks_like_price_anchor(x):
            continue
        if "@" in x:
            continue
        filtered.append(x)

    return filtered[0] if filtered else ""

def post_reply(tweet_id: str, reply_text: str):
    if not reply_text:
        print("REPLY: empty reply after filtering. Skipping.")
        raise SystemExit(0)

    try:
        client_x.create_tweet(
            text=reply_text,
            in_reply_to_tweet_id=tweet_id,
            user_auth=True
        )
        save_replied_id(tweet_id)
        print(f"REPLY: Posted ✅ to tweet {tweet_id}")
    except tweepy.Forbidden as e:
        r = getattr(e, "response", None)
        if r is not None:
            print("X STATUS:", r.status_code)
            print("X BODY:", r.text)
        else:
            print("X Forbidden 403:", str(e))
        raise SystemExit(0)

# ---------------- ORIGINAL: fallback post ----------------
def generate_original(avoid_texts: list[str]) -> str:
    c = ai_client()

    avoid_block = ""
    if avoid_texts:
        recent = avoid_texts[-8:]
        avoid_block = (
            "\nExtra rule:\n"
            "Avoid repeating the same premise as these recent posts:\n"
            + "\n".join([f"- {t}" for t in recent])
            + "\n"
        )

    prompt = ORIGINAL_PROMPT + avoid_block

    candidates = []
    for _ in range(5):
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        text = re.sub(r"\s+", " ", (resp.text or "").strip())
        text = strip_quotes(text)
        if text:
            candidates.append(text[:MAX_ORIGINAL_CHARS])

    filtered = []
    for x in candidates:
        if basic_safety_block(x):
            continue
        if looks_like_price_anchor(x):
            continue
        if "@" in x:
            continue
        filtered.append(x)

    return filtered[0] if filtered else ""

def post_original():
    day = today_key(TIMEZONE)
    data = load_daily_posts()
    todays = data.get(day, [])

    for attempt in range(4):
        text = generate_original([p["text"] for p in todays])
        if not text:
            print("ORIGINAL: empty after filtering, retrying...")
            continue

        h = text_hash(text)
        if any(p.get("hash") == h for p in todays):
            print(f"ORIGINAL: duplicate hash (attempt {attempt+1}), retrying...")
            continue

        print("ORIGINAL:", text)

        try:
            client_x.create_tweet(text=text, user_auth=True)
            print("ORIGINAL: Posted ✅")
            todays.append({"hash": h, "text": text, "ts": now_local.isoformat()})
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
            raise SystemExit(0)

    print("ORIGINAL: failed to produce a non-duplicate post. Skipping.")
    raise SystemExit(0)

# ---------------- MAIN ----------------
if random.random() < REPLY_CHANCE:
    target = find_target_tweet()
    if not target:
        print("REPLY: No suitable targets found. Falling back to original post.")
        post_original()
    else:
        tid, ttext = target
        reply = generate_reply(ttext)
        print("TARGET:", ttext[:220], "...")
        print("REPLY:", reply)
        post_reply(tid, reply)
else:
    post_original()
