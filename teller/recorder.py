"""
Turning a successful discovery run into a Capability.

The model never writes the artifact. It only acted on numbered controls; the
recorder rebuilds each of those controls as a Target with several locators,
derives a checkpoint for every step from what the screen looked like after the
action, and swaps the concrete input values it was given for {param}
placeholders so the flow works for the next member id too.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from teller import __version__
from teller.policy import Policy, Redactor
from teller.schema import (MAIN_FRAME, AppRef, Capability, Checkpoint, Locator, OutputSpec, ParamSpec,
                           Provenance, Step, Target, Value, now_iso)
from teller.surface.base import Element, Observation

LABEL_LIKE = re.compile(r"[A-Za-z][A-Za-z .,'&/\-]{1,60}")


@dataclass
class TraceEntry:
    """One action the model took, with what it saw before and after."""

    seq: int
    tool: str
    args: dict[str, Any]
    before: Observation
    after: Observation | None = None
    element: Element | None = None
    ok: bool = True
    error: str = ""
    risk: str = "safe"
    note: str = ""


@dataclass
class Contract:
    id: str
    title: str
    goal: str
    inputs: dict[str, ParamSpec]
    outputs: dict[str, OutputSpec | dict]
    description: str = ""
    entry: str = "/"

    @classmethod
    def load(cls, path: str) -> "Contract":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return cls(id=d["id"], title=d["title"], goal=d["goal"], description=d.get("description", d["title"]),
                   entry=d.get("entry", "/"),
                   inputs={k: ParamSpec.model_validate(v) for k, v in d.get("inputs", {}).items()},
                   outputs=d.get("outputs", {}))


@dataclass
class Extraction:
    name: str
    element: Element
    raw: str
    observation: Observation


class Canon:
    """Turns one concrete run into a reusable flow: input values become {name}
    placeholders (longest first, so a value containing another is handled correctly),
    server-generated path segments become wildcards, and the app's content frame is
    referred to by role rather than by the name this instance happens to use."""

    def __init__(self, inputs: dict[str, str], specs: dict[str, ParamSpec],
                 main_frame: list[str] | None = None) -> None:
        self.pairs = sorted(((v, k) for k, v in inputs.items() if v and len(v) >= 3), key=lambda p: -len(p[0]))
        self.specs = specs
        self.main_frame = main_frame or []

    def frame(self, path: list[str]) -> list[str]:
        return [MAIN_FRAME] if list(path) == list(self.main_frame) else list(path)

    def __call__(self, s: str | None) -> str | None:
        if s is None:
            return None
        for v, k in self.pairs:
            s = s.replace(v, "{" + k + "}")
        return s

    def url(self, path: str | None) -> str | None:
        """Canonical URL pattern. Input values become {name}; a path segment that still
        carries digits after that (an account number the server just generated, a
        record id we never supplied) becomes '*', because it will differ next run."""
        if path is None:
            return None
        base, _, query = path.partition("?")
        segments = []
        for seg in base.split("/"):
            seg = self(seg) or ""
            if re.search(r"\d", re.sub(r"\{[a-z_]+\}", "", seg)):
                seg = "*"
            segments.append(seg)
        out = "/".join(segments)
        return out + ("?" + (self(query) or "") if query else "")

    def value_for(self, text: str) -> Value:
        for v, k in self.pairs:
            if text == v:
                return Value(param=k)
        return Value(literal=self(text) or "")


def choose_row_key(el: Element, obs: Observation, canon: Canon) -> str | None:
    """Pick a cell in the same row that identifies it and is unique in the table."""
    rows: list[list[str]] = []
    for other in obs.elements:
        if other.frame == el.frame and other.col_header and other.row_cells and other.row_cells not in rows:
            rows.append(other.row_cells)
    for text in el.row_cells:
        if text == el.name or text == el.col_header or not LABEL_LIKE.fullmatch(text):
            continue
        if canon(text) != text:
            continue  # contains an input value; not a stable label
        if sum(1 for r in rows if text in r) == 1:
            return text
    return None


def make_target(el: Element, obs: Observation, canon: Canon, describe: str | None = None) -> Target:
    name = canon(el.name) or ""
    locs: list[Locator] = []
    role = Locator(strategy="role", value=name, note="ARIA role plus accessible name, the way a screen reader finds it")
    row = None
    is_cell = el.role in ("cell", "text")
    if el.row_label and (el.row_label != el.name or (not is_cell and el.name_source == "row")):
        row = Locator(strategy="row_label", value=canon(el.row_label) or "",
                      note="label in the first cell of the same table row; how legacy forms are laid out")
    css = Locator(strategy="css", value=el.css, note="structural path; brittle, last resort")
    bbox = Locator(strategy="bbox", value=json.dumps([round(v) for v in el.bbox]),
                   note="screen position at recording time; only used when policy allows coordinate fallback")

    if is_cell:
        if el.col_header:
            key = choose_row_key(el, obs, canon)
            if key:
                locs.append(Locator(strategy="table_cell", value=json.dumps({"row": key, "column": el.col_header}),
                                    note=f"the row containing '{key}', under the '{el.col_header}' column"))
        # row_label means "the cell after the label", which is only the same thing as
        # this cell in a two-column label/value table. In a grid it would point at a
        # different column, so it must not become a silent fallback.
        if row and len(el.row_cells) <= 2:
            locs.append(row)
        is_label_cell = not el.row_label or el.row_label == el.name
        if is_label_cell and LABEL_LIKE.fullmatch(el.name) and canon(el.name) == el.name and not el.col_header:
            locs.append(Locator(strategy="text", value=el.name, note="exact visible text"))
        locs.extend([css, bbox])
    else:
        if el.name_source == "row" and row:
            locs.extend([row, role])
        elif el.name_source == "placeholder":
            locs.extend([Locator(strategy="placeholder", value=name), role])
        else:
            locs.append(role)
            if el.name_source == "label":
                locs.append(Locator(strategy="label", value=name))
            if row:
                locs.append(row)
        if el.role in ("link", "button") and name:
            locs.append(Locator(strategy="text", value=name, note="exact visible text"))
        locs.extend([css, bbox])

    what = describe or _describe(el, name, canon)
    return Target(describe=what, role=el.role, name=name, frame=canon.frame(el.frame), locators=locs)


def _describe(el: Element, name: str, canon: Canon) -> str:
    frame = canon.frame(el.frame)
    where = " in the content frame" if frame == [MAIN_FRAME] else (f" in the {'/'.join(frame)} frame" if frame else "")
    if el.role in ("cell", "text"):
        if el.col_header:
            return f"the '{el.col_header}' cell of the row {canon(el.row_label)!r}{where}"
        if el.row_label and el.row_label != el.name:
            return f"the value next to {el.row_label!r}{where}"
        return f"the cell reading {name!r}{where}"
    return f"the {name!r} {el.role}{where}"


def derive_checkpoint(entry: TraceEntry, target: Target | None, canon: Canon, timeout_ms: int) -> Checkpoint:
    after = entry.after
    assert after is not None
    cp = Checkpoint(url=canon.url(after.path), timeout_ms=timeout_ms)
    moved = after.path != entry.before.path
    if (moved or entry.tool in ("click", "navigate", "press")) and after.heading:
        cp.heading = canon(after.heading)
    return cp


def build_capability(trace: list[TraceEntry], extractions: dict[str, Extraction], contract: Contract,
                     inputs: dict[str, str], profile_id: str, profile_version: str, run_id: str,
                     model: str, policy: Policy, redactor: Redactor, endpoint: str | None = None,
                     main_frame: list[str] | None = None) -> Capability:
    canon = Canon(inputs, contract.inputs, main_frame)
    steps: list[Step] = []
    n = 0
    for entry in trace:
        if not entry.ok or entry.tool not in ("click", "type", "select", "press", "navigate") or entry.after is None:
            continue
        n += 1
        target = make_target(entry.element, entry.before, canon) if entry.element is not None else None
        value = None
        key = None
        if entry.tool == "type":
            value = canon.value_for(str(entry.args.get("text", "")))
        elif entry.tool == "select":
            value = canon.value_for(str(entry.args.get("option", "")))
        elif entry.tool == "navigate":
            value = Value(literal=canon(str(entry.args.get("url", ""))) or "/")
        elif entry.tool == "press":
            key = str(entry.args.get("key", "Enter"))
        steps.append(Step(
            id=f"s{n}", action=entry.tool, target=target, value=value, key=key,
            on_url=canon.url(entry.before.path), risk=entry.risk,
            expect=derive_checkpoint(entry, target, canon, policy.replay_step_timeout_ms),
            why=redactor.text(str(entry.args.get("why", "")))[:200]))
        if entry.tool == "type" and bool(entry.args.get("submit")):
            n += 1
            steps.append(Step(id=f"s{n}", action="press", key="Enter", on_url=canon.url(entry.before.path),
                              expect=Checkpoint(url=canon.url(entry.after.path), heading=canon(entry.after.heading),
                                                timeout_ms=policy.replay_step_timeout_ms),
                              why="submit the form with Enter"))
            steps[-2].expect = Checkpoint(url=canon.url(entry.before.path), timeout_ms=policy.replay_step_timeout_ms)

    outputs: dict[str, OutputSpec] = {}
    for name, spec in contract.outputs.items():
        ex = extractions[name]
        d = spec if isinstance(spec, dict) else spec.model_dump()
        parse = {"money": "money", "integer": "integer", "date": "date"}.get(d.get("type", "string"), "text")
        outputs[name] = OutputSpec(type=d.get("type", "string"), description=d.get("description", ""),
                                   source=make_target(ex.element, ex.observation, canon), parse=parse)

    last = steps[-1].expect if steps else Checkpoint()
    success = Checkpoint(url=last.url, heading=last.heading, timeout_ms=policy.replay_step_timeout_ms)
    risk = "risky" if any(s.risk == "risky" for s in steps) else "safe"
    specs = {k: v.model_copy(update={"example": None} if v.sensitive else {"example": inputs.get(k, v.example)})
             for k, v in contract.inputs.items()}
    return Capability(
        id=contract.id, title=contract.title, description=contract.description or contract.title,
        app=AppRef(profile=profile_id, version=profile_version, entry=canon(contract.entry) or "/"),
        inputs=specs, outputs=outputs, steps=steps, success=success, risk=risk,
        provenance=Provenance(recorded_at=now_iso(), model=model, endpoint=endpoint, discovery_run=run_id,
                              teller_version=__version__))
