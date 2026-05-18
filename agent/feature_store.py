"""OMNI Agent — Feature Store: ML feature registry with versioning, serving, materialization."""
from __future__ import annotations
import json, math, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class FeatureType(str, Enum):
    FLOAT     = "float"
    INTEGER   = "integer"
    STRING    = "string"
    BOOLEAN   = "boolean"
    EMBEDDING = "embedding"
    JSON      = "json"


class FeatureStatus(str, Enum):
    ACTIVE     = "active"
    DEPRECATED = "deprecated"
    DRAFT      = "draft"


@dataclass
class FeatureDef:
    feature_id: str
    name: str
    feature_group: str
    feature_type: FeatureType
    description: str = ""
    version: int = 1
    status: FeatureStatus = FeatureStatus.ACTIVE
    default_value: Any = None
    tags: List[str] = field(default_factory=list)
    transform: Optional[Callable] = None
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "name": self.name,
            "group": self.feature_group,
            "type": self.feature_type.value,
            "version": self.version,
            "status": self.status.value,
            "description": self.description,
        }


@dataclass
class FeatureVector:
    entity_id: str
    features: Dict[str, Any]
    feature_group: str = ""
    ts: float = field(default_factory=time.time)
    version: int = 1

    def get(self, name: str, default: Any = None) -> Any:
        return self.features.get(name, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "group": self.feature_group,
            "features": self.features,
            "ts": self.ts,
        }


@dataclass
class FeatureGroup:
    group_id: str
    name: str
    entity_type: str = "user"     # user | item | session | event
    description: str = ""
    ttl_s: Optional[float] = None
    online_enabled: bool = True
    offline_enabled: bool = True
    features: List[str] = field(default_factory=list)   # feature_ids
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_id": self.group_id,
            "name": self.name,
            "entity_type": self.entity_type,
            "features": len(self.features),
            "ttl_s": self.ttl_s,
        }


class FeatureStore:
    """
    ML Feature Store with:
    - Feature definitions (typed, versioned, with transforms)
    - Feature groups (logical collections per entity type)
    - Online store: fast key-value serving (dict + SQLite)
    - Offline store: historical feature retrieval with timestamps
    - Point-in-time correct feature retrieval
    - Materialization from source functions
    - Batch ingest and serving
    - Feature statistics (min, max, mean, null_rate)
    - Freshness tracking
    """

    def __init__(self, db_path: str = ":memory:"):
        self._features: Dict[str, FeatureDef] = {}
        self._groups: Dict[str, FeatureGroup] = {}
        self._name_idx: Dict[str, str] = {}           # name → feature_id
        self._online: Dict[str, Dict[str, Any]] = {}  # entity_id → {feat: val}
        self._online_ts: Dict[str, float] = {}        # entity_id → written_at
        self._sources: Dict[str, Callable] = {}       # group_id → fn(entity_id)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._serve_count = 0
        self._ingest_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS fs_offline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT, feature_group TEXT,
                features TEXT, ts REAL
            );
            CREATE TABLE IF NOT EXISTS fs_stats (
                feature_id TEXT PRIMARY KEY, count INTEGER,
                null_count INTEGER, min_val REAL, max_val REAL,
                sum_val REAL, last_updated REAL
            );
        """)
        self._db.commit()

    # ── FEATURE DEFINITION ────────────────────────────────────────────

    def register_feature(self, name: str,
                          feature_group: str,
                          feature_type: FeatureType = FeatureType.FLOAT,
                          description: str = "",
                          default_value: Any = None,
                          transform: Optional[Callable] = None,
                          tags: Optional[List[str]] = None,
                          feature_id: Optional[str] = None,
                          metadata: Optional[Dict] = None) -> FeatureDef:
        fid = feature_id or str(uuid.uuid4())[:8]
        feat = FeatureDef(
            feature_id=fid, name=name, feature_group=feature_group,
            feature_type=feature_type, description=description,
            default_value=default_value, transform=transform,
            tags=list(tags or []), metadata=metadata or {})
        self._features[fid] = feat
        self._name_idx[name] = fid
        if feature_group in self._groups:
            if fid not in self._groups[feature_group].features:
                self._groups[feature_group].features.append(fid)
        return feat

    def register_group(self, name: str,
                        entity_type: str = "user",
                        description: str = "",
                        ttl_s: Optional[float] = None,
                        group_id: Optional[str] = None) -> FeatureGroup:
        gid = group_id or name
        group = FeatureGroup(group_id=gid, name=name,
                              entity_type=entity_type,
                              description=description, ttl_s=ttl_s)
        self._groups[gid] = group
        return group

    def deprecate_feature(self, feature_id: str):
        f = self._features.get(feature_id)
        if f: f.status = FeatureStatus.DEPRECATED

    def get_feature_def(self, name: str) -> Optional[FeatureDef]:
        fid = self._name_idx.get(name)
        return self._features.get(fid) if fid else None

    # ── SOURCE REGISTRATION ───────────────────────────────────────────

    def register_source(self, group_id: str,
                         fn: Callable[[str], Dict[str, Any]]):
        """Register a function that computes features for an entity."""
        self._sources[group_id] = fn

    # ── INGEST (WRITE) ────────────────────────────────────────────────

    def ingest(self, entity_id: str,
               features: Dict[str, Any],
               feature_group: str = "",
               ts: Optional[float] = None) -> FeatureVector:
        """Write features for an entity to both online and offline stores."""
        now = ts or time.time()

        # Apply transforms
        transformed = {}
        for k, v in features.items():
            fid  = self._name_idx.get(k)
            feat = self._features.get(fid) if fid else None
            if feat and feat.transform:
                try: v = feat.transform(v)
                except Exception: pass
            transformed[k] = v

        # Online store (latest only)
        if entity_id not in self._online:
            self._online[entity_id] = {}
        self._online[entity_id].update(transformed)
        self._online_ts[entity_id] = now

        # Offline store (append history)
        self._db.execute(
            "INSERT INTO fs_offline (entity_id,feature_group,features,ts) "
            "VALUES (?,?,?,?)",
            (entity_id, feature_group, json.dumps(transformed, default=str), now))
        self._db.commit()

        self._ingest_count += 1
        self._update_stats(transformed)

        return FeatureVector(entity_id=entity_id, features=transformed,
                             feature_group=feature_group, ts=now)

    def ingest_batch(self, records: List[Dict[str, Any]],
                     feature_group: str = "") -> List[FeatureVector]:
        """Ingest a list of {entity_id, features} dicts."""
        return [self.ingest(r["entity_id"], r["features"],
                            feature_group=feature_group,
                            ts=r.get("ts"))
                for r in records]

    # ── MATERIALIZE ───────────────────────────────────────────────────

    def materialize(self, entity_id: str,
                    group_id: str) -> Optional[FeatureVector]:
        """Compute and store features using the registered source fn."""
        fn = self._sources.get(group_id)
        if not fn: return None
        try:
            features = fn(entity_id)
            return self.ingest(entity_id, features, feature_group=group_id)
        except Exception:
            return None

    def materialize_batch(self, entity_ids: List[str],
                           group_id: str) -> List[FeatureVector]:
        result = []
        for eid in entity_ids:
            fv = self.materialize(eid, group_id)
            if fv: result.append(fv)
        return result

    # ── SERVE (READ) ──────────────────────────────────────────────────

    def get_online(self, entity_id: str,
                   feature_names: Optional[List[str]] = None,
                   group_id: Optional[str] = None) -> Optional[FeatureVector]:
        """Serve latest features for an entity (online store)."""
        data = self._online.get(entity_id)
        if data is None:
            # Try materializing on-demand
            if group_id and group_id in self._sources:
                return self.materialize(entity_id, group_id)
            return None

        # Check TTL
        if group_id and group_id in self._groups:
            ttl = self._groups[group_id].ttl_s
            if ttl and (time.time() - self._online_ts.get(entity_id, 0)) > ttl:
                del self._online[entity_id]
                return None

        if feature_names:
            filtered = {k: data.get(k) for k in feature_names}
        else:
            filtered = dict(data)

        # Fill missing with defaults
        for name, val in filtered.items():
            if val is None:
                fid  = self._name_idx.get(name)
                feat = self._features.get(fid) if fid else None
                if feat: filtered[name] = feat.default_value

        self._serve_count += 1
        return FeatureVector(entity_id=entity_id, features=filtered,
                             feature_group=group_id or "",
                             ts=self._online_ts.get(entity_id, time.time()))

    def get_online_batch(self, entity_ids: List[str],
                          feature_names: Optional[List[str]] = None,
                          group_id: Optional[str] = None) -> Dict[str, FeatureVector]:
        result = {}
        for eid in entity_ids:
            fv = self.get_online(eid, feature_names, group_id)
            if fv: result[eid] = fv
        return result

    def get_offline(self, entity_id: str,
                    as_of: Optional[float] = None,
                    limit: int = 10) -> List[FeatureVector]:
        """Get historical features (point-in-time correct)."""
        q = "SELECT features,ts FROM fs_offline WHERE entity_id=?"
        params: List[Any] = [entity_id]
        if as_of:
            q += " AND ts <= ?"; params.append(as_of)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [FeatureVector(entity_id=entity_id,
                              features=json.loads(r[0]), ts=r[1])
                for r in rows]

    def get_feature_value(self, entity_id: str, feature_name: str) -> Any:
        """Convenience: get single feature value."""
        fv = self.get_online(entity_id)
        if fv: return fv.get(feature_name)
        fid  = self._name_idx.get(feature_name)
        feat = self._features.get(fid) if fid else None
        return feat.default_value if feat else None

    # ── STATISTICS ────────────────────────────────────────────────────

    def _update_stats(self, features: Dict[str, Any]):
        for name, val in features.items():
            fid = self._name_idx.get(name)
            if not fid: continue
            row = self._db.execute(
                "SELECT count,null_count,min_val,max_val,sum_val "
                "FROM fs_stats WHERE feature_id=?", (fid,)).fetchone()
            is_null = val is None
            try: num = float(val) if val is not None else 0.0
            except (TypeError, ValueError): num = 0.0

            if row:
                cnt, nc, mn, mx, sm = row
                cnt += 1
                nc  += int(is_null)
                mn   = min(mn, num) if mn is not None else num
                mx   = max(mx, num) if mx is not None else num
                sm  += num
                self._db.execute(
                    "UPDATE fs_stats SET count=?,null_count=?,min_val=?,"
                    "max_val=?,sum_val=?,last_updated=? WHERE feature_id=?",
                    (cnt, nc, mn, mx, sm, time.time(), fid))
            else:
                self._db.execute(
                    "INSERT INTO fs_stats VALUES (?,1,?,?,?,?,?)",
                    (fid, int(is_null), num, num, num, time.time()))
        self._db.commit()

    def feature_stats(self, feature_name: str) -> Optional[Dict[str, Any]]:
        fid = self._name_idx.get(feature_name)
        if not fid: return None
        row = self._db.execute(
            "SELECT count,null_count,min_val,max_val,sum_val "
            "FROM fs_stats WHERE feature_id=?", (fid,)).fetchone()
        if not row: return None
        cnt, nc, mn, mx, sm = row
        return {
            "feature": feature_name,
            "count": cnt,
            "null_rate": round(nc / cnt, 4) if cnt else 0.0,
            "min": mn, "max": mx,
            "mean": round(sm / cnt, 4) if cnt else 0.0,
        }

    def freshness(self, entity_id: str) -> Optional[float]:
        """Seconds since last feature update for this entity."""
        ts = self._online_ts.get(entity_id)
        return time.time() - ts if ts else None

    # ── QUERY ─────────────────────────────────────────────────────────

    def list_features(self, group: Optional[str] = None,
                       status: Optional[FeatureStatus] = None) -> List[Dict]:
        feats = list(self._features.values())
        if group:
            feats = [f for f in feats if f.feature_group == group]
        if status:
            feats = [f for f in feats if f.status == status]
        return [f.to_dict() for f in feats]

    def list_groups(self) -> List[Dict]:
        return [g.to_dict() for g in self._groups.values()]

    def stats(self) -> Dict[str, Any]:
        return {
            "features": len(self._features),
            "groups": len(self._groups),
            "entities_online": len(self._online),
            "ingest_count": self._ingest_count,
            "serve_count": self._serve_count,
        }
