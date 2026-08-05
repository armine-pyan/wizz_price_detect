# Wizz Air Fare Tracker

Tracks Wizz Air fares departing **Yerevan (EVN)** on **2026-08-22 through 2026-08-24**, saves price history, and sends a Telegram alert when a fare changes by at least **USD 10**.

## What it stores

Each run appends observations to `data/prices.csv` with the timestamp, route, departure time, currency, original price, USD price, previous USD price, and change.

## Telegram setup

1. In Telegram, open **@BotFather** and create a bot with `/newbot`.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in your browser and copy the `chat.id` value.
4. In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.
5. Add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

Never commit these values into the repository.

## Run it

Open **Actions → Wizz fare watch → Run workflow**. The scheduled workflow runs hourly.

## Configuration

Edit `config.json` to change dates, origin, or alert threshold.

## Important limitation

Wizz Air does not publish a stable public consumer fare API. This project uses Wizz Air's booking backend, which may change. If the workflow starts failing, inspect the Actions log and update `WIZZ_API_VERSION` or the response parser.
