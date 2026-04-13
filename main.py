import asyncio
import os
import uuid
from asyncio.subprocess import PIPE
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Message


def load_env() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "Rewixx-png")
STORAGE_REPO = os.getenv("STORAGE_REPO", "telegram-apk-storage")
RELEASE_TAG = os.getenv("STORAGE_RELEASE_TAG", "apk-storage")
TMP_DIR = Path(".tmp_apk")

router = Router()


async def run_command(*args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()
    output = stdout.decode().strip() or stderr.decode().strip()
    return process.returncode, output


async def ensure_release() -> None:
    code, _ = await run_command(
        "gh",
        "release",
        "view",
        RELEASE_TAG,
        "--repo",
        f"{GITHUB_OWNER}/{STORAGE_REPO}",
    )
    if code == 0:
        return
    code, output = await run_command(
        "gh",
        "release",
        "create",
        RELEASE_TAG,
        "--repo",
        f"{GITHUB_OWNER}/{STORAGE_REPO}",
        "--title",
        "APK Storage",
        "--notes",
        "Telegram APK mirror storage",
    )
    if code != 0:
        raise RuntimeError(output)


@router.message(F.document)
async def apk_handler(message: Message) -> None:
    document = message.document
    if document is None or not document.file_name or not document.file_name.lower().endswith(".apk"):
        await message.answer("Send an .apk file.")
        return
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(document.file_name).name
    local_name = f"{Path(safe_name).stem}-{uuid.uuid4().hex[:10]}.apk"
    local_path = TMP_DIR / local_name
    try:
        await message.answer("Downloading file...")
        telegram_file = await message.bot.get_file(document.file_id)
        await message.bot.download_file(telegram_file.file_path, destination=local_path)
        await message.answer("Uploading to GitHub release...")
        await ensure_release()
        code, output = await run_command(
            "gh",
            "release",
            "upload",
            RELEASE_TAG,
            str(local_path),
            "--repo",
            f"{GITHUB_OWNER}/{STORAGE_REPO}",
        )
        if code != 0:
            raise RuntimeError(output)
        link = f"https://github.com/{GITHUB_OWNER}/{STORAGE_REPO}/releases/download/{RELEASE_TAG}/{local_path.name}"
        await message.answer(link)
    except Exception as exc:
        await message.answer(f"Upload failed: {exc}")
    finally:
        local_path.unlink(missing_ok=True)


@router.message()
async def fallback_handler(message: Message) -> None:
    await message.answer("Send an .apk document.")


async def main() -> None:
    session = AiohttpSession(api=TelegramAPIServer.from_base("http://localhost:18081"))
    bot = Bot(token=TOKEN, session=session)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
