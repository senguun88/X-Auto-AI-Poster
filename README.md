# X Auto Poster 🤖

A beginner project that automatically posts market-related updates to X (Twitter) using AI and GitHub Actions.

This project is mainly for learning and experimentation. I’m not a professional developer — just exploring automation, APIs, and AI by building something practical.

---

## ✨ What this does

- Automatically runs on a schedule using **GitHub Actions**
- Uses **Google Gemini (Vertex AI)** to generate short, factual market updates
- Posts directly to **X (Twitter)** using the X API
- Avoids posting during quiet hours
- Randomly skips some runs to look more human
- Prevents duplicate posts on the same day

---

## 🧠 How it works (high level)

1. GitHub Actions runs the workflow on a schedule  
2. A Python script:
   - Decides whether to post
   - Asks Gemini AI to generate a short market update
   - Posts it to X using the API
3. A small local state (cached by GitHub Actions) helps avoid repeats

No database. No server. Just GitHub Actions + Python.

---

## 🛠 Tech used

- Python
- GitHub Actions
- Google Gemini (Vertex AI)
- X (Twitter) API v2
- Tweepy

---

## ⚠️ Important notes

- This project uses the **free X API tier**, which limits read/search features  
- Automated tweet discovery and retweeting may not work on free plans
- This is a learning project, not production software
- Use at your own risk and follow X’s API rules

---

## 🚀 Getting started (basic idea)

To run something like this yourself, you would need:
- A GitHub repository
- GitHub Actions enabled
- X API credentials stored as GitHub Secrets
- Google Cloud project with Gemini enabled

This repo is mainly shared for learning and reference.

---

## 📚 Why I built this

I wanted to:
- Learn Python by building something real
- Understand GitHub Actions
- Experiment with AI-generated content
- Explore automation without running servers

---

## 🧪 Status

- Actively learning
- Expect rough edges
- Improvements coming as I learn more

---

## 📄 License

MIT License
