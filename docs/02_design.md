# Retail Price Label and Promotion Verification Agent — Design

**Template ID:** RET-C2-017
**Pattern × Industry:** Verification × RET (retail)
**Category:** Cat 2

---

## 1. What this agent is for

A price label goes wrong in three ways, and they need three different kinds of
check. The price can disagree with itself — the shelf says one thing, the till
another, the website a third. The promotional claim can overstate the discount
the prices actually deliver. And the offer can breach an advertising rule
regardless of whether the arithmetic is right.

This agent takes a batch of labels and answers all three, returning a verdict
per label with the rule cited and a remediation suggestion. The output is
designed to be acted on rather than interpreted: a category manager reading it
should know which label to change and what to change about it.

Two design choices follow from the domain and are worth stating up front.

**Amounts are reported exactly as submitted.** There is no rounding grid on this
output and there must not be one. The report exists to demonstrate that two
displayed prices differ; an amount rounded before it is rendered can no longer
demonstrate the difference it was rendered to demonstrate. This is the inverse
of the invariant a reporting agent would carry, and it is enforced by test —
`TC-03` drives amounts chosen to be corrupted by the common rounding schemes (a
five-digit run, a comma-grouped value, a decimal fraction, a small value beside
a three-letter code) and asserts each appears byte-identically.

**The rules are data, not code.** They live in `config/keihin_rules.yaml` and are
loaded at run time. Amending the regulations means editing that file. Nothing in
`src/` encodes a specific rule; the compliance node dispatches on the
`check_type` each rule declares.

### 1.1 Identity

| Field | Value |
|---|---|
| Template ID | RET-C2-017 |
| Pattern x Industry | Verification x RET |
| Category | Cat 2 |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Generation mode | llm — one model call per label carrying a promotional claim |

---

## 2. Architecture

### 2.1 Backbone slots

The agent fills the three middle slots of the framework backbone. The backbone
owns its own edges, so this template does not define any; there is no
conditional routing at all, which is deliberate — a conditional path callable
annotated with anything but the graph's own `State` would have the engine
project away every field the annotation omits, and the branch condition would
read as absent on every real invocation while unit tests kept passing.

```
initialize
    |
pre_process    InputParse  ->  RuleLoad
    |
main           PriceConsistencyCheck  ->  PromotionClaimCheck
    |
post_process   ResultAssemble
    |
finalize
```

Each slot runs its sub-nodes inline and stops at the first one that returns an
error, so a refusal keeps its status all the way to `finalize`. A slot that
receives an already-failed state passes it through untouched: the backbone
visits every slot in order, so without that guard a later slot would overwrite
an earlier refusal.

#### The regulatory determination is not a graph layer

It used to be one — a `RegulatoryComplianceCheck` node ahead of `ResultAssemble`
in the post-process slot. That form is wrong twice over. It is prohibited:
`_security_gate_output()` is the mandatory output-security mechanism on this
template and a separate compliance-check node standing beside it is an
anti-pattern under the platform's mandatory architecture rules. And it was
bypassable in fact — a layer is enforced only by the topology that contains it,
so any path reaching the output without traversing it ships un-determined
content. Measured on the shipped pipeline, with that one registration removed
and nothing else changed:

| | shipped | layer dropped |
|---|---|---|
| envelope status | `success` | `success` |
| determination | FAIL, KH-001 / KH-004 / KH-005 | absent — the report omitted the key |
| the document says | FAIL, with remediation for all three | `PASS`, no citation, no remediation |

The label was one whose advertised reference price did not exceed the price
actually charged, carrying a prize draw five times the statutory ceiling.

The rules therefore live in `src/nodes/regulatory_determination.py` as a module,
not a node. `ResultAssemble` evaluates them and returns the determination in the
**same delta** as `verification_report`, so a report and the determination it
rests on cannot exist apart, and its output gate re-checks the contract between
the two. `post_process` is a fixed backbone slot that the base graph routes
every successful invocation through, so a check placed there cannot be routed
around the way a graph layer could.

### 2.2 Node responsibilities

| Node | Responsibility | Reads from `config/config.yaml` |
|---|---|---|
| `InputParse` | Owns the caller contract. Decodes the batch, screens it, validates every field, normalizes prices, strips markup from text bound for the model. | `accept_image_input` |
| `RuleLoad` | Selects and publishes the rule set for the requested regulatory profile. | `rules_path`, `regulatory_profile`, `enable_keihin_check` |
| `PriceConsistencyCheck` | Deterministic. Pairwise price comparison and the discount-implied price check. | `price.tolerance_abs`, `price.tolerance_pct` |
| `PromotionClaimCheck` | Model-assisted assessment of claim accuracy and defensibility — the one judgement that is genuinely a judgement. | `llm` |
| `ResultAssemble` | Evaluates the loaded rule set, builds the report, and applies the output gate — determination and document in one delta. | `report.note`, `price.discount_rate_tolerance` |

`regulatory_determination.py` sits beside the nodes but is a module, not a node:
it holds the rule dispatch, the report vocabulary the renderer and the gate
share, and the output-boundary contract between them.

### 2.3 Services

`src/services/` holds the two things nodes need but should not implement:

- `rule_library.py` — loads and structurally validates the rule set once, at
  construction, so a malformed library surfaces when the process boots rather
  than on a caller's request.
- `validation.py` — the finite-number parser, the inert-identifier check, the
  credential screen and the injection screen. Kept in one place because these
  are guarantees about the whole boundary, not conveniences for one node.

### 2.4 State schema (`src/schemas/state.py`)

A flat `TypedDict` extending the framework state. Every structured payload is
carried as a JSON string: graph checkpoints use msgpack serialization, which
silently corrupts richer objects. No credentials are ever placed in state.

| Field | Carries |
|---|---|
| `labels_input` | the normalized label batch |
| `regulatory_profile` | the profile actually applied |
| `loaded_rules` | the active rule set |
| `price_findings` | per-label price-consistency results |
| `promotion_findings` | per-label claim assessments |
| `regulatory_findings` | per-label rule verdicts — written only in the same delta as `verification_report`, and cleared with it |
| `verification_report` | the assembled report |
| `blocked` | true when the output gate withheld the report |

### 2.5 Configuration split

`config/agent.yaml` is the registry's discovery record — identity, entry point,
required trust level, declared secrets and extras. It holds no tunable values.
`config/config.yaml` holds the runtime parameters and is loaded separately and
passed to the graph constructor. The standalone HTTP adapter loads the same file
by the same route, so a declared value is live in both deployments rather than
live in one and inert in the other.

The model client arrives already constructed on `config["llm"]`, which makes the
provider a deployment decision. A deployment with no model key still boots and
still runs every deterministic check; each promotional claim is then reported as
needing review. It is never reported as approved — an agent that could not
assess a claim must not answer as though it had.

A call that RAISES is the same outcome and is handled the same way: the label is
recorded as needing review, and the run continues. This is also the template's
only third-party call, so it is the one place an upstream response body can
enter the process — a provider exception's message quotes the body it came from,
and a body carries identifiers, echoed request fields and occasionally a
credential. Unhandled, the exception reaches the framework's own error path,
which writes `str(exc)` and a full traceback into `error_log`; the closed-set
rule therefore applies at the call site, not only at the response. What is
recorded is the exception's class and, when the client carries one, the HTTP
status — both bound to locals before anything is formatted, so no caught
exception ever reaches a message.

---

## 3. The caller contract

`POST /invoke` takes the batch as a JSON string in `input` and structured
parameters in `input_context`.

```json
[
  {
    "label_id": "AEON-2026W23-0012",
    "shelf_label_text": "本日限り 最大30%オフ",
    "pos_price": 980,
    "web_price": 980,
    "regular_price": 1400,
    "sale_price": 980,
    "promotion_spec": {
      "claim": "最大30%オフ",
      "discount_rate": 0.30,
      "period_start": "2026-06-01",
      "period_end": "2026-06-01"
    }
  }
]
```

Every label needs a `label_id` and at least one price field. Everything else is
optional; nothing is trusted.

### 3.1 Numbers are finite and bounded

Every caller-controlled number — the four price fields, `discount_rate`,
`prize_value`, `transaction_amount` — is parsed through a bounded check that
rejects booleans, non-numerics, NaN, both infinities, and out-of-range
magnitudes. The NaN case is the one that matters most and is the least visible:
it does not raise, and every comparison against it evaluates false, so an
unchecked NaN answers "within tolerance" to the exact question this agent exists
to decide, and the label ships.

Non-finite values travel two routes and both are closed. Over the wire they can
only arrive as the bare JSON literals `NaN`, `Infinity` and `-Infinity`, which
the decoder accepts by default and turns into real non-finite floats — so the
batch is decoded with those literals refused outright. Inside the process they
arrive as real floats from configuration and from rule parameters, which is why
the tolerances are parsed at construction and each rule parameter is parsed
before it is compared against. A non-finite ceiling would pass every prize on
every label, and the verdict it produced would be shaped exactly like a genuine
pass.

### 3.2 Strings that reach the report are inert

`label_id` is echoed back in the report, so it is locked to a short ASCII
identifier alphabet rather than accepted as free text. That alphabet is not, by
itself, protection: letters, digits and dashes is also the shape of an API key.
So every value in a label is additionally screened for credential shapes using
the framework's own detector — refusing at the door names the field the caller
got wrong, where leaving it to the output gate returns a withheld report and no
indication of the cause.

Rule names, rule details and the model's own assessment are not caller-supplied
and keep their full character set. Japanese text renders in the report exactly
as the rule library states it.

### 3.3 Free text is screened twice

Text fields are screened for prompt injection as received, and again with markup
removed. Both passes are needed and each catches what the other cannot.

Stripping markup is not refusal. The tag stripper removes `<|im_start|>` as
though it were an HTML tag and forwards whatever directive followed it as
ordinary prose — turning a detectable token attack into an undetectable one. The
raw pass exists for that. Conversely, `ig<b>nore all previous instructions`
contains no contiguous directive until the markup is gone, which is what the
second pass is for.

Chat-template control tokens are screened as a class — `<|…|>`, `[INST]`,
`<<SYS>>` — rather than as a list of known phrases, because the delimiters
themselves are the signal and no legitimate price label contains one. Directive
phrases are anchored to a verb plus an explicit reference to earlier
instructions. Anchoring matters in both directions: this agent's entire purpose
is to read promotional copy, and copy legitimately contains "act as", "now you
are" and "system". Screens are probed against real wording in both directions —
attacks refused, ordinary copy unaffected.

The screen walks keys as well as values, and runs after parsing, so a directive
placed in a field name is caught and no wire-format escaping can evade it.

### 3.4 Structural bounds

At most 500 labels per batch; text fields capped at 2,000 characters, period
strings at 64; amounts bounded above and non-negative. The HTTP adapter caps the
serialized `input_context` at 256 KB before it reaches the graph at all.

### 3.5 Errors name fields, never values

A refusal identifies the field that failed and stops there. The rejected value
is never written into the error log, where it would outlive the request that was
refused.

---

## 4. The rule engine

`regulatory_determination.py` dispatches on each rule's `check_type`:

| `check_type` | Evaluated as |
|---|---|
| `price_reference` | The advertised reference price must genuinely exceed the sale price. |
| `claim_substantiation` | Reuses the assessment already produced for the claim, rather than asking a model the same question twice. |
| `discount_accuracy` | Deterministic: the claimed rate against the rate the prices imply, within `price.discount_rate_tolerance`. |
| `prize_limit` | Prize-value ceilings, driven entirely by the rule's own parameters. |

An unrecognised `check_type`, or a rule whose parameters do not parse, is
reported for review. It is never silently skipped and never counted as a pass —
and unlike the previous roll-up, that now holds at the label level too, which is
the level the report renders. See §5.2.

A rule set that was never applied is likewise a REVIEW, never a pass. Setting
`enable_keihin_check: false` publishes zero rules; every label then trivially
satisfied "no rule failed" and was rendered `"regulatory": "PASS"`. An empty rule
set is now rendered as an unapplied rule set, with the reason in the label's
remediation list.

The shipped rule set covers reference-price honesty, substantiation of
superlatives, discount-rate accuracy and three prize-value ceilings. It is a
starting point: replace it with the rule set that applies in your market.

Only the `JP` profile is implemented. Another profile is refused rather than
quietly falling back to a default the caller did not ask for.

Image labels are out of scope. When `accept_image_input` is false — the default
— a label carrying an image field is refused rather than processed as though the
image had been read.

---

## 5. Security

| Concern | Where it is enforced |
|---|---|
| Caller authentication | The HTTP adapter. The pipeline's entry node requires a verified caller, and nothing else establishes trust in a standalone deployment. |
| Input validation | `InputParse` and `src/services/validation.py` — see §3. |
| Prompt injection | Screened raw and post-sanitize, keys included, in the node that owns the caller contract — not left to the framework alone, so the guarantee holds wherever the framework gate is configured off. |
| Credential shapes in caller data | Screened at the adapter for the structured context channel and in `InputParse` for the batch, both using the framework's own detector. |
| Output gating | `ResultAssemble` — one choke point, two layers: the framework's own credential detector over every payload it returns, then the regulatory determination contract. |
| Audit trail | Every node emits a domain event, including on its refusal paths. Findings are recorded by class, never by value. |

### 5.1 Why the context channel is screened at the adapter

The backbone's first node copies the caller's structured context verbatim into
its own result, and the framework's output gate scans every value of every node
result and raises on a credential shape. So a credential-shaped string anywhere
in that context makes the *first* node fail, before any of this template's code
runs, and the caller receives an error naming nothing.

The request cannot succeed either way. Screening at the adapter does not change
what is accepted — it turns an opaque failure into one the caller can act on.
The screen calls the same detector the framework's gate calls, on the same
assembled object, so what is refused at the door and what is blocked at the exit
are one set by construction. Scanning field by field composes exactly to scanning
the whole mapping, and that identity is what allows the refusal to name the
offending field without widening or narrowing what is blocked. It is pinned as a
property test rather than assumed.

Field names are caller data too, so a name is echoed only when it is short,
inert and carries no credential shape of its own; otherwise the field is
reported by position.

### 5.2 The output gate, and what it is actually for

`ResultAssemble`'s gate is one choke point with two layers. On a violation in
either, it returns an error status, replaces the report with the closed-set
payload built by `_contain()`, clears the determination, and records only the
LOCATION of the violation — a credential class, or a field path — in
`error_log`. Never a matched value: echoing one would put it back into this
node's own result, where the framework's own final gate raises, and a raise
discards the whole delta, containment included.

**Layer 1 — credentials.** Scans the rendered report and the determination with
the framework's detector. Both, not only the report: the determination is
returned in the same delta and a rule detail quotes the promotion assessment, so
a value the report happens not to render still reaches the framework's scan
through that field. The scanned set is pinned as an inventory against the keys
the node actually returns.

**Layer 2 — the regulatory determination contract.** A report may not ship
unless a determination of record exists for every label, is complete, carries
recognised verdicts, and agrees with the document: the rendered
`checks.regulatory` matches it, every failing rule it found is cited, and a
label it did not pass is not rendered as `PASS`. This is what replaces the
deleted graph layer, and it sits on a fixed backbone slot rather than in
topology, so it cannot be routed around.

Four properties of the payload are load-bearing:

- **It is truthy.** Output resolution returns the first non-empty field it
  finds, so "withheld" expressed as an empty string produces the exact leak the
  substitution was written to prevent.
- **Every output-bearing field is overwritten**, not merely omitted. An absent
  field falls through to whatever the surrounding machinery finds next.
- **Its key set is fixed** — a `reason` code and the notice — and pinned by
  test, so a future field carrying answer text cannot quietly join the withheld
  response. No count travels in it: the size of the refused batch is an outcome
  signal and goes to the audit event. The pass / fail / review counts are
  deliberately absent for a second reason too — when layer 2 refuses because the
  document disagrees with the determination, those counts are the numbers just
  declared untrustworthy.
- **Every value is a constant this module declared.** `reason` is one of
  `ERROR_REASONS` (`regulatory_rules_unavailable`, `output_withheld_by_gate`)
  and `note` is the fixed withheld notice. Nothing is read out of state: not the
  determination, not `error_log`, not the gate's own violation string.
  Node-authored error text can embed an upstream response body, an identifier or
  a caller fragment, and truncating or redacting such a string is not a closed
  set — the only way to bound the channel is to publish values the template
  chose. `error_log` stays the INTERNAL channel: the state reducer appends to it
  and the audit trail needs it, and it is never projected to the caller.

The detector is the framework's rather than a local list, and that is not a
stylistic choice. A local list narrower than the framework's would let a value
pass this gate, trip the framework inside the same call, and have the
framework's error path discard the node's whole result — including the
substitution made here. A detector gap is not a missed finding; it is a
containment bypass.

**Where this gate actually fires.** Measured through `/invoke`: on most paths it
does not, because the framework scans each node's result as that node returns
it, so a credential in a model assessment is caught inside `PromotionClaimCheck`
and the post-process slot never runs. The path that does reach this gate is
report content that no upstream node ever returned — the note read from
configuration and joined into the report inside the assembling node. That path
is driven end to end in the boundary suite. Recording this distinction keeps the
boundary honest: containment is real on both paths, but only on one of them is
it this template's gate providing it.

Layer 2 is different in kind. It has no upstream that could pre-empt it: it runs
on every successful invocation, because it is a property of the artefact this
node itself assembles.

### 5.3 Output resolution

The framework resolves its caller-visible output without consulting the status,
so a populated output field beside an error status is still delivered. This
agent's `get_output` closes that on a closed set rather than on a flag: on any
non-success outcome the only thing released is the payload `_contain()` built —
a declared reason code and the fixed notice — and an assembled report is never
surfaced beside an error status. Membership is checked at the boundary, by
reading the payload, rather than trusted from the node four steps upstream, so a
future path that sets `blocked` while leaving a draft in state publishes
nothing. `error_log` is never consulted there and no `error_log` key appears in
the response.

Both layers are measured separately in the boundary suite, because layered
guards otherwise mask one another and each looks decorative when removed alone.

### 5.4 Invariants

- `security.output_gate_enabled` must be true; construction fails if it is not.
- A report never ships without the determination it rests on, and never
  contradicts it. Neither exists in state without the other.
- No verdict is affirmative by default. A rule set that was not applied, a rule
  that could not be evaluated, and a determination that is missing all render as
  REVIEW — never as a pass.
- The rules live in configuration, never in `src/`.
- Nothing in `src/` reaches past the published framework surface.
- Amounts are reported exactly as submitted.

---

## 6. Test matrix

Test cases cover business semantics; boundary tests cover the architectural and
security invariants. See `docs/03_test_spec.md` for the full specification.

| ID | Scope |
|---|---|
| TC-01 | State contract — extends the framework state, no shadowing |
| TC-02 | A clean label batch passes with a well-formed report |
| TC-03 | Price discrepancy is found; amounts render exactly |
| TC-04 | An unsubstantiated superlative is cited |
| TC-05 | A prize above the ceiling is cited |
| TC-06 | A claimed rate disagreeing with the prices is cited |
| TC-07 | A mixed batch resolves per label with correct counts |
| TC-08 | Japanese text survives in the report, the rules and the model prompt |
| TC-09 | Construction, configuration liveness, and profile refusal |

| ID | Boundary |
|---|---|
| PB-1 | The caller contract refuses hostile input and accepts ordinary copy |
| PB-1b | Every caller number is finite and bounded, on both arrival routes |
| PB-2 | The output gate covers every shape the framework knows |
| PB-2b | Containment — no draft, traceback or path travels in an error envelope |
| PB-3 | Regulatory boundary values, on and either side of the ceiling |
| PB-4 | Nothing in `src/` reaches past the published framework surface |
| PB-5 | State is msgpack-safe and carries no credential-shaped field |
| PB-6 | Backbone slot order, structure, and the absence of conditional routing |
| PB-7 | The trust boundary admits a verified caller and refuses an anonymous one |

---

## 7. Dependencies

- The AgentCore framework — `AgentBaseGraph`, `FunctionNode`, the state and
  trust schemas, the credential detector, the audit logger
- `pyyaml` — configuration and rule-library loading
- `fastapi` / `pydantic` — the HTTP adapter
- pytest, ruff, mypy for development

No vector store and no retrieval corpus: the rules are configuration, not a
knowledge base.
