"""OMNI Agent — Secrets Manager V2: vault with rotation, encryption, access audit."""
from __future__ import annotations
import base64, hashlib, hmac, json, os, secrets, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SecretStatus(str, Enum):
    ACTIVE   = "active"
    INACTIVE = "inactive"
    ROTATING = "rotating"
    EXPIRED  = "expired"
    DELETED  = "deleted"


class SecretType(str, Enum):
    PASSWORD    = "password"
    API_KEY     = "api_key"
    TOKEN       = "token"
    CERTIFICATE = "certificate"
    DATABASE    = "database"
    ENCRYPTION  = "encryption"
    CUSTOM      = "custom"


def _simple_encrypt(value: str, key: str) -> str:
    """XOR-based obfuscation (not cryptographic — use in stdlib-only context)."""
    key_bytes = hashlib.sha256(key.encode()).digest()
    val_bytes = value.encode()
    enc = bytes(v ^ key_bytes[i % len(key_bytes)]
                for i, v in enumerate(val_bytes))
    return base64.b64encode(enc).decode()


def _simple_decrypt(enc_value: str, key: str) -> str:
    key_bytes = hashlib.sha256(key.encode()).digest()
    enc_bytes = base64.b64decode(enc_value)
    dec = bytes(v ^ key_bytes[i % len(key_bytes)]
                for i, v in enumerate(enc_bytes))
    return dec.decode()


@dataclass
class SecretEntry:
    secret_id: str
    name: str
    secret_type: SecretType = SecretType.CUSTOM
    status: SecretStatus = SecretStatus.ACTIVE
    encrypted_value: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    owner: str = ""
    version: int = 1
    max_versions: int = 5
    ttl_s: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    rotation_interval_s: Optional[float] = None
    next_rotation_at: Optional[float] = None
    last_accessed_at: Optional[float] = None
    access_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return (self.expires_at is not None and
                time.time() > self.expires_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "secret_id": self.secret_id,
            "name": self.name,
            "type": self.secret_type.value,
            "status": self.status.value,
            "version": self.version,
            "expires_at": self.expires_at,
            "access_count": self.access_count,
        }


@dataclass
class SecretVersion:
    version_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    secret_id: str = ""
    version: int = 1
    encrypted_value: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class AccessRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    secret_id: str = ""
    accessor: str = ""
    action: str = "read"   # read | write | rotate | delete
    success: bool = True
    ip: str = ""
    ts: float = field(default_factory=time.time)


class SecretsManagerV2:
    """
    Secrets vault:
    - Store/retrieve secrets with optional encryption (XOR+hash, stdlib-only)
    - Versioned secrets (keep last N versions)
    - TTL-based expiry
    - Automatic rotation scheduling
    - Custom rotation functions
    - Access control (owner + allowed accessors)
    - Full access audit log
    - Secret generation (random tokens, passwords, API keys)
    - Bulk operations (list, filter, expire)
    - SQLite persistence
    """

    def __init__(self, master_key: str = "default-master-key",
                 db_path: str = ":memory:",
                 encrypt: bool = True):
        self._master_key   = master_key
        self._encrypt      = encrypt
        self._secrets:     Dict[str, SecretEntry] = {}
        self._versions:    Dict[str, List[SecretVersion]] = {}
        self._acl:         Dict[str, List[str]] = {}   # secret_id → [accessor]
        self._rotators:    Dict[str, Callable] = {}    # secret_id → rotation_fn
        self._audit:       List[AccessRecord] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sm_secrets (
                secret_id TEXT PRIMARY KEY, name TEXT,
                secret_type TEXT, status TEXT, version INTEGER,
                expires_at REAL, rotation_interval_s REAL,
                created_at REAL, updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS sm_audit (
                record_id TEXT PRIMARY KEY, secret_id TEXT,
                accessor TEXT, action TEXT, success INTEGER, ts REAL
            );
        """)
        self._db.commit()

    # ── STORE / RETRIEVE ──────────────────────────────────────────────

    def store(self, name: str, value: str,
              secret_type: SecretType = SecretType.CUSTOM,
              tags: Optional[List[str]] = None,
              description: str = "",
              owner: str = "",
              ttl_s: Optional[float] = None,
              rotation_interval_s: Optional[float] = None,
              rotation_fn: Optional[Callable] = None,
              max_versions: int = 5,
              allowed_accessors: Optional[List[str]] = None,
              secret_id: Optional[str] = None,
              metadata: Optional[Dict] = None) -> SecretEntry:
        sid = secret_id or str(uuid.uuid4())[:10]
        enc = _simple_encrypt(value, self._master_key) if self._encrypt else value
        exp = time.time() + ttl_s if ttl_s else None
        nxt = time.time() + rotation_interval_s if rotation_interval_s else None
        e   = SecretEntry(
            secret_id=sid, name=name,
            secret_type=secret_type,
            encrypted_value=enc,
            tags=list(tags or []),
            description=description, owner=owner,
            max_versions=max_versions,
            ttl_s=ttl_s, expires_at=exp,
            rotation_interval_s=rotation_interval_s,
            next_rotation_at=nxt,
            metadata=metadata or {})
        self._secrets[sid] = e
        self._versions[sid] = [SecretVersion(
            secret_id=sid, version=1, encrypted_value=enc)]
        if allowed_accessors:
            self._acl[sid] = list(allowed_accessors)
        if rotation_fn:
            self._rotators[sid] = rotation_fn
        self._persist_secret(e)
        return e

    def get(self, secret_id: str,
            accessor: str = "",
            check_acl: bool = True) -> Optional[str]:
        e = self._secrets.get(secret_id)
        if not e: return None
        if e.is_expired:
            e.status = SecretStatus.EXPIRED
            return None
        if e.status in (SecretStatus.DELETED, SecretStatus.INACTIVE):
            return None
        # ACL check
        if check_acl and accessor:
            acl = self._acl.get(secret_id)
            if acl is not None and accessor not in acl:
                self._log(secret_id, accessor, "read", success=False)
                return None
        e.last_accessed_at = time.time()
        e.access_count    += 1
        self._log(secret_id, accessor, "read", success=True)
        if self._encrypt:
            return _simple_decrypt(e.encrypted_value, self._master_key)
        return e.encrypted_value

    def get_by_name(self, name: str, accessor: str = "") -> Optional[str]:
        e = next((s for s in self._secrets.values()
                  if s.name == name), None)
        return self.get(e.secret_id, accessor) if e else None

    def update(self, secret_id: str, new_value: str,
               accessor: str = "") -> bool:
        e = self._secrets.get(secret_id)
        if not e or e.status == SecretStatus.DELETED:
            return False
        enc = _simple_encrypt(new_value, self._master_key) if self._encrypt else new_value
        # Version history
        versions = self._versions.setdefault(secret_id, [])
        versions.append(SecretVersion(
            secret_id=secret_id, version=e.version + 1,
            encrypted_value=enc))
        # Trim to max_versions
        if len(versions) > e.max_versions:
            self._versions[secret_id] = versions[-e.max_versions:]
        e.encrypted_value = enc
        e.version        += 1
        e.updated_at      = time.time()
        self._log(secret_id, accessor, "write")
        self._persist_secret(e)
        return True

    def delete(self, secret_id: str, accessor: str = "") -> bool:
        e = self._secrets.get(secret_id)
        if not e: return False
        e.status = SecretStatus.DELETED
        self._log(secret_id, accessor, "delete")
        self._persist_secret(e)
        return True

    def disable(self, secret_id: str):
        e = self._secrets.get(secret_id)
        if e: e.status = SecretStatus.INACTIVE

    def enable(self, secret_id: str):
        e = self._secrets.get(secret_id)
        if e and e.status == SecretStatus.INACTIVE:
            e.status = SecretStatus.ACTIVE

    # ── ROTATION ─────────────────────────────────────────────────────

    def rotate(self, secret_id: str,
               new_value: Optional[str] = None,
               accessor: str = "") -> bool:
        e = self._secrets.get(secret_id)
        if not e: return False
        if new_value is None:
            fn = self._rotators.get(secret_id)
            if fn:
                try: new_value = fn()
                except Exception: return False
            else:
                new_value = self.generate_token(32)
        e.status = SecretStatus.ROTATING
        ok = self.update(secret_id, new_value, accessor)
        e.status = SecretStatus.ACTIVE
        if e.rotation_interval_s:
            e.next_rotation_at = time.time() + e.rotation_interval_s
        self._log(secret_id, accessor, "rotate")
        return ok

    def due_for_rotation(self) -> List[str]:
        now = time.time()
        return [sid for sid, e in self._secrets.items()
                if (e.next_rotation_at and e.next_rotation_at <= now
                    and e.status == SecretStatus.ACTIVE)]

    def register_rotator(self, secret_id: str, fn: Callable):
        self._rotators[secret_id] = fn

    # ── GENERATION ───────────────────────────────────────────────────

    @staticmethod
    def generate_token(length: int = 32) -> str:
        return secrets.token_hex(length // 2)

    @staticmethod
    def generate_password(length: int = 16,
                           symbols: bool = True) -> str:
        import string
        chars = string.ascii_letters + string.digits
        if symbols:
            chars += "!@#$%^&*"
        return "".join(secrets.choice(chars) for _ in range(length))

    @staticmethod
    def generate_api_key(prefix: str = "sk") -> str:
        return f"{prefix}-{secrets.token_urlsafe(24)}"

    # ── VERSIONS ────────────────────────────────────────────────────

    def get_versions(self, secret_id: str) -> List[Dict]:
        return [{"version": v.version, "created_at": v.created_at}
                for v in self._versions.get(secret_id, [])]

    def get_version(self, secret_id: str, version: int) -> Optional[str]:
        for v in self._versions.get(secret_id, []):
            if v.version == version:
                val = v.encrypted_value
                if self._encrypt:
                    return _simple_decrypt(val, self._master_key)
                return val
        return None

    # ── ACCESS CONTROL ────────────────────────────────────────────────

    def grant_access(self, secret_id: str, accessor: str):
        self._acl.setdefault(secret_id, [])
        if accessor not in self._acl[secret_id]:
            self._acl[secret_id].append(accessor)

    def revoke_access(self, secret_id: str, accessor: str):
        acl = self._acl.get(secret_id, [])
        if accessor in acl: acl.remove(accessor)

    # ── AUDIT ────────────────────────────────────────────────────────

    def _log(self, secret_id: str, accessor: str, action: str,
             success: bool = True):
        r = AccessRecord(secret_id=secret_id,
                         accessor=accessor, action=action, success=success)
        self._audit.append(r)
        self._db.execute(
            "INSERT INTO sm_audit VALUES (?,?,?,?,?,?)",
            (r.record_id, secret_id, accessor, action,
             int(success), r.ts))
        self._db.commit()

    def audit_log(self, secret_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict]:
        q = ("SELECT record_id,secret_id,accessor,action,success,ts "
             "FROM sm_audit ORDER BY ts DESC LIMIT ?")
        rows = self._db.execute(q, (limit,)).fetchall()
        result = [{"id": r[0], "secret": r[1], "accessor": r[2],
                   "action": r[3], "success": bool(r[4])} for r in rows]
        if secret_id:
            result = [r for r in result if r["secret"] == secret_id]
        return result

    # ── LIST / FILTER ─────────────────────────────────────────────────

    def list_secrets(self, secret_type: Optional[SecretType] = None,
                     tag: Optional[str] = None,
                     status: Optional[SecretStatus] = None) -> List[Dict]:
        entries = list(self._secrets.values())
        if secret_type: entries = [e for e in entries if e.secret_type == secret_type]
        if tag:         entries = [e for e in entries if tag in e.tags]
        if status:      entries = [e for e in entries if e.status == status]
        return [e.to_dict() for e in entries]

    def expire_secrets(self) -> List[str]:
        expired = []
        for e in self._secrets.values():
            if e.is_expired and e.status == SecretStatus.ACTIVE:
                e.status = SecretStatus.EXPIRED
                expired.append(e.secret_id)
        return expired

    def _persist_secret(self, e: SecretEntry):
        self._db.execute(
            "INSERT OR REPLACE INTO sm_secrets VALUES (?,?,?,?,?,?,?,?,?)",
            (e.secret_id, e.name, e.secret_type.value,
             e.status.value, e.version, e.expires_at,
             e.rotation_interval_s, e.created_at, e.updated_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "total": len(self._secrets),
            "active": sum(1 for e in self._secrets.values()
                          if e.status == SecretStatus.ACTIVE),
            "expired": sum(1 for e in self._secrets.values()
                           if e.status == SecretStatus.EXPIRED),
            "audit_records": len(self._audit),
        }
