"""认证与鉴权：PBKDF2 口令哈希 + HS256 JWT（标准库实现，网关统一鉴权 §2）。

认证强化（优化项 P0-6）：
- access token（JWT）+ refresh token（服务端存储可吊销，默认 7 天，轮换使用）
- 口令策略：长度 ≥ 8 + 常见弱口令黑名单
- 登录失败锁定：同一用户名 15 分钟内失败 5 次锁定（security.antifraud）
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from ..config import settings
from ..db import get_db

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# 常见弱口令黑名单（演示级最小集；不包含任何真实可用凭据）
_WEAK_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1",
    "qwe123456", "11111111", "88888888", "00000000", "abc12345",
    "66666666", "99999999", "12341234", "11223344",
}


def password_policy(password: str) -> tuple:
    """返回 (ok, 错误信息)。"""
    pw = password or ""
    if len(pw) < 8:
        return False, "密码长度至少 8 位"
    if pw.lower() in _WEAK_PASSWORDS:
        return False, "密码过于常见，请更换"
    return True, ""


def _token_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def issue_refresh_token(user_id: int) -> str:
    """签发 refresh token：明文只出现一次，库中仅存哈希（可吊销）。"""
    raw = secrets.token_urlsafe(32)
    get_db().auth_token_save(
        _token_hash(raw), user_id, "refresh",
        time.time() + settings.refresh_token_days * 86400,
    )
    return raw


def validate_refresh_token(raw: str):
    """有效返回 user_id，否则 None。"""
    row = get_db().auth_token_get_valid(_token_hash(raw or ""), "refresh")
    return row["user_id"] if row else None


def rotate_refresh_token(raw: str):
    """轮换：旧令牌作废，签发新令牌。返回 (user_id, 新refresh) 或 (None, None)。"""
    user_id = validate_refresh_token(raw)
    if not user_id:
        return None, None
    get_db().auth_token_revoke(_token_hash(raw or ""))
    return user_id, issue_refresh_token(user_id)


def revoke_refresh_token(raw: str) -> None:
    get_db().auth_token_revoke(_token_hash(raw or ""))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64url(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120000)
    return _b64url(salt) + "$" + _b64url(digest)


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_part, hash_part = (stored or "").split("$", 1)
        salt = _unb64url(salt_part)
        expect = _unb64url(hash_part)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120000)
        return hmac.compare_digest(actual, expect)
    except (ValueError, TypeError):
        return False


def _jwt_secret() -> str:
    s = os.environ.get("JWT_SECRET", "").strip()
    if s:
        return s
    db = get_db()
    saved = db.kv_get("jwt_secret")
    if saved:
        return saved
    import secrets

    generated = secrets.token_hex(32)
    db.kv_set("jwt_secret", generated)
    return generated


def create_token(user_id: int, username: str, role: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + settings.token_ttl_hours * 3600,
    }
    signing = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(_jwt_secret().encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + _b64url(sig)


def decode_token(token: str):
    """返回 payload dict；无效/过期返回 None。"""
    try:
        head_b64, payload_b64, sig_b64 = (token or "").split(".")
        signing = head_b64 + "." + payload_b64
        expect = hmac.new(_jwt_secret().encode(), signing.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _unb64url(sig_b64)):
            return None
        payload = json.loads(_unb64url(payload_b64))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, TypeError, KeyError):
        return None


def user_from_token(token: str):
    payload = decode_token(token)
    if not payload:
        return None
    return {
        "user_id": payload.get("sub"),
        "username": payload.get("username"),
        "role": payload.get("role", "user"),
    }
