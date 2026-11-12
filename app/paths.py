"""Vaste paden binnen de container. Alleen in tests worden ze via de omgeving omgebogen."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("HULP_DATA_DIR", "/data"))
KEY_DIR = Path(os.environ.get("HULP_KEY_DIR", "/keys"))
DB_PATH = DATA_DIR / "hulpdashboard.sqlite3"
IMAGE_DIR = DATA_DIR / "afbeeldingen"
KEY_PATH = KEY_DIR / "secret.key"

# Versie (korte git-commit), meegegeven als build-arg in GitHub Actions.
VERSION = os.environ.get("APP_VERSION", "dev")
