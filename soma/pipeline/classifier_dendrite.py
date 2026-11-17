from __future__ import annotations
import re
import tempfile
from pathlib import Path

from store_reference import Soma

DEFAULT_RULES: dict[str, tuple[str, ...]] = {
    "code": ("def ", "class ", "import ", "return ", "function ", "{", "}", ";"),
    "error": ("error", "exception", "traceback", "failed", "fatal"),
    "question": ("?", "how ", "what ", "why ", "when ", "where "),
    "command": ("run ", "execute ", "install ", "deploy ", "build "),
}
FALLBACK_LABEL = "unclassified"


class ClassifierDendrite:
    """Classifies text payloads by keyword rules and records results in a Soma graph."""

    def __init__(self, soma: Soma, rules: dict[str, tuple[str, ...]] | None = None):
        self.soma = soma
        self.rules = {label: tuple(k.lower() for k in kws) for label, kws in (rules or DEFAULT_RULES).items()}

    def score(self, text: str) -> dict[str, int]:
        lowered = text.lower()
        return {label: sum(lowered.count(k) for k in kws) for label, kws in self.rules.items()}

    def classify(self, text: str) -> tuple[str, float]:
        if not isinstance(text, str):
            raise TypeError("ClassifierDendrite.classify expects str input")
        scores = self.score(text)
        total = sum(scores.values())
        if total == 0:
            return FALLBACK_LABEL, 0.0
        label = max(scores, key=lambda k: (scores[k], -list(self.rules).index(k)))
        return label, round(scores[label] / total, 4)

    def process(self, input_id: str, text: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", input_id or ""):
            raise ValueError(f"invalid input_id: {input_id!r}")
        label, confidence = self.classify(text)
        self.soma.create_node("Input", {"id": input_id, "text": text})
        class_id = f"class:{label}"
        if self.soma.get_node(class_id) is None:
            self.soma.create_node("Classification", {"id": class_id, "label": label})
        result_id = f"result:{input_id}"
        self.soma.create_node(
            "ClassificationResult",
            {"id": result_id, "input_id": input_id, "label": label, "confidence": confidence, "status": "classified"},
        )
        self.soma.create_edge(input_id, "CLASSIFIED_AS", class_id)
        self.soma.create_edge(result_id, "DESCRIBES", input_id)
        return {"input_id": input_id, "label": label, "confidence": confidence, "result_id": result_id}


def run_stage_smoke_test() -> bool:
    samples = {
        "in-code": "def foo():\n    return 1",
        "in-error": "Traceback: fatal error, exception raised",
        "in-question": "How does this work?",
        "in-empty": "",
    }
    expected = {"in-code": "code", "in-error": "error", "in-question": "question", "in-empty": FALLBACK_LABEL}

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "soma.json"
        dendrite = ClassifierDendrite(Soma(db_path))
        for input_id, text in samples.items():
            out = dendrite.process(input_id, text)
            if out["label"] != expected[input_id]:
                return False

        reloaded = Soma(db_path)
        for input_id, label in expected.items():
            result = reloaded.get_node(f"result:{input_id}")
            if result is None or result["properties"]["label"] != label:
                return False
            if reloaded.get_node(f"class:{label}") is None:
                return False
        edges = {(e["source"], e["relationship"], e["target"]) for e in reloaded._edges}
        if not all((i, "CLASSIFIED_AS", f"class:{l}") in edges for i, l in expected.items()):
            return False

        try:
            dendrite.process("bad id!", "x")
            return False
        except ValueError:
            pass
    return True
