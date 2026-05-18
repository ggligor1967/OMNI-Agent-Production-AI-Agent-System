"""OMNI Agent — Replay Buffer: experience replay for RL/RLHF agent training loops."""
from __future__ import annotations
import math, random, sqlite3, time, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class SamplingStrategy(str, Enum):
    UNIFORM     = "uniform"
    PRIORITIZED = "prioritized"   # PER - proportional to priority
    RECENCY     = "recency"       # favour recent experiences
    REWARD      = "reward"        # favour high-reward experiences


@dataclass
class Experience:
    exp_id: str
    state: Any
    action: Any
    reward: float
    next_state: Any
    done: bool = False
    priority: float = 1.0
    weight: float   = 1.0      # importance-sampling weight
    created_at: float = field(default_factory=time.time)
    episode: int   = 0
    step: int      = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exp_id": self.exp_id,
            "reward": self.reward,
            "done": self.done,
            "priority": round(self.priority, 4),
            "episode": self.episode,
            "step": self.step,
            "age_s": round(time.time() - self.created_at, 1),
        }


class ReplayBuffer:
    """
    Circular replay buffer with:
    - Uniform, Prioritized (PER), Recency, and Reward-weighted sampling
    - Priority updates (for PER)
    - N-step returns
    - Episode tracking
    - SQLite persistence for checkpointing
    """

    def __init__(
        self,
        capacity: int = 10_000,
        strategy: SamplingStrategy = SamplingStrategy.UNIFORM,
        alpha: float = 0.6,     # PER priority exponent
        beta: float  = 0.4,     # IS weight exponent (annealed to 1)
        db_path: str = ":memory:",
        seed: Optional[int] = None,
    ):
        self.capacity  = capacity
        self.strategy  = strategy
        self.alpha     = alpha
        self.beta      = beta
        self._rng      = random.Random(seed)
        self._buffer: List[Experience] = []
        self._pos      = 0          # write pointer (circular)
        self._episode  = 0
        self._step     = 0
        self._total_added = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS rb_experiences (
                exp_id TEXT PRIMARY KEY, reward REAL, done INTEGER,
                priority REAL, episode INTEGER, step INTEGER, added_at REAL
            )""")
        self._db.commit()

    # ── ADD ───────────────────────────────────────────────────────────

    def add(self, state: Any, action: Any, reward: float,
            next_state: Any, done: bool = False,
            priority: Optional[float] = None,
            metadata: Optional[Dict] = None) -> Experience:
        if done:
            self._episode += 1
        exp = Experience(
            exp_id=str(uuid.uuid4())[:8],
            state=state, action=action, reward=reward,
            next_state=next_state, done=done,
            priority=priority if priority is not None else 1.0,
            episode=self._episode, step=self._step,
            metadata=metadata or {},
        )
        self._step += 1
        self._total_added += 1
        if len(self._buffer) < self.capacity:
            self._buffer.append(exp)
        else:
            self._buffer[self._pos] = exp
        self._pos = (self._pos + 1) % self.capacity
        self._db.execute(
            "INSERT OR REPLACE INTO rb_experiences VALUES (?,?,?,?,?,?,?)",
            (exp.exp_id, reward, int(done), exp.priority,
             exp.episode, exp.step, exp.created_at))
        self._db.commit()
        return exp

    def add_episode(self, transitions: List[Tuple]) -> List[Experience]:
        """Add a full episode: list of (state, action, reward, next_state, done)."""
        return [self.add(*t) for t in transitions]

    # ── SAMPLE ────────────────────────────────────────────────────────

    def sample(self, n: int) -> List[Experience]:
        if len(self._buffer) == 0:
            return []
        n = min(n, len(self._buffer))
        if self.strategy == SamplingStrategy.UNIFORM:
            return self._rng.sample(self._buffer, n)
        if self.strategy == SamplingStrategy.PRIORITIZED:
            return self._per_sample(n)
        if self.strategy == SamplingStrategy.RECENCY:
            return self._recency_sample(n)
        if self.strategy == SamplingStrategy.REWARD:
            return self._reward_sample(n)
        return self._rng.sample(self._buffer, n)

    def _per_sample(self, n: int) -> List[Experience]:
        priorities = [e.priority ** self.alpha for e in self._buffer]
        total  = sum(priorities)
        probs  = [p / total for p in priorities]
        indices = self._rng.choices(range(len(self._buffer)), weights=probs, k=n)
        max_w  = (len(self._buffer) * min(probs)) ** (-self.beta) if min(probs) > 0 else 1.0
        batch  = []
        for i in indices:
            exp = self._buffer[i]
            w   = ((len(self._buffer) * probs[i]) ** (-self.beta)) / max_w
            exp.weight = w
            batch.append(exp)
        return batch

    def _recency_sample(self, n: int) -> List[Experience]:
        weights = [i + 1 for i in range(len(self._buffer))]
        total   = sum(weights)
        probs   = [w / total for w in weights]
        indices = self._rng.choices(range(len(self._buffer)), weights=probs, k=n)
        return [self._buffer[i] for i in indices]

    def _reward_sample(self, n: int) -> List[Experience]:
        rewards = [abs(e.reward) + 1e-6 for e in self._buffer]
        total   = sum(rewards)
        probs   = [r / total for r in rewards]
        indices = self._rng.choices(range(len(self._buffer)), weights=probs, k=n)
        return [self._buffer[i] for i in indices]

    # ── PRIORITY UPDATES ──────────────────────────────────────────────

    def update_priority(self, exp_id: str, priority: float):
        for exp in self._buffer:
            if exp.exp_id == exp_id:
                exp.priority = max(1e-6, priority)
                return

    def update_priorities(self, updates: Dict[str, float]):
        lookup = {e.exp_id: e for e in self._buffer}
        for exp_id, p in updates.items():
            if exp_id in lookup:
                lookup[exp_id].priority = max(1e-6, p)

    # ── N-STEP RETURNS ────────────────────────────────────────────────

    def n_step_returns(self, n: int = 3, gamma: float = 0.99) -> List[Experience]:
        """Compute n-step discounted returns for all buffered experiences."""
        buf = list(self._buffer)
        result = []
        for i, exp in enumerate(buf):
            G = 0.0
            for k in range(n):
                if i + k >= len(buf):
                    break
                G += (gamma ** k) * buf[i + k].reward
                if buf[i + k].done:
                    break
            updated = Experience(
                exp_id=exp.exp_id, state=exp.state, action=exp.action,
                reward=G, next_state=exp.next_state, done=exp.done,
                priority=exp.priority, episode=exp.episode, step=exp.step)
            result.append(updated)
        return result

    # ── QUERY ─────────────────────────────────────────────────────────

    def get(self, exp_id: str) -> Optional[Experience]:
        for exp in self._buffer:
            if exp.exp_id == exp_id:
                return exp
        return None

    def filter(self, fn: Callable[[Experience], bool]) -> List[Experience]:
        return [e for e in self._buffer if fn(e)]

    def by_episode(self, episode: int) -> List[Experience]:
        return [e for e in self._buffer if e.episode == episode]

    def latest(self, n: int = 10) -> List[Experience]:
        return self._buffer[-n:]

    # ── STATS ─────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._buffer)

    def is_full(self) -> bool:
        return len(self._buffer) >= self.capacity

    def mean_reward(self) -> float:
        if not self._buffer:
            return 0.0
        return sum(e.reward for e in self._buffer) / len(self._buffer)

    def max_reward(self) -> float:
        return max((e.reward for e in self._buffer), default=0.0)

    def min_reward(self) -> float:
        return min((e.reward for e in self._buffer), default=0.0)

    def clear(self):
        self._buffer.clear()
        self._pos = 0

    def stats(self) -> Dict[str, Any]:
        return {
            "size": len(self._buffer),
            "capacity": self.capacity,
            "full": self.is_full(),
            "strategy": self.strategy.value,
            "episodes": self._episode,
            "total_added": self._total_added,
            "mean_reward": round(self.mean_reward(), 4),
            "max_reward":  round(self.max_reward(), 4),
            "min_reward":  round(self.min_reward(), 4),
        }
