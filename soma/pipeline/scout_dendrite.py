from __future__ import annotations
import tempfile
from pathlib import Path
from store_reference import Soma

NODE_KEYS = ("node_id", "type", "properties")
EDGE_KEYS = ("source", "relationship", "target")


class ScouterDendrite:
    """Read-only scout over a Soma store: neighborhood lookup and invariant checks."""

    def __init__(self, soma: Soma):
        self.soma = soma

    def scout(self, node_id: str) -> dict | None:
        node = self.soma.get_node(node_id)
        if node is None:
            return None
        outgoing = [e for e in self.soma._edges if e.get("source") == node_id]
        incoming = [e for e in self.soma._edges if e.get("target") == node_id]
        return {"node": node, "outgoing": outgoing, "incoming": incoming}

    def check_invariants(self) -> list[str]:
        violations = []
        for key, node in self.soma._nodes.items():
            missing = [k for k in NODE_KEYS if k not in node]
            if missing:
                violations.append(f"node {key!r} missing keys {missing}")
            elif node["node_id"] != key:
                violations.append(f"node key {key!r} != node_id {node['node_id']!r}")
            elif not isinstance(node["properties"], dict):
                violations.append(f"node {key!r} properties is not a dict")
        for i, edge in enumerate(self.soma._edges):
            missing = [k for k in EDGE_KEYS if k not in edge]
            if missing:
                violations.append(f"edge {i} missing keys {missing}")
                continue
            for end in ("source", "target"):
                if edge[end] not in self.soma._nodes:
                    violations.append(f"edge {i} dangling {end} {edge[end]!r}")
        return violations


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        soma = Soma(db_path)
        soma.create_node("TestNode", {"id": "test-1", "status": "active"})
        soma.create_node("TestNode", {"id": "test-2", "status": "idle"})
        soma.create_edge("test-1", "LINKS_TO", "test-2")

        scouter = ScouterDendrite(soma)
        view = scouter.scout("test-1")
        if view is None or view["node"]["properties"]["status"] != "active":
            return False
        if [e["target"] for e in view["outgoing"]] != ["test-2"] or view["incoming"]:
            return False
        if scouter.scout("missing") is not None:
            return False
        if scouter.check_invariants():
            return False

        reloaded = ScouterDendrite(Soma(db_path))
        if reloaded.check_invariants() or reloaded.scout("test-2")["incoming"] != view["outgoing"]:
            return False

        soma.create_edge("test-1", "LINKS_TO", "ghost")
        violations = scouter.check_invariants()
        return violations == ["edge 1 dangling target 'ghost'"]
