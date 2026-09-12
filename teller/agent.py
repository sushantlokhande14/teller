"""
The discovery loop: observe, decide, act, until the goal is met or we stop.

The model sees each screen as a numbered list of controls plus a screenshot
with the same numbers, and acts through a small set of tools. It never sees
credentials, never sees sensitive input values (it writes {name} and the
surface substitutes), and every action passes the same policy check replay
uses. Known interstitials from the app profile are handled by code before the
model is asked, so the model spends its turns on the actual task and the
recording stays clean.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from teller.conditions import apply_handler, find_condition
from teller.evidence import RunLog
from teller.handoff import ControlBroker
from teller.policy import Policy, Redactor
from teller.recorder import Contract, Extraction, TraceEntry
from teller.replay import parse_value
from teller.schema import AppProfile, fill
from teller.surface.base import Element, Observation
from teller.surface.playwright_surface import SurfaceError

TOOLS: list[dict[str, Any]] = [
    {"name": "click", "description": "Click a control by its ref number.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "integer"}, "why": {"type": "string"}},
                      "required": ["ref", "why"], "additionalProperties": False}},
    {"name": "type", "description": "Clear a text field (or pick a dropdown option) by ref and enter text. "
                                    "Write {name} to enter an input value without seeing it. Set submit to press Enter afterwards.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "integer"}, "text": {"type": "string"},
                                                       "submit": {"type": "boolean"}, "why": {"type": "string"}},
                      "required": ["ref", "text", "why"], "additionalProperties": False}},
    {"name": "select", "description": "Choose an option in a dropdown by ref and visible option text.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "integer"}, "option": {"type": "string"},
                                                       "why": {"type": "string"}},
                      "required": ["ref", "option", "why"], "additionalProperties": False}},
    {"name": "press", "description": "Press a keyboard key (Enter, Tab, Escape).",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "why": {"type": "string"}},
                      "required": ["key", "why"], "additionalProperties": False}},
    {"name": "navigate", "description": "Go to a path on the application, only when no link or button gets you there.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "why": {"type": "string"}},
                      "required": ["url", "why"], "additionalProperties": False}},
    {"name": "extract", "description": "Record the value shown in a cell or field as one of the declared outputs.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "ref": {"type": "integer"},
                                                       "why": {"type": "string"}},
                      "required": ["name", "ref", "why"], "additionalProperties": False}},
    {"name": "done", "description": "The goal is met and every declared output has been extracted.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                      "required": ["summary"], "additionalProperties": False}},
    {"name": "give_up", "description": "You cannot make progress. Say why, and whether a person could.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}, "needs_human": {"type": "boolean"}},
                      "required": ["reason"], "additionalProperties": False}},
]

SYSTEM = """You are operating a legacy back-office application for a credit union, on behalf of an automation system that will record what you do and replay it later without you.

You see each screen as a numbered list of controls and cells, plus a screenshot carrying the same numbers. Act through the tools, one action per turn, and say briefly in `why` what you expect the action to do.

Rules:
- You are already signed in. Never type credentials or anything that looks like a password.
- Prefer the links, fields and buttons you can see. Use navigate only when nothing on screen gets you there.
- Do not click anything that posts a change (confirm, submit, transfer, delete, close) unless the goal requires it, and say so in `why`.
- When the goal asks you to read a value, call extract with the declared output name and the ref of the cell that holds exactly that value, then call done once every output is recorded.
- Keep to the shortest path. Do not explore for its own sake.
- If you cannot make progress, call give_up with a clear reason.
"""


@dataclass
class Decision:
    tool: str | None
    args: dict[str, Any]
    text: str = ""
    content: list[Any] = field(default_factory=list)  # what to append back as the assistant turn
    stop_reason: str = ""


class Model(Protocol):
    name: str

    def decide(self, system: str, messages: list[dict[str, Any]], observation: Observation) -> Decision: ...


class ClaudeModel:
    def __init__(self, model: str | None = None) -> None:
        import anthropic

        self.name = model or os.environ.get("TELLER_MODEL", "claude-opus-5")
        self._client = anthropic.Anthropic()

    def decide(self, system: str, messages: list[dict[str, Any]], observation: Observation) -> Decision:
        resp = self._client.messages.create(
            model=self.name, max_tokens=4096, system=system, tools=TOOLS,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True}, messages=messages)
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        tool = next((b for b in resp.content if b.type == "tool_use"), None)
        content = [b.model_dump() for b in resp.content]
        if tool is None:
            return Decision(tool=None, args={}, text=text, content=content, stop_reason=resp.stop_reason)
        return Decision(tool=tool.name, args=dict(tool.input), text=text, content=content, stop_reason=resp.stop_reason)


class ScriptedModel:
    """A stand-in for tests and key-less demos: a fixed list of tool calls that
    name controls by role and name instead of by ref."""

    name = "scripted"

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self._i = 0

    def decide(self, system: str, messages: list[dict[str, Any]], observation: Observation) -> Decision:
        if self._i >= len(self._script):
            return Decision(tool="give_up", args={"reason": "script exhausted"}, content=[])
        item = self._script[self._i]
        self._i += 1
        args = dict(item.get("args", {}))
        if "by" in item:
            args["ref"] = find_ref(observation, item["by"])
        block = {"type": "tool_use", "id": f"scripted-{self._i}", "name": item["tool"], "input": args}
        return Decision(tool=item["tool"], args=args, content=[block], stop_reason="tool_use")


def find_ref(obs: Observation, by: dict[str, str]) -> int:
    """Match a scripted description (role, name, row, column, row_contains) to a ref."""
    for el in obs.elements:
        if by.get("role") and el.role != by["role"]:
            continue
        if by.get("name") and el.name != by["name"]:
            continue
        if by.get("row") and (el.row_label != by["row"] or el.name == by["row"]):
            continue
        if by.get("column") and el.col_header != by["column"]:
            continue
        if by.get("row_contains") and by["row_contains"] not in el.row_cells:
            continue
        return el.ref
    raise KeyError(f"scripted model: nothing matching {by} on the current screen")


class DiscoveryRun:
    def __init__(self, contract: Contract, inputs: dict[str, str], profile: AppProfile, policy: Policy,
                 surface, model: Model, log: RunLog, broker: ControlBroker, redactor: Redactor) -> None:
        self.contract = contract
        self.inputs = inputs
        self.profile = profile
        self.policy = policy
        self.surface = surface
        self.model = model
        self.log = log
        self.broker = broker
        self.redactor = redactor
        self.trace: list[TraceEntry] = []
        self.extractions: dict[str, Extraction] = {}
        self.messages: list[dict[str, Any]] = []
        self.status = "running"
        self.reason = ""

    # ------------------------------------------------------------------ prompt

    def _system(self) -> str:
        shown = {k: ("[hidden: write {" + k + "} to enter it]" if self.contract.inputs[k].sensitive else v)
                 for k, v in self.inputs.items()}
        outs = {k: (v if isinstance(v, dict) else v.model_dump()) for k, v in self.contract.outputs.items()}
        return (SYSTEM + f"\nGoal: {fill(self.contract.goal, self.inputs)}\n"
                + f"Inputs: {json.dumps(shown)}\n"
                + f"Outputs to extract: {json.dumps(outs)}\n")

    def _user_turn(self, obs: Observation, note: str = "") -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if obs.screenshot:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.standard_b64encode(obs.screenshot).decode()}})
        text = obs.to_prompt()
        if note:
            text = f"{text}\n\nNote: {note}"
        content.append({"type": "text", "text": text})
        return {"role": "user", "content": content}

    def _trim_images(self, keep: int = 2) -> None:
        seen = 0
        for m in reversed(self.messages):
            if m["role"] != "user":
                continue
            for block in m["content"]:
                if block.get("type") == "image":
                    seen += 1
                    if seen > keep:
                        block.clear()
                        block.update({"type": "text", "text": "[earlier screenshot removed]"})

    # ------------------------------------------------------------------ loop

    def run(self) -> str:
        self.log.event("discovery.start", goal=fill(self.contract.goal, self.inputs), model=self.model.name,
                       inputs={k: ("[redacted]" if self.contract.inputs[k].sensitive else v) for k, v in self.inputs.items()})
        self.surface.login()
        self.surface.navigate(fill(self.contract.entry, self.inputs) or "/")
        obs = self._observe(0)
        self.messages.append(self._user_turn(obs))
        system = self._system()
        for step in range(1, self.policy.discovery.max_steps + 1):
            self._trim_images()
            t0 = time.monotonic()
            decision = self.model.decide(system, self.messages, obs)
            self.log.event("decide", step=step, tool=decision.tool, args=decision.args, text=decision.text,
                           stop_reason=decision.stop_reason, ms=int((time.monotonic() - t0) * 1000))
            if decision.content:
                self.messages.append({"role": "assistant", "content": decision.content})
            if decision.stop_reason == "refusal":
                return self._finish("failed", "the model declined to continue")
            if decision.tool is None:
                self.messages.append({"role": "user", "content": [{"type": "text", "text":
                                      "Reply with a tool call: act on the screen, or call done / give_up."}]})
                continue
            tool_use_id = next((b.get("id") for b in decision.content if b.get("type") == "tool_use"), "t")
            result, obs_after, finished = self._execute(step, decision, obs)
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": result, "is_error": result.startswith("Error")}]})
            if finished:
                return self.status
            if obs_after is not None:
                obs = obs_after
                self.messages.append(self._user_turn(obs))
        return self._finish("failed", f"step budget of {self.policy.discovery.max_steps} exhausted")

    def _observe(self, step: int) -> Observation:
        """Observe, handling known interstitials in code first (bounded)."""
        for _ in range(3):
            obs = self.surface.observe(screenshot=True)
            hit = find_condition(self.profile.conditions, obs, self.surface)
            if hit and hit[0].kind == "recoverable":
                cond, msg = hit
                self.log.event("condition", step=step, id=cond.id, kind=cond.kind, handled_by="code")
                problem = apply_handler(cond.then, self.surface, self.inputs)
                if problem:
                    self.log.event("condition.unhandled", step=step, id=cond.id, problem=problem)
                    break
                continue
            if hit:
                cond, msg = hit
                self.log.event("condition", step=step, id=cond.id, kind=cond.kind, message=msg, handled_by="model")
                obs.text = f"[known condition {cond.id}: {msg or cond.kind}] " + obs.text
            self.log.save_observation(obs, f"step-{step:02d}")
            return obs
        return self.surface.observe(screenshot=True)

    def _execute(self, step: int, d: Decision, obs: Observation) -> tuple[str, Observation | None, bool]:
        tool, a = d.tool, d.args
        try:
            if tool == "done":
                missing = [k for k in self.contract.outputs if k not in self.extractions]
                if missing:
                    return f"Error: outputs not extracted yet: {missing}. Extract them before calling done.", None, False
                self._finish("success", str(a.get("summary", "")))
                return "Recorded.", None, True
            if tool == "give_up":
                if a.get("needs_human"):
                    handoff = self.broker.request("stuck", str(a.get("reason", "")), f"step-{step}",
                                                  {"capability": self.contract.id, "goal": self.contract.goal,
                                                   "url": obs.path, "heading": obs.heading})
                    if handoff.resolution == "resumed":
                        self.log.event("discovery.resumed_after_handoff", note=handoff.operator_note)
                        obs2 = self._observe(step)
                        return f"A person intervened ({handoff.operator_note or 'no note'}). Continue from the current screen.", obs2, False
                self._finish("failed", str(a.get("reason", "gave up")))
                return "Stopped.", None, True
            if tool == "extract":
                return self._extract(step, a, obs), None, False

            entry = TraceEntry(seq=len(self.trace) + 1, tool=tool, args=a, before=obs)
            el: Element | None = None
            if tool in ("click", "type", "select"):
                el = obs.find(int(a["ref"]))
                entry.element = el
            target_url = None
            if tool == "navigate":
                target_url = str(a["url"])
                if target_url.startswith("/"):
                    target_url = self.profile.base_url.rstrip("/") + target_url
            verdict = self.policy.check(tool, obs.url, name=el.name if el else "",
                                        href_or_form=el.form_action if el else "", target_url=target_url)
            entry.risk = verdict.risk
            if not verdict.allowed:
                entry.ok, entry.error = False, verdict.reason
                self.trace.append(entry)
                self.log.event("policy.blocked", step=step, tool=tool, reason=verdict.reason)
                return f"Error: blocked by policy: {verdict.reason}", None, False
            if verdict.needs_confirmation:
                handoff = self.broker.request("approval", verdict.reason, f"step-{step}",
                                              {"capability": self.contract.id, "goal": self.contract.goal, "url": obs.path,
                                               "heading": obs.heading, "action": f"{tool} {el.name if el else a}"})
                if handoff.resolution != "resumed":
                    entry.ok, entry.error = False, "operator declined"
                    self.trace.append(entry)
                    return "Error: a person declined this action. Find another way or give_up.", None, False
                self.log.event("policy.confirmed", step=step, by="operator", note=handoff.operator_note)

            self.broker.assert_automation()
            self.log.event("act", step=step, tool=tool, ref=a.get("ref"), target=el.short() if el else None,
                           value=self._shown_text(a), risk=verdict.risk, why=a.get("why", ""))
            if tool == "click":
                self.surface.click_ref(int(a["ref"]))
            elif tool == "type":
                self.surface.fill_ref(int(a["ref"]), fill(str(a["text"]), self.inputs) or "", bool(a.get("submit")))
                entry.args = {**a, "text": fill(str(a["text"]), self.inputs)}
            elif tool == "select":
                self.surface.select_ref(int(a["ref"]), str(a["option"]))
            elif tool == "press":
                self.surface.press(str(a["key"]))
            elif tool == "navigate":
                self.surface.navigate(str(a["url"]))
            else:
                return f"Error: unknown tool {tool}", None, False
            obs_after = self._observe(step)
            entry.after = obs_after
            self.trace.append(entry)
            dialogs = self.surface.drain_dialogs()
            note = f" A dialog appeared and was dismissed: {dialogs[-1]['message']!r}." if dialogs else ""
            return f"Done.{note}", obs_after, False
        except (SurfaceError, KeyError, ValueError) as e:
            self.log.event("act.error", step=step, tool=tool, error=str(e))
            obs_after = self._observe(step)
            return f"Error: {e}", obs_after, False

    def _extract(self, step: int, a: dict[str, Any], obs: Observation) -> str:
        name = str(a.get("name"))
        if name not in self.contract.outputs:
            return f"Error: {name!r} is not a declared output. Declared: {list(self.contract.outputs)}"
        el = obs.find(int(a["ref"]))
        raw = self.surface.read_ref(int(a["ref"]))
        spec = self.contract.outputs[name]
        typ = (spec if isinstance(spec, dict) else spec.model_dump()).get("type", "string")
        try:
            value = parse_value(raw, {"money": "money", "integer": "integer", "date": "date"}.get(typ, "text"))
        except ValueError as e:
            return f"Error: {e}. Pick the cell that holds just the {typ} value."
        self.extractions[name] = Extraction(name=name, element=el, raw=raw, observation=obs)
        self.log.event("extract", step=step, output=name, ref=a["ref"], target=el.short(), value=value)
        return f"Recorded {name} = {value!r}."

    def _shown_text(self, a: dict[str, Any]) -> str | None:
        if "text" not in a:
            return None
        return self.redactor.text(fill(str(a["text"]), self.inputs) or "")

    def _finish(self, status: str, reason: str) -> str:
        self.status = status
        self.reason = reason
        self.log.event("discovery.end", status=status, reason=reason, actions=len(self.trace),
                       extracted={k: v.raw for k, v in self.extractions.items()})
        transcript = [{"role": m["role"], "content": [
            ({"type": "text", "text": "[screenshot]"} if b.get("type") == "image" else b) for b in m["content"]]
            if isinstance(m["content"], list) else m["content"]} for m in self.messages]
        self.log.save_json("transcript.json", transcript)
        return status
