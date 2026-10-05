# V2Box links Telegram bot

An agentic bot: every 12 hours Claude searches GitHub for fresh V2Ray/V2Box subscription repos,
opens their files, downloads and checks each subscription (config count per protocol), picks the
best ones and the bot posts them to your Telegram channel. Only links the bot actually downloaded
and validated in that run can be posted.

It runs on GitHub Actions (`.github/workflows/v2box-bot.yml`), so no server is needed.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Add the bot to your channel as an **admin** with permission to post messages.
3. In this repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `ANTHROPIC_API_KEY`: your Claude API key
   - `TELEGRAM_BOT_TOKEN`: the BotFather token
   - `TELEGRAM_CHANNEL_ID`: `@your_channel` (public) or the numeric id like `-100123456789` (private)
4. Merge this into the default branch (scheduled workflows only run from there).
5. To test right away: **Actions → V2Box links bot → Run workflow**.

The schedule is `0 */12 * * *` (00:00 and 12:00 UTC). Change the cron line to adjust it.

## Run locally

```bash
pip install -r v2box_bot/requirements.txt
export ANTHROPIC_API_KEY=... GITHUB_TOKEN=...   # GITHUB_TOKEN optional
DRY_RUN=1 python v2box_bot/bot.py               # prints the post instead of sending
```
