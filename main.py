from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic
import httpx
import urllib.request
import sqlite3
import json
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ytmusic = YTMusic()

def init_db():
    conn = sqlite3.connect("turovfy.db")
    cursor = conn.cursor()
    cursor.execute("""
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

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "TurovFy Backend Active"

@app.get("/manifest.json")
async def serve_manifest():
    if os.path.exists("manifest.json"):
        with open("manifest.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {"name": "TurovFy"}

@app.get("/api/search")
async def search_tracks(query: str):
    try:
        results = ytmusic.search(query, filter="songs")
        tracks = []
        for r in results:
            video_id = r.get("videoId")
            if not video_id:
                continue
            title = r.get("title")
            artists = r.get("artists", [{"name": "Unknown"}])
            artist_name = artists[0]["name"] if artists else "Unknown"
            thumbnails = r.get("thumbnails", [])
            cover = thumbnails[-1]["url"] if thumbnails else ""
            
            tracks.append({
                "id": video_id,
                "title": title,
                "artist": artist_name,
                "cover": cover
            })
        return {"results": tracks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/artist")
async def get_artist(query: str):
    try:
        search_res = ytmusic.search(query, filter="artists")
        if not search_res:
            return {"name": query, "avatar": "", "tracks": []}
        
        artist_data = search_res[0]
        browse_id = artist_data.get("browseId")
        artist_name = artist_data.get("artist", query)
        thumbnails = artist_data.get("thumbnails", [])
        avatar = thumbnails[-1]["url"] if thumbnails else ""

        tracks = []
        if browse_id:
            artist_page = ytmusic.get_artist(browse_id)
            songs = artist_page.get("songs", {}).get("results", [])
            for s in songs:
                video_id = s.get("videoId")
                if not video_id:
                    continue
                thumbs = s.get("thumbnails", [])
                cover = thumbs[-1]["url"] if thumbs else avatar
                tracks.append({
                    "id": video_id,
                    "title": s.get("title"),
                    "artist": artist_name,
                    "cover": cover
                })

        return {"name": artist_name, "avatar": avatar, "tracks": tracks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/lyrics")
async def get_lyrics(track: str, artist: str):
    try:
        search_res = ytmusic.search(f"{artist} {track}", filter="songs")
        if not search_res:
            return {"lyrics": "Текст не найден", "type": "plain"}
        
        video_id = search_res[0].get("videoId")
        lyrics_data = ytmusic.get_watch_playlist(video_id)
        
        lyrics_id = lyrics_data.get("lyrics")
        if not lyrics_id:
            return {"lyrics": "Текст недоступен", "type": "plain"}

        full_lyrics = ytmusic.get_lyrics(lyrics_id)
        lyrics_text = full_lyrics.get("lyrics", "Текст недоступен")
        
        return {"lyrics": lyrics_text, "type": "plain"}
    except Exception as e:
        return {"lyrics": "Текст недоступен", "type": "plain"}

@app.post("/api/user/auth")
async def user_auth(request: Request):
    data = await request.json()
    email = data.get("email")
    name = data.get("name", "")
    picture = data.get("picture", "")

    conn = sqlite3.connect("turovfy.db")
    cursor = conn.cursor()
    cursor.execute("SELECT playlists, state FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()

    if not row:
        default_playlists = json.dumps({"Любимое": []})
        default_state = json.dumps({})
        cursor.execute(
            "INSERT INTO users (email, name, picture, playlists, state) VALUES (?, ?, ?, ?, ?)",
            (email, name, picture, default_playlists, default_state)
        )
        conn.commit()
        playlists, state = {"Любимое": []}, {}
    else:
        playlists = json.loads(row[0]) if row[0] else {"Любимое": []}
        state = json.loads(row[1]) if row[1] else {}

    conn.close()
    return {"playlists": playlists, "state": state}

@app.post("/api/user/sync")
async def user_sync(request: Request):
    data = await request.json()
    email = data.get("email")
    playlists = data.get("playlists")
    state = data.get("state")

    if not email:
        return {"status": "error"}

    conn = sqlite3.connect("turovfy.db")
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET playlists = ?, state = ? WHERE email = ?",
        (json.dumps(playlists), json.dumps(state), email)
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/listen/{video_id}")
async def listen_track(video_id: str, request: Request):
    invidious_instances = [
        "https://vid.priv.au",
        "https://invidious.perennialte.ch",
        "https://inv.nadeko.net"
    ]
    
    direct_url = None
    async with httpx.AsyncClient(timeout=6) as client:
        for instance in invidious_instances:
            try:
                res = await client.get(f"{instance}/api/v1/videos/{video_id}")
                if res.status_code == 200:
                    data = res.json()
                    adaptive_formats = data.get("adaptiveFormats", [])
                    audio_formats = [f for f in adaptive_formats if "audio" in f.get("type", "")]
                    if audio_formats:
                        audio_formats.sort(key=lambda x: int(x.get("bitrate", 0)), reverse=True)
                        direct_url = audio_formats[0].get("url")
                        if direct_url:
                            break
            except Exception:
                continue

    if not direct_url:
        raise HTTPException(status_code=404, detail="Audio stream unavailable")

    range_header = request.headers.get("range", "bytes=0-")
    req = urllib.request.Request(direct_url)
    req.add_header("Range", range_header)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

    try:
        remote_file = urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CDN connection error: {e}")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": remote_file.headers.get("Content-Type", "audio/mp4"),
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
    }
    if "Content-Range" in remote_file.headers:
        headers["Content-Range"] = remote_file.headers["Content-Range"]
    if "Content-Length" in remote_file.headers:
        headers["Content-Length"] = remote_file.headers["Content-Length"]

    def audio_generator():
        try:
            while True:
                chunk = remote_file.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            remote_file.close()

    status_code = 206 if range_header else 200
    return StreamingResponse(audio_generator(), status_code=status_code, headers=headers)
