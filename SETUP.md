# 🚀 Amazon Shift Holder — Deployment Guide

## YOUR CREDENTIALS (KEEP SAFE!)
- Bot: @Jibhub_bot
- Chat ID: 1027065157
- Render: dashboard.render.com ✅
- GitHub: github.com ✅
- Bright Data: brightdata.com ✅
- 2Captcha: 2captcha.com ✅

---

## STEP 1 — GitHub Repository

1. Go to github.com
2. Click "+" → New repository
3. Name: amazon-shift-holder
4. Set to PRIVATE
5. Click Create repository
6. Upload these files:
   - backend/bot.py
   - backend/requirements.txt
   - backend/render.yaml
   - backend/start.sh
   - frontend/index.html

---

## STEP 2 — Deploy to Render

1. Go to dashboard.render.com
2. Click "+ New" → Background Worker
3. Connect GitHub → select amazon-shift-holder
4. Settings:
   - Root Directory: backend
   - Build Command: pip install -r requirements.txt && playwright install chromium && playwright install-deps chromium
   - Start Command: python bot.py
5. Add Environment Variables:
   - BOT_TOKEN = 8713464696:AAG9F4SudtujRHaBePItPBuZq3dEYxV648E
   - CHAT_ID = 1027065157
   - BRIGHT_DATA_USER = (from brightdata.com)
   - BRIGHT_DATA_PASS = (from brightdata.com)
   - CAPTCHA_KEY = (from 2captcha.com)
6. Click Deploy!

---

## STEP 3 — GitHub Pages (Frontend)

1. Go to your repo on GitHub
2. Settings → Pages
3. Source: Deploy from branch
4. Branch: main / Folder: /frontend
5. Your dashboard: https://YOUR-USERNAME.github.io/amazon-shift-holder

---

## STEP 4 — Get Bright Data Credentials

1. Go to brightdata.com
2. Proxies → Residential proxies
3. Add zone → Copy username & password
4. Add to Render environment variables

---

## STEP 5 — Get 2Captcha Key

1. Go to 2captcha.com
2. Dashboard → API key
3. Copy key
4. Add to Render environment variables

---

## TELEGRAM COMMANDS

Once running, send these to @Jibhub_bot:

/start   — Welcome + status
/status  — Check if bot is running
/jobs    — See active jobs
/pause   — Pause alerts temporarily
/resume  — Resume alerts
/stats   — Your daily statistics
/help    — All commands

---

## HOW IT WORKS

1. Bot watches jobsatamazon.co.uk every 30 seconds
2. During peak hours (10pm-midnight): every 10 seconds!
3. New job found → Bot navigates application
4. Telegram alert sent to your phone
5. You open the link → fill your details → SUBMIT
6. Reminders sent at 30, 60, 90 minute marks
7. 2 hour window tracked automatically

---

## MONTHLY COSTS

| Service | Cost |
|---------|------|
| Render Worker | £7 |
| Bright Data | £10 |
| 2Captcha | £5 |
| Total | £22/month |
