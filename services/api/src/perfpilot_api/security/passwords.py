import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_PASSWORD_HASHER = PasswordHasher()


def normalize_username(username: str) -> str:
    return unicodedata.normalize("NFKC", username).strip().casefold()


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False
