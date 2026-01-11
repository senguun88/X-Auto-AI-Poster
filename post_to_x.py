import os
import random
from datetime import datetime, timezone, timedelta
import json
import pytz
import re

import tweepy
from google import genai
from google.genai.types import HttpOptions

# ============================================================
#  REPLY-ONLY AI BOT (NEVER POSTS ORIGINAL TWEETS)
#
#  Optimized for X rate limits:
#   - Fewer searches per run
#   - Smaller candidate pool
#   - Exits cleanly on rate limit (no 900s sleep)
# ============================================================

# ---------------- SETTINGS ----------------
TIMEZONE = "US/Mountain"
QUIET_START = 23
QUIET_END = 6

RUN_CHANCE = 1.00

MAX_REPLY_CHARS = 200
MAX_TWEET_AGE_HOURS = 24

# ↓ Lower = fewer API calls / less load
PER_ACCOUNT_MAX = 10          # was 50
CANDIDATE_POOL_LIMIT = 30     # was 80
AI_FINALISTS = 10             # was 12

MIN_TARGET_LIKES = 3          # loosened slightly to find more targets
MIN_TARGET_RTS = 0

TARGET_ACCOUNTS = [a.strip().lstrip("@") for a in os.getenv("TARGET_ACCOUNTS", "").split(",") if a.strip()]

# ---------------- STATE ----------------
STATE_DIR = ".bot_state"
REPLIED_IDS_FILE = os.path.join(STATE_DIR, "replied_ids.txt")

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
            r"\$\s*\d{2,3}\s*[kK]\b",
            r"\b\d{2,3}\s*[kK]\b",
            r"\$\s*\d{1,3}(?:,\d{3})+\b",
            r"\b\d{5,6}\b",
        ]
        return any(re.search(p, text) for p in pats)
    return False

def strip_quotes(text: str) -> str:
    return text.strip().strip('"').strip("'")

def ai_client():
    return genai.Client(http_options=HttpOptions(api_version="v1"))

# ---------------- X CLIENTS ----------------
# READ/search client
client_read = tweepy.Client(
    bearer_token=os.getenv("X_BEARER_TOKEN"),
    wait_on_rate_limit=False  # IMPORTANT: don't sleep 900s in Actions
)

# WRITE/reply client
client_write = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

# ---------------- RUN DECISION ----------------
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
    if r > RUN_CHANCE:
        print(f"SKIP: Random skip r={r:.3f} > chance={RUN_CHANCE}")
        raise SystemExit(0)

print("RUN: Proceeding (reply-only)")
print("DEBUG: TARGET_ACCOUNTS =", TARGET_ACCOUNTS)
bt = os.getenv("X_BEARER_TOKEN") or ""
print("DEBUG: X_BEARER_TOKEN present =", bool(bt), "len =", len(bt))

# ---------------- AI: pick best tweet ----------------
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

def find_target_tweet() -> dict | None:
    if not TARGET_ACCOUNTS:
        print("SKIP: TARGET_ACCOUNTS is empty. Set it in GitHub Actions env.")
        return None

    replied = load_replied_ids()
    pool = []

    for handle in TARGET_ACCOUNTS:
        query = f"from:{handle} -is:retweet -is:reply lang:en"
        try:
            resp = client_read.search_recent_tweets(
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

        except tweepy.TooManyRequests:
            print("RATE LIMIT: X search rate limit hit. Skipping this run.")
            return None
        except Exception as e:
            print(f"SEARCH: failed for {handle}: {repr(e)}")
            continue

    print("DEBUG: candidate pool size =", len(pool))
    if not pool:
        return None

    pool.sort(key=lambda x: (x["score"], x["created_at"]), reverse=True)
    pool = pool[:CANDIDATE_POOL_LIMIT]

    return ai_pick_best_tweet(pool)

# ---------------- AI: generate reply ----------------
def generate_reply(tweet_text: str) -> str:
    c = ai_client()
    prompt = REPLY_PROMPT + tweet_text.strip()

    candidates = []
    for _ in range(6):
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
        print("SKIP: empty reply after filtering.")
        raise SystemExit(0)

    try:
        client_write.create_tweet(
            text=reply_text,
            in_reply_to_tweet_id=tweet_id,
            user_auth=True
        )
        save_replied_id(tweet_id)
        print(f"REPLY: Posted ✅ to tweet {tweet_id}")
    except tweepy.TooManyRequests:
        print("RATE LIMIT: X write rate limit hit. Skipping.")
        raise SystemExit(0)
    except tweepy.Forbidden as e:
        r = getattr(e, "response", None)
        if r is not None:
            print("X STATUS:", r.status_code)
            print("X BODY:", r.text)
        else:
            print("X Forbidden 403:", str(e))
        raise SystemExit(0)

# ---------------- MAIN (REPLY ONLY) ----------------
best = find_target_tweet()
if not best:
    print("SKIP: No suitable target tweets found (reply-only mode).")
    raise SystemExit(0)

tid = best["id"]
ttext = best["text"]

reply = generate_reply(ttext)
print("TARGET:", ttext[:220], "...")
print("REPLY:", reply)

post_reply(tid, reply)
