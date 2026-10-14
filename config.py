import os

from dotenv import load_dotenv

# Load order (earlier wins, nothing is overridden):
#   1. real environment variables (Heroku Config Vars / Docker / shell)
#   2. .env
#   3. .env.sample   <- fallback, so values filled there also work
_here = os.path.dirname(os.path.abspath(__file__))
for _name in (".env", ".env.sample"):
    _path = os.path.join(_here, _name)
    if os.path.exists(_path):
        load_dotenv(_path, override=False)


def _get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int = 0) -> int:
    val = _get(name)
    try:
        return int(val) if val else default
    except ValueError:
        raise SystemExit(f"Config error: {name} must be a number, got {val!r}")


_missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN") if not _get(k)]
if _missing:
    raise SystemExit(
        f"Config error: missing {', '.join(_missing)}. "
        "Set them in Heroku Config Vars, or fill them in .env / .env.sample"
    )

API_ID = _int("API_ID")
API_HASH = _get("API_HASH")
BOT_TOKEN = _get("BOT_TOKEN")
OWNER_ID = _int("OWNER_ID", 0)
MONGO_URI = _get("MONGO_URI")

TERABOX_COOKIE = _get("TERABOX_COOKIE")
TERABOX_API_URL = _get("TERABOX_API_URL")

UPDATES_URL = _get("UPDATES_URL")
SUPPORT_URL = _get("SUPPORT_URL")

MAX_SIZE_MB = _int("MAX_SIZE_MB", 2000)
MAX_PARALLEL = _int("MAX_PARALLEL", 2)
DOWNLOAD_DIR = _get("DOWNLOAD_DIR", "downloads")
