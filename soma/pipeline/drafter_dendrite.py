from __future__ import annotations
import tempfile
from pathlib import Path
from store_reference import Soma


class DrafterDendrite:
    """Drafter stage: turns an input node into a Draft node linked back to its source."""

    NODE_TYPE = "Draft"
    RELATIONSHIP = "DRAFTED_FROM"

    def __init__(self, soma: Soma):
        self.soma = soma

    def draft(self, source_id: str, content: str) -> str:
        source = self.soma.get_node(source_id)
        if source is None:
            raise KeyError(f"source node not found: {source_id}")
        if not content or not content.strip():
            raise ValueError("draft content must be non-empty")
        draft_id = self.soma.create_node(self.NODE_TYPE, {
            "id": f"draft-{source_id}",
            "status": "drafted",
            "source_id": source_id,
            "content": content,
        })
        self.soma.create_edge(draft_id, self.RELATIONSHIP, source_id)
        return draft_id


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        soma = Soma(db_path)
        soma.create_node("Input", {"id": "input-1", "status": "active"})

        drafter = DrafterDendrite(soma)
        draft_id = drafter.draft("input-1", "first draft")

        # Reload from disk to confirm the draft and its edge were persisted.
        reloaded = Soma(db_path)
        node = reloaded.get_node(draft_id)
        edge = {"source": draft_id, "relationship": DrafterDendrite.RELATIONSHIP, "target": "input-1"}
        if node is None or node["type"] != DrafterDendrite.NODE_TYPE:
            return False
        if node["properties"]["status"] != "drafted" or node["properties"]["content"] != "first draft":
            return False
        if edge not in reloaded._edges:
            return False

        try:
            drafter.draft("missing", "x")
            return False
        except KeyError:
            pass
        try:
            drafter.draft("input-1", "   ")
            return False
        except ValueError:
            pass
        return True
