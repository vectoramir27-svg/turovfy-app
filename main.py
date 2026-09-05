import os
import sqlite3
import json
import re
import asyncio
from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
import yt_dlp
from ytmusicapi import YTMusic

app = FastAPI(title="TurovFy Audio Core")

os.makedirs("assets", exist_ok=True)
if os.path.exists("assets"):
    app.mount("/assets", StaticFiles(directory="assets"), name="assets")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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

async def resolve_audio_url(video_id: str) -> str:
    if video_id in STREAM_CACHE:
        return STREAM_CACHE[video_id]

    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 10,
    }

    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get('url')

    try:
        loop = asyncio.get_event_loop()
        target_url = await loop.run_in_executor(None, extract)
        if target_url:
            STREAM_CACHE[video_id] = target_url
            return target_url
    except Exception as e:
        print(f"yt-dlp extraction error for {video_id}: {e}")

    # Запасной шлюз, если yt-dlp заблокирован на хостинге
    fallback_url = f"https://inv.nadeko.net/latest_version?id={video_id}&itag=140"
    STREAM_CACHE[video_id] = fallback_url
    return fallback_url

@app.get("/")
async def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "TurovFy Backend Active"}

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
            if not vid:
                continue
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
            if not vid:
                continue
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

@app.get("/api/listen/{video_id}")
async def listen_track(video_id: str, request: Request):
    target_url = await resolve_audio_url(video_id)
    
    range_header = request.headers.get("range", None)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    try:
        req = client.build_request("GET", target_url, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream upstream error: {e}")

    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": resp.headers.get("content-type", "audio/mp4"),
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*"
    }
    if "content-range" in resp.headers:
        response_headers["Content-Range"] = resp.headers["content-range"]
    if "content-length" in resp.headers:
        response_headers["Content-Length"] = resp.headers["content-length"]

    return StreamingResponse(stream_generator(), status_code=resp.status_code, headers=response_headers)

@app.get("/api/prefetch/{video_id}")
async def prefetch_track(video_id: str):
    asyncio.create_task(resolve_audio_url(video_id))
    return {"status": "ok"}
