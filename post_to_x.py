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
MAX_TWEET_AGE_HOURS = 24

TWEETS_PER_ACCOUNT = 8          # how many recent tweets to pull per account
AI_FINALISTS = 10               # how many candidates sent to AI picker
MIN_TARGET_LIKES = 3
MIN_TARGET_RTS = 0
AUTHOR_COOLDOWN_HOURS = 24

CANDIDATE_CACHE_MINUTES = 30

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

def author_key_from_username(username: str) -> str:
    u = (username or "").strip().lower()
    return f"user:{u}" if u else ""

def is_author_on_cooldown(state: dict, akey: str, cooldown_hours: int) -> bool:
    if not akey:
        return False
    author_map = state.get("author_last_replied", {}) or {}
    last = int(author_map.get(akey, 0) or 0)
    if last <= 0:
        return False
    return now_epoch() < (last + cooldown_hours * 3600)

def mark_author_replied(state: dict, akey: str):
    if not akey:
        return
    author_map = state.get("author_last_replied", {}) or {}
    author_map[akey] = now_epoch()
    state["author_last_replied"] = author_map

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

def clear_block(state: dict, key: str, reason: str):
    if key in state:
        print(f"UNBLOCK: clearing {key} ({reason}).")
        state.pop(key, None)
        save_state(state)

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

print("RUN: Proceeding (reply-only, timeline mode)")

# ---------------- AI: pick best tweet ----------------
def ai_pick_best_tweet(candidates: list[dict]) -> Optional[dict]:
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
        prompt += (
            f"{i}. author={t.get('author','?')} created={t.get('created_at_str','?')} "
            f"likes={t.get('likes',0)} rts={t.get('rts',0)} score={t.get('score',0)}\n"
            f"   text={t.get('text','')}\n\n"
        )

    try:
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw = (resp.text or "").strip()
        data = json.loads(raw)
        idx = int(data.get("best_index", 0))
        if 0 <= idx < len(finalists):
            print("AI_PICK reason:", data.get("reason", ""))
            return finalists[idx]
    except Exception as e:
        if is_gemini_429(e):
            print("GEMINI 429: pick step rate-limited. Falling back to top scored tweet.")
            return finalists[0]
        print("AI_PICK: failed, falling back:", repr(e))
        return finalists[0]

    return finalists[0]

# ---------------- FETCH VIA TIMELINES ----------------
def username_to_user_id(username: str) -> Optional[str]:
    try:
        resp = client_read.get_user(username=username)
        headers = getattr(resp, "headers", None)
        if not headers:
            inner = getattr(resp, "response", None)
            if inner is not None:
                headers = getattr(inner, "headers", None)
        print_rl(headers)
        if resp and resp.data and getattr(resp.data, "id", None):
            return str(resp.data.id)
    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("get_user failed:", repr(e), "status=", status)
        if headers:
            print_rl(headers)
        if body:
            print("get_user body:", body[:1200])
    return None

def fetch_recent_from_user(user_id: str) -> list[dict]:
    # timeline endpoint, not search
    try:
        resp = client_read.get_users_tweets(
            id=user_id,
            max_results=min(TWEETS_PER_ACCOUNT, 100),
            tweet_fields=["public_metrics", "created_at", "referenced_tweets"],
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
        return []
    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("timeline failed:", repr(e), "status=", status)
        if headers:
            print_rl(headers)
        if body:
            print("timeline body:", body[:1200])
        return []

# ---------------- FIND TARGET ----------------
def find_target_tweet() -> Optional[dict]:
    state = load_state()
    replied = load_replied_ids()

    # Manual run override for any previous block
    if is_manual and state.get("timeline_block_until"):
        clear_block(state, "timeline_block_until", "manual override")

    block_until = int(state.get("timeline_block_until", 0) or 0)
    if block_until and now_epoch() < block_until and not is_manual:
        print(f"RATE LIMIT GUARD: timeline blocked for {block_until - now_epoch()}s.")
        return None
    if block_until and now_epoch() >= block_until:
        clear_block(state, "timeline_block_until", "expired")

    # candidate cache
    cache_exp = int(state.get("candidate_cache_expires", 0) or 0)
    cached = state.get("candidate_cache", []) or []
    if cached and now_epoch() < cache_exp:
        cached = [c for c in cached if str(c.get("id", "")) not in replied]
        if cached:
            print(f"CACHE: Using {len(cached)} cached candidates.")
            cached.sort(key=lambda x: (x.get("score", 0), x.get("created_at_epoch", 0)), reverse=True)
            return ai_pick_best_tweet(cached)

    pool = []
    dropped = {
        "already_replied": 0,
        "blocked_user": 0,
        "author_cooldown": 0,
        "too_old": 0,
        "low_engagement": 0,
        "too_short": 0,
    }

    for uname in TARGET_ACCOUNTS:
        uname_l = uname.lower().strip()
        if uname_l in BLOCKED_USERNAMES:
            dropped["blocked_user"] += 1
            continue

        akey = author_key_from_username(uname_l)
        if is_author_on_cooldown(state, akey, AUTHOR_COOLDOWN_HOURS):
            dropped["author_cooldown"] += 1
            continue

        uid = username_to_user_id(uname)
        if not uid:
            continue

        tweets = fetch_recent_from_user(uid)

        for t in tweets:
            tid = t["id"]
            if tid in replied:
                dropped["already_replied"] += 1
                continue

            # recency
            created_epoch = t.get("created_at_epoch", 0)
            created_dt = datetime.fromtimestamp(created_epoch, tz=timezone.utc) if created_epoch else None
            if not is_recent(created_dt, MAX_TWEET_AGE_HOURS):
                dropped["too_old"] += 1
                continue

            # engagement
            likes = t.get("likes", 0)
            rts = t.get("rts", 0)
            if likes < MIN_TARGET_LIKES or rts < MIN_TARGET_RTS:
                dropped["low_engagement"] += 1
                continue

            # text length
            txt = (t.get("text") or "").strip()
            if len(txt) < 40:
                dropped["too_short"] += 1
                continue

            t["author"] = uname
            t["author_key"] = akey
            pool.append(t)

    print("DEBUG: dropped counts =", dropped)
    print("DEBUG: candidate pool size =", len(pool))

    if not pool:
        return None

    pool.sort(key=lambda x: (x.get("score", 0), x.get("created_at_epoch", 0)), reverse=True)

    # cache candidates
    state["candidate_cache"] = pool[:50]
    state["candidate_cache_expires"] = now_epoch() + CANDIDATE_CACHE_MINUTES * 60
    save_state(state)

    return ai_pick_best_tweet(pool)

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
                print("GEMINI 429: reply generation rate-limited. Skipping this run cleanly.")
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

def post_reply(tweet_id: str, reply_text: str, author_key: str = ""):
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
        mark_author_replied(state, author_key)
        save_state(state)

        print(f"REPLY: Posted ✅ to tweet {tweet_id}")

    except tweepy.TooManyRequests as e:
        status, body, headers = get_status_body_headers(e)
        print("WRITE TooManyRequests status =", status)
        if headers:
            print_rl(headers)
        if body:
            print("WRITE ERROR BODY:", body[:1200])
        raise SystemExit(0)

    except tweepy.Forbidden as e:
        status, body, headers = get_status_body_headers(e)
        print("X Forbidden status =", status)
        if body:
            print("X BODY:", body[:1200])
        raise SystemExit(0)

    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("WRITE: failed:", repr(e), "status=", status)
        if body:
            print("WRITE ERROR BODY:", body[:1200])
        raise SystemExit(0)

# ---------------- MAIN ----------------
best = find_target_tweet()
if not best:
    print("SKIP: No suitable target tweets found (timeline mode).")
    raise SystemExit(0)

tid = best["id"]
ttext = best["text"]
author_key = best.get("author_key", "")

reply = generate_reply(ttext)
print("TARGET:", ttext[:220], "...")
print("REPLY:", reply)

post_reply(tid, reply, author_key)
