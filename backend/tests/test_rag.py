import numpy as np

from customchat.metadata import SourceMetadata
from customchat.rag import chunk_text, citation_id, cosine_rank, pack_context


def test_chunk_text_adds_overlap_and_citation_ids():
    text = " ".join(f"token{i}" for i in range(30))

    chunks = chunk_text(
        document_id=7,
        source_id=3,
        text=text,
        metadata=SourceMetadata(title="Chunk Test"),
        chunk_tokens=10,
        overlap_tokens=3,
    )

    assert len(chunks) == 4
    assert chunks[0].citation_id == citation_id(7, 0)
    assert chunks[1].text.startswith("token7 token8 token9")
    assert chunks[-1].metadata.title == "Chunk Test"


def test_cosine_rank_orders_by_similarity_descending():
    query = np.array([1.0, 0.0], dtype=np.float32)
    candidates = [
        (1, np.array([0.2, 0.9], dtype=np.float32)),
        (2, np.array([0.9, 0.1], dtype=np.float32)),
        (3, np.array([-1.0, 0.0], dtype=np.float32)),
    ]

    ranked = cosine_rank(query, candidates, top_k=2)

    assert [item.chunk_id for item in ranked] == [2, 1]
    assert ranked[0].score > ranked[1].score


def test_pack_context_keeps_citation_markers():
    chunks = chunk_text(
        document_id=4,
        source_id=2,
        text="Alpha evidence. Beta evidence. Gamma evidence.",
        metadata=SourceMetadata(title="Evidence"),
        chunk_tokens=4,
        overlap_tokens=0,
    )

    packed = pack_context(chunks, max_chars=500)

    assert "[D4-C0]" in packed
    assert "Evidence" in packed
    assert "Alpha evidence" in packed

