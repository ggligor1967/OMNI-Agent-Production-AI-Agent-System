# knowledge_graph.py Quality Ratchet

## Starting Coverage

`43.14%`

## Target

`>= 60%`

## Final Coverage

`99.00%`

## Tests Added

- `tests/test_knowledge_graph_quality_ratchet.py`
- existing guard retained: `tests/test_export_api_contracts.py`

## Operational Behaviors Covered

- `GraphStore` persists nodes and edges, reloads typed rows, reports stats, and deletes nodes/edges correctly
- `KnowledgeGraph` CRUD covers add/get/update/delete paths and rejects edges for missing nodes
- neighbour queries cover `out`, `in`, and `both` directions with relation filters
- traversal and path-finding cover BFS, DFS, weighted and unweighted shortest paths, and unreachable targets
- analytics cover `find_nodes()`, `degree_centrality()`, `connected_components()`, `stats()`, and subgraph extraction
- merge behavior re-routes edges from the removed node to the kept node and persists the resulting graph state
- `/kg/*` routes cover node creation, missing-node edge rejection, query filtering, path lookup, and stats output

## Notes

- real defects fixed: none
- product code changes: none
