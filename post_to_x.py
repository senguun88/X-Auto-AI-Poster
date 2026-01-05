import os
import tweepy
from openai import OpenAI

# OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# X OAuth 1.0a (user context)
auth = tweepy.OAuth1UserHandler(
    os.getenv("X_API_KEY"),
    os.getenv("X_API_SECRET"),
    os.getenv("X_ACCESS_TOKEN"),
    os.getenv("X_ACCESS_SECRET"),
)
api = tweepy.API(auth)

prompt = (
    "Write ONE original X post about a useful tech or crypto idea. "
    "Clear, punchy, no emojis, max 240 characters."
)

resp = client.responses.create(
    model="gpt-4o-mini",
    input=prompt,
)

tweet_text = resp.output_text.strip()[:280]
api.update_status(tweet_text)

print("Posted:", tweet_text)
