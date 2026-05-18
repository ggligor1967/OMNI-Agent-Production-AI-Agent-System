import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_memorydb_get_all_memories_returns_deserialized_export_rows(tmp_path):
    from agent.memory import MemoryDB

    memory = MemoryDB(db_path=str(tmp_path / "memory.db"))
    memory.save_memory("profile", {"theme": "dark"}, category="prefs", importance=9, source="pytest")
    memory.save_memory("note", "plain text", category="notes", importance=3, source="pytest")

    all_memories = memory.get_all_memories()
    prefs_only = memory.get_all_memories(category="prefs")

    assert [item["key"] for item in prefs_only] == ["profile"]
    assert prefs_only[0]["value"] == {"theme": "dark"}
    assert prefs_only[0]["source"] == "pytest"
    assert "created_at" in prefs_only[0]
    assert {item["key"] for item in all_memories} == {"profile", "note"}


def test_knowledge_graph_export_matches_exporter_contract(tmp_path):
    from agent.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.db"))
    kg.add_node("alice", "Alice", node_type="PERSON", properties={"description": "Engineer"}, tags=["human"])
    kg.add_node("acme", "Acme Corp", node_type="ORG")
    kg.add_edge("alice", "acme", "WORKS_AT", properties={"description": "employment"})

    exported = kg.export()

    assert exported["entities"] == [
        {
            "id": "acme",
            "name": "Acme Corp",
            "type": "ORG",
            "description": "",
            "properties": {},
            "tags": [],
            "created_at": exported["entities"][0]["created_at"],
        },
        {
            "id": "alice",
            "name": "Alice",
            "type": "PERSON",
            "description": "Engineer",
            "properties": {"description": "Engineer"},
            "tags": ["human"],
            "created_at": exported["entities"][1]["created_at"],
        },
    ]
    assert exported["relationships"][0]["source"] == "alice"
    assert exported["relationships"][0]["target"] == "acme"
    assert exported["relationships"][0]["label"] == "WORKS_AT"
    assert exported["relationships"][0]["description"] == "employment"
    assert exported["stats"]["nodes"] == 2
    assert exported["stats"]["edges"] == 1


def test_exporter_uses_real_memory_and_kg_contracts(tmp_path):
    from agent.export import ExportFormat, Exporter
    from agent.knowledge_graph import KnowledgeGraph
    from agent.memory import MemoryDB

    memory = MemoryDB(db_path=str(tmp_path / "memory.db"))
    memory.save_memory("project", {"name": "OMNI"}, category="meta", importance=7)

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.db"))
    kg.add_node("omni", "OMNI Agent", node_type="SYSTEM")

    exporter = Exporter(SimpleNamespace(memory=memory, knowledge_graph=kg))

    memories_payload = json.loads(exporter.export_memories(ExportFormat.JSON))
    kg_payload = json.loads(exporter.export_kg(ExportFormat.JSON))

    assert memories_payload["count"] == 1
    assert memories_payload["memories"][0]["value"] == {"name": "OMNI"}
    assert kg_payload["entities"][0]["name"] == "OMNI Agent"
    assert kg_payload["relationships"] == []
