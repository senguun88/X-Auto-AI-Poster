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
    return (utcn
