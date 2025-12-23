# youtube_analyzer.py

import asyncio
import datetime
import re

import httpx
import numpy as np
from googleapiclient.discovery import build

from config import YOUTUBE_API_KEY
import zipfile
import io

class YouTubeAnalyzer:
    """
    Класс для взаимодействия с YouTube Data API v3
    и сторонним API 'Return YouTube Dislike'.
    """

    def __init__(self):
        # Инициализация сервиса YouTube API
        self.youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)

        # Клиент для API Return YouTube Dislike
        self.ryd_client = httpx.AsyncClient(
            base_url="https://returnyoutubedislikeapi.com",
            timeout=5.0
        )

    # --- Утилитарные функции для извлечения ID ---

    def _extract_video_id(self, url: str) -> str | None:
        if not url or not isinstance(url, str):
            return None
        # Ограничиваем длину URL для предотвращения атак
        if len(url) > 2048:
            return None
        # Очищаем URL от потенциально опасных символов
        url = url.strip()
        match_standard = re.search(r'(?<=v=)[a-zA-Z0-9_-]+', url)
        if match_standard: return match_standard.group(0)
        match_short = re.search(r'youtu\.be/([a-zA-Z0-9_-]+)', url)
        if match_short: return match_short.group(1)
        match_shorts = re.search(r'/shorts/([a-zA-Z0-9_-]+)', url)
        if match_shorts: return match_shorts.group(1)
        return None

    def _extract_channel_info(self, text_input: str) -> dict | None:
        if not text_input or not isinstance(text_input, str):
            return None
        # Ограничиваем длину для предотвращения атак
        if len(text_input) > 2048:
            return None
        # Очищаем от потенциально опасных символов
        text_input = text_input.strip()
        match_raw_handle = re.fullmatch(r'@([a-zA-Z0-9_.-]+)', text_input)
        if match_raw_handle: return {'type': 'search_query', 'value': match_raw_handle.group(1)}
        match_id = re.search(r'/channel/([a-zA-Z0-9_-]+)', text_input)
        if match_id: return {'type': 'id', 'value': match_id.group(1)}
        match_user = re.search(r'/user/([a-zA-Z0-9_-]+)', text_input)
        if match_user: return {'type': 'username', 'value': match_user.group(1)}
        match_handle = re.search(r'/@([a-zA-Z0-9_.-]+)', text_input)
        if match_handle: return {'type': 'search_query', 'value': match_handle.group(1)}
        match_custom = re.search(r'/c/([a-zA-Z0-9_.-]+)', text_input)
        if match_custom: return {'type': 'search_query', 'value': match_custom.group(1)}
        if not (text_input.startswith('http') or text_input.startswith('www.') or '/' in text_input):
            clean_input = text_input.replace('@', '').strip()
            if clean_input and len(clean_input) <= 100:  # Дополнительная проверка длины
                return {'type': 'search_query', 'value': clean_input}
        return None

    # --- Функционал "Аналитика видео" ---

    async def _get_ryd_dislikes(self, video_id: str) -> str:
        try:
            response = await self.ryd_client.get(f"/votes?videoId={video_id}")
            response.raise_for_status()
            data = response.json()
            dislikes = data.get('dislikes', 'N/A')
            return str(dislikes) if isinstance(dislikes, int) else 'N/A'
        except Exception:
            return 'N/A'

    async def _get_category_name(self, category_id: str) -> str:
        try:
            request = self.youtube.videoCategories().list(part="snippet", regionCode="US")
            response = request.execute()
            for item in response['items']:
                if item['id'] == category_id: return item['snippet']['title']
            return "Неизвестно"
        except Exception:
            return "Ошибка загрузки категории"

    def _get_best_thumbnail_url(self, thumbnails: dict) -> str | None:
        if 'maxres' in thumbnails: return thumbnails['maxres']['url']
        if 'standard' in thumbnails: return thumbnails['standard']['url']
        if 'high' in thumbnails: return thumbnails['high']['url']
        if 'medium' in thumbnails: return thumbnails['medium']['url']
        if 'default' in thumbnails: return thumbnails['default']['url']
        return None

    async def get_video_data_by_id(self, video_id: str) -> dict | None:
        if not video_id or not isinstance(video_id, str) or len(video_id) > 11:
            return {"error": "Неверный ID видео."}
        # Проверяем формат ID видео (должен соответствовать YouTube ID)
        import string
        valid_chars = set(string.ascii_letters + string.digits + '_-')
        if not all(c in valid_chars for c in video_id):
            return {"error": "Неверный формат ID видео."}
        try:
            request = self.youtube.videos().list(part="snippet,statistics", id=video_id)
            response = request.execute()
            if not response.get('items'):
                return {"error": "Видео не найдено или недоступно."}
            item = response['items'][0]
            snippet = item['snippet']
            stats = item.get('statistics', {})
            geo_info = snippet.get('countryCode', 'N/A')
            dislike_count = await self._get_ryd_dislikes(video_id)
            thumbnail_url = self._get_best_thumbnail_url(snippet.get('thumbnails', {}))
            # Очищаем потенциально опасные данные
            title = snippet['title'] if isinstance(snippet['title'], str) else 'N/A'
            description = snippet['description'] if isinstance(snippet['description'], str) else 'N/A'
            tags = snippet.get('tags', []) if isinstance(snippet.get('tags'), list) else []
            # Ограничиваем длину данных
            title = title[:500] if len(title) > 500 else title
            description = description[:2000] if len(description) > 2000 else description
            tags = [tag[:100] for tag in tags if isinstance(tag, str)][:50]  # Максимум 50 тегов по 100 символов
            data = {
                "title": title, "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": snippet['publishedAt'], "category_id": snippet['categoryId'],
                "description": description, "tags": tags,
                "geo_code": geo_info, "views": stats.get('viewCount', '0'),
                "likes": stats.get('likeCount', '0'), "dislikes": dislike_count,
                "comments": stats.get('commentCount', '0'), "thumbnail_url": thumbnail_url
            }
            category_name = await self._get_category_name(data['category_id'])
            data['category_name'] = category_name
            return data
        except Exception as e:
            return {"error": f"Ошибка при обращении к YouTube API: {str(e)}"}

    async def analyze_video(self, video_url: str) -> dict | None:
        if not video_url or not isinstance(video_url, str):
            return {"error": "Неверный формат ссылки на видео."}
        # Ограничиваем длину URL
        if len(video_url) > 2048:
            return {"error": "Слишком длинная ссылка на видео."}
        video_id = self._extract_video_id(video_url)
        if not video_id: 
            return {"error": "Не удалось найти ID видео в ссылке. Проверьте формат."}
        return await self.get_video_data_by_id(video_id)

    # --- "Аналитика канала" ---

    async def _get_channel_id_by_search(self, query: str) -> str | None:
        try:
            request = self.youtube.search().list(part="snippet", q=query, type="channel", maxResults=1)
            response = request.execute()
            if response.get('items'): return response['items'][0]['snippet']['channelId']
            return None
        except Exception:
            return None

    async def _get_uploads_playlist_id(self, channel_id: str) -> str | None:
        """Вспомогательная функция для получения ID плейлиста 'Uploads'."""
        try:
            request_details = self.youtube.channels().list(
                part="contentDetails",
                id=channel_id
            )
            response_details = request_details.execute()
            if not response_details.get('items'):
                return None
            return response_details['items'][0]['contentDetails'].get('relatedPlaylists', {}).get('uploads')
        except Exception:
            return None

    async def get_recent_video_stats(self, channel_id: str) -> dict:
        """
        Собирает статистику (просмотры, лайки, комменты)
        по 10 последним видео для "Здоровья канала".
        """
        uploads_playlist_id = await self._get_uploads_playlist_id(channel_id)
        if not uploads_playlist_id:
            return {"error": "У канала нет плейлиста загрузок."}

        request_videos = self.youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=10
        )
        response_videos = request_videos.execute()
        video_ids = [item['contentDetails']['videoId'] for item in response_videos.get('items', [])]

        if not video_ids: return {"error": "На канале нет недавних видео."}

        request_stats = self.youtube.videos().list(part="statistics", id=",".join(video_ids))
        response_stats = request_stats.execute()

        views_list, likes_list, comments_list = [], [], []
        for video_stat in response_stats.get('items', []):
            stats = video_stat.get('statistics', {})
            views_list.append(int(stats.get('viewCount', 0)))
            likes_list.append(int(stats.get('likeCount', 0)))
            comments_list.append(int(stats.get('commentCount', 0)))

        if not views_list: return {"error": "Не удалось собрать статистику по видео."}

        return {"views_list": views_list, "likes_list": likes_list, "comments_list": comments_list}

    async def analyze_channel(self, channel_input: str) -> dict | None:
        """
        Получает и обрабатывает ГЛУБОКУЮ статистику для конкретного канала.
        """
        channel_info = self._extract_channel_info(channel_input)
        if not channel_input or not isinstance(channel_input, str):
            return {
                "error": "Неверный формат данных. Ожидается строка."}
        # Ограничиваем длину входных данных
        if len(channel_input) > 2048:
            return {
                "error": "Слишком длинный запрос. Пожалуйста, сократите ввод."}
        channel_info = self._extract_channel_info(channel_input)
        if not channel_info:
            return {
                "error": "Не удалось распознать формат. Введите ссылку на канал, псевдоним (@vdud) или просто название."}

        try:
            request_args = {"part": "snippet,statistics"}
            channel_id = None
            if channel_info['type'] == 'id':
                # Проверяем формат ID канала
                if not isinstance(channel_info['value'], str) or len(channel_info['value']) > 50:
                    return {"error": "Неверный формат ID канала."}
                request_args['id'] = channel_info['value']
                channel_id = channel_info['value']
            elif channel_info['type'] == 'username':
                # Проверяем формат имени пользователя
                if not isinstance(channel_info['value'], str) or len(channel_info['value']) > 50:
                    return {"error": "Неверный формат имени пользователя."}
                request_args['forUsername'] = channel_info['value']
            elif channel_info['type'] == 'search_query':
                # Проверяем формат поискового запроса
                if not isinstance(channel_info['value'], str) or len(channel_info['value']) > 100:
                    return {"error": "Неверный формат поискового запроса."}
                channel_id = await self._get_channel_id_by_search(channel_info['value'])
                if not channel_id:
                    return {"error": f"Не удалось найти канал по имени '{channel_info['value']}'. "}
                request_args['id'] = channel_id

            request = self.youtube.channels().list(**request_args)
            response = request.execute()
            if not response.get('items'): return {"error": "Канал не найден или недоступен."}

            item = response['items'][0]
            snippet, stats = item['snippet'], item.get('statistics', {})
            if not channel_id: channel_id = item['id']

            # Очищаем и валидируем полученные данные
            title = snippet['title'] if isinstance(snippet['title'], str) else 'N/A'
            title = title[:200] if len(title) > 200 else title  # Ограничиваем длину названия

            data = {
                "channel_id": channel_id, "title": title,
                "url": f"https://www.youtube.com/channel/{channel_id}",
                "published_at": snippet['publishedAt'],
                "video_count": stats.get('videoCount', '0'),
                "view_count": stats.get('viewCount', '0'),
                "subscriber_count": stats.get('subscriberCount', '0')
            }

            health_data = await self.get_recent_video_stats(channel_id)

            if 'error' not in health_data:
                num_videos = len(health_data['views_list'])
                total_views = sum(health_data['views_list'])
                total_likes = sum(health_data['likes_list'])
                total_comments = sum(health_data['comments_list'])
                data['avg_views'] = int(total_views / num_videos)
                data['avg_likes'] = int(total_likes / num_videos)
                data['avg_comments'] = int(total_comments / num_videos)
                data[
                    'er'] = f"{((total_likes + total_comments) / total_views) * 100:.2f}" if total_views > 0 else "0.00"

            return data

        except Exception as e:
            return {"error": f"Ошибка при обращении к YouTube API: {e}"}

    # ⭐️⭐️⭐️ ФУНКЦИЯ ДЛЯ ТЕПЛОКАРТЫ ⭐️⭐️⭐️
    async def get_publication_heatmap_data(self, channel_id: str) -> dict:
        try:
            uploads_playlist_id = await self._get_uploads_playlist_id(channel_id)
            if not uploads_playlist_id:
                return {"error": "У канала нет плейлиста загрузок."}

            request_videos = self.youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=50
            )
            response_videos = request_videos.execute()

            items = response_videos.get('items', [])
            if not items:
                return {"error": "На канале нет недавних видео."}

            grid = np.zeros((7, 24), dtype=int)
            day_map = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

            for item in items:
                pub_str = item['snippet']['publishedAt']
                dt = datetime.datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                weekday = dt.weekday()
                hour = dt.hour
                grid[weekday, hour] += 1

            max_idx = np.unravel_index(np.argmax(grid), grid.shape)
            report_day = day_map[max_idx[0]]
            report_hour = f"{max_idx[1]:02d}:00 - {max_idx[1] + 1:02d}:00"

            report = (
                f"<b>Отчет по 50 последним видео:</b>\n"
                f"├ <b>Самый частый день:</b> {report_day}\n"
                f"└ <b>Самое \"горячее\" время (UTC):</b> {report_hour}"
            )

            return {
                "grid": grid,
                "report": report
            }
        except Exception as e:
            return {"error": f"Ошибка при сборе данных для теплокарты: {e}"}

    # ⭐️⭐️⭐️ ФУНКЦИЯ ДЛЯ EXCEL ⭐️⭐️⭐️
    async def get_most_popular_video_in_range(self, channel_id: str, days_ago: int) -> str:
        try:
            start_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
            published_after = start_date.isoformat()
            request = self.youtube.search().list(
                part="snippet", channelId=channel_id,
                publishedAfter=published_after, order="viewCount",
                type="video", maxResults=1
            )
            response = request.execute()
            if response.get('items'):
                video_id = response['items'][0]['id']['videoId']
                return f"https://youtu.be/{video_id}"
            else:
                return "N/A"
        except Exception:
            return "Ошибка API"

    # ⭐️⭐️⭐️ НОВАЯ ФУНКЦИЯ: СБОР ВСЕХ НАЗВАНИЙ ⭐️⭐️⭐️
    async def get_all_video_titles(self, channel_input: str) -> dict:
        """
        Собирает названия ВСЕХ видео с канала через пагинацию.
        Возвращает список строк (названий).
        """
        # 1. Получаем ID канала
        channel_info = self._extract_channel_info(channel_input)
        if not channel_info:
            return {"error": "Неверная ссылка или ID канала."}

        channel_id = None
        if channel_info['type'] == 'id':
            channel_id = channel_info['value']
        else:
            try:
                if channel_info['type'] == 'username':
                    req = self.youtube.channels().list(part="id", forUsername=channel_info['value'])
                    resp = req.execute()
                    if resp.get('items'):
                        channel_id = resp['items'][0]['id']
                
                # Если не нашли по username или это search query
                if not channel_id:
                    channel_id = await self._get_channel_id_by_search(channel_info['value'])
            except Exception as e:
                return {"error": f"Ошибка поиска канала: {e}"}

        if not channel_id:
            return {"error": "Канал не найден."}

        # 2. Получаем ID плейлиста "Uploads"
        uploads_id = await self._get_uploads_playlist_id(channel_id)
        if not uploads_id:
            return {"error": "Не удалось найти плейлист загрузок."}

        # 3. Цикл по всем страницам (Pagination)
        all_titles = []
        next_page_token = None
        
        try:
            while True:
                request = self.youtube.playlistItems().list(
                    part="snippet",
                    playlistId=uploads_id,
                    maxResults=50, # Максимум за 1 запрос
                    pageToken=next_page_token
                )
                response = request.execute()
                
                items = response.get('items', [])
                if not items:
                    break

                for item in items:
                    title = item['snippet']['title']
                    all_titles.append(title)

                next_page_token = response.get('nextPageToken')
                
                # Если токена следующей страницы нет, мы дошли до конца
                if not next_page_token:
                    break
                    
                # Маленькая пауза
                await asyncio.sleep(0.05)

            # Получаем название канала для имени файла (опционально, доп. запрос)
            channel_title = f"Channel_{channel_id}"

            return {
                "channel_title": channel_title,
                "titles": all_titles
            }

        except Exception as e:
            return {"error": f"Ошибка при сборе видео: {e}"}

    # ⭐️⭐️⭐️ НОВАЯ ФУНКЦИЯ: СКАЧИВАНИЕ ПРЕВЬЮ В ZIP ⭐️⭐️⭐️
    async def download_thumbnails_zip(self, channel_input: str, limit: int) -> dict:
        """
        Скачивает N последних превью с канала и упаковывает их в ZIP-архив в памяти.
        """
        # 1. Получаем ID канала
        channel_info = self._extract_channel_info(channel_input)
        if not channel_info:
            return {"error": "Неверная ссылка или ID канала."}

        channel_id = None
        # Логика определения ID (копируем из get_all_video_titles или вызываем helper)
        if channel_info['type'] == 'id':
            channel_id = channel_info['value']
        else:
            try:
                if channel_info['type'] == 'username':
                    req = self.youtube.channels().list(part="id", forUsername=channel_info['value'])
                    resp = req.execute()
                    if resp.get('items'):
                        channel_id = resp['items'][0]['id']
                if not channel_id:
                    channel_id = await self._get_channel_id_by_search(channel_info['value'])
            except Exception as e:
                return {"error": f"Ошибка поиска канала: {e}"}

        if not channel_id:
            return {"error": "Канал не найден."}

        # 2. Получаем ID плейлиста "Uploads"
        uploads_id = await self._get_uploads_playlist_id(channel_id)
        if not uploads_id:
            return {"error": "Не удалось найти плейлист загрузок."}

        # 3. Подготовка к скачиванию
        zip_buffer = io.BytesIO()
        next_page_token = None
        videos_processed = 0

        # Создаем ZIP файл в памяти
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            try:
                while videos_processed < limit:
                    # Вычисляем сколько осталось скачать
                    remaining = limit - videos_processed
                    # MaxResults API = 50. Берем минимум между 50 и остатком.
                    fetch_count = min(50, remaining)

                    request = self.youtube.playlistItems().list(
                        part="snippet",
                        playlistId=uploads_id,
                        maxResults=fetch_count,
                        pageToken=next_page_token
                    )
                    response = request.execute()

                    items = response.get('items', [])
                    if not items:
                        break

                    # Асинхронно скачиваем картинки для текущей пачки
                    for item in items:
                        snippet = item['snippet']
                        title = snippet['title']
                        # Очищаем название файла от недопустимых символов
                        safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c == ' ']).strip()
                        safe_title = safe_title[:50]  # Обрезаем, если слишком длинное

                        # Получаем URL лучшего качества
                        thumb_url = self._get_best_thumbnail_url(snippet.get('thumbnails', {}))

                        if thumb_url:
                            # Скачиваем байты картинки
                            try:
                                r = await self.ryd_client.get(thumb_url)  # Используем существующий клиент httpx
                                if r.status_code == 200:
                                    # Добавляем в архив: имя файла, данные
                                    file_name = f"{videos_processed + 1:03d}_{safe_title}.jpg"
                                    zip_file.writestr(file_name, r.content)
                            except Exception:
                                pass  # Пропускаем, если картинка битая

                        videos_processed += 1
                        if videos_processed >= limit:
                            break

                    next_page_token = response.get('nextPageToken')
                    if not next_page_token:
                        break

                    await asyncio.sleep(0.05)

            except Exception as e:
                return {"error": f"Ошибка при скачивании: {e}"}

        # Возвращаем буфер, перемотанный в начало
        zip_buffer.seek(0)
        safe_channel_name = f"thumbnails_{channel_id}"
        return {
            "buffer": zip_buffer,
            "filename": f"{safe_channel_name}.zip",
            "count": videos_processed
        }