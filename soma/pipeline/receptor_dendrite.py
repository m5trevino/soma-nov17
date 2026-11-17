from __future__ import annotations
import tempfile
from pathlib import Path
from store_reference import Soma


class ReceptorDendrite:
    """Receives raw signals, distills them, and records them in a Soma store."""

    NODE_TYPE = "Signal"
    RECEPTOR_TYPE = "Receptor"
    RELATIONSHIP = "RECEIVED"

    def __init__(self, soma: Soma, receptor_id: str = "receptor-1"):
        self.soma = soma
        self.receptor_id = receptor_id
        if self.soma.get_node(receptor_id) is None:
            self.soma.create_node(self.RECEPTOR_TYPE, {"id": receptor_id})

    @staticmethod
    def distill(signal: dict) -> dict:
        if not isinstance(signal, dict):
            raise TypeError("signal must be a dict")
        if not signal.get("id"):
            raise ValueError("signal requires a non-empty 'id'")
        return {
            key.strip().lower(): value.strip() if isinstance(value, str) else value
            for key, value in signal.items()
            if isinstance(key, str) and key.strip() and value is not None
        }

    def receive(self, signal: dict) -> str:
        properties = self.distill(signal)
        node_id = self.soma.create_node(self.NODE_TYPE, properties)
        self.soma.create_edge(self.receptor_id, self.RELATIONSHIP, node_id)
        return node_id


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        dendrite = ReceptorDendrite(Soma(db_path))
        node_id = dendrite.receive({"id": "test-1", " Status ": " active ", "noise": None})

        node = dendrite.soma.get_node(node_id)
        if node is None or node["type"] != ReceptorDendrite.NODE_TYPE:
            return False
        if node["properties"] != {"id": "test-1", "status": "active"}:
            return False

        reloaded = Soma(db_path)
        persisted = reloaded.get_node("test-1")
        if persisted is None or persisted["properties"]["status"] != "active":
            return False
        if {"source": "receptor-1", "relationship": "RECEIVED", "target": "test-1"} not in reloaded._edges:
            return False

        try:
            dendrite.receive({"status": "missing-id"})
        except ValueError:
            return True
        return False
