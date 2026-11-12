import os
import sys
import tempfile
from pathlib import Path

import pytest

_base = Path(tempfile.mkdtemp(prefix="hulp-test-"))
os.environ.setdefault("HULP_DATA_DIR", str(_base / "data"))
os.environ.setdefault("HULP_KEY_DIR", str(_base / "keys"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import crypto, db, paths  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "data" / "test.sqlite3")
    monkeypatch.setattr(paths, "IMAGE_DIR", tmp_path / "data" / "afbeeldingen")
    monkeypatch.setattr(paths, "KEY_DIR", tmp_path / "keys")
    monkeypatch.setattr(paths, "KEY_PATH", tmp_path / "keys" / "secret.key")
    db.init(paths.DB_PATH)
    crypto.init(paths.KEY_PATH)
    yield
