import os
import stat

from app import crypto, db, paths, settings


def test_key_created_with_strict_permissions():
    mode = stat.S_IMODE(os.stat(paths.KEY_PATH).st_mode)
    assert mode == 0o600


def test_key_is_not_overwritten():
    before = paths.KEY_PATH.read_bytes()
    crypto.init(paths.KEY_PATH)
    assert paths.KEY_PATH.read_bytes() == before


def test_secret_encrypted_at_rest():
    settings.set_secret("discord_token", "  super-geheim-token  ")
    raw = db.query_one("SELECT value FROM secrets WHERE key='discord_token'")["value"]
    assert "super-geheim" not in raw
    assert settings.get_secret("discord_token") == "super-geheim-token"
    assert settings.has_secret("discord_token")


def test_secret_with_other_key_fails_gracefully(tmp_path):
    settings.set_secret("openrouter_key", "abc")
    crypto.init(tmp_path / "anderekey" / "k.key")
    assert settings.get_secret("openrouter_key") is None


def test_settings_defaults_and_profile_merge():
    assert settings.get("memory_mode") == "review"
    settings.set("profile", {"naam": "Pa", "onbekend_veld": "x"})
    p = settings.get("profile")
    assert p["naam"] == "Pa" and p["taal_pc"] == "Nederlands" and "onbekend_veld" not in p


def test_models_dedup():
    settings.set("model_primary", "a:free")
    settings.set("model_fallbacks", ["b:free", "a:free", " "])
    assert settings.models() == ["a:free", "b:free"]
