"""PriceConsistencyCheck node — first step of the main slot.

Deterministic. No model call: a price either agrees across channels or it does
not, and that question has an arithmetic answer.

A pair is flagged only when BOTH bounds are exceeded::

    abs(delta) > tolerance_abs  AND  abs(delta) / reference > tolerance_pct

Requiring both keeps rounding noise on large amounts and one-yen differences on
small ones out of the findings, without letting a genuine discrepancy through at
either end of the range.

Two checks per label:

1. **Selling-price consistency** — the point-of-sale, web and sale prices are
   compared pairwise, so a label priced differently in two channels is caught
   whichever channel is wrong.
2. **Discount-implied price** — when a regular price and a claimed discount rate
   are both present, the sale price they imply is compared against the price
   actually shown.

The tolerances are operator configuration, but they are still parsed through the
finite, bounded parser at construction: a non-finite tolerance would make every
comparison against it False and turn the whole check into a silent pass.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Dict, Mapping, Optional, TypeGuard

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.validation import finite_in_range

_SELLING_PRICE_FIELDS = ("pos_price", "web_price", "sale_price")
_MAX_AMOUNT = 1e12


class PriceConsistencyCheckNode(FunctionNode):
    """Cross-channel price consistency and discount-implied price check."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, price_tolerance_abs: float = 1.0, price_tolerance_pct: float = 0.001) -> None:
        self.price_tolerance_abs = finite_in_range(
            price_tolerance_abs, field="price.tolerance_abs", minimum=0.0, maximum=_MAX_AMOUNT
        )
        self.price_tolerance_pct = finite_in_range(
            price_tolerance_pct, field="price.tolerance_pct", minimum=0.0, maximum=1.0
        )

    def execute(self, state: Mapping[str, Any], config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        raw = state.get("labels_input")
        if not raw:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PriceConsistencyCheckNode: labels_input is required"],
            }
        labels = json.loads(raw)
        findings = [self._check_label(label) for label in labels]
        emit_trace_event(
            "price_consistency_check",
            {
                "labels_checked": len(findings),
                "fail": sum(1 for finding in findings if finding["status"] == "FAIL"),
            },
            state,
        )
        return {
            "status": AgentStatus.SUCCESS.value,
            "price_findings": json.dumps(findings, sort_keys=True, ensure_ascii=False),
        }

    # ── per-label ────────────────────────────────────────────

    def _check_label(self, label: Mapping[str, Any]) -> dict[str, Any]:
        discrepancies: list[dict[str, Any]] = []

        present = [
            (field, label[field])
            for field in _SELLING_PRICE_FIELDS
            if isinstance(label.get(field), (int, float)) and not isinstance(label.get(field), bool)
        ]
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                field_a, value_a = present[i]
                field_b, value_b = present[j]
                if self._flagged(value_a, value_b):
                    discrepancies.append(
                        {
                            "type": "selling_price_mismatch",
                            "fields": [field_a, field_b],
                            "values": [value_a, value_b],
                            "delta": abs(value_a - value_b),
                        }
                    )

        regular = label.get("regular_price")
        spec = label.get("promotion_spec") or {}
        rate = spec.get("discount_rate") if isinstance(spec, Mapping) else None
        if self._is_number(regular) and self._is_number(rate):
            expected = regular * (1.0 - rate)
            actual_field = next(
                (field for field in ("sale_price", "pos_price", "web_price") if self._is_number(label.get(field))),
                None,
            )
            if actual_field is not None:
                actual = label[actual_field]
                if self._flagged(expected, actual):
                    discrepancies.append(
                        {
                            "type": "discount_implied_mismatch",
                            "expected_sale": round(expected, 4),
                            "actual_field": actual_field,
                            "actual": actual,
                            "regular_price": regular,
                            "discount_rate": rate,
                            "delta": abs(expected - actual),
                        }
                    )

        return {
            "label_id": label.get("label_id"),
            "status": "FAIL" if discrepancies else "PASS",
            "discrepancies": discrepancies,
        }

    @staticmethod
    def _is_number(value: Any) -> TypeGuard[float]:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _flagged(self, a: float, b: float) -> bool:
        delta = abs(a - b)
        if delta <= self.price_tolerance_abs:
            return False
        reference = max(abs(a), abs(b))
        if reference == 0:
            return True
        return (delta / reference) > self.price_tolerance_pct
