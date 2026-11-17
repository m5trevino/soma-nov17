from __future__ import annotations
import tempfile
from pathlib import Path

from store_reference import Soma


class DefenseRejection(Exception):
    pass


class DefenseDendrite:
    """Gate in front of Soma: validates nodes and edges before they are stored."""

    REQUIRED_NODE_KEYS = ("id",)

    def __init__(self, soma: Soma, allowed_types: set[str] | None = None):
        self.soma = soma
        self.allowed_types = allowed_types
        self.rejections: list[dict] = []

    def _reject(self, kind: str, reason: str, payload) -> None:
        self.rejections.append({"kind": kind, "reason": reason, "payload": payload})
        raise DefenseRejection(reason)

    def admit_node(self, node_type: str, properties: dict) -> str:
        if not isinstance(node_type, str) or not node_type.strip():
            self._reject("node", "node_type must be a non-empty string", properties)
        if self.allowed_types is not None and node_type not in self.allowed_types:
            self._reject("node", f"node_type '{node_type}' not allowed", properties)
        if not isinstance(properties, dict):
            self._reject("node", "properties must be a dict", properties)
        for key in self.REQUIRED_NODE_KEYS:
            value = properties.get(key)
            if not isinstance(value, str) or not value.strip():
                self._reject("node", f"missing or empty required key '{key}'", properties)
        if self.soma.get_node(properties["id"]) is not None:
            self._reject("node", f"duplicate node id '{properties['id']}'", properties)
        return self.soma.create_node(node_type, properties)

    def admit_edge(self, source_id: str, relationship: str, target_id: str) -> None:
        payload = {"source": source_id, "relationship": relationship, "target": target_id}
        if not isinstance(relationship, str) or not relationship.strip():
            self._reject("edge", "relationship must be a non-empty string", payload)
        if self.soma.get_node(source_id) is None:
            self._reject("edge", f"unknown source '{source_id}'", payload)
        if self.soma.get_node(target_id) is None:
            self._reject("edge", f"unknown target '{target_id}'", payload)
        self.soma.create_edge(source_id, relationship, target_id)


def _expect_rejection(fn, *args) -> bool:
    try:
        fn(*args)
    except DefenseRejection:
        return True
    return False


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        dendrite = DefenseDendrite(Soma(db_path), allowed_types={"TestNode"})

        node_id = dendrite.admit_node("TestNode", {"id": "test-1", "status": "active"})
        dendrite.admit_node("TestNode", {"id": "test-2", "status": "idle"})
        dendrite.admit_edge("test-1", "LINKS_TO", "test-2")

        checks = [
            node_id == "test-1",
            _expect_rejection(dendrite.admit_node, "TestNode", {"id": "test-1"}),
            _expect_rejection(dendrite.admit_node, "Intruder", {"id": "x-1"}),
            _expect_rejection(dendrite.admit_node, "TestNode", {"status": "no-id"}),
            _expect_rejection(dendrite.admit_edge, "test-1", "LINKS_TO", "ghost"),
            _expect_rejection(dendrite.admit_edge, "test-1", "", "test-2"),
            len(dendrite.rejections) == 5,
        ]

        reloaded = Soma(db_path)
        node = reloaded.get_node("test-1")
        checks += [
            node is not None and node["properties"]["status"] == "active",
            reloaded.get_node("x-1") is None,
            reloaded._edges == [{"source": "test-1", "relationship": "LINKS_TO", "target": "test-2"}],
        ]
        return all(checks)
