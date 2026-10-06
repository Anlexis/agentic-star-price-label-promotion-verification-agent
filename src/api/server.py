"""Standalone HTTP entry point.

An adapter, not a place for domain logic: it authenticates the caller, bounds
and screens the request, and hands it to the graph. When the agent runs under
the platform gateway instead, the gateway calls ``invoke()`` directly and this
module is not in the path — which is why everything here is a transport concern
and every domain rule lives in a node.

Three things happen before ``invoke()``:

**Authentication.** The pipeline's entry node requires a verified caller.
Nothing else establishes trust in a standalone deployment, so without this
boundary every deployed request would arrive anonymous and be refused by the
trust gate. The bearer token is a deployment credential presented before any
invocation context exists, so it is read from the environment rather than from
the agent's secret provider.

**A size bound** on the structured context channel, so an oversized payload
never reaches the graph at all.

**A credential screen** on that same channel. The reason it belongs here rather
than in a node is mechanical: the backbone's first node copies the caller's
context verbatim into its own result, and the framework's mandatory output gate
scans every value of every node result and raises on a credential shape. So a
credential-shaped string anywhere in the context makes the *first* node fail,
before any of this agent's code runs, and the caller receives an error with no
indication of which field caused it. The request cannot succeed either way;
screening here does not change what is accepted, it turns an opaque failure into
one the caller can act on.

The screen calls the same detector the framework's gate calls, on the same
assembled object, so the set it refuses and the set the gate blocks are
identical by construction — there is no local pattern list that could drift out
of step. Scanning field by field composes exactly to scanning the whole mapping,
which is what allows the refusal to name the offending field without widening or
narrowing the match. Field names are caller data too, so a name is echoed only
when it is short, inert, and carries no credential shape of its own. The
rejected value and the matched text are never echoed, in the response or in the
audit record.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as secrets_module
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from pydantic import BaseModel, Field
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event

from src.graph.graph import PriceLabelVerificationAgent, runtime_config

_LOGGER = logging.getLogger(__name__)

# Upper bound on the serialized structured context, in bytes. The graph enforces
# per-field bounds of its own; this is the coarse guard that keeps an oversized
# payload out of the graph entirely.
_MAX_INPUT_CONTEXT_BYTES = 262_144

_SAFE_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

app = FastAPI(title="Agent")

_config = runtime_config()
_secrets_provider = secrets_factory(namespace="ret", agent_name="PriceLabelVerificationAgent")

# .get(), not .require(): a deployment without a provisioned model key must boot
# and run its deterministic checks rather than fail at import. The manifest
# therefore declares no required secrets — declaring one here would hard-fail
# exactly the key-less boot this branch exists to support.
_api_key = _secrets_provider.get("OPENAI_API_KEY")
_model_settings = dict(_config.get("llm") or {})
_client: Any = None
if _api_key:
    # Imported inside the branch: the provider client pulls in a large
    # dependency tree that costs real time at every process start, and the
    # no-key path this branch exists to support never needs it.
    from shared.services.llm.openai_client import OpenAIClient

    _client = OpenAIClient(config={**_model_settings, "api_key": _api_key})
else:
    _LOGGER.warning("No model key is configured — promotional claims will be reported as needing review.")
_config["llm"] = _client

agent = PriceLabelVerificationAgent(config=_config)
agent.compile()
agent.provision_secrets(_secrets_provider)


def _field_reference(name: object, index: int) -> str:
    """Render a caller-supplied field name that is safe to put in a message."""
    if isinstance(name, str) and _SAFE_FIELD_NAME_RE.match(name) and not detect_credentials_in_value(name):
        return f"input_context.{name}"
    return f"input_context field #{index}"


def screen_input_context(input_context: dict[str, Any]) -> str | None:
    """Return a reference to the first credential-bearing field, else None.

    Walks the top-level fields in caller order and hands each value to the
    framework detector, which recurses through nested mappings and lists on its
    own. Only the first offending field is reported: one is enough to act on,
    and the message stays bounded however many fields were sent.
    """
    for index, (name, value) in enumerate(input_context.items(), start=1):
        if detect_credentials_in_value(value):
            return _field_reference(name, index)
    return None


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured invocation parameters — currently the regulatory profile to
    # apply. Validated field by field inside the graph.
    input_context: dict[str, Any] = Field(default_factory=dict)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: constant-time comparison raises on non-ASCII str input,
        # and headers decode as latin-1, so a non-ASCII header would produce a
        # server error instead of the intended generic refusal.
        if not secrets_module.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Deliberately generic: do not reveal whether the token was absent,
            # malformed, or simply wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    input_context = req.input_context or {}
    if len(json.dumps(input_context, default=str).encode("utf-8")) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")

    offending_field = screen_input_context(input_context)
    if offending_field is not None:
        emit_trace_event(
            "input_context_credential_refused",
            {"field": offending_field},
            {"session_id": req.session_id},
        )
        # 400 rather than 422: the validation framework owns 422 and answers it
        # with a list of error objects, so reusing it would make the response
        # shape ambiguous for a client.
        raise HTTPException(
            status_code=400,
            detail=(
                f"{offending_field} contains a credential-shaped value. Remove API keys, "
                "tokens and connection strings from input_context and retry."
            ),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # invoke() comes from the untyped framework package; its result is the
        # documented output mapping.
        return cast(
            dict[str, Any],
            agent.invoke(req.input, ctx=ctx, input_context=input_context),
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "PriceLabelVerificationAgent"}
