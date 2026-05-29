import os
import sys
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("EPIC_EMAIL", "test@example.com")
os.environ.setdefault("EPIC_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from settings import _normalize_gemini_base_url


def test_normalize_gemini_base_url_empty_uses_default():
    assert _normalize_gemini_base_url("") == "https://aihubmix.com/gemini"


def test_normalize_gemini_base_url_relative_path_becomes_absolute():
    assert _normalize_gemini_base_url("/gemini") == "https://aihubmix.com/gemini"


def test_normalize_gemini_base_url_v1_suffix_trimmed():
    assert _normalize_gemini_base_url("https://aihubmix.com/v1") == "https://aihubmix.com/gemini"
