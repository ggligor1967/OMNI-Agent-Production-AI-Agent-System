# RAG Retrieval Benchmark Results

- Platform: `Windows-11-10.0.26200-SP0`
- Python: `3.13.3`
- Query count per measured corpus: `15`
- Embedding dimension: `16`
- 100k force override: `False`
- 100k gating thresholds: `750.0 ms` max latency, `256.0 MiB` peak traced memory

| Corpus size | Status | Build seconds | p50 ms | p95 ms | max ms | DB size MB | Peak traced memory MB | Notes |
| ----------- | ------ | -------------: | -----: | -----: | -----: | ---------: | ---------------------: | ----- |
| 1000 | MEASURED | 0.212 | 91.591 | 104.855 | 107.585 | 0.512 | 1.514 | deterministic synthetic embeddings; brute-force cosine similarity |
| 10000 | MEASURED | 2.091 | 836.574 | 1027.294 | 1133.687 | 4.922 | 13.2 | deterministic synthetic embeddings; brute-force cosine similarity |
| 100000 | SKIPPED_RESOURCE_LIMIT | - | - | - | - | - | - | SKIPPED_RESOURCE_LIMIT: 10k max retrieval latency 1133.687 ms exceeded local threshold 750.0 ms |

## Interpretation

- `MEASURED` means the corpus size was built and queried locally during this run.
- `SKIPPED_RESOURCE_LIMIT` means the script intentionally avoided a 100k run because the 10k baseline exceeded local guardrails or was forced off by the environment.
- Retrieval latency measures the existing brute-force `VectorStore.similarity_search()` path in `agent/rag.py`, not an external vector database.
