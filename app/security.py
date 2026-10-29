from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .database import DATA_DIR

SECRET_PATH = DATA_DIR / 'auth_secret.key'
RECOVERY_CODE_PATH = DATA_DIR / 'admin_recovery_code.txt'
PASSWORD_ROUNDS = 310_000
SESSION_SECONDS = 12 * 60 * 60
REMEMBER_SESSION_SECONDS = 30 * 24 * 60 * 60


def _secret() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with SECRET_PATH.open('xb') as handle:
            handle.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    value = SECRET_PATH.read_bytes()
    if len(value) < 32:
        raise RuntimeError(f'Chave de sessão ausente ou inválida: {SECRET_PATH}')
    return value


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PASSWORD_ROUNDS)
    return f'pbkdf2_sha256${PASSWORD_ROUNDS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}'


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split('$', 3)
        if algorithm != 'pbkdf2_sha256': return False
        derived = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), base64.urlsafe_b64decode(salt), int(rounds))
        return hmac.compare_digest(base64.urlsafe_b64encode(derived).decode(), expected)
    except (ValueError, TypeError):
        return False


def new_recovery_code() -> str:
    return secrets.token_urlsafe(18)


def save_recovery_code(code: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Direct writing also works in Windows profiles that deny replacing existing files.
    with RECOVERY_CODE_PATH.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(code.strip() + '\n')
        handle.flush()
        import os
        os.fsync(handle.fileno())
    return RECOVERY_CODE_PATH


def rotate_session_secret() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SECRET_PATH.open('wb') as handle:
        handle.write(secrets.token_bytes(32))
        handle.flush()
        import os
        os.fsync(handle.fileno())


def create_session(user_id: int, duration: int = SESSION_SECONDS) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(32)
    payload = json.dumps({'uid': user_id, 'exp': int(time.time()) + duration, 'csrf': csrf}, separators=(',', ':')).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip('=')
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f'{body}.{signature}', csrf


def read_session(token: str | None) -> dict | None:
    if not token: return None
    try:
        body, signature = token.split('.', 1)
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected): return None
        payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if int(payload.get('exp', 0)) < int(time.time()): return None
        if not isinstance(payload.get('uid'), int) or not payload.get('csrf'): return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return None
