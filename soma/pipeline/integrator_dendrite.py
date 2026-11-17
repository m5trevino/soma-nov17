from __future__ import annotations
import tempfile
from pathlib import Path
from store_reference import Soma


class IntegratorDendrite:
    """Integrates incoming signals into a Soma as nodes linked to a hub node."""

    def __init__(self, soma: Soma, hub_id: str = "integrator-hub"):
        self.soma = soma
        self.hub_id = hub_id
        if self.soma.get_node(hub_id) is None:
            self.soma.create_node("IntegratorHub", {"id": hub_id})

    def integrate(self, signal_type: str, properties: dict) -> str:
        if not signal_type:
            raise ValueError("signal_type is required")
        node_id = self.soma.create_node(signal_type, dict(properties))
        self.soma.create_edge(self.hub_id, "INTEGRATES", node_id)
        return node_id

    def integrated_ids(self) -> list[str]:
        return [
            e["target"]
            for e in self.soma._edges
            if e["source"] == self.hub_id and e["relationship"] == "INTEGRATES"
        ]


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        dendrite = IntegratorDendrite(Soma(db_path))
        node_id = dendrite.integrate("TestNode", {"id": "test-1", "status": "active"})
        if node_id != "test-1" or dendrite.integrated_ids() != ["test-1"]:
            return False

        # Reload from disk to confirm the integration persisted.
        reloaded = IntegratorDendrite(Soma(db_path))
        node = reloaded.soma.get_node("test-1")
        return (
            node is not None
            and node["properties"]["status"] == "active"
            and reloaded.integrated_ids() == ["test-1"]
        )
