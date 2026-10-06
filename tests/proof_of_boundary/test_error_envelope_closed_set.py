# PB (containment): what the caller-visible response carries on every
# non-success path.
#
# A different property from "is the answer cleared". Clearing the report says
# nothing about what the ERROR itself says, and the two fail independently: a
# response can carry no draft at all and still publish node-authored text —
# error_log lines, a gate's own violation string, an upstream exception message.
# Those can embed an identifier, a name, a third-party response body; truncating
# or redacting them is not a closed set. So the assertion here is not "the
# refused content is absent" but the stronger "every value present is one this
# template declared".
#
# Three surfaces, because each can hold the property while the next drops it:
#
#   1. the node delta        — ResultAssembleNode's own return
#   2. the resolved response — PriceLabelVerificationAgent.get_output(), after
#                              the state reducer has merged the delta
#   3. the invoke body       — the real ASGI POST /invoke, which is what a
#                              deployed caller actually receives
#
# error_log is seeded on every path with the kind of line an upstream failure
# leaves behind — a name and a credential-shaped token inside an echoed response
# body — and each fragment is asserted absent from all three, walking nested
# mapping KEYS as well as values. The line itself stays in error_log: that is
# the internal channel, the state reducer appends to it and the audit trail
# needs it. It is simply never projected.
#
# Faults are injected on the DATA path — the report note, the determination, the
# model client — never on the gate. A test that patches the gate to force a
# violation has tested the patch.

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any, ClassVar, Iterator, Mapping, Optional

import httpx
import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes import result_assemble
from src.nodes.promotion_claim_check import MODEL_CALL_FAILED, PromotionClaimCheckNode
from src.nodes.result_assemble import (
    ERROR_REASONS,
    WITHHELD_KEYS,
    WITHHELD_NOTICE,
    ResultAssembleNode,
)
from src.nodes.rule_load import RuleLoadNode
from tests.conftest import StubChatClient, base_config

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TOKEN = "containment-test-token"

_CLEAN_LABEL = {
    "label_id": "CONT-1",
    "pos_price": 980,
    "web_price": 980,
    "sale_price": 980,
    "regular_price": 1400,
    "promotion_spec": {"claim": "最大30%オフ", "discount_rate": 0.30},
}


# ── sentinels, assembled at runtime ──────────────────────────
#
# Never written as literals: the blocking credential gate scans the whole tree,
# and a committed credential-shaped string is a finding whether or not it is
# real. Assembling here keeps the gate honest and the fixture recognisable.


def _token() -> str:
    return "sk-" + "live-" + "x" * 3


def _sentinel() -> str:
    """An error_log line of the kind an upstream failure produces: a name and a
    credential-shaped token inside an echoed response body."""
    return "boom: upstream said {'customer':'A. Tanaka','token':'" + _token() + "'}"


_SENTINEL_FRAGMENTS = ("A. Tanaka", "boom: upstream", "sk-" + "live-")


def _credential() -> str:
    """A value the output gate is meant to catch — an OpenAI-shaped key."""
    return "sk-" + "abcdefghijklmnopqrstuvwxyz012345"


def _walk(value: Any) -> Iterator[str]:
    """Every string in a nested structure, mapping KEYS included.

    Keys are walked because a leaked fragment can land in one: a payload keyed
    by a caller field name publishes that name whatever the values say.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _assert_clean(payload: Any, *, also: tuple[str, ...] = ()) -> None:
    """No sentinel fragment, and nothing in `also`, anywhere in `payload`."""
    strings = list(_walk(payload))
    for fragment in (*_SENTINEL_FRAGMENTS, *also):
        offenders = [text for text in strings if fragment in text]
        assert not offenders, f"{fragment!r} published in {offenders!r}"


# ── driving the node and the resolved response ───────────────

# Fields the state reducer accumulates rather than replaces.
_ACCUMULATED = ("error_log", "node_history")


def _state(labels: list, **overrides: Any) -> dict:
    """The outer state as it stands when the assembling node runs, with an
    upstream failure line already in error_log."""
    state: dict[str, Any] = {
        "labels_input": json.dumps(labels, ensure_ascii=False),
        "loaded_rules": json.dumps({"profile": "JP", "rules": _shipped_rules()}, ensure_ascii=False),
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pb-containment",
        "trace_id": "pb-containment-trace",
        "node_history": ["InitializeNode", "PreProcessSlotNode", "MainSlotNode"],
        "error_log": [_sentinel()],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _shipped_rules() -> list:
    import yaml

    return list(yaml.safe_load((_ROOT / "config" / "keihin_rules.yaml").read_text(encoding="utf-8"))["rules"])


def _merge(state: dict, partial: dict) -> dict:
    merged = dict(state)
    for key, value in partial.items():
        if key in _ACCUMULATED:
            merged[key] = list(merged.get(key, [])) + list(value)
        else:
            merged[key] = value
    return merged


def _run(state: dict, node: Optional[ResultAssembleNode] = None) -> tuple[dict, dict, dict]:
    """Run the assembling node over `state`.

    Returns (node delta, merged state, resolved response). The node is driven
    through `__call__` so the framework's trust gate and credential scan run
    exactly as they do in a real invocation.
    """
    from src.graph.graph import PriceLabelVerificationAgent

    partial = (node or ResultAssembleNode())(state)
    merged = _merge(state, partial)
    agent = PriceLabelVerificationAgent(config=base_config(llm=StubChatClient()), project_root=_ROOT)
    return partial, merged, agent.get_output(merged)


# The non-success paths this node can take, each reached by data.
_REFUSED_STATES = [
    pytest.param(
        _state([_CLEAN_LABEL], loaded_rules=None),
        result_assemble._REASON_RULES_UNAVAILABLE,
        id="no-rule-set-to-ground-the-report",
    ),
    pytest.param(
        _state([_CLEAN_LABEL], loaded_rules="{not json"),
        result_assemble._REASON_RULES_UNAVAILABLE,
        id="unreadable-rule-set",
    ),
    pytest.param(
        _state(
            [_CLEAN_LABEL],
            promotion_findings=json.dumps(
                [
                    {
                        "label_id": "CONT-1",
                        "status": "FAIL",
                        "defensible": False,
                        "assessment": "根拠不足 " + _credential(),
                    }
                ]
            ),
        ),
        result_assemble._REASON_OUTPUT_WITHHELD,
        id="credential-in-the-assembled-report",
    ),
]


class TestTheNodeDeltaIsClosedSet:
    """Surface 1 — what ResultAssembleNode itself returns."""

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_every_published_value_is_a_declared_constant(self, state, reason):
        partial, _merged, _envelope = _run(state)

        assert partial["status"] == AgentStatus.ERROR.value
        payload = json.loads(partial["formatted_output"])
        assert set(payload) == WITHHELD_KEYS
        assert set(payload.values()) <= ERROR_REASONS | {WITHHELD_NOTICE}
        assert payload["reason"] == reason
        # Both caller-reachable renderings say the same thing.
        assert partial["verification_report"] == partial["formatted_output"]

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_the_payload_is_truthy(self, state, reason):
        """A falsy replacement re-opens `formatted_output or result`.

        AgentBaseGraph.get_output() applies no status check, so an empty
        mapping or an empty string hands the caller whatever survived in state
        — and the failure mode looks identical to no fix at all.
        """
        partial, _merged, _envelope = _run(state)

        assert partial["formatted_output"]
        assert partial["verification_report"]

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_no_upstream_error_text_is_published(self, state, reason):
        partial, _merged, _envelope = _run(state)

        assert_also = (_credential(),) if reason == result_assemble._REASON_OUTPUT_WITHHELD else ()
        published = {key: value for key, value in partial.items() if key != "error_log"}
        _assert_clean(published, also=assert_also)

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_the_determination_is_cleared_by_presence(self, state, reason):
        """An omitted key leaves the previous value in state for a checkpoint
        or a downstream reader to pick straight back up."""
        partial, _merged, _envelope = _run(state)

        assert "regulatory_findings" in partial
        assert partial["regulatory_findings"] is None

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_the_inner_line_is_kept_once_and_not_re_emitted(self, state, reason):
        """error_log is the internal channel. The reducer appends, so a node
        that re-emitted what it read would duplicate every line."""
        _partial, merged, _envelope = _run(state)

        assert merged["error_log"].count(_sentinel()) == 1

    def test_the_violation_location_travels_in_error_log_only(self):
        """A refusal names a place, not a value — and it stays internal."""
        state, reason = _REFUSED_STATES[2].values
        partial, _merged, envelope = _run(state)

        reported = " ".join(partial["error_log"])
        assert "credential classes openai_key in verification_report" in reported
        assert _credential() not in reported
        # None of that wording is in the response: the only sentence a caller
        # reads is the declared notice.
        rendered = json.dumps(envelope, ensure_ascii=False, default=str)
        assert "openai_key" not in rendered
        assert "credential classes" not in rendered
        assert "refused the report" not in rendered


class TestTheResolvedResponseIsClosedSet:
    """Surface 2 — what get_output() hands back after the reducer merges."""

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_the_response_publishes_the_reason_code_and_nothing_else(self, state, reason):
        _partial, _merged, envelope = _run(state)

        assert envelope["status"] == AgentStatus.ERROR.value
        assert envelope["blocked"] is True
        assert envelope["output"], "a falsy payload re-opens the fallback"
        payload = json.loads(envelope["output"])
        assert set(payload) == WITHHELD_KEYS
        assert payload["reason"] in ERROR_REASONS
        assert payload["note"] == WITHHELD_NOTICE

    @pytest.mark.parametrize("state, reason", _REFUSED_STATES)
    def test_no_error_log_and_no_upstream_text_reaches_the_caller(self, state, reason):
        _partial, _merged, envelope = _run(state)

        assert "error_log" not in envelope
        assert_also = (_credential(),) if reason == result_assemble._REASON_OUTPUT_WITHHELD else ()
        _assert_clean(envelope, also=assert_also)

    def test_an_assembled_draft_beside_an_error_is_not_released(self):
        """The one channel this override exists to close, measured directly."""
        from src.graph.graph import PriceLabelVerificationAgent

        agent = PriceLabelVerificationAgent(config=base_config(llm=StubChatClient()), project_root=_ROOT)
        released = agent.get_output(
            {
                "status": AgentStatus.ERROR.value,
                "blocked": True,
                "verification_report": json.dumps({"summary": {"total": 1}, "labels": ["un-gated draft"]}),
                "error_log": [_sentinel()],
                "node_history": [],
            }
        )
        assert released["output"] is None
        _assert_clean(released, also=("un-gated draft",))


class TestTheInvokeBodyIsClosedSet:
    """Surface 3 — the real ASGI POST /invoke, what a deployed caller reads."""

    def test_a_refused_response_publishes_the_reason_code_only(self, invoke):
        body = invoke(note="contact " + _credential(), seed_error_log=True)

        assert body["status"] in ("error", AgentStatus.ERROR.value)
        assert body["blocked"] is True
        payload = json.loads(body["output"])
        assert set(payload) == WITHHELD_KEYS
        assert payload["reason"] == result_assemble._REASON_OUTPUT_WITHHELD
        assert "error_log" not in body
        _assert_clean(body, also=(_credential(), "openai_key"))

    def test_an_upstream_refusal_publishes_nothing_at_all(self, invoke):
        """The batch never reaches the assembling node: the backbone routes an
        error straight to finalize, so post_process does not run."""
        body = invoke(labels="not-a-list", seed_error_log=True)

        assert body["status"] in ("error", AgentStatus.ERROR.value)
        assert not body["output"]
        assert "error_log" not in body
        _assert_clean(body)

    def test_a_clean_response_still_ships_the_report(self, invoke):
        """CONTROL: a gate that refused everything would satisfy every
        assertion above."""
        body = invoke(seed_error_log=True)

        assert body["status"] in ("success", AgentStatus.SUCCESS.value)
        report = json.loads(body["output"])
        assert report["summary"]["total"] == 1
        assert report["labels"][0]["label_id"] == "CONT-1"
        # The upstream line is in state throughout and still reaches no caller.
        _assert_clean(body)
        assert not any(reason in json.dumps(body, ensure_ascii=False) for reason in ERROR_REASONS)


class TestTheAssessmentCallCannotLeakUpstreamText:
    """The source half: this template's only third-party call.

    A provider exception's message quotes the response body it came from. Left
    unhandled it reaches the framework's error path, which writes str(exc) and a
    full traceback into error_log — so the closed-set rule is applied at the call
    site rather than at the envelope.
    """

    def test_a_raising_client_records_the_class_not_the_message(self, promotion_prompt):
        class _Raises:
            status_code = 502

            def complete(self, messages: list) -> dict:
                raise RuntimeError(_sentinel())

        system, template = promotion_prompt
        node = PromotionClaimCheckNode(llm_client=_Raises(), system_prompt=system, user_prompt_template=template)
        out = node.execute({"labels_input": json.dumps([_CLEAN_LABEL], ensure_ascii=False)})

        findings = json.loads(out["promotion_findings"])
        assert out["status"] == AgentStatus.SUCCESS.value
        assert findings[0]["status"] == "REVIEW"
        assert findings[0]["reason"] == MODEL_CALL_FAILED
        _assert_clean(out)
        assert "Traceback" not in json.dumps(out, ensure_ascii=False)

    def test_a_failed_assessment_is_never_an_approval(self, promotion_prompt):
        class _Raises:
            def complete(self, messages: list) -> dict:
                raise RuntimeError(_sentinel())

        system, template = promotion_prompt
        node = PromotionClaimCheckNode(llm_client=_Raises(), system_prompt=system, user_prompt_template=template)
        findings = json.loads(
            node.execute({"labels_input": json.dumps([_CLEAN_LABEL], ensure_ascii=False)})["promotion_findings"]
        )

        assert findings[0]["status"] != "PASS"

    def test_the_failure_reaches_no_caller_end_to_end(self, invoke):
        body = invoke(client=_RaisingChatClient())

        assert body["status"] in ("success", AgentStatus.SUCCESS.value)
        report = json.loads(body["output"])
        assert report["labels"][0]["checks"]["promotion_claim"] == "REVIEW"
        _assert_clean(body)
        assert "Traceback" not in json.dumps(body, ensure_ascii=False)


class TestARefusedValueIsNeverEchoed:
    """The caller-contract half: a refusal names the field, never the value.

    Every CallerInputError message in src/services/validation is composed of
    that module's own constant phrases plus a positional field path. This drives
    a recognisable value through each refusal path and pins the invariant, so a
    future message that interpolates the value fails here rather than in review.
    """

    @pytest.mark.parametrize(
        "label",
        [
            pytest.param("not-an-object", id="label-is-not-an-object"),
            pytest.param({"label_id": "A. Tanaka boom: upstream", "pos_price": 1}, id="identifier-alphabet"),
            pytest.param({"label_id": "L", "pos_price": "boom: upstream"}, id="price-is-not-a-number"),
            pytest.param({"label_id": "L", "pos_price": 1, "image": "boom: upstream"}, id="image-label"),
            pytest.param({"label_id": "L", "shelf_label_text": "boom: upstream"}, id="no-price-field"),
            pytest.param({"label_id": "L", "pos_price": 1, "shelf_label_text": 42}, id="text-is-not-a-string"),
            pytest.param(
                {"label_id": "L", "pos_price": 1, "promotion_spec": {"claim": "boom: upstream", "period_start": 7}},
                id="period-is-not-a-string",
            ),
            pytest.param(
                {"label_id": "L", "pos_price": 1, "promotion_spec": {"prize_value": "boom: upstream"}},
                id="prize-is-not-a-number",
            ),
        ],
    )
    def test_the_refusal_names_the_field_not_the_value(self, label):
        from src.nodes.input_parse import InputParseNode

        out = InputParseNode().execute({"labels_input": json.dumps([label], ensure_ascii=False)})

        assert out["status"] == AgentStatus.ERROR.value
        _assert_clean(out)

    def test_a_non_finite_literal_is_refused_without_echoing_it(self):
        from src.nodes.input_parse import InputParseNode

        out = InputParseNode().execute({"labels_input": '[{"label_id": "L", "pos_price": NaN}]'})

        assert out["status"] == AgentStatus.ERROR.value
        assert "NaN" not in " ".join(out["error_log"])

    def test_a_malformed_batch_is_refused_without_echoing_the_decoder(self):
        from src.nodes.input_parse import InputParseNode

        out = InputParseNode().execute({"labels_input": '[{"label_id": "boom: upstream"'})

        assert out["status"] == AgentStatus.ERROR.value
        assert out["error_log"] == ["InputParseNode: labels_input is not valid JSON"]


# ── the deployed surface ─────────────────────────────────────


class _RaisingChatClient:
    """A model client that fails the way a provider client fails."""

    status_code = 502

    def complete(self, messages: list) -> dict:
        raise RuntimeError(_sentinel())


class _RuleLoadThatLogsAnUpstreamFailure(FunctionNode):
    """RuleLoad, plus the line an upstream failure leaves behind.

    error_log is an appending channel, so a line written here is in state when
    the assembling node runs AND when the response is resolved — which is
    exactly the state this contract is about. Injected on the data path; no gate
    is touched.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, inner: RuleLoadNode) -> None:
        self._inner = inner

    def execute(self, state: Mapping[str, Any], config: Optional[dict] = None) -> dict[str, Any]:
        result = dict(self._inner.execute(state))
        result["error_log"] = [_sentinel()]
        return result


class _Client:
    """Drives the application object over ASGI, exactly as a server would."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent.invalid") as client:
                return await client.post(url, **kwargs)

        return asyncio.run(_run())


@pytest.fixture
def invoke(monkeypatch):
    """POST a batch to the shipped application and return the response body."""
    from src.api import server
    from src.graph.graph import PriceLabelVerificationAgent

    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)

    def _invoke(
        labels: Any = None,
        note: str = "for internal reporting purposes",
        seed_error_log: bool = False,
        client: Any = None,
    ) -> dict:
        agent = PriceLabelVerificationAgent(
            config=base_config(llm=client or StubChatClient(), report={"note": note}),
            project_root=_ROOT,
        )
        if seed_error_log:
            agent._rule_load = _RuleLoadThatLogsAnUpstreamFailure(agent._rule_load)
        agent.compile()
        monkeypatch.setattr(server, "agent", agent)

        payload = [_CLEAN_LABEL] if labels is None else labels
        response = _Client(server.app).post(
            "/invoke",
            json={
                "input": json.dumps(payload, ensure_ascii=False),
                "session_id": "pb-containment",
                "input_context": {},
            },
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    return _invoke
