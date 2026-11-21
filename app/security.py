from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

_PBKDF2_ITERATIONS = 200_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    """Normalizuje e-mailovou adresu (oříznutí mezer a malá písmena)."""
    return email.strip().lower()


def is_valid_email(email: str) -> bool:
    """Základní validace formátu e-mailové adresy."""
    cleaned = normalize_email(email)
    return bool(cleaned and len(cleaned) <= 200 and _EMAIL_RE.match(cleaned))


def hash_password(password: str) -> str:
    """Zahashuje heslo pomocí PBKDF2-HMAC-SHA256 s náhodnou solí."""
    if not password:
        raise ValueError("Heslo nesmí být prázdné.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return (
        f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Ověří heslo proti uloženému PBKDF2 hash řetězci."""
    if not password or not stored_hash:
        return False
    try:
        scheme, iter_s, salt_b64, digest_b64 = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_totp_secret() -> str:
    """Vygeneruje náhodný 160bitový Base32 klíč pro TOTP (RFC 6238)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_base32_secret(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    pad_len = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ("=" * pad_len), casefold=True)


def generate_totp_code(
    secret: str,
    *,
    for_time: float | None = None,
    step: int = 30,
    digits: int = 6,
) -> str:
    """Spočítá 6místný TOTP kód podle RFC 6238 (SHA-1)."""
    key = _decode_base32_secret(secret)
    ts = time.time() if for_time is None else for_time
    counter = int(ts // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code_int).zfill(digits)


def verify_totp_code(
    secret: str,
    code: str,
    *,
    for_time: float | None = None,
    step: int = 30,
    window: int = 1,
) -> bool:
    """Ověří 6místný TOTP kód s tolerancí ±`window` časových oken."""
    cleaned_code = code.strip().replace(" ", "").replace("-", "")
    if not cleaned_code.isdigit() or len(cleaned_code) != 6 or not secret.strip():
        return False
    base_ts = time.time() if for_time is None else for_time
    try:
        for delta in range(-window, window + 1):
            candidate = generate_totp_code(secret, for_time=base_ts + (delta * step), step=step)
            if hmac.compare_digest(candidate, cleaned_code):
                return True
    except Exception:
        return False
    return False


def build_otpauth_uri(
    secret: str,
    username: str,
    issuer: str = "Asistent Antigravity",
) -> str:
    """Sestaví standardní `otpauth://totp/...` URI pro autentizační aplikace."""
    label = quote(f"{issuer}:{username}")
    issuer_q = quote(issuer)
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer_q}&algorithm=SHA1&digits=6&period=30"


def _normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash_recovery_code(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_recovery_codes(count: int = 8) -> tuple[list[str], list[str]]:
    """Vygeneruje sadu jednorázových záložních kódů a jejich SHA-256 hashů."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    plain_codes: list[str] = []
    hashed_codes: list[str] = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        formatted = f"{raw[:4]}-{raw[4:]}"
        plain_codes.append(formatted)
        hashed_codes.append(_hash_recovery_code(raw))
    return plain_codes, hashed_codes


def verify_and_consume_recovery_code(
    code: str, stored_hashes: list[str]
) -> tuple[bool, list[str]]:
    """Ověří záložní kód a v případě shody jej ze seznamu odstraní."""
    norm = _normalize_recovery_code(code)
    if len(norm) < 6:
        return False, list(stored_hashes)
    target = _hash_recovery_code(norm)
    matched_idx = -1
    for idx, item in enumerate(stored_hashes):
        if hmac.compare_digest(item, target):
            matched_idx = idx
            break
    if matched_idx == -1:
        return False, list(stored_hashes)
    remaining = [h for i, h in enumerate(stored_hashes) if i != matched_idx]
    return True, remaining


def load_or_create_signing_key(data_dir: Path) -> bytes:
    """Načte nebo vygeneruje 256bitový podepisovací klíč pro dočasné 2FA výzvy."""
    key_path = data_dir / "auth_secret.key"
    data_dir.mkdir(parents=True, exist_ok=True)
    if key_path.is_file():
        raw = key_path.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    key = secrets.token_bytes(32)
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def create_2fa_challenge(user_id: int, signing_key: bytes, *, ttl_seconds: int = 300) -> str:
    """Vytvoří krátkodobý podepsaný challenge token pro druhý krok 2FA přihlášení."""
    exp = int(time.time()) + ttl_seconds
    nonce = secrets.token_urlsafe(12)
    payload = f"{user_id}.{exp}.{nonce}"
    sig = base64.urlsafe_b64encode(
        hmac.new(signing_key, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{payload}.{sig}"


def verify_2fa_challenge(token: str, signing_key: bytes) -> int | None:
    """Ověří podpis a platnost challenge tokenu a vrátí `user_id` (jinak `None`)."""
    parts = token.strip().split(".")
    if len(parts) != 4:
        return None
    uid_s, exp_s, nonce, sig = parts
    try:
        uid = int(uid_s)
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    payload = f"{uid_s}.{exp_s}.{nonce}"
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(signing_key, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return uid


