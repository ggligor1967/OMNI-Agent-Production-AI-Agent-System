"""OMNI Agent — Data Augmentor: training data augmentation with transforms and sampling."""
from __future__ import annotations
import hashlib, json, random, re, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class AugmentStrategy(str, Enum):
    SYNONYM_SWAP   = "synonym_swap"
    PARAPHRASE     = "paraphrase"
    BACK_TRANSLATE = "back_translate"
    RANDOM_INSERT  = "random_insert"
    RANDOM_DELETE  = "random_delete"
    RANDOM_SWAP    = "random_swap"
    CASE_CHANGE    = "case_change"
    TYPO_INJECT    = "typo_inject"
    TEMPLATE_FILL  = "template_fill"
    LABEL_SMOOTH   = "label_smooth"
    MIXUP          = "mixup"
    CUSTOM         = "custom"


@dataclass
class AugSample:
    sample_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    original_id: str = ""
    text: str = ""
    label: Any = None
    strategy: AugmentStrategy = AugmentStrategy.CUSTOM
    metadata: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "original_id": self.original_id,
            "text": self.text[:200],
            "label": self.label,
            "strategy": self.strategy.value,
        }


@dataclass
class AugConfig:
    strategy: AugmentStrategy
    n_augments: int = 1       # augmentations per sample
    prob: float = 1.0         # probability this strategy applies to each sample
    params: Dict[str, Any] = field(default_factory=dict)


class DataAugmentor:
    """
    Training data augmentation pipeline:
    - Multiple strategies (synonym swap, case change, typo inject,
      random insert/delete/swap, template fill, mixup, label smooth)
    - Pluggable custom strategies
    - Per-strategy probability and count
    - Deduplication (hash-based)
    - Dataset balancing (oversample minority classes)
    - Train/val/test split
    - Augmentation audit (track source of each sample)
    - SQLite persistence
    """

    # Simple synonym map (no external deps)
    _SYNONYMS: Dict[str, List[str]] = {
        "good": ["great", "excellent", "fine", "positive"],
        "bad": ["poor", "terrible", "negative", "awful"],
        "fast": ["quick", "rapid", "swift", "speedy"],
        "slow": ["sluggish", "gradual", "leisurely"],
        "big": ["large", "huge", "enormous", "vast"],
        "small": ["tiny", "little", "compact", "minor"],
        "happy": ["glad", "joyful", "pleased", "content"],
        "sad": ["unhappy", "sorrowful", "melancholy"],
        "important": ["critical", "significant", "essential", "key"],
        "show": ["display", "present", "reveal", "demonstrate"],
    }

    _TYPOS: List[Tuple[str, str]] = [
        ("the", "teh"), ("and", "adn"), ("is", "si"),
        ("of", "fo"), ("to", "ot"), ("a", "aa"),
        ("in", "ni"), ("for", "fro"), ("on", "no"),
    ]

    def __init__(self, seed: int = 42, db_path: str = ":memory:"):
        self._rng = random.Random(seed)
        self._samples: List[AugSample] = []
        self._original: List[Dict[str, Any]] = []
        self._custom_strategies: Dict[str, Callable] = {}
        self._seen_hashes: set = set()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self._aug_count = 0

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS da_samples (
                sample_id TEXT PRIMARY KEY, original_id TEXT,
                text TEXT, label TEXT, strategy TEXT, ts REAL
            );
        """)
        self._db.commit()

    # ── DATA LOADING ──────────────────────────────────────────────────

    def load(self, records: List[Dict[str, Any]]) -> int:
        """Load original samples: [{text, label, id?}]."""
        for r in records:
            sid = r.get("id") or str(uuid.uuid4())[:8]
            self._original.append({"id": sid, "text": r["text"],
                                    "label": r.get("label")})
        return len(records)

    def add_sample(self, text: str, label: Any = None,
                   sample_id: Optional[str] = None) -> str:
        sid = sample_id or str(uuid.uuid4())[:8]
        self._original.append({"id": sid, "text": text, "label": label})
        return sid

    # ── STRATEGIES ────────────────────────────────────────────────────

    def _synonym_swap(self, text: str, **params) -> str:
        words = text.split()
        for i, w in enumerate(words):
            w_lower = w.lower()
            if w_lower in self._SYNONYMS and self._rng.random() < 0.3:
                words[i] = self._rng.choice(self._SYNONYMS[w_lower])
        return " ".join(words)

    def _random_delete(self, text: str, p: float = 0.1, **params) -> str:
        words = text.split()
        if len(words) <= 1: return text
        return " ".join(w for w in words if self._rng.random() > p)

    def _random_insert(self, text: str, **params) -> str:
        words = text.split()
        if not words: return text
        insert_word = self._rng.choice(list(self._SYNONYMS.keys()))
        pos = self._rng.randint(0, len(words))
        words.insert(pos, insert_word)
        return " ".join(words)

    def _random_swap(self, text: str, n: int = 1, **params) -> str:
        words = text.split()
        if len(words) < 2: return text
        for _ in range(n):
            i, j = self._rng.sample(range(len(words)), 2)
            words[i], words[j] = words[j], words[i]
        return " ".join(words)

    def _case_change(self, text: str, **params) -> str:
        choice = self._rng.choice(["upper", "lower", "title"])
        if choice == "upper": return text.upper()
        if choice == "lower": return text.lower()
        return text.title()

    def _typo_inject(self, text: str, **params) -> str:
        for orig, typo in self._TYPOS:
            if orig in text and self._rng.random() < 0.3:
                text = text.replace(orig, typo, 1)
                break
        return text

    def _template_fill(self, text: str,
                        templates: Optional[List[str]] = None, **params) -> str:
        if not templates:
            templates = [
                "In other words, {text}",
                "To put it simply: {text}",
                "Consider this: {text}",
            ]
        tmpl = self._rng.choice(templates)
        return tmpl.replace("{text}", text)

    def _apply_strategy(self, cfg: AugConfig, text: str) -> str:
        p = cfg.params
        s = cfg.strategy
        if s == AugmentStrategy.SYNONYM_SWAP:   return self._synonym_swap(text, **p)
        if s == AugmentStrategy.RANDOM_DELETE:  return self._random_delete(text, **p)
        if s == AugmentStrategy.RANDOM_INSERT:  return self._random_insert(text, **p)
        if s == AugmentStrategy.RANDOM_SWAP:    return self._random_swap(text, **p)
        if s == AugmentStrategy.CASE_CHANGE:    return self._case_change(text, **p)
        if s == AugmentStrategy.TYPO_INJECT:    return self._typo_inject(text, **p)
        if s == AugmentStrategy.TEMPLATE_FILL:  return self._template_fill(text, **p)
        if s == AugmentStrategy.CUSTOM:
            fn_name = p.get("fn")
            fn = self._custom_strategies.get(fn_name) if fn_name else None
            return fn(text) if fn else text
        return text

    # ── AUGMENTATION ──────────────────────────────────────────────────

    def augment(self, configs: List[AugConfig],
                deduplicate: bool = True) -> List[AugSample]:
        new_samples: List[AugSample] = []
        for record in self._original:
            for cfg in configs:
                if self._rng.random() > cfg.prob:
                    continue
                for _ in range(cfg.n_augments):
                    aug_text = self._apply_strategy(cfg, record["text"])
                    if deduplicate:
                        h = hashlib.md5(  # nosec B324 - augmentation dedup key only
                            aug_text.encode(), usedforsecurity=False
                        ).hexdigest()
                        if h in self._seen_hashes:
                            continue
                        self._seen_hashes.add(h)
                    sample = AugSample(
                        original_id=record["id"],
                        text=aug_text, label=record["label"],
                        strategy=cfg.strategy)
                    new_samples.append(sample)
                    self._samples.append(sample)
                    self._aug_count += 1
                    self._persist(sample)
        return new_samples

    def augment_one(self, text: str, cfg: AugConfig) -> str:
        return self._apply_strategy(cfg, text)

    # ── MIXUP ─────────────────────────────────────────────────────────

    def mixup(self, alpha: float = 0.5) -> List[AugSample]:
        """Mix pairs of samples (text concatenation with label avg)."""
        results = []
        srcs = self._original[:]
        self._rng.shuffle(srcs)
        for i in range(0, len(srcs) - 1, 2):
            a, b = srcs[i], srcs[i + 1]
            mixed_text = f"{a['text']} {b['text']}"
            try:
                mixed_label = alpha * float(a["label"]) + (1 - alpha) * float(b["label"])
            except (TypeError, ValueError):
                mixed_label = a["label"]
            sample = AugSample(
                original_id=f"{a['id']}+{b['id']}",
                text=mixed_text, label=mixed_label,
                strategy=AugmentStrategy.MIXUP)
            results.append(sample)
            self._samples.append(sample)
            self._persist(sample)
        return results

    # ── BALANCING ─────────────────────────────────────────────────────

    def balance_classes(self, cfg: Optional[AugConfig] = None) -> List[AugSample]:
        """Oversample minority classes to match majority count."""
        if cfg is None:
            cfg = AugConfig(strategy=AugmentStrategy.SYNONYM_SWAP, n_augments=1)
        # Count classes
        counts: Dict[Any, int] = {}
        label_records: Dict[Any, List] = {}
        for r in self._original:
            lbl = r["label"]
            counts[lbl] = counts.get(lbl, 0) + 1
            label_records.setdefault(lbl, []).append(r)
        if not counts: return []
        max_count = max(counts.values())
        new_samples = []
        for lbl, records in label_records.items():
            deficit = max_count - len(records)
            for _ in range(deficit):
                src = self._rng.choice(records)
                aug_text = self._apply_strategy(cfg, src["text"])
                sample = AugSample(
                    original_id=src["id"],
                    text=aug_text, label=lbl,
                    strategy=cfg.strategy)
                new_samples.append(sample)
                self._samples.append(sample)
                self._persist(sample)
        return new_samples

    # ── SPLIT ─────────────────────────────────────────────────────────

    def split(self, train: float = 0.8, val: float = 0.1,
              shuffle: bool = True) -> Tuple[List, List, List]:
        all_data = self._original + [
            {"id": s.sample_id, "text": s.text, "label": s.label}
            for s in self._samples]
        if shuffle:
            self._rng.shuffle(all_data)
        n = len(all_data)
        t = int(n * train)
        v = int(n * val)
        return all_data[:t], all_data[t:t + v], all_data[t + v:]

    # ── CUSTOM ────────────────────────────────────────────────────────

    def register_strategy(self, name: str, fn: Callable[[str], str]):
        self._custom_strategies[name] = fn

    def _persist(self, s: AugSample):
        self._db.execute(
            "INSERT OR IGNORE INTO da_samples VALUES (?,?,?,?,?,?)",
            (s.sample_id, s.original_id, s.text[:1000],
             json.dumps(s.label), s.strategy.value, s.ts))
        self._db.commit()

    # ── QUERY ─────────────────────────────────────────────────────────

    def get_all(self) -> List[AugSample]:
        return list(self._samples)

    def filter_by_strategy(self, strategy: AugmentStrategy) -> List[AugSample]:
        return [s for s in self._samples if s.strategy == strategy]

    def stats(self) -> Dict[str, Any]:
        strategy_counts: Dict[str, int] = {}
        for s in self._samples:
            k = s.strategy.value
            strategy_counts[k] = strategy_counts.get(k, 0) + 1
        return {
            "original_samples": len(self._original),
            "augmented_samples": len(self._samples),
            "total": len(self._original) + len(self._samples),
            "aug_count": self._aug_count,
            "strategies_used": strategy_counts,
        }
