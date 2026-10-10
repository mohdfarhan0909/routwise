"""Unit tests for authentication utilities."""

import hashlib

from app.core.auth import generate_api_key, get_key_prefix, hash_api_key


def test_generate_api_key_structure():
    raw_key, hashed = generate_api_key()
    assert raw_key.startswith("rw_")
    assert len(raw_key) > 30
    assert len(hashed) == 64  # SHA-256 hex string


def test_hash_api_key_consistency():
    key = "rw_test_secret_12345"
    expected = hashlib.sha256(key.encode()).hexdigest()
    assert hash_api_key(key) == expected


def test_get_key_prefix():
    raw_key = "rw_abcdefghijk123456"
    prefix = get_key_prefix(raw_key)
    assert prefix == "rw_abcd"
    assert len(prefix) == 7
