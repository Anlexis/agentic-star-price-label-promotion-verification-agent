"""The regulatory determination: the rule set, and the output-boundary contract.

WHY THIS IS A MODULE AND NOT A NODE
-----------------------------------
This logic used to be a registered graph layer (``RegulatoryComplianceCheck``),
the first of two nodes in the post-process slot. That FORM is prohibited: the
mandatory output-security mechanism on this template is
``_security_gate_output()``, and a separate compliance-check node standing
beside it is an anti-pattern under the platform's mandatory architecture rules.

The objection is not cosmetic, because a node is bypassable — any path that
reaches the output without traversing it ships un-determined content. Measured
on the shipped pipeline before this change, with that one layer dropped from the
slot and nothing else altered, a label whose advertised reference price did not
exceed the price actually charged (二重価格表示) and whose prize draw was five
times the statutory ceiling came back as::

    status: success
    summary: {"total": 1, "pass": 1, "fail": 0, "review": 0}
    checks:  {"price_consistency": "PASS", "promotion_claim": "PASS"}

— the same request that, with the layer present, returned FAIL and cited
KH-001 / KH-004 / KH-005. Nothing downstream required the determination: the
assembling step read it with ``if regulatory:`` and simply omitted the key when
it was absent, so the label's overall verdict rolled up from the two remaining
checks and read PASS. The reader was not told the regulatory rules had not run.

So the determination now happens where the report is ASSEMBLED — ResultAssemble
derives it and returns it in the SAME delta as ``verification_report``, so a
report and its determination cannot exist apart — and it is ENFORCED at the
output boundary by that node's ``_security_gate_output()``. ``post_process`` is
a fixed backbone slot that the base graph's ``route()`` sends every SUCCESS
through, so a check placed there cannot be routed around the way a graph layer
could.

The rules themselves are unchanged; only their placement moved. Rules stay data,
not code: each declares a ``check_type`` and dispatch happens on it, so amending
the regulations means editing the rule library rather than this module.

``price_reference``
    The advertised reference price must genuinely exceed the sale price.
``claim_substantiation``
    Reuses the assessment already produced for the claim rather than asking a
    model the same question twice.
``discount_accuracy``
    Deterministic: the claimed rate against the rate the prices imply.
``prize_limit``
    Prize-value ceilings, driven entirely by the rule's own parameters.

Every rule parameter is parsed through the finite, bounded parser before it is
compared against. A non-finite ceiling compares False against any prize value,
so an unchecked one would turn a limit into a pass for every label it touched.

TWO FAIL-OPEN DEFAULTS CLOSED HERE
----------------------------------
Both were measured on the shipped code, both produced an affirmative
``"regulatory": "PASS"`` on a label that violates the rules, and neither needed
a source edit to reach:

1. **An empty rule set.** ``enable_keihin_check: false`` publishes zero rules.
   Every label then satisfied "no rule failed" and was rolled up as PASS. An
   unapplied rule set is now REVIEW, and says so in the report.
2. **A rule that could not be evaluated.** A non-finite ``max_abs`` produced a
   REVIEW verdict on the rule — and the roll-up, ``FAIL if any FAIL else PASS``,
   converted it to PASS. The rule-level REVIEW never reached the reader. The
   roll-up now carries REVIEW through, and unevaluable rules are rendered.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeGuard

from src.services.validation import CallerInputError, finite_in_range

_MAX_AMOUNT = 1e12

# The report key this determination owns. ONE source of truth shared by the
# renderer and the output gate: if the gate kept its own copy of this string it
# would drift from the renderer and quietly become a dead layer comparing
# against a key nothing writes.
REGULATORY_CHECK = "regulatory"

# Keys every finding of record must carry. The output gate refuses a report
# whose determination is missing any of them, so a future change that drops one
# fails closed instead of shipping a partial determination.
REQUIRED_FINDING_KEYS: Tuple[str, ...] = ("label_id", "status", "rules")
REQUIRED_RULE_KEYS: Tuple[str, ...] = ("rule_id", "name", "status", "detail")

# A finding rolls up to exactly one of these. NOT_APPLICABLE is a per-rule
# verdict only — a label is never "not applicable" as a whole.
FINDING_VERDICTS = frozenset({"PASS", "FAIL", "REVIEW"})
RULE_VERDICTS = frozenset({"PASS", "FAIL", "REVIEW", "NOT_APPLICABLE"})

# Vocabulary for the two cases where no rule could decide anything.
RULE_SET_ID = "RULE-SET"
RULE_SET_NAME = "規制ルールセット"
RULE_SET_NOT_APPLIED = "the regulatory rule set was not applied to this label; no rule was evaluated"
UNRECOGNISED_CHECK = "unrecognised check type"

# The detail rendered when a rule's own parameters would not parse. A rule
# detail is CALLER-VISIBLE — the renderer puts it in the report's remediation
# lines — so it carries this constant rather than the parse error's message.
# That message is composed here, but it is still exception text: a future
# parameter parsed from a source outside this project would put that source's
# words into a caller's report, and by then the leak reads as ordinary code.
# The entry already names the rule, which is the part an operator acts on.
RULE_PARAMS_UNEVALUABLE = "rule parameters could not be evaluated"


def _is_number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ── individual checks ────────────────────────────────────────


def _check_price_reference(label: Mapping[str, Any]) -> tuple[str, str]:
    regular = label.get("regular_price")
    sale = label.get("sale_price")
    if not (_is_number(regular) and _is_number(sale)):
        return "NOT_APPLICABLE", "no regular/sale price pair"
    if regular <= sale:
        return (
            "FAIL",
            f"regular_price ({regular}) is not above sale_price ({sale}); "
            "the reference price does not support the discount shown",
        )
    return "PASS", "reference price is genuinely above the sale price"


def _check_discount_accuracy(label: Mapping[str, Any], tolerance: float) -> tuple[str, str]:
    spec = label.get("promotion_spec") or {}
    claimed = spec.get("discount_rate") if isinstance(spec, Mapping) else None
    regular = label.get("regular_price")
    sale = label.get("sale_price")
    if not _is_number(claimed):
        return "NOT_APPLICABLE", "no claimed discount rate"
    if not (_is_number(regular) and _is_number(sale) and regular > 0):
        return "NOT_APPLICABLE", "the actual rate cannot be computed from these prices"
    computed = 1.0 - (sale / regular)
    if abs(claimed - computed) > tolerance:
        return (
            "FAIL",
            f"claimed {claimed:.3f} against computed {computed:.3f} (tolerance {tolerance})",
        )
    return "PASS", f"claimed {claimed:.3f} agrees with computed {computed:.3f}"


def _check_claim_substantiation(promotion: Optional[Mapping[str, Any]]) -> tuple[str, str]:
    if not promotion:
        return "NOT_APPLICABLE", "no promotion finding for this label"
    status = promotion.get("status")
    if status == "SKIP":
        return "NOT_APPLICABLE", "no promotion claim"
    if status == "REVIEW":
        return "REVIEW", "the promotion claim could not be assessed"
    if promotion.get("defensible") is False:
        return "FAIL", f"claim is not defensible: {promotion.get('assessment', '')}"
    return "PASS", "claim is defensible per the promotion assessment"


def _check_prize_limit(params: Mapping[str, Any], label: Mapping[str, Any]) -> tuple[str, str]:
    spec = label.get("promotion_spec") or {}
    if not isinstance(spec, Mapping):
        return "NOT_APPLICABLE", "no promotion spec"
    prize = spec.get("prize_value")
    if not _is_number(prize):
        return "NOT_APPLICABLE", "no prize value on this label"
    transaction = spec.get("transaction_amount")

    if params.get("open_prize_unlimited"):
        return "PASS", "open prize draw — no value ceiling applies"

    if "threshold" in params:
        if not _is_number(transaction):
            return "NOT_APPLICABLE", "a transaction amount is needed for this ceiling"
        threshold = finite_in_range(
            params["threshold"], field="rule.params.threshold", minimum=0.0, maximum=_MAX_AMOUNT
        )
        if transaction < threshold:
            limit = finite_in_range(
                params["below_max"], field="rule.params.below_max", minimum=0.0, maximum=_MAX_AMOUNT
            )
        else:
            limit = transaction * finite_in_range(
                params["above_pct"], field="rule.params.above_pct", minimum=0.0, maximum=1.0
            )
        if prize > limit:
            return "FAIL", f"prize {prize} exceeds the ceiling of {limit}"
        return "PASS", f"prize {prize} is within the ceiling of {limit}"

    if "max_abs" in params:
        max_abs = finite_in_range(params["max_abs"], field="rule.params.max_abs", minimum=0.0, maximum=_MAX_AMOUNT)
        if prize > max_abs:
            return "FAIL", f"prize {prize} exceeds the absolute cap of {max_abs}"
        if "max_multiplier" in params and _is_number(transaction):
            multiplier = finite_in_range(
                params["max_multiplier"],
                field="rule.params.max_multiplier",
                minimum=0.0,
                maximum=_MAX_AMOUNT,
            )
            if prize > transaction * multiplier:
                return "FAIL", f"prize {prize} exceeds {multiplier} times the transaction ({transaction})"
        return "PASS", f"prize {prize} is within the prize-draw limits"

    return "REVIEW", "this rule declares no recognised parameters"


# ── dispatch and roll-up ─────────────────────────────────────


def _eval_rule(
    rule: Mapping[str, Any],
    label: Mapping[str, Any],
    promotion: Optional[Mapping[str, Any]],
    tolerance: float,
) -> dict[str, Any]:
    check_type = rule.get("check_type")
    try:
        if check_type == "price_reference":
            status, detail = _check_price_reference(label)
        elif check_type == "discount_accuracy":
            status, detail = _check_discount_accuracy(label, tolerance)
        elif check_type == "claim_substantiation":
            status, detail = _check_claim_substantiation(promotion)
        elif check_type == "prize_limit":
            status, detail = _check_prize_limit(rule.get("params") or {}, label)
        else:
            status, detail = "REVIEW", UNRECOGNISED_CHECK
    except CallerInputError:
        status, detail = "REVIEW", RULE_PARAMS_UNEVALUABLE

    return {
        "rule_id": rule.get("id"),
        "name": rule.get("name"),
        "status": status,
        "detail": detail,
    }


def _roll_up(rule_results: Sequence[Mapping[str, Any]]) -> str:
    """One label's verdict from its rule verdicts.

    REVIEW is carried through rather than folded into PASS. The previous
    ``FAIL if any FAIL else PASS`` turned every rule that could not be evaluated
    into an affirmative pass, which is the one verdict an unevaluable rule must
    never produce — and the rule-level REVIEW was not rendered anywhere, so the
    reader had no way to notice.
    """
    statuses = {result.get("status") for result in rule_results}
    if "FAIL" in statuses:
        return "FAIL"
    if "REVIEW" in statuses:
        return "REVIEW"
    return "PASS"


def _unapplied_rule_set() -> list[dict[str, Any]]:
    """The rule entry emitted when no rule was applied at all.

    Carrying it as an ordinary rule entry means the renderer needs no special
    case: it is rendered, and it rolls up to REVIEW like any other unevaluable
    rule.
    """
    return [
        {
            "rule_id": RULE_SET_ID,
            "name": RULE_SET_NAME,
            "status": "REVIEW",
            "detail": RULE_SET_NOT_APPLIED,
        }
    ]


def evaluate_labels(
    rules: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    promotion_by_id: Mapping[Any, Mapping[str, Any]],
    tolerance: float,
) -> list[dict[str, Any]]:
    """Evaluate the rule set against every label. One finding per label, in order."""
    findings: list[dict[str, Any]] = []
    for label in labels:
        label_id = label.get("label_id")
        if rules:
            rule_results = [_eval_rule(rule, label, promotion_by_id.get(label_id), tolerance) for rule in rules]
        else:
            rule_results = _unapplied_rule_set()
        findings.append(
            {
                "label_id": label_id,
                "status": _roll_up(rule_results),
                "rules": rule_results,
            }
        )
    return findings


# ── report vocabulary shared with the renderer ───────────────


def citations(finding: Mapping[str, Any]) -> list[str]:
    """Rule identifiers to cite — failures only."""
    return [rule.get("rule_id") for rule in finding.get("rules", []) if rule.get("status") == "FAIL"]


def remediation_lines(finding: Mapping[str, Any]) -> list[str]:
    """One line per rule the reader has to act on.

    REVIEW is included as well as FAIL. A rule the agent could not evaluate is
    something the reader must resolve themselves, and rendering only failures
    left it invisible.
    """
    lines: list[str] = []
    for rule in finding.get("rules", []):
        if rule.get("status") in ("FAIL", "REVIEW"):
            lines.append(f"{rule.get('rule_id')} {rule.get('name')}: {rule.get('detail')}")
    return lines


# ── the output-boundary contract ─────────────────────────────


def determination_violation(
    label_count: int,
    findings: Any,
    report_labels: Any,
) -> Optional[str]:
    """The contract the assembled report must satisfy on its determination.

    Returns the FIELD PATH of the first violation, or None when the report may
    ship. Never returns a value taken from state — not a label identifier, not a
    rule detail. The framework's own final output gate raises when a credential
    appears in ANY value this node returns, and that raise discards the whole
    node delta, including the containment performed alongside it. Field paths
    and positional indices only.

    Reads only keys the assembling node writes in the same delta as the report,
    so no layer here compares against a key absent from its own scope.

    Checked, in order:
      1. a determination of record exists, one finding per label, each complete;
      2. every rule entry is complete and carries a recognised verdict;
      3. the report the caller reads carries the determination for every label,
         and its rendered verdict agrees with it;
      4. the report's citations carry every failing rule the determination
         found;
      5. a label whose determination is not PASS is not rendered as PASS.
    """
    parsed = findings
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return "regulatory_findings"
    if not isinstance(parsed, list) or len(parsed) != label_count:
        return "regulatory_findings"
    if not isinstance(report_labels, list) or len(report_labels) != label_count:
        return "verification_report.labels"

    for index, finding in enumerate(parsed):
        where = f"regulatory_findings[{index}]"
        if not isinstance(finding, Mapping):
            return where
        for key in REQUIRED_FINDING_KEYS:
            if key not in finding:
                return f"{where}.{key}"
        verdict = finding.get("status")
        if verdict not in FINDING_VERDICTS:
            return f"{where}.status"
        rule_results = finding.get("rules")
        if not isinstance(rule_results, list) or not rule_results:
            return f"{where}.rules"
        for rule_index, rule in enumerate(rule_results):
            rule_where = f"{where}.rules[{rule_index}]"
            if not isinstance(rule, Mapping):
                return rule_where
            for key in REQUIRED_RULE_KEYS:
                if key not in rule:
                    return f"{rule_where}.{key}"
            if rule.get("status") not in RULE_VERDICTS:
                return f"{rule_where}.status"

        rendered = report_labels[index]
        report_where = f"verification_report.labels[{index}]"
        if not isinstance(rendered, Mapping):
            return report_where
        if rendered.get("label_id") != finding.get("label_id"):
            return f"{report_where}.label_id"
        checks = rendered.get("checks")
        if not isinstance(checks, Mapping):
            return f"{report_where}.checks"
        if checks.get(REGULATORY_CHECK) != verdict:
            return f"{report_where}.checks.{REGULATORY_CHECK}"
        cited = rendered.get("citations")
        if not isinstance(cited, list):
            return f"{report_where}.citations"
        for rule_id in citations(finding):
            if rule_id not in cited:
                return f"{report_where}.citations"
        if verdict != "PASS" and rendered.get("status") == "PASS":
            return f"{report_where}.status"

    return None


def findings_by_label(findings: Sequence[Mapping[str, Any]]) -> Dict[Any, Mapping[str, Any]]:
    """Index a determination by label identifier, for the renderer."""
    indexed: Dict[Any, Mapping[str, Any]] = {}
    for finding in findings:
        indexed[finding.get("label_id")] = finding
    return indexed


__all__: List[str] = [
    "REGULATORY_CHECK",
    "REQUIRED_FINDING_KEYS",
    "REQUIRED_RULE_KEYS",
    "FINDING_VERDICTS",
    "RULE_VERDICTS",
    "RULE_SET_ID",
    "RULE_SET_NAME",
    "RULE_SET_NOT_APPLIED",
    "RULE_PARAMS_UNEVALUABLE",
    "UNRECOGNISED_CHECK",
    "citations",
    "determination_violation",
    "evaluate_labels",
    "findings_by_label",
    "remediation_lines",
]
