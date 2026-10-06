"""Domain test cases TC-01..TC-09. See docs/03_test_spec.md."""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.nodes.input_parse import InputParseNode

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _report(agent, labels, profile="JP"):
    ctx = InvocationContext(session_id="tc", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    out = agent.invoke(
        user_input=json.dumps(labels, ensure_ascii=False),
        ctx=ctx,
        input_context={"regulatory_profile": profile},
    )
    return json.loads(out["output"])


def _label_report(report, label_id):
    return next(r for r in report["labels"] if r["label_id"] == label_id)


# ── TC-01 — state contract ───────────────────────────────────


class TestTC01StateContract:
    def test_state_extends_the_framework_state(self):
        tree = ast.parse((_ROOT / "src" / "schemas" / "state.py").read_text())
        assert any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("framework.schemas.agent_state")
            and "AgentState" in [alias.name for alias in node.names]
            for node in ast.walk(tree)
        )

    def test_does_not_shadow_the_framework_state_class(self):
        tree = ast.parse((_ROOT / "src" / "schemas" / "state.py").read_text())
        classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "AgentState" not in classes
        assert "State" in classes


# ── TC-02 — a clean label passes ─────────────────────────────


class TestTC02ValidBatch:
    def test_clean_label_passes(self, build_agent, clean_label):
        report = _report(build_agent(accurate=True, defensible=True), [clean_label])
        assert report["summary"]["total"] == 1
        assert report["summary"]["pass"] == 1
        label = _label_report(report, "AEON-1")
        assert label["status"] == "PASS"
        assert label["checks"]["price_consistency"] == "PASS"
        assert label["checks"]["regulatory"] == "PASS"


# ── TC-03 — price discrepancy ────────────────────────────────


class TestTC03PriceDiscrepancy:
    def test_point_of_sale_against_web_mismatch_fails(self, build_agent):
        label = {"label_id": "D1", "pos_price": 980, "web_price": 1080}
        result = _label_report(_report(build_agent(), [label]), "D1")
        assert result["status"] == "FAIL"
        assert result["checks"]["price_consistency"] == "FAIL"

    def test_prices_are_reported_exactly_never_rounded(self, build_agent):
        """The report carries submitted amounts unchanged.

        There is no rounding grid on this output and there must not be one:
        the report exists to show that two displayed prices differ, and an
        amount rounded before it is rendered can no longer demonstrate the
        difference it was rendered to demonstrate. Amounts chosen to be
        corrupted by any of the common grids — a five-digit run, a
        comma-grouped value, a decimal fraction, a small value beside a
        three-letter code — must appear byte-identically.
        """
        label = {
            "label_id": "EXACT-1",
            "pos_price": 129999,
            "web_price": 8.512345,
            "regular_price": 1234.56,
            "sale_price": 9999,
        }
        rendered = json.dumps(_report(build_agent(), [label]), ensure_ascii=False)
        for amount in ("129999", "8.512345", "1234.56", "9999"):
            assert amount in rendered, f"{amount} was altered on the way to the report"


# ── TC-04 — an unsubstantiated superlative fails ─────────────


class TestTC04ClaimSubstantiation:
    def test_unsubstantiated_superlative_is_cited(self, build_agent):
        label = {
            "label_id": "C1",
            "pos_price": 500,
            "web_price": 500,
            "promotion_spec": {"claim": "業界最安値"},
        }
        result = _label_report(_report(build_agent(accurate=True, defensible=False), [label]), "C1")
        assert result["status"] == "FAIL"
        assert "KH-002" in result["citations"]


# ── TC-05 — prize ceiling ────────────────────────────────────


class TestTC05PrizeLimit:
    def test_prize_above_the_absolute_cap_is_cited(self, build_agent):
        label = {
            "label_id": "P1",
            "pos_price": 500,
            "promotion_spec": {"prize_value": 100001, "transaction_amount": 100000},
        }
        result = _label_report(_report(build_agent(), [label]), "P1")
        assert result["status"] == "FAIL"
        assert "KH-004" in result["citations"]


# ── TC-06 — discount-rate mismatch ───────────────────────────


class TestTC06DiscountMismatch:
    def test_claimed_rate_disagreeing_with_prices_is_cited(self, build_agent):
        label = {
            "label_id": "M1",
            "pos_price": 980,
            "web_price": 980,
            "sale_price": 980,
            "regular_price": 1400,
            "promotion_spec": {"claim": "最大50%オフ", "discount_rate": 0.50},
        }
        result = _label_report(_report(build_agent(), [label]), "M1")
        assert "KH-003" in result["citations"]
        assert result["status"] == "FAIL"


# ── TC-07 — mixed batch ──────────────────────────────────────


class TestTC07Batch:
    def test_mixed_batch(self, build_agent):
        labels = [
            {"label_id": "OK", "pos_price": 500, "web_price": 500},
            {"label_id": "BAD", "pos_price": 500, "web_price": 700},
        ]
        report = _report(build_agent(), labels)
        assert report["summary"]["total"] == 2
        assert _label_report(report, "OK")["status"] == "PASS"
        assert _label_report(report, "BAD")["status"] == "FAIL"


# ── TC-08 — Japanese text ────────────────────────────────────


class TestTC08JapaneseText:
    def test_rule_names_and_details_keep_their_characters(self, build_agent):
        """Regulatory text renders in the report exactly as the rule library states it."""
        label = {"label_id": "JP-1", "regular_price": 1000, "sale_price": 1200}
        rendered = json.dumps(_report(build_agent(), [label]), ensure_ascii=False)
        assert "二重価格表示禁止" in rendered

    def test_the_assessment_keeps_its_characters(self, build_agent):
        label = {
            "label_id": "JP-2",
            "pos_price": 500,
            "web_price": 500,
            "promotion_spec": {"claim": "業界最安値"},
        }
        agent = build_agent(accurate=False, defensible=False, assessment="根拠が示されていません")
        rendered = json.dumps(_report(agent, [label]), ensure_ascii=False)
        assert "根拠が示されていません" in rendered

    def test_shelf_text_keeps_its_characters(self):
        node = InputParseNode()
        out = node.execute(
            {
                "labels_input": json.dumps(
                    [{"label_id": "L", "pos_price": 1, "shelf_label_text": "最大30%オフ"}],
                    ensure_ascii=False,
                )
            }
        )
        assert "最大30%オフ" in out["labels_input"]

    def test_the_claim_reaches_the_model_unchanged(self, build_agent):
        agent = build_agent()
        label = {
            "label_id": "JP-3",
            "pos_price": 980,
            "regular_price": 1400,
            "sale_price": 980,
            "promotion_spec": {"claim": "最大30%オフ", "discount_rate": 0.30},
        }
        _report(agent, [label])
        assert "最大30%オフ" in agent._promotion._llm.last_prompt


# ── TC-09 — construction and configuration ───────────────────


class TestTC09Construction:
    def test_backbone_slots_are_registered(self, build_agent):
        agent = build_agent()
        agent.register_nodes()
        assert set(agent._nodes) == {
            "initialize",
            "pre_process",
            "main",
            "post_process",
            "finalize",
        }

    def test_invoke_returns_a_report(self, build_agent, clean_label):
        report = _report(build_agent(), [clean_label])
        assert "summary" in report and "labels" in report

    def test_disabling_the_output_gate_is_refused(self):
        from tests.conftest import base_config
        from src.graph.graph import PriceLabelVerificationAgent

        with pytest.raises(ValueError, match="output_gate_enabled must be true"):
            PriceLabelVerificationAgent(
                config=base_config(security={"output_gate_enabled": False}),
                project_root=_ROOT,
            )

    def test_declared_runtime_values_reach_the_nodes(self, build_agent):
        """A value declared in config/config.yaml is live inside the graph.

        Read end to end rather than asserted on the file: the failure this
        guards against is a reader pointed at a file that no longer holds the
        key, which leaves the declared value inert while every test that reads
        the file directly still passes.
        """
        agent = build_agent()
        assert agent._price.price_tolerance_abs == 1.0
        assert agent._price.price_tolerance_pct == 0.001
        assert agent._result.discount_rate_tolerance == 0.01
        assert agent._default_profile == "JP"

    def test_a_changed_runtime_value_changes_the_verdict(self):
        """The declared tolerance decides the outcome, not a hard-coded default."""
        from tests.conftest import StubChatClient, base_config
        from src.graph.graph import PriceLabelVerificationAgent

        labels = [{"label_id": "T1", "pos_price": 1000, "web_price": 1200}]
        strict = PriceLabelVerificationAgent(config=base_config(llm=StubChatClient()), project_root=_ROOT)
        relaxed = PriceLabelVerificationAgent(
            config=base_config(
                llm=StubChatClient(),
                price={"tolerance_abs": 500.0, "tolerance_pct": 0.5, "discount_rate_tolerance": 0.01},
            ),
            project_root=_ROOT,
        )
        assert _label_report(_report(strict, labels), "T1")["status"] == "FAIL"
        assert _label_report(_report(relaxed, labels), "T1")["status"] == "PASS"

    def test_an_unimplemented_profile_is_refused(self, build_agent):
        ctx = InvocationContext(session_id="tc", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        out = build_agent().invoke(
            user_input=json.dumps([{"label_id": "L", "pos_price": 1}]),
            ctx=ctx,
            input_context={"regulatory_profile": "EU"},
        )
        assert out["status"] in ("error", "ERROR")
        assert not out.get("output")
