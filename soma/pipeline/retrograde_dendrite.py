from __future__ import annotations
import tempfile
from collections import deque
from pathlib import Path

from store_reference import Soma


class RetrogradeDendrite:
    """Walks Soma edges backward (target -> source) from a terminal node."""

    def __init__(self, soma: Soma):
        self.soma = soma

    def incoming(self, node_id: str, relationship: str | None = None) -> list[dict]:
        return [
            e for e in self.soma._edges
            if e["target"] == node_id
            and (relationship is None or e["relationship"] == relationship)
        ]

    def trace(self, node_id: str, relationship: str | None = None) -> list[str]:
        """Return ancestor node ids in breadth-first order, nearest first."""
        if self.soma.get_node(node_id) is None:
            raise KeyError(f"unknown node: {node_id}")
        seen = {node_id}
        order: list[str] = []
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            for edge in self.incoming(current, relationship):
                source = edge["source"]
                if source in seen:
                    continue
                seen.add(source)
                order.append(source)
                queue.append(source)
        return order

    def roots(self, node_id: str, relationship: str | None = None) -> list[str]:
        """Return ancestors that have no incoming edges of their own."""
        return [
            n for n in self.trace(node_id, relationship)
            if not self.incoming(n, relationship)
        ]


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        soma = Soma(Path(tmp) / "soma.json")
        for nid in ("root", "mid-a", "mid-b", "leaf"):
            soma.create_node("TestNode", {"id": nid, "status": "active"})
        soma.create_edge("root", "FEEDS", "mid-a")
        soma.create_edge("root", "FEEDS", "mid-b")
        soma.create_edge("mid-a", "FEEDS", "leaf")
        soma.create_edge("mid-b", "FEEDS", "leaf")
        soma.create_edge("leaf", "FEEDS", "root")  # cycle must not loop forever

        dendrite = RetrogradeDendrite(Soma(Path(tmp) / "soma.json"))
        node = dendrite.soma.get_node("leaf")
        if node is None or node["properties"]["status"] != "active":
            return False
        if dendrite.trace("leaf") != ["mid-a", "mid-b", "root"]:
            return False
        if dendrite.trace("leaf", "OTHER") != []:
            return False

        soma.create_node("TestNode", {"id": "origin", "status": "active"})
        soma.create_edge("origin", "SEEDS", "root")
        dendrite = RetrogradeDendrite(Soma(Path(tmp) / "soma.json"))
        if dendrite.roots("mid-a") != ["origin"]:
            return False
        try:
            dendrite.trace("missing")
            return False
        except KeyError:
            pass
        return True
