from __future__ import annotations
import tempfile
from collections import defaultdict
from pathlib import Path
from store_reference import Soma


def run_macro_synthesis(soma: Soma, source_type: str = "Finding", group_key: str = "theme") -> list[str]:
    """Roll up source nodes into one MacroSynthesis node per group_key value.

    Each synthesis node records its members and is linked to them with
    SYNTHESIZES edges. Returns the created synthesis node ids, sorted by group.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for node in soma._nodes.values():
        if node["type"] == source_type:
            groups[str(node["properties"].get(group_key, "ungrouped"))].append(node)

    synthesis_ids = []
    for group in sorted(groups):
        members = sorted(groups[group], key=lambda n: n["node_id"])
        member_ids = [m["node_id"] for m in members]
        synthesis_id = soma.create_node("MacroSynthesis", {
            "id": f"macro-{group}",
            group_key: group,
            "member_count": len(member_ids),
            "member_ids": member_ids,
            "summary": "; ".join(str(m["properties"].get("text", m["node_id"])) for m in members),
        })
        for member_id in member_ids:
            soma.create_edge(synthesis_id, "SYNTHESIZES", member_id)
        synthesis_ids.append(synthesis_id)
    return synthesis_ids


def run_stage_smoke_test() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        soma = Soma(db_path)
        soma.create_node("Finding", {"id": "f-1", "theme": "auth", "text": "token expiry unchecked"})
        soma.create_node("Finding", {"id": "f-2", "theme": "auth", "text": "weak session ids"})
        soma.create_node("Finding", {"id": "f-3", "theme": "io", "text": "unbounded reads"})

        synthesis_ids = run_macro_synthesis(soma)
        if synthesis_ids != ["macro-auth", "macro-io"]:
            return False

        reloaded = Soma(db_path)
        auth = reloaded.get_node("macro-auth")
        io = reloaded.get_node("macro-io")
        if auth is None or io is None:
            return False
        if auth["properties"]["member_ids"] != ["f-1", "f-2"] or auth["properties"]["member_count"] != 2:
            return False
        if io["properties"]["member_ids"] != ["f-3"]:
            return False

        edges = {(e["source"], e["relationship"], e["target"]) for e in reloaded._edges}
        expected = {
            ("macro-auth", "SYNTHESIZES", "f-1"),
            ("macro-auth", "SYNTHESIZES", "f-2"),
            ("macro-io", "SYNTHESIZES", "f-3"),
        }
        return edges == expected
