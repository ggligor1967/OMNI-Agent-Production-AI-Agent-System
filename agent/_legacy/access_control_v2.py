"""OMNI Agent — Access Control V2: RBAC + ABAC with roles, permissions, resources."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class Action(str, Enum):
    CREATE = "create"
    READ   = "read"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    ADMIN  = "admin"
    ANY    = "*"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY  = "deny"


@dataclass
class Permission:
    permission_id: str
    resource: str        # e.g. "document", "user", "api/v1/*"
    action: Action
    description: str = ""
    conditions: Dict[str, Any] = field(default_factory=dict)

    def matches(self, resource: str, action: Action) -> bool:
        res_match = (self.resource == "*" or
                     self.resource == resource or
                     resource.startswith(self.resource.rstrip("*")))
        act_match = (self.action == Action.ANY or
                     self.action == action or
                     action == Action.ANY)
        return res_match and act_match

    def to_dict(self) -> Dict[str, Any]:
        return {
            "permission_id": self.permission_id,
            "resource": self.resource,
            "action": self.action.value,
            "description": self.description,
        }


@dataclass
class Role:
    role_id: str
    name: str
    description: str = ""
    permissions: List[str] = field(default_factory=list)   # permission_ids
    parent_roles: List[str] = field(default_factory=list)  # role_ids (inheritance)
    is_system: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role_id": self.role_id,
            "name": self.name,
            "permissions": len(self.permissions),
            "parent_roles": self.parent_roles,
            "is_system": self.is_system,
        }


@dataclass
class Principal:
    principal_id: str
    name: str
    principal_type: str = "user"    # user | service | group
    roles: List[str] = field(default_factory=list)          # role_ids
    direct_permissions: List[str] = field(default_factory=list)  # permission_ids
    attributes: Dict[str, Any] = field(default_factory=dict)    # for ABAC
    active: bool = True
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "name": self.name,
            "type": self.principal_type,
            "roles": self.roles,
            "active": self.active,
        }


@dataclass
class AccessDecision:
    allowed: bool
    principal_id: str
    resource: str
    action: str
    reason: str = ""
    matched_permission: Optional[str] = None
    matched_role: Optional[str] = None
    duration_ms: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "principal": self.principal_id,
            "resource": self.resource,
            "action": self.action,
            "reason": self.reason,
            "permission": self.matched_permission,
        }


class AccessControlV2:
    """
    Unified RBAC + ABAC access control:
    - Roles with hierarchical inheritance (parent roles)
    - Fine-grained permissions (resource + action)
    - Wildcard resource matching (api/v1/*)
    - Direct permissions on principals (bypass roles)
    - Attribute-based conditions (ABAC) via condition functions
    - Explicit DENY overrides ALLOW
    - Audit log in SQLite
    - Bulk permission check
    - Role introspection (effective permissions)
    """

    def __init__(self, default_deny: bool = True,
                 db_path: str = ":memory:"):
        self.default_deny = default_deny
        self._permissions: Dict[str, Permission] = {}
        self._roles:       Dict[str, Role] = {}
        self._principals:  Dict[str, Principal] = {}
        self._deny_rules:  List[Tuple[str, str, Action]] = []  # (principal, resource, action)
        self._abac_conditions: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._check_count = 0
        self._deny_count  = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS ac_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                allowed INTEGER, principal_id TEXT, resource TEXT,
                action TEXT, reason TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── PERMISSIONS ───────────────────────────────────────────────────

    def add_permission(self, resource: str, action: Action,
                       description: str = "",
                       conditions: Optional[Dict] = None,
                       permission_id: Optional[str] = None) -> Permission:
        pid = permission_id or str(uuid.uuid4())[:8]
        perm = Permission(permission_id=pid, resource=resource,
                          action=action, description=description,
                          conditions=dict(conditions or {}))
        self._permissions[pid] = perm
        return perm

    def remove_permission(self, permission_id: str):
        self._permissions.pop(permission_id, None)

    # ── ROLES ─────────────────────────────────────────────────────────

    def create_role(self, name: str,
                    description: str = "",
                    permission_ids: Optional[List[str]] = None,
                    parent_role_ids: Optional[List[str]] = None,
                    is_system: bool = False,
                    role_id: Optional[str] = None) -> Role:
        rid  = role_id or name
        role = Role(role_id=rid, name=name, description=description,
                    permissions=list(permission_ids or []),
                    parent_roles=list(parent_role_ids or []),
                    is_system=is_system)
        self._roles[rid] = role
        return role

    def add_permission_to_role(self, role_id: str, permission_id: str):
        r = self._roles.get(role_id)
        if r and permission_id not in r.permissions:
            r.permissions.append(permission_id)

    def remove_permission_from_role(self, role_id: str, permission_id: str):
        r = self._roles.get(role_id)
        if r and permission_id in r.permissions:
            r.permissions.remove(permission_id)

    def delete_role(self, role_id: str):
        self._roles.pop(role_id, None)

    def _get_effective_permissions(self, role_id: str,
                                    visited: Optional[Set[str]] = None) -> List[str]:
        """Recursively gather permissions including parent roles."""
        if visited is None: visited = set()
        if role_id in visited: return []
        visited.add(role_id)
        role = self._roles.get(role_id)
        if not role: return []
        perms = list(role.permissions)
        for parent_id in role.parent_roles:
            perms.extend(self._get_effective_permissions(parent_id, visited))
        return perms

    # ── PRINCIPALS ────────────────────────────────────────────────────

    def create_principal(self, name: str,
                          principal_type: str = "user",
                          role_ids: Optional[List[str]] = None,
                          attributes: Optional[Dict] = None,
                          principal_id: Optional[str] = None) -> Principal:
        pid = principal_id or str(uuid.uuid4())[:8]
        p   = Principal(principal_id=pid, name=name,
                        principal_type=principal_type,
                        roles=list(role_ids or []),
                        attributes=dict(attributes or {}))
        self._principals[pid] = p
        return p

    def assign_role(self, principal_id: str, role_id: str):
        p = self._principals.get(principal_id)
        if p and role_id not in p.roles:
            p.roles.append(role_id)

    def revoke_role(self, principal_id: str, role_id: str):
        p = self._principals.get(principal_id)
        if p and role_id in p.roles:
            p.roles.remove(role_id)

    def grant_direct(self, principal_id: str, permission_id: str):
        p = self._principals.get(principal_id)
        if p and permission_id not in p.direct_permissions:
            p.direct_permissions.append(permission_id)

    def revoke_direct(self, principal_id: str, permission_id: str):
        p = self._principals.get(principal_id)
        if p and permission_id in p.direct_permissions:
            p.direct_permissions.remove(permission_id)

    def deactivate_principal(self, principal_id: str):
        p = self._principals.get(principal_id)
        if p: p.active = False

    # ── DENY RULES ────────────────────────────────────────────────────

    def add_deny_rule(self, principal_id: str, resource: str,
                      action: Action = Action.ANY):
        self._deny_rules.append((principal_id, resource, action))

    def add_abac_condition(self, fn: Callable[[Principal, str, Action], bool]):
        """ABAC: fn(principal, resource, action) → True if allowed."""
        self._abac_conditions.append(fn)

    # ── CHECK ─────────────────────────────────────────────────────────

    def check(self, principal_id: str, resource: str,
              action: Action,
              context: Optional[Dict] = None) -> AccessDecision:
        t0 = time.time()
        self._check_count += 1
        principal = self._principals.get(principal_id)

        if not principal or not principal.active:
            return self._deny(principal_id, resource, action,
                              "principal not found or inactive", t0)

        # Explicit deny rules
        for pid, res, act in self._deny_rules:
            if pid in (principal_id, "*"):
                perm_mock = Permission("", res, act)
                if perm_mock.matches(resource, action):
                    return self._deny(principal_id, resource, action,
                                      "explicit deny rule", t0)

        # Collect all effective permission IDs
        all_perm_ids: List[str] = list(principal.direct_permissions)
        for role_id in principal.roles:
            all_perm_ids.extend(self._get_effective_permissions(role_id))

        # Check permissions
        for perm_id in all_perm_ids:
            perm = self._permissions.get(perm_id)
            if not perm: continue
            if perm.matches(resource, action):
                # Check ABAC conditions
                if self._abac_conditions:
                    if not all(fn(principal, resource, action)
                               for fn in self._abac_conditions):
                        continue
                d = AccessDecision(
                    allowed=True, principal_id=principal_id,
                    resource=resource, action=action.value,
                    reason="permission match",
                    matched_permission=perm_id,
                    duration_ms=(time.time() - t0) * 1000)
                self._log(d)
                return d

        if self.default_deny:
            return self._deny(principal_id, resource, action,
                              "no matching permission", t0)

        d = AccessDecision(
            allowed=True, principal_id=principal_id,
            resource=resource, action=action.value,
            reason="default allow",
            duration_ms=(time.time() - t0) * 1000)
        self._log(d)
        return d

    def is_allowed(self, principal_id: str, resource: str,
                   action: Action) -> bool:
        return self.check(principal_id, resource, action).allowed

    def check_bulk(self, principal_id: str,
                   requests: List[Tuple[str, Action]]) -> List[AccessDecision]:
        return [self.check(principal_id, res, act) for res, act in requests]

    def _deny(self, principal_id: str, resource: str, action: Action,
               reason: str, t0: float) -> AccessDecision:
        self._deny_count += 1
        d = AccessDecision(
            allowed=False, principal_id=principal_id,
            resource=resource, action=action.value,
            reason=reason,
            duration_ms=(time.time() - t0) * 1000)
        self._log(d)
        return d

    def _log(self, d: AccessDecision):
        self._db.execute(
            "INSERT INTO ac_decisions (allowed,principal_id,resource,action,reason,ts) "
            "VALUES (?,?,?,?,?,?)",
            (int(d.allowed), d.principal_id, d.resource,
             d.action, d.reason, d.ts))
        self._db.commit()

    # ── INTROSPECTION ─────────────────────────────────────────────────

    def effective_permissions(self, principal_id: str) -> List[Dict]:
        p = self._principals.get(principal_id)
        if not p: return []
        all_ids: Set[str] = set(p.direct_permissions)
        for rid in p.roles:
            all_ids.update(self._get_effective_permissions(rid))
        return [self._permissions[pid].to_dict()
                for pid in all_ids if pid in self._permissions]

    def audit_log(self, principal_id: Optional[str] = None,
                  limit: int = 50) -> List[Dict]:
        q = ("SELECT allowed,principal_id,resource,action,reason,ts "
             "FROM ac_decisions")
        params: List[Any] = []
        if principal_id:
            q += " WHERE principal_id=?"; params.append(principal_id)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"allowed": bool(r[0]), "principal": r[1],
                 "resource": r[2], "action": r[3],
                 "reason": r[4]} for r in rows]

    def stats(self) -> Dict[str, Any]:
        return {
            "permissions": len(self._permissions),
            "roles": len(self._roles),
            "principals": len(self._principals),
            "checks": self._check_count,
            "denied": self._deny_count,
            "deny_rate": round(self._deny_count / self._check_count, 4)
                         if self._check_count else 0.0,
        }
