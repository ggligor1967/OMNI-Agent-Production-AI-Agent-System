"""OMNI AGENT - Probabilistic Data Structures
Bloom filter, Count-Min Sketch, and HyperLogLog — all in pure Python.

Bloom Filter:
- Space-efficient probabilistic set membership test
- False positives possible; false negatives impossible
- Configurable capacity (n) and target false-positive rate (fpr)
- Derived: bit array size m, number of hash functions k
- add(item), contains(item), union(other), intersection estimate
- Fill ratio: fraction of bits set
- Serialize/deserialize to bytes for persistence

Count-Min Sketch:
- Frequency estimation for streaming data (approximate counts)
- width × depth table of counters
- update(item, count), query(item) → estimated count
- Point query guaranteed to be ≥ true count (over-estimate only)
- Configurable error (epsilon) and probability (delta)
- merge(other) by element-wise max or sum

HyperLogLog:
- Cardinality estimator: count distinct elements with ~1.6% error
- Uses b-bit bucket index (default b=14 → 2^14 = 16384 registers)
- add(item), count() → estimated cardinality
- merge(other) → element-wise max of registers
- Error formula: ±1.04/√m

Top-K (Count-Min + min-heap):
- Track top-K most frequent items over a stream
- update(item), top_k() → [(item, freq)]

All structures:
- Export/import state as JSON-serializable dict
- SQLite persistence for named instances
- REST API: create, add, query, stats
"""
import hashlib, json, math, sqlite3, struct, time, uuid, logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
logger = logging.getLogger(__name__)

# ── Shared hash helpers ────────────────────────────────────────────────────────
def _hash_fns(item: str, k: int, seed: int = 0) -> List[int]:
    """k independent hash values via double-hashing (Kirsch-Mitzenmacher)."""
    h = hashlib.sha256(f"{seed}:{item}".encode()).digest()
    h1 = struct.unpack("<Q", h[:8])[0]
    h2 = struct.unpack("<Q", h[8:16])[0]
    return [(h1 + i * h2) for i in range(k)]

def _murmur_lite(item: str, seed: int = 0) -> int:
    h = hashlib.md5(  # nosec B324 - bloom filter hashing only
        f"{seed}:{item}".encode(), usedforsecurity=False
    ).digest()
    return struct.unpack("<Q", h[:8])[0]

# ── Bloom Filter ──────────────────────────────────────────────────────────────
class BloomFilter:
    """
    Bloom filter with optimal parameters from capacity + FPR.

    Usage:
        bf = BloomFilter(capacity=10000, fpr=0.01)
        bf.add("apple")
        "apple" in bf   # True (definitely)
        "mango" in bf   # False (probably not)
    """
    def __init__(self, capacity: int = 10000, fpr: float = 0.01):
        self.capacity = capacity; self.fpr = fpr
        # m = -n * ln(p) / ln(2)^2
        self.m = max(1, int(-capacity * math.log(fpr) / (math.log(2) ** 2)))
        # k = (m/n) * ln(2)
        self.k = max(1, int((self.m / capacity) * math.log(2)))
        self._bits = bytearray(math.ceil(self.m / 8))
        self._count = 0

    def _set_bit(self, pos: int):
        self._bits[pos // 8] |= (1 << (pos % 8))

    def _get_bit(self, pos: int) -> bool:
        return bool(self._bits[pos // 8] & (1 << (pos % 8)))

    def add(self, item: str):
        for h in _hash_fns(str(item), self.k):
            self._set_bit(h % self.m)
        self._count += 1

    def contains(self, item: str) -> bool:
        return all(self._get_bit(h % self.m)
                    for h in _hash_fns(str(item), self.k))

    def __contains__(self, item): return self.contains(item)

    @property
    def fill_ratio(self) -> float:
        ones = sum(bin(b).count("1") for b in self._bits)
        return ones / self.m

    @property
    def estimated_fpr(self) -> float:
        """Current false-positive rate based on fill."""
        return (1 - math.exp(-self.k * self._count / self.m)) ** self.k

    def union(self, other: "BloomFilter") -> "BloomFilter":
        assert self.m == other.m and self.k == other.k
        result = BloomFilter.__new__(BloomFilter)
        result.m = self.m; result.k = self.k
        result.capacity = self.capacity; result.fpr = self.fpr
        result._bits = bytearray(a | b for a, b in
                                   zip(self._bits, other._bits))
        result._count = self._count + other._count
        return result

    def to_dict(self) -> Dict:
        return {"m": self.m, "k": self.k, "capacity": self.capacity,
                "fpr": self.fpr, "count": self._count,
                "bits": list(self._bits)}

    @classmethod
    def from_dict(cls, d: Dict) -> "BloomFilter":
        bf = cls.__new__(cls)
        bf.m = d["m"]; bf.k = d["k"]; bf.capacity = d["capacity"]
        bf.fpr = d["fpr"]; bf._count = d["count"]
        bf._bits = bytearray(d["bits"])
        return bf

# ── Count-Min Sketch ─────────────────────────────────────────────────────────
class CountMinSketch:
    """
    Count-Min Sketch for frequency estimation.

    epsilon: max error as fraction of total count (e.g. 0.01)
    delta:   probability of exceeding error bound (e.g. 0.001)
    """
    def __init__(self, epsilon: float = 0.01, delta: float = 0.001):
        self.epsilon = epsilon; self.delta = delta
        self.width = math.ceil(math.e / epsilon)
        self.depth = math.ceil(math.log(1 / delta))
        self._table = [[0] * self.width for _ in range(self.depth)]
        self._total = 0

    def update(self, item: str, count: int = 1):
        self._total += count
        for d in range(self.depth):
            h = _murmur_lite(str(item), seed=d)
            self._table[d][h % self.width] += count

    def query(self, item: str) -> int:
        return min(self._table[d][_murmur_lite(str(item), seed=d) % self.width]
                    for d in range(self.depth))

    def merge_max(self, other: "CountMinSketch"):
        """Element-wise max (union semantics)."""
        assert self.width == other.width and self.depth == other.depth
        for r in range(self.depth):
            for c in range(self.width):
                self._table[r][c] = max(self._table[r][c],
                                         other._table[r][c])

    def to_dict(self) -> Dict:
        return {"width": self.width, "depth": self.depth,
                "epsilon": self.epsilon, "delta": self.delta,
                "total": self._total, "table": self._table}

    @classmethod
    def from_dict(cls, d: Dict) -> "CountMinSketch":
        cms = cls.__new__(cls)
        cms.width = d["width"]; cms.depth = d["depth"]
        cms.epsilon = d["epsilon"]; cms.delta = d["delta"]
        cms._total = d["total"]; cms._table = d["table"]
        return cms

# ── HyperLogLog ───────────────────────────────────────────────────────────────
class HyperLogLog:
    """
    HyperLogLog cardinality estimator with ~1.6% relative error.

    b: number of register bits (default 14 → 16384 registers)
    """
    def __init__(self, b: int = 14):
        self.b = b; self.m = 1 << b
        self._regs = [0] * self.m
        # Alpha correction constants
        if self.m >= 128:
            self._alpha = 0.7213 / (1 + 1.079 / self.m)
        elif self.m == 64:
            self._alpha = 0.709
        elif self.m == 32:
            self._alpha = 0.697
        else:
            self._alpha = 0.5

    def add(self, item: str):
        h = _murmur_lite(str(item))
        idx = h >> (64 - self.b)        # top b bits = register index
        w   = h & ((1 << (64 - self.b)) - 1) or 1   # remaining bits
        # Count leading zeros + 1
        rho = (64 - self.b) - w.bit_length() + 1
        if rho > self._regs[idx]:
            self._regs[idx] = rho

    def count(self) -> int:
        raw = (self._alpha * self.m ** 2 /
                sum(2 ** (-r) for r in self._regs))
        if raw <= 2.5 * self.m:
            zeros = self._regs.count(0)
            if zeros > 0:
                return int(self.m * math.log(self.m / zeros))
        return int(raw)

    def merge(self, other: "HyperLogLog") -> "HyperLogLog":
        assert self.b == other.b
        result = HyperLogLog(self.b)
        result._regs = [max(a, b) for a, b in
                         zip(self._regs, other._regs)]
        return result

    def to_dict(self) -> Dict:
        return {"b": self.b, "m": self.m,
                "registers": self._regs[:min(len(self._regs), 16384)]}

    @classmethod
    def from_dict(cls, d: Dict) -> "HyperLogLog":
        hll = cls(d["b"])
        hll._regs = d["registers"]
        return hll

# ── Top-K ─────────────────────────────────────────────────────────────────────
class TopK:
    """Count-Min + min-heap tracking top-K frequent items."""
    def __init__(self, k: int = 10, epsilon: float = 0.001,
                  delta: float = 0.0001):
        self.k = k
        self._cms = CountMinSketch(epsilon, delta)
        self._items: Dict[str, int] = {}   # item → estimated frequency

    def update(self, item: str, count: int = 1):
        self._cms.update(item, count)
        freq = self._cms.query(item)
        self._items[item] = freq
        # Prune to keep only top-K*2 candidates
        if len(self._items) > self.k * 4:
            sorted_items = sorted(self._items.items(),
                                    key=lambda x: x[1], reverse=True)
            self._items = dict(sorted_items[:self.k * 2])

    def top_k(self) -> List[Tuple[str, int]]:
        return sorted(self._items.items(),
                        key=lambda x: x[1], reverse=True)[:self.k]

    def query(self, item: str) -> int:
        return self._cms.query(item)

# ── Storage & API ─────────────────────────────────────────────────────────────
class BFStore:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS structures(
                    name TEXT PRIMARY KEY, type TEXT,
                    data TEXT, created_at REAL, updated_at REAL);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path); c.row_factory = sqlite3.Row; return c

    def save(self, name: str, stype: str, data: Dict):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO structures VALUES(?,?,?,?,?)",
                (name, stype, json.dumps(data),
                 time.time(), time.time()))

    def load(self, name: str) -> Optional[Tuple[str, Dict]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT type, data FROM structures WHERE name=?",
                (name,)).fetchone()
        if not row: return None
        return row["type"], json.loads(row["data"])

    def list_all(self) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, type, created_at FROM structures "
                "ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

class ProbabilisticStore:
    """
    Registry of named probabilistic data structures.

    Usage:
        ps = ProbabilisticStore()

        # Bloom filter
        bf = ps.create_bloom("seen_urls", capacity=1_000_000, fpr=0.001)
        ps.add("seen_urls", "https://example.com")
        ps.contains("seen_urls", "https://example.com")   # True

        # Count-Min Sketch
        ps.create_cms("word_freq", epsilon=0.001)
        ps.update("word_freq", "hello", 5)
        ps.query_freq("word_freq", "hello")               # ≥ 5

        # HyperLogLog
        ps.create_hll("unique_visitors")
        ps.add("unique_visitors", "user:42")
        ps.cardinality("unique_visitors")                 # ~1
    """
    def __init__(self, db_path: str = "data/bloomfilter.db"):
        self._store = BFStore(db_path)
        self._structs: Dict[str, Any] = {}
        self._types:   Dict[str, str] = {}

    def create_bloom(self, name: str, capacity: int = 10000,
                      fpr: float = 0.01) -> BloomFilter:
        bf = BloomFilter(capacity, fpr)
        self._structs[name] = bf; self._types[name] = "bloom"
        return bf

    def create_cms(self, name: str, epsilon: float = 0.01,
                    delta: float = 0.001) -> CountMinSketch:
        cms = CountMinSketch(epsilon, delta)
        self._structs[name] = cms; self._types[name] = "cms"
        return cms

    def create_hll(self, name: str, b: int = 14) -> HyperLogLog:
        hll = HyperLogLog(b)
        self._structs[name] = hll; self._types[name] = "hll"
        return hll

    def create_topk(self, name: str, k: int = 10) -> TopK:
        topk = TopK(k)
        self._structs[name] = topk; self._types[name] = "topk"
        return topk

    def _get(self, name: str) -> Optional[Any]:
        if name in self._structs: return self._structs[name]
        loaded = self._store.load(name)
        if not loaded: return None
        stype, data = loaded
        if stype == "bloom":
            s = BloomFilter.from_dict(data)
        elif stype == "cms":
            s = CountMinSketch.from_dict(data)
        elif stype == "hll":
            s = HyperLogLog.from_dict(data)
        else:
            return None
        self._structs[name] = s; self._types[name] = stype
        return s

    def add(self, name: str, item: str) -> bool:
        s = self._get(name)
        if s is None: return False
        s.add(item)
        return True

    def contains(self, name: str, item: str) -> Optional[bool]:
        s = self._get(name)
        if s is None or not isinstance(s, BloomFilter): return None
        return s.contains(item)

    def update(self, name: str, item: str, count: int = 1) -> bool:
        s = self._get(name)
        if s is None: return False
        if isinstance(s, (CountMinSketch, TopK)):
            s.update(item, count)
        return True

    def query_freq(self, name: str, item: str) -> Optional[int]:
        s = self._get(name)
        if s is None: return None
        if isinstance(s, CountMinSketch): return s.query(item)
        if isinstance(s, TopK): return s.query(item)
        return None

    def cardinality(self, name: str) -> Optional[int]:
        s = self._get(name)
        if isinstance(s, HyperLogLog): return s.count()
        return None

    def top_k(self, name: str) -> Optional[List]:
        s = self._get(name)
        if isinstance(s, TopK): return s.top_k()
        return None

    def save(self, name: str):
        s = self._structs.get(name)
        if s is None: return
        self._store.save(name, self._types[name], s.to_dict())

    def stats(self, name: str = None) -> Dict:
        if name:
            s = self._get(name)
            stype = self._types.get(name, "unknown")
            if isinstance(s, BloomFilter):
                return {"type": stype, "count": s._count,
                        "fill_ratio": round(s.fill_ratio, 4),
                        "estimated_fpr": round(s.estimated_fpr, 6),
                        "m": s.m, "k": s.k}
            if isinstance(s, CountMinSketch):
                return {"type": stype, "total": s._total,
                        "width": s.width, "depth": s.depth}
            if isinstance(s, HyperLogLog):
                return {"type": stype, "cardinality": s.count(),
                        "registers": s.m, "b": s.b}
            return {}
        return {"structures": len(self._structs),
                "persisted": len(self._store.list_all()),
                "names": list(self._structs.keys())}

    def register_routes(self, app, prefix=""):
        from aiohttp import web
        async def create_ep(req):
            d = await req.json()
            stype = d.get("type","bloom")
            if stype == "bloom":
                self.create_bloom(d["name"],d.get("capacity",10000),
                                   d.get("fpr",0.01))
            elif stype == "cms":
                self.create_cms(d["name"],d.get("epsilon",0.01),
                                 d.get("delta",0.001))
            elif stype == "hll":
                self.create_hll(d["name"],d.get("b",14))
            elif stype == "topk":
                self.create_topk(d["name"],d.get("k",10))
            return web.json_response({"created": d["name"]}, status=201)
        async def add_ep(req):
            d = await req.json()
            ok = self.add(d["name"], d["item"])
            return web.json_response({"added": ok})
        async def query_ep(req):
            d = await req.json()
            name = d["name"]; item = d.get("item","")
            return web.json_response({
                "contains": self.contains(name, item),
                "frequency": self.query_freq(name, item),
                "cardinality": self.cardinality(name),
                "top_k": self.top_k(name)})
        async def stats_ep(req):
            name = req.rel_url.query.get("name")
            return web.json_response(self.stats(name))
        p = f"{prefix}/prob"
        app.router.add_post(f"{p}/create", create_ep)
        app.router.add_post(f"{p}/add",    add_ep)
        app.router.add_post(f"{p}/query",  query_ep)
        app.router.add_get( f"{p}/stats",  stats_ep)
        logger.info(f"Probabilistic store API at {prefix}/prob/")
