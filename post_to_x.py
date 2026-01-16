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
#  Rate-limit safe:
#   - ONE search call per run (OR query across accounts)
#   - Persists rate-limit reset in .bot_state/state.json
#   - Exits cleanly on rate limit (no 900s sleep)
# ============================================================

# ---------------- SETTINGS ----------------
TIMEZONE = "US/Mountain"
QUIET_START = 23
QUIET_END = 6

RUN_CHANCE = 1.00  # consider 0.30–0.50 if you schedule many runs/day

MAX_REPLY_CHARS = 200
MAX_TWEET_AGE_HOURS = 24

CANDIDATE_POOL_LIMIT = 30
AI_FINALISTS = 10

MIN_TARGET_LIKES = 3
MIN_TARGET_RTS = 0

# Max 1 reply per author per cooldown window
AUTHOR_COOLDOWN_HOURS = 24

# Hard-block these authors entirely
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
    except Exception:
        return {}

def save_state(state: dict):
    ensure_state_dir()
    with open(STATE_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

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

# --- Gemini 429 helper ---
def is_gemini_429(e: Exception) -> bool:
    msg = str(e).lower()
    return ("resource_exhausted" in msg) or ("429" in msg) or ("quota" in msg) or ("rate" in msg)

# --- Author cooldown helpers ---
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

# ---------------- X CLIENTS ----------------
client_read = tweepy.Client(
    bearer_token=os.getenv("X_BEARER_TOKEN"),
    wait_on_rate_limit=False  # IMPORTANT: don't sleep 900s in Actions
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

# ---------------- FIND TARGET (ONE SEARCH + RL GUARD) ----------------
def find_target_tweet() -> dict | None:
    if not TARGET_ACCOUNTS:
        print("SKIP: TARGET_ACCOUNTS is empty. Set it in GitHub Actions env.")
        return None

    # Rate-limit guard: if we previously hit 429, skip until reset
    state = load_state()
    block_until = int(state.get("search_block_until", 0) or 0)
    if now_epoch() < block_until:
        wait_s = block_until - now_epoch()
        print(f"RATE LIMIT GUARD: Skipping search (blocked for {wait_s}s).")
        return None

    replied = load_replied_ids()
    pool = []

    from_query = build_multi_from_query(TARGET_ACCOUNTS)
    query = f"{from_query} -is:retweet -is:reply lang:en"
    max_results = min(CANDIDATE_POOL_LIMIT, 100)

    try:
        resp = client_read.search_recent_tweets(
            query=query,
            max_results=max_results,
            tweet_fields=["public_metrics", "created_at", "author_id"],
            expansions=["author_id"],
            user_fields=["username"],
        )

        headers = getattr(resp, "headers", None)
        if headers:
            print("RL:", {
                "limit": headers.get("x-rate-limit-limit"),
                "remaining": headers.get("x-rate-limit-remaining"),
                "reset": headers.get("x-rate-limit-reset"),
            })

        if not resp or not resp.data:
            return None

        # Build author_id -> user dict map
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

        for t in resp.data:
            tid = str(t.id)
            if tid in replied:
                continue

            author_id = str(getattr(t, "author_id", "") or "")
            user = users_by_id.get(author_id, {})
            username = (user.get("username") or "").lower().strip()

            # Hard blocklist (stop replying to WatcherGuru etc.)
            if username in BLOCKED_USERNAMES:
                continue

            akey = author_key_from_user(user) or (f"id:{author_id}" if author_id else "")
            if is_author_on_cooldown(state, akey, AUTHOR_COOLDOWN_HOURS):
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
                "author": (user.get("username") or author_id or "unknown"),
                "author_key": akey
            })

    except tweepy.TooManyRequests as e:
        # Persist reset time to stop hammering the endpoint on scheduled runs
        reset = 0
        try:
            reset = int(getattr(e, "response", None).headers.get("x-rate-limit-reset", "0"))
        except Exception:
            reset = 0

        if reset <= now_epoch():
            reset = now_epoch() + 15 * 60  # fallback

        state["search_block_until"] = reset + 2
        save_state(state)

        print(f"RATE LIMIT: X search hit. Blocking until epoch={state['search_block_until']}. Skipping this run.")
        return None

    except Exception as e:
        print("SEARCH: failed:", repr(e))
        return None

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
    for _ in range(3):  # reduced from 6 to lower chance of Gemini 429
        try:
            resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        except Exception as e:
            if is_gemini_429(e):
                print("GEMINI 429: reply generation rate-limited. Skipping this run cleanly.")
                return ""
            raise

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

        # Record author cooldown
        state = load_state()
        mark_author_replied(state, author_key)
        save_state(state)

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
author_key = best.get("author_key", "")

reply = generate_reply(ttext)
print("TARGET:", ttext[:220], "...")
print("REPLY:", reply)

post_reply(tid, reply, author_key)
