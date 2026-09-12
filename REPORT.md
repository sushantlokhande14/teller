# teller: design write-up

## 1. Architecture

One Python process per run, five modules with a clear order of dependency, and a
directory on disk as the only shared state between processes.

```
contract.json  ->  agent.py (the LLM)  ->  recorder.py  ->  capabilities/<id>.json
                        |                                          |
                   surface/ (Playwright)  <----------------  replay.py (no model)
                        |                                          |
                   policy.py (allowlist, risk, redaction)     handoff.py <-> operator.py
                        |
                   evidence.py (runs/<id>/log.jsonl, screens/, snapshots/, result.json)
```

**The contract comes first.** Before discovery runs, the caller declares what the
capability is for: a goal in plain language, typed inputs, typed outputs. The model
is asked only to work out *how*. That split is what makes the artifact a callable
capability rather than a transcript with a name. The recorder knows which value
became `{member_id}` and which cell became `savings_balance` because those names
existed before the run.

**One surface abstraction.** `Surface` turns what is on screen into an `Observation`
(numbered controls and cells, each with a role, an accessible name, a frame path, a
position, and for table cells the row and column it sits in) and executes actions.
Discovery acts by ref number from the latest observation; replay acts through
locators recorded in the artifact. Nothing above this layer knows what a DOM is. The
Playwright implementation computes names the way a screen reader would (aria, label,
button text, and for legacy table forms the text of the cell before it) and never
uses ids, classes or test hooks for identity. Models that accept images also get a
screenshot with the same numbers drawn on it.

**The model is a seam, not a dependency.** `Model` is a protocol with one method,
with three implementations: Anthropic, any OpenAI-compatible endpoint (local Ollama,
a hosted tier, OpenAI), and a scripted stand-in so the end-to-end tests are
deterministic and need no network. One canonical transcript format is translated per
provider, so the recorder, the redaction and the evidence see the same thing
whichever model drove the run. This began as a constraint, since I had no API budget,
and turned out to be the right shape anyway: a bank is unlikely to accept one
hard-wired vendor for the component driving its core systems, and the cheapest model
that can finish discovery is the right one, because no model is in the production
path at all. The evidence here was produced by **qwen2.5:7b running locally through
Ollama**, recorded in each capability's `provenance.model` and `provenance.endpoint`.
A 7B model is weaker than I would use in production, which turned out to be useful
(section 3).

**Code handles the known, the model handles the unknown.** Interstitials the app
profile already knows about are handled during discovery by the same code path replay
uses, before the model is asked. The model spends its turns on the task, and the
recording does not contain "click Acknowledge" as a step.

**A directory is the bus.** A run directory holds the log, screenshots, snapshots, the
result, and during a handoff the intervention request and reply. The operator CLI is a
separate process that reads that directory and attaches to the live browser over CDP.
No queue, no service, and a person could do the operator's job with a text editor.

Deliberate trade-offs: Playwright because role-based locators are the closest thing to
"what a person sees" a browser offers; a single process because the brief says scaling
infrastructure is not the point; a local sample app (`meridian/`) rather than a public
site, because I wanted framesets, table layouts, quirks mode, injectable session expiry,
permission denials, slow loads and application errors, and a second deployment of the
same product to test reuse against.

## 2. Artifact schema

A `Capability` (`teller/schema.py`) is JSON, versioned, and meant to be read in a code
review. The top level is the contract: `id`, `version`, `status` (`draft`,
`approved`, `retired`), `inputs`, `outputs`, `risk`, and `app` (which profile and
product version it was recorded against). Below that are `steps`, a `success`
checkpoint, capability-specific `outcomes`, and `provenance`.

**Targets carry several locators, in order of trust.** A `Target` describes a control
the way a person would (`describe`, `role`, `name`, `frame`) plus ordered `Locator`
strategies. The recorder picks the order from where the name actually came from: a
control named by a real label gets `role` first; one named by the cell before it, how
legacy forms are built, gets `row_label` first, because Playwright's accessible-name
computation will not see that label and a `role` lookup would miss. Table cells get a
`table_cell` locator (the row containing "Savings", under the "Current Balance"
column), which is how one capability found Elena's balance in a different row than
Priya's. A structural CSS path and the screen position come last, labelled brittle,
and the position is used only if policy allows coordinate fallback. Every locator
carries a `note` saying why it should hold.

**Every step has a checkpoint.** `expect` says what must be true afterwards: URL
pattern, visible heading, text fragments, or a control that must be visible. Replay
never assumes a click worked. Steps also record `on_url`, the page they were recorded
on, which is what makes recovery possible.

**Values are parameterized, not stored.** A `type` step's value is a literal or
`{"param": "member_id"}`. The recorder swaps concrete input values for `{name}` in
values, URL patterns, headings and descriptions, then turns any path segment still
carrying digits into `*`, because a segment we never supplied (an account number the
server just generated) differs next time. I found that the hard way: the first
sub-account replay failed its final checkpoint on the recorded account number. The
heading check still guards the state.

**Outputs are typed and say where they come from:** a type, a description for the
calling agent, a `source` target, and a `parse` rule. Money comes back as a decimal
string on purpose.

**Approval binds to a fingerprint.** `fingerprint()` hashes what decides a replay's
behaviour (inputs, outputs, steps, success, outcomes, app). Replay refuses a draft or
anything edited since approval. Provenance sits outside the hash so approving does
not invalidate itself.

`to_tool_schema()` renders the same artifact as a function-calling tool definition
(`teller catalog --tools`), so an agent can discover and invoke capabilities by name
with typed arguments. That stretch goal fell out of having a real contract.

## 3. Determinism & error handling

Replay is a fixed loop with no branch decided by a model:

1. observe; if a known condition is showing, handle it first;
2. check we are on the page the step was recorded on, otherwise go back;
3. resolve the target through its locators, first one yielding exactly one visible
   match wins, a fallback match is logged as `drift`;
4. policy check; act; for `type`, read the field back;
5. wait for the checkpoint, bounded by the step timeout, bailing early if a known
   condition appears;
6. if it fails, classify the screen.

The taxonomy is data, not code. A `Condition` has a detector (URL regex, text regex,
HTTP status, a visible control), a kind, and for recoverable ones a handler. The
result contract keeps the three kinds apart: `outcome` is a legitimate answer with a
code and the message from the screen (`not_found`, `validation_error`,
`permission_denied`); `recoverable` is handled and the step continues
(`session_expired` re-signs in, `system_notice` is dismissed, `host_busy` waits);
`fatal` stops with a screenshot, a snapshot, the step, what was expected and what was
observed. Anything unmatched is an unknown state and goes to a person (section 5).
Conditions live in the app profile, because "No member found" is knowledge about
Meridian rather than about one capability; a capability may add its own, checked first.

Recovery has a deliberate unit of retry: the *page group*, the run of steps recorded
on the same URL. Form state lives on a page, so after a recovery that moved us off it
the engine returns and re-runs the group from its first step, with a bound. Where the
recovery itself completed the step, the engine notices and moves on. Every retry,
recovery and drift lands in `StepResult`, so a result says not just "success" but
"success, after re-authenticating at step 1".

**What a weak model exposed.** The 7B completed the three-step read flow first time.
On the nine-step write flow it failed twice, and both failures were my design's fault:

* It reached for the `navigate` tool and guessed a URL instead of clicking the link
  two lines above it in the listing, so `navigate` is now withheld from discovery by
  default (`discovery.may_navigate`). The argument is not that a small model misused
  it: a recorded URL hop is the least portable thing a flow can contain, since paths
  are exactly what differs between tenants and versions, and a flow that clicks what a
  person clicks survives that. It stays available as a policy flag for genuinely
  unreachable deep links.
* It clicked ref 1 ("Home", in the navigation frame) when it wanted a link in the
  content frame, because a frameset's menu occupied the first ref numbers on every
  screen. Observations are now ordered controls before text and content frame first,
  split into "controls you can act on" and "text and table cells", which mirrors how a
  screen reader user moves through a page.

I would rather report that than re-run quietly until a transcript looked clean. Two
bugs I also introduced and fixed, both now pinned by tests: a loose validation
detector matched "Passwords must be changed every 90 days" on the password alert and
called it a validation outcome, and the card-number redaction pattern matched the
timestamp in run ids, scrubbing evidence paths out of results.

Drift is secondary here, as the brief says, but the artifact is built for it: several
locators per target, `drift` flagged per step, and `locator_used` recorded, so a fleet
could see which capabilities are one locator away from breaking before they break.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface` plus the shape of `Target`. A target
states role, name, frame path and strategies; a surface decides which it can honour.
Legacy web is the case I built against: framesets (the content frame is named in the
profile, every target carries its frame path), table layouts (`row_label`,
`table_cell`), no ids, quirks mode (headings detected by being bold and larger than
surrounding text, not by tag). A desktop app implements the same protocol over an
accessibility API such as UI Automation: `role` and `name` map onto accessibility
roles and names, `frame` becomes the window or pane path, `table_cell` becomes a grid
pattern lookup, and `bbox` becomes first-class rather than a last resort. The
perception script becomes a tree walk; recorder, replay, policy, handoff and evidence
do not change. The one genuinely new idea a desktop surface needs is checkpoints:
`url` means nothing there, so the surface would expose a window-title-plus-heading
signature and `Checkpoint` would grow one optional field.

**Multi-tenant reuse.** Three layers, each smaller than the one above:

* the *app profile* (`profiles/meridian.yaml`) is per vendor product: sign-in, which
  frame holds content, the taxonomy of known screens;
* the *capability* is recorded against a profile, not a tenant, and holds no URLs,
  credentials or tenant-specific text;
* the *tenant overlay* (`tenants/summit-credit-union.yaml`) is the diff: base URL,
  version, a renamed content frame, an extra branded interstitial. Overlay conditions
  replace base conditions with the same id, so a tenant whose session expiry looks
  different swaps one entry rather than re-recording.

One artifact refers to the content frame as `@main` rather than by the name this
instance happens to use, so an integrator who renamed or re-nested that frame is an
overlay line, not a re-recording. That symbolic reference is the smallest change that
made the tenant story real rather than asserted.

This is demonstrated rather than argued: the sample app also runs as Summit Credit
Union, a second institution on the same product with its own branding, release 4.4.02,
a content frame named `content`, a welcome banner after sign-in, and one renamed menu
item. `evidence/replay-second-tenant/` is the capability recorded against the base
deployment, replayed unchanged against Summit. The overlay absorbs the host, the frame
name and the banner. The renamed menu item is absorbed one level down, by the locator
fallbacks: the role and text locators miss, a structural locator holds, and the result
marks that step `drift` with `locator_used: css`. That is the signal worth acting on.
It says this tenant is running on a last-resort locator and needs an overlay entry
before the structure moves, rather than waiting for a production break to find out.
Across many tenants the thing to collect per (capability, tenant, version) is exactly
that pair, which strategy was used and how often a fallback was needed. The fields are
in every result today; the storage and reporting around them are not built.

## 5. Escalation & handoff

One live session per run, one controller at a time, and `ControlBroker` owns that
fact. Automation refuses to act while a person holds the session, and the state is
written to `control.json` so anyone can see who is in control.

**Detecting stuck.** Three explicit triggers: replay hit a screen no condition matches
(after retries and the checkpoint timeout); a risky step needs sign-off; the model
called `give_up` with `needs_human` during discovery. Timeouts are bounded everywhere,
so a hung page arrives here rather than hanging.

**Routing.** The broker writes `intervention.json` into the run directory: capability
and goal, the step, the URL and heading, what was expected and observed, a fresh
screenshot, the redacted inputs, the CDP endpoint, and instructions. Then it flips
control to human and waits, draining what the person does into the log as it waits.

**Taking over the same session.** The browser is the one automation opened. A person
can use its window, or attach any tool to the CDP endpoint; the scripted operator in
the evidence does that from another process, clicking "Remind Me Later" in the content
frame. Human actions are captured by a script installed in every frame before page code
runs, recording clicks, changes and submits by control description and never by typed
value, buffered in session storage so it survives the navigation a click causes.

**Handing back.** The reply says `resumed` or `aborted`, a note, and where to resume:
`verify` (re-check the checkpoint and continue if it holds), `next_step` (the person
finished it), or `retry_step`, where an approval resumes. Abort and timeout are
distinct failure codes carrying the operator's note, and the whole exchange is a
`Handoff` on the result, so the caller knows a person was involved.

Mocked: the operator UI is a terminal. Real: the request with context, the control
transfer, the shared session over CDP, the capture of what the person did, the resume
semantics, and the timeout.

## 6. Safety

One policy file, enforced in one function, called before every action by both
discovery and replay. Origins are allowlisted; paths can be allowlisted and blocked
(the sample app's test hooks and the sign-out link are blocked, so automation can
never touch them); action types are allowlisted; navigation targets are checked before
the navigation happens.

Risk is judged from what a person would read on the control (confirm, submit,
transfer, delete, close account) and where its form posts, and a risk level recorded
at discovery sticks at replay. The mode is the institution's call: `block`, `confirm`
(the default, signed off through the same handoff mechanism) or `allow`. Opening a
sub-account is the worked example: discovery stopped for approval before the confirm
click, the step and the capability are marked risky, and replay stops there too. A
declined approval ends the run before anything is posted.

Credentials come from the environment and are typed by the surface, never seen by the
model and never in an artifact. Inputs marked `sensitive` are never logged or stored;
the model writes `{name}` and the surface substitutes. A `Redactor` wraps every write
to disk with known secret values plus regex patterns for SSNs and card numbers.
Transcripts are saved with screenshots removed.

Limits, honestly: screenshots are stored as-is, so PII visible on screen sits in the
run directory. Regex redaction catches formats, not meaning, so a balance is not
redacted. The allowlist is by origin and path, not by what a page does, so a risky
action behind a harmless-looking link is caught only if its name or form action gives
it away. JavaScript dialogs are dismissed and logged rather than reasoned about. And
policy trusts the process it runs in: a hostile page cannot change it, a hostile
operator can.

## 7. Cuts

* **Desktop surface.** Designed for (section 4), not implemented.
* **Tenant fleet tooling.** Overlays and the per-step drift signal exist and are
  demonstrated on a second institution; storing and reporting those signals across many
  runs does not.
* **Operator console.** A terminal plus CDP. A web console would read the same files
  and attach to the same endpoint.
* **Screenshot masking**, as described in section 6.
* **Path minimization.** A model's detour is recorded as steps. The artifact is
  reviewable so a person can prune, and I would rather show the real path than
  silently edit it; automatic minimization (drop a step, replay, keep the drop if the
  checkpoints hold) is the obvious next feature.
* **Rescuing a discovery run.** When a person takes over a stuck discovery, their
  steps are logged but not folded into the capability, so a rescued run would have a
  hole in it. The run is marked failed instead, which is the safe behaviour; deriving
  recorded steps from captured human actions is real work, not a tweak.
* **Assisted fallback.** No bounded LLM recovery on replay failure. The handoff path
  covers those situations with a person, which I think is the right default for a
  bank; a single-step model recovery would plug in where `_unknown_state` calls the
  broker.
* **Stability scoring.** Replaying N times for a flakiness signal is a loop around what
  exists; not built.

Next, in order: screenshot masking for regulated data, path minimization, then the
cross-tenant drift signal, because those are what I would want before running this
unattended against a real core.
