"""OMNI Agent — Model Registry V2: ML model versioning, lineage, serving metadata."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class ModelStage(str, Enum):
    DEVELOPMENT = "development"
    STAGING     = "staging"
    PRODUCTION  = "production"
    ARCHIVED    = "archived"
    DEPRECATED  = "deprecated"


class ModelFramework(str, Enum):
    PYTORCH     = "pytorch"
    TENSORFLOW  = "tensorflow"
    SKLEARN     = "sklearn"
    HUGGINGFACE = "huggingface"
    ONNX        = "onnx"
    CUSTOM      = "custom"
    LLM         = "llm"


@dataclass
class ModelVersion:
    version_id: str
    model_id: str
    version: str               # e.g. "1.0.0"
    stage: ModelStage = ModelStage.DEVELOPMENT
    framework: ModelFramework = ModelFramework.CUSTOM
    artifact_path: str = ""    # path/URI to model artifact
    metrics: Dict[str, float] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    training_run_id: Optional[str] = None
    parent_version_id: Optional[str] = None   # lineage
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    is_champion: bool = False
    is_challenger: bool = False
    serving_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "model_id": self.model_id,
            "version": self.version,
            "stage": self.stage.value,
            "framework": self.framework.value,
            "metrics": self.metrics,
            "is_champion": self.is_champion,
            "created_at": self.created_at,
        }


@dataclass
class ModelEntry:
    model_id: str
    name: str
    description: str = ""
    task_type: str = ""          # classification, regression, generation…
    input_schema: Dict = field(default_factory=dict)
    output_schema: Dict = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    latest_version_id: Optional[str] = None
    champion_version_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "task_type": self.task_type,
            "owner": self.owner,
            "latest_version": self.latest_version_id,
            "champion_version": self.champion_version_id,
        }


@dataclass
class ModelLineageNode:
    node_id: str
    version_id: str
    parent_ids: List[str] = field(default_factory=list)
    child_ids: List[str] = field(default_factory=list)
    operation: str = ""   # finetune | distill | merge | retrain


class ModelRegistryV2:
    """
    ML Model Registry with:
    - Model entries with multiple versions
    - Stage transitions (dev → staging → prod → archived)
    - Champion/Challenger designation per model
    - Metrics and parameter tracking per version
    - Lineage graph (parent → child version chains)
    - Comparison between versions
    - Tag-based search
    - Serving config per version
    - Approval workflow (promote/demote stages)
    - SQLite persistence
    """

    def __init__(self, db_path: str = ":memory:"):
        self._models:   Dict[str, ModelEntry] = {}
        self._versions: Dict[str, ModelVersion] = {}
        self._lineage:  Dict[str, ModelLineageNode] = {}
        self._approval_hooks: List[Callable] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS mr_models (
                model_id TEXT PRIMARY KEY, name TEXT, task_type TEXT,
                description TEXT, owner TEXT, tags TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS mr_versions (
                version_id TEXT PRIMARY KEY, model_id TEXT, version TEXT,
                stage TEXT, framework TEXT, artifact_path TEXT,
                metrics TEXT, params TEXT, tags TEXT, description TEXT,
                training_run_id TEXT, parent_version_id TEXT,
                created_by TEXT, created_at REAL, is_champion INTEGER,
                serving_config TEXT
            );
            CREATE TABLE IF NOT EXISTS mr_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id TEXT, from_stage TEXT, to_stage TEXT,
                transitioned_by TEXT, ts REAL, reason TEXT
            );
        """)
        self._db.commit()

    # ── MODEL MANAGEMENT ──────────────────────────────────────────────

    def register_model(self, name: str,
                        description: str = "",
                        task_type: str = "",
                        tags: Optional[List[str]] = None,
                        owner: str = "",
                        model_id: Optional[str] = None) -> ModelEntry:
        mid = model_id or str(uuid.uuid4())[:8]
        m   = ModelEntry(model_id=mid, name=name,
                          description=description, task_type=task_type,
                          tags=list(tags or []), owner=owner)
        self._models[mid] = m
        self._db.execute(
            "INSERT OR REPLACE INTO mr_models VALUES (?,?,?,?,?,?,?)",
            (mid, name, task_type, description, owner,
             json.dumps(tags or []), m.created_at))
        self._db.commit()
        return m

    def get_model(self, model_id: str) -> Optional[ModelEntry]:
        return self._models.get(model_id)

    def find_model(self, name: str) -> Optional[ModelEntry]:
        return next((m for m in self._models.values()
                     if m.name == name), None)

    def list_models(self, task_type: Optional[str] = None,
                     tag: Optional[str] = None) -> List[Dict]:
        models = list(self._models.values())
        if task_type:
            models = [m for m in models if m.task_type == task_type]
        if tag:
            models = [m for m in models if tag in m.tags]
        return [m.to_dict() for m in models]

    # ── VERSION MANAGEMENT ────────────────────────────────────────────

    def log_version(self, model_id: str,
                     version: str,
                     stage: ModelStage = ModelStage.DEVELOPMENT,
                     framework: ModelFramework = ModelFramework.CUSTOM,
                     artifact_path: str = "",
                     metrics: Optional[Dict[str, float]] = None,
                     params: Optional[Dict] = None,
                     tags: Optional[Dict[str, str]] = None,
                     description: str = "",
                     training_run_id: Optional[str] = None,
                     parent_version_id: Optional[str] = None,
                     created_by: str = "",
                     serving_config: Optional[Dict] = None,
                     version_id: Optional[str] = None) -> ModelVersion:
        model = self._models.get(model_id)
        if not model:
            raise KeyError(f"Model {model_id} not registered")
        vid = version_id or str(uuid.uuid4())[:8]
        v   = ModelVersion(
            version_id=vid, model_id=model_id, version=version,
            stage=stage, framework=framework,
            artifact_path=artifact_path,
            metrics=dict(metrics or {}),
            params=dict(params or {}),
            tags=dict(tags or {}),
            description=description,
            training_run_id=training_run_id,
            parent_version_id=parent_version_id,
            created_by=created_by,
            serving_config=dict(serving_config or {}))
        self._versions[vid] = v
        model.latest_version_id = vid

        # Lineage
        ln = ModelLineageNode(node_id=vid, version_id=vid)
        if parent_version_id and parent_version_id in self._lineage:
            ln.parent_ids.append(parent_version_id)
            self._lineage[parent_version_id].child_ids.append(vid)
        self._lineage[vid] = ln

        self._persist_version(v)
        return v

    def get_version(self, version_id: str) -> Optional[ModelVersion]:
        return self._versions.get(version_id)

    def get_latest(self, model_id: str) -> Optional[ModelVersion]:
        model = self._models.get(model_id)
        if not model or not model.latest_version_id:
            return None
        return self._versions.get(model.latest_version_id)

    def get_champion(self, model_id: str) -> Optional[ModelVersion]:
        model = self._models.get(model_id)
        if not model or not model.champion_version_id:
            return None
        return self._versions.get(model.champion_version_id)

    def list_versions(self, model_id: str,
                       stage: Optional[ModelStage] = None) -> List[Dict]:
        vs = [v for v in self._versions.values() if v.model_id == model_id]
        if stage:
            vs = [v for v in vs if v.stage == stage]
        return sorted([v.to_dict() for v in vs],
                       key=lambda d: d["created_at"], reverse=True)

    # ── STAGE TRANSITIONS ─────────────────────────────────────────────

    def transition(self, version_id: str,
                    to_stage: ModelStage,
                    transitioned_by: str = "",
                    reason: str = "") -> bool:
        v = self._versions.get(version_id)
        if not v: return False
        from_stage = v.stage
        for fn in self._approval_hooks:
            try:
                approved = fn(v, from_stage, to_stage)
                if not approved: return False
            except Exception:
                return False
        v.stage      = to_stage
        v.updated_at = time.time()
        self._db.execute(
            "INSERT INTO mr_transitions "
            "(version_id,from_stage,to_stage,transitioned_by,ts,reason) "
            "VALUES (?,?,?,?,?,?)",
            (version_id, from_stage.value, to_stage.value,
             transitioned_by, time.time(), reason))
        self._db.commit()
        self._persist_version(v)
        return True

    def promote_to_production(self, version_id: str, **kwargs) -> bool:
        return self.transition(version_id, ModelStage.PRODUCTION, **kwargs)

    def archive(self, version_id: str, **kwargs) -> bool:
        return self.transition(version_id, ModelStage.ARCHIVED, **kwargs)

    # ── CHAMPION / CHALLENGER ─────────────────────────────────────────

    def set_champion(self, version_id: str) -> bool:
        v = self._versions.get(version_id)
        if not v: return False
        model = self._models.get(v.model_id)
        if not model: return False
        # Clear previous champion
        if model.champion_version_id:
            old = self._versions.get(model.champion_version_id)
            if old: old.is_champion = False
        v.is_champion = True
        model.champion_version_id = version_id
        self._persist_version(v)
        return True

    def set_challenger(self, version_id: str) -> bool:
        v = self._versions.get(version_id)
        if not v: return False
        v.is_challenger = True
        self._persist_version(v)
        return True

    # ── METRICS ───────────────────────────────────────────────────────

    def log_metric(self, version_id: str,
                    key: str, value: float) -> bool:
        v = self._versions.get(version_id)
        if not v: return False
        v.metrics[key] = value
        v.updated_at   = time.time()
        self._persist_version(v)
        return True

    def compare_versions(self, version_ids: List[str],
                          metric: str) -> List[Tuple[str, float]]:
        results = []
        for vid in version_ids:
            v = self._versions.get(vid)
            if v and metric in v.metrics:
                results.append((vid, v.metrics[metric]))
        return sorted(results, key=lambda x: -x[1])

    def best_version(self, model_id: str,
                      metric: str,
                      stage: Optional[ModelStage] = None) -> Optional[ModelVersion]:
        vs = [v for v in self._versions.values() if v.model_id == model_id]
        if stage: vs = [v for v in vs if v.stage == stage]
        vs = [v for v in vs if metric in v.metrics]
        if not vs: return None
        return max(vs, key=lambda v: v.metrics[metric])

    # ── LINEAGE ───────────────────────────────────────────────────────

    def get_lineage(self, version_id: str) -> Dict[str, Any]:
        ln = self._lineage.get(version_id)
        if not ln: return {}
        return {
            "version_id": version_id,
            "parents": ln.parent_ids,
            "children": ln.child_ids,
        }

    def lineage_chain(self, version_id: str) -> List[str]:
        """Walk up to root parent."""
        chain = [version_id]
        vid = version_id
        while True:
            ln = self._lineage.get(vid)
            if not ln or not ln.parent_ids: break
            vid = ln.parent_ids[0]
            chain.append(vid)
        return list(reversed(chain))

    # ── APPROVAL HOOK ─────────────────────────────────────────────────

    def add_approval_hook(self, fn: Callable):
        """fn(version, from_stage, to_stage) → bool"""
        self._approval_hooks.append(fn)

    # ── TRANSITION HISTORY ────────────────────────────────────────────

    def transition_history(self, version_id: Optional[str] = None,
                            limit: int = 50) -> List[Dict]:
        q = ("SELECT version_id,from_stage,to_stage,transitioned_by,ts,reason "
             "FROM mr_transitions")
        params: List[Any] = []
        if version_id:
            q += " WHERE version_id=?"; params.append(version_id)
        q += " ORDER BY ts DESC LIMIT ?"; params.append(limit)
        rows = self._db.execute(q, params).fetchall()
        return [{"version": r[0], "from": r[1], "to": r[2],
                 "by": r[3], "reason": r[5]} for r in rows]

    def _persist_version(self, v: ModelVersion):
        self._db.execute(
            "INSERT OR REPLACE INTO mr_versions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v.version_id, v.model_id, v.version,
             v.stage.value, v.framework.value,
             v.artifact_path,
             json.dumps(v.metrics), json.dumps(v.params),
             json.dumps(v.tags), v.description,
             v.training_run_id, v.parent_version_id,
             v.created_by, v.created_at, int(v.is_champion),
             json.dumps(v.serving_config)))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        by_stage: Dict[str, int] = {}
        for v in self._versions.values():
            k = v.stage.value
            by_stage[k] = by_stage.get(k, 0) + 1
        return {
            "models": len(self._models),
            "versions": len(self._versions),
            "by_stage": by_stage,
        }
