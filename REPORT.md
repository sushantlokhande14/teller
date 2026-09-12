# teller: design write-up

## 1. Architecture

The system is one Python process per run, five modules with a clear order of
dependency, and a directory on disk as the only shared state between processes.

```
contract.json  ->  agent.py (Claude)  ->  recorder.py  ->  capabilities/<id>.json
                        |                                          |
                   surface/ (Playwright)  <----------------  replay.py (no model)
                        |                                          |
                   policy.py (allowlist, risk, redaction)     handoff.py <-> operator.py
                        |
                   evidence.py (runs/<id>/log.jsonl, screens/, snapshots/, result.json)
```

The pieces that matter most:

**The contract comes first.** Before discovery runs, the caller declares what the
capability is for: a goal in plain language, typed inputs, typed outputs. The
model is only asked to work out *how*. That split is what makes the artifact a
callable capability rather than a transcript with a name: the recorder knows
which typed value became `{member_id}`, and which cell the model read became
`savings_balance`, because those names existed before the run.

**One surface abstraction.** `Surface` turns whatever is on screen into an
`Observation` (a numbered list of controls and cells, each with a role, an
accessible name, its frame path, its position, and for table cells the row and
column it sits in) and executes actions. Discovery acts by ref number from the
latest observation; replay acts through locators recorded in the artifact.
Nothing above this layer knows what a DOM is. The Playwright implementation
computes names the way a screen reader would (aria, label, button text, and for
legacy table forms the text of the previous cell), and never uses ids, classes or
test hooks for identity. The model also gets a screenshot with the same numbers
drawn on it, so the text list and the picture agree.

**Code handles the known, the model handles the unknown.** During discovery,
interstitials the app profile already knows about (a maintenance notice, a
dropped session) are handled by the same code path replay uses, before the model
is asked. The model spends its turns on the task, and the recording does not
contain "click Acknowledge" as a step.

**A directory is the bus.** A run directory holds the log, the screenshots, the
snapshots, the result, and during a handoff the intervention request and the
reply. The operator CLI is a separate process that reads and writes that
directory and attaches to the live browser over CDP. No queue, no service, and a
person can do the operator's job with a text editor if they have to.

Trade-offs I made on purpose: Python and Playwright because they are what I can
defend line by line and Playwright's role-based locators are the closest thing to
"what a person sees" that a browser offers. A single process because the brief is
explicit that scaling infrastructure is not the point. A local sample app
(`meridian/`) instead of a public site, because I wanted framesets, table
layouts, quirks mode, and the ability to inject session expiry, permission
denials, slow loads and application errors on demand. Building it taught me
things I would not have learned on a clean demo site (see section 3).

## 2. Artifact schema

A `Capability` (`teller/schema.py`) is JSON, versioned, and meant to be read in a
code review. The top level is the contract: `id`, `version`, `status`
(`draft`, `approved`, `retired`), `inputs`, `outputs`, `risk`, and `app` (which
profile and product version it was recorded against, and the entry page).
Underneath are the `steps`, a `success` checkpoint, capability-specific
`outcomes`, and `provenance` (which model, which run, who approved and when).

**Targets carry several locators, in order of trust.** A `Target` is a control
described the way a person would describe it (`describe`, `role`, `name`,
`frame`) plus an ordered list of `Locator` strategies. The recorder chooses the
order from where the name actually came from: a control named by a real label
gets `role` first; a control named by the cell before it (how legacy forms are
built) gets `row_label` first, because Playwright's accessible-name computation
will not see that label and a `role` lookup would miss. Table cells get a
`table_cell` locator (the row that contains "Savings", under the "Current
Balance" column), which is how the same capability found Elena's savings balance
in a different row than Priya's. A structural CSS path and the screen position
are recorded last, labelled as brittle, and the position is only used if policy
allows coordinate fallback. Each locator has a `note` saying why it should hold.

**Every step has a checkpoint.** `expect` says what must be true afterwards: the
main frame's URL pattern, the visible heading, text fragments, or a control that
must be visible. Replay never assumes a click worked. Steps also record `on_url`
(the page they were recorded on), which is what makes recovery possible: after a
re-login lands somewhere else, the engine knows where to go back to.

**Values are parameterized, not stored.** A `type` step's value is either a
literal or `{"param": "member_id"}`. The recorder swaps concrete input values for
`{name}` placeholders in values, URL patterns, headings and descriptions. It also
turns path segments that still carry digits after that swap into `*`, because a
segment we never supplied (the account number the server just generated) will be
different next time. I found this the hard way: the first sub-account replay
failed its last checkpoint because the recorded URL ended in the account number
from the recording. The heading check still guards the state.

**Outputs are typed and say where they come from.** `outputs.savings_balance` has
a type (`money`), a description for the calling agent, a `source` target, and a
`parse` rule. Money comes back as a decimal string, on purpose.

**Approval binds to a fingerprint.** `fingerprint()` hashes the parts that decide
what a replay does (inputs, outputs, steps, success, outcomes, app). `approve`
stores it; replay refuses a draft or anything edited since approval unless told
otherwise. Provenance is outside the hash so approving does not invalidate itself.

`to_tool_schema()` renders the same artifact as a function-calling tool definition
(`teller catalog --tools`), so an agent can discover and call capabilities by
name with typed arguments. That was the cheapest stretch goal and it fell out of
having a real contract.

## 3. Determinism & error handling

Replay is a fixed loop with no branching decided by a model:

1. observe; if a known condition is showing, handle it first;
2. check we are on the page the step was recorded on, otherwise go back to it;
3. resolve the target through its locators, first one that yields exactly one
   visible match wins; a fallback match is logged as `drift`;
4. policy check; act; for `type`, read the field back;
5. wait for the checkpoint (poll, bounded by the step's timeout, bail early if a
   known condition appears);
6. if the checkpoint fails, classify the screen.

The taxonomy is data, not code. A `Condition` has a detector (URL regex, text
regex, HTTP status, a visible control), a kind, and for recoverable ones a
handler. The kinds are the three the brief asks for, and the result contract
keeps them apart: `outcome` is a legitimate answer with a code and the message
from the screen (`not_found`, `validation_error`, `permission_denied`);
`recoverable` is handled and the step continues (`session_expired` re-signs in,
`system_notice` is dismissed, `host_busy` waits); `fatal` stops with a
screenshot, a snapshot, the step, what was expected and what was observed
(`app_error`). Anything unmatched is an unknown state and goes to a person
(section 5). Conditions live in the app profile because "No member found" is
knowledge about Meridian, not about one capability; a capability can add its
own, and they are checked first.

Recovery has a deliberate unit of retry: the *page group*, the run of steps
recorded on the same URL. Form state lives on a page, so after a recovery that
moved us off it (a re-login that redirected to a GET of a POST route and got a
405, which my sample app does because plenty of real ones do), the engine goes
back to the page and re-runs the group from its first step, with a bound on how
many times. Where the recovery itself completes the step (the interstitial
redirected onward), the engine notices and moves on without redoing anything.
Every retry, recovery and drift ends up in `StepResult`, so the result says not
just "success" but "success, after re-authenticating at step 1".

Two things I got wrong while building, both fixed and both now covered by
tests. The generic validation detector matched "Passwords must be changed every
90 days" on the surprise alert and reported it as a validation outcome; the fix
was to make detectors specific, and the test that pins it says why. The card
number redaction pattern matched the timestamp in run ids and scrubbed evidence
paths out of results; the pattern is tighter now and there is a test that run
ids survive redaction.

Drift is secondary here, as the brief says, but the artifact is built for it:
several locators per target, `drift` flagged per step in the result, and the
`locator_used` field, so a fleet could see which capabilities are one locator
away from breaking before they break.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface` plus the shape of `Target`. A
target says role, name, frame path, and a list of strategies; a surface decides
which strategies it can honour. A legacy web app is the case I actually built
against: framesets (the content frame is named in the profile and every target
carries its frame path), table layouts (`row_label`, `table_cell`), no ids
(nothing depends on them), quirks mode (headings are detected by being bold and
larger than the text around them, not by tag). A desktop app would implement the
same protocol over an accessibility API (UI Automation on Windows) plus
screenshots: `role` and `name` map directly onto accessibility roles and names,
`frame` becomes the window or pane path, `table_cell` becomes a grid pattern
lookup, and `bbox` becomes a first-class strategy instead of a last resort. The
perception script would be replaced by a tree walk; the recorder, replay engine,
policy, handoff and evidence would not change. The one place a desktop surface
would need a new idea is checkpoints: `url` has no meaning there, so a surface
would expose a window-title-plus-heading signature in its place, and the
`Checkpoint` model would grow one optional field.

**Multi-tenant reuse.** Three layers, each smaller than the one below:

* the *app profile* (`profiles/meridian.yaml`) is per vendor product: how to sign
  in, which frame holds content, and the taxonomy of known screens;
* the *capability* is recorded against a profile, not a tenant, and contains no
  URLs, credentials or tenant-specific text (input values and generated ids are
  parameterized out);
* the *tenant overlay* (`tenants/example-credit-union.yaml`) is the diff: this
  institution's base URL and version, a different sign-in form, an extra branded
  interstitial. Overlay conditions replace base conditions with the same id, so a
  tenant whose session expiry looks different swaps one entry.

Drift between tenants and versions shows up as `drift` and recovered steps in
replay results. With many tenants running the same capability, the signal I
would collect per (capability, tenant, version) is which locator strategy was
used per step and how often a fallback was needed; a tenant whose primary
locators keep missing needs an overlay, not a re-recording. What I did not
build is any of the storage or reporting for that; the fields exist in every
result today.

## 5. Escalation & handoff

There is one live session per run and one controller at a time, and the
`ControlBroker` owns that fact. Automation refuses to act while a person holds
the session (`assert_automation`), and the state is written to `control.json`
so anyone can see who is in control.

**Detecting stuck.** Three triggers, all explicit: replay hit a screen no
condition matches (after retries and after the checkpoint timeout); a risky step
needs sign-off; the model called `give_up` with `needs_human` during discovery.
Timeouts are bounded everywhere so a hung page ends up here rather than hanging.

**Routing.** The broker writes `intervention.json` into the run directory: the
capability and goal, the step (id, action, target description), the URL and
heading, what was expected and what was observed, a fresh screenshot, the
redacted inputs, the CDP endpoint of the live browser, and instructions. Then
it flips control to human and waits, draining what the person does into the log
while it waits.

**Taking over the same session.** The browser is the one the automation opened.
A person can use its window directly, or attach any tool (DevTools, another
Playwright) to the CDP endpoint. The scripted operator used in the evidence
does exactly that from a separate process: it connects over CDP, clicks
"Remind Me Later" in the content frame, and replies. Human actions are captured
by a script installed in every frame before any page code runs; it records
clicks, changes and submits with control descriptions (never typed values) into
session storage so the buffer survives the navigation a click causes. The
evidence shows `input "Remind Me Later" in frame main`, the form submit, and the
navigation, in order.

**Handing back.** The reply says `resumed` or `aborted`, a note, and where to
resume: `verify` (re-check the current step's checkpoint and continue if it
holds), `next_step` (the person finished the step), or `retry_step`. An approval
resumes at `retry_step`, which is the step that asked. Abort and timeout are
distinct failure codes with the operator's note attached. The whole exchange,
including captured actions, is a `Handoff` on the result, so the caller knows a
person was involved.

What is mocked: the operator UI is a terminal. What is real: the request with
context, the control transfer, the shared session over CDP, the capture of what
the person did, the resume semantics, and the timeout.

## 6. Safety

One policy file, enforced in one function, called before every action by both
discovery and replay. Origins are allowlisted; paths can be allowlisted and
blocked (the sample app's test hooks and the sign-out link are blocked, so the
automation can never touch them); action types are allowlisted; navigation
targets are checked before the navigation happens.

Risk is decided from what a person would read on the control (a name matching
confirm, submit, transfer, delete, close account) and where its form posts, and
a risk level recorded at discovery sticks at replay. The mode is the
institution's call: `block`, `confirm` (a person signs off through the same
handoff mechanism; the default) or `allow`. Opening a sub-account is the worked
example: discovery stopped for approval before the confirm click, the recorded
step is marked risky, the capability is marked risky, and replay stops there
too. A declined approval ends the run before anything is posted.

Sensitive data: credentials come from the environment and are typed by the
surface, never seen by the model and never in an artifact. Inputs marked
`sensitive` are never logged or stored; the model is told to write `{name}` and
the surface substitutes. A `Redactor` wraps every write to disk (log lines,
snapshots, transcripts, results) with the known secret values plus regex
patterns for SSNs and card numbers. Capabilities carry example values only for
non-sensitive inputs. The model transcript is saved with screenshots removed.

Limits, honestly: screenshots are stored as-is, so PII visible on screen is on
disk in the run directory (masking by region, or by the surface blanking cells
marked sensitive in the profile, is the next step). Regex redaction catches
formats, not meaning: a balance is not redacted. The allowlist is by origin and
path, not by what a page does; a risky action reachable through a "safe"-looking
link is caught only if its name or form action gives it away. JavaScript dialogs
are dismissed and logged rather than reasoned about. And the policy trusts the
process it runs in; a hostile page cannot change it, but a hostile operator can.

## 7. Cuts

Cut on purpose, with the seam in place:

* **Desktop surface.** The `Surface` protocol and `Target` shape are designed for
  it (section 4); nothing is implemented.
* **Tenant fleet tooling.** Overlays and the per-step drift signal exist; storing
  results across runs and reporting on them does not.
* **Operator console.** A terminal plus CDP. A web console would read the same
  files and attach to the same endpoint.
* **Screenshot masking**, described above.
* **Path minimization.** If the model wanders during discovery, the detour is
  recorded as steps. The artifact is reviewable so a person can prune, and I
  would rather show a reviewer the real path than silently edit it; automatic
  minimization (drop a step, replay, keep the drop if the checkpoints still
  hold) is the obvious next feature.
* **Assisted fallback.** No bounded LLM recovery on replay failure. The handoff
  path covers the same situations with a person instead, which I think is the
  right default for a bank; a model-driven single-step recovery would plug in
  where `_unknown_state` calls the broker.
* **Stability scoring.** Replay N times and report flakiness is a loop around
  what exists; not built.

What I would build next, in order: screenshot masking for regulated data, then
path minimization, then the drift dashboard across tenants, because those are
the three things I would want before running this unattended against a real
core.
