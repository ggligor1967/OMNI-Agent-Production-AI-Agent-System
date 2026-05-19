import os
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_graph_store_round_trips_and_deletes_nodes_and_edges(tmp_path):
    from agent.knowledge_graph import Edge, GraphStore, Node

    store = GraphStore(str(tmp_path / "kg.db"))
    alice = Node(
        id="alice",
        label="Alice",
        node_type="PERSON",
        properties={"role": "engineer"},
        tags=["human"],
    )
    acme = Node(id="acme", label="Acme", node_type="ORG")
    edge = Edge(
        id="edge-1",
        from_id="alice",
        to_id="acme",
        relation="WORKS_AT",
        weight=1.5,
        properties={"since": "2024"},
    )

    store.save_node(alice)
    store.save_node(acme)
    store.save_edge(edge)

    nodes, edges = store.load_all()

    assert {node.id for node in nodes} == {"alice", "acme"}
    assert nodes[0].properties == {"role": "engineer"} or nodes[1].properties == {"role": "engineer"}
    assert edges == [
        Edge(
            id="edge-1",
            from_id="alice",
            to_id="acme",
            relation="WORKS_AT",
            weight=1.5,
            properties={"since": "2024"},
            created_at=edges[0].created_at,
        )
    ]
    assert store.stats() == {"nodes": 2, "edges": 1}

    store.delete_edge("edge-1")
    assert store.stats()["edges"] == 0

    store.delete_node("alice")
    assert store.stats() == {"nodes": 1, "edges": 0}


def test_knowledge_graph_crud_traversal_and_analytics_paths(tmp_path):
    from agent.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.db"))
    kg.add_node("alice", "Alice", node_type="person")
    kg.add_node("bob", "Bob", node_type="person")
    kg.add_node("carol", "Carol", node_type="person")
    kg.add_node("acme", "Acme", node_type="company")

    kg.add_edge("alice", "bob", "knows", weight=1.0)
    kg.add_edge("bob", "carol", "knows", weight=0.5)
    kg.add_edge("carol", "acme", "works_at", weight=0.3)
    kg.add_edge("alice", "acme", "works_at", weight=3.0)

    assert kg.add_edge("ghost", "alice", "knows") is None
    assert kg.update_node("alice", age=30, city="Cluj") is True
    assert kg.update_node("ghost", age=1) is False

    assert [node.id for node in kg.neighbours("alice", relation="knows", direction="out")] == ["bob"]
    assert {node.id for node in kg.neighbours("acme", direction="in")} == {"alice", "carol"}
    assert {node.id for node in kg.neighbours("bob", direction="both")} == {"alice", "carol"}
    assert {node.id for node in kg.nodes_by_type("person")} == {"alice", "bob", "carol"}
    assert len(kg.edges_by_relation("works_at")) == 2
    assert [node.id for node in kg.find_nodes(lambda node: node.properties.get("age") == 30)] == ["alice"]

    bfs_ids = [node_id for node_id, _depth in kg.bfs("alice", max_depth=2)]
    assert bfs_ids == ["alice", "bob", "acme", "carol"]
    assert kg.bfs("missing") == []

    assert kg.dfs("alice", max_depth=2) == ["alice", "bob", "carol", "acme"]
    assert kg.dfs("missing") == []

    assert kg.shortest_path("alice", "carol") == ["alice", "bob", "carol"]
    assert kg.shortest_path("alice", "acme", weighted=True) == ["alice", "bob", "carol", "acme"]
    assert kg.shortest_path("alice", "missing") is None

    subgraph = kg.subgraph("bob", hops=1)
    assert set(subgraph._nodes) == {"alice", "bob", "carol"}
    assert set(subgraph._edges) == {edge_id for edge_id, edge in kg._edges.items() if edge.from_id in {"alice", "bob", "carol"} and edge.to_id in {"alice", "bob", "carol"}}

    degree = kg.degree_centrality()
    assert degree["bob"] > 0
    assert degree["acme"] > 0

    components = kg.connected_components()
    assert any(component == {"alice", "bob", "carol", "acme"} for component in components)

    stats = kg.stats()
    assert stats["nodes"] == 4
    assert stats["edges"] == 4
    assert stats["in_memory_nodes"] == 4
    assert stats["in_memory_edges"] == 4
    assert stats["directed"] is True
    assert stats["avg_degree"] > 0


def test_knowledge_graph_merge_delete_and_reload_paths(tmp_path):
    from agent.knowledge_graph import KnowledgeGraph

    db_path = tmp_path / "kg.db"
    kg = KnowledgeGraph(db_path=str(db_path))
    kg.add_node("alice", "Alice", node_type="person")
    kg.add_node("alice2", "Alice Secondary", node_type="person")
    kg.add_node("acme", "Acme", node_type="company")
    kg.add_node("bob", "Bob", node_type="person")
    kg.add_edge("alice2", "acme", "works_at", weight=1.0, properties={"description": "secondary"})
    kg.add_edge("bob", "alice2", "knows", weight=0.7)

    assert kg.merge_nodes("alice", "alice2") is True
    assert kg.get_node("alice2") is None
    assert kg.merge_nodes("alice", "ghost") is False

    rerouted = {(edge.from_id, edge.to_id, edge.relation) for edge in kg._edges.values()}
    assert ("alice", "acme", "works_at") in rerouted
    assert ("bob", "alice", "knows") in rerouted

    removable_edge = next(edge.id for edge in kg._edges.values() if edge.from_id == "bob")
    assert kg.delete_edge(removable_edge) is True
    assert kg.delete_edge("missing-edge") is False
    assert kg.delete_node("bob") is True
    assert kg.delete_node("missing-node") is False

    reloaded = KnowledgeGraph(db_path=str(db_path))
    assert reloaded.get_node("alice") is not None
    assert reloaded.get_node("alice2") is None
    assert reloaded.get_node("bob") is None


@pytest.mark.asyncio
async def test_knowledge_graph_register_routes_exposes_query_path_and_stats(tmp_path):
    from agent.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.db"))
    app = web.Application()
    kg.register_routes(app, prefix="/api")

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        missing_edge_resp = await client.post(
            "/api/kg/edge",
            json={"from": "ghost", "to": "alice", "relation": "KNOWS"},
        )
        missing_edge_body = await missing_edge_resp.json()

        alice_resp = await client.post(
            "/api/kg/node",
            json={
                "id": "alice",
                "label": "Alice",
                "type": "PERSON",
                "properties": {"team": "ops"},
                "tags": ["human"],
            },
        )
        acme_resp = await client.post(
            "/api/kg/node",
            json={"id": "acme", "label": "Acme", "type": "ORG"},
        )
        edge_resp = await client.post(
            "/api/kg/edge",
            json={
                "from": "alice",
                "to": "acme",
                "relation": "WORKS_AT",
                "weight": 1.2,
                "properties": {"since": "2024"},
            },
        )
        query_resp = await client.get("/api/kg/query?type=PERSON")
        path_resp = await client.post(
            "/api/kg/path",
            json={"from": "alice", "to": "acme", "weighted": True},
        )
        stats_resp = await client.get("/api/kg/stats")

        alice_body = await alice_resp.json()
        acme_body = await acme_resp.json()
        edge_body = await edge_resp.json()
        query_body = await query_resp.json()
        path_body = await path_resp.json()
        stats_body = await stats_resp.json()
    finally:
        await client.close()

    assert missing_edge_resp.status == 404
    assert missing_edge_body == {"error": "node not found"}

    assert alice_resp.status == 201
    assert alice_body["id"] == "alice"
    assert alice_body["type"] == "PERSON"
    assert alice_body["properties"] == {"team": "ops"}
    assert alice_body["tags"] == ["human"]

    assert acme_resp.status == 201
    assert acme_body["id"] == "acme"

    assert edge_resp.status == 201
    assert edge_body["from"] == "alice"
    assert edge_body["to"] == "acme"
    assert edge_body["relation"] == "WORKS_AT"
    assert edge_body["weight"] == 1.2
    assert edge_body["properties"] == {"since": "2024"}

    assert query_resp.status == 200
    assert query_body == {
        "nodes": [
            {
                "id": "alice",
                "label": "Alice",
                "type": "PERSON",
                "properties": {"team": "ops"},
                "tags": ["human"],
            }
        ]
    }

    assert path_resp.status == 200
    assert path_body == {"path": ["alice", "acme"]}

    assert stats_resp.status == 200
    assert stats_body["nodes"] == 2
    assert stats_body["edges"] == 1
    assert stats_body["in_memory_nodes"] == 2
    assert stats_body["in_memory_edges"] == 1
