import os
import io
import asyncio
import jwt
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pathlib import Path
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from telethon.errors.rpcerrorlist import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telethon import TelegramClient
from telethon.sessions import Session
from telethon.crypto import AuthKey
from telethon.tl.types import InputMessagesFilterPhotos, Channel
from telethon.tl.functions.channels import CreateChannelRequest, GetFullChannelRequest, DeleteChannelRequest
from typing import List, Dict, Optional
import uuid
from collections import defaultdict

# --- Load Environment Variables ---
load_dotenv()
TG_API_ID = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")

# --- Firebase Setup ---
SERVICE_ACCOUNT_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "firebase-service-account.json")

if not os.path.exists(SERVICE_ACCOUNT_PATH):
    print(f"Warning: Firebase service account file not found at {SERVICE_ACCOUNT_PATH}.")
else:
    try:
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK Initialized.")
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK: {e}")

# Get the Firestore client
db = firestore.client()

if not all([TG_API_ID, TG_API_HASH, JWT_SECRET_KEY]):
    raise ValueError("Please set TG_API_ID, TG_API_HASH, and JWT_SECRET_KEY environment variables.")

# --- Constants ---
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10080  # 7 days

# --- Directory Setup ---
BASE_DATA_DIR = Path(os.environ.get("RENDER_DISK_MOUNT_PATH", "."))
UPLOADS_DIR = BASE_DATA_DIR / "uploads"
TEMP_SESSIONS_DIR = BASE_DATA_DIR / "temp_sessions"

UPLOADS_DIR.mkdir(exist_ok=True)
TEMP_SESSIONS_DIR.mkdir(exist_ok=True)

# --- Custom Firebase Session for Telethon ---
class FirestoreSession(Session):
    """
    A custom Telethon session class that stores session data in Firestore.
    """
    def __init__(self, firestore_client, user_id):
        super().__init__()
        self.firestore_client = firestore_client
        self.user_id = user_id
        self.collection_name = "telethon_sessions"
        self.doc_ref = self.firestore_client.collection(self.collection_name).document(self.user_id)
        
        # Initialize internal storage
        self._dc_id = 0
        self._server_address = None
        self._port = None
        self._auth_key = None
        self._takeout_id = None
        
        # Load data on initialization
        self.load()

    def load(self):
        try:
            doc = self.doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                self._dc_id = data.get('dc_id', 0)
                self._server_address = data.get('server_address')
                self._port = data.get('port')
                auth_key_data = data.get('auth_key')
                if auth_key_data:
                    self._auth_key = AuthKey(data=auth_key_data)
                self._takeout_id = data.get('takeout_id')
        except Exception as e:
            print(f"Error loading session for {self.user_id}: {e}")

    def save(self):
        """Saves the current session data to Firestore."""
        if self._auth_key:
            data = {
                'dc_id': self._dc_id,
                'server_address': self._server_address,
                'port': self._port,
                'auth_key': self._auth_key.key if self._auth_key else None,
                'takeout_id': self._takeout_id
            }
            try:
                self.doc_ref.set(data)
            except Exception as e:
                print(f"Error saving session for {self.user_id}: {e}")

    # Properties required by Session base class
    @property
    def dc_id(self):
        return self._dc_id
    
    @dc_id.setter
    def dc_id(self, value):
        self._dc_id = value
        self.save()

    @property
    def server_address(self):
        return self._server_address
    
    @server_address.setter
    def server_address(self, value):
        self._server_address = value
        self.save()

    @property
    def port(self):
        return self._port
    
    @port.setter
    def port(self, value):
        self._port = value
        self.save()

    @property
    def auth_key(self):
        return self._auth_key
    
    @auth_key.setter
    def auth_key(self, value):
        if value is not None and not isinstance(value, AuthKey):
            value = AuthKey(data=value)
        self._auth_key = value
        self.save()

    @property
    def takeout_id(self):
        return self._takeout_id
    
    @takeout_id.setter
    def takeout_id(self, value):
        self._takeout_id = value
        self.save()

    # Methods to handle auth key
    def set_dc(self, dc_id, server_address, port):
        self._dc_id = dc_id
        self._server_address = server_address
        self._port = port
        self.save()

    def get_auth_key(self, dc_id=None):
        """Returns the authorization key for the given DC"""
        return self._auth_key

    def set_auth_key(self, auth_key, dc_id=None):
        """Sets the authorization key for the given DC"""
        if auth_key is not None and not isinstance(auth_key, AuthKey):
            auth_key = AuthKey(data=auth_key)
        self._auth_key = auth_key
        self.save()

    # Entity cache methods
    def get_input_entity(self, key):
        """Gets the input entity from cache"""
        return None

    def cache_file(self, md5_digest, file_size, instance):
        """Caches a file"""
        pass

    def get_file(self, md5_digest, file_size):
        """Gets a cached file"""
        return None

    def process_entities(self, tlo):
        """Processes entities to cache them"""
        pass

    def get_update_state(self, entity_id):
        """Gets the update state for an entity"""
        return None

    def set_update_state(self, entity_id, state):
        """Sets the update state for an entity"""
        pass

    def get_update_states(self):
        """Gets all update states"""
        return []

    def delete(self):
        """Deletes the session from Firestore."""
        try:
            self.doc_ref.delete()
        except Exception as e:
            print(f"Error deleting session for {self.user_id}: {e}")

    def close(self):
        """Closes the session"""
        pass


# --- FastAPI App Initialization ---
app = FastAPI(title="Telegram Gallery API (Firebase Sessions)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global State ---
login_attempts: Dict[str, dict] = {}
active_clients: Dict[str, TelegramClient] = {}
user_group_cache: Dict[str, List[dict]] = {}
cache_locks = defaultdict(asyncio.Lock)

# --- JWT & Auth Dependency ---

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login/verify")

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_client(token: str = Depends(oauth2_scheme)) -> TelegramClient:
    """
    FastAPI Dependency: Decodes JWT token, gets user_id, and returns an
    active, authenticated TelegramClient using the FirestoreSession.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired. Please log in again.")
    except jwt.PyJWTError:
        raise credentials_exception

    # Check active client cache first
    if user_id in active_clients:
        client = active_clients[user_id]
        if client.is_connected() and await client.is_user_authorized():
            return client
        del active_clients[user_id]

    # Create new client from Firebase session
    try:
        fs_session = FirestoreSession(db, user_id)
        
        if not fs_session.get_auth_key():
             raise HTTPException(status_code=401, detail="Session not found in DB. Please log in again.")

        client = TelegramClient(fs_session, int(TG_API_ID), TG_API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

        active_clients[user_id] = client
        return client
        
    except Exception as e:
        print(f"Error connecting client for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not connect Telegram client.")


# --- Authentication Endpoints ---

@app.get("/api/auth/status")
async def get_auth_status(client: TelegramClient = Depends(get_current_client)):
    return {"is_logged_in": True}


@app.post("/api/login/send-code")
async def send_login_code(data: dict):
    phone_number = data.get("phone")
    if not phone_number:
        raise HTTPException(status_code=400, detail="Phone number is required.")

    session_id = str(uuid.uuid4())
    temp_session_file = str(TEMP_SESSIONS_DIR / session_id)
    temp_client = TelegramClient(temp_session_file, int(TG_API_ID), TG_API_HASH)

    try:
        await temp_client.connect()
        result = await temp_client.send_code_request(phone_number)
        login_attempts[session_id] = {
            "client": temp_client,
            "phone_code_hash": result.phone_code_hash,
            "phone": phone_number,
            "file": temp_session_file
        }
        return {"status": "code_sent", "message": "OTP has been sent.", "session_id": session_id}
    except FloodWaitError as e:
        await temp_client.disconnect()
        raise HTTPException(status_code=429, detail=f"Trying too often. Please wait {e.seconds} seconds.")
    except Exception as e:
        await temp_client.disconnect()
        raise HTTPException(status_code=500, detail=f"Failed to send code: {e}")


@app.post("/api/login/verify")
async def verify_login(data: dict):
    """
    Step 2 of Login: Verifies code, and on success, copies the session
    data from the temp file session into a new FirestoreSession.
    """
    session_id = data.get("session_id")
    code = data.get("code")
    password = data.get("password")

    attempt = login_attempts.get(session_id)
    if not attempt or not code:
        raise HTTPException(status_code=400, detail="Invalid session or missing code. Please start over.")

    client: TelegramClient = attempt["client"]
    phone = attempt["phone"]
    phone_code_hash = attempt["phone_code_hash"]
    temp_session_file = attempt["file"]

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="The OTP code you entered is invalid.")
    except SessionPasswordNeededError:
        if not password:
            return {"status": "password_needed", "message": "2FA is enabled. Please enter your password."}
        try:
            await client.sign_in(password=password)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Failed to log in with password: {e}")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")

    # Login Successful: Promote temp session to Firebase
    try:
        me = await client.get_me()
        user_id = str(me.id)

        # Create a new Firestore session
        fs_session = FirestoreSession(db, user_id)
        
        # Copy the auth data from the temp client to the new session
        fs_session.set_dc(client.session.dc_id, client.session.server_address, client.session.port)
        fs_session.set_auth_key(client.session.auth_key)
        
        # Disconnect the temp client
        await client.disconnect()
        
        # Create JWT token
        access_token = create_access_token(data={"sub": user_id})

        return {"status": "login_successful", "token": access_token}
    finally:
        # Clean up the temp file session
        if session_id in login_attempts:
            del login_attempts[session_id]
        temp_session_file_path = Path(f"{temp_session_file}.session")
        if temp_session_file_path.exists():
            os.remove(temp_session_file_path)


@app.post("/api/logout")
async def logout(client: TelegramClient = Depends(get_current_client)):
    """Logs out from Telegram and deletes the session from Firestore."""
    try:
        me = await client.get_me()
        user_id = str(me.id)
        
        # Tell Telethon to log out
        await client.log_out() 
        
        # Delete the session from Firestore
        fs_session = FirestoreSession(db, user_id)
        fs_session.delete()

        # Clear local server caches
        if user_id in active_clients: del active_clients[user_id]
        if user_id in user_group_cache: del user_group_cache[user_id]
        
        return {"status": "logged_out"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Logout failed: {e}")


# --- API Endpoints ---

@app.get("/api/photos")
async def get_photos(
    chat: str, 
    limit: int = 50, 
    offset: int = 0, 
    client: TelegramClient = Depends(get_current_client)
):
    """Gets a paginated list of photo IDs for a given chat."""
    photos_data = []
    try:
        try: chat_entity_input = int(chat)
        except ValueError: chat_entity_input = chat

        entity = await client.get_entity(chat_entity_input)

        messages_to_check = await client.get_messages(
            entity, limit=limit + 1, add_offset=offset, filter=InputMessagesFilterPhotos()
        )
        has_more = len(messages_to_check) > limit
        messages_to_process = messages_to_check[:limit]

        for message in messages_to_process:
            if message.photo:
                photo_id = str(message.id)
                photos_data.append({"id": photo_id})
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")
    return {"photos": photos_data, "has_more": has_more}


@app.get("/api/photos/{message_id}/full", response_class=StreamingResponse)
async def get_full_photo(
    message_id: int, 
    chat: str = Query(...), 
    client: TelegramClient = Depends(get_current_client)
):
    """Streams the full-resolution photo."""
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
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/photos/{message_id}/thumb", response_class=StreamingResponse)
async def get_photo_thumb(
    message_id: int, 
    chat: str = Query(...), 
    client: TelegramClient = Depends(get_current_client)
):
    """Streams the thumbnail for a specific photo."""
    try:
        chat_entity = int(chat)
    except ValueError:
        chat_entity = chat
    try:
        message = await client.get_messages(chat_entity, ids=message_id)
        if not message or not message.photo:
            raise HTTPException(status_code=404, detail="Photo not found.")
        
        buffer = io.BytesIO()
        await message.download_media(thumb=1, file=buffer)
        buffer.seek(0)
        
        return StreamingResponse(buffer, media_type="image/jpeg")
    except Exception as e:
        print(f"Error streaming thumb {message_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/upload")
async def upload_photo(
    chat: str = Query(...), 
    file: UploadFile = File(...), 
    client: TelegramClient = Depends(get_current_client)
):
    """Uploads a new photo to the specified chat."""
    try:
        chat_entity = int(chat)
    except ValueError: chat_entity = chat
    
    filepath = UPLOADS_DIR / f"{uuid.uuid4()}_{file.filename}"
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
async def create_group(
    group_data: dict, 
    client: TelegramClient = Depends(get_current_client)
):
    """Creates a new 'album' (Supergroup)."""
    title = group_data.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Group title is required.")
    try:
        result = await client(CreateChannelRequest(title=title, about="Created via Web Gallery App", megagroup=True))
        created_channel = result.chats[0]
        
        me = await client.get_me()
        user_id = str(me.id)
        if user_id in user_group_cache:
            user_group_cache[user_id].insert(0, {"id": created_channel.id, "title": created_channel.title})
            
        return {"status": "success", "group_id": created_channel.id, "group_title": created_channel.title}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create group: {str(e)}")


@app.get("/api/my-groups")
async def get_my_groups(
    offset: int = 0, 
    limit: int = 15, 
    client: TelegramClient = Depends(get_current_client)
):
    """Gets a paginated list of the user's groups created by this app."""
    me = await client.get_me()
    user_id = str(me.id)
    
    lock = cache_locks[user_id]
    async with lock:
        if user_id not in user_group_cache:
            print(f"Cache miss for user {user_id}. Populating groups cache...")
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
                user_group_cache[user_id] = temp_groups
                print(f"Cache for user {user_id} populated with {len(temp_groups)} groups.")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to build groups cache: {str(e)}")
    
    paginated_groups = user_group_cache[user_id][offset : offset + limit]
    has_more = len(user_group_cache[user_id]) > offset + limit
    return {"groups": paginated_groups, "has_more": has_more}


@app.delete("/api/groups/{group_id}")
async def delete_group(
    group_id: int, 
    client: TelegramClient = Depends(get_current_client)
):
    """Deletes a group."""
    try:
        await client(DeleteChannelRequest(channel=group_id))
        
        me = await client.get_me()
        user_id = str(me.id)
        if user_id in user_group_cache:
            user_group_cache[user_id] = [g for g in user_group_cache[user_id] if g.get("id") != group_id]
            
        return {"status": "success", "message": f"Group {group_id} has been deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete group: {str(e)}")


@app.delete("/api/photos/{message_id}")
async def delete_photo(
    message_id: int, 
    chat: str = Query(...), 
    client: TelegramClient = Depends(get_current_client)
):
    """Deletes a specific photo (message) from a chat."""
    try:
        try: chat_entity_input = int(chat)
        except ValueError: chat_entity_input = chat

        entity = await client.get_entity(chat_entity_input)
        await client.delete_messages(entity, [message_id])
            
        return {"status": "success", "message": f"Photo {message_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete photo {message_id}: {str(e)}")


# --- Static File Serving ---
app.mount("/", StaticFiles(directory="frontend/static", html=True), name="static")
