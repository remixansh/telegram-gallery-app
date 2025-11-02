import os
from dotenv import load_dotenv
from pathlib import Path
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
# Import necessary Telethon exceptions
from telethon.errors.rpcerrorlist import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon import TelegramClient
from telethon.tl.types import InputMessagesFilterPhotos, Channel
from telethon.tl.functions.channels import CreateChannelRequest, GetFullChannelRequest, DeleteChannelRequest
from typing import List, Dict
import asyncio
import io

load_dotenv()
# --- FastAPI App Initialization ---
app = FastAPI(title="Telegram Gallery API")

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- App Configuration & Directory Setup ---
TG_API_ID = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")

if not TG_API_ID or not TG_API_HASH:
    raise ValueError("Please set TG_API_ID and TG_API_HASH environment variables.")

MEDIA_DIR = Path("media")
THUMB_DIR = MEDIA_DIR / "thumbs"
UPLOADS_DIR = Path("uploads") 
MEDIA_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

client = TelegramClient("telegram_session", int(TG_API_ID), TG_API_HASH)

login_state: Dict[str, str] = {}


app_group_cache: List[dict] = []
cache_lock = asyncio.Lock()
cache_populated = False


app.mount("/media", StaticFiles(directory="media"), name="media")

# --- NEW Authentication Endpoints ---

@app.get("/api/auth/status")
async def get_auth_status():
    """Checks if the client is connected and authorized."""
    if not client.is_connected():
        await client.connect()
    is_authorized = await client.is_user_authorized()
    return {"is_logged_in": is_authorized}

# UPDATED: Improved error handling for login code requests.
@app.post("/api/login/send-code")
async def send_login_code(data: dict):
    """Initiates the login process by sending an OTP code to the user's phone."""
    phone_number = data.get("phone")
    if not phone_number:
        raise HTTPException(status_code=400, detail="Phone number is required.")
    
    try:
        if not client.is_connected():
            await client.connect()
        result = await client.send_code_request(phone_number)
        login_state["phone_code_hash"] = result.phone_code_hash
        login_state["phone"] = phone_number
        return {"status": "code_sent", "message": "OTP has been sent to your Telegram account."}

    except FloodWaitError as e:
        # Handle cases where the user is trying too frequently.
        raise HTTPException(status_code=429, detail=f"You are trying too often. Please wait {e.seconds} seconds.")

    except Exception as e:
        error_message = str(e)
        # Handle the specific ResendCodeRequest error from the user's report.
        if "ResendCodeRequest" in error_message:
            raise HTTPException(status_code=400, detail="A code was recently sent. Please check Telegram or wait a moment.")
        
        raise HTTPException(status_code=500, detail=f"Failed to send code: {error_message}")


@app.post("/api/login/verify")
async def verify_login(data: dict):
    """Verifies the OTP and password (if needed) to complete the login."""
    code = data.get("code")
    password = data.get("password")
    phone = login_state.get("phone")
    phone_code_hash = login_state.get("phone_code_hash")

    if not all([code, phone, phone_code_hash]):
        raise HTTPException(status_code=400, detail="Missing code, phone, or hash. Please start over.")

    try:
        if not client.is_connected():
            await client.connect()
        
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        
        login_state.clear()
        return {"status": "login_successful"}

    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="The OTP code you entered is invalid.")
    
    except SessionPasswordNeededError:
        if not password:
            return {"status": "password_needed", "message": "Two-factor authentication is enabled. Please enter your password."}
        
        try:
            await client.sign_in(password=password)
            login_state.clear()
            return {"status": "login_successful"}
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Failed to log in with password: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")

@app.post("/api/logout")
async def logout():
    """Logs the current user out, deletes the session, and prepares for a new login."""
    global client, cache_populated
    
    if client and client.is_connected():
        await client.log_out()

    client = TelegramClient("telegram_session", int(TG_API_ID), TG_API_HASH)
    
    login_state.clear()
    async with cache_lock:
        app_group_cache.clear()
        cache_populated = False
        
    return {"status": "logged_out"}


# --- Helper for protected endpoints ---
async def check_auth():
    """Checks if the client is authorized before allowing an action."""
    status = await get_auth_status()
    if not status.get("is_logged_in"):
        raise HTTPException(status_code=401, detail="User is not logged in.")

# --- API Endpoints (with auth checks) ---
# FIX: Updated get_photos to support pagination (offset, limit) and has_more flag
@app.get("/api/photos")
async def get_photos(chat: str, limit: int = 50, offset: int = 0):
    await check_auth()
    photos_data = []
    try:
        try:
            chat_entity_input = int(chat)
        except ValueError:
            chat_entity_input = chat

        entity = await client.get_entity(chat_entity_input)
        numeric_chat_id = entity.id

        # Fetch limit + 1 messages to determine if there are more pages
        messages_to_check = await client.get_messages(
            entity,
            limit=limit + 1,
            add_offset=offset,
            filter=InputMessagesFilterPhotos()
        )

        has_more = len(messages_to_check) > limit
        messages_to_process = messages_to_check[:limit]

        for message in messages_to_process:
            if message.photo:
                photo_id = str(message.id)
                unique_thumb_filename = f"{numeric_chat_id}_{photo_id}.jpg"
                thumb_image_path = THUMB_DIR / unique_thumb_filename

                if not thumb_image_path.exists():
                    try:
                        await message.download_media(thumb=1, file=str(thumb_image_path))
                    except Exception as e:
                        print(f"Could not download thumbnail for {unique_thumb_filename}: {e}")
                        continue
                
                photos_data.append({"id": photo_id, "thumb_url": f"/media/thumbs/{unique_thumb_filename}"})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
    
    return {"photos": photos_data, "has_more": has_more}


@app.get("/api/photos/{message_id}/full", response_class=StreamingResponse)
async def get_full_photo(message_id: int, chat: str = Query(...)):
    await check_auth()
    try:
        chat_entity = int(chat)
    except ValueError:
        chat_entity = chat
    try:
        message = await client.get_messages(chat_entity, ids=message_id)
        if not message or not message.photo:
            raise HTTPException(status_code=404, detail="Photo not found.")
        buffer = io.BytesIO()
        await message.download_media(file=buffer)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="image/jpeg")
    except Exception as e:
        print(f"Error streaming full photo {message_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload")
async def upload_photo(chat: str = Query(...), file: UploadFile = File(...)):
    await check_auth()
    try:
        chat_entity = int(chat)
    except ValueError: chat_entity = chat
    filepath = UPLOADS_DIR / file.filename
    try:
        with open(filepath, "wb") as f:
            content = await file.read()
            f.write(content)
        await client.send_file(chat_entity, filepath, caption="Uploaded from web gallery 🌐")
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
    return {"status": "success", "message": f"Successfully uploaded {file.filename}."}


@app.post("/api/groups")
async def create_group(group_data: dict):
    await check_auth()
    global cache_populated
    title = group_data.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Group title is required.")
    try:
        result = await client(CreateChannelRequest(title=title, about="Created via Web Gallery App", megagroup=True))
        created_channel = result.chats[0]
        async with cache_lock:
            app_group_cache.insert(0, {"id": created_channel.id, "title": created_channel.title})
        return {"status": "success", "group_id": created_channel.id, "group_title": created_channel.title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create group: {str(e)}")

@app.get("/api/my-groups")
async def get_my_groups(offset: int = 0, limit: int = 15, populate_cache_only: bool = False):
    await check_auth()
    global cache_populated, app_group_cache
    async with cache_lock:
        if not cache_populated:
            print("Cache is empty. Populating groups cache...")
            app_group_cache.clear()
            try:
                temp_groups = []
                async for dialog in client.iter_dialogs():
                    if isinstance(dialog.entity, Channel) and dialog.entity.megagroup:
                        try:
                            full_channel = await client(GetFullChannelRequest(channel=dialog.entity))
                            if "Created via Web Gallery App" in full_channel.full_chat.about:
                                temp_groups.append({"id": dialog.id, "title": dialog.name})
                        except Exception:
                            continue
                app_group_cache = temp_groups
                cache_populated = True
                print(f"Cache populated with {len(app_group_cache)} groups.")
            except Exception as e:
                cache_populated = False; app_group_cache.clear()
                if populate_cache_only: return
                raise HTTPException(status_code=500, detail=f"Failed to build groups cache: {str(e)}")
    if populate_cache_only: return
    paginated_groups = app_group_cache[offset : offset + limit]
    has_more = len(app_group_cache) > offset + limit
    return {"groups": paginated_groups, "has_more": has_more}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int):
    await check_auth()
    global cache_populated
    try:
        await client(DeleteChannelRequest(channel=group_id))
        async with cache_lock:
            app_group_cache[:] = [g for g in app_group_cache if g.get("id") != group_id]
        return {"status": "success", "message": f"Group {group_id} has been deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete group: {str(e)}")


@app.delete("/api/photos/{message_id}")
async def delete_photo(message_id: int, chat: str = Query(...)):
    await check_auth()
    try:
        try:
            chat_entity_input = int(chat)
        except ValueError:
            chat_entity_input = chat

        entity = await client.get_entity(chat_entity_input)
        numeric_chat_id = entity.id
        
        await client.delete_messages(entity, [message_id])
        
        photo_id = str(message_id)

        unique_thumb_filename = f"{numeric_chat_id}_{photo_id}.jpg"
        thumb_image_path = THUMB_DIR / unique_thumb_filename

        if thumb_image_path.exists():
            os.remove(thumb_image_path)
            
        return {"status": "success", "message": f"Photo {message_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete photo {message_id}: {str(e)}")

# --- Static File Serving ---
@app.get("/", response_class=FileResponse, include_in_schema=False)
async def read_index():
    return "frontend/static/index.html"
