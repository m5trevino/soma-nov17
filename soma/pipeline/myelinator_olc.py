from __future__ import annotations
import tempfile
from pathlib import Path
from store_reference import Soma


class MyelinatorDendrite:
    """Receives signals and writes them into a Soma as linked nodes."""

    def __init__(self, soma: Soma):
        self.soma = soma

    def receive(self, signal_type: str, properties: dict, parent_id: str | None = None) -> str:
        if not signal_type:
            raise ValueError("signal_type is required")
        if parent_id is not None and self.soma.get_node(parent_id) is None:
            raise KeyError(f"parent node not found: {parent_id}")
        node_id = self.soma.create_node(signal_type, dict(properties))
        if parent_id is not None:
            self.soma.create_edge(parent_id, "FEEDS", node_id)
        return node_id

    def myelinate(self, node_id: str) -> dict:
        node = self.soma.get_node(node_id)
        if node is None:
            raise KeyError(f"node not found: {node_id}")
        node["properties"]["myelinated"] = True
        self.soma.persist()
        return node

    def downstream(self, node_id: str) -> list[str]:
        return [e["target"] for e in self.soma._edges if e["source"] == node_id]


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        dendrite = MyelinatorDendrite(Soma(db_path))

        root = dendrite.receive("TestNode", {"id": "test-1", "status": "active"})
        child = dendrite.receive("TestNode", {"id": "test-2", "status": "pending"}, parent_id=root)
        dendrite.myelinate(child)

        try:
            dendrite.receive("TestNode", {"id": "orphan"}, parent_id="missing")
            return False
        except KeyError:
            pass

        reloaded = Soma(db_path)
        root_node = reloaded.get_node("test-1")
        child_node = reloaded.get_node("test-2")
        return (
            root_node is not None
            and root_node["properties"]["status"] == "active"
            and child_node is not None
            and child_node["properties"].get("myelinated") is True
            and reloaded.get_node("orphan") is None
            and {"source": "test-1", "relationship": "FEEDS", "target": "test-2"} in reloaded._edges
        )
