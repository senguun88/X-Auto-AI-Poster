import os
import random
from datetime import datetime
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

PROMPT = (
"Write ONE concise X post about the single most important market-moving news from today.\n\n"
    "Priority:\n"
    "1) Major crypto news (Bitcoin, Ethereum, ETFs, regulation, exchange events)\n"
    "2) If no major crypto news, choose a significant non-crypto market mover such as:\n"
    "- Gold, oil, or USD moves\n"
    "- Major tech company news (earnings, AI, layoffs, product launches)\n"
    "- Macro events (Fed, CPI, jobs data, geopolitical shocks)\n\n"
    "Rules:\n"
    "- Be factual, neutral, and useful\n"
    "- No hype or opinions\n"
    "- No emojis\n"
    "- No hashtags unless absolutely necessary\n"
    "- Maximum 240 characters. Do NOT exceed the limit."
)

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

print("POST: Proceeding to generate and post")

# ---------------- GEMINI (VERTEX AI) ----------------
client = genai.Client(http_options=HttpOptions(api_version="v1"))

resp = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=PROMPT,
)

text = (resp.text or "").strip().replace("\n", " ")
text = text[:MAX_CHARS]

print("Generated text:", text)

# ---------------- X POST (API v2) ----------------
import tweepy

client_x = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

try:
    client_x.create_tweet(text=text, user_auth=True)
    print("Posted to X ✅")
except tweepy.Forbidden as e:
    # Print the actual response body from X (this is the key)
    r = getattr(e, "response", None)
    if r is not None:
        print("X STATUS:", r.status_code)
        print("X BODY:", r.text)
    else:
        print("X Forbidden 403:", str(e))

    # Don't fail the workflow (exit clean)
    print("SKIP: X API blocked this request (likely plan/permissions).")
    raise SystemExit(0)