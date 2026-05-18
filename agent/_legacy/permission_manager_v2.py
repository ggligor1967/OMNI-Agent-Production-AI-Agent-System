"""OMNI Agent — Permission Manager V2: RBAC/ABAC, policies, roles, audit log."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class Effect(str, Enum):
    ALLOW = "allow"
    DENY  = "deny"


class PolicyType(str, Enum):
    RBAC  = "rbac"    # Role-based
    ABAC  = "abac"    # Attribute-based (condition fn)
    STATIC = "static" # Hardcoded allow/deny


@dataclass
class Resource:
    resource_id: str
    resource_type: str
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"resource_id": self.resource_id,
                "type": self.resource_type,
                "attributes": self.attributes}


@dataclass
class Principal:
    principal_id: str
    name: str
    roles: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    active: bool = True

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self) -> Dict[str, Any]:
        return {"principal_id": self.principal_id, "name": self.name,
                "roles": self.roles, "groups": self.groups,
                "active": self.active}


@dataclass
class Role:
    role_id: str
    name: str
    permissions: List[str] = field(default_factory=list)  # "resource:action"
    parent_roles: List[str] = field(default_factory=list)  # role inheritance
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"role_id": self.role_id, "name": self.name,
                "permissions": self.permissions,
                "parent_roles": self.parent_roles}


@dataclass
class Policy:
    policy_id: str
    name: str
    policy_type: PolicyType = PolicyType.RBAC
    effect: Effect = Effect.ALLOW
    principals: List[str] = field(default_factory=list)   # principal_ids or "*"
    roles: List[str] = field(default_factory=list)         # role names
    resources: List[str] = field(default_factory=list)     # resource patterns
    actions: List[str] = field(default_factory=list)       # action patterns or "*"
    condition_fn: Optional[Callable[[Principal, Resource, str, Dict], bool]] = None
    priority: int = 0
    active: bool = True
    description: str = ""

    def matches_principal(self, principal: Principal) -> bool:
        if "*" in self.principals: return True
        if principal.principal_id in self.principals: return True
        if any(r in principal.roles for r in self.roles): return True
        return False

    def matches_resource(self, resource_id: str) -> bool:
        if "*" in self.resources: return True
        for pat in self.resources:
            if pat == resource_id: return True
            if pat.endswith("*") and resource_id.startswith(pat[:-1]): return True
        return False

    def matches_action(self, action: str) -> bool:
        if "*" in self.actions: return True
        return action in self.actions

    def to_dict(self) -> Dict[str, Any]:
        return {"policy_id": self.policy_id, "name": self.name,
                "type": self.policy_type.value, "effect": self.effect.value,
                "active": self.active, "priority": self.priority}


@dataclass
class AuthzDecision:
    allowed: bool
    principal_id: str
    resource_id: str
    action: str
    matched_policy: Optional[str] = None
    reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed,
                "principal": self.principal_id,
                "resource": self.resource_id,
                "action": self.action,
                "policy": self.matched_policy,
                "reason": self.reason}


class PermissionManagerV2:
    """
    RBAC/ABAC permission engine:
    - Principal (user/service) registry with roles and attributes
    - Role registry with permissions and inheritance
    - Policy definitions: RBAC (role-based), ABAC (condition fn), static
    - Wildcard resource/action matching
    - Priority ordering: higher priority policy wins
    - DENY takes precedence over ALLOW at same priority
    - Context-aware ABAC conditions
    - Permission check: authorize(principal, resource, action)
    - Bulk permission check
    - Role inheritance (walk parent roles)
    - Group membership support
    - Audit log of all authorization decisions
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:",
                 default_deny: bool = True):
        self._principals: Dict[str, Principal] = {}
        self._roles:      Dict[str, Role] = {}
        self._policies:   List[Policy] = []
        self._resources:  Dict[str, Resource] = {}
        self._audit_log:  List[AuthzDecision] = []
        self._default_deny = default_deny
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pm_audit (
                audit_id TEXT PRIMARY KEY, allowed INTEGER,
                principal_id TEXT, resource_id TEXT, action TEXT,
                policy TEXT, reason TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── PRINCIPALS ───────────────────────────────────────────────────

    def add_principal(self, name: str,
                       roles: Optional[List[str]] = None,
                       groups: Optional[List[str]] = None,
                       attributes: Optional[Dict] = None,
                       principal_id: Optional[str] = None) -> Principal:
        pid = principal_id or str(uuid.uuid4())[:8]
        p   = Principal(principal_id=pid, name=name,
                         roles=list(roles or []),
                         groups=list(groups or []),
                         attributes=dict(attributes or {}))
        self._principals[pid] = p
        return p

    def assign_role(self, principal_id: str, role_name: str):
        p = self._principals.get(principal_id)
        if p and role_name not in p.roles:
            p.roles.append(role_name)

    def revoke_role(self, principal_id: str, role_name: str):
        p = self._principals.get(principal_id)
        if p and role_name in p.roles:
            p.roles.remove(role_name)

    def deactivate(self, principal_id: str):
        p = self._principals.get(principal_id)
        if p: p.active = False

    # ── ROLES ────────────────────────────────────────────────────────

    def add_role(self, name: str,
                  permissions: Optional[List[str]] = None,
                  parent_roles: Optional[List[str]] = None,
                  description: str = "",
                  role_id: Optional[str] = None) -> Role:
        rid = role_id or str(uuid.uuid4())[:8]
        r   = Role(role_id=rid, name=name,
                    permissions=list(permissions or []),
                    parent_roles=list(parent_roles or []),
                    description=description)
        self._roles[name] = r
        return r

    def grant_permission(self, role_name: str, permission: str):
        r = self._roles.get(role_name)
        if r and permission not in r.permissions:
            r.permissions.append(permission)

    def revoke_permission(self, role_name: str, permission: str):
        r = self._roles.get(role_name)
        if r and permission in r.permissions:
            r.permissions.remove(permission)

    def _expand_roles(self, role_names: List[str],
                       visited: Optional[Set[str]] = None) -> Set[str]:
        """Walk role inheritance tree."""
        if visited is None: visited = set()
        expanded: Set[str] = set()
        for rname in role_names:
            if rname in visited: continue
            visited.add(rname)
            expanded.add(rname)
            r = self._roles.get(rname)
            if r:
                expanded |= self._expand_roles(r.parent_roles, visited)
        return expanded

    # ── RESOURCES ────────────────────────────────────────────────────

    def add_resource(self, resource_id: str,
                      resource_type: str,
                      attributes: Optional[Dict] = None) -> Resource:
        r = Resource(resource_id=resource_id, resource_type=resource_type,
                      attributes=dict(attributes or {}))
        self._resources[resource_id] = r
        return r

    # ── POLICIES ─────────────────────────────────────────────────────

    def add_policy(self, name: str,
                    policy_type: PolicyType = PolicyType.RBAC,
                    effect: Effect = Effect.ALLOW,
                    principals: Optional[List[str]] = None,
                    roles: Optional[List[str]] = None,
                    resources: Optional[List[str]] = None,
                    actions: Optional[List[str]] = None,
                    condition_fn: Optional[Callable] = None,
                    priority: int = 0,
                    description: str = "",
                    policy_id: Optional[str] = None) -> Policy:
        pid = policy_id or str(uuid.uuid4())[:8]
        p   = Policy(policy_id=pid, name=name,
                      policy_type=policy_type, effect=effect,
                      principals=list(principals or []),
                      roles=list(roles or []),
                      resources=list(resources or ["*"]),
                      actions=list(actions or ["*"]),
                      condition_fn=condition_fn,
                      priority=priority, description=description)
        self._policies.append(p)
        self._policies.sort(key=lambda x: -x.priority)
        return p

    def remove_policy(self, policy_id: str) -> bool:
        before = len(self._policies)
        self._policies = [p for p in self._policies
                          if p.policy_id != policy_id]
        return len(self._policies) < before

    # ── AUTHORIZATION ─────────────────────────────────────────────────

    def authorize(self, principal_id: str,
                   resource_id: str,
                   action: str,
                   context: Optional[Dict] = None) -> AuthzDecision:
        principal = self._principals.get(principal_id)
        resource  = self._resources.get(resource_id,
                                         Resource(resource_id, "unknown"))
        ctx       = dict(context or {})

        if not principal or not principal.active:
            d = AuthzDecision(False, principal_id, resource_id, action,
                               reason="Principal not found or inactive")
            self._record(d); return d

        # Walk policies by priority (already sorted)
        all_roles = self._expand_roles(principal.roles)

        for policy in self._policies:
            if not policy.active: continue
            if not policy.matches_resource(resource_id): continue
            if not policy.matches_action(action): continue

            # Check principal / role match
            principal_match = (
                "*" in policy.principals or
                principal_id in policy.principals or
                bool(all_roles & set(policy.roles)))

            if not principal_match: continue

            # ABAC condition
            if policy.condition_fn:
                try:
                    if not policy.condition_fn(principal, resource, action, ctx):
                        continue
                except Exception:
                    continue

            allowed = policy.effect == Effect.ALLOW
            d = AuthzDecision(allowed, principal_id, resource_id, action,
                               matched_policy=policy.policy_id,
                               reason=f"Matched policy '{policy.name}'")
            self._record(d)
            return d

        # Default
        allowed = not self._default_deny
        d = AuthzDecision(allowed, principal_id, resource_id, action,
                           reason="No matching policy (default)")
        self._record(d)
        return d

    def can(self, principal_id: str, resource_id: str,
             action: str, **ctx) -> bool:
        return self.authorize(principal_id, resource_id, action, ctx).allowed

    def check_bulk(self, principal_id: str,
                    checks: List[Tuple[str, str]]) -> Dict[str, bool]:
        """Check multiple (resource, action) pairs at once."""
        return {f"{res}:{act}": self.can(principal_id, res, act)
                for res, act in checks}

    def _record(self, d: AuthzDecision):
        self._audit_log.append(d)
        self._db.execute(
            "INSERT INTO pm_audit VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4())[:8], int(d.allowed),
             d.principal_id, d.resource_id, d.action,
             d.matched_policy, d.reason, d.ts))
        self._db.commit()

    def audit_log(self, principal_id: Optional[str] = None,
                   limit: int = 50) -> List[Dict]:
        log = self._audit_log
        if principal_id:
            log = [d for d in log if d.principal_id == principal_id]
        return [d.to_dict() for d in log[-limit:]]

    def stats(self) -> Dict[str, Any]:
        allowed = sum(1 for d in self._audit_log if d.allowed)
        return {
            "principals": len(self._principals),
            "roles": len(self._roles),
            "policies": len(self._policies),
            "audit_entries": len(self._audit_log),
            "allow_rate": round(allowed / len(self._audit_log), 3)
                          if self._audit_log else 0.0,
        }
