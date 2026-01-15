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
#  FIXES INCLUDED (copy/paste ready):
#   ✅ Manual runs OVERRIDE rate-limit guard (so it won't get stuck)
#   ✅ Auto-clears expired search_block_until
#   ✅ Candidate cache is JSON-safe (no datetime objects)
#   ✅ Better DEBUG logging + better error bodies
#   ✅ Only treats TooManyRequests as rate limit if status==429
# ============================================================

# ---------------- SETTINGS ----------------
TIMEZONE = "US/Mountain"
QUIET_START = 23
QUIET_END = 6

RUN_CHANCE = 1.00  # scheduled runs only (manual always runs)

MAX_REPLY_CHARS = 200
MAX_TWEET_AGE_HOURS = 24

CANDIDATE_POOL_LIMIT = 30
AI_FINALISTS = 10

MIN_TARGET_LIKES = 3
MIN_TARGET_RTS = 0

AUTHOR_COOLDOWN_HOURS = 24

# Candidate cache (helps manual reruns not hammer search)
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

def build_multi_from_query(handles: list[str]) -> str:
    parts = [f"from:{h}" for h in handles if h]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"

def is_gemini_429(e: Exception) -> bool:
    msg = str(e).lower()
    return ("resource_exhausted" in msg) or ("429" in msg) or ("quota" in msg) or ("rate" in msg)

def author_key_from_user(user: dict) -> str:
    if not user:
        return ""
    uid = str(user.get("id") or "").strip()
    if uid:
        return f"id:{uid}"
    uname = str(user.get("username") or "").strip().lower()
    if uname:
        return f"user:{uname}"
    return ""

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

def print_rl_headers_from_headers(headers):
    if not headers:
        return
    print("RL:", {
        "limit": headers.get("x-rate-limit-limit"),
        "remaining": headers.get("x-rate-limit-remaining"),
        "reset": headers.get("x-rate-limit-reset"),
    })

def to_epoch(dt: datetime | None) -> int:
    if not dt:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

def get_status_body_headers(e: Exception):
    r = getattr(e, "response", None)
    status = getattr(r, "status_code", None) if r is not None else None
    body = getattr(r, "text", None) if r is not None else None
    headers = getattr(r, "headers", None) if r is not None else None
    return status, body, headers

def clear_search_block(state: dict, reason: str):
    if "search_block_until" in state:
        print(f"UNBLOCK: clearing search_block_until ({reason}).")
        state.pop("search_block_until", None)
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

    r = random.random()
    if r > RUN_CHANCE:
        print(f"SKIP: Random skip r={r:.3f} > chance={RUN_CHANCE}")
        raise SystemExit(0)

print("RUN: Proceeding (reply-only)")

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
        created = t.get("created_at_str") or "unknown"
        prompt += (
            f"{i}. author={t.get('author','?')} created={created} score={t.get('score',0)}\n"
            f"   text={t.get('text','')}\n\n"
        )

    try:
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw = (resp.text or "").strip()
    except Exception as e:
        if is_gemini_429(e):
            print("GEMINI 429: pick step rate-limited. Falling back to top scored tweet.")
            return finalists[0]
        print("AI_PICK: Gemini call failed:", repr(e))
        return finalists[0]

    try:
        data = json.loads(raw)
        idx = int(data.get("best_index", 0))
        if 0 <= idx < len(finalists):
            print("AI_PICK reason:", data.get("reason", ""))
            return finalists[idx]
    except Exception as e:
        print("AI_PICK: parse failed:", repr(e))

    return finalists[0]

# ---------------- FIND TARGET ----------------
def find_target_tweet() -> dict | None:
    state = load_state()
    replied = load_replied_ids()

    # Clear expired block
    bu = int(state.get("search_block_until", 0) or 0)
    if bu and now_epoch() >= bu:
        clear_search_block(state, "expired")

    # Rate-limit guard (manual run overrides)
    block_until = int(state.get("search_block_until", 0) or 0)
    if block_until and now_epoch() < block_until:
        wait_s = block_until - now_epoch()
        print(f"RATE LIMIT GUARD: blocked for {wait_s}s (until epoch={block_until}).")
        if is_manual:
            clear_search_block(state, "manual run override")
        else:
            return None

    # Candidate cache (JSON-safe)
    cache_exp = int(state.get("candidate_cache_expires", 0) or 0)
    cached = state.get("candidate_cache", []) or []
    if cached and now_epoch() < cache_exp:
        cached = [c for c in cached if str(c.get("id", "")) not in replied]
        if cached:
            print(f"CACHE: Using {len(cached)} cached candidates (expires in {cache_exp - now_epoch()}s).")
            cached.sort(key=lambda x: (x.get("score", 0), x.get("created_at_epoch", 0)), reverse=True)
            return ai_pick_best_tweet(cached)

    pool = []
    from_query = build_multi_from_query(TARGET_ACCOUNTS)
    query = f"{from_query} -is:retweet -is:reply lang:en"
    max_results = min(CANDIDATE_POOL_LIMIT, 100)

    print("DEBUG: search query =", query)
    print("DEBUG: max_results =", max_results)
    print("DEBUG: filters => MIN_LIKES", MIN_TARGET_LIKES, "MAX_AGE_HOURS", MAX_TWEET_AGE_HOURS, "MIN_TEXT_LEN 40")

    try:
        resp = client_read.search_recent_tweets(
            query=query,
            max_results=max_results,
            tweet_fields=["public_metrics", "created_at", "author_id"],
            expansions=["author_id"],
            user_fields=["username"],
        )

        headers = getattr(resp, "headers", None)
        if not headers:
            inner = getattr(resp, "response", None)
            if inner is not None:
                headers = getattr(inner, "headers", None)
        print_rl_headers_from_headers(headers)

        if not resp or not resp.data:
            print("DEBUG: search returned no tweets.")
            return None

        # includes users
        users_by_id = {}
        includes = getattr(resp, "includes", None)
        if includes and isinstance(includes, dict) and includes.get("users"):
            for u in includes["users"]:
                if hasattr(u, "data") and isinstance(u.data, dict):
                    ud = u.data
                elif isinstance(u, dict):
                    ud = u
                else:
                    ud = {"id": getattr(u, "id", None), "username": getattr(u, "username", None)}
                if ud.get("id") is not None:
                    users_by_id[str(ud["id"])] = ud

        dropped = {
            "already_replied": 0,
            "blocked_user": 0,
            "author_cooldown": 0,
            "too_old": 0,
            "low_engagement": 0,
            "too_short": 0,
        }

        for t in resp.data:
            tid = str(t.id)
            if tid in replied:
                dropped["already_replied"] += 1
                continue

            author_id = str(getattr(t, "author_id", "") or "")
            user = users_by_id.get(author_id, {})
            username = (user.get("username") or "").lower().strip()

            if username in BLOCKED_USERNAMES:
                dropped["blocked_user"] += 1
                continue

            akey = author_key_from_user(user) or (f"id:{author_id}" if author_id else "")
            if is_author_on_cooldown(state, akey, AUTHOR_COOLDOWN_HOURS):
                dropped["author_cooldown"] += 1
                continue

            if not is_recent(t.created_at, MAX_TWEET_AGE_HOURS):
                dropped["too_old"] += 1
                continue

            m = t.public_metrics or {}
            likes = m.get("like_count", 0)
            rts = m.get("retweet_count", 0)
            if likes < MIN_TARGET_LIKES or rts < MIN_TARGET_RTS:
                dropped["low_engagement"] += 1
                continue

            txt = (getattr(t, "text", "") or "").strip()
            if len(txt) < 40:
                dropped["too_short"] += 1
                continue

            created_epoch = to_epoch(t.created_at)
            created_str = t.created_at.strftime("%Y-%m-%d %H:%M UTC") if t.created_at else "unknown"

            pool.append({
                "id": tid,
                "text": txt[:500],
                "created_at_epoch": created_epoch,
                "created_at_str": created_str,
                "metrics": m,
                "score": engagement_score(m),
                "author": (user.get("username") or author_id or "unknown"),
                "author_key": akey
            })

        print("DEBUG: dropped counts =", dropped)
        print("DEBUG: candidate pool size =", len(pool))

    except tweepy.TooManyRequests as e:
        status, body, headers = get_status_body_headers(e)
        print("ERROR: Tweepy TooManyRequests status =", status)
        if headers:
            print_rl_headers_from_headers(headers)

        if status == 429:
            reset = 0
            try:
                reset = int(headers.get("x-rate-limit-reset", "0")) if headers else 0
            except Exception:
                reset = 0
            if reset <= now_epoch():
                reset = now_epoch() + 15 * 60
            jitter = random.randint(10, 90)
            state["search_block_until"] = reset + jitter
            save_state(state)
            print(f"RATE LIMIT (429): Blocking until epoch={state['search_block_until']}. Skipping this run.")
            return None

        if body:
            print("NON-429 ERROR BODY:", body[:2000])
        return None

    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("SEARCH: failed:", repr(e), "status=", status)
        if headers:
            print_rl_headers_from_headers(headers)
        if body:
            print("SEARCH ERROR BODY:", body[:2000])
        return None

    if not pool:
        return None

    pool.sort(key=lambda x: (x["score"], x["created_at_epoch"]), reverse=True)
    pool = pool[:CANDIDATE_POOL_LIMIT]

    state["candidate_cache"] = pool
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
            print_rl_headers_from_headers(headers)
        if body:
            print("WRITE ERROR BODY:", body[:2000])
        raise SystemExit(0)

    except tweepy.Forbidden as e:
        status, body, headers = get_status_body_headers(e)
        print("X Forbidden status =", status)
        if body:
            print("X BODY:", body[:2000])
        raise SystemExit(0)

    except Exception as e:
        status, body, headers = get_status_body_headers(e)
        print("WRITE: failed:", repr(e), "status=", status)
        if body:
            print("WRITE ERROR BODY:", body[:2000])
        raise SystemExit(0)

# ---------------- MAIN ----------------
best = find_target_tweet()
if not best:
    print("SKIP: No suitable target tweets found (reply-only mode).")
    raise SystemExit(0)

tid = best["id"]
ttext = best["text"]
author_key = best.get("author_key", "")

reply = generate_reply(ttext)
print("TARGET:", ttext[:220], "...")
print("REPLY:", reply)

post_reply(tid, reply, author_key)
