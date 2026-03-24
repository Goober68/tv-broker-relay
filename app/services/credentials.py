"""
Credential encryption service.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
The encryption key is stored only in the environment variable
CREDENTIAL_ENCRYPTION_KEY — never in the database.

Fernet guarantees:
  - Ciphertext is authenticated (tamper-evident)
  - Each encryption produces a different ciphertext (random IV)
  - Decryption fails loudly if the key or ciphertext is wrong

Key rotation: if you need to rotate the key, decrypt all rows with the
old key and re-encrypt with the new key before swapping the env var.
A migration helper should be written for that case.
"""
import json
from cryptography.fernet import Fernet, InvalidToken
from app.config import get_settings

import logging
logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    settings = get_settings()
    try:
        return Fernet(settings.credential_encryption_key.encode())
    except Exception as e:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is invalid. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from e


def encrypt_credentials(credentials: dict) -> str:
    """
    Serialize a credentials dict to JSON and encrypt it.
    Returns a URL-safe base64 Fernet token (str).
    """
    f = _get_fernet()
    plaintext = json.dumps(credentials, separators=(",", ":")).encode()
    return f.encrypt(plaintext).decode()


def decrypt_credentials(encrypted: str) -> dict:
    """
    Decrypt a Fernet token and deserialize the credentials dict.
    Raises InvalidToken if the key is wrong or the ciphertext is tampered.
    """
    f = _get_fernet()
    try:
        plaintext = f.decrypt(encrypted.encode())
        return json.loads(plaintext)
    except InvalidToken as e:
        logger.error("Failed to decrypt broker credentials — wrong key or tampered ciphertext")
        raise


def rotate_credentials(encrypted: str, new_key: str) -> str:
    """
    Re-encrypt credentials with a new key.
    Used during key rotation — call for each BrokerAccount row.
    """
    # Decrypt with current key
    creds = decrypt_credentials(encrypted)
    # Encrypt with new key
    try:
        new_fernet = Fernet(new_key.encode())
    except Exception as e:
        raise ValueError(f"Invalid new encryption key: {e}") from e
    plaintext = json.dumps(creds, separators=(",", ":")).encode()
    return new_fernet.encrypt(plaintext).decode()
