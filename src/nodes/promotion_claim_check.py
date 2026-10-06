"""PromotionClaimCheck node — second step of the main slot.

Assesses each promotional claim with a language model: does the wording match
the discount the prices actually imply, and is it defensible as advertising
copy. One model call per label that carries a claim; labels without one are
recorded as skipped rather than silently passed.

The model is asked for a single JSON verdict. Output that cannot be parsed is
recorded for human review — never treated as approval. A model that fails to
answer must not become a pass, because the failure mode of this agent is a
non-compliant label going out unflagged.

A call that RAISES is the same outcome and is handled the same way. It is also
this template's only third-party call, so it is the one place an upstream
response body can enter this process: a provider exception's message quotes the
body it came from, and a body carries identifiers, echoed request fields and
occasionally a credential. Left unhandled the exception reaches the framework's
own error path, which writes ``str(exc)`` and a full traceback into
``error_log`` — so the closed-set rule has to be applied at the call site, not
at the envelope. What is recorded is the exception's CLASS and, when the client
carries one, the HTTP status; both are bound to locals before anything is
formatted, so no caught exception ever reaches an f-string.

The client contract is the platform's own chat interface: ``complete(messages)``
takes a role/content message list and returns a mapping whose ``content`` holds
the assistant text. Nodes never construct a client — one is injected — so the
provider is a deployment decision, not a source-code one. A deployment with no
client configured reports every claim as needing review; it never reports one as
approved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Dict, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Bound on the model text carried forward for human review, so an unparseable
# response cannot inflate the state payload without limit.
_MAX_RAW_CHARS = 500

# Recorded when the assessment call did not complete. A constant, and a REVIEW
# verdict — never an approval.
MODEL_CALL_FAILED = "the assessment model call did not complete"

# Attributes the platform and provider clients use for an HTTP status. A status
# is a closed set; the exception's message is not.
_STATUS_ATTRIBUTES = ("status_code", "status", "http_status")


class PromotionClaimCheckError(ValueError):
    """The node was constructed with an unusable client or prompt."""


def _http_status(exc: BaseException) -> Optional[int]:
    """The HTTP status a client exception carries, or None.

    Read by attribute and range-checked, so what travels onward is an integer
    this function chose to accept rather than whatever the client attached.
    """
    for attribute in _STATUS_ATTRIBUTES:
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


class PromotionClaimCheckNode(FunctionNode):
    """Model-assisted promotion-claim accuracy and defensibility assessment."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        llm_client: Any,
        system_prompt: str,
        user_prompt_template: str,
    ) -> None:
        if llm_client is not None and not hasattr(llm_client, "complete"):
            raise TypeError("llm_client must expose a .complete(messages) method")
        if not system_prompt or not user_prompt_template:
            raise ValueError("system_prompt and user_prompt_template must be non-empty")
        self._llm = llm_client
        self._system_prompt = system_prompt
        self._user_prompt_template = user_prompt_template

    def execute(self, state: Mapping[str, Any], config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        raw = state.get("labels_input")
        if not raw:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PromotionClaimCheckNode: labels_input is required"],
            }
        labels = json.loads(raw)
        findings = [self._assess_label(label) for label in labels]
        emit_trace_event(
            "promotion_claim_check",
            {
                "labels_assessed": len(findings),
                "model_calls": sum(1 for f in findings if f.get("status") in ("PASS", "FAIL")),
                "fail": sum(1 for f in findings if f.get("status") == "FAIL"),
                "review": sum(1 for f in findings if f.get("status") == "REVIEW"),
            },
            state,
        )
        return {
            "status": AgentStatus.SUCCESS.value,
            "promotion_findings": json.dumps(findings, sort_keys=True, ensure_ascii=False),
        }

    # ── per-label ────────────────────────────────────────────

    def _assess_label(self, label: Mapping[str, Any]) -> dict[str, Any]:
        label_id = label.get("label_id")
        spec = label.get("promotion_spec") or {}
        claim = spec.get("claim") if isinstance(spec, Mapping) else None

        if not claim:
            return {"label_id": label_id, "status": "SKIP", "reason": "no promotion claim"}

        computed_rate = self._computed_rate(label)
        if self._llm is None:
            # No model is configured for this deployment. The claim is reported
            # as needing review rather than approved: an unassessed claim that
            # reads as a pass is the one outcome this agent must never produce.
            return {
                "label_id": label_id,
                "status": "REVIEW",
                "claim": claim,
                "reason": "no assessment model is configured for this deployment",
                "computed_rate": computed_rate,
            }
        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": self._render_user_prompt(label_id, label, spec, claim, computed_rate),
            },
        ]
        try:
            response = self._llm.complete(messages)
        except Exception as exc:
            # Bound to locals BEFORE anything is formatted: the exception object
            # itself must not reach a message. Its text is the upstream response
            # body; the class and the status are the closed-set part.
            exception_class = type(exc).__name__
            status_code = _http_status(exc)
            emit_trace_event(
                "promotion_claim_model_failed",
                {"exception": exception_class, "http_status": status_code},
                {},
            )
            return {
                "label_id": label_id,
                "status": "REVIEW",
                "reason": MODEL_CALL_FAILED,
            }

        verdict = self._parse_verdict(self._content(response))

        if verdict is None:
            return {
                "label_id": label_id,
                "status": "REVIEW",
                "reason": "model output was not a parseable JSON verdict",
            }

        accurate = bool(verdict.get("claim_accurate"))
        defensible = bool(verdict.get("defensible"))
        return {
            "label_id": label_id,
            "status": "PASS" if (accurate and defensible) else "FAIL",
            "claim": claim,
            "claim_accurate": accurate,
            "defensible": defensible,
            "assessment": str(verdict.get("assessment", ""))[:_MAX_RAW_CHARS],
            "computed_rate": computed_rate,
        }

    @staticmethod
    def _content(response: Any) -> Optional[str]:
        """Read the assistant text out of a platform chat response."""
        if isinstance(response, Mapping):
            content = response.get("content")
            return content if isinstance(content, str) else None
        return response if isinstance(response, str) else None

    @staticmethod
    def _computed_rate(label: Mapping[str, Any]) -> Optional[float]:
        regular = label.get("regular_price")
        sale = label.get("sale_price")
        if isinstance(regular, (int, float)) and isinstance(sale, (int, float)) and regular > 0:
            return round(1.0 - (sale / regular), 4)
        return None

    @staticmethod
    def _parse_verdict(raw: Any) -> Optional[dict[str, Any]]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _render_user_prompt(
        self,
        label_id: Any,
        label: Mapping[str, Any],
        spec: Mapping[str, Any],
        claim: str,
        computed_rate: Optional[float],
    ) -> str:
        return self._user_prompt_template.format(
            label_id=label_id,
            claim=claim,
            claimed_rate=spec.get("discount_rate", "(not stated)"),
            computed_rate=computed_rate if computed_rate is not None else "(not computable)",
            regular_price=label.get("regular_price", "(none)"),
            sale_price=label.get("sale_price", "(none)"),
        )


# ── prompt loader (used by the graph layer) ──────────────────


def load_promotion_prompt(prompt_path: Optional[Path] = None) -> tuple[str, str]:
    """Read the prompt file and return ``(system_prompt, user_template)``."""
    if prompt_path is None:
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "promotion_claim.md"
    text = Path(prompt_path).read_text(encoding="utf-8")
    system = _extract_section(text, "## System")
    user_template = _extract_first_code_block(_extract_section(text, "## User template"))
    if not system or not user_template:
        raise PromotionClaimCheckError(f"prompt file {prompt_path} is missing its System and/or User template section")
    return system, user_template


def _extract_section(text: str, heading: str) -> str:
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("## "):
            if line.strip() == heading:
                inside = True
                continue
            if inside:
                break
        elif inside:
            out.append(line)
    return "\n".join(out).strip()


def _extract_first_code_block(section: str) -> str:
    inside = False
    out: list[str] = []
    for line in section.splitlines():
        if line.startswith("```"):
            if inside:
                break
            inside = True
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()
