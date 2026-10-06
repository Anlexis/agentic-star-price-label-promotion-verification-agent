"""State schema for the price-label and promotion verification pipeline.

The framework package ships without a ``py.typed`` marker, so ``AgentState``
resolves to ``Any`` for a type checker and this class is not recognised as a
TypedDict. At runtime it is a genuine TypedDict and these fields must stay
``NotRequired`` (nodes write them; they are absent at graph initialisation), so
the annotations are kept and only that one check is disabled.
# mypy: disable-error-code="valid-type"

State must be a flat TypedDict — never a Pydantic model. Graph checkpoints use
msgpack serialization, which silently corrupts Pydantic objects. Structured
payloads are therefore carried as JSON strings. Never place credentials or
secrets here.
"""

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Retail price-label and promotion verification state.

    Shared fields (``user_input``, ``input_context``, ``status``,
    ``session_id``, ``node_history``, ``error_log``, ``formatted_output`` …)
    are inherited. Agent-specific fields follow.

    Flow: InputParse -> RuleLoad -> PriceConsistencyCheck -> PromotionClaimCheck
          -> ResultAssemble
    """

    # ── Input ──
    labels_input: NotRequired[str]
    """JSON array of normalized label objects. Each carries ``label_id``, the
    price fields present, an optional sanitized ``shelf_label_text`` and an
    optional ``promotion_spec`` (claim / discount_rate / period_start /
    period_end / prize_value / transaction_amount)."""

    regulatory_profile: NotRequired[str]
    """Regulatory rule set to apply. ``JP`` is the implemented profile."""

    # ── Computed during the pipeline ──
    loaded_rules: NotRequired[str]
    """JSON: the active rule set loaded for ``regulatory_profile``."""

    price_findings: NotRequired[str]
    """JSON: per-label price-consistency results (shelf vs point-of-sale vs web,
    regular vs sale) with the delta and a flagged boolean."""

    promotion_findings: NotRequired[str]
    """JSON: per-label promotion-claim results (discount-rate accuracy and a
    wording assessment)."""

    regulatory_findings: NotRequired[str]
    """JSON: per-label regulatory results — per-rule verdict plus citation.

    Written at the output boundary, in the SAME delta as ``verification_report``
    and never separately, so a report and the determination it rests on cannot
    exist apart. Set to ``None`` when the gate withholds the report — cleared by
    presence, because a partial delta that omits the key leaves the previous
    value in state."""

    verification_report: NotRequired[str]
    """JSON: the assembled report — per-label, per-check verdict with rule
    citation and a remediation suggestion."""

    blocked: NotRequired[bool]
    """True when the output gate refused to release the assembled report."""


# Backwards-compatible alias for the pipeline's descriptive name.
PriceLabelVerificationState = State
