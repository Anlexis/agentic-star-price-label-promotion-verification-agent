"""Rule-library service — loads and validates the regulatory rule set.

Domain data access only: no routing, no business logic, no credentials. The
rule set is operator-maintained configuration, not caller input — when the
advertising regulations change, the YAML file changes and the source does not.
Structural validation happens once, at construction, so a malformed library
surfaces when the process boots rather than on a caller's request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

_REQUIRED_RULE_KEYS = ("id", "name", "description", "check_type")

VALID_CHECK_TYPES = frozenset(
    {
        "price_reference",
        "claim_substantiation",
        "discount_accuracy",
        "prize_limit",
    }
)

IMPLEMENTED_PROFILES = frozenset({"JP"})


class RuleLibraryError(ValueError):
    """The rule library is missing, unreadable, or structurally invalid."""


class RuleLibrary:
    """The validated rule set for one regulatory profile."""

    def __init__(self, rules_path: str | Path) -> None:
        self._path = Path(rules_path)
        self._rules = self._load_and_validate(self._path)

    @property
    def rules(self) -> list[dict[str, Any]]:
        """A defensive copy of the validated rule set."""
        return [dict(rule) for rule in self._rules]

    @staticmethod
    def _load_and_validate(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise RuleLibraryError(f"rule library not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RuleLibraryError(f"rule library is not valid YAML: {exc}") from exc

        if not isinstance(data, Mapping) or "rules" not in data:
            raise RuleLibraryError("rule library must be a mapping with a top-level 'rules' key")
        rules = data["rules"]
        if not isinstance(rules, list) or not rules:
            raise RuleLibraryError("'rules' must be a non-empty list")

        seen_ids: set[Any] = set()
        validated: list[dict[str, Any]] = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                raise RuleLibraryError(f"rules[{index}] must be a mapping")
            for key in _REQUIRED_RULE_KEYS:
                if key not in rule:
                    raise RuleLibraryError(f"rules[{index}] is missing required key {key!r}")
            if rule["check_type"] not in VALID_CHECK_TYPES:
                raise RuleLibraryError(
                    f"rules[{index}].check_type {rule['check_type']!r} is invalid; "
                    f"expected one of {sorted(VALID_CHECK_TYPES)}"
                )
            if rule["id"] in seen_ids:
                raise RuleLibraryError(f"duplicate rule id {rule['id']!r}")
            seen_ids.add(rule["id"])
            validated.append(dict(rule))

        return validated
