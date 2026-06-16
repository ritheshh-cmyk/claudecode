import contextlib
import os
from pathlib import Path

from cryptography.fernet import Fernet
from loguru import logger

_CRYPT_KEY = None


def get_crypt_key() -> bytes:
    """Load or generate the AES encryption key from the default location."""
    global _CRYPT_KEY
    if _CRYPT_KEY is not None:
        return _CRYPT_KEY

    # Check env first
    env_key = os.environ.get("FCC_CRYPT_KEY")
    if env_key:
        try:
            # Validate it's a valid Fernet key
            key_bytes = env_key.encode("utf-8")
            Fernet(key_bytes)
            _CRYPT_KEY = key_bytes
            return _CRYPT_KEY
        except Exception:
            logger.warning(
                "Invalid FCC_CRYPT_KEY environment variable. Falling back to file."
            )

    # Fall back to ~/.fcc/.crypt_key
    fcc_dir = Path.home() / ".fcc"
    fcc_dir.mkdir(parents=True, exist_ok=True)
    key_file = fcc_dir / ".crypt_key"

    if key_file.exists():
        try:
            key_bytes = key_file.read_bytes().strip()
            Fernet(key_bytes)
            _CRYPT_KEY = key_bytes
            return _CRYPT_KEY
        except Exception:
            logger.warning("Stale or invalid crypt key file found. Re-generating.")

    # Generate new key
    key_bytes = Fernet.generate_key()
    try:
        key_file.write_bytes(key_bytes)
        # Try to restrict file permissions to owner (read/write only) on POSIX
        with contextlib.suppress(Exception):
            os.chmod(key_file, 0o600)
    except Exception as e:
        logger.error("Failed to write cryptography key file: {}", e)

    _CRYPT_KEY = key_bytes
    return _CRYPT_KEY


def encrypt_key(value: str) -> str:
    """Encrypt a string key using AES-256."""
    if not value:
        return ""
    key = get_crypt_key()
    f = Fernet(key)
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_key(encrypted_value: str) -> str:
    """Decrypt a string key using AES-256."""
    if not encrypted_value:
        return ""
    key = get_crypt_key()
    f = Fernet(key)
    try:
        return f.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error("Decryption failed. Stale or invalid key? Detail: {}", e)
        return ""
