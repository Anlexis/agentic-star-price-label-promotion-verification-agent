"""Shared fixtures.

The framework is installed from the platform wheel, so ``framework.*`` resolves
from site-packages. Only the project root is placed on ``sys.path``, so
``src.*`` imports resolve the same way in a test run as they do in a deployment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

RULES_PATH = _ROOT / "config" / "keihin_rules.yaml"
PROMPT_PATH = _ROOT / "prompts" / "promotion_claim.md"


class StubChatClient:
    """Stands in for a platform chat client.

    Implements the same ``complete(messages) -> mapping`` contract the platform
    clients implement, and records what it was asked, so a test can assert on
    the text that actually reached the model rather than on the text that was
    submitted.
    """

    def __init__(
        self,
        accurate: bool = True,
        defensible: bool = True,
        assessment: str = "妥当",
        content: str | None = None,
    ) -> None:
        self._content = (
            content
            if content is not None
            else json.dumps(
                {
                    "claim_accurate": accurate,
                    "defensible": defensible,
                    "assessment": assessment,
                },
                ensure_ascii=False,
            )
        )
        self.calls = 0
        self.last_messages: list[dict[str, str]] | None = None

    def complete(self, messages: list) -> dict:
        self.calls += 1
        self.last_messages = messages
        return {"content": self._content, "tool_calls": [], "model": "stub"}

    @property
    def last_prompt(self) -> str:
        """Every message body the last call carried, concatenated."""
        return "\n".join(m.get("content", "") for m in (self.last_messages or []))


def base_config(**overrides: Any) -> dict:
    """The shipped runtime configuration, with test overrides applied."""
    from src.graph.graph import runtime_config

    config = runtime_config(_ROOT)
    config.update(overrides)
    return config


@pytest.fixture
def rules_path() -> Path:
    return RULES_PATH


@pytest.fixture
def prompt_path() -> Path:
    return PROMPT_PATH


@pytest.fixture
def promotion_prompt():
    from src.nodes.promotion_claim_check import load_promotion_prompt

    return load_promotion_prompt(PROMPT_PATH)


@pytest.fixture
def stub_client() -> StubChatClient:
    return StubChatClient()


@pytest.fixture
def clean_label() -> dict:
    return {
        "label_id": "AEON-1",
        "pos_price": 980,
        "web_price": 980,
        "sale_price": 980,
        "regular_price": 1400,
        "shelf_label_text": "本日限り 最大30%オフ",
        "promotion_spec": {"claim": "最大30%オフ", "discount_rate": 0.30},
    }


@pytest.fixture
def build_agent():
    """Factory: the agent on its shipped configuration with a stub chat client."""
    from src.graph.graph import PriceLabelVerificationAgent

    def _build(**client_kwargs: Any) -> PriceLabelVerificationAgent:
        return PriceLabelVerificationAgent(
            config=base_config(llm=StubChatClient(**client_kwargs)),
            project_root=_ROOT,
        )

    return _build
