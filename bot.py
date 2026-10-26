import os
import io
import re
import json
import base64
import asyncio
import threading
import uuid
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
import lyricsgenius
import gradio as gr

# ZeroGPU работает только на Hugging Face Spaces — локально просто заглушка
try:
    import spaces

    @spaces.GPU
    def keep_alive():
        return True
except Exception:
    def keep_alive():
        return True


# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GH_TOKEN = os.getenv("GH_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN", "").strip()
GH_REPO = os.getenv("GH_REPO", "Justik54/JT-music-app")
GH_FILE_PATH = os.getenv("GH_FILE_PATH", "tracks.json")
GH_BRANCH = os.getenv("GH_BRANCH", "main")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "1248604556").split(",") if x.strip()]

PLACEHOLDER_COVER = "https://placehold.co/600x600/18202a/2ea5ff?text=JT+Music"

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Genius не обязателен: без токена бот работает, автопоиск просто отключён
genius = None
if GENIUS_TOKEN:
    try:
        genius = lyricsgenius.Genius(
            GENIUS_TOKEN,
            timeout=20,
            retries=2,
            verbose=False,
            remove_section_headers=True,
            skip_non_songs=True,
        )
    except Exception as e:
        print(f"[Genius] не инициализирован: {e}")
else:
    print("[Genius] GENIUS_TOKEN не задан — автопоиск отключён, доступен ручной ввод.")


class TrackForm(StatesGroup):
    waiting_for_search_or_title = State()
    waiting_for_title = State()
    waiting_for_artist = State()
    waiting_for_album = State()
    waiting_for_year = State()
    waiting_for_cover = State()


# ---------------------------------------------------------------- ХОСТИНГИ

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def _url_alive(url: str) -> bool:
    """Проверяем, что ссылка реально отдаёт файл."""
    try:
        r = requests.get(url, headers=UA, stream=True, timeout=15, allow_redirects=True)
        ok = r.status_code == 200 and int(r.headers.get("Content-Length") or 1) > 0
        r.close()
        return ok
    except Exception:
        return False


def _upload_pomf2(data: bytes, ext: str) -> str:
    files = {"files[]": (f"{uuid.uuid4().hex}{ext}", io.BytesIO(data))}
    r = requests.post("https://pomf2.lain.la/upload.php", headers=UA, files=files, timeout=120)
    r.raise_for_status()
    js = r.json()
    if not js.get("success") or not js.get("files"):
        raise RuntimeError("pomf2: пустой ответ")
    return js["files"][0]["url"]


def _upload_catbox(data: bytes, ext: str) -> str:
    files = {"fileToUpload": (f"{uuid.uuid4().hex}{ext}", io.BytesIO(data))}
    r = requests.post(
        "https://catbox.moe/user/api.php",
        headers=UA,
        data={"reqtype": "fileupload"},
        files=files,
        timeout=120,
    )
    r.raise_for_status()
    text = r.text.strip()
    if not text.startswith("http"):
        raise RuntimeError(f"catbox: {text[:80]}")
    return text


def _upload_tmpfiles(data: bytes, ext: str) -> str:
    files = {"file": (f"{uuid.uuid4().hex}{ext}", io.BytesIO(data))}
    r = requests.post("https://tmpfiles.org/api/v1/upload", headers=UA, files=files, timeout=120)
    r.raise_for_status()
    raw = r.json()["data"]["url"]
    return raw.replace("tmpfiles.org/", "tmpfiles.org/dl/")


def upload_file_with_fallback(file_bytes: bytes, ext: str = ".flac") -> str:
    """Pomf2 -> Catbox -> Tmpfiles. Возвращаем первую живую ссылку."""
    errors = []
    for name, fn in (("pomf2", _upload_pomf2), ("catbox", _upload_catbox), ("tmpfiles", _upload_tmpfiles)):
        try:
            url = fn(file_bytes, ext)
            if not url or not url.startswith("http"):
                raise RuntimeError("пустая ссылка")
            if _url_alive(url):
                return url
            raise RuntimeError("ссылка не открывается")
        except Exception as e:
            errors.append(f"{name}: {e}")

    raise RuntimeError("Все хостинги отказали -> " + " | ".join(errors))


# ---------------------------------------------------------------- GITHUB

def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_github_tracks():
    """Возвращает (список треков, sha). Если файла нет — ([], None)."""
    if not GH_TOKEN:
        raise RuntimeError("Не задан GH_TOKEN")

    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE_PATH}"
    r = requests.get(url, headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30)

    if r.status_code == 404:
        return [], None
    if r.status_code != 200:
        raise RuntimeError(f"GitHub GET {r.status_code}: {r.text[:200]}")

    js = r.json()
    content = base64.b64decode(js["content"]).decode("utf-8")
    tracks = json.loads(content) if content.strip() else []
    if not isinstance(tracks, list):
        tracks = []
    return tracks, js["sha"]


def update_github_tracks(tracks_data, sha, message: str = "Add track via bot") -> bool:
    """Сохраняет tracks.json. При конфликте sha — перечитывает и пробует снова."""
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE_PATH}"
    body = json.dumps(tracks_data, ensure_ascii=False, indent=2)

    for attempt in range(3):
        payload = {
            "message": message,
            "content": base64.b64encode(body.encode("utf-8")).decode("utf-8"),
            "branch": GH_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(url, headers=_gh_headers(), json=payload, timeout=30)
        if r.status_code in (200, 201):
            return True

        # 409 — кто-то успел изменить файл, обновляем sha и повторяем
        if r.status_code in (409, 422) and attempt < 2:
            try:
                _, sha = get_github_tracks()
            except Exception:
                pass
            continue

        print(f"[GitHub] PUT {r.status_code}: {r.text[:300]}")
        return False

    return False


# ---------------------------------------------------------------- GENIUS

def clean_lyrics(raw: str) -> str:
    if not raw:
        return ""
    text = raw
    idx = text.rfind("Embed")
    if idx > 0:
        text = text[:idx]
    lines = text.split("\n")
    if lines and "lyrics" in lines[0].lower():
        lines = lines[1:]
    return "\n".join(lines).strip()


def genius_search(query: str, artist: str = ""):
    """Возвращает объект Song (с текстом) или dict (без текста). None если не найдено."""
    if not genius:
        return None

    try:
        song = genius.search_song(query, artist) if artist else genius.search_song(query)
        if song:
            return song
    except Exception as e:
        print(f"[Genius] search_song: {e}")

    try:
        res = genius.search_songs(query)
        hits = (res or {}).get("hits") or []
        if hits:
            return genius.song(hits[0]["result"]["id"])
    except Exception as e:
        print(f"[Genius] search_songs: {e}")

    return None


def genius_from_url(url: str):
    """Достаёт трек по ссылке вида https://genius.com/Artist-title-lyrics."""
    m = re.search(r"genius\.com/([^/?#]+)", url)
    if not m:
        return None

    slug = m.group(1)
    slug = re.sub(r"-(lyrics|translation|romanized)$", "", slug)
    words = slug.replace("-", " ").strip()
    if not words:
        return None

    return genius_search(words)


def build_track(song, audio_url: str, duration: int, fallback_title: str = "") -> dict:
    """Приводим Song/dict к единому формату tracks.json."""
    if isinstance(song, dict):
        if "result" in song:
            song = song["result"]
        if isinstance(song.get("song"), dict):
            song = song["song"]

        title = song.get("title") or fallback_title or "Без названия"
        artist = (song.get("primary_artist") or {}).get("name") or "Неизвестный исполнитель"
        album = (song.get("album") or {}).get("name") or "Сингл"
        release = song.get("release_date") or ""
        year = str(release)[:4] if release else ""
        cover = song.get("song_art_image_url") or PLACEHOLDER_COVER
        lyrics = ""
    else:
        title = getattr(song, "title", None) or fallback_title or "Без названия"
        artist = getattr(song, "artist", None) or "Неизвестный исполнитель"
        album = getattr(song, "album", None) or "Сингл"
        year_raw = getattr(song, "year", None)
        year = str(year_raw)[:4] if year_raw else ""
        cover = getattr(song, "song_art_image_url", None) or PLACEHOLDER_COVER
        lyrics = clean_lyrics(getattr(song, "lyrics", "") or "")

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "year": year,
        "lyrics": lyrics,
        "src": audio_url,
        "cover": cover,
        "duration": int(duration or 0),
    }


async def save_track(track: dict) -> bool:
    """Добавляет трек в tracks.json на GitHub (в отдельном потоке)."""
    def _work():
        tracks, sha = get_github_tracks()
        # защита от дублей по ссылке на аудио
        tracks = [t for t in tracks if t.get("src") != track["src"]]
        tracks.append(track)
        return update_github_tracks(tracks, sha, f"Add: {track['artist']} - {track['title']}")

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:
        print(f"[save_track] {e}")
        return False


# ---------------------------------------------------------------- ХЕНДЛЕРЫ

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("🎧 <b>Привет!</b> Слушай музыку в плеере — кнопка внизу слева ⬇️")
        return
    await message.answer(
        "👋 <b>Привет, админ!</b>\n\n"
        "Отправь файл <code>.flac</code> / <code>.mp3</code> — добавлю трек в плеер 🎵"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("🚫 Добавление трека отменено.")


@dp.message(F.audio | F.document)
async def handle_audio(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "⛔ У тебя нет доступа к загрузке.\nОткрой плеер кнопкой внизу слева 🎧"
        )
        return

    doc = message.audio or message.document
    raw_name = doc.file_name or "track.flac"
    ext = os.path.splitext(raw_name)[1].lower()

    if ext not in (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".aac", ".opus"):
        return

    status_msg = await message.answer("⏳ <i>Скачиваю и выгружаю в хранилище...</i>")

    try:
        file_info = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        audio_bytes = buf.getvalue()

        if len(audio_bytes) > 200 * 1024 * 1024:
            await status_msg.edit_text("❌ Файл больше 200 МБ — не пройдёт.")
            return

        audio_url = await asyncio.to_thread(upload_file_with_fallback, audio_bytes, ext)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Ошибка выгрузки:</b>\n<code>{e}</code>")
        return

    clean_guess = os.path.splitext(raw_name)[0]
    for tag in ["(без мата)", "[без мата]", "(clean)", "[clean]", "(prod.", "(Prod."]:
        clean_guess = clean_guess.replace(tag, "")
    clean_guess = clean_guess.strip()

    await state.update_data(
        audio_url=audio_url,
        duration=getattr(doc, "duration", 0) or 0,
        guess_query=clean_guess,
    )

    kb = InlineKeyboardBuilder()
    if genius:
        kb.button(text="🔍 Найти в Genius", callback_data="search_genius")
    kb.button(text="✏️ Ввести вручную", callback_data="manual_input")
    kb.adjust(1)

    hint = "Подтянуть обложку, альбом и текст автоматически?" if genius else "Заполним данные вручную."
    await status_msg.edit_text(
        f"✅ <b>Файл выгружен!</b>\n\n"
        f"🎵 <code>{clean_guess}</code>\n\n"
        f"{hint}",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(TrackForm.waiting_for_search_or_title)


@dp.callback_query(F.data == "search_genius")
async def process_genius_search(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    query = (data.get("guess_query") or "").strip()

    await call.message.edit_text(f"🔎 <i>Ищу «{query}»...</i>")

    song = None
    if " - " in query:
        artist_part, title_part = query.split(" - ", 1)
        song = await asyncio.to_thread(genius_search, title_part.strip(), artist_part.strip())
    if not song:
        song = await asyncio.to_thread(genius_search, query)

    if not song:
        await call.message.answer(
            "⚠️ Автопоиск не сработал.\n\n"
            "Пришли <b>ссылку на Genius</b> (<code>https://genius.com/...</code>)\n"
            "или напиши <b>название трека</b> для ручного ввода:"
        )
        await state.set_state(TrackForm.waiting_for_title)
        return

    track = build_track(song, data.get("audio_url"), data.get("duration", 0), query)
    ok = await save_track(track)
    await state.clear()

    if ok:
        await call.message.answer(
            f"🎉 <b>Трек добавлен!</b>\n\n"
            f"🎵 <b>{track['artist']}</b> — <b>{track['title']}</b>\n"
            f"💿 {track['album']}" + (f" ({track['year']})" if track["year"] else "") + "\n"
            f"📜 Текст: {'есть' if track['lyrics'] else 'не найден'}\n"
            f"🔗 <a href='{track['src']}'>Аудио</a>"
        )
    else:
        await call.message.answer("❌ Не смог сохранить в GitHub. Проверь GH_TOKEN и права на запись.")


@dp.callback_query(F.data == "manual_input")
async def process_manual(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("1️⃣ Введи <b>название трека</b>:")
    await state.set_state(TrackForm.waiting_for_title)


@dp.message(TrackForm.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст. Введи название трека:")
        return

    if "genius.com/" in text:
        await message.answer("🔎 <i>Загружаю по ссылке...</i>")
        song = await asyncio.to_thread(genius_from_url, text)

        if song:
            data = await state.get_data()
            track = build_track(song, data.get("audio_url"), data.get("duration", 0), "")
            ok = await save_track(track)
            await state.clear()

            if ok:
                await message.answer(
                    f"🎉 <b>Добавлено по ссылке!</b>\n\n"
                    f"🎵 <b>{track['artist']}</b> — <b>{track['title']}</b>\n"
                    f"💿 {track['album']}\n"
                    f"📜 Текст: {'есть' if track['lyrics'] else 'не найден'}"
                )
            else:
                await message.answer("❌ Не смог сохранить в GitHub.")
            return

        await message.answer(
            "⚠️ По ссылке ничего не нашёл. Введи название вручную:\n"
            "<i>(или /cancel)</i>"
        )
        return

    await state.update_data(title=text)
    await message.answer("2️⃣ Кто <b>исполнитель</b>?")
    await state.set_state(TrackForm.waiting_for_artist)


@dp.message(TrackForm.waiting_for_artist)
async def process_artist(message: types.Message, state: FSMContext):
    await state.update_data(artist=(message.text or "").strip() or "Неизвестный исполнитель")
    await message.answer("3️⃣ Какой <b>альбом</b>? <i>(или «-»)</i>")
    await state.set_state(TrackForm.waiting_for_album)


@dp.message(TrackForm.waiting_for_album)
async def process_album(message: types.Message, state: FSMContext):
    val = (message.text or "").strip()
    await state.update_data(album="" if val == "-" else val or "Сингл")
    await message.answer("4️⃣ Какой <b>год выпуска</b>? <i>(или «-»)</i>")
    await state.set_state(TrackForm.waiting_for_year)


@dp.message(TrackForm.waiting_for_year)
async def process_year(message: types.Message, state: FSMContext):
    val = (message.text or "").strip()
    await state.update_data(year="" if val == "-" else val)
    await message.answer("5️⃣ Отправь <b>обложку</b> картинкой:")
    await state.set_state(TrackForm.waiting_for_cover)


@dp.message(TrackForm.waiting_for_cover, F.photo)
async def process_cover(message: types.Message, state: FSMContext):
    status_msg = await message.answer("⏳ <i>Загружаю обложку...</i>")
    data = await state.get_data()

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=buf)

    try:
        cover_url = await asyncio.to_thread(upload_file_with_fallback, buf.getvalue(), ".jpg")
    except Exception:
        cover_url = PLACEHOLDER_COVER

    track = {
        "title": data.get("title") or "Без названия",
        "artist": data.get("artist") or "Неизвестный исполнитель",
        "album": data.get("album") or "Сингл",
        "year": data.get("year", ""),
        "lyrics": "",
        "src": data.get("audio_url"),
        "cover": cover_url,
        "duration": int(data.get("duration", 0) or 0),
    }

    ok = await save_track(track)
    await state.clear()

    if ok:
        await status_msg.edit_text(
            f"🎉 <b>Трек добавлен!</b>\n\n"
            f"🎵 <b>{track['artist']}</b> — <b>{track['title']}</b>\n"
            f"💿 {track['album']}"
        )
    else:
        await status_msg.edit_text("❌ Не смог сохранить в GitHub.")


@dp.message(TrackForm.waiting_for_cover)
async def process_cover_fallback(message: types.Message):
    await message.answer("Нужна именно картинка (фото). Отправь обложку:")


# ---------------------------------------------------------------- ЗАПУСК

_bot_started = False


def run_bot():
    asyncio.run(dp.start_polling(bot, handle_signals=False, drop_pending_updates=True))


def start_bot_once():
    global _bot_started
    if _bot_started:
        return
    _bot_started = True
    threading.Thread(target=run_bot, daemon=True).start()


start_bot_once()


with gr.Blocks(title="JT Music Bot") as demo:
    gr.Markdown("# 🎵 JT Music Bot запущен\nБот работает в фоне. Этот экран нужен только чтобы Space не засыпал.")


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("PORT", "7860")),
    )
