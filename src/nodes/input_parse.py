"""InputParse node — first step of the pre-process slot.

Owns the caller-data contract. Every value in the label batch arrives from
outside the trust boundary, so this node is where the batch stops being caller
text and becomes validated domain data:

  - the batch is decoded with the non-finite JSON literals refused outright;
  - each label identifier is locked to an inert identifier alphabet, because it
    is echoed back in the rendered report;
  - every numeric field goes through a finite, bounded parser — an unchecked
    NaN compares False against every tolerance and would quietly report a price
    discrepancy as being within bounds;
  - every value is screened for credential shapes with the framework's own
    detector, so a key hidden in an otherwise inert identifier is refused at the
    door rather than at the exit;
  - free-text fields are screened for prompt injection both as received and
    with markup removed, then stripped of markup before anything downstream can
    place them in a model prompt;
  - structural caps bound the batch size and the per-label field count.

Refusals return an error status rather than raising: the backbone routes an
error straight to the finalize slot, and the remaining slots pass it through
untouched. Error messages name the offending field and never echo its value.

Image labels are out of scope. When ``accept_image_input`` is false (the
default) a label carrying an image field is refused rather than silently
processed as though the image had been read.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Dict, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.validation import (
    CallerInputError,
    finite_in_range,
    reject_json_constants,
    safe_identifier,
    screen_for_credentials,
    screen_for_injection,
    strip_markup,
)

_PRICE_FIELDS = ("pos_price", "web_price", "regular_price", "sale_price")
_TEXT_FIELDS = ("shelf_label_text",)
_IMAGE_FIELDS = ("image", "label_image", "image_url", "image_b64")
_PERIOD_FIELDS = ("period_start", "period_end")
_PRIZE_FIELDS = ("prize_value", "transaction_amount")

# Structural bounds. A price in a retail system is a non-negative amount well
# inside this ceiling; a batch larger than this is an integration fault, not a
# verification request.
_MAX_LABELS = 500
_MAX_AMOUNT = 1e12
_MAX_TEXT_CHARS = 2000
_MAX_PERIOD_CHARS = 64


class InputParseNode(FunctionNode):
    """Parses, screens, validates and normalizes the caller-supplied label batch."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, accept_image_input: bool = False) -> None:
        self.accept_image_input = bool(accept_image_input)

    def execute(self, state: Mapping[str, Any], config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        raw = state.get("labels_input") or state.get("user_input")
        if not raw:
            return self._error("labels_input is required")
        if not isinstance(raw, str):
            return self._error("labels_input must be a JSON string")

        try:
            labels = json.loads(raw, parse_constant=reject_json_constants)
        except CallerInputError as exc:
            # CallerInputError messages are composed of this project's own
            # constant phrases plus a field path (see src/services/validation);
            # no caller value is ever part of one, so the message is closed.
            return self._error(str(exc))
        except json.JSONDecodeError:
            # The decoder's own `msg` is library text — it names positions and,
            # on an unterminated string, the construct it stopped at. It is not
            # a set this module controls, so it is not carried: that the batch
            # did not decode is the whole of what the caller needs.
            return self._error("labels_input is not valid JSON")

        if not isinstance(labels, list) or not labels:
            return self._error("labels_input must decode to a non-empty JSON array")
        if len(labels) > _MAX_LABELS:
            return self._error(f"labels_input must contain at most {_MAX_LABELS} labels")

        try:
            normalized = [self._normalize_label(label, index) for index, label in enumerate(labels)]
        except CallerInputError as exc:
            # Closed for the same reason as above: field path plus constants.
            return self._error(str(exc))

        emit_trace_event(
            "input_parse",
            {"labels_in": len(labels), "labels_normalized": len(normalized)},
            state,
        )
        return {
            "status": AgentStatus.SUCCESS.value,
            "labels_input": json.dumps(normalized, sort_keys=True, ensure_ascii=False),
        }

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        # The message names the field; the rejected value is never carried into
        # the log, where it would outlive the request that was refused.
        emit_trace_event("input_parse_refused", {"reason": message}, {})
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"InputParseNode: {message}"],
        }

    def _normalize_label(self, label: Any, index: int) -> dict[str, Any]:
        where = f"labels[{index}]"
        if not isinstance(label, Mapping):
            raise CallerInputError(f"{where} must be an object")

        # Screen the whole label, keys included, before reading any field:
        # a directive in a field name reaches the same downstream text as one
        # in a value, and a credential fits inside the identifier alphabet the
        # label id is otherwise allowed to use.
        screen_for_injection(label, field=where)
        screen_for_credentials(label, field=where)

        if not self.accept_image_input and any(key in label for key in _IMAGE_FIELDS):
            raise CallerInputError(f"{where}: image labels are not accepted by this deployment")

        result: dict[str, Any] = {"label_id": safe_identifier(label.get("label_id"), field=f"{where}.label_id")}

        present = [field for field in _PRICE_FIELDS if field in label]
        if not present:
            raise CallerInputError(f"{where}: at least one of {list(_PRICE_FIELDS)} is required")
        for field in present:
            result[field] = finite_in_range(label[field], field=f"{where}.{field}", minimum=0.0, maximum=_MAX_AMOUNT)

        for field in _TEXT_FIELDS:
            value = label.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise CallerInputError(f"{where}.{field} must be a string")
            if len(value) > _MAX_TEXT_CHARS:
                raise CallerInputError(f"{where}.{field} must be at most {_MAX_TEXT_CHARS} characters")
            result[field] = strip_markup(value)

        spec = label.get("promotion_spec")
        if spec is not None:
            result["promotion_spec"] = self._normalize_spec(spec, f"{where}.promotion_spec")

        return result

    def _normalize_spec(self, spec: Any, where: str) -> dict[str, Any]:
        if not isinstance(spec, Mapping):
            raise CallerInputError(f"{where} must be an object")
        normalized: dict[str, Any] = {}

        claim = spec.get("claim")
        if claim is not None:
            if not isinstance(claim, str) or not claim.strip():
                raise CallerInputError(f"{where}.claim must be a non-empty string")
            if len(claim) > _MAX_TEXT_CHARS:
                raise CallerInputError(f"{where}.claim must be at most {_MAX_TEXT_CHARS} characters")
            normalized["claim"] = strip_markup(claim)

        if "discount_rate" in spec:
            normalized["discount_rate"] = finite_in_range(
                spec["discount_rate"],
                field=f"{where}.discount_rate",
                minimum=0.0,
                maximum=1.0,
            )

        for field in _PERIOD_FIELDS:
            value = spec.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise CallerInputError(f"{where}.{field} must be a string")
            if len(value) > _MAX_PERIOD_CHARS:
                raise CallerInputError(f"{where}.{field} must be at most {_MAX_PERIOD_CHARS} characters")
            normalized[field] = value

        for field in _PRIZE_FIELDS:
            value = spec.get(field)
            if value is None:
                continue
            normalized[field] = finite_in_range(value, field=f"{where}.{field}", minimum=0.0, maximum=_MAX_AMOUNT)

        return normalized
