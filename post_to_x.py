import os
import random
from datetime import datetime
import pytz

import tweepy
from google import genai

# ---------------- SETTINGS ----------------

MAX_CHARS = 260          # stay safely under X limit
POST_CHANCE = 0.25       # ~5–6 posts/day (runs hourly)
TIMEZONE = "US/Mountain"

QUIET_HOURS = (23, 6)    # 11 PM – 6 AM

PROMPT = """
Write ONE concise, interesting tech news post for X.
Rules:
- Max 260 characters
- No hashtags
- No emojis
- No clickbait
- Neutral, informative tone
- One sentence only
- About recent tech, AI, software, or major companies
"""

# ---------------- TIME + RANDOM CONTROL ----------------

tz = pytz.timezone(TIMEZONE)
now = datetime.now(tz)
hour = now.hour

if hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]:
    print("Quiet hours — skipping post")
    exit(0)

if random.random() > POST_CHANCE:
    print("Random skip — not posting this hour")
    exit(0)

print("Posting this hour")

# ---------------- GEMINI (VERTEX AI) ----------------

client = genai.Client(
    vertexai=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
)

response = client.models.generate_content(
    model="gemini-1.5-flash",
    contents=PROMPT,
)

text = response.text.strip().replace("\n", " ")

if len(text) > MAX_CHARS:
    text = text[:MAX_CHARS - 1]

print("Generated post:")
print(text)

# ---------------- X (TWITTER) ----------------

auth = tweepy.OAuth1UserHandler(
    os.environ["X_API_KEY"],
    os.environ["X_API_SECRET"],
    os.environ["X_ACCESS_TOKEN"],
    os.environ["X_ACCESS_SECRET"],
)

api = tweepy.API(auth)

api.update_status(status=text)

print("Post sent successfully")
