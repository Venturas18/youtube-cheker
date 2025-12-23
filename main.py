# main.py

import logging
import html
import io
import os
import asyncio
import zipfile
import shutil
import aiohttp
import yt_dlp
import httpx
import numpy as np
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, FSInputFile

from config import TELEGRAM_BOT_TOKEN
from youtube_analyzer import YouTubeAnalyzer
from trends_analyzer import analyze_google_trends
from excel_generator import ExcelGenerator
from channel_graphics import create_activity_graphs, create_heatmap_graph

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
youtube_analyzer = YouTubeAnalyzer()


# --- СОСТОЯНИЯ ---
class UserStates(StatesGroup):
    waiting_for_video_link = State()
    waiting_for_channel_link = State()
    waiting_for_trends_query = State()
    waiting_for_niche_name = State()
    niche_analysis = State()
    waiting_for_all_titles_link = State()
    waiting_for_thumb_count = State()
    waiting_for_thumb_channel = State()


# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    buttons = [
        [types.InlineKeyboardButton(text="🎥 Аналитика видео", callback_data="analyze_video")],
        [types.InlineKeyboardButton(text="🔗 Аналитика канала", callback_data="analyze_channel")],
        [types.InlineKeyboardButton(text="📑 Все названия видео", callback_data="get_all_titles")],
        [
            types.InlineKeyboardButton(text="📈 Google Trends", callback_data="cmd_trends"),
            types.InlineKeyboardButton(text="📊 Анализ ниши (Excel)", callback_data="cmd_excel")
        ]
    ]
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


def get_niche_analysis_keyboard():
    buttons = [
        [KeyboardButton(text="💾 Готово и Скачать")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=False)
    return keyboard


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def pluralize_canal(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "канал"
    elif 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
        return "канала"
    else:
        return "каналов"


def format_number(num_str: str) -> str:
    try:
        num_int = int(num_str)
        return f"{num_int:,}".replace(',', '.')
    except (ValueError, TypeError):
        return str(num_str)


async def get_country_info(code: str) -> str:
    if code == 'N/A': return ""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"https://restcountries.com/v3.1/alpha/{code}")
            response.raise_for_status()
            data = response.json()[0]
            country_name = data['name']['common']
            flag_emoji = "".join([chr(0x1F1E6 + ord(char) - ord('A')) for char in code.upper()])
            return f"{flag_emoji} {country_name} ({code})"
    except Exception:
        return f"({code})"


def generate_metadata_content(data: dict) -> str:
    title = data.get('title', 'N/A')
    video_id = data.get('video_id', 'N/A')
    video_url = data.get('url', 'N/A')
    published_dt = datetime.fromisoformat(data['published_at'].replace('Z', '+00:00'))
    publish_date = published_dt.strftime("%Y-%m-%d %H:%M:%S")
    views = format_number(data.get('views', 'N/A'))
    category = data.get('category_name', 'N/A')
    tags = ", ".join(data.get('tags', []))
    description = data.get('description', '')
    content = (f"[TITLE]:       {title}\n[VIDEO ID]:    {video_id}\n[VIDEO URL]:   {video_url}\n"
               f"[PUBLISH DATE]: {publish_date}\n[VIEWS COUNT]: {views}\n[CATEGORY]:    {category}\n\n"
               f"[KEYWORDS (TAGS)]:\n{tags}\n\n[DESCRIPTION]:\n{description}\n")
    return content


# --- 🚀 ФУНКЦИИ СКАЧИВАНИЯ (ЯДРО) ---

async def send_archive(message, file_paths, part_num, total_processed):
    """Создает архив и отправляет его в чат."""
    if not file_paths: return

    zip_filename = f"thumbnails_part_{part_num}.zip"
    try:
        with zipfile.ZipFile(zip_filename, 'w', compression=zipfile.ZIP_STORED) as zipf:
            for file_p in file_paths:
                zipf.write(file_p, arcname=os.path.basename(file_p))

        input_file = FSInputFile(zip_filename)
        caption = f"📁 Архив №{part_num}\n🖼 Картинок: {len(file_paths)}\n(Всего обработано: {total_processed})"
        await message.answer_document(input_file, caption=caption)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка отправки архива №{part_num}: {e}")
    finally:
        if os.path.exists(zip_filename):
            try:
                os.remove(zip_filename)
            except:
                pass
        await asyncio.sleep(1)


async def batch_download_and_send(message: types.Message, channel_url: str, limit: int):
    """
    Основная логика скачивания HD превью.
    """
    # 1. Формируем URL для поиска (убираем лишнее, добавляем /videos если нужно)
    clean_url = channel_url.split('?')[0].rstrip('/')
    if not clean_url.endswith('/videos') and not clean_url.endswith('/shorts'):
        target_url = clean_url + '/videos'
    else:
        target_url = clean_url

    status_msg = await message.answer(f"🔄 Сканирую список видео (лимит: {limit})... Поиск HD картинок...")

    # Настройки yt-dlp
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'playlistend': limit,
        'ignoreerrors': True,
        'no_warnings': True,
        'skip_download': True,
    }

    try:
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(target_url, download=False))

        if 'entries' in info:
            entries = list(info['entries'])
        elif 'url' in info:
            entries = [info]
        else:
            entries = []

        total_found = len(entries)
        if total_found == 0:
            await status_msg.edit_text("❌ Видео не найдены. Возможно, канал пуст.")
            return

        await status_msg.edit_text(f"✅ Найдено видео: {total_found}. Начинаю скачивание HD превью...")

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при поиске: {str(e)}")
        return

    # Настройки пачек
    MAX_ARCHIVE_SIZE = 45 * 1024 * 1024  # 45 МБ
    MAX_FILES_COUNT = 500

    temp_dir = f"temp_thumbs_{message.from_user.id}"
    if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    current_batch_files = []
    current_batch_size = 0
    part_num = 1
    processed_count = 0

    async with aiohttp.ClientSession() as session:
        for index, entry in enumerate(entries):
            video_id = entry.get('id')
            title = entry.get('title', 'video')
            if not video_id: continue

            # Пытаемся скачать HD (maxresdefault)
            targets = [
                f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            ]

            img_data = None
            found_quality = False

            try:
                for url in targets:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()
                            found_quality = True
                            break

                if not found_quality or not img_data: continue

                file_size = len(img_data)

                # Проверка лимитов и отправка пачки
                is_size_limit = (current_batch_size + file_size) > MAX_ARCHIVE_SIZE
                is_count_limit = len(current_batch_files) >= MAX_FILES_COUNT

                if (is_size_limit or is_count_limit) and current_batch_files:
                    await send_archive(message, current_batch_files, part_num, processed_count)

                    for f in current_batch_files:
                        try:
                            os.remove(f)
                        except:
                            pass

                    part_num += 1
                    current_batch_files = []
                    current_batch_size = 0

                    if index % 50 == 0:
                        try:
                            await status_msg.edit_text(f"📦 Обработано {index} из {total_found} (HD качество)...")
                        except:
                            pass

                # Сохранение файла
                safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).strip()
                safe_title = safe_title[:50]
                if not safe_title: safe_title = "img"

                filename = f"{safe_title}_{video_id}.jpg"
                filepath = os.path.join(temp_dir, filename)

                with open(filepath, 'wb') as f:
                    f.write(img_data)

                current_batch_files.append(filepath)
                current_batch_size += file_size
                processed_count += 1

            except Exception:
                continue

        # Отправка остатков
        if current_batch_files:
            await send_archive(message, current_batch_files, part_num, processed_count)

    if os.path.exists(temp_dir): shutil.rmtree(temp_dir)

    try:
        await status_msg.delete()
    except:
        pass

    await message.answer(f"✅ Готово! Скачано в высоком качестве: {processed_count} шт.", parse_mode="HTML")


# --- ОБРАБОТЧИКИ КОМАНД ---

@dp.message(Command("start"))
async def command_start_handler(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "🙋 <b>Привет!</b>\n"
        "<b>Отправь ссылку на видео/канал для анализа.</b>\n\n"
        "<blockquote><b>👇Ниже список моих команд</b></blockquote>\n"
        "<code>/analyze_video</code> — (анализ видео)\n"
        "<code>/analyze_channel</code> — (анализ канала)\n"
        "<code>/get_titles</code> — (все названия)\n"
        "<code>/google_trends</code> — (тренд-запросы)\n"
        "<code>/excel</code> — (сбор в Excel)\n"
        "<code>/download_prev</code> — (скачать превью)\n"
        "<code>/cancel</code> — (отмена)\n"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())
    msg_to_delete = await message.answer(".", reply_markup=ReplyKeyboardRemove())
    await msg_to_delete.delete()


@dp.message(Command("cancel"))
async def command_cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard())


# --- АНАЛИЗ ВИДЕО И КАНАЛОВ ---

async def run_video_analysis(message: types.Message, video_url: str, state: FSMContext):
    msg = await message.answer("🔍 Анализирую видео...")
    data = await youtube_analyzer.analyze_video(video_url)

    if data.get("error"):
        await msg.edit_text(f"❌ Ошибка: {data['error']}")
        await state.clear()
        return

    video_id = data['video_id']

    # --- 1. ПОЛУЧАЕМ ДИЗЛАЙКИ (Return YouTube Dislike API) ---
    dislikes_count = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://returnyoutubedislikeapi.com/votes?videoId={video_id}") as resp:
                if resp.status == 200:
                    ryd_data = await resp.json()
                    dislikes_count = ryd_data.get('dislikes', 0)
    except Exception:
        dislikes_count = 0  # Если API недоступен
    # ---------------------------------------------------------

    # Форматирование даты
    try:
        dt = datetime.fromisoformat(data['published_at'].replace('Z', '+00:00'))
        formatted_date = dt.strftime("%d.%m.%Y %H:%M:%S")
    except:
        formatted_date = "Неизвестно"

    geo_info = await get_country_info(data.get('geo_code', 'N/A'))

    safe_title = html.escape(data['title'])
    safe_desc = html.escape(data.get('description', 'Нет описания'))

    # Обрезаем описание, если оно слишком длинное
    if len(safe_desc) > 800:  # Лимит поменьше, чтобы не засорять чат
        safe_desc = safe_desc[:800] + "... (читать полностью по ссылке)"

    tags = data.get('tags', [])
    safe_tags = html.escape(", ".join(tags)) if tags else "Теги не найдены"
    # Обрезаем теги, если их очень много
    if len(safe_tags) > 500:
        safe_tags = safe_tags[:500] + "..."

    # Сборка сообщения
    lines = [
        f"🎥 <b><a href='{data['url']}'>{safe_title}</a></b>",
        f"├ Время публикации: <code>{formatted_date}</code>",
        f"├ Категория: <code>{data.get('category_name', 'N/A')}</code>"
    ]

    if geo_info:
        lines.append(f"├ ГЕО: {geo_info}")

    # Добавляем дизлайки в строку статистики
    lines.append(
        f"└ 👀: {format_number(data.get('views', 0))} │ "
        f"👍: {format_number(data.get('likes', 0))} │ "
        f"👎: {format_number(dislikes_count)} │ "  # <--- ВОТ ОНИ
        f"💬: {format_number(data.get('comments', 0))}"
    )

    lines.append("")
    lines.append("📝│<b>Описание</b>")
    lines.append(f"<blockquote>{safe_desc}</blockquote>")

    lines.append("")
    lines.append("🏷│<b>Теги</b>")
    lines.append(f"<pre>{safe_tags}</pre>")

    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📥 Метаданные", callback_data=f"download_meta:{video_id}"),
         types.InlineKeyboardButton(text="🖼️ Превью", callback_data=f"download_thumb:{video_id}")]])

    await msg.delete()

    try:
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка вывода: {e}", reply_markup=markup)

    await state.clear()


async def run_channel_analysis(message: types.Message, channel_input: str, state: FSMContext):
    msg = await message.answer("🔍 Анализирую канал...")
    data = await youtube_analyzer.analyze_channel(channel_input)
    if data.get("error"):
        await msg.edit_text(f"❌ Ошибка: {data['error']}")
        await state.clear()
        return

    formatted_date = datetime.fromisoformat(data['published_at'].replace('Z', '+00:00')).strftime("%d.%m.%Y")
    lines = [f"👤 <b>Канал: <a href='{data['url']}'>{html.escape(data['title'])}</a></b>",
             f"├ Создан: <code>{formatted_date}</code>",
             f"├ Видео: <code>{format_number(data.get('video_count', 0))}</code>",
             f"└ Просмотров: <code>{format_number(data.get('view_count', 0))}</code>"]

    buttons = []
    if 'avg_views' in data:
        lines.append("\n❤️ <b>Здоровье (по 10 видео):</b>")
        lines.append(f"├ Ср. просмотры: {format_number(data['avg_views'])}")
        lines.append(f"└ ER: {data['er']} %")
        buttons.append(
            types.InlineKeyboardButton(text="📊 График активности", callback_data=f"show_graphs:{data['channel_id']}"))

    buttons.append(
        types.InlineKeyboardButton(text="📅 Теплокарта публикаций", callback_data=f"show_heatmap:{data['channel_id']}"))
    markup = types.InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    await state.clear()


# --- ОБРАБОТЧИКИ КОМАНД (ПРОДОЛЖЕНИЕ) ---

@dp.message(Command("analyze_video"))
async def cmd_analyze_video(message: types.Message, state: FSMContext):
    await message.answer("🔗 Вставьте ссылку на видео:")
    await state.set_state(UserStates.waiting_for_video_link)


@dp.message(Command("analyze_channel"))
async def cmd_analyze_channel(message: types.Message, state: FSMContext):
    await message.answer("🔗 Вставьте ссылку на канал:")
    await state.set_state(UserStates.waiting_for_channel_link)


@dp.message(Command("get_titles"))
async def cmd_get_titles(message: types.Message, state: FSMContext):
    await message.answer("🔗 Ссылка на канал для выгрузки названий:")
    await state.set_state(UserStates.waiting_for_all_titles_link)


@dp.message(Command("google_trends"))
async def cmd_trends(message: types.Message, state: FSMContext):
    await message.answer("Введите запрос для трендов:")
    await state.set_state(UserStates.waiting_for_trends_query)


@dp.message(Command("excel"))
async def cmd_excel(message: types.Message, state: FSMContext):
    await message.answer("📊 Введите название для Excel файла:")
    await state.set_state(UserStates.waiting_for_niche_name)


# --- ЛОГИКА СКАЧИВАНИЯ ПРЕВЬЮ (ОБРАБОТЧИКИ) ---

@dp.message(Command("download_prev"))
async def command_download_prev(message: types.Message, state: FSMContext):
    await message.answer("📥 <b>Скачивание HD превью</b>\n🔗 Отправьте ссылку на канал:", parse_mode="HTML")
    await state.set_state(UserStates.waiting_for_thumb_channel)


@dp.message(UserStates.waiting_for_thumb_channel)
async def process_thumb_channel_step(message: types.Message, state: FSMContext):
    channel_input = message.text.strip()
    msg = await message.answer("🔍 Проверяю канал...")
    channel_data = await youtube_analyzer.analyze_channel(channel_input)

    if channel_data.get("error"):
        await msg.edit_text(f"❌ Ошибка: {channel_data['error']}")
        return

    total_videos = int(channel_data.get('video_count', 0))
    if total_videos == 0:
        await msg.edit_text("❌ Видео не найдены.")
        await state.clear()
        return

    await state.update_data(thumb_channel=channel_input, max_videos=total_videos)
    await msg.delete()
    await message.answer(
        f"✅ Канал: <b>{html.escape(channel_data['title'])}</b>\n📹 Видео: {total_videos}\n🔢 <b>Сколько скачать? (от 1 до {total_videos})</b>",
        parse_mode="HTML")
    await state.set_state(UserStates.waiting_for_thumb_count)


@dp.message(UserStates.waiting_for_thumb_count)
async def process_thumb_count_step(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return

    count = int(message.text)
    data = await state.get_data()
    channel_input = data.get('thumb_channel')
    max_videos = data.get('max_videos', 0)

    if count < 1: count = 1
    if count > max_videos:
        await message.answer(f"⚠️ Всего {max_videos} видео. Скачиваю все.")
        count = max_videos

    await message.answer(f"🚀 Запуск скачивания {count} превью...")
    await state.clear()

    # Запускаем функцию скачивания
    await batch_download_and_send(message, channel_input, count)


# --- CALLBACKS ---

@dp.callback_query(F.data == "analyze_video")
async def cb_analyze_video(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🔗 Вставьте ссылку на видео:")
    await state.set_state(UserStates.waiting_for_video_link)
    await cb.answer()


@dp.callback_query(F.data == "analyze_channel")
async def cb_analyze_channel(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🔗 Вставьте ссылку на канал:")
    await state.set_state(UserStates.waiting_for_channel_link)
    await cb.answer()


@dp.callback_query(F.data == "get_all_titles")
async def cb_get_titles(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🔗 Ссылка на канал:")
    await state.set_state(UserStates.waiting_for_all_titles_link)
    await cb.answer()


@dp.callback_query(F.data == "cmd_trends")
async def cb_trends(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите запрос:")
    await state.set_state(UserStates.waiting_for_trends_query)
    await cb.answer()


@dp.callback_query(F.data == "cmd_excel")
async def cb_excel(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📊 Название файла:")
    await state.set_state(UserStates.waiting_for_niche_name)
    await cb.answer()


@dp.callback_query(F.data.startswith("download_meta:"))
async def cb_dl_meta(cb: types.CallbackQuery):
    video_id = cb.data.split(":")[-1]
    await cb.answer("⏳ Готовлю файл...")
    data = await youtube_analyzer.get_video_data_by_id(video_id)
    if not data.get("error"):
        content = generate_metadata_content(data)
        file = BufferedInputFile(content.encode('utf-8'), filename=f"{video_id}_meta.txt")
        await cb.message.answer_document(file)


@dp.callback_query(F.data.startswith("download_thumb:"))
async def cb_dl_thumb(cb: types.CallbackQuery):
    video_id = cb.data.split(":")[-1]
    await cb.answer("⏳ Загружаю...")
    data = await youtube_analyzer.get_video_data_by_id(video_id)
    if data.get("thumbnail_url"):
        await cb.message.answer_photo(data['thumbnail_url'])


@dp.callback_query(F.data.startswith("show_graphs:"))
async def cb_show_graphs(cb: types.CallbackQuery):
    channel_id = cb.data.split(":")[-1]
    await cb.answer("🎨 Рисую...")
    stats = await youtube_analyzer.get_recent_video_stats(channel_id)
    if not stats.get("error"):
        buf = create_activity_graphs(stats['views_list'], stats['likes_list'], stats['comments_list'])
        if buf: await cb.message.answer_photo(BufferedInputFile(buf.getvalue(), filename="graph.png"))


@dp.callback_query(F.data.startswith("show_heatmap:"))
async def cb_show_heatmap(cb: types.CallbackQuery):
    channel_id = cb.data.split(":")[-1]
    await cb.answer("🔥 Анализирую...")
    data = await youtube_analyzer.get_publication_heatmap_data(channel_id)
    if not data.get("error"):
        buf = create_heatmap_graph(data['grid'])
        if buf: await cb.message.answer_photo(BufferedInputFile(buf.getvalue(), filename="heatmap.png"),
                                              caption=data['report'], parse_mode="HTML")


# --- ОБРАБОТЧИКИ ВВОДА ДАННЫХ (STATES) ---

@dp.message(UserStates.waiting_for_video_link)
async def process_video_link(message: types.Message, state: FSMContext):
    await run_video_analysis(message, message.text, state)


@dp.message(UserStates.waiting_for_channel_link)
async def process_channel_link(message: types.Message, state: FSMContext):
    await run_channel_analysis(message, message.text, state)


@dp.message(UserStates.waiting_for_all_titles_link)
async def process_all_titles(message: types.Message, state: FSMContext):
    msg = await message.answer("⏳ Собираю заголовки...")
    res = await youtube_analyzer.get_all_video_titles(message.text)
    if res.get("error"):
        await msg.edit_text(f"❌ {res['error']}")
        return

    titles = res['titles']
    if not titles:
        await msg.edit_text("Видео не найдены.")
        await state.clear()
        return

    text = f"Всего: {len(titles)}\n\n" + "\n".join(titles)
    file = BufferedInputFile(text.encode('utf-8'), filename=f"titles.txt")
    await msg.delete()
    await message.answer_document(file, caption=f"✅ Готово: {len(titles)}")
    await state.clear()


@dp.message(UserStates.waiting_for_trends_query)
async def process_trends(message: types.Message, state: FSMContext):
    msg = await message.answer("📈 Анализирую...")
    res = await analyze_google_trends(message.text)
    if res.get("error"):
        await msg.edit_text(f"❌ {res['error']}")
        await state.clear()
        return

    photo = BufferedInputFile(res["image"].getvalue(), filename="trend.png")
    await msg.delete()
    await message.answer_photo(photo, caption=f"Топ страна: {res['top_country']}")
    await state.clear()


@dp.message(UserStates.waiting_for_niche_name)
async def process_niche_name(message: types.Message, state: FSMContext):
    await state.update_data(niche_name=message.text, channels=[])
    await message.answer(f"✅ Файл '{message.text}' создан. Отправляйте каналы.",
                         reply_markup=get_niche_analysis_keyboard())
    await state.set_state(UserStates.niche_analysis)


@dp.message(UserStates.niche_analysis, F.text == "💾 Готово и Скачать")
async def finish_excel(message: types.Message, state: FSMContext):
    data = await state.get_data()
    channels = data.get('channels', [])
    if not channels:
        await message.answer("Нет данных.", reply_markup=get_main_keyboard())
        await state.clear()
        return

    msg = await message.answer("⏳ Генерирую Excel...", reply_markup=ReplyKeyboardRemove())
    gen = ExcelGenerator(data['niche_name'])
    for ch in channels: gen.add_channel_data(ch['category'], ch)

    file = BufferedInputFile(gen.save_to_buffer().getvalue(), filename=f"{data['niche_name']}.xlsx")
    await msg.delete()
    await message.answer_document(file, caption="Ваш анализ готов.")
    await state.clear()


@dp.message(UserStates.niche_analysis)
async def process_niche_channel(message: types.Message, state: FSMContext):
    msg = await message.answer("🔍 Анализ...")
    data = await youtube_analyzer.analyze_channel(message.text)
    if data.get("error"):
        await msg.edit_text(f"❌ {data['error']}")
        return

    subs = int(data.get('subscriber_count', 0) or 0)
    cat = 'whales' if subs >= 100000 else 'small' if subs >= 1000 else 'tiny'

    # Собираем данные (упрощенно для примера, полная логика в вашем оригинале была такая же)
    idea_7d = await youtube_analyzer.get_most_popular_video_in_range(data['channel_id'], 7)
    idea_14d = await youtube_analyzer.get_most_popular_video_in_range(data['channel_id'], 14)
    idea_30d = await youtube_analyzer.get_most_popular_video_in_range(data['channel_id'], 30)

    st_data = await state.get_data()
    channels = st_data.get('channels', [])
    channels.append({
        'category': cat, 'name': data['title'], 'url': data['url'], 'subs': subs,
        'views': int(data.get('view_count', 0)), 'idea_7d': idea_7d, 'idea_14d': idea_14d, 'idea_30d': idea_30d
    })
    await state.update_data(channels=channels)
    await msg.edit_text(f"✅ Добавлен: {data['title']}. Всего: {len(channels)}.", parse_mode="HTML")


# --- УМНЫЙ ОБРАБОТЧИК (В САМОМ КОНЦЕ!) ---
@dp.message(F.text, StateFilter(None))
async def auto_detect_handler(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if youtube_analyzer._extract_video_id(text):
        await run_video_analysis(message, text, state)
    elif youtube_analyzer._extract_channel_info(text):
        await run_channel_analysis(message, text, state)
    else:
        await message.answer("Не распознал ссылку. Используйте меню.")


# --- ЗАПУСК ---
async def start_web_server():
    port = int(os.getenv("PORT", 8000))
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Alive"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"🌐 Server on {port}")


async def main():
    logging.info("🚀 Bot started")
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())