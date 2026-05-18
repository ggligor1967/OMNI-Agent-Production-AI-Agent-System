"""
OMNI AGENT - Authentication & Authorization
JWT-based API authentication, API key management, role-based access control
(RBAC), per-tier rate limiting, and request middleware for aiohttp.

Features:
  - API key generation with hashed storage (never store plaintext keys)
  - JWT access tokens with configurable expiry
  - Roles: admin, developer, user, readonly
  - Per-role endpoint permissions via decorators
  - Per-tier rate limits (admin=unlimited, developer=1000/hr, user=100/hr)
  - Token blacklist (for logout / key revocation)
  - aiohttp middleware for automatic auth enforcement
  - SQLite-backed key & session store
"""
import hmac
import json
import time
import uuid
import base64
import hashlib
import secrets
import sqlite3
import logging
from typing import Any, Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from collections import deque

logger = logging.getLogger(__name__)

MIN_SECRET_LENGTH = 32


# ══════════════════════════════════════════════════════════════════════════════
# ROLES & PERMISSIONS
# ══════════════════════════════════════════════════════════════════════════════

class Role(str, Enum):
    ADMIN     = "admin"      # full access + admin endpoints
    DEVELOPER = "developer"  # full API access, no admin endpoints
    USER      = "user"       # chat + memories + basic queries
    READONLY  = "readonly"   # GET only, no writes


# Endpoints accessible per role (wildcard = all)
ROLE_PERMISSIONS: Dict[Role, Set[str]] = {
    Role.ADMIN: {"*"},
    Role.DEVELOPER: {
        "chat", "status", "memories", "models", "route", "compare",
        "rag", "pipelines", "templates", "workflows", "tools",
        "tracing", "structured", "personas", "eval", "kg",
        "stream", "cache", "audit",
    },
    Role.USER: {
        "chat", "status", "memories", "stream/chat", "stream/events",
        "structured", "personas", "stream",
    },
    Role.READONLY: {
        "status", "models",
    },
}

# Rate limits per role (requests per hour)
ROLE_RATE_LIMITS: Dict[Role, int] = {
    Role.ADMIN:     0,       # unlimited
    Role.DEVELOPER: 10_000,
    Role.USER:      500,
    Role.READONLY:  100,
}


def has_permission(role: Role, endpoint: str) -> bool:
    """Check if a role can access an endpoint prefix."""
    allowed = ROLE_PERMISSIONS.get(role, set())
    if "*" in allowed:
        return True
    # Strip leading slash and match prefix
    clean = endpoint.lstrip("/").split("/")[0]
    return clean in allowed or endpoint.lstrip("/") in allowed


# ══════════════════════════════════════════════════════════════════════════════
# JWT (minimal, no external deps)
# ══════════════════════════════════════════════════════════════════════════════

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (padding % 4))


def validate_secret_value(value: str, label: str = "secret",
                          min_length: int = MIN_SECRET_LENGTH) -> str:
    candidate = value or ""
    if len(candidate) < min_length:
        raise ValueError(f"{label} must be at least {min_length} characters long")
    if candidate == "CHANGE_ME_IN_PRODUCTION":
        raise ValueError(f"{label} must not use the default placeholder value")
    return candidate


def create_jwt(payload: Dict, secret: str, expires_in: int = 3600) -> str:
    """Create a minimal HS256 JWT."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = {**payload, "iat": int(time.time()), "exp": int(time.time()) + expires_in}
    body = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url_encode(sig)}"


def verify_jwt(token: str, secret: str) -> Optional[Dict]:
    """
    Verify and decode a JWT. Returns payload dict or None on failure.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_part, body_part, sig_part = parts
        signing_input = f"{header_part}.{body_part}".encode()
        expected_sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_part)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(body_part))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class APIKey:
    key_id: str              # public identifier
    key_hash: str            # SHA-256 of the actual key
    user_id: str
    role: Role
    name: str = ""           # human-readable label
    description: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    last_used_at: Optional[float] = None
    revoked: bool = False
    request_count: int = 0

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.revoked and not self.is_expired

    def to_dict(self, include_hash: bool = False) -> Dict:
        d = {
            "key_id": self.key_id,
            "user_id": self.user_id,
            "role": self.role.value,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_used_at": self.last_used_at,
            "revoked": self.revoked,
            "request_count": self.request_count,
        }
        if include_hash:
            d["key_hash"] = self.key_hash
        return d


@dataclass
class AuthContext:
    """Populated on every authenticated request."""
    authenticated: bool = False
    user_id: str = ""
    role: Role = Role.READONLY
    key_id: str = ""
    auth_method: str = ""    # "api_key" | "jwt" | "anonymous"
    error: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def can_access(self, endpoint: str) -> bool:
        return self.authenticated and has_permission(self.role, endpoint)


# ══════════════════════════════════════════════════════════════════════════════
# KEY STORE (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

class AuthStore:
    def __init__(self, db_path: str = "data/auth.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id       TEXT PRIMARY KEY,
                    key_hash     TEXT NOT NULL,
                    user_id      TEXT NOT NULL,
                    role         TEXT NOT NULL,
                    name         TEXT DEFAULT '',
                    description  TEXT DEFAULT '',
                    created_at   REAL,
                    expires_at   REAL,
                    last_used_at REAL,
                    revoked      INTEGER DEFAULT 0,
                    request_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);
                CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);

                CREATE TABLE IF NOT EXISTS blacklist (
                    token_sig  TEXT PRIMARY KEY,
                    revoked_at REAL,
                    reason     TEXT DEFAULT ''
                );
            """)

    def save_key(self, key: APIKey):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO api_keys
                (key_id,key_hash,user_id,role,name,description,created_at,
                 expires_at,last_used_at,revoked,request_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (key.key_id, key.key_hash, key.user_id, key.role.value,
                  key.name, key.description, key.created_at,
                  key.expires_at, key.last_used_at, int(key.revoked),
                  key.request_count))

    def get_by_hash(self, key_hash: str) -> Optional[APIKey]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0",
                (key_hash,)
            ).fetchone()
        return self._row_to_key(row) if row else None

    def get_by_id(self, key_id: str) -> Optional[APIKey]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_id=?", (key_id,)
            ).fetchone()
        return self._row_to_key(row) if row else None

    def list_keys(self, user_id: str = None,
                  include_revoked: bool = False) -> List[APIKey]:
        with self._conn() as conn:
            q = "SELECT * FROM api_keys"
            params = []
            filters = []
            if user_id:
                filters.append("user_id=?"); params.append(user_id)
            if not include_revoked:
                filters.append("revoked=0")
            if filters:
                q += " WHERE " + " AND ".join(filters)
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_key(r) for r in rows]

    def count_keys(self, include_revoked: bool = False) -> int:
        with self._conn() as conn:
            q = "SELECT COUNT(*) FROM api_keys"
            params = []
            if not include_revoked:
                q += " WHERE revoked=0"
            return int(conn.execute(q, params).fetchone()[0])

    def revoke_key(self, key_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked=1 WHERE key_id=?", (key_id,)
            )
        return cur.rowcount > 0

    def record_usage(self, key_id: str):
        with self._conn() as conn:
            conn.execute("""
                UPDATE api_keys
                SET last_used_at=?, request_count=request_count+1
                WHERE key_id=?
            """, (time.time(), key_id))

    def blacklist_token(self, token: str, reason: str = ""):
        """Add a JWT to the blacklist by its signature."""
        sig = token.split(".")[-1] if "." in token else token
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO blacklist (token_sig,revoked_at,reason) VALUES (?,?,?)",
                (sig, time.time(), reason)
            )

    def is_blacklisted(self, token: str) -> bool:
        sig = token.split(".")[-1] if "." in token else token
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM blacklist WHERE token_sig=?", (sig,)
            ).fetchone()
        return row is not None

    def _row_to_key(self, row) -> APIKey:
        return APIKey(
            key_id=row["key_id"], key_hash=row["key_hash"],
            user_id=row["user_id"], role=Role(row["role"]),
            name=row["name"] or "", description=row["description"] or "",
            created_at=row["created_at"], expires_at=row["expires_at"],
            last_used_at=row["last_used_at"],
            revoked=bool(row["revoked"]), request_count=row["request_count"],
        )


# ══════════════════════════════════════════════════════════════════════════════
# AUTH MANAGER
# ══════════════════════════════════════════════════════════════════════════════

def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AuthManager:
    """
    Full authentication and authorization manager.

    Usage:
        auth = AuthManager(secret="my-jwt-secret")

        # Create an API key
        raw_key, key_obj = auth.create_api_key("user_123", Role.DEVELOPER,
                                                 name="My App Key")
        # raw_key is shown once — user must store it

        # Authenticate a request
        ctx = auth.authenticate(api_key="omni_xxx")
        if ctx.can_access("chat"):
            ...

        # Create JWT
        token = auth.create_token("user_123", Role.USER, expires_in=3600)

        # aiohttp middleware
        app.middlewares.append(auth.middleware(public_paths=["/status"]))
    """

    def __init__(self, secret: str = None,
                 db_path: str = "data/auth.db",
                 token_expiry: int = 3600,
                 enforce_auth: bool = False,
                 bootstrap_token: str = ""):
        self.secret = secret or secrets.token_hex(32)
        self.store = AuthStore(db_path)
        self.token_expiry = token_expiry
        self.enforce_auth = enforce_auth
        self.bootstrap_token = bootstrap_token
        # Per-user rate limit tracking: {user_id: deque of timestamps}
        self._rate_windows: Dict[str, deque] = {}
        if self.secret == "CHANGE_ME_IN_PRODUCTION":
            logger.warning("AuthManager is using the default SECRET_KEY placeholder.")

    # ── Key Management ────────────────────────────────────────────────────────

    def create_api_key(self, user_id: str, role: Role = Role.USER,
                        name: str = "", description: str = "",
                        expires_in: int = None,
                        raw_key: str = "") -> tuple:
        """
        Generate a new API key. Returns (raw_key, APIKey).
        The raw_key is only returned once and never stored in plaintext.
        """
        raw = validate_secret_value(raw_key, label="API key") if raw_key else (
            "omni_" + secrets.token_urlsafe(32)
        )
        key_hash = _hash_key(raw)
        key_id = str(uuid.uuid4())[:12]
        expires_at = time.time() + expires_in if expires_in else None
        key = APIKey(
            key_id=key_id, key_hash=key_hash,
            user_id=user_id, role=role, name=name, description=description,
            expires_at=expires_at,
        )
        self.store.save_key(key)
        logger.info(f"API key created: {key_id} for user={user_id} role={role.value}")
        return raw, key

    def revoke_key(self, key_id: str) -> bool:
        ok = self.store.revoke_key(key_id)
        if ok:
            logger.info(f"API key revoked: {key_id}")
        return ok

    def list_keys(self, user_id: str = None) -> List[Dict]:
        return [k.to_dict() for k in self.store.list_keys(user_id=user_id)]

    def is_bootstrapped(self) -> bool:
        return self.store.count_keys() > 0

    def bootstrap_admin(self, provided_token: str,
                        user_id: str = "admin",
                        name: str = "Bootstrap Admin",
                        admin_api_key: str = "") -> tuple:
        """
        Create the first admin API key using a one-time bootstrap token.
        This is intentionally disabled once any active API key exists.
        """
        if not self.bootstrap_token:
            raise PermissionError("Bootstrap is disabled")
        if not hmac.compare_digest(provided_token or "", self.bootstrap_token):
            raise PermissionError("Invalid bootstrap token")
        if self.is_bootstrapped():
            raise RuntimeError("Bootstrap has already completed")
        return self.create_api_key(
            user_id=user_id or "admin",
            role=Role.ADMIN,
            name=name,
            description="Initial admin key created via bootstrap",
            raw_key=admin_api_key,
        )

    def bootstrap_admin_identity(self, provided_token: str,
                                 user_id: str = "admin",
                                 name: str = "Bootstrap Admin",
                                 admin_api_key: str = "",
                                 token_expires_in: Optional[int] = None) -> tuple:
        raw_key, key = self.bootstrap_admin(
            provided_token,
            user_id=user_id,
            name=name,
            admin_api_key=admin_api_key,
        )
        token = self.create_token(
            key.user_id,
            key.role,
            expires_in=token_expires_in or self.token_expiry,
        )
        return raw_key, key, token

    # ── JWT ───────────────────────────────────────────────────────────────────

    def create_token(self, user_id: str, role: Role = Role.USER,
                      expires_in: int = None) -> str:
        return create_jwt(
            {"sub": user_id, "role": role.value},
            self.secret,
            expires_in or self.token_expiry
        )

    def revoke_token(self, token: str, reason: str = "logout"):
        self.store.blacklist_token(token, reason)
        logger.info(f"JWT revoked (reason={reason})")

    # ── Authentication ────────────────────────────────────────────────────────

    def authenticate(self, api_key: str = None,
                     bearer_token: str = None) -> AuthContext:
        """
        Authenticate via API key or JWT bearer token.
        Returns AuthContext with populated user_id, role, etc.
        """
        if api_key:
            return self._auth_api_key(api_key)
        if bearer_token:
            return self._auth_jwt(bearer_token)
        return AuthContext(authenticated=False, auth_method="anonymous",
                          error="No credentials provided")

    def _auth_api_key(self, raw_key: str) -> AuthContext:
        key_hash = _hash_key(raw_key)
        key = self.store.get_by_hash(key_hash)
        if not key:
            return AuthContext(error="Invalid API key")
        if not key.is_valid:
            return AuthContext(error="API key revoked or expired")
        self.store.record_usage(key.key_id)
        return AuthContext(
            authenticated=True, user_id=key.user_id, role=key.role,
            key_id=key.key_id, auth_method="api_key",
        )

    def _auth_jwt(self, token: str) -> AuthContext:
        if self.store.is_blacklisted(token):
            return AuthContext(error="Token has been revoked")
        payload = verify_jwt(token, self.secret)
        if not payload:
            return AuthContext(error="Invalid or expired JWT")
        try:
            role = Role(payload.get("role", Role.USER.value))
        except ValueError:
            role = Role.USER
        return AuthContext(
            authenticated=True,
            user_id=payload.get("sub", ""),
            role=role, auth_method="jwt",
        )

    # ── Rate Limiting ─────────────────────────────────────────────────────────

    def check_rate_limit(self, user_id: str, role: Role) -> Dict:
        """Sliding window rate limit check. Returns {allowed, retry_after}."""
        limit = ROLE_RATE_LIMITS.get(role, 100)
        if limit == 0:
            return {"allowed": True, "retry_after": 0}

        now = time.time()
        window = self._rate_windows.setdefault(user_id, deque())
        cutoff = now - 3600.0  # 1 hour window
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= limit:
            oldest = window[0]
            retry_after = 3600 - (now - oldest)
            return {"allowed": False, "retry_after": max(0, retry_after)}

        window.append(now)
        return {"allowed": True, "retry_after": 0,
                "remaining": limit - len(window)}

    # ── aiohttp Middleware ────────────────────────────────────────────────────

    def middleware(self, public_paths: List[str] = None,
                   anonymous_role: Role = Role.READONLY):
        """
        aiohttp middleware factory.
        Injects auth context into request and enforces auth if enabled.
        """
        from aiohttp import web
        _public = set(public_paths or ["/status", "/health"])

        @web.middleware
        async def _middleware(request, handler):
            path = request.path

            # Allow public paths without auth
            if path in _public or any(path.startswith(p) for p in _public):
                request["auth"] = AuthContext(
                    authenticated=False, auth_method="anonymous",
                    role=anonymous_role
                )
                return await handler(request)

            # Extract credentials
            api_key = request.headers.get("X-API-Key", "")
            auth_header = request.headers.get("Authorization", "")
            bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""

            ctx = self.authenticate(
                api_key=api_key or None,
                bearer_token=bearer or None,
            )

            # Rate limiting
            if ctx.authenticated:
                rate = self.check_rate_limit(ctx.user_id, ctx.role)
                if not rate["allowed"]:
                    return web.json_response(
                        {"error": "rate_limit_exceeded",
                         "retry_after": rate["retry_after"]},
                        status=429,
                    )

            # Enforce auth if enabled
            if self.enforce_auth and not ctx.authenticated:
                return web.json_response(
                    {"error": "unauthorized", "detail": ctx.error},
                    status=401,
                )

            # Permission check
            if ctx.authenticated and not ctx.can_access(path):
                return web.json_response(
                    {"error": "forbidden",
                     "detail": f"Role '{ctx.role.value}' cannot access '{path}'"},
                    status=403,
                )

            request["auth"] = ctx
            return await handler(request)

        return _middleware

    # ── REST Endpoints ────────────────────────────────────────────────────────

    def register_routes(self, app, prefix: str = ""):
        """Register auth management routes."""
        from aiohttp import web

        def _auth_ctx(request) -> AuthContext:
            return request.get("auth", AuthContext(
                authenticated=False, auth_method="anonymous",
                error="Authentication required",
            ))

        def _require_admin(request):
            ctx = _auth_ctx(request)
            if not ctx.authenticated:
                return web.json_response(
                    {"error": "unauthorized", "detail": ctx.error or "Authentication required"},
                    status=401,
                )
            if not ctx.is_admin:
                return web.json_response(
                    {"error": "forbidden", "detail": "Admin role required"},
                    status=403,
                )
            return None

        async def bootstrap(request):
            data = await request.json() if request.content_length else {}
            bootstrap_token = (
                request.headers.get("X-Bootstrap-Token", "")
                or data.get("bootstrap_token", "")
            )
            user_id = data.get("user_id", "admin")
            name = data.get("name", "Bootstrap Admin")
            try:
                raw, key = self.bootstrap_admin(
                    bootstrap_token, user_id=user_id, name=name
                )
            except PermissionError as e:
                return web.json_response({"error": str(e)}, status=403)
            except RuntimeError as e:
                return web.json_response({"error": str(e)}, status=409)

            resp = key.to_dict()
            resp["key"] = raw
            resp["warning"] = "Store this key safely — it will not be shown again."
            return web.json_response(resp, status=201)

        async def create_key(request):
            denied = _require_admin(request)
            if denied:
                return denied
            data = await request.json()
            user_id = data.get("user_id", "")
            role_str = data.get("role", "user")
            name = data.get("name", "")
            description = data.get("description", "")
            expires_in = data.get("expires_in")

            try:
                role = Role(role_str)
            except ValueError:
                return web.json_response({"error": f"Unknown role '{role_str}'"}, status=400)

            if not user_id:
                return web.json_response({"error": "user_id required"}, status=400)

            raw, key = self.create_api_key(
                user_id=user_id, role=role, name=name,
                description=description, expires_in=expires_in,
            )
            resp = key.to_dict()
            resp["key"] = raw  # Only shown once
            resp["warning"] = "Store this key safely — it will not be shown again."
            return web.json_response(resp, status=201)

        async def list_keys(request):
            denied = _require_admin(request)
            if denied:
                return denied
            user_id = request.rel_url.query.get("user_id")
            return web.json_response({"keys": self.list_keys(user_id=user_id)})

        async def revoke_key(request):
            denied = _require_admin(request)
            if denied:
                return denied
            key_id = request.match_info.get("key_id", "")
            ok = self.revoke_key(key_id)
            if not ok:
                return web.json_response({"error": "Key not found"}, status=404)
            return web.json_response({"revoked": True, "key_id": key_id})

        async def create_token(request):
            denied = _require_admin(request)
            if denied:
                return denied
            data = await request.json()
            user_id = data.get("user_id", "")
            role_str = data.get("role", "user")
            expires_in = int(data.get("expires_in", self.token_expiry))
            if not user_id:
                return web.json_response({"error": "user_id required"}, status=400)
            try:
                role = Role(role_str)
            except ValueError:
                return web.json_response({"error": f"Unknown role '{role_str}'"}, status=400)
            token = self.create_token(user_id, role, expires_in)
            return web.json_response({
                "token": token,
                "expires_in": expires_in,
                "user_id": user_id,
                "role": role.value,
            })

        async def revoke_token(request):
            denied = _require_admin(request)
            if denied:
                return denied
            data = await request.json()
            token = data.get("token", "")
            reason = data.get("reason", "revoked")
            if not token:
                return web.json_response({"error": "token required"}, status=400)
            self.revoke_token(token, reason)
            return web.json_response({"revoked": True})

        async def verify_token(request):
            denied = _require_admin(request)
            if denied:
                return denied
            data = await request.json()
            token = data.get("token", "")
            payload = verify_jwt(token, self.secret)
            if not payload:
                return web.json_response({"valid": False}, status=401)
            blacklisted = self.store.is_blacklisted(token)
            return web.json_response({
                "valid": not blacklisted,
                "payload": payload,
                "blacklisted": blacklisted,
            })

        app.router.add_post(f"{prefix}/auth/bootstrap", bootstrap)
        app.router.add_post(f"{prefix}/auth/keys", create_key)
        app.router.add_get(f"{prefix}/auth/keys", list_keys)
        app.router.add_delete(f"{prefix}/auth/keys/{{key_id}}", revoke_key)
        app.router.add_post(f"{prefix}/auth/token", create_token)
        app.router.add_post(f"{prefix}/auth/token/revoke", revoke_token)
        app.router.add_post(f"{prefix}/auth/token/verify", verify_token)

        logger.info(f"Auth routes registered at {prefix}/auth/*")
