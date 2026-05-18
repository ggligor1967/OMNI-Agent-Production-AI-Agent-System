"""OMNI Agent — Contextual Bandit: UCB1 + Thompson Sampling for model routing."""
from __future__ import annotations
import math, random, sqlite3, time, threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class Strategy(str, Enum):
    UCB1     = "ucb1"
    THOMPSON = "thompson"
    EPSILON  = "epsilon_greedy"
    GREEDY   = "greedy"


@dataclass
class Arm:
    arm_id: str
    label: str
    n: int = 0          # total pulls
    total_reward: float = 0.0
    successes: float = 0.0   # for Thompson (beta prior α)
    failures: float = 0.0    # for Thompson (beta prior β)
    last_pulled: Optional[float] = None

    @property
    def mean_reward(self) -> float:
        return self.total_reward / self.n if self.n > 0 else 0.0

    def ucb1_score(self, total_n: int, c: float = 1.41) -> float:
        if self.n == 0:
            return float("inf")
        return self.mean_reward + c * math.sqrt(math.log(total_n) / self.n)

    def thompson_sample(self) -> float:
        alpha = self.successes + 1.0
        beta  = self.failures  + 1.0
        return random.betavariate(alpha, beta)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "label": self.label,
            "n": self.n,
            "mean_reward": self.mean_reward,
            "total_reward": self.total_reward,
            "last_pulled": self.last_pulled,
        }


class Bandit:
    """Multi-armed bandit with pluggable exploration strategy."""

    def __init__(
        self,
        bandit_id: str = "default",
        strategy: Strategy = Strategy.UCB1,
        epsilon: float = 0.1,
        ucb_c: float = 1.41,
        db_path: str = ":memory:",
    ):
        self.bandit_id = bandit_id
        self.strategy = strategy
        self.epsilon = epsilon
        self.ucb_c = ucb_c
        self._arms: Dict[str, Arm] = {}
        self._total_n = 0
        self._lock = threading.Lock()
        self._history: List[Dict[str, Any]] = []
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS bandit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bandit_id TEXT, arm_id TEXT, reward REAL, ts REAL, context TEXT
            )""")
        self._db.commit()

    # ── ARMS ──────────────────────────────────────────────────────────

    def add_arm(self, arm_id: str, label: str = "") -> Arm:
        arm = Arm(arm_id=arm_id, label=label or arm_id)
        with self._lock:
            self._arms[arm_id] = arm
        return arm

    def remove_arm(self, arm_id: str):
        with self._lock:
            self._arms.pop(arm_id, None)

    def get_arm(self, arm_id: str) -> Optional[Arm]:
        return self._arms.get(arm_id)

    # ── SELECT ────────────────────────────────────────────────────────

    def select(self, context: Optional[Dict] = None) -> str:
        """Select best arm according to strategy. Returns arm_id."""
        if not self._arms:
            raise ValueError("No arms registered")

        arms = list(self._arms.values())

        # Always explore arms with 0 pulls first
        unpulled = [a for a in arms if a.n == 0]
        if unpulled:
            chosen = random.choice(unpulled)
            return chosen.arm_id

        if self.strategy == Strategy.UCB1:
            chosen = max(arms, key=lambda a: a.ucb1_score(self._total_n, self.ucb_c))
        elif self.strategy == Strategy.THOMPSON:
            chosen = max(arms, key=lambda a: a.thompson_sample())
        elif self.strategy == Strategy.EPSILON:
            if random.random() < self.epsilon:
                chosen = random.choice(arms)
            else:
                chosen = max(arms, key=lambda a: a.mean_reward)
        else:  # GREEDY
            chosen = max(arms, key=lambda a: a.mean_reward)

        return chosen.arm_id

    # ── UPDATE ────────────────────────────────────────────────────────

    def update(self, arm_id: str, reward: float,
               context: Optional[Dict] = None):
        """Record reward for an arm pull."""
        arm = self._arms.get(arm_id)
        if arm is None:
            raise KeyError(f"Unknown arm: {arm_id}")
        with self._lock:
            arm.n += 1
            arm.total_reward += reward
            arm.last_pulled = time.time()
            self._total_n += 1
            # Thompson bookkeeping (reward treated as Bernoulli 0/1 or clipped)
            r_clipped = min(max(reward, 0.0), 1.0)
            arm.successes += r_clipped
            arm.failures  += (1.0 - r_clipped)
            # History
            record = {"arm_id": arm_id, "reward": reward,
                      "ts": time.time(), "context": str(context or {})}
            self._history.append(record)
            self._db.execute(
                "INSERT INTO bandit_history (bandit_id,arm_id,reward,ts,context) VALUES (?,?,?,?,?)",
                (self.bandit_id, arm_id, reward, record["ts"], record["context"]))
            self._db.commit()

    # ── CONVENIENCE ───────────────────────────────────────────────────

    def select_and_update(self, reward_fn, context: Optional[Dict] = None) -> Tuple[str, float]:
        """Select arm, compute reward via reward_fn(arm_id), update."""
        arm_id = self.select(context)
        reward = reward_fn(arm_id)
        self.update(arm_id, reward, context)
        return arm_id, reward

    def best_arm(self) -> Optional[str]:
        if not self._arms:
            return None
        return max(self._arms.values(), key=lambda a: a.mean_reward).arm_id

    def regret(self, optimal_reward: float) -> float:
        """Cumulative regret vs optimal reward."""
        actual = sum(a.total_reward for a in self._arms.values())
        return optimal_reward * self._total_n - actual

    # ── PERSISTENCE ───────────────────────────────────────────────────

    def history(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._history[-limit:]

    def db_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT arm_id,reward,ts FROM bandit_history WHERE bandit_id=? ORDER BY ts DESC LIMIT ?",
            (self.bandit_id, limit)).fetchall()
        return [{"arm_id": r[0], "reward": r[1], "ts": r[2]} for r in rows]

    # ── STATS ─────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "bandit_id": self.bandit_id,
            "strategy": self.strategy.value,
            "total_pulls": self._total_n,
            "arms": {k: v.to_dict() for k, v in self._arms.items()},
            "best_arm": self.best_arm(),
        }


# ── MULTI-BANDIT REGISTRY ─────────────────────────────────────────────────────

class BanditRegistry:
    """Registry of named bandits."""

    def __init__(self):
        self._bandits: Dict[str, Bandit] = {}

    def get_or_create(self, name: str, **kwargs) -> Bandit:
        if name not in self._bandits:
            self._bandits[name] = Bandit(bandit_id=name, **kwargs)
        return self._bandits[name]

    def list_bandits(self) -> List[str]:
        return list(self._bandits.keys())

    def stats_all(self) -> Dict[str, Any]:
        return {k: v.stats() for k, v in self._bandits.items()}
