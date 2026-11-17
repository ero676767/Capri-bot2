import os
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "Capri"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

DB_PATH = os.getenv("DB_PATH", "capri.db")

MAX_HISTORY_MESSAGES = 20
STORED_MESSAGES_PER_USER = 100
MAX_FACTS_PER_USER = 50
MAX_FACT_LENGTH = 100

MAX_RETRIES = 2
COOLDOWN_SECONDS = 3
MAX_INPUT_CHARS = 2000

DAILY_MESSAGE_LIMIT = 200
DAILY_IMAGE_LIMIT = 10
DAILY_MEDIA_LIMIT = 30

IMAGES_ENABLED = os.getenv("IMAGES_ENABLED", "true").lower() in ("1", "true", "yes")
MAX_ATTACHMENTS = 5
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TOTAL_MEDIA_BYTES = 25 * 1024 * 1024

IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime"}
AUDIO_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg",
    "audio/webm", "audio/mp4", "audio/aac", "audio/flac",
}
MIME_FIX = {"image/jpg": "image/jpeg", "audio/mp3": "audio/mpeg"}

ERROR_MESSAGE = (
    "Aïe chou, mon petit cerveau est surchargé là 🥺 "
    "Réessaie dans quelques instants, promis je reviens !"
)