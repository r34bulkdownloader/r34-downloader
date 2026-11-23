"""Optional password protection for the user ID and API key.

The password is stretched into a key with scrypt (standard library) and the credentials are encrypted with
AES-GCM (the `cryptography` package). Only the encrypted blob is stored; a forgotten password can't be recovered
— the user re-enters the key from their rule34.xxx account instead."""
import base64
import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

N, R, P = 2 ** 15, 8, 1  # scrypt cost: ~0.1 s and 32 MB per try
MIN_PASSWORD = 6
_AAD = b"r34-downloader credentials v1"


class WrongPassword(Exception):
    pass


class BadSecret(Exception):
    """The stored blob can't be read at all (damaged file, or settings this version doesn't support)."""


@dataclass(frozen=True)
class Vault:
    """An unlocked password: the derived key plus the exact scrypt settings it came from, so re-sealing keeps
    them (a blob made with other settings must not be relabelled with ours — that would lock the user out)."""
    key: bytes
    salt: bytes
    n: int = N
    r: int = R
    p: int = P


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _derive(password: str, salt: bytes, n: int = N, r: int = R, p: int = P) -> bytes:
    if not (2 ** 10 <= n <= 2 ** 20 and 1 <= r <= 32 and 1 <= p <= 4):  # a hand-edited file can't make us hang
        raise BadSecret("unsupported key settings")
    try:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32)
    except (ValueError, MemoryError) as e:  # e.g. settings needing more memory than allowed
        raise BadSecret(str(e)) from None


def new_key(password: str) -> Vault:
    """A vault for a newly chosen password (fresh salt, current settings)."""
    salt = os.urandom(16)
    return Vault(_derive(password, salt), salt)


def seal(vault: Vault, creds: dict) -> dict:
    """Encrypt {"user_id", "api_key"} into a JSON-safe blob for the settings file (a fresh nonce every time)."""
    nonce = os.urandom(12)
    data = AESGCM(vault.key).encrypt(nonce, json.dumps(creds).encode(), _AAD)
    return {"v": 1, "kdf": "scrypt", "n": vault.n, "r": vault.r, "p": vault.p, "salt": _b64(vault.salt),
            "nonce": _b64(nonce), "data": _b64(data)}


def unlock(blob: dict, password: str) -> tuple[dict, Vault]:
    """(creds, vault), or WrongPassword / BadSecret. The vault is kept to re-save without asking again."""
    try:
        salt = base64.b64decode(blob["salt"])
        nonce = base64.b64decode(blob["nonce"])
        data = base64.b64decode(blob["data"])
        n, r, p = int(blob.get("n", N)), int(blob.get("r", R)), int(blob.get("p", P))
    except (KeyError, TypeError, ValueError) as e:
        raise BadSecret(str(e)) from None
    key = _derive(password, salt, n, r, p)
    try:
        creds = json.loads(AESGCM(key).decrypt(nonce, data, _AAD))
    except InvalidTag:
        raise WrongPassword() from None
    except ValueError as e:  # bad nonce length or not JSON
        raise BadSecret(str(e)) from None
    out = {"user_id": str(creds.get("user_id", "")), "api_key": str(creds.get("api_key", ""))}
    out.update({k: str(creds[k]) for k in ("eh_member_id", "eh_pass_hash") if creds.get(k)})  # optional login
    return out, Vault(key, salt, n, r, p)
