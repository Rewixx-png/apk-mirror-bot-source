import asyncio
import json
import html
import os
import re
import uuid
from asyncio.subprocess import PIPE
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message


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
BOT_API_BASE = os.getenv("BOT_API_BASE", "http://localhost:18081")
BOT_API_LOCAL = os.getenv("BOT_API_LOCAL", "1").strip().lower() not in {"0", "false", "no", "off"}
BOT_API_DOCKER_COPY = os.getenv("BOT_API_DOCKER_COPY", "1").strip().lower() not in {"0", "false", "no", "off"}
BOT_API_CONTAINER = os.getenv("BOT_API_CONTAINER", "telegram_bot_api")
MAX_ASSET_BYTES = int(os.getenv("MAX_ASSET_BYTES", str(2 * 1024 * 1024 * 1024 - 1)))
TMP_DIR = Path(".tmp_apk")
STORAGE_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{STORAGE_REPO}"
STORAGE_RELEASE_URL = f"{STORAGE_REPO_URL}/releases/tag/{RELEASE_TAG}"

router = Router()
pending_uploads: dict[tuple[int, int], dict[str, object]] = {}


def format_size(size: int | None) -> str:
    if not size or size <= 0:
        return "размер неизвестен"
    value = float(size)
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def to_custom_asset_name(value: str, fallback: str | None = None) -> str:
    raw = value.strip().replace("\n", " ").replace("\r", " ")
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    if raw.lower().endswith(".apk"):
        raw = raw[:-4].strip(" .")
    if not raw and fallback:
        raw = Path(fallback).stem.strip()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in {" ", "-", "_", ".", "+", "(", ")", "[", "]"})
    safe = re.sub(r"\s+", "-", safe).strip("-_.")[:120]
    if not safe:
        safe = f"apk-{uuid.uuid4().hex[:8]}"
    return f"{safe}.apk"


def message_key(message: Message) -> tuple[int, int]:
    user_id = message.from_user.id if message.from_user else 0
    return (message.chat.id, user_id)


def redact(value: str) -> str:
    return value.replace(TOKEN, "<hidden>")


def file_path_candidates(file_path: str) -> list[str]:
    value = file_path.strip()
    if not value:
        return []
    items = [value]
    stripped = value.lstrip("/")
    if stripped and stripped != value:
        items.append(stripped)
    marker = f"/{TOKEN}/"
    if marker in value:
        tail = value.split(marker, 1)[1].lstrip("/")
        if tail:
            items.append(tail)
    for prefix in ("documents/", "photos/", "videos/", "voice/", "audio/", "animations/", "video_notes/"):
        index = value.find(prefix)
        if index != -1:
            items.append(value[index:])
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def stream_download(url: str, destination: Path) -> None:
    timeout = aiohttp.ClientTimeout(total=0, sock_connect=30, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            if response.status != 200:
                body = (await response.text()).strip()
                message = body[:140] if body else ""
                raise RuntimeError(f"HTTP {response.status} {message}".strip())
            with destination.open("wb") as stream:
                async for chunk in response.content.iter_chunked(262144):
                    stream.write(chunk)


async def copy_from_container(container_path: str, destination: Path) -> None:
    code, output = await run_command(
        "docker",
        "cp",
        f"{BOT_API_CONTAINER}:{container_path}",
        str(destination),
    )
    if code != 0:
        raise RuntimeError(output)


async def cloud_file_path(file_id: str) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"https://api.telegram.org/bot{TOKEN}/getFile", params={"file_id": file_id}) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(f"Cloud API getFile failed with {response.status}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cloud API returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Cloud API returned unexpected payload")
    result = payload.get("result") if isinstance(payload, dict) else None
    file_path = result.get("file_path") if isinstance(result, dict) else None
    if payload.get("ok") is True and isinstance(file_path, str) and file_path:
        return file_path
    raise RuntimeError("Cloud API did not return file_path")


async def download_with_fallback(bot: Bot, file_id: str, file_path: str, destination: Path) -> None:
    errors: list[str] = []
    try:
        await bot.download_file(file_path, destination=destination)
        if destination.exists() and destination.stat().st_size > 0:
            return
    except Exception as exc:
        errors.append(redact(str(exc)))
    if BOT_API_DOCKER_COPY and file_path.startswith("/"):
        try:
            await copy_from_container(file_path, destination)
            if destination.exists() and destination.stat().st_size > 0:
                return
        except Exception as exc:
            errors.append(redact(str(exc)))
    for candidate in file_path_candidates(file_path):
        try:
            url = f"{BOT_API_BASE.rstrip('/')}/file/bot{TOKEN}/{quote(candidate, safe='/')}"
            await stream_download(url, destination)
            if destination.exists() and destination.stat().st_size > 0:
                return
        except Exception as exc:
            errors.append(redact(str(exc)))
    try:
        remote_path = await cloud_file_path(file_id)
        cloud_url = f"https://api.telegram.org/file/bot{TOKEN}/{quote(remote_path, safe='/')}"
        await stream_download(cloud_url, destination)
        if destination.exists() and destination.stat().st_size > 0:
            return
    except Exception as exc:
        errors.append(redact(str(exc)))
    details = " | ".join(errors[-3:]) if errors else "unknown download error"
    raise RuntimeError(details)


def storage_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть хранилище APK", url=STORAGE_RELEASE_URL)]]
    )


def success_markup(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Скачать APK", url=link)],
            [InlineKeyboardButton(text="Открыть хранилище", url=STORAGE_RELEASE_URL)],
        ]
    )


START_TEXT = (
    "<b>APK Mirror Bot</b>\n"
    "Отправь APK как <b>документ</b>, затем задай имя файла, и я загружу его в GitHub Releases.\n\n"
    "Команды:\n"
    "<code>/start</code> - приветствие\n"
    "<code>/help</code> - краткая инструкция\n"
    "<code>/skip</code> - оставить исходное имя\n"
    "<code>/cancel</code> - отменить текущую загрузку"
)

HELP_TEXT = (
    "Как это работает:\n"
    "1) Отправляешь APK как документ\n"
    "2) Отправляешь желаемое имя файла\n"
    "3) Бот скачивает файл и загружает в GitHub Releases\n"
    "4) Получаешь прямую ссылку на скачивание"
)


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


async def upload_to_release(local_path: Path) -> str:
    await ensure_release()
    code, output = await run_command(
        "gh",
        "release",
        "upload",
        RELEASE_TAG,
        str(local_path),
        "--clobber",
        "--repo",
        f"{GITHUB_OWNER}/{STORAGE_REPO}",
    )
    if code != 0:
        raise RuntimeError(output)
    return f"{STORAGE_REPO_URL}/releases/download/{RELEASE_TAG}/{quote(local_path.name)}"


async def process_upload(
    message: Message,
    file_id: str,
    source_name: str,
    file_size: int | None,
    asset_name: str,
) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = TMP_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    local_path = job_dir / asset_name
    progress = await message.answer(
        f"Принял: <b>{html.escape(source_name)}</b> ({format_size(file_size)}).\n"
        f"Имя публикации: <code>{html.escape(asset_name)}</code>.\n"
        "1/3 Скачиваю файл..."
    )
    try:
        telegram_file = await message.bot.get_file(file_id)
        if not telegram_file.file_path:
            raise RuntimeError("Telegram не вернул путь к файлу")
        await download_with_fallback(message.bot, file_id, telegram_file.file_path, local_path)
        await progress.edit_text(
            f"Принял: <b>{html.escape(source_name)}</b> ({format_size(file_size)}).\n"
            f"Имя публикации: <code>{html.escape(asset_name)}</code>.\n"
            "2/3 Загружаю в GitHub Releases..."
        )
        link = await upload_to_release(local_path)
        await progress.edit_text(
            f"Готово.\n3/3 Файл успешно загружен.\n\nПрямая ссылка:\n<code>{html.escape(link)}</code>",
            reply_markup=success_markup(link),
        )
    except Exception as exc:
        await progress.edit_text(f"Ошибка загрузки:\n<code>{html.escape(redact(str(exc)))}</code>")
    finally:
        local_path.unlink(missing_ok=True)
        try:
            job_dir.rmdir()
        except OSError:
            pass


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=storage_markup())


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=storage_markup())


@router.message(Command("cancel"))
async def cancel_handler(message: Message) -> None:
    key = message_key(message)
    if pending_uploads.pop(key, None) is None:
        await message.answer("Нет активной загрузки для отмены.")
        return
    await message.answer("Текущая загрузка отменена.")


@router.message(Command("skip"))
async def skip_name_handler(message: Message) -> None:
    key = message_key(message)
    item = pending_uploads.pop(key, None)
    if item is None:
        await message.answer("Сначала отправь APK как документ.")
        return
    source_name = str(item["file_name"])
    file_id = str(item["file_id"])
    file_size = int(item["file_size"]) if isinstance(item.get("file_size"), int) else None
    asset_name = to_custom_asset_name(source_name, source_name)
    await process_upload(message, file_id, source_name, file_size, asset_name)


@router.message(F.document)
async def apk_handler(message: Message) -> None:
    document = message.document
    if document is None:
        await message.answer("Отправь .apk файл.")
        return
    file_name = document.file_name or "file"
    is_apk = file_name.lower().endswith(".apk") or document.mime_type == "application/vnd.android.package-archive"
    if not is_apk:
        await message.answer("Это не APK. Отправь файл с расширением .apk как документ.")
        return
    if document.file_size and document.file_size > MAX_ASSET_BYTES:
        await message.answer(
            f"Файл слишком большой: {format_size(document.file_size)}. Лимит GitHub Release asset: 2 GB."
        )
        return
    key = message_key(message)
    pending_uploads[key] = {
        "file_id": document.file_id,
        "file_name": file_name,
        "file_size": document.file_size,
    }
    await message.answer(
        f"Файл принят: <b>{html.escape(file_name)}</b> ({format_size(document.file_size)}).\n"
        "Теперь отправь название для публикации.\n"
        "Пример: <code>After Motion Z+</code>\n"
        "Или используй <code>/skip</code>, чтобы оставить исходное имя."
    )


@router.message(F.text)
async def name_handler(message: Message) -> None:
    key = message_key(message)
    item = pending_uploads.get(key)
    if item is None:
        await message.answer("Отправь APK как документ. Для подсказки: /help")
        return
    name_text = (message.text or "").strip()
    if not name_text:
        await message.answer("Название пустое. Отправь текст с именем файла.")
        return
    pending_uploads.pop(key, None)
    source_name = str(item["file_name"])
    file_id = str(item["file_id"])
    file_size = int(item["file_size"]) if isinstance(item.get("file_size"), int) else None
    asset_name = to_custom_asset_name(name_text, source_name)
    await process_upload(message, file_id, source_name, file_size, asset_name)


@router.message()
async def fallback_handler(message: Message) -> None:
    await message.answer("Отправь APK как документ. Для подсказки: /help")


async def main() -> None:
    api = TelegramAPIServer.from_base(BOT_API_BASE, is_local=BOT_API_LOCAL)
    session = AiohttpSession(api=api)
    bot = Bot(token=TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
