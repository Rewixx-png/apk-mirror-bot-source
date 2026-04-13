# apk-mirror-bot-source

Telegram bot source code for mirroring APK files into GitHub Releases.

## Overview

This project receives `.apk` documents in Telegram, downloads each file, uploads it to the public release storage repository, returns a direct release asset link, and removes the local temporary file.

## Stack

- Python 3.11+
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
2. Bot downloads the file to a local temporary directory.
3. Bot ensures the target GitHub Release tag exists.
4. Bot uploads the file as a release asset with `gh release upload`.
5. Bot returns a direct asset URL to the user.
6. Bot deletes the local file.

## Requirements

- Authenticated GitHub CLI session for the runtime user.
- Access to both GitHub repositories.
- Running local Bot API server on `localhost:18081`.

## Setup

```bash
python3.11 -m pip install aiogram aiohttp
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

## Security Note

Keep `.env` local and untracked.
