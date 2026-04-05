import re
import logging
import asyncio
from datetime import datetime
from collections import defaultdict
from plugins.Dreamxfutures.Imdbposter import get_movie_detailsx, fetch_image, get_movie_details
from database.users_chats_db import db
from pyrogram import Client, filters, enums
from info import CHANNELS, MOVIE_UPDATE_CHANNEL, LINK_PREVIEW, ABOVE_PREVIEW, BAD_WORDS, LANDSCAPE_POSTER, TMDB_POSTER
from Script import script
from database.ia_filterdb import save_file
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils import temp
from pymongo.errors import PyMongoError, DuplicateKeyError
from pyrogram.errors import MessageIdInvalid, MessageNotModified, FloodWait
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Precomputed sets for faster lookups
IGNORE_WORDS = {
    "rarbg", "dub", "sub", "sample", "mkv", "aac", "combined",
    "action", "adventure", "animation", "biography", "comedy", "crime", 
    "documentary", "drama", "fantasy", "film-noir", "history", 
    "horror", "music", "musical", "mystery", "romance", "sci-fi", "sport", 
    "thriller", "war", "western", "hdcam", "hdtc", "camrip", "ts", "tc", 
    "telesync", "dvdscr", "dvdrip", "predvd", "webrip", "web-dl", "tvrip", 
    "hdtv", "web dl", "webdl", "bluray", "brrip", "bdrip", "360p", "480p", 
    "720p", "1080p", "2160p", "4k", "1440p", "540p", "240p", "140p", "hevc", 
    "hdrip", "hin", "hindi", "tam", "tamil", "kan", "kannada", "tel", "telugu", 
    "mal", "malayalam", "eng", "english", "pun", "punjabi", "ben", "bengali", 
    "mar", "marathi", "guj", "gujarati", "urd", "urdu", "kor", "korean", "jpn", 
    "japanese", "nf", "netflix", "sonyliv", "sony", "sliv", "amzn", "prime", 
    "primevideo", "hotstar", "zee5", "jio", "jhs", "aha", "hbo", "paramount", 
    "apple", "hoichoi", "sunnxt", "viki"
} | BAD_WORDS

CAPTION_LANGUAGES = {
    "hin": "Hindi", "hindi": "Hindi", "tam": "Tamil", "tamil": "Tamil",
    "kan": "Kannada", "kannada": "Kannada", "tel": "Telugu", "telugu": "Telugu",
    "mal": "Malayalam", "malayalam": "Malayalam", "eng": "English", "english": "English",
}

OTT_PLATFORMS = {
    "nf": "Netflix", "netflix": "Netflix", "sonyliv": "SonyLiv", "sony": "SonyLiv",
    "amzn": "Amazon Prime Video", "prime": "Amazon Prime Video", "hotstar": "Disney+ Hotstar",
}

STANDARD_GENRES = {
    'Action', 'Adventure', 'Animation', 'Biography', 'Comedy', 'Crime', 'Documentary',
    'Drama', 'Family', 'Fantasy', 'Film-Noir', 'History', 'Horror', 'Music',
    'Musical', 'Mystery', 'Romance', 'Sci-Fi', 'Sport', 'Thriller', 'War', 'Western'
}

CLEAN_PATTERN = re.compile(r'@[^ \n\r\t\.,:;!?()\[\]{}<>\\/"\'=_%]+|\bwww\.[^\s\]\)]+|\([\@^]+\)|\[[\@^]+\]')
NORMALIZE_PATTERN = re.compile(r"[._]+|[()\[\]{}:;'–!,.?_]")
QUALITY_PATTERN = re.compile(r"\b(?:HDCam|HDTC|CamRip|TS|TC|TeleSync|DVDScr|DVDRip|WEBRip|WEB-DL|BluRay|BRRip|BDRip|360p|480p|720p|1080p|2160p|4K|HEVC|HDRip)\b", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])")
RANGE_REGEX = re.compile(r'\bS(\d{1,2})[^\w\n\r]*E(?:p(?:isode)?)?0*(\d{1,2})\s*(?:to|-)\s*(?:E(?:p(?:isode)?)?)?0*(\d{1,2})',re.IGNORECASE)
SINGLE_REGEX = re.compile(r'\bS(\d{1,2})[^\w\n\r]*E(?:p(?:isode)?)?0*(\d{1,3})', re.IGNORECASE)
NAMED_REGEX = re.compile(r'Season\s*0*(\d{1,2})[\s\-,:]*Ep(?:isode)?\s*0*(\d{1,3})', re.IGNORECASE)
EP_ONLY_RANGE = re.compile(r'\b(?:EP|Episode)0*(\d{1,3})\s*-\s*0*(\d{1,3})\b',re.IGNORECASE)

MEDIA_FILTER = filters.document | filters.video | filters.audio
locks = defaultdict(asyncio.Lock)
pending_updates = {}

def clean_mentions_links(text: str) -> str:
    return CLEAN_PATTERN.sub("", text or "").strip()

def normalize(s: str) -> str:
    s = NORMALIZE_PATTERN.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

def remove_ignored_words(text: str) -> str:
    lower_ignored = {w.lower() for w in IGNORE_WORDS}
    return " ".join(word for word in text.split() if word.lower() not in lower_ignored)

def get_qualities(text: str) -> str:
    qualities = QUALITY_PATTERN.findall(text)
    return ", ".join(qualities) if qualities else "N/A"

def extract_ott_platform(text: str) -> str:
    text = text.lower()
    platforms = {plat for key, plat in OTT_PLATFORMS.items() if key in text}
    return " | ".join(platforms) if platforms else "N/A"

def extract_season_episode(filename: str) -> Tuple[Optional[int], Optional[str]]:
    if m := EP_ONLY_RANGE.search(filename):
        return 1, f"{int(m.group(1))}-{int(m.group(2))}"
    for pattern in (RANGE_REGEX, SINGLE_REGEX, NAMED_REGEX):
        if m := pattern.search(filename):
            season = int(m.group(1))
            ep = f"{m.group(2)}-{m.group(3)}" if pattern == RANGE_REGEX else m.group(2)
            return season, ep
    return None, None

def extract_media_info(filename: str, caption: str):
    filename = normalize(clean_mentions_links(filename).title())
    caption_clean = clean_mentions_links(caption).lower() if caption else ""
    unified = f"{caption_clean} {filename.lower()}".strip()

    season = episode = year = None
    tag = "#MOVIE"
    processed_raw = base_raw = filename
    quality = get_qualities(caption_clean) or get_qualities(filename.lower()) or "N/A"
    ott_platform = extract_ott_platform(f"{filename} {caption_clean}")

    lang_keys = {k for k in CAPTION_LANGUAGES if k in caption_clean or k in filename.lower()}
    language = ", ".join(sorted({CAPTION_LANGUAGES[k] for k in lang_keys})) if lang_keys else "N/A"

    season, episode = extract_season_episode(filename)
    if season is not None:
        tag = "#SERIES"
        if m := (RANGE_REGEX.search(filename) or SINGLE_REGEX.search(filename) or NAMED_REGEX.search(filename) or EP_ONLY_RANGE.search(filename)):
            match_str = m.group(0)
            start_idx = filename.lower().find(match_str.lower())
            end_idx = start_idx + len(match_str)
            processed_raw = filename[:end_idx]
            base_raw = filename[:start_idx]
            if year_match := YEAR_PATTERN.search(filename.lower()[end_idx:]):
                y = year_match.group(0)
                yi = filename.lower().find(y, end_idx)
                if yi != -1:
                    processed_raw = filename[:yi+4]
                    base_raw += f" {y}"
    else:
        if year_match := YEAR_PATTERN.search(unified):
            year = year_match.group(0)
            year_idx = filename.lower().find(year.lower())
            if year_idx != -1:
                processed_raw = filename[:year_idx + 4]
                base_raw = processed_raw
        elif qual_match := QUALITY_PATTERN.search(unified):
            qual_idx = filename.lower().find(qual_match.group(0).lower())
            if qual_idx != -1:
                processed_raw = filename[:qual_idx]
                base_raw = processed_raw

    base_name = normalize(remove_ignored_words(normalize(base_raw)))
    if year and year not in base_name:
        base_name += f" {year}"

    def _strip_season_episode_tokens(name: str) -> str:
        if not name: return name
        year_match = re.search(r'\(?\b(19|20)\d{2}\b\)?\s*$', name)
        year_part = year_match.group(0) if year_match else ""
        if year_match: name = name[:year_match.start()].strip()
        
        patterns = [r'\bS\d{1,2}E\d{1,2}\b', r'\bS\d{1,2}\b', r'\bE\d{1,2}\b', r'\bSeason\s*\d{1,2}\b', r'\bPart\s*\d{1,2}\b']
        for p in patterns: name = re.sub(p, ' ', name, flags=re.IGNORECASE)
        name = re.sub(r'[_\.\-]+', ' ', name)
        name = re.sub(r'\s+', ' ', name).strip()
        return f"{name} {year_part}".strip()

    base_name = _strip_season_episode_tokens(base_name)
    return {
        "processed": normalize(processed_raw), "base_name": base_name or filename,
        "tag": tag, "season": season, "episode": episode, "year": year,
        "quality": quality, "ott_platform": ott_platform, "language": language
    }

def schedule_update(bot, base_name, delay=5):
    if handle := pending_updates.get(base_name):
        if not handle.cancelled(): handle.cancel()
    loop = asyncio.get_event_loop()
    pending_updates[base_name] = loop.call_later(delay, lambda: asyncio.create_task(update_movie_message(bot, base_name)))

@Client.on_message(filters.chat(CHANNELS) & MEDIA_FILTER)
async def media_handler(bot, message):
    media = next((getattr(message, ft) for ft in ("document", "video", "audio") if getattr(message, ft, None)), None)
    if not media: return
    media.file_type = next(ft for ft in ("document", "video", "audio") if hasattr(message, ft))
    media.caption = message.caption or ""
    success, _ = await save_file(media)
    if success and await db.movie_update_status(bot.me.id):
        await process_and_send_update(bot, media.file_name, media.caption)

async def process_and_send_update(bot, filename, caption):
    media_info = extract_media_info(filename, caption)
    base_name, processed = media_info["base_name"], media_info["processed"]
    async with locks[base_name]:
        if not hasattr(db, 'movie_updates'): db.movie_updates = db.db.movie_updates
        movie_doc = await db.movie_updates.find_one({"_id": base_name})
        
        file_data = {
            "filename": filename, "processed": processed, "quality": media_info["quality"],
            "language": media_info["language"], "ott_platform": media_info["ott_platform"],
            "timestamp": datetime.now(), "tag": media_info["tag"], "season": media_info["season"], "episode": media_info["episode"]
        }

        if not movie_doc:
            error_tmdb = False
            if TMDB_POSTER:
                details = await get_movie_detailsx(base_name)
                if not details or details.get("error") or (not details.get("poster_url") and not details.get("backdrop_url")):
                    error_tmdb = True
                    details = await get_movie_details(base_name) or {}
            else:
                details = await get_movie_details(base_name) or {}

            raw_genres = details.get("genres", "N/A")
            genres = ", ".join(g.strip() for g in (raw_genres.split(",") if isinstance(raw_genres, str) else raw_genres) if g.strip() in STANDARD_GENRES) or "N/A"
            
            movie_doc = {
                "_id": base_name, "files": [file_data], "genres": genres, "rating": details.get("rating", "N/A"),
                "poster_url": details.get("backdrop_url") if LANDSCAPE_POSTER and TMDB_POSTER and details.get("backdrop_url") and not error_tmdb else details.get("poster_url"),
                "imdb_url": details.get("url", "") if not TMDB_POSTER or error_tmdb else details.get("tmdb_url"),
                "year": media_info["year"] or details.get("year"), "tag": media_info["tag"], "message_id": None, "is_photo": False, "error_tmdb": error_tmdb, "is_backdrop": details.get("backdrop_url")
            }
            await db.movie_updates.insert_one(movie_doc)
            await send_movie_update(bot, base_name)
        elif not any(f["filename"] == filename for f in movie_doc["files"]):
            await db.movie_updates.update_one({"_id": base_name}, {"$push": {"files": file_data}})
            schedule_update(bot, base_name)

async def send_movie_update(bot, base_name):
    movie_doc = await db.movie_updates.find_one({"_id": base_name})
    if not movie_doc: return
    
    text = generate_movie_message(movie_doc, base_name)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("𝗖𝗹𝗶𝗰𝗸 𝗛𝗲𝗿𝗲 𝗧𝗼 𝗚𝗲𝘁 𝗙𝗶𝗹𝗲𝘀 ⬇️", url=f"https://t.me/{temp.U_NAME}?start=getfile-{base_name.replace(' ', '-')}")],
        [InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟭 🎬", url="https://t.me/MalluMoviePort"), InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟮 🎬", url="https://t.me/serieslokam02")],
        [InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟯 🎬", url="https://t.me/+5heDwxgdnfFmYmZl"), InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟰 🎬", url="https://t.me/+f0mS-Xgwc1E4ZDA9")],
        [InlineKeyboardButton("🍿", url="https://t.me/newcinemaupdates"), InlineKeyboardButton("👑", url="https://t.me/HC_Founder"), InlineKeyboardButton("🤖", url="https://t.me/HodyCloud")]
    ])

    try:
        size = (2560, 1440) if LANDSCAPE_POSTER and TMDB_POSTER and movie_doc.get("is_backdrop") and not movie_doc.get("error_tmdb") else (853, 1280)
        if movie_doc.get("poster_url") and not LINK_PREVIEW:
            resized_poster = await fetch_image(movie_doc["poster_url"], size)
            msg = await bot.send_photo(chat_id=MOVIE_UPDATE_CHANNEL, photo=resized_poster, caption=text, reply_markup=buttons)
            is_photo = True
        else:
            msg = await bot.send_message(chat_id=MOVIE_UPDATE_CHANNEL, text=text, reply_markup=buttons, invert_media=ABOVE_PREVIEW if movie_doc.get("poster_url") else False)
            is_photo = False
        
        await db.movie_updates.update_one({"_id": base_name}, {"$set": {"message_id": msg.id, "is_photo": is_photo}})
    except FloodWait as e:
        await asyncio.sleep(e.value + 2)
    except Exception as e:
        logger.error(f"Send update error: {e}")

async def update_movie_message(bot, base_name):
    movie_doc = await db.movie_updates.find_one({"_id": base_name})
    if not movie_doc or not movie_doc.get("message_id"): return

    text = generate_movie_message(movie_doc, base_name)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("𝗖𝗹𝗶𝗰𝗸 𝗛𝗲𝗿𝗲 𝗧𝗼 𝗚𝗲𝘁 𝗙𝗶𝗹𝗲𝘀 ⬇️", url=f"https://t.me/{temp.U_NAME}?start=getfile-{base_name.replace(' ', '-')}")],
        [InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟭 🎬", url="https://t.me/MalluMoviePort"), InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟮 🎬", url="https://t.me/serieslokam02")],
        [InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟯 🎬", url="https://t.me/+5heDwxgdnfFmYmZl"), InlineKeyboardButton("𝗚𝗿𝗼𝘂𝗽 𝟰 🎬", url="https://t.me/+f0mS-Xgwc1E4ZDA9")],
        [InlineKeyboardButton("🍿", url="https://t.me/newcinemaupdates"), InlineKeyboardButton("👑", url="https://t.me/HC_Founder"), InlineKeyboardButton("🤖", url="https://t.me/HodyCloud")]
    ])
    
    try:
        if movie_doc.get("is_photo"):
            await bot.edit_message_caption(chat_id=MOVIE_UPDATE_CHANNEL, message_id=movie_doc["message_id"], caption=text, reply_markup=buttons)
        else:
            await bot.edit_message_text(chat_id=MOVIE_UPDATE_CHANNEL, message_id=movie_doc["message_id"], text=text, reply_markup=buttons)
    except (MessageIdInvalid, MessageNotModified):
        pass
    except Exception as e:
        logger.error(f"Edit update error: {e}")

def generate_movie_message(movie_doc, base_name):
    all_qualities, all_languages, all_ott, all_tags = set(), set(), set(), set()
    episodes_by_season = defaultdict(set)

    for file in movie_doc["files"]:
        if file["quality"] != "N/A": all_qualities.update(q.strip() for q in file["quality"].split(","))
        if file["language"] != "N/A": all_languages.update(l.strip() for l in file["language"].split(","))
        if file["ott_platform"] != "N/A": all_ott.update(p.strip() for p in file["ott_platform"].split("|"))
        if file["tag"]: all_tags.add(file["tag"])
        if file.get("season"): episodes_by_season[file["season"]].add(str(file["episode"]))

    epi_block = ""
    if episodes_by_season:
        lines = [f"S{s}: {', '.join(sorted(list(eps)))}" for s, eps in sorted(episodes_by_season.items())]
        epi_block = f"\n📺 ᴇᴘɪsᴏᴅᴇs: <b>{', '.join(lines)}</b>"

    return script.MOVIE_UPDATE_NOTIFY_TXT.format(
        poster_url=movie_doc.get("poster_url", ""), imdb_url=movie_doc.get("imdb_url", ""),
        filename=base_name, tag="#SERIES" if "#SERIES" in all_tags else "#MOVIE",
        genres=movie_doc.get("genres", "N/A"), ott=", ".join(all_ott) or "N/A",
        quality=", ".join(all_qualities) or "N/A", language=", ".join(all_languages) or "N/A",
        episodes=epi_block, rating=movie_doc.get("rating", "N/A"), search_link=temp.B_LINK
    )
