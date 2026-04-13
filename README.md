# apk-mirror-bot-source

Telegram bot source code for mirroring APK files into GitHub Releases.

## Overview

This project receives `.apk` documents in Telegram, downloads each file, uploads it to the public release storage repository, returns a direct release asset link, and removes the local temporary file.

## Stack

- Python 3.12+
- aiogram 3.x
- aiohttp
- GitHub CLI (`gh`)
- PM2
- Local Telegram Bot API endpoint at `http://localhost:18081`

## Repositories

- Source code: `apk-mirror-bot-source`
- APK storage: `telegram-apk-storage`

## Runtime Flow

1. User sends an `.apk` file to the bot.
2. Bot asks for a custom release filename.
3. User sends the desired name (or uses `/skip` to keep source name).
4. Bot shows progress states (`1/3`, `2/3`, `3/3`) and validates file type/size.
5. Bot downloads the file to a local temporary directory with fallback strategies (local filesystem, Docker `telegram_bot_api` container copy, local file endpoint, cloud API).
6. Bot ensures the target GitHub Release tag exists.
7. Bot uploads the file as a release asset with `gh release upload --clobber`.
8. Bot returns a direct asset URL and inline download/storage buttons.
9. Bot deletes the local file.

## Requirements

- Authenticated GitHub CLI session for the runtime user.
- Access to both GitHub repositories.
- Running local Bot API server on `localhost:18081`.
- If local Bot API returns absolute file paths, keep `BOT_API_LOCAL=1`.
- If a local path is not directly readable, bot retries via local file endpoint and then via Telegram Cloud API.

## Setup

```bash
python3.12 -m pip install aiogram aiohttp
cp .env.example .env
```

## PM2

Start and persist the process:

```bash
pm2 start ecosystem.config.js
pm2 save
```

## Environment Variables

- `BOT_TOKEN` (required)
- `GITHUB_OWNER` (default: `Rewixx-png`)
- `STORAGE_REPO` (default: `telegram-apk-storage`)
- `STORAGE_RELEASE_TAG` (default: `apk-storage`)
- `BOT_API_BASE` (default: `http://localhost:18081`)
- `BOT_API_LOCAL` (default: `1`)
- `BOT_API_DOCKER_COPY` (default: `1`)
- `BOT_API_CONTAINER` (default: `telegram_bot_api`)
- `MAX_ASSET_BYTES` (default: `2147483647`)

## Security Note

Keep `.env` local and untracked.
