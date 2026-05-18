import hashlib
import gc
import os
import platform
import shutil
import statistics
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.rag import Chunk, Document, VectorStore


SNAPSHOT_DIR = PROJECT_ROOT / "snapshot-phase-2"
RESULTS_PATH = SNAPSHOT_DIR / "rag_benchmark_results.md"
RUN_SIZES = [1_000, 10_000, 100_000]
QUERY_COUNT = 15
BATCH_SIZE = 1_000
EMBED_DIM = 16
MAX_10K_MS_FOR_100K = float(os.getenv("OMNI_RAG_BENCHMARK_100K_MAX_MS", "750"))
MAX_10K_PEAK_MB_FOR_100K = float(os.getenv("OMNI_RAG_BENCHMARK_100K_MAX_PEAK_MB", "256"))
FORCE_100K = os.getenv("OMNI_RAG_BENCHMARK_FORCE_100K", "false").lower() == "true"


@dataclass
class BenchmarkResult:
    size: int
    status: str
    query_count: int
    build_seconds: Optional[float]
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    max_ms: Optional[float]
    db_size_mb: Optional[float]
    peak_traced_memory_mb: Optional[float]
    note: str


def deterministic_embedding(seed: int, dim: int = EMBED_DIM) -> List[float]:
    values: List[float] = []
    counter = 0
    while len(values) < dim:
        digest = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).digest()
        for index in range(0, len(digest), 4):
            chunk = digest[index:index + 4]
            if len(chunk) < 4:
                continue
            raw = int.from_bytes(chunk, "big") / 0xFFFFFFFF
            values.append((raw * 2.0) - 1.0)
            if len(values) == dim:
                break
        counter += 1
    return values


def percentile(values: List[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def build_store(size: int, db_path: Path) -> VectorStore:
    store = VectorStore(db_path=str(db_path))
    doc_id = f"synthetic-{size}"
    store.save_document(
        Document(
            id=doc_id,
            title=f"Synthetic corpus ({size} chunks)",
            source="benchmark",
            doc_type="synthetic",
            total_chunks=size,
            metadata={"size": size},
        )
    )

    for start in range(0, size, BATCH_SIZE):
        batch = []
        for chunk_index in range(start, min(start + BATCH_SIZE, size)):
            batch.append(
                Chunk(
                    id=f"{doc_id}-{chunk_index}",
                    doc_id=doc_id,
                    text=f"synthetic benchmark chunk {chunk_index}",
                    index=chunk_index,
                    metadata={"size": size},
                    embedding=deterministic_embedding(chunk_index),
                )
            )
        store.save_chunks(batch)
    return store


def benchmark_size(size: int) -> BenchmarkResult:
    print(f"\n== Benchmarking {size} chunks ==")
    bench_dir = SNAPSHOT_DIR / f"rag-bench-{size}"
    if bench_dir.exists():
        shutil.rmtree(bench_dir, ignore_errors=True)
    bench_dir.mkdir(parents=True, exist_ok=True)
    db_path = bench_dir / "rag.db"

    tracemalloc.start()
    build_start = time.perf_counter()
    store = build_store(size, db_path)
    build_seconds = time.perf_counter() - build_start

    query_indices = [((size * step) // QUERY_COUNT) % size for step in range(QUERY_COUNT)]
    timings_ms: List[float] = []
    for query_index in query_indices:
        query_embedding = deterministic_embedding(query_index)
        query_start = time.perf_counter()
        results = store.similarity_search(query_embedding, top_k=5)
        elapsed_ms = (time.perf_counter() - query_start) * 1000.0
        timings_ms.append(elapsed_ms)
        if not results:
            raise RuntimeError(f"No retrieval result returned for chunk index {query_index}")

    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    db_size_mb = db_path.stat().st_size / (1024 * 1024)

    store = None
    gc.collect()
    shutil.rmtree(bench_dir, ignore_errors=True)

    result = BenchmarkResult(
        size=size,
        status="MEASURED",
        query_count=QUERY_COUNT,
        build_seconds=round(build_seconds, 3),
        p50_ms=round(percentile(timings_ms, 0.50), 3),
        p95_ms=round(percentile(timings_ms, 0.95), 3),
        max_ms=round(max(timings_ms), 3),
        db_size_mb=round(db_size_mb, 3),
        peak_traced_memory_mb=round(peak_bytes / (1024 * 1024), 3),
        note="deterministic synthetic embeddings; brute-force cosine similarity",
    )
    print(asdict(result))
    return result


def should_skip_100k(ten_k_result: BenchmarkResult) -> Optional[str]:
    if FORCE_100K:
        return None
    if ten_k_result.max_ms is None or ten_k_result.peak_traced_memory_mb is None:
        return "SKIPPED_RESOURCE_LIMIT: insufficient 10k baseline data to assess 100k safely"
    if ten_k_result.max_ms > MAX_10K_MS_FOR_100K:
        return (
            "SKIPPED_RESOURCE_LIMIT: 10k max retrieval latency "
            f"{ten_k_result.max_ms} ms exceeded local threshold {MAX_10K_MS_FOR_100K} ms"
        )
    if ten_k_result.peak_traced_memory_mb > MAX_10K_PEAK_MB_FOR_100K:
        return (
            "SKIPPED_RESOURCE_LIMIT: 10k peak traced memory "
            f"{ten_k_result.peak_traced_memory_mb} MiB exceeded local threshold "
            f"{MAX_10K_PEAK_MB_FOR_100K} MiB"
        )
    return None


def render_markdown(results: List[BenchmarkResult]) -> str:
    lines = [
        "# RAG Retrieval Benchmark Results",
        "",
        f"- Platform: `{platform.platform()}`",
        f"- Python: `{platform.python_version()}`",
        f"- Query count per measured corpus: `{QUERY_COUNT}`",
        f"- Embedding dimension: `{EMBED_DIM}`",
        f"- 100k force override: `{FORCE_100K}`",
        f"- 100k gating thresholds: `{MAX_10K_MS_FOR_100K} ms` max latency, `{MAX_10K_PEAK_MB_FOR_100K} MiB` peak traced memory",
        "",
        "| Corpus size | Status | Build seconds | p50 ms | p95 ms | max ms | DB size MB | Peak traced memory MB | Notes |",
        "| ----------- | ------ | -------------: | -----: | -----: | -----: | ---------: | ---------------------: | ----- |",
    ]

    for result in results:
        lines.append(
            "| {size} | {status} | {build_seconds} | {p50_ms} | {p95_ms} | {max_ms} | {db_size_mb} | {peak_traced_memory_mb} | {note} |".format(
                size=result.size,
                status=result.status,
                build_seconds=result.build_seconds if result.build_seconds is not None else "-",
                p50_ms=result.p50_ms if result.p50_ms is not None else "-",
                p95_ms=result.p95_ms if result.p95_ms is not None else "-",
                max_ms=result.max_ms if result.max_ms is not None else "-",
                db_size_mb=result.db_size_mb if result.db_size_mb is not None else "-",
                peak_traced_memory_mb=(
                    result.peak_traced_memory_mb
                    if result.peak_traced_memory_mb is not None else "-"
                ),
                note=result.note,
            )
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `MEASURED` means the corpus size was built and queried locally during this run.",
            "- `SKIPPED_RESOURCE_LIMIT` means the script intentionally avoided a 100k run because the 10k baseline exceeded local guardrails or was forced off by the environment.",
            "- Retrieval latency measures the existing brute-force `VectorStore.similarity_search()` path in `agent/rag.py`, not an external vector database.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    results: List[BenchmarkResult] = []

    ten_k_result: Optional[BenchmarkResult] = None
    for size in RUN_SIZES:
        if size == 100_000 and ten_k_result is not None:
            skip_reason = should_skip_100k(ten_k_result)
            if skip_reason:
                skipped = BenchmarkResult(
                    size=size,
                    status="SKIPPED_RESOURCE_LIMIT",
                    query_count=0,
                    build_seconds=None,
                    p50_ms=None,
                    p95_ms=None,
                    max_ms=None,
                    db_size_mb=None,
                    peak_traced_memory_mb=None,
                    note=skip_reason,
                )
                print(f"\n== Benchmarking {size} chunks ==")
                print(asdict(skipped))
                results.append(skipped)
                continue

        result = benchmark_size(size)
        results.append(result)
        if size == 10_000:
            ten_k_result = result

    RESULTS_PATH.write_text(render_markdown(results), encoding="utf-8")
    print(f"\nWrote benchmark summary to {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
