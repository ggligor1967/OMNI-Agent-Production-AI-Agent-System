import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_auth_store_user_filter_rejects_sql_injection_payload():
    from agent.auth import AuthManager, Role

    tmpdir = tempfile.mkdtemp()
    auth = AuthManager(
        secret="test_secret_32chars_xxxxxxxxxx",
        db_path=os.path.join(tmpdir, "auth.db"),
        enforce_auth=True,
    )
    auth.create_api_key("user-a", Role.USER, name="User A")
    auth.create_api_key("user-b", Role.USER, name="User B")

    rows = auth.store.list_keys(user_id="' OR 1=1--")

    assert rows == []
    assert len(auth.store.list_keys()) == 2


def test_memory_search_filters_remain_parameterized_with_injection_payloads():
    from agent.memory import MemoryDB

    tmpdir = tempfile.mkdtemp()
    memory = MemoryDB(os.path.join(tmpdir, "memory.db"))
    memory.save_memory("alpha", "public note", category="general")
    memory.save_memory("beta", "admin note", category="admin")

    rows = memory.search_memories("' OR 1=1--", category="general' OR 1=1--")

    assert rows == []
    assert len(memory.search_memories("note")) == 2


def test_rag_keyword_search_parameterizes_doc_filter_and_limit():
    from agent.rag import Chunk, Document, VectorStore

    tmpdir = tempfile.mkdtemp()
    store = VectorStore(os.path.join(tmpdir, "rag.db"))
    store.save_document(Document(
        id="doc-1",
        title="Doc 1",
        source="manual",
        doc_type="txt",
        total_chunks=1,
    ))
    store.save_document(Document(
        id="doc-2",
        title="Doc 2",
        source="manual",
        doc_type="txt",
        total_chunks=1,
    ))
    store.save_chunks([
        Chunk(id="chunk-1", doc_id="doc-1", text="alpha bravo", index=0),
        Chunk(id="chunk-2", doc_id="doc-2", text="alpha charlie", index=0),
    ])

    normal_rows = store.keyword_search("alpha", top_k=5, doc_id="doc-1")
    injected_rows = store.keyword_search("alpha", top_k=5, doc_id="' OR 1=1--")

    assert len(normal_rows) == 1
    assert normal_rows[0].chunk.doc_id == "doc-1"
    assert injected_rows == []

    with pytest.raises(ValueError, match="top_k must be an integer"):
        store.keyword_search("alpha", top_k="1 OR 1=1")
