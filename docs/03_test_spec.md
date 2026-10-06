# Retail Price Label and Promotion Verification Agent — Test Specification

**Template ID:** RET-C2-017
**Test framework:** pytest

---

## 1. Layout

| Path | Contains |
|---|---|
| `tests/test_agent.py` | TC-01..TC-09 — business semantics through the graph |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | The framework security gates are non-bypassable |
| `tests/proof_of_boundary/` | PB-1..PB-7 — architectural and security invariants |
| `tests/integration/test_invoke_endpoint.py` | End-to-end through the real HTTP entry point |

The model is stubbed by a client implementing the same `complete(messages)`
contract the platform clients implement. It records the messages it was handed,
so a test can assert on the text that actually reached the model rather than on
the text that was submitted. No live model call is made and no real pricing data
is used.

---

## 2. Test cases

| ID | Component | Case | Expected |
|---|---|---|---|
| TC-01 | `src/schemas/state.py` | State extends the framework state and does not shadow its class | AST checks pass |
| TC-02 | full graph | Consistent prices, accurate and defensible claim | overall PASS, well-formed report |
| TC-03 | `PriceConsistencyCheck` | Point-of-sale 980 against web 1080 | price consistency FAIL |
| TC-03 | full graph | Amounts chosen to be corrupted by common rounding schemes | every amount byte-identical in the report |
| TC-04 | regulatory determination | Unsubstantiated superlative claim | KH-002 cited, label FAIL |
| TC-05 | regulatory determination | Prize value one above the absolute ceiling | KH-004 cited, label FAIL |
| TC-06 | regulatory determination | Claimed rate disagreeing with the prices | KH-003 cited, label FAIL |
| TC-07 | full graph | Mixed batch | per-label verdicts correct, summary counts correct |
| TC-08 | full graph / `InputParse` | Japanese text in rules, assessments, shelf text and the model prompt | preserved throughout |
| TC-09 | construction | Backbone slots registered; output gate cannot be disabled; declared runtime values are live; an unimplemented profile is refused | as stated |

TC-09 includes a liveness pair worth naming: one case reads the tolerances back
out of the constructed nodes, and one changes a declared tolerance and asserts
the verdict changes with it. A configuration reader pointed at a file that no
longer holds the key leaves every declared value inert while the file itself
still parses, so only the second case can detect it.

---

## 3. Proof-of-Boundary

| ID | Boundary | Expected |
|---|---|---|
| PB-1 | The caller contract | Markup never reaches the model. Chat-template control tokens, directives split by markup, directives in field names and directives nested in a promotion spec are all refused. Ordinary promotional copy containing "act as", "now you are" and "system" is not. Label identifiers are locked to an inert alphabet; credential-shaped identifiers are refused at the door; rejected values are never echoed. Batch size and image labels are bounded. |
| PB-1b | Finite numbers | Bare `NaN`, `Infinity` and `-Infinity` JSON literals are refused for every numeric field. Real non-finite floats are refused by the parser. A non-finite rule parameter is reported for review, not passed — asserted at the rule verdict AND at the label roll-up the report renders, because asserting only the rule verdict is what let the roll-up turn it into a pass. Non-finite tolerances are refused at construction. Ordinary amounts still pass. |
| PB-2 | The output gate, layer 1 | Every credential shape the framework's detector knows is withheld. The withheld payload's key set is fixed, every value in it is a declared constant, and it is truthy. The error log names the finding class, never the value. A clean report is released with its determination. A report with no rule set to stand on is withheld rather than rendered. Every case supplies a rule set, so the report actually reaches the credential layer instead of being withheld earlier. |
| PB-2c | The output gate, layer 2 | The determination and the report ship in one delta and are cleared together. A report is refused when the determination is missing, incomplete, carries an unrecognised verdict, or is contradicted by the rendered verdict, the citations or the label status. Violations name a field path and never a caller value. Both fail-open defaults are pinned closed: an unapplied rule set and an unevaluable rule render REVIEW, never PASS. Clean-path controls prove a refuse-everything gate would not pass, and that the block happens at the output slot. |
| PB-2b | Containment | A credential travelling the data path never reaches the caller; the error envelope carries no released text, no traceback and no source path. The framework blocks one node earlier than this template's gate on that path, which is asserted explicitly rather than assumed. The path that does reach this template's gate — report content originating inside the assembling node — is driven end to end. Output resolution withholds a draft left beside an error status while still releasing the gate's notice. A clean control proves a refuse-everything gate would not pass. |
| PB-2d | The error contract is a closed set | Over every non-success path, each of three surfaces publishes only values this template declared: the node delta, the response `get_output()` resolves, and the invoke body of a real ASGI `POST /invoke`. The payload's key set is `{reason, note}`, its `reason` is one of `ERROR_REASONS`, and it is truthy on every path. `error_log` is seeded throughout with an upstream-shaped failure line (a name and a credential-shaped token inside an echoed response body, assembled at runtime) and every fragment is asserted absent from all three, walking nested mapping KEYS as well as values — while the line itself stays in `error_log`, once, not re-emitted. The violation location travels in `error_log` alone; no `error_log` key appears in the response; a report that is not a declared payload is not released even with `blocked` set. A raising model client is recorded by exception class and HTTP status, never by message, and never as an approval. A recognisable value driven through every `CallerInputError` path appears in no refusal message. Clean-path controls at all three surfaces. |
| PB-3 | Regulatory ceiling | Exactly at the ceiling passes; one above fails. |
| PB-4 | Import isolation | No file under `src/` reaches past the published framework surface. |
| PB-5 | State safety | No credential-shaped field name and no non-serializable annotation in the state definition. The persistence case is waived while checkpointing is off. |
| PB-6 | Structure | Backbone slot order holds; edges are owned by the base graph; `register_nodes` calls its parent; every node is a `FunctionNode` requiring a verified caller; no conditional routing is declared. |
| PB-7 | Trust | An unverified caller is denied before `execute()` runs; a verified caller is admitted. |

Three further cases sit outside the numbered grid: unparseable model output is
reported for review rather than approval; a deployment with no model configured
reports claims as needing review while still running every deterministic check
on real caller data; and an assessment call that RAISES is the same outcome
again — review, never a pass — with the exception's class and HTTP status
recorded and its message discarded.

---

## 4. Integration

Everything in `tests/integration/` goes through the application object a server
would serve. The distinction matters for this template: the entry node requires
a verified caller and nothing but the adapter establishes that in a standalone
deployment, so a test calling the graph directly proves the pipeline works while
only this one proves a deployed request can reach it.

| Case | Expected |
|---|---|
| Health | reports ready |
| A verified caller submits a batch | a real report computed from the submitted numbers |
| A discrepancy submitted over HTTP | found and reported |
| No token / wrong token | 401 with a body that does not say which |
| A non-ASCII authorization header, sent as raw bytes | 401, not a server error |
| The structured context channel | reaches the graph — an unimplemented profile sent over HTTP is refused |
| A credential in the context channel | 400 naming the field, never the value |
| A hostile context field name | reported by position, not echoed |
| Ordinary domain text on the same field | still passes |
| Refusal set against the framework block set | identical for every probed shape |
| An oversized context | 413 |
| A malformed batch | refused without echoing it |

---

## 5. Running the tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

The framework is installed from the platform wheel; the tests place only the
project root on the import path, so `src.*` resolves the same way it does in a
deployment.
