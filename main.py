from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import yt_dlp
import os
import uuid
from io import BytesIO
import json
import base64
import tempfile

app = FastAPI()

# 🔐 Hardcoded Drive folder ID (shared with service account)
SHARED_FOLDER_ID = '15qjD_koVrx_aecL9feTOrXAB7GDyjp7H'  # replace with your own folder ID
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# 📁 Get Google Drive service using service account from env
def get_drive_service():
    encoded_creds = os.getenv("GOOGLE_CREDENTIALS")
    if not encoded_creds:
        raise Exception("Missing GOOGLE_CREDENTIALS environment variable")

    creds_json = base64.b64decode(encoded_creds).decode('utf-8')
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)

# 🎧 Download YouTube audio and convert to MP3
def download_audio_to_memory(video_url: str) -> (BytesIO, str):
    buffer = BytesIO()
    temp_id = str(uuid.uuid4())
    filename = f"{temp_id}.mp3"

    # 🔓 Load cookies from env
    encoded_cookies = os.getenv("YOUTUBE_COOKIES")
    if not encoded_cookies:
        raise Exception("Missing YOUTUBE_COOKIES environment variable")

    cookies_text = base64.b64decode(encoded_cookies).decode("utf-8")
    with tempfile.NamedTemporaryFile(delete=False, mode='w+', suffix=".txt") as cookie_file:
        cookie_file.write(cookies_text)
        cookie_file_path = cookie_file.name

    print(f"[INFO] Cookies written to: {cookie_file_path}")

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f"{temp_id}.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'cookiefile': cookie_file_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        full_path = f"{temp_id}.mp3"

    with open(full_path, 'rb') as f:
        buffer.write(f.read())
        buffer.seek(0)

    os.remove(full_path)
    os.remove(cookie_file_path)
    return buffer, filename

# ☁️ Upload audio file to Google Drive
def upload_memory_to_drive(memory_file: BytesIO, filename: str) -> str:
    service = get_drive_service()

    media = MediaIoBaseUpload(memory_file, mimetype='audio/mpeg', resumable=True)
    file_metadata = {
        'name': filename,
        'parents': [SHARED_FOLDER_ID]
    }

    uploaded_file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()

    file_id = uploaded_file['id']

    service.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view"

# 🌐 Health check route
@app.get("/")
def home():
    return {"message": "YouTube to MP3 Uploader is running!"}

# 🚀 Upload endpoint
@app.get("/upload")
def upload(link: str = Query(..., description="YouTube video URL")):
    try:
        memory_file, filename = download_audio_to_memory(link)
        drive_url = upload_memory_to_drive(memory_file, filename)
        memory_file.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse(content={"drive_link": drive_url})
