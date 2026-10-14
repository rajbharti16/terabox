import os

from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
MONGO_URI = os.environ.get("MONGO_URI", "")

TERABOX_COOKIE = os.environ.get("TERABOX_COOKIE", "").strip()
TERABOX_API_URL = os.environ.get("TERABOX_API_URL", "").strip()

UPDATES_URL = os.environ.get("UPDATES_URL", "").strip()
SUPPORT_URL = os.environ.get("SUPPORT_URL", "").strip()

MAX_SIZE_MB = int(os.environ.get("MAX_SIZE_MB", "2000"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
DOWNLOAD_DIR = "downloads"
