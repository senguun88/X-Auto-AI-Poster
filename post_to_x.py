import os
import tweepy
from google import genai
from google.genai.types import HttpOptions

# --- X (OAuth 1.0a) ---
auth = tweepy.OAuth1UserHandler(
    os.getenv("X_API_KEY"),
    os.getenv("X_API_SECRET"),
    os.getenv("X_ACCESS_TOKEN"),
    os.getenv("X_ACCESS_SECRET"),
)
api = tweepy.API(auth)

# --- Gemini via Vertex AI ---
# These are set by GitHub Actions env:
# GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, GOOGLE_GENAI_USE_VERTEXAI
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
api.update_status(tweet_text)

print("Posted:", tweet_text)
