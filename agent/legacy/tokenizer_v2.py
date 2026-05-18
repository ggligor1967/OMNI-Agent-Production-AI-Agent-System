"""OMNI Agent — Tokenizer V2: text tokenization, vocabulary, encoding/decoding."""
from __future__ import annotations
import json, re, sqlite3, time, uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Vocabulary:
    vocab_id: str
    name: str
    token_to_id: Dict[str, int] = field(default_factory=dict)
    id_to_token: Dict[int, str] = field(default_factory=dict)
    special_tokens: Dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def size(self) -> int:
        return len(self.token_to_id)

    def add(self, token: str) -> int:
        if token not in self.token_to_id:
            idx = len(self.token_to_id)
            self.token_to_id[token] = idx
            self.id_to_token[idx]   = token
        return self.token_to_id[token]

    def get_id(self, token: str) -> Optional[int]:
        return self.token_to_id.get(token)

    def get_token(self, idx: int) -> Optional[str]:
        return self.id_to_token.get(idx)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vocab_id": self.vocab_id,
            "name": self.name,
            "size": self.size,
            "special_tokens": list(self.special_tokens.keys()),
        }


@dataclass
class TokenizerConfig:
    lowercase: bool = True
    strip_punctuation: bool = False
    max_length: Optional[int] = None
    pad_token: str = "<pad>"
    unk_token: str = "<unk>"
    bos_token: str = "<bos>"
    eos_token: str = "<eos>"
    sep_token: str = "<sep>"
    mask_token: str = "<mask>"
    add_bos: bool = False
    add_eos: bool = False
    padding: bool = False
    truncation: bool = True


class TokenizerV2:
    """
    Text tokenization engine:
    - Word-level, character-level, and subword (BPE-style) tokenization
    - Vocabulary building from corpus with frequency threshold
    - BPE (Byte-Pair Encoding) merge rules learning
    - Encoding: text → token IDs
    - Decoding: token IDs → text
    - Special tokens (PAD, UNK, BOS, EOS, SEP, MASK)
    - Padding and truncation
    - Batch encoding/decoding
    - Vocabulary serialization (save/load JSON)
    - Token frequency statistics
    - Named vocabulary registry
    - SQLite persistence
    """

    def __init__(self, config: Optional[TokenizerConfig] = None,
                 db_path: str = ":memory:"):
        self.config    = config or TokenizerConfig()
        self._vocabs:  Dict[str, Vocabulary] = {}
        self._bpe_merges: Dict[str, List[Tuple[str, str]]] = {}
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tk_vocabs (
                vocab_id TEXT PRIMARY KEY, name TEXT,
                token_to_id TEXT, special_tokens TEXT, created_at REAL
            );
        """)
        self._db.commit()

    # ── VOCABULARY ───────────────────────────────────────────────────

    def create_vocab(self, name: str,
                     vocab_id: Optional[str] = None) -> Vocabulary:
        vid = vocab_id or str(uuid.uuid4())[:8]
        v   = Vocabulary(vocab_id=vid, name=name)
        cfg = self.config
        for tok in [cfg.pad_token, cfg.unk_token, cfg.bos_token,
                    cfg.eos_token, cfg.sep_token, cfg.mask_token]:
            idx = v.add(tok)
            v.special_tokens[tok] = idx
        self._vocabs[vid] = v
        return v

    def build_vocab(self, corpus: List[str],
                    vocab_id: Optional[str] = None,
                    name: str = "built",
                    min_freq: int = 1,
                    max_size: Optional[int] = None) -> Vocabulary:
        v   = self.create_vocab(name, vocab_id)
        freq: Counter = Counter()
        for text in corpus:
            for tok in self._word_tokenize(text):
                freq[tok] += 1
        tokens = [(t, c) for t, c in freq.items() if c >= min_freq]
        tokens.sort(key=lambda x: -x[1])
        if max_size:
            tokens = tokens[:max_size - v.size]
        for tok, _ in tokens:
            v.add(tok)
        self._persist_vocab(v)
        return v

    def load_vocab(self, data: Dict[str, Any]) -> Vocabulary:
        vid  = data.get("vocab_id", str(uuid.uuid4())[:8])
        v    = Vocabulary(vocab_id=vid, name=data.get("name", "loaded"))
        t2i  = data.get("token_to_id", {})
        for tok, idx in t2i.items():
            v.token_to_id[tok] = idx
            v.id_to_token[idx] = tok
        v.special_tokens = data.get("special_tokens", {})
        self._vocabs[vid] = v
        return v

    def save_vocab(self, vocab_id: str) -> Dict[str, Any]:
        v = self._vocabs.get(vocab_id)
        if not v: raise KeyError(f"Vocab {vocab_id} not found")
        return {
            "vocab_id": v.vocab_id,
            "name": v.name,
            "token_to_id": v.token_to_id,
            "special_tokens": v.special_tokens,
        }

    def get_vocab(self, vocab_id: str) -> Optional[Vocabulary]:
        return self._vocabs.get(vocab_id)

    # ── TOKENIZATION ─────────────────────────────────────────────────

    def _word_tokenize(self, text: str) -> List[str]:
        if self.config.lowercase:
            text = text.lower()
        if self.config.strip_punctuation:
            text = re.sub(r"[^\w\s]", "", text)
        return text.split()

    def _char_tokenize(self, text: str) -> List[str]:
        if self.config.lowercase:
            text = text.lower()
        return list(text)

    def _bpe_tokenize(self, text: str,
                       merge_rules: List[Tuple[str, str]]) -> List[str]:
        if self.config.lowercase:
            text = text.lower()
        words = text.split()
        result = []
        for word in words:
            chars = list(word) + ["</w>"]
            for a, b in merge_rules:
                i = 0
                merged = []
                while i < len(chars):
                    if i < len(chars) - 1 and chars[i] == a and chars[i+1] == b:
                        merged.append(a + b)
                        i += 2
                    else:
                        merged.append(chars[i])
                        i += 1
                chars = merged
            result.extend(chars)
        return result

    def tokenize(self, text: str,
                  mode: str = "word") -> List[str]:
        if mode == "char":
            return self._char_tokenize(text)
        elif mode == "bpe" and self._bpe_merges:
            rules = next(iter(self._bpe_merges.values()), [])
            return self._bpe_tokenize(text, rules)
        return self._word_tokenize(text)

    # ── ENCODING / DECODING ──────────────────────────────────────────

    def encode(self, text: str,
               vocab_id: str,
               mode: str = "word") -> List[int]:
        v = self._vocabs.get(vocab_id)
        if not v: raise KeyError(f"Vocab {vocab_id} not found")
        unk = v.special_tokens.get(self.config.unk_token, 0)
        tokens = self.tokenize(text, mode)
        ids: List[int] = []
        if self.config.add_bos:
            ids.append(v.special_tokens.get(self.config.bos_token, 1))
        for tok in tokens:
            ids.append(v.token_to_id.get(tok, unk))
        if self.config.add_eos:
            ids.append(v.special_tokens.get(self.config.eos_token, 2))
        # Truncation
        if self.config.truncation and self.config.max_length:
            ids = ids[:self.config.max_length]
        # Padding
        if self.config.padding and self.config.max_length:
            pad = v.special_tokens.get(self.config.pad_token, 0)
            while len(ids) < self.config.max_length:
                ids.append(pad)
        return ids

    def decode(self, ids: List[int],
               vocab_id: str,
               skip_special: bool = True) -> str:
        v = self._vocabs.get(vocab_id)
        if not v: raise KeyError(f"Vocab {vocab_id} not found")
        specials = set(v.special_tokens.values())
        tokens = []
        for idx in ids:
            if skip_special and idx in specials:
                continue
            tok = v.id_to_token.get(idx, self.config.unk_token)
            tokens.append(tok)
        return " ".join(tokens)

    def encode_batch(self, texts: List[str],
                     vocab_id: str,
                     mode: str = "word") -> List[List[int]]:
        return [self.encode(t, vocab_id, mode) for t in texts]

    def decode_batch(self, batch: List[List[int]],
                     vocab_id: str) -> List[str]:
        return [self.decode(ids, vocab_id) for ids in batch]

    # ── BPE TRAINING ─────────────────────────────────────────────────

    def train_bpe(self, corpus: List[str],
                   num_merges: int = 50,
                   bpe_id: Optional[str] = None) -> List[Tuple[str, str]]:
        bid = bpe_id or str(uuid.uuid4())[:8]
        # Build initial vocabulary from character pairs
        word_freqs: Counter = Counter()
        for text in corpus:
            for word in text.lower().split():
                word_freqs[" ".join(list(word) + ["</w>"]) ] += 1

        merges: List[Tuple[str, str]] = []
        vocab = dict(word_freqs)

        for _ in range(num_merges):
            pair_freq: Counter = Counter()
            for word, freq in vocab.items():
                symbols = word.split()
                for i in range(len(symbols) - 1):
                    pair_freq[(symbols[i], symbols[i+1])] += freq
            if not pair_freq: break
            best = max(pair_freq, key=lambda x: pair_freq[x])
            merges.append(best)
            new_vocab: Dict[str, int] = {}
            bigram = re.escape(" ".join(best))
            pattern = re.compile(r"(?<!\S)" + bigram + r"(?!\S)")
            for word, freq in vocab.items():
                new_word = pattern.sub("".join(best), word)
                new_vocab[new_word] = freq
            vocab = new_vocab

        self._bpe_merges[bid] = merges
        return merges

    # ── STATS ────────────────────────────────────────────────────────

    def token_frequencies(self, corpus: List[str],
                           mode: str = "word") -> Dict[str, int]:
        freq: Counter = Counter()
        for text in corpus:
            for tok in self.tokenize(text, mode):
                freq[tok] += 1
        return dict(freq.most_common())

    def _persist_vocab(self, v: Vocabulary):
        self._db.execute(
            "INSERT OR REPLACE INTO tk_vocabs VALUES (?,?,?,?,?)",
            (v.vocab_id, v.name,
             json.dumps(v.token_to_id),
             json.dumps(v.special_tokens),
             v.created_at))
        self._db.commit()

    def stats(self) -> Dict[str, Any]:
        return {
            "vocabs": len(self._vocabs),
            "bpe_models": len(self._bpe_merges),
            "vocab_sizes": {vid: v.size
                            for vid, v in self._vocabs.items()},
        }
