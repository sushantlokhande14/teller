"""
Deterministic replay: run a saved capability with concrete inputs, no model.

The loop per step is always the same:

    observe -> (are we on the right page? is a known condition showing?)
            -> resolve the target through its locators
            -> policy check (allowlist, risk)
            -> act
            -> wait for the step's checkpoint
            -> if the checkpoint fails, classify what is on screen:
                 recoverable -> handle it, re-check, retry the page group if needed
                 outcome     -> stop, return it to the caller (this is not a failure)
                 fatal       -> stop with evidence
                 unknown     -> hand the session to a person, then re-check

Form state lives on a page, so the unit of retry after a recovery that moved
us off the page is the *page group*: the run of consecutive steps recorded on
the same URL.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from teller.conditions import apply_handler, find_condition
from teller.evidence import RunLog
from teller.handoff import ControlBroker
from teller.policy import Policy, Redactor
from teller.schema import (AppProfile, Capability, Checkpoint, Condition, EvidenceRef, Failure, Handler,
                           Outcome, ReplayResult, Step, StepResult, fill)
from teller.surface.base import Observation, Resolved
from teller.surface.playwright_surface import SurfaceError

MAX_STEP_ATTEMPTS = 3
MAX_GROUP_RESTARTS = 2


class ReplayStop(Exception):
    """Raised inside a step to end the run with an outcome or a failure."""

    def __init__(self, outcome: Outcome | None = None, failure: Failure | None = None) -> None:
        super().__init__(outcome.code if outcome else failure.code if failure else "stop")
        self.outcome = outcome
        self.failure = failure


class RestartGroup(Exception):
    """A recovery moved us off the page; re-run the page group from its first step."""


@dataclass
class StepOutcome:
    result: StepResult


def parse_value(raw: str, how: str) -> Any:
    raw = raw.strip()
    if how == "money":
        cleaned = re.sub(r"[^\d.\-]", "", raw)
        try:
            return f"{Decimal(cleaned):.2f}"  # a string on purpose: exact, and JSON-safe
        except InvalidOperation:
            raise ValueError(f"could not parse {raw!r} as money")
    if how == "integer":
        return int(re.sub(r"[^\d\-]", "", raw))
    if how == "date":
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError(f"could not parse {raw!r} as a date")
    return raw


def validate_inputs(cap: Capability, inputs: dict[str, str]) -> str | None:
    for name, spec in cap.inputs.items():
        if name not in inputs:
            if spec.required:
                return f"missing required input {name!r}"
            continue
        v = inputs[name]
        if spec.type == "integer" and not re.fullmatch(r"-?\d+", v):
            return f"input {name!r} must be an integer"
        if spec.pattern and not re.fullmatch(spec.pattern, v):
            return f"input {name!r} does not match {spec.pattern!r}"
    extra = set(inputs) - set(cap.inputs)
    if extra:
        return f"unexpected inputs: {sorted(extra)}"
    return None


def url_matches(pattern: str | None, path: str, inputs: dict[str, str]) -> bool:
    """Segment-wise match. '*' stands for a server-generated segment (see Canon.url)."""
    expected = fill(pattern, inputs) or ""
    want, _, want_q = expected.partition("?")
    got, _, got_q = path.partition("?")
    a, b = want.split("/"), got.split("/")
    if len(a) != len(b) or want_q != got_q:
        return False
    return all(x == "*" and y or x == y for x, y in zip(a, b))


def checkpoint_holds(cp: Checkpoint, obs: Observation, inputs: dict[str, str], surface) -> tuple[bool, str]:
    if cp.url:
        expected = fill(cp.url, inputs)
        if not url_matches(cp.url, obs.path, inputs):
            return False, f"url is {obs.path}, expected {expected}"
    if cp.heading:
        expected = fill(cp.heading, inputs)
        if (obs.heading or "") != expected:
            return False, f"heading is {obs.heading!r}, expected {expected!r}"
    for t in cp.text or []:
        want = fill(t, inputs) or ""
        if want not in obs.text:
            return False, f"text {want!r} not on screen"
    if cp.element is not None and not surface.is_visible(cp.element):
        return False, f"control not visible: {cp.element.describe}"
    return True, ""


class Replayer:
    def __init__(self, cap: Capability, profile: AppProfile, policy: Policy, surface, log: RunLog,
                 broker: ControlBroker, redactor: Redactor) -> None:
        self.cap = cap
        self.profile = profile
        self.policy = policy
        self.surface = surface
        self.log = log
        self.broker = broker
        self.redactor = redactor
        self.conditions: list[Condition] = list(cap.outcomes) + list(profile.conditions)
        self.step_results: list[StepResult] = []
        self._recoveries: dict[str, list[str]] = {}
        self._page_urls: dict[str, str] = {}
        self.inputs: dict[str, str] = {}

    # ------------------------------------------------------------------ entry

    def run(self, inputs: dict[str, str], allow_draft: bool = False, on_ready=None) -> ReplayResult:
        t0 = time.monotonic()
        self.inputs = inputs
        shown = {k: ("[redacted]" if self.cap.inputs.get(k) and self.cap.inputs[k].sensitive else v)
                 for k, v in inputs.items()}
        self.log.event("replay.start", capability=self.cap.id, version=self.cap.version,
                       status=self.cap.status, fingerprint=self.cap.fingerprint(), inputs=shown)

        def finish(status: str, **kw: Any) -> ReplayResult:
            res = ReplayResult(status=status, capability=self.cap.id, version=self.cap.version,
                               run_id=self.log.run_id, inputs=shown, steps=self.step_results,
                               handoffs=self.broker.handoffs, duration_ms=int((time.monotonic() - t0) * 1000),
                               run_dir=str(self.log.dir), **kw)
            loggable = {k: (v.model_dump(mode="json", exclude_none=True) if hasattr(v, "model_dump") else v)
                        for k, v in kw.items()}
            self.log.event("replay.end", status=status, **loggable)
            self.log.save_json("result.json", res.model_dump(mode="json", exclude_none=True))
            return res

        problem = validate_inputs(self.cap, inputs)
        if problem:
            return finish("failure", failure=Failure(code="bad_input", expected="inputs matching the contract", observed=problem))
        if self.policy.require_approval and not allow_draft and not self.cap.approval_is_current():
            why = "capability is a draft" if self.cap.status != "approved" else "capability changed since it was approved"
            return finish("failure", failure=Failure(code="not_approved", expected="an approved, unmodified capability", observed=why))

        try:
            self.surface.login()
            self.surface.navigate(fill(self.cap.app.entry, inputs) or "/")
            if on_ready is not None:
                on_ready()  # test hook: the CLI injects faults here, once the session is established
            self._run_steps()
            outputs = self._extract()
            ok, obs, why = self._wait_checkpoint(self.cap.success, self.cap.success.timeout_ms)
            if not ok:
                raise ReplayStop(failure=self._failure("checkpoint_timeout", None, obs, "success condition", why))
            self.log.save_observation(self.surface.observe(screenshot=True), "final")
            return finish("success", outputs=outputs)
        except ReplayStop as stop:
            if stop.outcome:
                return finish("outcome", outcome=stop.outcome)
            return finish("failure", failure=stop.failure)
        except SurfaceError as e:
            return finish("failure", failure=self._failure("surface_error", None, None, "the surface to respond", str(e)))

    # ------------------------------------------------------------------ steps

    def _run_steps(self) -> None:
        steps = self.cap.steps
        i = 0
        restarts = 0
        while i < len(steps):
            step = steps[i]
            try:
                self._run_step(step)
                i += 1
            except _StepDone:
                i += 1
            except RestartGroup:
                restarts += 1
                if restarts > MAX_GROUP_RESTARTS:
                    raise ReplayStop(failure=self._failure("checkpoint_timeout", step.id, None,
                                                           "to recover within a bounded number of page retries",
                                                           f"page group restarted {restarts - 1} times"))
                first = i
                while first > 0 and steps[first - 1].on_url == step.on_url:
                    first -= 1
                self.log.event("replay.restart_group", from_step=step.id, to_step=steps[first].id, restarts=restarts)
                # drop the results of the steps we are about to redo
                redo = {s.id for s in steps[first:i + 1]}
                self.step_results = [r for r in self.step_results if r.id not in redo]
                i = first

    def _run_step(self, step: Step) -> None:
        t0 = time.monotonic()
        attempts = 0
        recoveries = self._recoveries.setdefault(step.id, [])  # kept across page-group restarts
        condition_hits: dict[str, int] = {}
        while True:
            attempts += 1
            if attempts > MAX_STEP_ATTEMPTS:
                obs = self.surface.observe(screenshot=True)
                raise ReplayStop(failure=self._failure("checkpoint_timeout", step.id, obs,
                                                       self._describe_expect(step), f"step did not complete after {attempts - 1} attempts"))
            self.broker.assert_automation()
            obs = self.surface.observe(screenshot=False)

            # 1. a known condition may already be showing before we act
            hit = find_condition(self.conditions, obs, self.surface)
            if hit:
                cond, msg = hit
                self._handle_condition(cond, msg, step, obs, condition_hits, recoveries)
                continue

            # 2. are we on the page this step was recorded on?
            if step.on_url and not url_matches(step.on_url, obs.path, self.inputs):
                self._rehome(step, obs.path)
                raise RestartGroup()
            if step.on_url:
                self._page_urls[step.on_url] = obs.path  # the concrete page, for re-homing later

            # 3. find the control
            control: Resolved | None = None
            if step.target is not None:
                control = self.surface.resolve(step.target)
                if control is None:
                    obs = self.surface.observe(screenshot=True)
                    tried = self._tried(step)
                    self._unknown_state(step, obs, f"control {step.target.describe!r}",
                                        f"none of the locators matched ({tried})", recoveries)
                    continue
                if control.drift:
                    self.log.event("replay.drift", step=step.id, used=control.strategy, missed=control.tried,
                                   note="a fallback locator was needed; the recorded primary locator no longer matches")

            # 4. policy
            verdict = self.policy.check(step.action, obs.url, name=step.target.name if step.target else "",
                                        recorded=step.risk,
                                        target_url=self._nav_url(step) if step.action == "navigate" else None)
            if not verdict.allowed:
                raise ReplayStop(failure=self._failure("policy_blocked", step.id, self.surface.observe(screenshot=True),
                                                       "an action permitted by policy", verdict.reason))
            if verdict.needs_confirmation:
                handoff = self.broker.request("approval", verdict.reason, step.id, self._context(step, obs, "", ""))
                if handoff.resolution != "resumed":
                    raise ReplayStop(failure=self._failure("operator_abort" if handoff.resolution == "aborted" else "handoff_timeout",
                                                           step.id, None, "operator approval", handoff.operator_note or handoff.resolution))
                self.log.event("policy.confirmed", step=step.id, by="operator", note=handoff.operator_note)

            # 5. act
            self.log.event("act", step=step.id, action=step.action, target=step.target.describe if step.target else None,
                           strategy=control.strategy if control else None, drift=control.drift if control else False,
                           value=self._shown_value(step), risk=verdict.risk)
            self._act(step, control)

            # 6. checkpoint
            ok, obs, why = self._wait_checkpoint(step.expect, step.expect.timeout_ms)
            if ok:
                self.log.save_observation(self.surface.observe(screenshot=True), f"after-{step.id}")
                self.step_results.append(StepResult(
                    id=step.id, status="recovered" if recoveries else "ok", attempts=attempts,
                    locator_used=control.strategy if control else None, drift=bool(control and control.drift),
                    recoveries=recoveries, duration_ms=int((time.monotonic() - t0) * 1000)))
                self.log.event("checkpoint.ok", step=step.id, attempts=attempts)
                return
            self.log.event("checkpoint.failed", step=step.id, expected=self._describe_expect(step), observed=why)

            hit = find_condition(self.conditions, obs, self.surface)
            if hit:
                cond, msg = hit
                self._handle_condition(cond, msg, step, obs, condition_hits, recoveries)
                continue
            self._unknown_state(step, obs, self._describe_expect(step), why, recoveries)

    # ------------------------------------------------------------------ pieces

    def _act(self, step: Step, control: Resolved | None) -> None:
        if step.action == "navigate":
            self.surface.navigate(self._nav_url(step))
        elif step.action == "click":
            self.surface.click(control)
        elif step.action == "type":
            text = step.value.render(self.inputs)
            self.surface.fill(control, text)
            got = self.surface.read(control)
            if got != text:
                self.log.event("act.readback_mismatch", step=step.id, expected_len=len(text), got_len=len(got))
        elif step.action == "select":
            self.surface.select(control, step.value.render(self.inputs))
        elif step.action == "press":
            self.surface.press(step.key or "Enter")

    def _nav_url(self, step: Step) -> str:
        return step.value.render(self.inputs) if step.value else "/"

    def _shown_value(self, step: Step) -> str | None:
        if step.value is None:
            return None
        if step.value.param and self.cap.inputs.get(step.value.param, None) and self.cap.inputs[step.value.param].sensitive:
            return f"[redacted:{step.value.param}]"
        return step.value.render(self.inputs)

    def _wait_checkpoint(self, cp: Checkpoint, timeout_ms: int) -> tuple[bool, Observation, str]:
        deadline = time.monotonic() + timeout_ms / 1000
        why = ""
        while True:
            obs = self.surface.observe(screenshot=False)
            ok, why = checkpoint_holds(cp, obs, self.inputs, self.surface)
            if ok:
                return True, obs, ""
            if time.monotonic() > deadline:
                return False, obs, why
            # bail out early when a known condition is on screen; no point waiting the full timeout
            if find_condition(self.conditions, obs, self.surface):
                return False, obs, why
            time.sleep(0.3)

    def _handle_condition(self, cond: Condition, msg: str, step: Step, obs: Observation,
                          hits: dict[str, int], recoveries: list[str]) -> None:
        hits[cond.id] = hits.get(cond.id, 0) + 1
        self.log.event("condition", step=step.id, id=cond.id, kind=cond.kind, message=msg, hit=hits[cond.id])
        if cond.kind == "outcome":
            self.log.save_observation(self.surface.observe(screenshot=True), f"outcome-{step.id}")
            raise ReplayStop(outcome=Outcome(code=cond.outcome or cond.id, message=msg, step_id=step.id, condition_id=cond.id))
        if cond.kind == "fatal":
            raise ReplayStop(failure=self._failure("fatal_condition", step.id, self.surface.observe(screenshot=True),
                                                   self._describe_expect(step), msg or cond.id, condition_id=cond.id))
        if hits[cond.id] > cond.max_attempts:
            raise ReplayStop(failure=self._failure("checkpoint_timeout", step.id, self.surface.observe(screenshot=True),
                                                   self._describe_expect(step),
                                                   f"condition {cond.id} kept recurring ({hits[cond.id]} times)", condition_id=cond.id))
        recoveries.append(cond.id)
        self._apply(cond.then, step)
        after = self.surface.observe(screenshot=False)
        self.log.event("recovery", step=step.id, id=cond.id, handler=cond.then.kind, now_at=after.path)
        if step.on_url and not url_matches(step.on_url, after.path, self.inputs):
            ok, _, _ = self._wait_checkpoint(step.expect, 1500)
            if ok:
                # The recovery itself completed the step (a re-login or a dismissed interstitial
                # landed on the page the step was heading for). Nothing left to redo.
                self.log.save_observation(self.surface.observe(screenshot=True), f"after-{step.id}")
                self.step_results.append(StepResult(id=step.id, status="recovered", attempts=hits[cond.id],
                                                    locator_used=None, recoveries=recoveries))
                self.log.event("checkpoint.ok", step=step.id, via=f"recovery:{cond.id}")
                raise _StepDone()
            self._rehome(step, after.path)
            raise RestartGroup()

    def _rehome(self, step: Step, at: str) -> None:
        """Get back to the page the step was recorded on. Prefer the concrete URL we saw
        earlier in this run; a pattern with a server-generated segment cannot be typed in."""
        target = self._page_urls.get(step.on_url or "") or fill(step.on_url, self.inputs) or "/"
        if "*" in target:
            raise ReplayStop(failure=self._failure("checkpoint_timeout", step.id, self.surface.observe(screenshot=True),
                                                   f"to be on {step.on_url}", f"landed on {at} and the page cannot be re-opened by URL"))
        self.log.event("replay.rehome", step=step.id, at=at, to=target)
        self.surface.navigate(target)

    def _apply(self, handler: Handler | None, step: Step) -> None:
        problem = apply_handler(handler, self.surface, self.inputs)
        if problem:
            raise ReplayStop(failure=self._failure("target_not_found", step.id, self.surface.observe(screenshot=True),
                                                   "the recovery handler's control", problem))

    def _unknown_state(self, step: Step, obs: Observation, expected: str, observed: str, recoveries: list[str]) -> None:
        """Nothing in the taxonomy matched. Ask a person, or fail with evidence."""
        if not self.policy.escalate_on_unknown:
            raise ReplayStop(failure=self._failure("checkpoint_timeout", step.id, obs, expected, observed))
        handoff = self.broker.request("unknown_state", f"unexpected screen at step {step.id}: {observed}", step.id,
                                      self._context(step, obs, expected, observed))
        if handoff.resolution == "aborted":
            raise ReplayStop(failure=self._failure("operator_abort", step.id, None, expected, handoff.operator_note or "aborted by operator"))
        if handoff.resolution == "timeout":
            raise ReplayStop(failure=self._failure("handoff_timeout", step.id, None, expected, "no operator reply"))
        recoveries.append(f"handoff:{handoff.id}")
        resume_from = self.broker.last_reply.get("resume_from", "verify")
        if resume_from == "next_step":
            self.step_results.append(StepResult(id=step.id, status="recovered", attempts=1, recoveries=recoveries,
                                                locator_used="operator"))
            raise _StepDone()
        if resume_from == "verify":
            ok, _, _ = self._wait_checkpoint(step.expect, 3000)
            if ok:
                self.step_results.append(StepResult(id=step.id, status="recovered", attempts=1, recoveries=recoveries,
                                                    locator_used="operator"))
                raise _StepDone()
        # retry_step, or verify that did not hold: fall through and redo the step

    def _extract(self) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for name, spec in self.cap.outputs.items():
            control = self.surface.resolve(spec.source)
            if control is None:
                obs = self.surface.observe(screenshot=True)
                raise ReplayStop(failure=self._failure("output_not_found", None, obs, f"output {name!r} at {spec.source.describe}",
                                                       f"none of the locators matched ({', '.join(l.strategy for l in spec.source.locators)})"))
            raw = self.surface.read(control)
            try:
                outputs[name] = parse_value(raw, spec.parse)
            except ValueError as e:
                raise ReplayStop(failure=self._failure("output_parse_error", None, self.surface.observe(screenshot=True),
                                                       f"a {spec.parse} value for {name!r}", str(e)))
            self.log.event("extract", output=name, strategy=control.strategy, drift=control.drift, value=outputs[name])
        return outputs

    # ------------------------------------------------------------------ helpers

    def _failure(self, code: str, step_id: str | None, obs: Observation | None, expected: str, observed: str,
                 condition_id: str | None = None) -> Failure:
        ev = EvidenceRef(log="log.jsonl")  # paths are relative to the run directory (result.run_dir)
        if obs is not None:
            if obs.screenshot is None:
                obs = self.surface.observe(screenshot=True)
            shot, snap = self.log.save_observation(obs, f"failure-{step_id or 'final'}")
            ev.screenshot = os.path.relpath(shot, self.log.dir) if shot else None
            ev.snapshot = os.path.relpath(snap, self.log.dir)
        self.log.event("failure", code=code, step=step_id, expected=expected, observed=observed, condition=condition_id)
        return Failure(code=code, step_id=step_id, expected=expected, observed=observed, condition_id=condition_id, evidence=ev)

    def _context(self, step: Step, obs: Observation, expected: str, observed: str) -> dict[str, Any]:
        return {"capability": f"{self.cap.id} v{self.cap.version}", "goal": self.cap.title, "url": obs.path,
                "heading": obs.heading, "expected": expected, "observed": observed,
                "step": {"id": step.id, "action": step.action, "target": step.target.describe if step.target else None},
                "inputs": self.redactor.any({k: v for k, v in self.inputs.items()
                                             if not (self.cap.inputs.get(k) and self.cap.inputs[k].sensitive)})}

    def _describe_expect(self, step: Step) -> str:
        cp = step.expect
        bits = []
        if cp.url:
            bits.append(f"url={fill(cp.url, self.inputs)}")
        if cp.heading:
            bits.append(f"heading={fill(cp.heading, self.inputs)!r}")
        if cp.text:
            bits.append("text=" + ", ".join(repr(fill(t, self.inputs)) for t in cp.text))
        if cp.element:
            bits.append(f"visible={cp.element.describe!r}")
        return "; ".join(bits) or "(no checkpoint)"

    @staticmethod
    def _tried(step: Step) -> str:
        return ", ".join(f"{l.strategy}={l.value!r}" for l in (step.target.locators if step.target else []))


class _StepDone(Exception):
    pass
