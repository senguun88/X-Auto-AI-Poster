# X Auto Bot 🤖

A small learning project that automates interactions on X (Twitter) using **Python**, **AI**, and **GitHub Actions**.

This repository is for experimentation and learning, not production use.  
I’m not a professional developer — just building things to better understand automation, APIs, and AI.

---

## What it does
- Runs automatically on a schedule using GitHub Actions  
- Uses AI to generate short replies or posts  
- Interacts with X (Twitter) via the X API  
- Skips quiet hours  
- Randomly skips some runs to look more human  
- Avoids replying to the same tweet twice  
- Uses a small cached state (no database, no server)

---

## How it works (high level)
1. GitHub Actions triggers the workflow  
2. A Python script decides whether to act  
3. AI generates a reply  
4. The bot replies on X  
5. A small local state (cached between runs) prevents duplicates and rate-limit spam  

No servers. No database. Just GitHub Actions + Python.

---

## Tech used
- Python  
- GitHub Actions  
- Google Gemini (Vertex AI)  
- X (Twitter) API v2  
- Tweepy  

---

## Important notes
- X API rate limits apply  
- The bot actively avoids hitting search limits  
- Behavior may change as the project evolves  
- This is a learning project — use at your own risk  
- Always follow X’s API rules  

---

## Why I built this
- Learn Python by building something real  
- Understand GitHub Actions  
- Experiment with AI-generated content  
- Explore automation without running servers  

---

## Status
🧪 Active learning project  
Expect changes, refactors, and rough edges.
