from __future__ import annotations
import tempfile
from pathlib import Path
from store_reference import Soma


class AxonHillockDendrite:
    """Integrates incoming dendritic signals and fires once the threshold is crossed.

    Each received signal is recorded as a Signal node linked to the hillock node.
    When the integrated potential reaches the threshold, an ActionPotential node
    is emitted, linked to the hillock, and the potential resets.
    """

    def __init__(self, soma: Soma, hillock_id: str = "axon-hillock", threshold: float = 1.0):
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        self.soma = soma
        self.hillock_id = hillock_id
        self.threshold = threshold
        self.potential = 0.0
        self.signal_count = 0
        self.spike_count = 0
        if self.soma.get_node(hillock_id) is None:
            self.soma.create_node("AxonHillock", {"id": hillock_id, "threshold": threshold})

    def receive(self, source: str, weight: float) -> str | None:
        """Integrate one signal. Returns the ActionPotential node id if it fired, else None."""
        self.signal_count += 1
        signal_id = f"{self.hillock_id}-signal-{self.signal_count}"
        self.soma.create_node("Signal", {"id": signal_id, "source": source, "weight": weight})
        self.soma.create_edge(signal_id, "INPUT_TO", self.hillock_id)
        self.potential += weight
        if self.potential < self.threshold:
            return None
        return self._fire()

    def _fire(self) -> str:
        self.spike_count += 1
        spike_id = f"{self.hillock_id}-spike-{self.spike_count}"
        self.soma.create_node(
            "ActionPotential",
            {"id": spike_id, "potential": self.potential, "signals_integrated": self.signal_count},
        )
        self.soma.create_edge(self.hillock_id, "FIRED", spike_id)
        self.potential = 0.0
        return spike_id


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        hillock = AxonHillockDendrite(Soma(db_path), threshold=1.0)

        if hillock.receive("dendrite-a", 0.4) is not None:
            return False
        if hillock.receive("dendrite-b", 0.3) is not None:
            return False
        spike_id = hillock.receive("dendrite-c", 0.5)
        if spike_id is None or hillock.potential != 0.0:
            return False

        reloaded = Soma(db_path)
        spike = reloaded.get_node(spike_id)
        hillock_node = reloaded.get_node(hillock.hillock_id)
        if spike is None or hillock_node is None:
            return False
        if spike["type"] != "ActionPotential" or spike["properties"]["signals_integrated"] != 3:
            return False
        if abs(spike["properties"]["potential"] - 1.2) > 1e-9:
            return False

        edges = reloaded._edges
        inputs = [e for e in edges if e["relationship"] == "INPUT_TO" and e["target"] == hillock.hillock_id]
        fired = [e for e in edges if e["relationship"] == "FIRED" and e["target"] == spike_id]
        return len(inputs) == 3 and len(fired) == 1
