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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


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
ASSETS_PAGE_SIZE = 8


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


def callback_key(query: CallbackQuery) -> tuple[int, int] | None:
    if query.message is None:
        return None
    return (query.message.chat.id, query.from_user.id)


def short_name(value: str, limit: int = 36) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


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


def mode_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Новый файл", callback_data="upload_mode:new")],
            [InlineKeyboardButton(text="Обновление существующего", callback_data="upload_mode:update")],
            [InlineKeyboardButton(text="Отмена", callback_data="upload_cancel")],
        ]
    )


def assets_markup(assets: list[str], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(assets) + ASSETS_PAGE_SIZE - 1) // ASSETS_PAGE_SIZE)
    safe_page = max(0, min(page, total_pages - 1))
    start = safe_page * ASSETS_PAGE_SIZE
    end = min(start + ASSETS_PAGE_SIZE, len(assets))
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(start, end):
        rows.append(
            [
                InlineKeyboardButton(
                    text=short_name(assets[index]),
                    callback_data=f"upload_pick:{index}",
                )
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if safe_page > 0:
        navigation.append(InlineKeyboardButton(text="⬅", callback_data=f"upload_page:{safe_page - 1}"))
    if safe_page < total_pages - 1:
        navigation.append(InlineKeyboardButton(text="➡", callback_data=f"upload_page:{safe_page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="upload_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def success_markup(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Скачать APK", url=link)],
            [InlineKeyboardButton(text="Открыть хранилище", url=STORAGE_RELEASE_URL)],
        ]
    )


START_TEXT = (
    "<b>APK Mirror Bot</b>\n"
    "Отправь APK как <b>документ</b>, выбери режим и я загружу файл в GitHub Releases.\n\n"
    "Команды:\n"
    "<code>/start</code> - приветствие\n"
    "<code>/help</code> - краткая инструкция\n"
    "<code>/skip</code> - оставить исходное имя\n"
    "<code>/cancel</code> - отменить текущую загрузку"
)

HELP_TEXT = (
    "Как это работает:\n"
    "1) Отправляешь APK как документ\n"
    "2) Выбираешь режим: новый файл или обновление существующего\n"
    "3) Для нового файла задаешь имя, для обновления выбираешь файл из списка\n"
    "4) Бот скачивает APK и загружает в GitHub Releases\n"
    "5) Получаешь прямую ссылку на скачивание"
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


async def release_apk_assets() -> list[str]:
    await ensure_release()
    code, output = await run_command(
        "gh",
        "release",
        "view",
        RELEASE_TAG,
        "--repo",
        f"{GITHUB_OWNER}/{STORAGE_REPO}",
        "--json",
        "assets",
    )
    if code != 0:
        raise RuntimeError(output)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid release payload: {exc}") from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        return []
    result: list[str] = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.lower().endswith(".apk"):
            result.append(name)
    return sorted(set(result), key=str.casefold)


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
    mode: str,
) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = TMP_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    local_path = job_dir / asset_name
    mode_label = "обновление" if mode == "update" else "новый файл"
    progress = await message.answer(
        f"Принял: <b>{html.escape(source_name)}</b> ({format_size(file_size)}).\n"
        f"Режим: <b>{mode_label}</b>.\n"
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
            f"Режим: <b>{mode_label}</b>.\n"
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


@router.callback_query(F.data == "upload_cancel")
async def cancel_callback_handler(query: CallbackQuery) -> None:
    key = callback_key(query)
    if key is None:
        await query.answer("Не удалось определить сессию", show_alert=True)
        return
    if pending_uploads.pop(key, None) is None:
        await query.answer("Сессия уже завершена", show_alert=False)
        return
    await query.answer("Загрузка отменена", show_alert=False)
    if query.message is not None:
        await query.message.edit_text("Текущая загрузка отменена.")


@router.message(Command("skip"))
async def skip_name_handler(message: Message) -> None:
    key = message_key(message)
    item = pending_uploads.get(key)
    if item is None:
        await message.answer("Сначала отправь APK как документ.")
        return
    if str(item.get("step")) != "enter_name":
        await message.answer("Сейчас пропуск имени недоступен.")
        return
    pending_uploads.pop(key, None)
    source_name = str(item["file_name"])
    file_id = str(item["file_id"])
    file_size = int(item["file_size"]) if isinstance(item.get("file_size"), int) else None
    asset_name = to_custom_asset_name(source_name, source_name)
    await process_upload(message, file_id, source_name, file_size, asset_name, mode="new")


@router.callback_query(F.data.startswith("upload_mode:"))
async def mode_callback_handler(query: CallbackQuery) -> None:
    key = callback_key(query)
    if key is None:
        await query.answer("Не удалось определить сессию", show_alert=True)
        return
    item = pending_uploads.get(key)
    if item is None:
        await query.answer("Сначала отправь APK", show_alert=True)
        return
    if query.message is None:
        await query.answer("Сообщение недоступно", show_alert=True)
        return
    mode = (query.data or "").split(":", 1)[1]
    if mode == "new":
        item["mode"] = "new"
        item["step"] = "enter_name"
        item.pop("assets", None)
        item.pop("page", None)
        await query.message.edit_text(
            "Режим: <b>новый файл</b>.\n"
            "Отправь название для публикации.\n"
            "Пример: <code>After Motion Z+</code>\n"
            "Или используй <code>/skip</code>, чтобы оставить исходное имя."
        )
        await query.answer()
        return
    if mode != "update":
        await query.answer("Неизвестный режим", show_alert=True)
        return
    try:
        assets = await release_apk_assets()
    except Exception as exc:
        await query.message.edit_text(f"Не удалось получить список файлов:\n<code>{html.escape(redact(str(exc)))}</code>")
        await query.answer()
        return
    if not assets:
        item["mode"] = "new"
        item["step"] = "enter_name"
        item.pop("assets", None)
        item.pop("page", None)
        await query.message.edit_text(
            "В релизе пока нет APK для обновления.\n"
            "Переключил в режим <b>новый файл</b>.\n"
            "Отправь название для публикации или используй <code>/skip</code>."
        )
        await query.answer()
        return
    item["mode"] = "update"
    item["step"] = "choose_existing"
    item["assets"] = assets
    item["page"] = 0
    await query.message.edit_text(
        "Режим: <b>обновление существующего</b>.\nВыбери APK для замены:",
        reply_markup=assets_markup(assets, 0),
    )
    await query.answer()


@router.callback_query(F.data.startswith("upload_page:"))
async def assets_page_callback_handler(query: CallbackQuery) -> None:
    key = callback_key(query)
    if key is None:
        await query.answer("Не удалось определить сессию", show_alert=True)
        return
    item = pending_uploads.get(key)
    if item is None or str(item.get("step")) != "choose_existing":
        await query.answer("Сессия неактивна", show_alert=True)
        return
    if query.message is None:
        await query.answer("Сообщение недоступно", show_alert=True)
        return
    assets = item.get("assets")
    if not isinstance(assets, list):
        await query.answer("Список файлов не найден", show_alert=True)
        return
    raw_page = (query.data or "").split(":", 1)[1]
    try:
        page = int(raw_page)
    except ValueError:
        await query.answer()
        return
    total_pages = max(1, (len(assets) + ASSETS_PAGE_SIZE - 1) // ASSETS_PAGE_SIZE)
    safe_page = max(0, min(page, total_pages - 1))
    item["page"] = safe_page
    await query.message.edit_text(
        "Режим: <b>обновление существующего</b>.\nВыбери APK для замены:",
        reply_markup=assets_markup(assets, safe_page),
    )
    await query.answer()


@router.callback_query(F.data.startswith("upload_pick:"))
async def asset_pick_callback_handler(query: CallbackQuery) -> None:
    key = callback_key(query)
    if key is None:
        await query.answer("Не удалось определить сессию", show_alert=True)
        return
    item = pending_uploads.get(key)
    if item is None or str(item.get("step")) != "choose_existing":
        await query.answer("Сессия неактивна", show_alert=True)
        return
    if query.message is None:
        await query.answer("Сообщение недоступно", show_alert=True)
        return
    assets = item.get("assets")
    if not isinstance(assets, list):
        await query.answer("Список файлов не найден", show_alert=True)
        return
    raw_index = (query.data or "").split(":", 1)[1]
    try:
        index = int(raw_index)
    except ValueError:
        await query.answer("Некорректный выбор", show_alert=True)
        return
    if index < 0 or index >= len(assets):
        await query.answer("Файл не найден", show_alert=True)
        return
    pending_uploads.pop(key, None)
    source_name = str(item["file_name"])
    file_id = str(item["file_id"])
    file_size = int(item["file_size"]) if isinstance(item.get("file_size"), int) else None
    asset_name = str(assets[index])
    await query.answer("Запускаю обновление", show_alert=False)
    await query.message.edit_text(
        f"Выбран файл для обновления: <code>{html.escape(asset_name)}</code>.\nНачинаю загрузку..."
    )
    await process_upload(query.message, file_id, source_name, file_size, asset_name, mode="update")


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
        "step": "choose_mode",
        "mode": None,
    }
    await message.answer(
        f"Файл принят: <b>{html.escape(file_name)}</b> ({format_size(document.file_size)}).\n"
        "Выбери режим загрузки:",
        reply_markup=mode_markup(),
    )


@router.message(F.text)
async def name_handler(message: Message) -> None:
    key = message_key(message)
    item = pending_uploads.get(key)
    if item is None:
        await message.answer("Отправь APK как документ. Для подсказки: /help")
        return
    if str(item.get("step")) == "choose_mode":
        await message.answer("Сначала выбери режим кнопками: новый файл или обновление.")
        return
    if str(item.get("step")) == "choose_existing":
        await message.answer("Выбери существующий файл кнопкой из списка или нажми /cancel.")
        return
    if str(item.get("step")) != "enter_name":
        await message.answer("Сессия в неизвестном состоянии. Отправь /cancel и начни заново.")
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
    await process_upload(message, file_id, source_name, file_size, asset_name, mode="new")


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
