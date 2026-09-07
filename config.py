import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if present
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CF_SITE_KEY = os.getenv("CF_SITE_KEY", "")
CF_SECRET_KEY = os.getenv("CF_SECRET_KEY", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:4567").rstrip('/')
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "4567"))

def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not CF_SITE_KEY:
        missing.append("CF_SITE_KEY")
    if not CF_SECRET_KEY:
        missing.append("CF_SECRET_KEY")
    return missing
