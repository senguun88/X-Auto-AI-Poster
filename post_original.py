import os
import random
import json
import hashlib
import re
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

# How many recent posts to remember (used to avoid same vibes)
RECENT_POSTS_TO_REMEMBER = 20

# If a post contains too many of these, we treat it as "generic" and retry
GENERIC_PHRASES = [
    "closely tracking",
    "macroeconomic factors",
    "broader market direction",
    "remain a primary influence",
    "continue to monitor",
    "data-dependent",
    "central bank policy",
    "risk assets",
    "global economic indicators",
    "market participants",
]

# Topic + format rotation to prevent same voice every time
TOPICS = [
    "crypto market structure (liquidity, order books, ETFs, flows)",
    "macro basics in plain English (rates, inflation, jobs data) — but make it fresh",
    "trading psychology (FOMO, chop, risk management, position sizing)",
    "common crypto scams and how to spot them",
    "on-chain signals explained simply (without numbers)",
    "tech + AI + markets crossover (how narratives spread)",
    "productivity/discipline lessons from trading (habits, focus, rules)",
    "internet culture + investing behavior (attention, outrage, confirmation bias)",
]

FORMATS = [
    "Hot take (one strong opinion + one reason).",
    "Myth vs fact (1 myth sentence, 1 fact sentence).",
    "Tiny story (1 short scenario + 1 lesson).",
    "Checklist (2–3 short items separated by semicolons).",
    "Contrarian angle (say what most people miss, simply).",
    "Analogy (compare markets to something normal people understand).",
    "Question hook (ask a sharp question, then answer it briefly).",
    "Rule of thumb (one rule + why it works).",
]

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

def remember_post(state: dict, post_text: str, meta: dict | None = None):
    day = today_key_local()

    # daily hash memory
    key = f"hashes_{day}"
    hashes = list(state.get(key, []) or [])
    hashes.append(sha1(post_text.lower()))
    state[key] = hashes[-50:]

    # recent posts memory
    recent = list(state.get("recent_posts", []) or [])
    recent.append({
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "text": post_text,
        "meta": meta or {},
    })
    state["recent_posts"] = recent[-RECENT_POSTS_TO_REMEMBER:]

    state["last_post_day"] = day
    state["last_post_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

def ai_client():
    return genai.Client(http_options=HttpOptions(api_version="v1"))

def extract_recent_phrases(state: dict, max_phrases: int = 12) -> list[str]:
    """
    Pull a few overused chunks from recent posts so the model avoids repeating them.
    """
    recent = list(state.get("recent_posts", []) or [])
    texts = " ".join([x.get("text", "") for x in recent][-10:])
    texts = texts.lower()

    # quick phrase candidates: 2–4 word chunks (very lightweight)
    words = re.findall(r"[a-zA-Z']+", texts)
    chunks = []
    for n in (2, 3, 4):
        for i in range(0, max(0, len(words) - n + 1)):
            chunk = " ".join(words[i:i+n])
            if len(chunk) < 10:
                continue
            if chunk in chunks:
                continue
            # avoid super-common English filler
            if chunk in ("in the", "of the", "to the", "on the", "and the"):
                continue
            chunks.append(chunk)

    # prefer ones that look “market-y”
    markety = [c for c in chunks if any(k in c for k in ["macro", "market", "rate", "inflation", "central", "policy", "risk", "crypto"])]
    out = (markety + chunks)[:max_phrases]
    return out

def looks_generic(text: str) -> bool:
    t = (text or "").lower()
    hits = sum(1 for p in GENERIC_PHRASES if p in t)
    # also penalize if it repeats "macro" too much
    macro_hits = len(re.findall(r"\bmacro\b", t))
    return hits >= 2 or macro_hits >= 2

def build_prompt(state: dict, topic: str, fmt: str, avoid_phrases: list[str]) -> str:
    avoid_line = ""
    if avoid_phrases:
        avoid_line = "Avoid reusing these phrases (or close rewrites): " + "; ".join(avoid_phrases[:12]) + "\n"

    return (
        "Write ONE X post that has a real chance to get replies/retweets.\n"
        "It can be about crypto, markets, tech, or human behavior — but keep it truthful and specific.\n\n"
        f"Topic to focus on: {topic}\n"
        f"Format to use: {fmt}\n\n"
        "Rules:\n"
        "- Sound like a human, not a newswire.\n"
        "- Use plain English. No corporate filler.\n"
        "- Be specific (mention a concrete concept), but DO NOT include price targets or exact levels.\n"
        "- No hashtags. No emojis. No links.\n"
        "- 1–3 sentences max.\n"
        f"- Stay under {MAX_CHARS} characters.\n"
        "- Try to include either (a) a sharp takeaway or (b) a question at the end.\n"
        f"{avoid_line}"
        "Output ONLY the post text."
    )

# ---------------- MAIN ----------------
event_name = os.getenv("GITHUB_EVENT_NAME", "")
is_manual = (event_name == "workflow_dispatch")

should_skip_now(is_manual)

state = load_state()
print("RUN: Proceeding (original post)")

# Pick a topic/format (avoid repeating last used)
used_topics = state.get("used_topic_ids", []) or []
used_formats = state.get("used_format_ids", []) or []

topic_id = random.randrange(len(TOPICS))
fmt_id = random.randrange(len(FORMATS))

# light rotation: try not to repeat last 3
for _ in range(10):
    if topic_id not in used_topics[-3:]:
        break
    topic_id = random.randrange(len(TOPICS))

for _ in range(10):
    if fmt_id not in used_formats[-3:]:
        break
    fmt_id = random.randrange(len(FORMATS))

topic = TOPICS[topic_id]
fmt = FORMATS[fmt_id]

avoid_phrases = extract_recent_phrases(state)

# 1) Generate post text (with retries)
post_text = ""
try:
    c = ai_client()

    for attempt in range(1, 4):  # 3 tries
        POST_PROMPT = build_prompt(state, topic, fmt, avoid_phrases)
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=POST_PROMPT)
        candidate = clamp_text(resp.text, MAX_CHARS)

        if not candidate or len(candidate) < 25:
            print(f"AI: attempt {attempt} too short/empty, retrying...")
            continue

        if looks_generic(candidate):
            print(f"AI: attempt {attempt} looks generic, retrying...")
            # strengthen avoid list using this candidate
            avoid_phrases = (avoid_phrases + [candidate.lower()[:60]] )[:20]
            continue

        post_text = candidate
        break

except Exception as e:
    print("AI ERROR:", repr(e))
    raise SystemExit(0)

if not post_text:
    print("SKIP: failed to generate a good post after retries.")
    raise SystemExit(0)

if is_duplicate_today(state, post_text):
    print("SKIP: duplicate content today (hash match).")
    raise SystemExit(0)

# 2) Post via X API v2 (POST /2/tweets)
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

client_write = tweepy.Client(
    consumer_key=api_key,
    consumer_secret=api_secret,
    access_token=access_token,
    access_token_secret=access_secret,
    wait_on_rate_limit=False,
)

try:
    resp = client_write.create_tweet(text=post_text, user_auth=True)

    # remember topic/format ids used
    used_topics.append(topic_id)
    used_formats.append(fmt_id)
    state["used_topic_ids"] = used_topics[-50:]
    state["used_format_ids"] = used_formats[-50:]

    remember_post(state, post_text, meta={"topic_id": topic_id, "format_id": fmt_id})
    print("POSTED ✅:", resp.data)
    print("TEXT:", post_text)
    print("META:", {"topic": topic, "format": fmt})

except tweepy.Forbidden as e:
    print("X Forbidden 403")
    r = getattr(e, "response", None)
    if r is not None:
        print("X STATUS:", r.status_code)
        print("X BODY:", (r.text or "")[:1200])
    print("DETAIL:", str(e)[:500])
    raise SystemExit(0)

except tweepy.TooManyRequests:
    print("RATE LIMIT: X write rate limit hit. Skipping.")
    raise SystemExit(0)

except Exception as e:
    print("X POST ERROR:", repr(e))
    raise SystemExit(0)