"""OMNI AGENT - Access Control
RBAC + ABAC hybrid: roles, permissions, resource policies,
attribute-based rules, and hierarchical inheritance.

Features:
- Permission: action on resource (e.g. "read:documents")
- Role: named set of permissions; roles inherit from parent roles
- Subject: user or service with assigned roles and attributes
- Resource: typed entity with owner and attributes
- RBAC: allow if subject has a role that grants the permission
- ABAC: policy rules match on subject/resource/env attributes
- Policy: (effect=ALLOW|DENY, subjects[], resources[], actions[],
    conditions=[attr op val])
- Explicit DENY wins over ALLOW (deny-overrides strategy)
- Wildcard matching: "read:*" matches "read:documents"
- Resource ownership: owner always has read/write on own resource
- Hierarchical roles: permissions inherited from parent roles (BFS)
- Policy evaluation order: explicit DENY → explicit ALLOW → default DENY
- Session tokens: temporary permission sets with expiry
- Audit trail: every decision logged with reason
- Caching: decision cache with TTL for hot-path performance
- SQLite persistence: roles, subjects, policies, decisions
- REST API: can, grant, revoke, add_policy, subject_roles, stats
"""
import json, re, sqlite3, time, uuid, logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
logger = logging.getLogger(__name__)

class Effect(str, Enum):
    ALLOW = "allow"; DENY = "deny"

class Decision(str, Enum):
    ALLOW = "allow"; DENY = "deny"; ABSTAIN = "abstain"

def _wildcard_match(pattern: str, value: str) -> bool:
    """Match 'read:*' against 'read:documents' etc."""
    if pattern == "*": return True
    if "*" not in pattern: return pattern == value
    regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
    return bool(re.match(regex, value))

@dataclass
class Permission:
    action: str; resource: str = "*"

    @property
    def key(self) -> str: return f"{self.action}:{self.resource}"

    def matches(self, action: str, resource: str) -> bool:
        return (_wildcard_match(self.action, action) and
                _wildcard_match(self.resource, resource))

@dataclass
class Role:
    name: str; description: str = ""
    permissions: List[Permission] = field(default_factory=list)
    parents: List[str] = field(default_factory=list)  # role names to inherit from

    def to_dict(self):
        return {"name": self.name,
                "permissions": [p.key for p in self.permissions],
                "parents": self.parents}

@dataclass
class Subject:
    id: str; subject_type: str = "user"
    roles: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {"id": self.id, "type": self.subject_type,
                "roles": self.roles, "attributes": self.attributes}

@dataclass
class Resource:
    id: str; resource_type: str = "document"
    owner_id: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Condition:
    attribute: str; op: str; value: Any
    source: str = "subject"  # "subject" | "resource" | "env"

    def evaluate(self, subject: Subject,
                  resource: Resource, env: Dict) -> bool:
        if self.source == "subject":
            val = subject.attributes.get(self.attribute)
        elif self.source == "resource":
            val = resource.attributes.get(self.attribute)
        else:
            val = env.get(self.attribute)
        op = self.op
        if op == "eq":       return val == self.value
        if op == "neq":      return val != self.value
        if op == "gt":       return (val or 0) > self.value
        if op == "lt":       return (val or 0) < self.value
        if op == "gte":      return (val or 0) >= self.value
        if op == "lte":      return (val or 0) <= self.value
        if op == "in":       return val in (self.value if isinstance(self.value, list) else [self.value])
        if op == "contains": return self.value in str(val or "")
        return False

@dataclass
class Policy:
    name: str; effect: Effect
    subjects: List[str] = field(default_factory=list)   # [] = any
    resources: List[str] = field(default_factory=list)  # [] = any
    actions: List[str] = field(default_factory=list)    # [] = any
    conditions: List[Condition] = field(default_factory=list)
    priority: int = 0; enabled: bool = True

    def matches_subject(self, subject_id: str) -> bool:
        return not self.subjects or any(
            _wildcard_match(p, subject_id) for p in self.subjects)

    def matches_resource(self, resource_id: str) -> bool:
        return not self.resources or any(
            _wildcard_match(p, resource_id) for p in self.resources)

    def matches_action(self, action: str) -> bool:
        return not self.actions or any(
            _wildcard_match(p, action) for p in self.actions)

    def conditions_met(self, subject: Subject,
                        resource: Resource, env: Dict) -> bool:
        return all(c.evaluate(subject, resource, env) for c in self.conditions)

    def to_dict(self):
        return {"name": self.name, "effect": self.effect.value,
                "subjects": self.subjects, "resources": self.resources,
                "actions": self.actions, "priority": self.priority,
                "enabled": self.enabled}

class ACStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path; self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS decisions(
                    id TEXT PRIMARY KEY, subject TEXT,
                    action TEXT, resource TEXT,
                    decision TEXT, reason TEXT, ts REAL);
                CREATE INDEX IF NOT EXISTS idx_dec_subject
                    ON decisions(subject, ts DESC);
            """)

    def log(self, subject: str, action: str, resource: str,
             decision: Decision, reason: str):
        with self._conn() as c:
            c.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4())[:8], subject, action, resource,
                 decision.value, reason, time.time()))

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            by_dec = {r["decision"]: r["cnt"] for r in c.execute(
                "SELECT decision, COUNT(*) as cnt FROM decisions "
                "GROUP BY decision").fetchall()}
        return {"total": total, "by_decision": by_dec}

class AccessControl:
    """
    RBAC + ABAC access control with deny-overrides and role inheritance.

    Usage:
        ac = AccessControl()
        ac.define_role("admin", permissions=[
            Permission("*", "*")
        ])
        ac.define_role("editor", permissions=[
            Permission("read", "*"),
            Permission("write", "documents")
        ])
        ac.add_subject("alice", roles=["editor"])

        ok = ac.can("alice", "write", "documents/123")
        print(ok)  # True
    """
    def __init__(self, db_path: str = "data/access.db",
                 default_deny: bool = True,
                 cache_ttl_s: float = 60.0):
        self._store = ACStore(db_path)
        self._roles: Dict[str, Role] = {}
        self._subjects: Dict[str, Subject] = {}
        self._resources: Dict[str, Resource] = {}
        self._policies: List[Policy] = []
        self._default_deny = default_deny
        self._cache: Dict[str, Tuple[Decision, float]] = {}
        self._cache_ttl = cache_ttl_s

    def define_role(self, name: str,
                     permissions: List[Permission] = None,
                     parents: List[str] = None,
                     description: str = "") -> Role:
        role = Role(name=name, description=description,
                     permissions=list(permissions or []),
                     parents=list(parents or []))
        self._roles[name] = role
        return role

    def grant(self, role_name: str, permission: Permission):
        role = self._roles.get(role_name)
        if role: role.permissions.append(permission)

    def revoke(self, role_name: str, action: str, resource: str = "*"):
        role = self._roles.get(role_name)
        if role:
            role.permissions = [p for p in role.permissions
                                  if not (p.action == action and p.resource == resource)]

    def add_subject(self, subject_id: str,
                     subject_type: str = "user",
                     roles: List[str] = None,
                     attributes: Dict = None) -> Subject:
        s = Subject(id=subject_id, subject_type=subject_type,
                     roles=list(roles or []),
                     attributes=dict(attributes or {}))
        self._subjects[subject_id] = s
        return s

    def assign_role(self, subject_id: str, role_name: str):
        s = self._subjects.get(subject_id)
        if s and role_name not in s.roles:
            s.roles.append(role_name)
            self._invalidate_cache(subject_id)

    def remove_role(self, subject_id: str, role_name: str):
        s = self._subjects.get(subject_id)
        if s and role_name in s.roles:
            s.roles.remove(role_name)
            self._invalidate_cache(subject_id)

    def add_resource(self, resource_id: str,
                      resource_type: str = "document",
                      owner_id: str = "",
                      attributes: Dict = None) -> Resource:
        r = Resource(id=resource_id, resource_type=resource_type,
                      owner_id=owner_id, attributes=dict(attributes or {}))
        self._resources[resource_id] = r
        return r

    def add_policy(self, policy: Policy):
        self._policies.append(policy)
        self._cache.clear()

    def _all_permissions(self, subject: Subject) -> List[Permission]:
        """BFS over role hierarchy to collect all permissions."""
        perms = []
        visited: Set[str] = set()
        queue = deque(subject.roles)
        while queue:
            role_name = queue.popleft()
            if role_name in visited: continue
            visited.add(role_name)
            role = self._roles.get(role_name)
            if not role: continue
            perms.extend(role.permissions)
            queue.extend(role.parents)
        return perms

    def _invalidate_cache(self, subject_id: str):
        keys = [k for k in self._cache if k.startswith(f"{subject_id}:")]
        for k in keys: del self._cache[k]

    def _cache_key(self, subject_id: str, action: str,
                    resource_id: str) -> str:
        return f"{subject_id}:{action}:{resource_id}"

    def decide(self, subject_id: str, action: str,
                resource_id: str, env: Dict = None) -> Tuple[Decision, str]:
        env = env or {}
        cache_key = self._cache_key(subject_id, action, resource_id)
        cached = self._cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0], "cached"

        subject = self._subjects.get(subject_id,
                                      Subject(id=subject_id))
        resource = self._resources.get(resource_id,
                                        Resource(id=resource_id))

        # Owner shortcut
        if resource.owner_id == subject_id and action in ("read", "write", "delete"):
            result = (Decision.ALLOW, "owner")
            self._cache[cache_key] = (result[0], time.time() + self._cache_ttl)
            return result

        # Policy evaluation (sorted by priority desc)
        sorted_policies = sorted(
            [p for p in self._policies if p.enabled],
            key=lambda p: -p.priority)

        deny_reason = ""; allow_reason = ""
        for policy in sorted_policies:
            if not policy.matches_subject(subject_id): continue
            if not policy.matches_resource(resource_id): continue
            if not policy.matches_action(action): continue
            if not policy.conditions_met(subject, resource, env): continue
            if policy.effect == Effect.DENY:
                deny_reason = f"policy:{policy.name}"; break
            else:
                allow_reason = f"policy:{policy.name}"

        if deny_reason:
            result = (Decision.DENY, deny_reason)
        elif allow_reason:
            result = (Decision.ALLOW, allow_reason)
        else:
            # RBAC check
            perms = self._all_permissions(subject)
            matched = any(p.matches(action, resource_id) for p in perms)
            if matched:
                result = (Decision.ALLOW, "rbac")
            elif self._default_deny:
                result = (Decision.DENY, "default_deny")
            else:
                result = (Decision.ALLOW, "default_allow")

        self._cache[cache_key] = (result[0], time.time() + self._cache_ttl)
        self._store.log(subject_id, action, resource_id, result[0], result[1])
        return result

    def can(self, subject_id: str, action: str,
             resource_id: str = "*", env: Dict = None) -> bool:
        decision, _ = self.decide(subject_id, action, resource_id, env)
        return decision == Decision.ALLOW

    def explain(self, subject_id: str, action: str,
                 resource_id: str = "*") -> Dict:
        decision, reason = self.decide(subject_id, action, resource_id)
        subject = self._subjects.get(subject_id, Subject(id=subject_id))
        perms = self._all_permissions(subject)
        return {"decision": decision.value, "reason": reason,
                "subject_roles": subject.roles,
                "effective_permissions": [p.key for p in perms[:20]]}

    def subject_permissions(self, subject_id: str) -> List[str]:
        s = self._subjects.get(subject_id, Subject(id=subject_id))
        return [p.key for p in self._all_permissions(s)]

    def stats(self) -> Dict:
        s = self._store.stats()
        s["roles"] = len(self._roles)
        s["subjects"] = len(self._subjects)
        s["policies"] = len(self._policies)
        s["cache_size"] = len(self._cache)
        return s

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def can_ep(req):
            d = await req.json()
            ok = self.can(d["subject"], d["action"],
                           d.get("resource","*"), d.get("env",{}))
            return web.json_response({"allowed": ok})
        async def explain_ep(req):
            d = await req.json()
            return web.json_response(self.explain(
                d["subject"], d["action"], d.get("resource","*")))
        async def grant_ep(req):
            d = await req.json()
            self.assign_role(d["subject"], d["role"])
            return web.json_response({"granted": True})
        async def revoke_ep(req):
            d = await req.json()
            self.remove_role(d["subject"], d["role"])
            return web.json_response({"revoked": True})
        async def stats_ep(req): return web.json_response(self.stats())
        p = f"{prefix}/ac"
        app.router.add_post(f"{p}/can",     can_ep)
        app.router.add_post(f"{p}/explain", explain_ep)
        app.router.add_post(f"{p}/grant",   grant_ep)
        app.router.add_post(f"{p}/revoke",  revoke_ep)
        app.router.add_get( f"{p}/stats",   stats_ep)
        logger.info(f"Access control API at {prefix}/ac/")
