import os
import random
from datetime import datetime, timezone, timedelta
import json
import pytz
import re
from typing import Optional

import tweepy
from google import genai
from google.genai.types import HttpOptions

# ---------------- SETTINGS ----------------
TIMEZONE = "US/Mountain"
QUIET_START = 23
QUIET_END = 6

RUN_CHANCE = 1.00  # scheduled runs only

MAX_REPLY_CHARS = 200
MAX_TWEET_AGE_HOURS = 48  # give yourself more options

TWEETS_PER_ACCOUNT = 10        # keep small to reduce payload
AI_FINALISTS = 8

MIN_TARGET_LIKES = 1           # lower a bit so it finds something
MIN_TARGET_RTS = 0
AUTHOR_COOLDOWN_HOURS = 24

BLOCKED_USERNAMES = {"watcherguru", "watcher.guru", "watcher_guru"}
TARGET_ACCOUNTS = [a.strip().lstrip("@") for a in os.getenv("TARGET_ACCOUNTS", "").split(",") if a.strip()]

# ---------------- STATE ----------------
STATE_DIR = ".bot_state"
REPLIED_IDS_FILE = os.path.join(STATE_DIR, "replied_ids.txt")
STATE_JSON_FILE = os.path.join(STATE_DIR, "state.json")

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

def load_state() -> dict:
    ensure_state_dir()
    if not os.path.exists(STATE_JSON_FILE):
        return {}
    try:
        with open(STATE_JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("WARN: state.json unreadable:", repr(e))
        return {}

def save_state(state: dict):
    ensure_state_dir()
    try:
        with open(STATE_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("WARN: state.json write failed:", repr(e))

def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def utcnow():
    return datetime.now(timezone.utc)

def is_recent(dt: Optional[datetime], max_age_hours: int) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (utcnow() - dt) <= timedelta(hours=max_age_hours)

def to_epoch(dt: Optional[datetime]) -> int:
    if not dt:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

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

def is_gemini_429(e: Exception) -> bool:
    msg = str(e).lower()
    return ("resource_exhausted" in msg) or ("429" in msg) or ("quota" in msg) or ("rate" in msg)

def print_rl(headers):
    if not headers:
        return
    print("RL:", {
        "limit": headers.get("x-rate-limit-limit"),
        "remaining": headers.get("x-rate-limit-remaining"),
        "reset": headers.get("x-rate-limit-reset"),
    })

def get_status_body_headers(e: Exception):
    r = getattr(e, "response", None)
    status = getattr(r, "status_code", None) if r is not None else None
    body = getattr(r, "text", None) if r is not None else None
    headers = getattr(r, "headers", None) if r is not None else None
    return status, body, headers

def author_key(username: str) -> str:
    u = (username or "").strip().lower()
    return f"user:{u}" if u else ""

def is_author_on_cooldown(state: dict, akey: str, cooldown_hours: int) -> bool:
    if not akey:
        return False
    m = state.get("author_last_replied", {}) or {}
    last = int(m.get(akey, 0) or 0)
    if last <= 0:
        return False
    return now_epoch() < (last + cooldown_hours * 3600)

def mark_author_replied(state: dict, akey: str):
    if not akey:
        return
    m = state.get("author_last_replied", {}) or {}
    m[akey] = now_epoch()
    state["author_last_replied"] = m

# ---------------- X CLIENTS ----------------
client_read = tweepy.Client(
    bearer_token=os.getenv("X_BEARER_TOKEN"),
    wait_on_rate_limit=False
)

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

print("DEBUG: event =", event_name, "manual =", is_manual, "local_hour =", hour, TIMEZONE)
print("DEBUG: TARGET_ACCOUNTS =", TARGET_ACCOUNTS)

bt = os.getenv("X_BEARER_TOKEN") or ""
print("DEBUG: X_BEARER_TOKEN present =", bool(bt), "len =", len(bt))

print("DEBUG: GOOGLE_GENAI_USE_VERTEXAI =", (os.getenv("GOOGLE_GENAI_USE_VERTEXAI") or "").lower())
print("DEBUG: GOOGLE_CLOUD_PROJECT =", os.getenv("GOOGLE_CLOUD_PROJECT") or "")
print("DEBUG: GOOGLE_CLOUD_LOCATION =", os.getenv("GOOGLE_CLOUD_LOCATION") or "")
print("DEBUG: GOOGLE_APPLICATION_CREDENTIALS =", os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "")

if not TARGET_ACCOUNTS:
    print("SKIP: TARGET_ACCOUNTS is empty.")
    raise SystemExit(0)

if not bt:
    print("SKIP: X_BEARER_TOKEN missing.")
    raise SystemExit(0)

if not is_manual:
    if hour >= QUIET_START or hour < QUIET_END:
        print(f"SKIP: Quiet hours ({hour}:00 {TIMEZONE})")
        raise SystemExit(0)
    if random.random() > RUN_CHANCE:
        print("SKIP: Random skip.")
        raise SystemExit(0)

print("RUN: Proceeding (reply-only, low-call mode)")

# ---------------- AI: pick best tweet ----------------
def ai_pick_best(candidates: list[dict]) -> Optional[dict]:
    if not candidates:
        return None
    finalists = candidates[:AI_FINALISTS]
    prompt = (
        "Pick ONE tweet to reply to for maximum engagement.\n"
        "Avoid politics/tragedy/harassment. Prefer short, relatable.\n"
        "Return ONLY JSON: {\"best_index\": <int>}.\n\nTweets:\n"
    )
    for i, t in enumerate(finalists):
        prompt += (
            f"{i}. author={t.get('author')} created={t.get('created_at_str')} "
            f"likes={t.get('likes',0)} rts={t.get('rts',0)} score={t.get('score',0)}\n"
            f"   text={t.get('text','')}\n\n"
        )
    try:
        c = ai_client()
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw = (resp.text or "").strip()
        idx = int(json.loads(raw).get("best_index", 0))
        if 0 <= idx < len(finalists):
            return finalists[idx]
    except Exception as e:
        if is_gemini_429(e):
            print("GEMINI 429 on pick, falling back to top.")
            return finalists[0]
        print("AI pick failed:", repr(e), "falling back.")
        return finalists[0]
    return finalists[0]

# ---------------- X: cached user ids + single account per run ----------------
def get_user_id_cached(state: dict, username: str) -> Optional[str]:
    cache = state.get("user_id_cache", {}) or {}
    key = username.lower().strip()
    if key in cache:
        return cache[key]

    # Only attempt resolving if not cached (saves rate limit)
    try:
        resp = client_read.get_user(username=username)
        headers = getattr(resp, "headers", None)
        if not headers:
            inner = getattr(resp, "response", None)
            if inner is not None:
                headers = getattr(inner, "headers", None)
        print_rl(headers)

        if resp and resp.data and getattr(resp.data, "id", None):
            uid = str(resp.data.id)
            cache[key] = uid
            state["user_id_cache"] = cache
            save_state(state)
            print(f"CACHE: saved user_id for @{username} => {uid}")
            return uid

    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print(f"get_user failed for @{username} status={status}")
        if headers:
            print_rl(headers)
        if body:
            print("get_user body:", body[:1000])
        return None

    return None

def fetch_timeline(user_id: str) -> list[dict]:
    try:
        resp = client_read.get_users_tweets(
            id=user_id,
            max_results=min(TWEETS_PER_ACCOUNT, 100),
            tweet_fields=["public_metrics", "created_at"],
            exclude=["retweets", "replies"],
        )
        headers = getattr(resp, "headers", None)
        if not headers:
            inner = getattr(resp, "response", None)
            if inner is not None:
                headers = getattr(inner, "headers", None)
        print_rl(headers)

        out = []
        if not resp or not resp.data:
            return out

        for t in resp.data:
            txt = (getattr(t, "text", "") or "").strip()
            created = getattr(t, "created_at", None)
            m = getattr(t, "public_metrics", None) or {}
            out.append({
                "id": str(t.id),
                "text": txt[:500],
                "created_at_epoch": to_epoch(created),
                "created_at_str": created.strftime("%Y-%m-%d %H:%M UTC") if created else "unknown",
                "metrics": m,
                "likes": m.get("like_count", 0),
                "rts": m.get("retweet_count", 0),
                "score": engagement_score(m),
            })
        return out

    except tweepy.TooManyRequests as e:
        status, body, headers = get_status_body_headers(e)
        print("timeline TooManyRequests status=", status)
        if headers:
            print_rl(headers)

        # If 429, store a short block until reset (scheduled runs only)
        if status == 429 and headers:
            try:
                reset = int(headers.get("x-rate-limit-reset", "0"))
            except Exception:
                reset = 0
            if reset <= now_epoch():
                reset = now_epoch() + 15 * 60
            state = load_state()
            jitter = random.randint(5, 30)
            state["timeline_block_until"] = reset + jitter
            save_state(state)
            print(f"RATE LIMIT: blocking timeline until {state['timeline_block_until']}")
        return []

    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("timeline failed:", repr(e), "status=", status)
        if headers:
            print_rl(headers)
        if body:
            print("timeline body:", body[:1000])
        return []

def choose_one_account(state: dict) -> str:
    # rotate accounts to spread calls
    idx = int(state.get("acct_idx", 0) or 0)
    accounts = [a for a in TARGET_ACCOUNTS if a.lower() not in BLOCKED_USERNAMES]
    if not accounts:
        return ""
    pick = accounts[idx % len(accounts)]
    state["acct_idx"] = (idx + 1) % len(accounts)
    save_state(state)
    return pick

# ---------------- FIND TARGET ----------------
def find_target() -> Optional[dict]:
    state = load_state()
    replied = load_replied_ids()

    # manual override for timeline block
    bu = int(state.get("timeline_block_until", 0) or 0)
    if bu and now_epoch() < bu and not is_manual:
        print(f"RATE LIMIT GUARD: timeline blocked for {bu - now_epoch()}s.")
        return None
    if bu and now_epoch() >= bu:
        state.pop("timeline_block_until", None)
        save_state(state)

    uname = choose_one_account(state)
    if not uname:
        print("SKIP: no valid accounts.")
        return None

    akey = author_key(uname)
    if is_author_on_cooldown(state, akey, AUTHOR_COOLDOWN_HOURS):
        print(f"SKIP: author cooldown @{uname}")
        return None

    uid = get_user_id_cached(state, uname)
    if not uid:
        print(f"SKIP: could not resolve user id for @{uname}")
        return None

    tweets = fetch_timeline(uid)
    pool = []

    for t in tweets:
        tid = t["id"]
        if tid in replied:
            continue

        created_epoch = t.get("created_at_epoch", 0)
        created_dt = datetime.fromtimestamp(created_epoch, tz=timezone.utc) if created_epoch else None
        if not is_recent(created_dt, MAX_TWEET_AGE_HOURS):
            continue

        likes = t.get("likes", 0)
        rts = t.get("rts", 0)
        if likes < MIN_TARGET_LIKES or rts < MIN_TARGET_RTS:
            continue

        txt = (t.get("text") or "").strip()
        if len(txt) < 40:
            continue

        t["author"] = uname
        t["author_key"] = akey
        pool.append(t)

    print("DEBUG: chosen account =", uname)
    print("DEBUG: candidate pool size =", len(pool))

    if not pool:
        return None

    pool.sort(key=lambda x: (x.get("score", 0), x.get("created_at_epoch", 0)), reverse=True)
    return ai_pick_best(pool)

# ---------------- AI: generate reply ----------------
def generate_reply(tweet_text: str) -> str:
    c = ai_client()
    prompt = REPLY_PROMPT + tweet_text.strip()

    candidates = []
    for _ in range(3):
        try:
            resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        except Exception as e:
            if is_gemini_429(e):
                print("GEMINI 429: reply generation rate-limited. Skipping.")
                return ""
            print("GEMINI ERROR:", repr(e))
            return ""

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

def post_reply(tweet_id: str, reply_text: str, author_key_str: str = ""):
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

        state = load_state()
        mark_author_replied(state, author_key_str)
        save_state(state)

        print(f"REPLY: Posted ✅ to tweet {tweet_id}")

    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("WRITE failed:", repr(e), "status=", status)
        if headers:
            print_rl(headers)
        if body:
            print("WRITE BODY:", body[:1200])
        raise SystemExit(0)

# ---------------- MAIN ----------------
best = find_target()
if not best:
    print("SKIP: No suitable target tweets found (low-call mode).")
    raise SystemExit(0)

tid = best["id"]
ttext = best["text"]
akey = best.get("author_key", "")

reply = generate_reply(ttext)
print("TARGET:", ttext[:220], "...")
print("REPLY:", reply)

post_reply(tid, reply, akey)
