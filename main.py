from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
import yt_dlp
import os, uuid, requests, json, traceback, base64, tempfile
from io import BytesIO
from supabase import create_client, Client

# --- ENV Variables ---
AUTH_TOKEN = os.getenv("PCLOUD_AUTH_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
YOUTUBE_COOKIES_BASE64 = os.getenv("YOUTUBE_COOKIES")

# --- Initialize ---
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

SONGS_FOLDER = "songs"
IMGS_FOLDER = "imgs"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# --- Cookie file from env ---
def write_temp_cookie_file():
    if not YOUTUBE_COOKIES_BASE64:
        raise Exception("YOUTUBE_COOKIES env var missing")
    cookie_bytes = base64.b64decode(YOUTUBE_COOKIES_BASE64)
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="wb")
    temp.write(cookie_bytes)
    temp.close()
    return temp.name

# --- Folder setup in pCloud ---
def get_or_create_folder(folder_name):
    res = requests.get("https://api.pcloud.com/listfolder", params={"auth": AUTH_TOKEN, "folderid": 0})
    for item in res.json().get("metadata", {}).get("contents", []):
        if item.get("isfolder") and item.get("name") == folder_name:
            return item["folderid"]
    res = requests.get("https://api.pcloud.com/createfolder", params={"auth": AUTH_TOKEN, "name": folder_name, "folderid": 0})
    return res.json()["metadata"]["folderid"]

# --- Upload file to pCloud ---
def upload_file(file_buffer, filename, folder_id):
    file_buffer.seek(0)
    res = requests.post("https://api.pcloud.com/uploadfile", params={"auth": AUTH_TOKEN, "folderid": folder_id}, files={"file": (filename, file_buffer)})
    data = res.json()
    fileid = data["metadata"][0]["fileid"]
    requests.get("https://api.pcloud.com/getfilepublink", params={"auth": AUTH_TOKEN, "fileid": fileid})
    return fileid, filename

# --- Download and extract song + thumbnail ---
def download_audio_and_thumbnail(video_url, cookie_path):
    buffer = BytesIO()
    temp_id = str(uuid.uuid4())

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f"{temp_id}.%(ext)s",
        'quiet': True,
        'cookiefile': cookie_path,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        title = info.get("title", "Unknown")
        thumbnail_url = info.get("thumbnail")
        full_path = f"{temp_id}.mp3"

    filename = f"{title}.mp3"

    with open(full_path, 'rb') as f:
        buffer.write(f.read())
    buffer.seek(0)
    os.remove(full_path)

    return buffer, filename, thumbnail_url, title

def download_thumbnail(url):
    res = requests.get(url)
    if res.status_code == 200:
        return BytesIO(res.content), f"{uuid.uuid4()}.jpg"
    raise Exception("Thumbnail download failed")

# --- Gemini Tagging ---
def get_tags_from_gemini(song_name):
    PREDEFINED_TAGS = {
        "genre": ["pop", "rock", "hiphop", "rap", "r&b", "jazz", "blues", "classical", "electronic", "edm", "house", "techno", "trance", "dubstep", "lofi", "indie", "folk", "country", "metal", "reggae", "latin", "kpop", "jpop", "bhajan", "devotional", "sufi", "instrumental", "soundtrack", "acoustic", "chillstep", "ambient"],
        "mood": ["happy", "sad", "romantic", "chill", "energetic", "dark", "peaceful", "motivational", "angry", "nostalgic", "dreamy", "emotional", "fun", "relaxing", "aggressive", "uplifting", "sensual", "dramatic", "lonely", "hopeful", "spiritual"],
        "occasion": ["party", "workout", "study", "sleep", "meditation", "travel", "roadtrip", "driving", "wedding", "breakup", "background", "cooking", "cleaning", "gaming", "focus", "night", "morning", "rainy_day", "summer_vibes", "monsoon_mood"],
        "era": ["80s", "90s", "2000s", "2010s", "2020s", "oldschool", "vintage", "retro", "modern", "trending", "classic", "timeless", "underground", "viral"],
        "vocal_instrument": ["female_vocals", "male_vocals", "duet", "group", "instrumental_only", "beats_only", "piano", "guitar", "violin", "flute", "drums", "orchestra", "bass", "live", "remix", "acoustic_version", "cover_song", "mashup", "karaoke"]
    }

    prompt = f"""
Given the song name "{song_name}", identify its primary artist and language.
Then, suggest appropriate tags from the predefined categories below.
Use ONLY tags from these predefined lists (do not invent new ones).
Return the output in this exact JSON format:

{{
  "artist": "Artist Name",
  "language": "Language",
  "genre": [...],
  "mood": [...],
  "occasion": [...],
  "era": [...],
  "vocal_instrument": [...]
}}
Predefined tag categories:
{json.dumps(PREDEFINED_TAGS, indent=2)}
"""

    response = requests.post(
        f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]})
    )

    if response.status_code == 200:
        try:
            raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            if raw_text.startswith("```json"):
                raw_text = raw_text.strip("` \n").replace("json", "", 1).strip()
            result = json.loads(raw_text)
            tags = []
            for cat in ["genre", "mood", "occasion", "era", "vocal_instrument"]:
                tags.extend(result.get(cat, []))
            return {
                "artist": result.get("artist", "Unknown"),
                "language": result.get("language", "english"),
                "tags": tags
            }
        except Exception as e:
            raise Exception(f"❌ Error parsing Gemini response: {e}")
    else:
        raise Exception(f"❌ Gemini API Error {response.status_code}: {response.text}")

# --- FastAPI Routes ---
@app.get("/")
def home():
    return {"message": "✅ Render finished loading"}

@app.get("/upload")
def upload(link: str = Query(..., description="YouTube video URL")):
    try:
        cookie_path = write_temp_cookie_file()
        songs_folder_id = get_or_create_folder(SONGS_FOLDER)
        imgs_folder_id = get_or_create_folder(IMGS_FOLDER)

        audio_buffer, audio_filename, thumb_url, song_name = download_audio_and_thumbnail(link, cookie_path)
        thumb_buffer, thumb_filename = download_thumbnail(thumb_url)

        file_id, _ = upload_file(audio_buffer, audio_filename, songs_folder_id)
        img_id, _ = upload_file(thumb_buffer, thumb_filename, imgs_folder_id)

        tag_data = get_tags_from_gemini(song_name)

        supabase.table("songs").insert({
            "file_id": file_id,
            "img_id": img_id,
            "name": song_name,
            "artist": tag_data["artist"],
            "language": tag_data["language"],
            "tags": tag_data["tags"],
            "views": 0,
            "likes": 0
        }).execute()

        audio_buffer.close()
        thumb_buffer.close()
        os.remove(cookie_path)

        return JSONResponse(content={
            "status": "success",
            "file_id": file_id,
            "img_id": img_id,
            "name": song_name,
            "artist": tag_data["artist"],
            "tags": tag_data["tags"]
        })

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"status": "failure", "error": str(e)})
