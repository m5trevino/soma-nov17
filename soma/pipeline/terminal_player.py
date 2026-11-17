from __future__ import annotations
import tempfile
from pathlib import Path

from store_reference import Soma


class TerminalPlayer:
    NODE_TYPE = "TerminalPlayer"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.soma = Soma(self.db_path)

    def register(self, player_id: str, name: str, status: str = "active") -> str:
        return self.soma.create_node(
            self.NODE_TYPE, {"id": player_id, "name": name, "status": status}
        )

    def get(self, player_id: str) -> dict | None:
        node = self.soma.get_node(player_id)
        if node is None or node["type"] != self.NODE_TYPE:
            return None
        return node

    def set_status(self, player_id: str, status: str) -> bool:
        node = self.get(player_id)
        if node is None:
            return False
        node["properties"]["status"] = status
        self.soma.persist()
        return True

    def link(self, source_id: str, relationship: str, target_id: str) -> bool:
        if self.get(source_id) is None or self.get(target_id) is None:
            return False
        self.soma.create_edge(source_id, relationship, target_id)
        return True

    def edges_from(self, player_id: str) -> list[dict]:
        return [e for e in self.soma._edges if e["source"] == player_id]


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"

        player = TerminalPlayer(db_path)
        if player.register("player-1", "alpha") != "player-1":
            return False
        if player.register("player-2", "beta") != "player-2":
            return False

        node = player.get("player-1")
        if node is None or node["properties"]["status"] != "active":
            return False

        if not player.set_status("player-1", "idle"):
            return False
        if player.set_status("missing", "idle"):
            return False

        if not player.link("player-1", "FOLLOWS", "player-2"):
            return False
        if player.link("player-1", "FOLLOWS", "missing"):
            return False

        reloaded = TerminalPlayer(db_path)
        node = reloaded.get("player-1")
        if node is None or node["properties"]["status"] != "idle":
            return False
        if reloaded.get("player-2") is None:
            return False
        edges = reloaded.edges_from("player-1")
        return edges == [
            {"source": "player-1", "relationship": "FOLLOWS", "target": "player-2"}
        ]
