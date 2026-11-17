"""TurnKey client — the only place that talks to models.

Dendrites and Soma call this with a stage name and messages.
They do not pick a model. They do not know the engine URL.
TurnKey SDK does rotation, retry, gateway, and the HTTP call.
"""
from __future__ import annotations

import http.client
import inspect
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Stage 1 extracts. There is exactly one right answer to "which sentence is
# the directive", so sampling a distribution over answers is wrong: the same
# prompt against the same model returned 0, 7 and 0 verified entries on three
# consecutive calls. Later stages compose plans and journals, where some
# latitude is legitimate.
_EXTRACTIVE_STAGES = frozenset({
    "distiller", "introbeaver", "planbeaver", "outrobeaver",
    "scout", "oc", "olc",
})
DEFAULT_EXTRACTIVE_TEMPERATURE = 0.0


# ── live status helpers ─────────────────────────────────────────────────
# These feed the ``emit`` callback of ``EngineClient.call_stage``. Each event
# is printed at the moment the step happens (not batched after the call), and
# every value is read from that step: instruction size from the database row,
# input size from the assembled messages, tokens and latency from the reply,
# the gate verdict from validate(), anchors from provenance. Nothing is
# estimated. Same idea as the distiller's step lines in progress.py, which
# show real work live; see the comment above ``_DISTILLER_STEPS`` there.

def _tok(n: Any) -> str:
    """A token count, or an em dash when the provider reported none."""
    return f"{n:,}" if isinstance(n, int) and n else "—"


def _payload_desc(payload: dict | None, chunk: str = "") -> str:
    """What the stage was handed, by name and size."""
    if not payload:
        return f"chunk ({len(chunk or ''):,} chars)"
    skip = {"session_id", "chatset_id", "timestamp_utc", "chatset_hash"}
    parts = []
    for key, val in payload.items():
        if key in skip:
            continue
        if isinstance(val, list):
            parts.append(f"{key} ({len(val)} items)")
        elif isinstance(val, dict):
            parts.append(f"{key} ({len(json.dumps(val, ensure_ascii=False)):,} chars)")
        else:
            parts.append(str(key))
    return ", ".join(parts) or "chatset hash only"


def _say_gate(say: Any, gate: Any, is_done: Any) -> None:
    """Report the gate's verdict, and the model's own complaint if it made one."""
    if gate.skipped and not gate.errors:
        word = f"SKIP · {gate.skip_reason or gate.verdict or 'skipped'}"
    elif is_done(gate):
        word = "PASS" + (f" · verdict {gate.verdict}" if gate.verdict else "")
    else:
        word = "FAIL · " + ("; ".join(gate.errors[:2]) or gate.verdict or "unknown")
    say("gate", word)
    parsed = gate.parsed
    if isinstance(parsed, dict) and parsed.get("error"):
        missing = parsed.get("missing_fields")
        say("model said",
            f"{parsed['error']}"
            + (f" · missing {missing}" if missing else "")
            + (" · halt" if parsed.get("halt") else ""))


_STAGE_MAP = {
    "receptor": "distiller",
    "distiller": "distiller",
    "classifier": "introbeaver",
    "introbeaver": "introbeaver",
    "integrator": "planbeaver",
    "planbeaver": "planbeaver",
    "retrograde": "outrobeaver",
    "outrobeaver": "outrobeaver",
    "nexus": "nexus",
    "spark": "spark",
    "falcon": "falcon",
    "drafter": "drafter",
    "eagle": "drafter",
    "scout": "scout",
    "axon_hillock": "oc",
    "oc": "oc",
    "myelinator": "olc",
    "olc": "olc",
    "terminal": "terminal",
    "saltatory_gate": "saltatory_gate",
}


def _int_metric(value: Any) -> int:
    """Coerce a provider-reported counter to int, or 0 when it is unusable.

    Providers differ: some omit the cache keys, some send null. Telemetry
    must never be the reason a call fails, so anything unreadable is 0.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _messages_chars(messages: Any) -> int:
    """Total characters of the outgoing message contents.

    Measured from the messages themselves, before the request is sent, so a
    provider that reports nothing still leaves a size on the record. Only a
    count is kept; the text itself never goes onto the usage record.
    """
    if not isinstance(messages, list):
        return 0
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
    return total


@dataclass
class CallRecord:
    stage: str = ""
    session_id: str = ""
    chatset_id: str = ""
    model: str = ""
    status: str = ""
    attempt: int = 0
    attempts: list = field(default_factory=list)
    gate: Any = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    chars_in: int = 0
    latency_ms: float = 0.0
    error: str = ""
    # What the engine says it recorded: its session, this call's stage id
    # (DI-193) and the dashboard link. Empty when the engine returns none.
    engine_session: str = ""
    engine_stage: str = ""
    log_link: str = ""
    raw_response: str = ""
    sys_prompt: str = ""
    user_msg: str = ""


class StageResult:
    """What a dendrite gets back. No model selection happened on their side."""

    def __init__(
        self,
        content: str,
        model_id: str = "unknown",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: float = 0.0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        chars_in: int = 0,
        engine_session: str = "",
        engine_stage: str = "",
        log_link: str = "",
    ) -> None:
        self.engine_session = engine_session
        self.engine_stage = engine_stage
        self.log_link = log_link
        self.content = content
        self.model_id = model_id
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.latency_ms = latency_ms
        self.cache_hit_tokens = cache_hit_tokens
        self.cache_miss_tokens = cache_miss_tokens
        self.chars_in = chars_in


class ProfileError(RuntimeError):
    """The deck file is absent, unreadable, or not a profile.

    A run whose deck is wrong cannot be interpreted afterwards, so this is
    raised rather than defaulted: there is no correct deck to guess.
    """


def describe_deck(profile_path: str | Path) -> dict:
    """Describe the deck a profile loads, read-only.

    Returns ``{"path", "name", "models", "stages", "stages_pinned"}``.
    ``models`` is the number of cards the profile puts in play and ``stages``
    names them, so a log line says which deck produced a run instead of only
    which file was named.

    Raises ProfileError if the path is unset, missing, unreadable, not JSON,
    or not a JSON object. Nothing is written and no model is selected here.
    """
    if not profile_path:
        raise ProfileError(
            "TurnKey profile path is not set: no deck can be described")

    path = Path(profile_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProfileError(f"TurnKey profile not found: {path}") from exc
    except OSError as exc:
        raise ProfileError(
            f"TurnKey profile is unreadable: {path}: {exc}") from exc

    try:
        profile = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileError(
            f"TurnKey profile is not valid JSON: {path}: {exc}") from exc

    if not isinstance(profile, dict):
        raise ProfileError(
            f"TurnKey profile root is not a JSON object: {path}")

    deck = profile.get("deck")
    if not isinstance(deck, list):
        # Peacock-era profiles list their cards under "attempts".
        deck = profile.get("attempts")
    if not isinstance(deck, list):
        raise ProfileError(
            f"TurnKey profile has no 'deck' or 'attempts' list: {path}")

    raw_stages = profile.get("stages")
    stages: list[str] = []
    pinned = 0
    if isinstance(raw_stages, list):
        for entry in raw_stages:
            if not isinstance(entry, dict):
                continue
            name = entry.get("stageName") or entry.get("stage") or ""
            if name:
                stages.append(str(name))
            if entry.get("pinnedModels"):
                pinned += 1
    elif isinstance(raw_stages, dict):
        for name, entry in raw_stages.items():
            stages.append(str(name))
            if isinstance(entry, dict) and entry.get("pinnedModels"):
                pinned += 1

    return {
        "path": str(path),
        "name": str(profile.get("name") or ""),
        "models": len(deck),
        "stages": stages,
        "stages_pinned": pinned,
    }


class TurnKeyClient:
    """Middleware. Takes a stage name. Hands it to TurnKey. Returns the text."""

    def __init__(self, profile_path: str) -> None:
        self._profile_path = profile_path
        # Describe the deck before the SDK loads it: the line must exist even
        # if the deck turns out to be unusable, and a bad path is fatal here
        # with the path in the message rather than deep inside the SDK.
        self._deck = describe_deck(profile_path)
        print(f"deck: {self._deck['path']}  models={self._deck['models']}  "
              f"stages_pinned={self._deck['stages_pinned']}", flush=True)
        self._sdk = self._load_sdk(profile_path)
        self._pea_param = self._pea_param_name(self._sdk.run_stage)
        self._pea_kw = self._pea_param is not None

    @staticmethod
    def _pea_param_name(fn) -> str | None:
        """The keyword the installed SDK's run_stage takes for the session id.

        The SDK names it ``session_id`` and turns it into the engine's
        ``pea_session``. It has no ``**kwargs``, so looking only for a
        ``pea_session`` parameter found nothing and the id was never sent:
        the engine then made a new session for every call.
        """
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return "pea_session"
        for name in ("pea_session", "session_id"):
            if name in params:
                return name
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return "pea_session"
        return None

    @staticmethod
    def _supports_pea_session(fn) -> bool:
        """True when the installed SDK accepts a session id."""
        return TurnKeyClient._pea_param_name(fn) is not None

    @staticmethod
    def _load_sdk(profile_path: str):
        try:
            from turnkey.profile_integration import initialize_sdk_with_profile
        except ImportError as exc:
            raise RuntimeError(
                "TurnKey SDK is not importable. It is the only middleware "
                "for model calls. Install or path it before running."
            ) from exc
        return initialize_sdk_with_profile(profile_path)

    def call(self, stage: str, messages: list[dict[str, str]],
             pea_session: str | None = None,
             temperature: float | None = None) -> StageResult:
        mapped = _STAGE_MAP.get(stage, stage)
        # Size is measured before the request leaves, so it is available even
        # when the provider reports no usage block at all.
        chars_in = _messages_chars(messages)
        start = time.monotonic()
        pea_param = getattr(self, "_pea_param", "pea_session")
        kwargs = {pea_param: pea_session} if self._pea_kw and pea_param else {}
        # run_stage only sends temperature when it is not None, so passing it
        # unconditionally is safe and makes unset the explicit default.
        if temperature is not None:
            kwargs["temperature"] = temperature
        raw = self._sdk.run_stage(mapped, messages, **kwargs)
        elapsed = (time.monotonic() - start) * 1000

        choices = raw.get("choices", []) if isinstance(raw, dict) else []
        content = ""
        if choices:
            content = choices[0].get("message", {}).get("content", "") or ""

        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        return StageResult(
            content=content,
            model_id=raw.get("model", "unknown") if isinstance(raw, dict) else "unknown",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=elapsed,
            # DeepSeek and Qwen report prefix-cache accounting here; other
            # providers omit it. Absent or unreadable is 0, never a raise.
            cache_hit_tokens=_int_metric(usage.get("prompt_cache_hit_tokens", 0)),
            cache_miss_tokens=_int_metric(usage.get("prompt_cache_miss_tokens", 0)),
            chars_in=chars_in,
            # The engine returns these beside the answer. Absent is "", never
            # a raise: a run must not fail because a link did not come back.
            engine_session=str(raw.get("session") or "") if isinstance(raw, dict) else "",
            engine_stage=str(raw.get("stage") or "") if isinstance(raw, dict) else "",
            log_link=str(raw.get("log_link") or "") if isinstance(raw, dict) else "",
        )

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> StageResult:
        stage = kwargs.get("stage") or kwargs.get("model") or "distiller"
        return self.call(stage, messages)


class EngineClient:
    """Adapter so the engine can call TurnKey with the old call_stage shape.

    TurnKey still picks the model. The engine never sees a model id until
    the response comes back.
    """

    def __init__(self, profile_path: str) -> None:
        self._tk = TurnKeyClient(profile_path)
        self.timeout = 180.0

    def model_for(self, stage: str) -> str:
        """Name the model the next call will use.

        This used to return the literal string "turnkey", which appeared in
        every progress line and summary table as though it were a model. The
        rotator can say which card it is about to serve without consuming it.
        """
        return self.route_for(stage).get("model_id") or "unknown"

    def route_for(self, stage: str) -> dict:
        """Model id and provider for the next call, without consuming it.

        The provider is the gateway the card belongs to (openrouter, merge,
        xkiro, ollama, novita). Two models with the same id can behave
        differently behind different gateways, so a telemetry block that
        names only the model is ambiguous when something needs explaining.
        """
        try:
            info = self._tk._sdk.rotator.peek_model(stage)
        except Exception:
            # A profile with no deck is a configuration error the call itself
            # will surface; the display should not crash first.
            return {"model_id": "unknown", "provider": "unknown"}
        return {
            "model_id": info.get("model_id") or "unknown",
            "provider": info.get("gateway") or "unknown",
        }

    # A DNS failure cannot be fixed by waiting: the host does not resolve at
    # all, so every retry fails the same way. Give it one quick second chance
    # and then surface it instead of burning the whole retry budget.
    _UNRECOVERABLE = ("Name or service not known",
                      "nodename nor servname",
                      "Temporary failure in name resolution")

    def _call_with_retry(self, stage: str, messages: list[dict[str, str]],
                         attempts: int = 10,
                         pea_session: str | None = None,
                         temperature: float | None = None) -> Any:
        """Retry transport failures that escape the TurnKey SDK.

        The SDK converts URLError/TimeoutError to SDKError and retries those,
        but ``http.client.RemoteDisconnected`` is a ConnectionError/BadStatusLine
        and is not a URLError, so it propagates out of ``run_stage`` and kills
        the chatset. Retrying here also advances the SDK's deck rotator, so a
        bad model is skipped instead of being hit again.

        Failures that cannot improve with time (DNS resolution) abort after the
        first retry, because retrying them only multiplies the wait by the
        attempt count.
        """
        last: Exception | None = None
        dns_failures = 0
        for attempt in range(attempts):
            # Name the model this attempt will use. Without it a retry storm
            # reads as one model failing repeatedly, when the deck is actually
            # rotating behind it and every card is being burned in turn.
            model = self.model_for(stage)
            try:
                return self._tk.call(stage, messages, pea_session=pea_session,
                                     temperature=temperature)
            except TypeError:
                # A signature mismatch is a code bug, not a transport failure.
                # Retrying it ten times only burns the deck.
                raise
            except (ConnectionError, TimeoutError, http.client.HTTPException) as exc:
                last = exc
                text = f"{type(exc).__name__}: {exc}"
                print(f"\n[retry] {stage} attempt {attempt + 1}/{attempts} "
                      f"model={model} failed: {text}", file=sys.stderr, flush=True)
                if any(tok in text for tok in self._UNRECOVERABLE):
                    dns_failures += 1
                    if dns_failures >= 2:
                        print(f"[retry] {stage}: DNS failure will not recover, "
                              f"stopping after {attempt + 1} attempt(s)",
                              file=sys.stderr, flush=True)
                        raise
            except Exception as exc:
                # SDKError and anything else — also retry to rotate the deck
                last = exc
                text = f"{type(exc).__name__}: {exc}"
                print(f"\n[retry] {stage} attempt {attempt + 1}/{attempts} "
                      f"model={model} failed: {text}", file=sys.stderr, flush=True)
                if any(tok in text for tok in self._UNRECOVERABLE):
                    dns_failures += 1
                    if dns_failures >= 2:
                        print(f"[retry] {stage}: DNS failure will not recover, "
                              f"stopping after {attempt + 1} attempt(s)",
                              file=sys.stderr, flush=True)
                        raise
        assert last is not None
        raise last

    def call_stage(self, stage: str, *, session_id: str, chatset_id: str,
                   chunk: str = "", contracts: str = "",
                   payload: dict | None = None,
                   expected_hash: str | None = None,
                   temperature: float | None = None,
                   soma: Any = None,
                   raw_chat_set: list[dict] | None = None,
                   session_code: str | None = None,
                   emit: Any = None) -> Any:
        """Call one stage and verify its output before it is trusted.

        ``session_code`` is the id the engine generated for this session (None
        on its first call, so the engine generates one); see ``engine_sessions``.

        ``emit(label, text)``, when given, is called at each real step, at the
        moment it happens, so the operator watches the work live instead of
        reading a dump after the call. Every value in ``text`` is read from
        the step that just ran; nothing is estimated. A fault in ``emit`` is
        swallowed, because a display problem must never fail a stage.

        Instructions come from the ``stage_instructions`` row for this stage,
        never from the markdown files on disk. A stage is told its own job and
        nothing about the other stages, so it has no pipeline to reason about.

        When ``raw_chat_set`` is supplied (distiller and introbeaver), every
        emitted entry must match the source text at the offsets it claims.
        A provenance failure fails the stage: the model's claim is not
        allowed into the graph unverified.
        """
        from .fleet import assemble
        from .gate import summarize, validate, is_done, is_hard_block
        from .provenance import (verify_distiller, verify_introbeaver)

        # The ENGINE generates the session id; the pipeline does not make one
        # up. ``session_code`` is the id the engine already generated for this
        # session, or None on the session's first call.
        #   * None  -> nothing is sent, which tells the engine "generate one";
        #              its reply names it (engine_session) and the caller keeps it.
        #   * a code -> sent as pea_session, which tells the engine the id is
        #              already assigned, so it does not generate another.
        # Never fall back to the chat-session name or to a local counter such
        # as SES-007: sending those made every re-run of the same chat session
        # land in one engine session. See engine_sessions.py and
        # session-stage-linkage.md.
        pea_session = session_code or None

        def say(label: str, text: str) -> None:
            """Show one real event now. Never lets a display fault fail the stage."""
            if emit is None:
                return
            try:
                emit(label, text)
            except Exception:
                pass

        instruction = None
        if soma is not None:
            instruction = soma.get_stage_instruction(stage)
        if instruction is None:
            raise RuntimeError(
                f"stage {stage!r} has no row in stage_instructions. "
                f"Instructions are read from the database, not from disk; "
                f"build the store from soma_template.sql first.")

        sys_prompt = instruction["body"]
        say("instructions",
            f"read from the Soma: stage_instructions[{stage}] · "
            f"{instruction.get('tb_number') or '—'} v{instruction.get('version', '—')} · "
            f"{len(sys_prompt):,} bytes")
        _, user_msg = assemble(stage, sys_prompt, chunk=chunk,
                               contracts=contracts, payload=payload)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]
        say("input",
            f"read from the Soma: {_payload_desc(payload, chunk)} · "
            f"system {len(sys_prompt):,} + user {len(user_msg):,} chars")
        if temperature is None:
            temperature = (DEFAULT_EXTRACTIVE_TEMPERATURE
                           if stage in _EXTRACTIVE_STAGES else None)
        say("request",
            f"sent · {len(messages)} messages · temperature "
            f"{temperature if temperature is not None else 'model default'} · waiting for the model")
        result = self._call_with_retry(stage, messages, pea_session=pea_session,
                                       temperature=temperature)
        say("response",
            f"{result.model_id} replied · {len(result.content or ''):,} chars · "
            f"tokens in {_tok(result.prompt_tokens)} / out {_tok(result.completion_tokens)} · "
            f"{result.latency_ms / 1000:.1f}s · engine "
            f"{result.engine_session or '—'}/{result.engine_stage or '—'}")
        gate = validate(stage, result.content, expected_hash=expected_hash)
        _say_gate(say, gate, is_done)

        # ── provenance: the claim must exist in the source ──────────────
        if raw_chat_set is not None and gate.parsed and is_done(gate):
            if stage == "distiller":
                prov = verify_distiller(gate.parsed, raw_chat_set)
            elif stage == "introbeaver":
                prov = verify_introbeaver(gate.parsed, raw_chat_set)
            else:
                prov = None
            if prov is not None:
                say("anchors",
                    f"{prov.verified}/{prov.checked} quotes found in the transcript "
                    f"(offsets located by Python)"
                    + (f" · {len(prov.ambiguous)} repeated" if prov.ambiguous else "")
                    + ("" if prov.ok else " · REJECTED: " + "; ".join(
                        str(i) for i in prov.issues[:2])))
            if prov is not None and not prov.ok:
                gate.errors.extend(str(i) for i in prov.issues)
                gate.ok = False
                gate.provenance = prov.summary()
            if prov is not None:
                # The counts must describe the entries that survived the
                # check. The model's arrays still contain the rejected ones,
                # so counting the payload here would overstate the stage.
                gate.accomplishment_summary = summarize(gate, prov.accepted)
            if stage == "distiller" and prov is not None and prov.ok:
                # The arithmetic is Python's: bytes, ratios, noise counts and
                # the two numeric skip rules overwrite anything the model sent.
                from . import measure
                m = measure.apply(gate, prov.accepted, raw_chat_set)
                b = (m.get("bytes") or {}) if isinstance(m, dict) else {}
                say("python",
                    f"measured: {b.get('original', '—')} bytes in, "
                    f"{b.get('signal', '—')} signal, {b.get('noise', '—')} noise · "
                    f"signal quality {m.get('signal_quality')} · "
                    + (f"SKIP {m.get('skip_reason')}" if m.get("skip") else "proceed"))
            if stage == "introbeaver" and prov is not None and prov.ok:
                # Counts and confidence are arithmetic on what the model quoted
                # (TB-004 SCORING); Python's values overwrite the model's.
                from . import intro_measure
                n_warn = len(gate.warnings)
                m = intro_measure.apply(gate, raw_chat_set)
                body = (gate.parsed or {}).get("introbeaver_output") or {}
                confs = [e.get("confidence") for e in body.get("entries") or []
                         if isinstance(e, dict)]
                say("python",
                    f"computed: {m.get('counts', {}).get('extracted_count', '—')} entries "
                    f"{m.get('counts', {}).get('entry_count_by_type', {})} · "
                    f"signal quality {m.get('signal_quality', '—')} · "
                    f"confidence {confs} · status {m.get('status', '—')} · "
                    f"{len(gate.warnings) - n_warn} correction(s) to the model's reply")
            if stage == "outrobeaver":
                # Hashes, per-turn scores and session health are Python's too
                # (TB-006 SCORING); the model's values are overwritten.
                from . import outro_measure
                n_warn = len(gate.warnings)
                m = outro_measure.apply(gate, raw_chat_set)
                if m.get("applied"):
                    say("python",
                        f"computed: health {m.get('session_health')} (score {m.get('score')}"
                        f"{', override: ' + m['override'] if m.get('override') else ''}) · "
                        f"{m.get('journal_entries', '—')} journal hashes · "
                        f"{len(gate.warnings) - n_warn} correction(s) to the model's reply")
                else:
                    say("python", f"health not computed: {m.get('why')}")

        rec = CallRecord(
            stage=stage,
            session_id=session_id,
            chatset_id=chatset_id,
            model=result.model_id,
            status="pass" if is_done(gate) else ("blocked" if is_hard_block(gate) else "fail"),
            attempt=1,
            attempts=[{"attempt": 1, "model": result.model_id}],
            gate=gate,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            cache_hit_tokens=result.cache_hit_tokens,
            cache_miss_tokens=result.cache_miss_tokens,
            chars_in=result.chars_in,
            latency_ms=result.latency_ms,
            engine_session=result.engine_session,
            engine_stage=result.engine_stage,
            log_link=result.log_link,
            raw_response=result.content,
            sys_prompt=sys_prompt,
            user_msg=user_msg,
        )

        class _Res:
            pass
        res = _Res()
        res.ok = is_done(gate)
        res.record = rec
        res.payload = gate.parsed
        res.skipped = gate.skipped
        res.verdict = gate.verdict
        return res
