import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load .env file if present
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CF_SITE_KEY = os.getenv("CF_SITE_KEY", "")
CF_SECRET_KEY = os.getenv("CF_SECRET_KEY", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:4567").rstrip('/')
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "4567"))

# Where pending verifications are persisted, so a restart does not leave users muted forever
DATA_FILE = Path(os.getenv("DATA_FILE", "data/pending.json"))
if not DATA_FILE.is_absolute():
    DATA_FILE = BASE_DIR / DATA_FILE

# Maximum accepted age (seconds) of Telegram WebApp initData, to limit replay of captured initData
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not CF_SITE_KEY:
        missing.append("CF_SITE_KEY")
    if not CF_SECRET_KEY:
        missing.append("CF_SECRET_KEY")
    return missing
