"""
The capability artifact and the result contracts around it.

A Capability is the thing a discovery run produces and a replay consumes. It is
the contract between four parties: the model that recorded it, the replay engine
that runs it, the human who reviews and approves it, and the agent that calls it.
Everything in here is plain data (pydantic v2) so it serializes to JSON and can
be diffed in a code review.

Design notes live in REPORT.md. The short version:

* A Target describes a control by what a human sees (role + name), and carries an
  ordered list of Locators so replay can degrade gracefully instead of failing on
  the first miss. Nothing here depends on ids, classes, or test hooks.
* Steps carry an explicit Checkpoint (what must be true afterwards). Replay never
  assumes a click worked.
* Conditions are the error taxonomy: recoverable (handle and carry on), outcome
  (a legitimate business answer the caller needs), fatal (stop with evidence).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1"

ActionKind = Literal["navigate", "click", "type", "select", "press"]
DataType = Literal["string", "integer", "money", "date"]
RiskLevel = Literal["safe", "risky"]
CapabilityStatus = Literal["draft", "approved", "retired"]
LocatorStrategy = Literal["role", "label", "row_label", "table_cell", "text", "placeholder", "css", "bbox"]


# --------------------------------------------------------------------------- targets

class Locator(BaseModel):
    """One way to find a control. Target.locators is ordered from most to least trusted."""

    strategy: LocatorStrategy
    value: str
    note: str = ""


class Target(BaseModel):
    """A control on the surface, described by what a human sees, with fallbacks.

    `frame` is the path of frame names from the top document ([] means the top
    document itself). Legacy apps live in framesets, so this is part of identity.
    """

    describe: str
    role: str
    name: str = ""
    frame: list[str] = Field(default_factory=list)
    locators: list[Locator] = Field(default_factory=list)


# --------------------------------------------------------------------------- values

class Value(BaseModel):
    """Either a literal or a reference to a declared input parameter."""

    literal: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "Value":
        if (self.literal is None) == (self.param is None):
            raise ValueError("Value needs exactly one of literal or param")
        return self

    def render(self, inputs: dict[str, str]) -> str:
        if self.param is not None:
            return inputs[self.param]
        return fill(self.literal, inputs) or ""


def fill(template: str | None, inputs: dict[str, str]) -> str | None:
    """Replace {param} placeholders. Unknown placeholders are left alone on purpose."""
    if template is None:
        return None
    out = template
    for k, v in inputs.items():
        out = out.replace("{" + k + "}", v)
    return out


# --------------------------------------------------------------------------- checkpoints

class Checkpoint(BaseModel):
    """What must be true after a step. Every listed condition must hold.

    Values may contain {param} placeholders that are filled from the inputs at
    replay time, so one recorded flow verifies correctly for any member id.
    """

    url: str | None = None        # path (+query) of the main frame, exact after filling
    heading: str | None = None    # first visible heading text
    text: list[str] | None = None  # visible text fragments
    element: Target | None = None  # a control that must be visible
    timeout_ms: int = 10_000

    def is_empty(self) -> bool:
        return not (self.url or self.heading or self.text or self.element)


# --------------------------------------------------------------------------- steps

class Step(BaseModel):
    id: str
    action: ActionKind
    target: Target | None = None
    value: Value | None = None      # type / select / navigate
    key: str | None = None          # press
    on_url: str | None = None       # page pattern the step starts on; used to re-home after a recovery
    expect: Checkpoint = Field(default_factory=Checkpoint)
    risk: RiskLevel = "safe"
    why: str = ""                   # the model's stated intent at recording time (redacted)

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        if self.action in ("click", "type", "select") and self.target is None:
            raise ValueError(f"step {self.id}: {self.action} needs a target")
        if self.action in ("type", "select", "navigate") and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} needs a value")
        if self.action == "press" and not self.key:
            raise ValueError(f"step {self.id}: press needs a key")
        return self


# --------------------------------------------------------------------------- contract

class ParamSpec(BaseModel):
    type: DataType = "string"
    description: str = ""
    required: bool = True
    sensitive: bool = False         # never logged, never stored, masked in screenshots' captions
    pattern: str | None = None      # validation regex applied before replay starts
    example: str | None = None      # never set for sensitive params


class OutputSpec(BaseModel):
    type: DataType = "string"
    description: str = ""
    source: Target
    parse: Literal["text", "money", "integer", "date"] = "text"


# --------------------------------------------------------------------------- conditions

class Detector(BaseModel):
    """All given fields must match. Regexes run against the main frame."""

    text: str | None = None
    url: str | None = None
    element: Target | None = None
    http_status: int | None = None

    @model_validator(mode="after")
    def _some(self) -> "Detector":
        if not (self.text or self.url or self.element or self.http_status):
            raise ValueError("Detector needs at least one of text, url, element, http_status")
        return self


class Handler(BaseModel):
    kind: Literal["dismiss", "reauth", "wait", "navigate"]
    target: Target | None = None    # dismiss
    wait_ms: int = 0                # wait
    url: str | None = None          # navigate (may use {param})


class Condition(BaseModel):
    """One entry in the error taxonomy.

    recoverable: apply `then`, re-check the step, retry the action if needed.
    outcome:     stop and return `outcome` to the caller. Not a failure.
    fatal:       stop with evidence.
    """

    id: str
    kind: Literal["recoverable", "outcome", "fatal"]
    when: Detector
    then: Handler | None = None
    outcome: str | None = None
    message: str | None = None      # regex; group 1 (or the whole match) becomes the message
    max_attempts: int = 2

    @model_validator(mode="after")
    def _shape(self) -> "Condition":
        if self.kind == "recoverable" and self.then is None:
            raise ValueError(f"condition {self.id}: recoverable needs a handler")
        if self.kind == "outcome" and not self.outcome:
            raise ValueError(f"condition {self.id}: outcome needs an outcome code")
        return self


# --------------------------------------------------------------------------- capability

class AppRef(BaseModel):
    profile: str                    # app profile id, e.g. "meridian"
    version: str = ""               # vendor version the flow was recorded against
    entry: str = "/"                # path the flow starts from (after authentication)


class Provenance(BaseModel):
    recorded_at: str
    model: str
    discovery_run: str
    teller_version: str
    approved_by: str | None = None
    approved_at: str | None = None
    approved_fingerprint: str | None = None


class Capability(BaseModel):
    schema_version: str = SCHEMA_VERSION
    id: str
    version: int = 1
    status: CapabilityStatus = "draft"
    title: str
    description: str
    app: AppRef
    inputs: dict[str, ParamSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    steps: list[Step]
    success: Checkpoint
    outcomes: list[Condition] = Field(default_factory=list)
    risk: RiskLevel = "safe"
    provenance: Provenance

    # -- integrity -------------------------------------------------------------

    def fingerprint(self) -> str:
        """Hash of everything that affects what a replay does. Approval binds to it."""
        body = self.model_dump(mode="json", include={"inputs", "outputs", "steps", "success", "outcomes", "app"})
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def approval_is_current(self) -> bool:
        return self.status == "approved" and self.provenance.approved_fingerprint == self.fingerprint()

    # -- agent-facing contract ---------------------------------------------------

    def to_tool_schema(self) -> dict[str, Any]:
        """The capability as a function-calling tool definition an agent can invoke."""
        props: dict[str, Any] = {}
        for name, spec in self.inputs.items():
            entry: dict[str, Any] = {"type": "integer" if spec.type == "integer" else "string",
                                     "description": spec.description}
            if spec.pattern:
                entry["pattern"] = spec.pattern
            props[name] = entry
        returns = {name: {"type": spec.type, "description": spec.description}
                   for name, spec in self.outputs.items()}
        return {
            "name": self.id,
            "description": self.description + " Returns: " + json.dumps(returns) +
                           ". Possible outcomes: " + ", ".join(c.outcome for c in self.outcomes if c.outcome),
            "input_schema": {"type": "object", "properties": props,
                             "required": [n for n, s in self.inputs.items() if s.required],
                             "additionalProperties": False},
        }

    # -- io --------------------------------------------------------------------

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2, exclude_none=True))
            f.write("\n")

    @classmethod
    def load(cls, path: str) -> "Capability":
        with open(path, encoding="utf-8") as f:
            return cls.model_validate_json(f.read())


# --------------------------------------------------------------------------- app profile

class AuthSpec(BaseModel):
    """How the surface signs in. Credentials come from the environment, never the artifact."""

    login_url: str
    user_target: Target
    pass_target: Target
    submit_target: Target
    user_env: str
    pass_env: str
    signed_in_check: Detector


class AppProfile(BaseModel):
    """Per vendor-product knowledge shared by every capability recorded against it."""

    id: str
    product: str
    version: str = ""
    base_url: str
    main_frame: list[str] = Field(default_factory=list)  # frame path of the content frame
    auth: AuthSpec | None = None
    conditions: list[Condition] = Field(default_factory=list)


# --------------------------------------------------------------------------- results

class StepResult(BaseModel):
    id: str
    status: Literal["ok", "recovered", "failed", "skipped"]
    attempts: int = 1
    locator_used: str | None = None
    drift: bool = False             # a fallback locator was needed
    recoveries: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class EvidenceRef(BaseModel):
    screenshot: str | None = None
    snapshot: str | None = None
    log: str | None = None


class Outcome(BaseModel):
    code: str
    message: str = ""
    step_id: str
    condition_id: str


class Failure(BaseModel):
    code: str                       # target_not_found | checkpoint_timeout | fatal_condition | policy_blocked | handoff_timeout | operator_abort | bad_input | surface_error
    step_id: str | None = None
    expected: str = ""
    observed: str = ""
    condition_id: str | None = None
    evidence: EvidenceRef = Field(default_factory=EvidenceRef)


class HumanAction(BaseModel):
    at: str
    kind: str                       # click | input | submit | navigate | note
    detail: str


class Handoff(BaseModel):
    id: str
    reason: str
    kind: Literal["stuck", "approval", "unknown_state"]
    step_id: str | None = None
    requested_at: str
    resolved_at: str | None = None
    resolution: Literal["resumed", "aborted", "timeout"] | None = None
    operator_note: str = ""
    human_actions: list[HumanAction] = Field(default_factory=list)


class ReplayResult(BaseModel):
    status: Literal["success", "outcome", "failure"]
    capability: str
    version: int
    run_id: str
    inputs: dict[str, str]          # sensitive values already redacted
    outputs: dict[str, Any] | None = None
    outcome: Outcome | None = None
    failure: Failure | None = None
    steps: list[StepResult] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    duration_ms: int = 0
    run_dir: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
