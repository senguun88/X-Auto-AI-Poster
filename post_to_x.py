import os
import tweepy
from google import genai
from google.genai.types import HttpOptions

# ---------- X API v2 (OAuth 2.0 / OAuth 1.0a compatible) ----------
client_x = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_SECRET"),
)

# ---------- Gemini via Vertex AI ----------
client = genai.Client(http_options=HttpOptions(api_version="v1"))

prompt = (
    "Write ONE original X post about tech or crypto. "
    "Clear, useful, no emojis. Under 240 characters."
)

resp = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt,
)

tweet_text = (resp.text or "").strip()[:280]

# ---------- Post using X API v2 ----------
client_x.create_tweet(text=tweet_text)

print("Posted:", tweet_text)
