"""Layer 2 of the output gate — the regulatory determination contract.

The determination used to be a registered graph layer ahead of the assembling
node. A layer is enforced only by the topology that contains it, so the first
test here measures what that was worth: with the layer dropped and nothing else
changed, the shipped pipeline returned ``status: success`` and a report reading
PASS for a label breaching three rules. Everything after it pins the replacement
— the determination is produced where the report is assembled, in the same
delta, and the output boundary refuses a report that disagrees with it.

Faults are injected on the DATA path (the determination, the renderer, the rule
set), never on the gate. A test that patches the gate to force a violation has
tested the patch.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.nodes import regulatory_determination as determination
from src.nodes.result_assemble import ERROR_REASONS, NOT_RUN, WITHHELD_NOTICE, ResultAssembleNode

_ROOT = pathlib.Path(__file__).resolve().parents[2]

# 二重価格表示: the advertised reference price does not exceed the price actually
# charged, and the prize draw is five times the statutory ceiling. Neither is a
# price-consistency question and neither is a claim-wording question — no
# discount rate is declared, so the deterministic price checks stay clean and
# this label's verdict rests entirely on the regulatory determination.
BREACHING_LABEL = {
    "label_id": "GATE-1",
    "sale_price": 1980,
    "regular_price": 1980,
    "shelf_label_text": "通常価格1980円のところ本日限り1980円",
    "promotion_spec": {"claim": "本日限りの特別価格", "prize_value": 500000, "transaction_amount": 10000},
}


def _invoke(agent, labels, profile="JP"):
    ctx = InvocationContext(session_id="gate", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    out = agent.invoke(
        user_input=json.dumps(labels, ensure_ascii=False),
        ctx=ctx,
        input_context={"regulatory_profile": profile},
    )
    return (json.loads(out["output"]) if out.get("output") else None), out


def _loaded(rules: list) -> str:
    return json.dumps({"profile": "JP", "rules": rules}, ensure_ascii=False)


def _shipped_rules() -> list:
    import yaml

    return list(yaml.safe_load((_ROOT / "config" / "keihin_rules.yaml").read_text(encoding="utf-8"))["rules"])


def _state(labels: list, rules: list | None = None, **extra) -> dict:
    return {
        "loaded_rules": _loaded(_shipped_rules() if rules is None else rules),
        "labels_input": json.dumps(labels, ensure_ascii=False),
        **extra,
    }


def _errored(result: dict) -> bool:
    return result.get("status") in (AgentStatus.ERROR, AgentStatus.ERROR.value)


# ── what the graph layer was worth ───────────────────────────


class TestTheOldFormWasBypassable:
    def test_dropping_the_layer_fabricated_a_pass(self, build_agent):
        """The measurement the fix rests on, kept as a regression pin.

        The old topology is reconstructed by removing the determination from
        the post-process slot — the one-line edit the node form invited — and
        the same request is driven through the real graph. The gate is
        untouched: what changes is the data reaching it.
        """
        agent = build_agent()
        report, out = _invoke(agent, [BREACHING_LABEL])
        assert out["status"] not in (AgentStatus.ERROR, AgentStatus.ERROR.value)
        assert report["summary"]["fail"] == 1
        label = report["labels"][0]
        assert label["status"] == "FAIL"
        assert label["checks"]["regulatory"] == "FAIL"
        assert {"KH-001", "KH-004", "KH-005"} <= set(label["citations"])

        # Now drop the determination the way the graph layer could be dropped.
        stripped = build_agent()
        stripped_report, stripped_out = _invoke_without_determination(stripped, [BREACHING_LABEL])
        # It no longer ships a fabricated PASS: the boundary refuses.
        assert stripped_out["status"] in (AgentStatus.ERROR, AgentStatus.ERROR.value)
        assert stripped_report is not None and stripped_report["note"] == WITHHELD_NOTICE
        # Closed set: a reason code this module declared, and nothing else.
        assert stripped_report["reason"] in ERROR_REASONS
        assert set(stripped_report) == {"reason", "note"}


def _invoke_without_determination(agent, labels):
    """Drive the pipeline with the determination removed from the data path.

    ``evaluate_labels`` returning nothing is what a dropped graph layer looked
    like from the assembling node's point of view: no determination in state.
    Patched on the module the renderer calls — the gate itself is untouched.
    """
    import src.nodes.result_assemble as result_assemble

    original = result_assemble.evaluate_labels
    result_assemble.evaluate_labels = lambda *args, **kwargs: []
    try:
        return _invoke(agent, labels)
    finally:
        result_assemble.evaluate_labels = original


# ── the determination and the report are one delta ───────────


class TestOneDelta:
    def test_the_determination_ships_with_the_report(self):
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert out["status"] == AgentStatus.SUCCESS.value
        assert "verification_report" in out
        assert "regulatory_findings" in out
        assert json.loads(out["regulatory_findings"])[0]["status"] == "FAIL"

    def test_a_withheld_report_takes_the_determination_with_it(self):
        out = ResultAssembleNode().execute({"labels_input": json.dumps([BREACHING_LABEL])})
        assert _errored(out)
        assert "regulatory_findings" in out
        assert out["regulatory_findings"] is None

    def test_every_payload_this_node_returns_is_scanned(self):
        """Inventory guard on the credential layer's field list.

        The framework applies its own detector to EVERY value this node returns
        and raises on a hit, and that raise discards the whole delta — the
        containment with it. So a payload field the domain scan does not cover
        is not a narrower gate, it is a containment bypass. Pinning the exact
        set means a future payload field has to be added deliberately rather
        than quietly inheriting the unscanned default.
        """
        import src.nodes.result_assemble as result_assemble

        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        # One scan covers both: the caller-facing copy is byte-identical.
        assert out["formatted_output"] == out["verification_report"]
        payloads = {key for key, value in out.items() if isinstance(value, str)}
        assert payloads - {"status", "formatted_output"} == set(result_assemble._GATED_FIELDS)

    def test_a_credential_reaching_only_the_determination_is_blocked(self):
        """Forward-looking pin, and it is honest about being one.

        No rule detail reachable today puts caller text in the determination
        without also putting it in the rendered report, so this state shape is
        not one the current dispatch produces. It is pinned because the next
        rule that quotes an assessment on a PASS or NOT_APPLICABLE verdict would
        produce exactly it, and that detail would reach the framework's own scan
        through a field the domain gate had stopped covering.
        """
        import src.nodes.result_assemble as result_assemble

        violation = result_assemble._security_gate_output(
            {
                "verification_report": json.dumps({"summary": {}, "labels": [], "note": "n"}),
                "regulatory_findings": json.dumps([{"detail": "AKIAABCDEFGHIJKLMNOP"}]),
            },
            0,
        )
        assert violation is not None
        # The CREDENTIAL layer must be the one that blocks. Asserting only that
        # the field is named would also be satisfied by the determination
        # contract, which refuses this shape for an unrelated reason.
        assert violation.startswith("credential classes")
        assert "aws_key" in violation
        assert "regulatory_findings" in violation
        assert "AKIAABCDEFGHIJKLMNOP" not in violation


# ── layer 2, driven through the data path ────────────────────


class TestDeterminationContract:
    def test_a_missing_determination_is_refused(self, monkeypatch):
        import src.nodes.result_assemble as result_assemble

        monkeypatch.setattr(result_assemble, "evaluate_labels", lambda *a, **k: [])
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert _errored(out)
        assert "regulatory_findings" in " ".join(out["error_log"])

    def test_an_incomplete_determination_is_refused(self, monkeypatch):
        import src.nodes.result_assemble as result_assemble

        monkeypatch.setattr(
            result_assemble,
            "evaluate_labels",
            lambda rules, labels, promotion, tol: [{"label_id": label.get("label_id")} for label in labels],
        )
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert _errored(out)
        assert "regulatory_findings[0].status" in " ".join(out["error_log"])

    def test_an_unrecognised_verdict_is_refused(self, monkeypatch):
        import src.nodes.result_assemble as result_assemble

        monkeypatch.setattr(
            result_assemble,
            "evaluate_labels",
            lambda rules, labels, promotion, tol: [
                {"label_id": label.get("label_id"), "status": "OK", "rules": []} for label in labels
            ],
        )
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert _errored(out)
        assert "regulatory_findings[0].status" in " ".join(out["error_log"])

    def test_a_rule_entry_missing_its_verdict_is_refused(self, monkeypatch):
        import src.nodes.result_assemble as result_assemble

        monkeypatch.setattr(
            result_assemble,
            "evaluate_labels",
            lambda rules, labels, promotion, tol: [
                {
                    "label_id": label.get("label_id"),
                    "status": "PASS",
                    "rules": [{"rule_id": "KH-001", "name": "n", "detail": "d"}],
                }
                for label in labels
            ],
        )
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert _errored(out)
        assert "rules[0].status" in " ".join(out["error_log"])

    def test_a_report_that_contradicts_the_determination_is_refused(self, monkeypatch):
        """The renderer is mutated, not the determination.

        This is the shape a future regression takes: the determination is still
        computed and still correct, and the document the caller reads stops
        agreeing with it.
        """
        original = ResultAssembleNode._build_label_report

        def bland(self, label_id, price, promotion, regulatory):
            report = original(self, label_id, price, promotion, regulatory)
            report["checks"][determination.REGULATORY_CHECK] = "PASS"
            report["status"] = "PASS"
            return report

        monkeypatch.setattr(ResultAssembleNode, "_build_label_report", bland)
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert _errored(out)
        assert "checks.regulatory" in " ".join(out["error_log"])

    def test_a_dropped_citation_is_refused(self, monkeypatch):
        original = ResultAssembleNode._build_label_report

        def uncited(self, label_id, price, promotion, regulatory):
            report = original(self, label_id, price, promotion, regulatory)
            report["citations"] = []
            return report

        monkeypatch.setattr(ResultAssembleNode, "_build_label_report", uncited)
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        assert _errored(out)
        assert "citations" in " ".join(out["error_log"])

    def test_the_violation_names_a_location_and_no_value(self, monkeypatch):
        """A matched value copied into the result is refused by the framework's
        own final gate, and that refusal discards this node's whole delta —
        containment included. Locations only, therefore."""
        import src.nodes.result_assemble as result_assemble

        monkeypatch.setattr(result_assemble, "evaluate_labels", lambda *a, **k: [])
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL]))
        joined = " ".join(out["error_log"])
        assert "GATE-1" not in joined
        assert "1980" not in joined
        assert "二重価格表示禁止" not in joined


# ── the fail-open defaults, both closed ──────────────────────


class TestFailOpenDefaultsClosed:
    def test_an_unapplied_rule_set_is_review_not_pass(self):
        """`enable_keihin_check: false` publishes no rules.

        Measured on the shipped code, that produced an affirmative
        `"regulatory": "PASS"` on this label — from a configuration flag, with
        no source edit anywhere.
        """
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL], rules=[]))
        assert out["status"] == AgentStatus.SUCCESS.value
        label = json.loads(out["verification_report"])["labels"][0]
        assert label["checks"]["regulatory"] == "REVIEW"
        assert label["status"] == "REVIEW"
        assert any(determination.RULE_SET_NOT_APPLIED in line for line in label["remediation"])

    def test_an_unapplied_rule_set_end_to_end(self):
        from tests.conftest import StubChatClient, base_config

        from src.graph.graph import PriceLabelVerificationAgent

        agent = PriceLabelVerificationAgent(
            config=base_config(llm=StubChatClient(), enable_keihin_check=False),
            project_root=_ROOT,
        )
        report, out = _invoke(agent, [BREACHING_LABEL])
        assert report["summary"]["pass"] == 0
        assert report["labels"][0]["checks"]["regulatory"] == "REVIEW"

    def test_a_rule_that_cannot_be_evaluated_is_not_a_pass(self):
        rules = [
            {
                "id": "KH-004",
                "name": "景品上限額",
                "description": "d",
                "check_type": "prize_limit",
                "params": {"max_abs": float("nan"), "max_multiplier": 20},
            }
        ]
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL], rules=rules))
        label = json.loads(out["verification_report"])["labels"][0]
        assert label["checks"]["regulatory"] == "REVIEW"
        assert label["status"] == "REVIEW"
        assert any("KH-004" in line for line in label["remediation"])

    def test_an_unrecognised_check_type_is_not_a_pass(self):
        rules = [{"id": "X-9", "name": "n", "description": "d", "check_type": "telepathy"}]
        out = ResultAssembleNode().execute(_state([BREACHING_LABEL], rules=rules))
        label = json.loads(out["verification_report"])["labels"][0]
        assert label["checks"]["regulatory"] == "REVIEW"
        assert any(determination.UNRECOGNISED_CHECK in line for line in label["remediation"])

    def test_a_missing_determination_renders_not_run_never_an_absent_key(self):
        """Omission is what let the old form roll a missing determination into
        a PASS. The key is always present, so the gate has something to catch."""
        built = ResultAssembleNode()._build_label_report("L", None, None, None)
        assert built["checks"][determination.REGULATORY_CHECK] == NOT_RUN
        assert built["status"] == "REVIEW"


# ── the roll-up itself ───────────────────────────────────────


class TestRollUp:
    @pytest.mark.parametrize(
        "verdicts,expected",
        [
            (["PASS", "NOT_APPLICABLE"], "PASS"),
            (["PASS", "REVIEW"], "REVIEW"),
            (["REVIEW", "FAIL"], "FAIL"),
            (["FAIL", "PASS"], "FAIL"),
        ],
    )
    def test_review_is_carried_through_never_folded_into_pass(self, verdicts, expected):
        findings = determination.evaluate_labels(
            [{"id": f"R{i}", "name": "n", "check_type": "unknown"} for i, _ in enumerate(verdicts)],
            [{"label_id": "L"}],
            {},
            0.01,
        )
        # The dispatch above yields REVIEW for every rule; assert the property
        # directly on the roll-up so the table is not hostage to the dispatch.
        rolled = determination._roll_up([{"status": v} for v in verdicts])
        assert rolled == expected
        assert findings[0]["status"] == "REVIEW"


# ── clean-path controls ──────────────────────────────────────


class TestCleanPath:
    def test_a_compliant_label_still_passes(self, build_agent, clean_label):
        """A refuse-everything gate would pass every test above."""
        report, out = _invoke(build_agent(), [clean_label])
        assert out["status"] not in (AgentStatus.ERROR, AgentStatus.ERROR.value)
        assert report["summary"]["pass"] == 1
        assert report["labels"][0]["checks"]["regulatory"] == "PASS"

    def test_the_block_happens_at_the_output_slot(self, build_agent, monkeypatch):
        """The refusal is the boundary's, not an upstream node's."""
        import src.nodes.result_assemble as result_assemble

        monkeypatch.setattr(result_assemble, "evaluate_labels", lambda *a, **k: [])
        _, out = _invoke(build_agent(), [BREACHING_LABEL])
        assert out["status"] in (AgentStatus.ERROR, AgentStatus.ERROR.value)
        assert "PostProcessSlotNode" in out["node_history"]
