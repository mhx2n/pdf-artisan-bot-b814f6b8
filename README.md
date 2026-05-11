# CSV TO PDF Telegram Bot — Render Free Web Service

## Render settings
- Service Type: Web Service
- Build Command: `pip install --upgrade pip && pip install -r requirements.txt`
- Start Command: `python bot.py`
- Health Check Path: `/health`

## Environment Variables
Add these in Render → Environment:
- `PYTHON_VERSION` = `3.11.10`
- `PORT` = `10000`
- `BOT_TOKEN` = your Telegram bot token
- `OWNER_ID` = your Telegram numeric user id

## Why previous errors happened
- `PDF তৈরি ব্যর্থ: 'super' object has no attribute 'transform'` came from invalid WeasyPrint/CSS transform handling. This version avoids transform-based watermark code.
- `Ignored font-weight: 100 900` happened because WeasyPrint does not accept variable-font weight ranges there. This version uses fixed numeric weights only.
- `Font-face 'NotoBn' cannot be loaded` came from missing/incorrect font-face path. This version includes `fonts/NotoSansBengali-Regular.ttf` and uses the correct local path.
- Render Web Service kept running/not live because it must bind to `$PORT`. This version starts `/` and `/health` HTTP endpoints before polling.

## Important
Render Free Web Service sleeps after inactivity. Use UptimeRobot to ping:
`https://your-service.onrender.com/health`
every 5 minutes.
