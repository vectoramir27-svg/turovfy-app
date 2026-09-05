import os
import time
import sqlite3
import json
import re
import asyncio
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from ytmusicapi import YTMusic
import yt_dlp

app = FastAPI(title="TurovFy Core")

os.makedirs("assets", exist_ok=True)
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "turovfy.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            name TEXT,
            picture TEXT,
            playlists TEXT,
            state TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

ytmusic = YTMusic()
STREAM_CACHE = {}

YTDL_OPTS = {
    "format": "ba[ext=m4a]/ba/b",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "no_check_formats": True,
    "socket_timeout": 8,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web"],
            "player_skip": ["js", "configs", "webpage"]
        }
    }
}

def enhance_cover_quality(raw_url: str, video_id: str = "") -> str:
    if not raw_url:
        return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    if "=w" in raw_url and "-h" in raw_url:
        raw_url = re.sub(r'=w\d+-h\d+[^=]*$', '=w800-h800-l90-rj', raw_url)
    elif "=s" in raw_url:
        raw_url = re.sub(r'=s\d+[^=]*$', '=s800', raw_url)
    elif "hqdefault.jpg" in raw_url:
        raw_url = raw_url.replace("hqdefault.jpg", "maxresdefault.jpg")
    return raw_url

def extract_direct_url(video_id: str) -> str:
    now = time.time()
    if video_id in STREAM_CACHE and STREAM_CACHE[video_id]["expires"] > now:
        return STREAM_CACHE[video_id]["url"]

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(video_url, download=False)
        stream_url = info.get("url") if info else None

        if not stream_url and info and "formats" in info:
            audio_formats = [
                f for f in info["formats"]
                if f.get("acodec") != "none" and f.get("vcodec") == "none"
            ]
            audio_formats.sort(key=lambda x: x.get("abr") or 0, reverse=True)
            if audio_formats:
                stream_url = audio_formats[0].get("url")

        if not stream_url:
            raise HTTPException(status_code=404, detail="Audio stream not found")

        STREAM_CACHE[video_id] = {"url": stream_url, "expires": now + 14400}
        return stream_url

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

@app.get("/favicon.ico")
async def favicon():
    if os.path.exists("assets/logo.png"):
        return FileResponse("assets/logo.png")
    return Response(status_code=204)

class UserAuthPayload(BaseModel):
    email: str
    name: str
    picture: str

class SyncPayload(BaseModel):
    email: str
    playlists: dict
    state: dict

@app.post("/api/user/auth")
async def user_auth(user: UserAuthPayload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT playlists, state FROM users WHERE email = ?", (user.email,))
    row = cur.fetchone()

    if not row:
        default_playlists = json.dumps({"Любимое": []})
        default_state = json.dumps({
            "currentTrack": None,
            "currentTime": 0,
            "volume": 1.0,
            "eqBands": [0, 0, 0, 0, 0],
            "activePreset": "flat"
        })
        cur.execute(
            "INSERT INTO users (email, name, picture, playlists, state) VALUES (?, ?, ?, ?, ?)",
            (user.email, user.name, user.picture, default_playlists, default_state)
        )
        conn.commit()
        conn.close()
        return {
            "playlists": json.loads(default_playlists),
            "state": json.loads(default_state)
        }

    conn.close()
    return {
        "playlists": json.loads(row[0]) if row[0] else {"Любимое": []},
        "state": json.loads(row[1]) if row[1] else {}
    }

@app.post("/api/user/sync")
async def sync_data(data: SyncPayload):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET playlists = ?, state = ? WHERE email = ?",
        (json.dumps(data.playlists), json.dumps(data.state), data.email)
    )
    conn.commit()
    conn.close()
    return {"status": "synced"}

@app.get("/api/search")
async def search_tracks(query: str):
    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, lambda: ytmusic.search(query=query, filter="songs", limit=20))
        tracks = []
        for item in results:
            vid = item.get("videoId")
            thumbnails = item.get("thumbnails", [])
            raw_cover = thumbnails[-1]["url"] if thumbnails else None
            cover = enhance_cover_quality(raw_cover, vid)
            artists = ", ".join([a["name"] for a in item.get("artists", [])])

            tracks.append({
                "id": vid,
                "title": item.get("title"),
                "artist": artists,
                "duration": item.get("duration"),
                "cover": cover,
            })
        return {"results": tracks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/artist")
async def get_artist_page(query: str):
    try:
        loop = asyncio.get_event_loop()
        artist_search = await loop.run_in_executor(None, lambda: ytmusic.search(query=query, filter="artists", limit=1))
        artist_name = query
        artist_thumb = ""

        if artist_search:
            artist_item = artist_search[0]
            artist_name = artist_item.get("artist", query)
            thumbs = artist_item.get("thumbnails", [])
            if thumbs:
                artist_thumb = enhance_cover_quality(thumbs[-1]["url"])

        songs_search = await loop.run_in_executor(None, lambda: ytmusic.search(query=f"{artist_name}", filter="songs", limit=40))
        tracks = []
        clean_target = artist_name.lower().strip()

        for s in songs_search:
            artists_list = [a["name"] for a in s.get("artists", [])]
            artists_str = ", ".join(artists_list)
            title = s.get("title", "")

            matches_artist = any(clean_target in a.lower() for a in artists_list) or (clean_target in title.lower())
            if not matches_artist:
                continue

            vid = s.get("videoId")
            thumbs = s.get("thumbnails", [])
            raw_cover = thumbs[-1]["url"] if thumbs else None
            cover = enhance_cover_quality(raw_cover, vid)

            tracks.append({
                "id": vid,
                "title": title,
                "artist": artists_str,
                "duration": s.get("duration"),
                "cover": cover
            })

        if not artist_thumb and tracks:
            artist_thumb = tracks[0]["cover"]

        return {
            "name": artist_name,
            "avatar": artist_thumb,
            "tracks": tracks
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/lyrics")
async def get_track_lyrics(track: str, artist: str):
    try:
        async with httpx.AsyncClient(timeout=3.5) as client:
            res = await client.get(
                "https://lrclib.net/api/get",
                params={"track_name": track, "artist_name": artist}
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("syncedLyrics"):
                    return {"type": "synced", "lyrics": data["syncedLyrics"]}
                elif data.get("plainLyrics"):
                    return {"type": "plain", "lyrics": data["plainLyrics"]}

            search_res = await client.get(
                "https://lrclib.net/api/search",
                params={"q": f"{artist} {track}"}
            )
            if search_res.status_code == 200:
                items = search_res.json()
                if items:
                    for item in items:
                        if item.get("syncedLyrics"):
                            return {"type": "synced", "lyrics": item["syncedLyrics"]}
                        elif item.get("plainLyrics"):
                            return {"type": "plain", "lyrics": item["plainLyrics"]}

        return {"type": "none", "lyrics": "Текст песни отсутствует."}
    except Exception:
        return {"type": "none", "lyrics": "Текст песни отсутствует."}

# Молниеносный 307 редирект на прямой поток Google CDN
@app.get("/api/listen/{video_id}")
async def listen_track(video_id: str):
    try:
        loop = asyncio.get_event_loop()
        direct_url = await loop.run_in_executor(None, extract_direct_url, video_id)
        return RedirectResponse(url=direct_url, status_code=307)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/prefetch/{video_id}")
async def prefetch_track(video_id: str):
    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, extract_direct_url, video_id)
        return {"status": "prefetching"}
    except Exception:
        return {"status": "ignored"}
