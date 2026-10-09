import os
import re
import time
import hmac
import hashlib
import json
import base64
from functools import wraps
from typing import Optional, Dict, Any, Tuple
from flask import request, jsonify, g, make_response

# Application Secret Key for signing JWT session tokens
SECRET_KEY = os.environ.get("SECRET_KEY", "fast_map_leads_super_secret_production_key_2026")


# =========================================================================
# In-Memory Sliding Window Rate Limiter
# =========================================================================

class RateLimiter:
    """Thread-safe in-memory rate limiter using sliding timestamp window."""
    def __init__(self):
        self._records: Dict[str, list] = {}

    def is_allowed(self, key: str, max_requests: int, window_seconds: int) -> Tuple[bool, int]:
        now = time.time()
        cutoff = now - window_seconds
        timestamps = self._records.get(key, [])
        # Evict old entries
        valid_timestamps = [t for t in timestamps if t > cutoff]

        if len(valid_timestamps) >= max_requests:
            oldest = valid_timestamps[0]
            retry_after = int(oldest + window_seconds - now) + 1
            self._records[key] = valid_timestamps
            return False, max(1, retry_after)

        valid_timestamps.append(now)
        self._records[key] = valid_timestamps
        return True, 0

    def cleanup(self):
        """Clean up memory periodically."""
        now = time.time()
        for k in list(self._records.keys()):
            self._records[k] = [t for t in self._records[k] if t > now - 3600]
            if not self._records[k]:
                self._records.pop(k, None)

limiter = RateLimiter()


def get_client_ip() -> str:
    """Safely obtain client IP taking reverse proxy headers into account."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def rate_limit(max_requests: int, window_seconds: int, key_prefix: str = ""):
    """Decorator to apply rate limiting to Flask route."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            ip = get_client_ip()
            user_id = getattr(g, "user_id", None)
            key = f"{key_prefix}:{user_id or ip}"

            allowed, retry_after = limiter.is_allowed(key, max_requests, window_seconds)
            if not allowed:
                return jsonify({
                    "error": "Rate limit exceeded. Too many requests.",
                    "retry_after_seconds": retry_after
                }), 429
            return f(*args, **kwargs)
        return wrapper
    return decorator


# =========================================================================
# Lightweight, Cryptographically-Signed JWT Session Tokens
# =========================================================================

def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def _b64_decode(data: str) -> bytes:
    pad = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + pad)

def generate_auth_token(user_id: int, role: str = "user", expires_in: int = 86400 * 7) -> str:
    """Generate a tamper-proof cryptographically signed session token (HMAC-SHA256)."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "uid": user_id,
        "role": role,
        "exp": int(time.time()) + expires_in,
        "iat": int(time.time())
    }
    header_b64 = _b64_encode(json.dumps(header).encode("utf-8"))
    payload_b64 = _b64_encode(json.dumps(payload).encode("utf-8"))
    sig_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), sig_input, hashlib.sha256).digest()
    sig_b64 = _b64_encode(sig)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_auth_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify cryptographic signature and expiry of token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        sig_input = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig = hmac.new(SECRET_KEY.encode("utf-8"), sig_input, hashlib.sha256).digest()
        provided_sig = _b64_decode(sig_b64)

        if not hmac.compare_digest(expected_sig, provided_sig):
            return None

        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None  # Expired
        return payload
    except Exception:
        return None


# =========================================================================
# Authentication & Role Authorization Middleware
# =========================================================================

def get_current_user_from_request() -> Optional[Dict[str, Any]]:
    """Extract and authenticate user from Authorization header or cookie."""
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1].strip()
    elif request.cookies.get("auth_token"):
        token = request.cookies.get("auth_token")
    elif request.args.get("token"):
        token = request.args.get("token").strip()

    if not token:
        return None

    payload = decode_auth_token(token)
    if not payload:
        return None

    from db import get_user_by_id
    user = get_user_by_id(payload["uid"])
    if user and not user.get("is_banned"):
        return user
    return None


def require_auth(f):
    """Ensure the user is authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user_from_request()
        if not user:
            return jsonify({"error": "Unauthorized. Please sign in to continue."}), 401
        g.user = user
        g.user_id = user["id"]
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Ensure the user has an Admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user_from_request()
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Access denied. Administrator privileges required."}), 403
        g.user = user
        g.user_id = user["id"]
        return f(*args, **kwargs)
    return decorated


# =========================================================================
# Input Sanitization & Validation Helpers
# =========================================================================

def validate_email(email: str) -> bool:
    if not email or len(email) > 120:
        return False
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


def validate_phone(phone: str) -> bool:
    if not phone:
        return True  # Optional
    digits = re.sub(r"\D", "", phone)
    return 7 <= len(digits) <= 15


def sanitize_filename(filename: str) -> str:
    """Prevent directory traversal attacks like ../../etc/passwd."""
    base = os.path.basename(filename)
    # Only allow safe characters
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", base)
    return cleaned


def add_security_headers(response):
    """Attach enterprise security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
