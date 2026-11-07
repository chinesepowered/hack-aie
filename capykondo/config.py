"""Configuration: environment, paths and model ids."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("CAPY_DATA_DIR", ROOT / "data"))
MEDIA_DIR = DATA_DIR / "media"
BRAIN_DIR = DATA_DIR / "brain"
DB_PATH = DATA_DIR / "capykondo.db"
WEB_DIR = ROOT / "web"
for _d in (DATA_DIR, MEDIA_DIR, BRAIN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
AGENTVERSE_API_KEY = os.environ.get("AGENTVERSE_API_KEY", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
ASI1_API_KEY = os.environ.get("ASI1_API_KEY", "")
ASI1_MODEL = os.environ.get("CAPY_ASI1_MODEL", "asi1-mini")

HOST = os.environ.get("CAPY_HOST", "0.0.0.0")
PORT = int(os.environ.get("CAPY_PORT", "8787"))

META_BASE_URL = "https://api.meta.ai/v1"

# Meta models
MUSE_FAST = os.environ.get("CAPY_MUSE_FAST", "muse-spark-1.3")      # discovery, pricing, chat
MUSE_SMART = os.environ.get("CAPY_MUSE_SMART", "muse-wonder-1.0")   # broker main loop (optional)
SAM = "sam-3.1"

# Google models
ARGON = os.environ.get("CAPY_ARGON", "gemini-4-argon")              # condition inspection, lesson coach
FLASH = os.environ.get("CAPY_FLASH", "gemini-3.8-flash")
NANO_BANANA = os.environ.get("CAPY_IMAGE", "gemini-nano-banana-2.1")  # studio backdrops

# Pipeline knobs
SAM_VIDEO_WIDTH = 640
SAM_VIDEO_FPS = 8
MAX_ITEMS = 12
CURRENCY = "USD"

BROKER_SEED = os.environ.get("BROKER_SEED", "")
APPRAISER_SEED = os.environ.get("APPRAISER_SEED", "")
BROKER_PORT = int(os.environ.get("CAPY_BROKER_PORT", "8011"))
APPRAISER_PORT = int(os.environ.get("CAPY_APPRAISER_PORT", "8012"))


def public_url(path: str) -> str | None:
    """Absolute URL for a server path (e.g. /media/...), if a public base URL is configured."""
    if not PUBLIC_BASE_URL:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return PUBLIC_BASE_URL + path
