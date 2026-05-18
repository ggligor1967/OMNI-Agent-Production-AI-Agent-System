"""OMNI AGENT - Role Manager
Role-Based Access Control (RBAC): define roles with permissions, assign
roles to actors, evaluate policies, and support role inheritance.

Features:
- Roles: named sets of permissions with optional parent role
- Permissions: resource:action strings (e.g. "reports:read", "users:*")
- Wildcard matching: "users:*" grants all actions on users resource
- Role inheritance: child role gets all parent permissions
- Actor assignment: assign one or more roles to any actor ID
- Policy evaluation: check actor has permission before an action
- Deny list: explicit denials override any grants
- Audit trail: log every allow/deny decision
- Context-aware: pass metadata to policy evaluation hooks
- SQLite persistence: roles, assignments, and audit log
- REST API: assign, check, create-role, list-roles, audit
"""
import json, time, uuid, sqlite3, fnmatch, logging
from typing import Callable, Dict, List, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

@dataclass
class Permission:
    resource: str; action: str
    @property
    def key(self): return f"{self.resource}:{self.action}"
    def matches(self, resource: str, action: str) -> bool:
        return (fnmatch.fnmatch(resource, self.resource) and
                fnmatch.fnmatch(action, self.action))
    def to_dict(self): return {"resource": self.resource, "action": self.action, "key": self.key}

@dataclass
class Role:
    name: str; description: str = ""
    permissions: List[Permission] = field(default_factory=list)
    parent: str = ""        # role inheritance
    deny: List[Permission] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def has_permission(self, resource: str, action: str) -> bool:
        # Check denies first
        if any(d.matches(resource, action) for d in self.deny):
            return False
        return any(p.matches(resource, action) for p in self.permissions)

    def to_dict(self):
        return {"name": self.name, "description": self.description,
                "permissions": [p.to_dict() for p in self.permissions],
                "deny": [d.to_dict() for d in self.deny],
                "parent": self.parent}

@dataclass
class PolicyDecision:
    allowed: bool; actor: str; resource: str; action: str
    matched_role: str = ""; reason: str = ""
    timestamp: float = field(default_factory=time.time)
    def to_dict(self):
        return {"allowed": self.allowed, "actor": self.actor,
                "resource": self.resource, "action": self.action,
                "matched_role": self.matched_role, "reason": self.reason,
                "timestamp": self.timestamp}

def _parse_perm(perm_str: str) -> Permission:
    parts = perm_str.split(":", 1)
    return Permission(resource=parts[0], action=parts[1] if len(parts) > 1 else "*")

class RBACStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()
    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c
    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS roles(
                    name TEXT PRIMARY KEY, description TEXT DEFAULT '',
                    permissions TEXT DEFAULT '[]', deny TEXT DEFAULT '[]',
                    parent TEXT DEFAULT '', created_at REAL);
                CREATE TABLE IF NOT EXISTS assignments(
                    actor TEXT NOT NULL, role TEXT NOT NULL,
                    assigned_at REAL, PRIMARY KEY(actor, role));
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY, actor TEXT, resource TEXT, action TEXT,
                    allowed INTEGER, matched_role TEXT, reason TEXT, timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_asgn_actor ON assignments(actor);
                CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor, timestamp DESC);
            """)
    def save_role(self, role: Role):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO roles VALUES(?,?,?,?,?,?)",
                (role.name, role.description,
                 json.dumps([p.key for p in role.permissions]),
                 json.dumps([d.key for d in role.deny]),
                 role.parent, role.created_at))
    def load_role(self, name: str) -> Optional[Role]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
        if not row: return None
        return Role(name=row["name"], description=row["description"] or "",
                    permissions=[_parse_perm(p) for p in json.loads(row["permissions"] or "[]")],
                    deny=[_parse_perm(d) for d in json.loads(row["deny"] or "[]")],
                    parent=row["parent"] or "", created_at=row["created_at"])
    def list_roles(self) -> List[str]:
        with self._conn() as c:
            rows = c.execute("SELECT name FROM roles ORDER BY name").fetchall()
        return [r["name"] for r in rows]
    def assign(self, actor: str, role: str):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO assignments VALUES(?,?,?)",
                (actor, role, time.time()))
    def revoke(self, actor: str, role: str):
        with self._conn() as c:
            c.execute("DELETE FROM assignments WHERE actor=? AND role=?", (actor, role))
    def get_actor_roles(self, actor: str) -> List[str]:
        with self._conn() as c:
            rows = c.execute("SELECT role FROM assignments WHERE actor=?", (actor,)).fetchall()
        return [r["role"] for r in rows]
    def log_decision(self, d: PolicyDecision):
        with self._conn() as c:
            c.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:10], d.actor, d.resource, d.action,
                 int(d.allowed), d.matched_role, d.reason, d.timestamp))
    def get_audit(self, actor: str = None, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            if actor:
                rows = c.execute("SELECT * FROM audit WHERE actor=? ORDER BY timestamp DESC LIMIT ?",
                                   (actor, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM audit ORDER BY timestamp DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [dict(r) for r in rows]
    def stats(self):
        with self._conn() as c:
            nr = c.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
            na = c.execute("SELECT COUNT(DISTINCT actor) FROM assignments").fetchone()[0]
            nd = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            allow = c.execute("SELECT COUNT(*) FROM audit WHERE allowed=1").fetchone()[0]
        return {"roles": nr, "actors_with_roles": na, "decisions_logged": nd,
                "allow_rate": round(allow / max(1, nd), 4)}

class RoleManager:
    """
    Role-Based Access Control with inheritance, wildcards, and audit logging.

    Usage:
        rm = RoleManager()
        rm.create_role("admin",  permissions=["*:*"])
        rm.create_role("editor", permissions=["posts:read","posts:write","comments:*"])
        rm.create_role("viewer", permissions=["posts:read","comments:read"])
        rm.create_role("senior_editor", permissions=["drafts:publish"], parent="editor")

        rm.assign_role("alice", "admin")
        rm.assign_role("bob",   "senior_editor")

        rm.check("alice", "users", "delete")   # True (admin)
        rm.check("bob",   "posts", "write")     # True (via editor parent)
        rm.check("bob",   "users", "delete")    # False
    """
    BUILT_IN_ROLES = [
        ("superadmin", ["*:*"], "Full access to everything"),
        ("readonly",   ["*:read"], "Read-only access to all resources"),
    ]

    def __init__(self, db_path: str = "data/roles.db", audit: bool = True):
        self._store = RBACStore(db_path)
        self._audit = audit
        self._roles: Dict[str, Role] = {}
        self._hooks: List[Callable] = []
        # Load existing roles
        for name in self._store.list_roles():
            r = self._store.load_role(name)
            if r: self._roles[name] = r
        # Seed built-ins if absent
        for name, perms, desc in self.BUILT_IN_ROLES:
            if name not in self._roles:
                self.create_role(name, permissions=perms, description=desc)

    def create_role(self, name: str, permissions: List[str] = None,
                     deny: List[str] = None, parent: str = "",
                     description: str = "") -> Role:
        role = Role(name=name, description=description,
                    permissions=[_parse_perm(p) for p in (permissions or [])],
                    deny=[_parse_perm(d) for d in (deny or [])],
                    parent=parent)
        self._roles[name] = role; self._store.save_role(role)
        logger.info(f"Role created: {name!r} ({len(role.permissions)} permissions)")
        return role

    def add_permission(self, role_name: str, perm: str):
        role = self._roles.get(role_name)
        if not role: raise ValueError(f"Role {role_name!r} not found")
        role.permissions.append(_parse_perm(perm)); self._store.save_role(role)

    def remove_permission(self, role_name: str, perm: str):
        role = self._roles.get(role_name)
        if not role: return
        key = _parse_perm(perm).key
        role.permissions = [p for p in role.permissions if p.key != key]
        self._store.save_role(role)

    def add_deny(self, role_name: str, perm: str):
        role = self._roles.get(role_name)
        if not role: raise ValueError(f"Role {role_name!r} not found")
        role.deny.append(_parse_perm(perm)); self._store.save_role(role)

    def assign_role(self, actor: str, role_name: str):
        if role_name not in self._roles: raise ValueError(f"Role {role_name!r} not found")
        self._store.assign(actor, role_name)
        logger.debug(f"Assigned role {role_name!r} to {actor!r}")

    def revoke_role(self, actor: str, role_name: str):
        self._store.revoke(actor, role_name)

    def get_roles(self, actor: str) -> List[str]:
        return self._store.get_actor_roles(actor)

    def get_effective_permissions(self, actor: str) -> Set[str]:
        """Resolve all permissions for actor including role inheritance."""
        perms: Set[str] = set()
        visited: Set[str] = set()
        def collect(role_name: str):
            if role_name in visited: return
            visited.add(role_name)
            role = self._roles.get(role_name)
            if not role: return
            if role.parent: collect(role.parent)
            for p in role.permissions: perms.add(p.key)
            for d in role.deny: perms.discard(d.key)
        for role_name in self.get_roles(actor):
            collect(role_name)
        return perms

    def check(self, actor: str, resource: str, action: str,
               context: Dict = None) -> PolicyDecision:
        """Evaluate whether actor can perform action on resource."""
        actor_roles = self.get_roles(actor)
        visited: Set[str] = set()

        def evaluate_role(role_name: str) -> Optional[PolicyDecision]:
            if role_name in visited: return None
            visited.add(role_name)
            role = self._roles.get(role_name)
            if not role: return None
            # Check denies first
            if any(d.matches(resource, action) for d in role.deny):
                return PolicyDecision(allowed=False, actor=actor, resource=resource,
                                       action=action, matched_role=role_name,
                                       reason=f"Denied by role {role_name!r}")
            # Check grants
            if any(p.matches(resource, action) for p in role.permissions):
                return PolicyDecision(allowed=True, actor=actor, resource=resource,
                                       action=action, matched_role=role_name,
                                       reason=f"Granted by role {role_name!r}")
            # Check parent
            if role.parent:
                return evaluate_role(role.parent)
            return None

        for role_name in actor_roles:
            decision = evaluate_role(role_name)
            if decision and decision.allowed:
                if self._audit: self._store.log_decision(decision)
                return decision
            if decision and not decision.allowed:
                if self._audit: self._store.log_decision(decision)
                return decision

        # Custom hooks
        for hook in self._hooks:
            result = hook(actor, resource, action, context or {})
            if result is not None:
                d = PolicyDecision(allowed=bool(result), actor=actor,
                                    resource=resource, action=action, reason="hook")
                if self._audit: self._store.log_decision(d)
                return d

        decision = PolicyDecision(allowed=False, actor=actor, resource=resource,
                                   action=action, reason="No matching role")
        if self._audit: self._store.log_decision(decision)
        return decision

    def add_policy_hook(self, hook: Callable):
        self._hooks.append(hook)

    def list_roles(self) -> List[Role]:
        return list(self._roles.values())

    def get_role(self, name: str) -> Optional[Role]:
        return self._roles.get(name)

    def delete_role(self, name: str) -> bool:
        if name in self._roles:
            del self._roles[name]; return True
        return False

    def audit_log(self, actor: str = None, limit: int = 50) -> List[Dict]:
        return self._store.get_audit(actor, limit)

    def stats(self) -> Dict:
        return self._store.stats()

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def check_ep(req):
            d = await req.json()
            dec = self.check(d["actor"], d["resource"], d["action"], d.get("context",{}))
            return web.json_response(dec.to_dict())
        async def assign_ep(req):
            d = await req.json()
            self.assign_role(d["actor"], d["role"])
            return web.json_response({"assigned": True}, status=201)
        async def revoke_ep(req):
            d = await req.json()
            self.revoke_role(d["actor"], d["role"])
            return web.json_response({"revoked": True})
        async def create_ep(req):
            d = await req.json()
            role = self.create_role(d["name"], d.get("permissions",[]),
                                     d.get("deny",[]), d.get("parent",""), d.get("description",""))
            return web.json_response(role.to_dict(), status=201)
        async def list_ep(req):
            return web.json_response({"roles": [r.to_dict() for r in self.list_roles()]})
        async def audit_ep(req):
            return web.json_response({"audit": self.audit_log(req.rel_url.query.get("actor"))})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/rbac"
        app.router.add_post(f"{p}/check", check_ep)
        app.router.add_post(f"{p}/assign", assign_ep)
        app.router.add_post(f"{p}/revoke", revoke_ep)
        app.router.add_post(f"{p}/role",   create_ep)
        app.router.add_get( f"{p}/roles",  list_ep)
        app.router.add_get( f"{p}/audit",  audit_ep)
        app.router.add_get( f"{p}/stats",  stats_ep)
        logger.info(f"Role manager API at {prefix}/rbac/")
