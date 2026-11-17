"""A stage is given the chat set from the Soma, once.

The chat set is written into the Soma as Message nodes when the session starts.
The distiller used to be sent it from the in-memory copy loaded from the file,
twice: as JSON inside a META block, and again as a plain-text transcript. Now it
is read back out of the Soma, checked against the chat set's hash, and sent once.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import machinetalk.engine as engine_mod
from machinetalk.fleet import assemble, payload_envelope
from machinetalk.hashes import chatset_hash_for
from machinetalk.soma_bridge import SomaBridge
from store import Soma

RAW = [
    {"role": "USER", "content": "UNIQUE-USER-LINE-7d41 build the thing\nsecond line"},
    {"role": "AGENT", "content": "UNIQUE-AGENT-LINE-9a02 here is how"},
    {"role": "USER", "content": "ok \"quoted\" and unicode: café"},
]


def _written(raw=RAW):
    soma = Soma(":memory:")
    bridge = SomaBridge(soma)
    cs = SimpleNamespace(chatset_id="S1:1", session_id="S1", set_number=1,
                         chars=sum(len(m["content"]) for m in raw),
                         raw_chat_set=[dict(m) for m in raw], chatset_hash="",
                         provider="p", models=[])
    bridge.seed_session("S1", [cs])             # the session is seeded, hash assigned
    cs.chatset_hash = soma.get_node("S1:1").properties["chatset_hash"]
    engine = SimpleNamespace(_bridge=bridge, _soma=soma)
    return soma, bridge, cs, engine


def _read(engine, cs):
    return engine_mod.Engine._chat_set_from_soma(engine, cs)


def test_bridge_round_trip_reproduces_the_chat_set_and_its_hash():
    _soma, bridge, cs, _e = _written()
    got = bridge.read_chat_set(cs.chatset_id)
    assert got == RAW
    assert chatset_hash_for(got) == cs.chatset_hash


def test_stage_input_comes_from_the_soma_not_the_in_memory_copy():
    _soma, _b, cs, engine = _written()
    cs.raw_chat_set[0]["content"] = "TAMPERED in memory after the Soma was written"
    assert _read(engine, cs) == RAW            # the Soma's text, not the mutated copy


def test_soma_copy_that_does_not_match_the_hash_fails_loudly():
    soma, _b, cs, engine = _written()
    node = soma.get_node("msg-S1:1-1")
    soma.delete_node("msg-S1:1-1")
    soma.create_node("Message", {**node.properties, "content": "changed after it was written"})
    with pytest.raises(RuntimeError, match="does not match its hash"):
        _read(engine, cs)


def test_chat_set_that_was_never_seeded_fails_loudly():
    soma = Soma(":memory:")
    engine = SimpleNamespace(_bridge=SomaBridge(soma), _soma=soma)
    cs = SimpleNamespace(chatset_id="S9:1", session_id="S9", raw_chat_set=RAW,
                         chatset_hash=chatset_hash_for(RAW))
    with pytest.raises(RuntimeError, match="not seeded"):
        _read(engine, cs)


def _payload(raw):
    return {"session_id": "S1", "chatset_id": "S1:1", "timestamp_utc": "t",
            "chatset_hash": chatset_hash_for(raw), "raw_chat_set": raw}


def test_distiller_message_holds_the_chat_set_exactly_once():
    system, user = assemble("distiller", "SYSTEM", chunk="", payload=_payload(RAW))
    assert system == "SYSTEM"
    assert user == payload_envelope(_payload(RAW))      # the canonical JSON, nothing else
    assert user.count("UNIQUE-USER-LINE-7d41") == 1
    assert user.count("UNIQUE-AGENT-LINE-9a02") == 1
    assert "[RAW_CHAT_TRANSCRIPT]" not in user
    assert "[CHAT_SET_META]" not in user


def test_legacy_slot_form_with_an_explicit_chunk_is_unchanged():
    _system, user = assemble("distiller", "SYSTEM", chunk="the transcript", payload=_payload(RAW))
    assert user.startswith("[CHAT_SET_META]\n")
    assert "[RAW_CHAT_TRANSCRIPT]\nthe transcript" in user


@pytest.mark.parametrize("stage,expected", [("distiller", True), ("planbeaver", True),
                                            ("outrobeaver", True), ("introbeaver", False)])
def test_beaver_payload_gets_the_chat_set_from_the_soma(stage, expected):
    sentinel = [{"role": "USER", "content": "from the soma"}]
    cs = SimpleNamespace(session_id="s", chatset_id="s:1", chatset_hash="h",
                         raw_chat_set=[{"role": "USER", "content": "from the file"}])
    fake = SimpleNamespace(_upstream_output=lambda _cs, _stage: {"k": "v"},
                           _chat_set_from_soma=lambda _cs: sentinel)
    payload = engine_mod.Engine._beaver_payload(fake, cs, stage=stage)
    if expected:
        assert payload["raw_chat_set"] is sentinel
    else:
        assert "raw_chat_set" not in payload
