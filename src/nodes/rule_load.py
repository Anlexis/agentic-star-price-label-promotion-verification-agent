"""RuleLoad node — second step of the pre-process slot.

Publishes the active regulatory rule set for the requested profile so the
compliance step downstream evaluates rules it was handed rather than rules
compiled into the source. The library itself is validated once, at construction,
by the rule-library service; this node only selects and publishes.

The profile may arrive seeded directly in state or on the caller's structured
context channel. It is matched against the implemented set rather than used to
build a path, so an unknown profile is refused instead of silently falling back
to a default the caller did not ask for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Dict, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.rule_library import IMPLEMENTED_PROFILES, RuleLibrary


class RuleLoadNode(FunctionNode):
    """Loads the regulatory rule set and publishes it to state."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        rules_path: str | Path,
        default_profile: str = "JP",
        enable_keihin_check: bool = True,
    ) -> None:
        self._library = RuleLibrary(rules_path)
        self._default_profile = default_profile
        self._enable_keihin_check = bool(enable_keihin_check)

    def execute(self, state: Mapping[str, Any], config: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        context = state.get("input_context")
        context_profile = context.get("regulatory_profile") if isinstance(context, Mapping) else None
        profile = state.get("regulatory_profile") or context_profile or self._default_profile

        if not isinstance(profile, str):
            return self._error("regulatory_profile must be a string")
        if profile not in IMPLEMENTED_PROFILES:
            return self._error(
                f"regulatory_profile is not one of the implemented profiles " f"{sorted(IMPLEMENTED_PROFILES)}"
            )

        rules = self._library.rules if self._enable_keihin_check else []
        payload = {"profile": profile, "rules": rules}
        emit_trace_event(
            "rule_load",
            {"profile": profile, "rules_loaded": len(rules)},
            state,
        )
        return {
            "status": AgentStatus.SUCCESS.value,
            "regulatory_profile": profile,
            "loaded_rules": json.dumps(payload, sort_keys=True, ensure_ascii=False),
        }

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        emit_trace_event("rule_load_refused", {"reason": message}, {})
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"RuleLoadNode: {message}"],
        }
