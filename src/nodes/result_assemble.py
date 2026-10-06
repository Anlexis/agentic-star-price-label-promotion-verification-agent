"""ResultAssemble node — the whole post-process slot, and the output boundary.

Three jobs, in this order.

**Determine.** Evaluate the loaded regulatory rule set against every label (see
``src/nodes/regulatory_determination``). This used to be a separate graph layer
ahead of this node; that form is prohibited and it was bypassable in fact —
with the layer dropped, a label carrying an unsupported reference price and an
over-ceiling prize came back ``status: success`` and a report reading PASS. The
determination is produced here and returned in the SAME delta as the report, so
a report and its determination cannot exist apart.

**Assemble.** Build the per-label, per-check report from the three finding sets
— price consistency, promotion claim, regulatory determination — adding the rule
citation and a remediation suggestion for each failure, and for each rule the
agent could not evaluate.

**Gate the output.** Nothing leaves this node unscanned, and
``_security_gate_output`` is the single choke point over both layers.

*Layer 1 — credentials.* The scan uses the framework's own credential detector
rather than a local pattern list, and that choice is load-bearing rather than
stylistic: the framework applies the same detector to every value this node
returns, and it raises when it finds one. A local list narrower than the
framework's would let a value pass this gate, trip the framework inside the same
call, and have the framework's error path discard the withheld-output
substitution made here — so a detector gap is not a missed finding, it is a
containment bypass. Calling the framework's detector makes the two sets
identical by construction. Both fields this node returns to the caller-visible
surface are scanned, not only the rendered report: the determination is returned
in the same delta, and a rule detail quotes the promotion assessment.

*Layer 2 — the regulatory determination contract.* Refuse to release a report
that carries no determination of record, whose determination is incomplete, or
whose rendered verdict contradicts it. ``post_process`` is a fixed backbone slot
that the base graph routes every successful invocation through, so a check
placed here cannot be routed around the way a graph layer could.

On a violation the node returns an error status and replaces the assembled
report with the closed-set payload built by ``_contain()``. That payload is the
whole of what a non-success outcome publishes: a constant ``reason`` code drawn
from ``ERROR_REASONS`` and the constant withheld notice, and nothing else. It
carries no count, no verdict, and nothing read out of state — not ``error_log``,
not the gate's own violation string. Node-authored error text can embed an
upstream response body, an identifier or a caller fragment, and truncating or
redacting such a string is not a closed set; the only way to bound the channel
is to publish values this module chose.

``error_log`` stays the INTERNAL channel. The state reducer appends to it and
the audit trail needs it; it is simply never projected to the caller (see
``get_output`` in ``src/graph/graph.py``). The violation LOCATION — a credential
CLASS, or a field path — is written there and to nothing else. The matched text
is never echoed, in the report or in the log, where it would outlive the request
that was refused.

The domain gate is a module-level function, not an instance method on the node
class: the framework marks its own gate methods final and rejects a same-named
override at class-definition time, and raising from the extension hook instead
would discard this node's whole delta — including the containment below.

**Numeric fidelity.** This report is read to decide whether a displayed price is
wrong, so every amount it carries is the amount that was submitted, rendered
exactly. There is no rounding grid and there must not be one: rounding a price
before comparing it to another price would destroy the very discrepancy the
report exists to surface. The invariant enforced here is the inverse of a
rounding grid — amounts pass through unchanged — and it is pinned by test.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Dict, Mapping, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.nodes.regulatory_determination import (
    REGULATORY_CHECK,
    citations,
    determination_violation,
    evaluate_labels,
    findings_by_label,
    remediation_lines,
)
from src.services.validation import finite_in_range

_DEFAULT_NOTE = "for internal reporting purposes"

WITHHELD_NOTICE = "Report withheld: the output gate refused the assembled output and it was not released."

# Reason codes — with WITHHELD_NOTICE, the ONLY values the caller-visible
# non-success payload may carry. Chosen here, never derived from state, so the
# payload says WHAT happened and never in whose words.
_REASON_RULES_UNAVAILABLE = "regulatory_rules_unavailable"  # no rule set to ground the report
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the report
ERROR_REASONS = frozenset({_REASON_RULES_UNAVAILABLE, _REASON_OUTPUT_WITHHELD})

# The exact key set of a withheld payload. Asserted by test so a future field
# carrying answer text cannot quietly join the withheld response.
WITHHELD_KEYS = frozenset({"reason", "note"})

# Fields this node returns that carry report content or a structured payload.
# BOTH are scanned: the determination is returned in the same delta as the
# report, and a rule detail quotes the promotion assessment — so a credential in
# the model's own text reaches the framework's scan through the determination
# even when the rendered report is clean.
_GATED_FIELDS: Tuple[str, ...] = ("verification_report", "regulatory_findings")

# Rendered when the determination for a label is missing entirely. The gate
# refuses such a report; the placeholder exists so the disagreement is visible
# in the artefact rather than expressed as an absent key, which is what let the
# previous form roll a missing determination up into a PASS.
NOT_RUN = "NOT_RUN"


def _finding_classes(text: str) -> list[str]:
    """Return the CLASS of each credential finding — never the matched text."""
    return sorted({finding["type"] for finding in detect_credentials(text)})


def _contain(reason: str, new_errors: Optional[list[str]] = None) -> dict[str, Any]:
    """The node result for ANY non-success outcome — the single error shape.

    Error status, every output-bearing field replaced, and a payload made of
    closed-set labels only: ``reason`` is one of :data:`ERROR_REASONS` and
    ``note`` is :data:`WITHHELD_NOTICE`. Nothing is read out of state — not the
    determination, not ``error_log``, not the gate's violation string.

    ``new_errors`` are appended to ``error_log``, the internal channel the state
    reducer accumulates, and never enter the payload. The reducer appends, so an
    entry written by an earlier node is already there and is deliberately not
    re-emitted here.

    Every output-bearing field is overwritten rather than merely omitted:
    partial deltas are merged, so an omitted key leaves the previous value in
    state, and an absent or empty field falls through to whatever the
    surrounding machinery finds next — which on the framework's own output
    resolution is the un-gated draft. The payload is deliberately TRUTHY for the
    same reason: ``AgentBaseGraph.get_output()`` resolves ``formatted_output or
    result`` with no status check, so a falsy replacement re-opens the very
    fallback the replacement exists to close. A constant ``reason`` key
    guarantees truthiness.

    No count travels in the payload. The size of the refused batch is an outcome
    signal and belongs in the audit event, not in the caller's error.
    """
    withheld = json.dumps(
        {"reason": reason, "note": WITHHELD_NOTICE},
        sort_keys=True,
        ensure_ascii=False,
    )
    contained: dict[str, Any] = {
        "status": AgentStatus.ERROR.value,
        "blocked": True,
        "verification_report": withheld,
        "formatted_output": withheld,
        # Cleared by PRESENCE, not by omission: a partial delta that simply
        # leaves this key out keeps whatever value state already held.
        "regulatory_findings": None,
    }
    if new_errors:
        contained["error_log"] = list(new_errors)  # internal channel, not the caller's
    return contained


def _security_gate_output(fields: Mapping[str, Any], label_count: int) -> Optional[str]:
    """THE output gate for this template — the single choke point on the report.

    ``fields`` is the caller-reachable view of what this node is about to
    return. Every key it reads is written by this node in the same delta, so no
    layer here compares against a key absent from its own scope — a layer that
    reads a key nothing writes at its level compares against nothing on every
    real invocation and is dead while its tests stay green.

    Returns the first violation as a short LOCATION string — a credential class
    or a field path, never a matched value. Echoing a value would put it back
    into this node's own result, where the framework's final output gate raises,
    and a raise discards the whole delta including the containment. Returns None
    when the report may ship.
    """
    # ── Layer 1: credentials ─────────────────────────────────
    for field in _GATED_FIELDS:
        content = fields.get(field)
        if not isinstance(content, str) or not content:
            continue
        classes = _finding_classes(content)
        if classes:
            return f"credential classes {', '.join(classes)} in {field}"

    # ── Layer 2: the regulatory determination contract ───────
    report = fields.get("verification_report")
    try:
        rendered_labels = json.loads(report).get("labels") if isinstance(report, str) else None
    except json.JSONDecodeError:
        return "verification_report"
    location = determination_violation(label_count, fields.get("regulatory_findings"), rendered_labels)
    if location:
        return f"regulatory determination contract violated at {location}"
    return None


class ResultAssembleNode(FunctionNode):
    """Determines regulatory compliance, assembles the report, applies the output gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, note: str = _DEFAULT_NOTE, discount_rate_tolerance: float = 0.01) -> None:
        if not note or not note.strip():
            raise ValueError("note must be a non-empty string")
        self._note = note
        # Parsed at construction: a non-finite tolerance compares False against
        # every difference and would turn the discount-accuracy rule into a pass
        # for every label it touched.
        self.discount_rate_tolerance = finite_in_range(
            discount_rate_tolerance,
            field="price.discount_rate_tolerance",
            minimum=0.0,
            maximum=1.0,
        )

    def execute(self, state: Mapping[str, Any], config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        labels = json.loads(state.get("labels_input") or "[]")
        price = self._by_id(state.get("price_findings"))
        promotion = self._by_id(state.get("promotion_findings"))

        # ── Determine ────────────────────────────────────────
        # Fail closed. A report the boundary cannot ground in a rule set carries
        # no determination, and the previous form rendered exactly that case as
        # a report with the regulatory key simply absent — which rolled up to
        # PASS.
        rules = self._rules(state.get("loaded_rules"))
        if rules is None:
            # Outcome signals only — a closed-set reason code and a count. The
            # audit log is not a store for message content.
            emit_trace_event(
                "result_assemble_withheld",
                {"reason": _REASON_RULES_UNAVAILABLE, "total": len(labels)},
                state,
            )
            return _contain(
                _REASON_RULES_UNAVAILABLE,
                ["ResultAssembleNode: loaded_rules is absent or unreadable"],
            )
        determinations = evaluate_labels(rules, labels, promotion, self.discount_rate_tolerance)
        regulatory = findings_by_label(determinations)

        # ── Assemble ─────────────────────────────────────────
        label_reports = [
            self._build_label_report(
                label.get("label_id"),
                price.get(label.get("label_id")),
                promotion.get(label.get("label_id")),
                regulatory.get(label.get("label_id")),
            )
            for label in labels
        ]

        summary = {
            "total": len(label_reports),
            "pass": sum(1 for report in label_reports if report["status"] == "PASS"),
            "fail": sum(1 for report in label_reports if report["status"] == "FAIL"),
            "review": sum(1 for report in label_reports if report["status"] == "REVIEW"),
        }
        rendered = json.dumps(
            {"summary": summary, "labels": label_reports, "note": self._note},
            sort_keys=True,
            ensure_ascii=False,
        )
        determination = json.dumps(determinations, sort_keys=True, ensure_ascii=False)

        # ── Gate ─────────────────────────────────────────────
        violation = _security_gate_output(
            {"verification_report": rendered, "regulatory_findings": determination},
            len(labels),
        )
        if violation:
            # The violation names a LOCATION, not a value — and it travels in
            # error_log only. The caller receives the reason code.
            emit_trace_event(
                "result_assemble_withheld",
                {"reason": _REASON_OUTPUT_WITHHELD, "total": len(labels)},
                state,
            )
            return _contain(
                _REASON_OUTPUT_WITHHELD,
                [f"ResultAssembleNode: output gate refused the report — {violation}"],
            )

        emit_trace_event(
            "result_assemble",
            {
                "total": summary["total"],
                "pass": summary["pass"],
                "fail": summary["fail"],
                "review": summary["review"],
                "rules_applied": len(rules),
                "withheld": False,
            },
            state,
        )
        return {
            "status": AgentStatus.SUCCESS.value,
            "blocked": False,
            "verification_report": rendered,
            "formatted_output": rendered,
            "regulatory_findings": determination,
        }

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _rules(raw: Any) -> Optional[list[dict[str, Any]]]:
        """The rule set published upstream, or None when there is none to read."""
        if not raw or not isinstance(raw, str):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, Mapping):
            return None
        rules = payload.get("rules")
        if not isinstance(rules, list):
            return None
        return [dict(rule) for rule in rules if isinstance(rule, Mapping)]

    @staticmethod
    def _by_id(raw: Any) -> dict[Any, Any]:
        if not raw:
            return {}
        return {finding.get("label_id"): finding for finding in json.loads(raw)}

    def _build_label_report(
        self,
        label_id: Any,
        price: Optional[Mapping[str, Any]],
        promotion: Optional[Mapping[str, Any]],
        regulatory: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        cited: list[str] = []
        remediation: list[str] = []

        if price:
            checks["price_consistency"] = price.get("status", "NOT_RUN")
            if price.get("status") == "FAIL":
                for discrepancy in price.get("discrepancies", []):
                    cited.append("PRICE")
                    remediation.append(self._price_remediation(discrepancy))

        if promotion:
            checks["promotion_claim"] = promotion.get("status", "NOT_RUN")
            if promotion.get("status") == "FAIL":
                remediation.append(
                    "Revise promotional claim: "
                    f"{promotion.get('assessment') or 'claim inaccurate or not defensible'}"
                )

        # Unconditional: a missing determination is rendered as NOT_RUN rather
        # than omitted, so the disagreement is in the artefact for the gate to
        # find. Omission is what previously let it roll up to PASS.
        regulatory_verdict = regulatory.get("status", NOT_RUN) if regulatory else NOT_RUN
        checks[REGULATORY_CHECK] = regulatory_verdict
        if regulatory:
            cited.extend(citations(regulatory))
            remediation.extend(remediation_lines(regulatory))

        return {
            "label_id": label_id,
            "status": self._overall_status(price, promotion, regulatory_verdict),
            "checks": checks,
            "citations": sorted({citation for citation in cited if citation}),
            "remediation": remediation,
        }

    @staticmethod
    def _overall_status(
        price: Optional[Mapping[str, Any]],
        promotion: Optional[Mapping[str, Any]],
        regulatory_verdict: str,
    ) -> str:
        statuses: list[Any] = [finding.get("status") for finding in (price, promotion) if finding]
        statuses.append(regulatory_verdict)
        if "FAIL" in statuses:
            return "FAIL"
        if "REVIEW" in statuses or NOT_RUN in statuses:
            return "REVIEW"
        return "PASS"

    @staticmethod
    def _price_remediation(discrepancy: Mapping[str, Any]) -> str:
        kind = discrepancy.get("type")
        if kind == "selling_price_mismatch":
            return (
                f"Align prices across {discrepancy.get('fields', [])} " f"(currently {discrepancy.get('values', [])})"
            )
        if kind == "discount_implied_mismatch":
            return (
                f"Sale price ({discrepancy.get('actual')}) does not match "
                f"regular x (1 - discount) = {discrepancy.get('expected_sale')}; "
                "correct the label or the discount rate"
            )
        return "Resolve price discrepancy"


__all__ = ["ERROR_REASONS", "NOT_RUN", "WITHHELD_KEYS", "WITHHELD_NOTICE", "ResultAssembleNode"]
