import hashlib
import json
import re
import time
import jwt
from pydantic import ValidationError
from app.errors import AppError
from app.schemas import Principal

# Defense in depth, NOT a complete DLP or prompt-injection detector.
INJECTION = re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions|prompts)"
    r"|reveal\s+(?:the\s+)?system\s+prompt|<\|(?:im_start|system)\|>"
    r"|(?:exfiltrate|steal)\s+(?:the\s+)?(?:api\s*key|secret|credentials)", re.I)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)")
KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b")


def redact(value):
    value = EMAIL.sub("[REDACTED_EMAIL]", value)
    value = KEY.sub("[REDACTED_SECRET]", value)
    return PHONE.sub(lambda m: "[REDACTED_NUMBER]" if sum(c.isdigit() for c in m[0]) >= 10 else m[0], value)


def suspicious(value):
    return bool(INJECTION.search(value))


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def decode_token(token, settings):
    key = settings.jwt_secret if settings.jwt_algorithm == "HS256" else settings.jwt_public_key.replace("\\n", "\n")
    try:
        claims = jwt.decode(token, key, algorithms=[settings.jwt_algorithm], audience=settings.jwt_audience,
            issuer=settings.jwt_issuer, options={"require": ["exp", "iat", "sub", "tid", "iss", "aud"]}, leeway=5)
        roles = claims.get("roles", ["user"])
        if not isinstance(roles, list) or len(roles) > 20 or any(not isinstance(r, str) for r in roles):
            raise ValueError("Invalid roles")
        return Principal(tenant_id=claims["tid"], user_id=claims["sub"], roles=frozenset(roles))
    except (jwt.PyJWTError, ValidationError, ValueError, TypeError, KeyError):
        raise AppError("unauthorized", "A valid bearer token is required.", 401) from None


def mint_demo_token(settings, tenant, user, roles, seconds=3600):
    if settings.environment == "production" or settings.jwt_algorithm != "HS256":
        raise ValueError("Demo tokens are disabled in production")
    Principal(tenant_id=tenant, user_id=user, roles=frozenset(roles))
    now = int(time.time())
    return jwt.encode({"sub": user, "tid": tenant, "roles": roles, "iat": now, "exp": now + seconds,
        "iss": settings.jwt_issuer, "aud": settings.jwt_audience}, settings.jwt_secret, algorithm="HS256")
