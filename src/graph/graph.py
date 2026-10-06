"""Graph composition for the price-label and promotion verification agent.

The agent inherits the framework's base graph directly and fills the three
middle backbone slots; the backbone owns its own edges, so they are not
redefined here.

    pre_process   InputParse -> RuleLoad
    main          PriceConsistencyCheck -> PromotionClaimCheck
    post_process  ResultAssemble

Each slot runs its sub-nodes inline and stops at the first one that returns an
error, so a refusal keeps its status all the way to the finalize slot. A slot
that receives an already-failed state passes it through untouched rather than
overwriting it — the backbone visits every slot in order, so without that guard
a later slot would erase an earlier refusal.

**The regulatory determination is not a layer here.** It used to be a
``RegulatoryComplianceCheck`` node ahead of ResultAssemble in the post-process
slot; that form is prohibited, and it was bypassable in fact — dropping that one
registration left the pipeline returning ``status: success`` and a report
reading PASS for a label that breached three rules. The rules now live in
``src/nodes/regulatory_determination`` as a module, are evaluated where the
report is assembled, and are enforced at the output boundary by
ResultAssemble's own output gate. ``post_process`` is a fixed backbone slot that
the base graph routes every successful invocation through, so a check placed
there cannot be routed around.

**Runtime configuration** arrives as ``Graph(config=...)``, exactly as the
platform registry supplies it from ``config/config.yaml``. The manifest
(``config/agent.yaml``) is the registry's discovery record and holds no tunable
values; reading runtime settings from it would find nothing and silently fall
back to defaults.

**The model client is a deployment decision.** It arrives on ``config["llm"]``,
already constructed. When no client is configured the deterministic price and
regulatory checks still run on real caller data, and every promotional claim is
reported as needing review — never as approved. An agent that cannot assess a
claim must not answer as though it had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.input_parse import InputParseNode
from src.nodes.price_consistency_check import PriceConsistencyCheckNode
from src.nodes.promotion_claim_check import PromotionClaimCheckNode, load_promotion_prompt
from src.nodes.result_assemble import (
    ERROR_REASONS,
    WITHHELD_KEYS,
    WITHHELD_NOTICE,
    ResultAssembleNode,
)
from src.nodes.rule_load import RuleLoadNode
from src.schemas.state import State

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _is_error(result: Mapping[str, Any]) -> bool:
    return result.get("status") in (AgentStatus.ERROR, AgentStatus.ERROR.value)


def _contained_payload(report: Any) -> Optional[str]:
    """The only rendering a non-success response may release.

    Membership in the closed set is checked HERE rather than trusted from the
    node that produced it, so the property belongs to the boundary the caller
    reads instead of to a promise made four nodes upstream. Anything else is
    dropped: an assembled draft left in state beside an error, a payload whose
    reason is not one this template declares, a partial shape.

    ``error_log`` is never consulted. It is the internal channel — the state
    reducer appends to it and the audit trail needs it — and it carries
    node-authored text and, wherever the framework's own error path runs, a
    caught exception and its traceback. None of that is a closed set, so none of
    it is projected to the caller.
    """
    if not isinstance(report, str):
        return None
    try:
        payload = json.loads(report)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or set(payload) != WITHHELD_KEYS:
        return None
    if payload.get("reason") not in ERROR_REASONS or payload.get("note") != WITHHELD_NOTICE:
        return None
    return report


def runtime_config(project_root: Optional[Path] = None) -> dict[str, Any]:
    """Read ``config/config.yaml`` — the runtime parameters.

    The registry loads this file itself and hands it to the constructor. The
    standalone adapter calls this so both deployments run on the same values
    instead of one of them quietly running on defaults.
    """
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    path = root / "config" / "config.yaml"
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


class _SequentialSlotNode(FunctionNode):
    """Runs an ordered list of sub-nodes inline, merging their partial results."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    # A slot is an orchestrator: the audit record is emitted by the sub-nodes it
    # runs, each of which reports its own domain event.
    _s4_audit_exempt: ClassVar[str] = (
        "Slot orchestrator: runs domain sub-nodes inline and emits no domain "
        "event of its own; every sub-node it runs emits one."
    )
    _sub_nodes: tuple[FunctionNode, ...] = ()

    def execute(self, state: Mapping[str, Any], config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if _is_error(state) or state.get("blocked") is True:
            return {
                "status": state.get("status"),
                "blocked": state.get("blocked", True),
            }

        merged = dict(state)
        accumulated: dict[str, Any] = {}
        for node in self._sub_nodes:
            result = node(merged)  # __call__ applies the trust gate, then execute()
            merged = {**merged, **result}
            accumulated = {**accumulated, **result}
            if _is_error(result):
                break
        # The backbone appends node history per slot; drop the inner copies.
        accumulated.pop("node_history", None)
        accumulated.pop("execution_time", None)
        return accumulated


class PreProcessSlotNode(_SequentialSlotNode):
    """pre_process slot: InputParse -> RuleLoad."""

    def __init__(self, input_parse: InputParseNode, rule_load: RuleLoadNode) -> None:
        self._sub_nodes = (input_parse, rule_load)


class MainSlotNode(_SequentialSlotNode):
    """main slot: PriceConsistencyCheck -> PromotionClaimCheck."""

    def __init__(self, price: PriceConsistencyCheckNode, promotion: PromotionClaimCheckNode) -> None:
        self._sub_nodes = (price, promotion)


class PostProcessSlotNode(_SequentialSlotNode):
    """post_process slot: ResultAssemble — determination, assembly, output gate.

    One sub-node, kept in a slot wrapper for the already-failed passthrough
    guard: without it a slot reached after an upstream refusal would overwrite
    that refusal with its own result.
    """

    def __init__(self, result: ResultAssembleNode) -> None:
        self._sub_nodes = (result,)


class PriceLabelVerificationAgent(AgentBaseGraph):
    """Retail price-label and promotion verification agent."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        project_root: Optional[Path] = None,
    ) -> None:
        super().__init__(config=dict(config) if config else {})

        root = Path(project_root) if project_root is not None else _PROJECT_ROOT
        settings = self.config

        security = settings.get("security") or {}
        if security.get("output_gate_enabled") is False:
            raise ValueError("security.output_gate_enabled must be true — the output gate is mandatory")

        price_settings = settings.get("price") or {}
        rules_path = root / str(settings.get("rules_path", "config/keihin_rules.yaml"))
        prompt_path = root / str(settings.get("prompt_path", "prompts/promotion_claim.md"))
        system_prompt, user_template = load_promotion_prompt(prompt_path)

        self._default_profile = str(settings.get("regulatory_profile", "JP"))

        self._input_parse = InputParseNode(accept_image_input=bool(settings.get("accept_image_input", False)))
        self._rule_load = RuleLoadNode(
            rules_path=rules_path,
            default_profile=self._default_profile,
            enable_keihin_check=bool(settings.get("enable_keihin_check", True)),
        )
        self._price = PriceConsistencyCheckNode(
            price_tolerance_abs=price_settings.get("tolerance_abs", 1.0),
            price_tolerance_pct=price_settings.get("tolerance_pct", 0.001),
        )
        self._promotion = PromotionClaimCheckNode(
            llm_client=settings.get("llm"),
            system_prompt=system_prompt,
            user_prompt_template=user_template,
        )
        self._result = ResultAssembleNode(
            note=str((settings.get("report") or {}).get("note", "for internal reporting purposes")),
            discount_rate_tolerance=price_settings.get("discount_rate_tolerance", 0.01),
        )

    # ── identity / schema hooks ──────────────────────────────

    @property
    def name(self) -> str:
        return "PriceLabelVerificationAgent"

    @property
    def state_schema(self) -> type:
        return State

    # ── backbone wiring ──────────────────────────────────────

    def register_nodes(self) -> None:
        super().register_nodes()  # injects the initialize and finalize slots
        self._nodes["pre_process"] = PreProcessSlotNode(self._input_parse, self._rule_load)
        self._nodes["main"] = MainSlotNode(self._price, self._promotion)
        self._nodes["post_process"] = PostProcessSlotNode(self._result)

    # add_edges() is owned by the base graph and is deliberately not overridden.

    def get_output(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve the caller-visible response.

        The base implementation returns the first non-empty output field
        regardless of status, so an error envelope still carries whatever draft
        the pipeline had produced. That is the shape this override exists to
        close, and it closes it on a closed set rather than on a flag: on any
        non-success outcome the only thing released is the payload built by
        ``ResultAssembleNode``'s ``_contain()`` — a reason code this template
        declared and the constant withheld notice — and an assembled report is
        never surfaced beside an error status.

        Before this change the release turned on ``blocked`` alone, so any
        future path that set that flag while leaving an assembled report in
        state would have published it. ``_contained_payload`` decides instead,
        by reading the payload.
        """
        status = state.get("status")
        failed = status in (AgentStatus.ERROR, AgentStatus.ERROR.value)
        report = state.get("verification_report")
        if failed:
            report = _contained_payload(report)
        return {
            "output": report,
            "status": status,
            "blocked": bool(state.get("blocked", False)),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }


# The registry resolves the entry point by dotted path; this alias keeps the
# generic name available for tooling that expects it.
Graph = PriceLabelVerificationAgent
