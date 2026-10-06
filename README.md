# Price Label & Promotion Verification Agent

AI agent for verifying retail price labels and promotional claims, built with Agentic Star.

> **Category**: Cat 2 (domain-specific verification pipeline)
> **Industry**: Retail
> **Template ID**: RET-C2-017

## Overview

Checks a batch of retail price labels for the three ways a label goes wrong: the price
disagrees with itself across channels, the promotional claim does not match the discount the
prices imply, or the offer breaches an advertising rule. It returns a structured verdict per
label with the rule cited and a remediation suggestion, so the result is something a
merchandising team can act on rather than a score to interpret.

Amounts are reported exactly as submitted. There is deliberately no rounding on the output:
the report exists to show that two displayed prices differ, and a rounded amount can no
longer demonstrate the difference it was rendered to demonstrate.

The advertising rules are data rather than code — they live in `config/keihin_rules.yaml`, so
amending them means editing that file, not this source. A language model is used for the one
judgement that is genuinely a judgement, whether a claim's wording is defensible; every
numeric check is deterministic. Output the model cannot be parsed from is reported for review
and never as approval.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the framework version does not match, the agent fails
during graph compile / start-up preflight rather than starting in a partially working state.
This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Using it

The agent is served over HTTP by `src/api/server.py`:

```bash
uvicorn src.api.server:app --host 0.0.0.0 --port 8000
```

`POST /invoke` takes the label batch as a JSON string in `input`, with optional structured
parameters in `input_context`:

```json
{
  "input": "[{\"label_id\": \"AEON-1\", \"pos_price\": 980, \"web_price\": 980, \"regular_price\": 1400, \"sale_price\": 980, \"promotion_spec\": {\"claim\": \"30% off\", \"discount_rate\": 0.30}}]",
  "session_id": "batch-2026-08-31",
  "input_context": {"regulatory_profile": "JP"}
}
```

Set `INVOKE_AUTH_TOKEN` on the server and present it as a bearer token: the pipeline requires
a verified caller and refuses anonymous requests. `GET /health` reports readiness.

Without a model key the agent still boots and still runs its deterministic price and rule
checks; every promotional claim is then reported as needing review rather than approved.

### What it checks

| Check | How |
|---|---|
| Price consistency | The point-of-sale, web and sale prices are compared pairwise. A pair is flagged only when it exceeds both an absolute and a proportional tolerance, so rounding noise on large amounts does not become a finding. |
| Discount-implied price | Where a regular price and a claimed discount rate are both present, the sale price they imply is compared against the price actually shown. |
| Promotion claim | A language model assesses whether the wording matches the computed discount and whether it is defensible as advertising copy. |
| Advertising rules | The rule set is applied per label — reference-price honesty, substantiation of superlatives, discount-rate accuracy, prize-value ceilings. |

### Input contract

Every value in a batch is treated as untrusted. Numbers are parsed through a finite, bounded
check, because a NaN compares False against every tolerance and would otherwise report a
discrepancy as being within bounds. Label identifiers are locked to a short inert alphabet
since they are echoed back in the report, and screened for credential shapes because that same
alphabet describes an API key. Free text is screened for prompt injection both as received and
with markup removed — each pass catches what the other cannot.

### Configuration

| File | Holds |
|---|---|
| `config/agent.yaml` | The static manifest: identity, entry point, required trust level, declared secrets and extras. |
| `config/config.yaml` | Runtime parameters: tolerances, rule-library path, regulatory profile, model settings. |
| `config/keihin_rules.yaml` | The advertising rule set applied per label. |
| `prompts/promotion_claim.md` | The claim-assessment prompt. |

## Project Structure

```
src/          agent implementation (api, graph, nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest, runtime configuration, rule library
prompts/      the claim-assessment prompt
docs/         design document and test specification
```

See `docs/02_design.md` for the architecture and `docs/03_test_spec.md` for the test cases.

## Customising

1. Adjust `config/config.yaml` for your own tolerances and regulatory profile.
2. Replace `config/keihin_rules.yaml` with the rule set that applies in your market. The four
   `check_type` values the engine dispatches on are listed in `docs/02_design.md`; a rule
   declaring anything else is reported for review rather than skipped.
3. Adapt `prompts/promotion_claim.md` to your own advertising standards.
4. Review the node implementations under `src/nodes/` for domain-specific logic.
5. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
