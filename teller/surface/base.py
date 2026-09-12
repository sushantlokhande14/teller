"""
The seam between "how we perceive and act on a surface" and "the recorded flow".

A Surface turns whatever is on screen into an Observation (a flat list of
controls described by role, name and position, plus visible text), and executes
actions against it. Discovery acts on refs from the latest observation; replay
acts on Targets resolved through their recorded locators. Nothing above this
layer knows about Playwright, DOM, or frames beyond a frame *name path*.

A desktop surface would implement the same protocol over an accessibility API
(UIA on Windows, AX on macOS) plus screenshots, with window titles in place of
frame names. See REPORT.md, section 4.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from teller.schema import Target


@dataclass
class Element:
    ref: int
    role: str                      # button | link | textbox | checkbox | radio | combobox | cell | heading | text
    name: str                      # accessible name, computed the way a screen reader would
    frame: list[str]               # frame name path from the top document
    bbox: tuple[float, float, float, float]  # x, y, w, h in top-level viewport pixels
    tag: str = ""
    name_source: str = "content"   # aria | label | content | placeholder | row | attr
    value: str = ""                # current value (inputs) or selected option
    row_label: str = ""            # text of the first cell in the same table row
    row_cells: list[str] = field(default_factory=list)  # every cell in the row, in order
    col_header: str = ""           # header text above this cell, if the table has one
    placeholder: str = ""
    options: list[str] = field(default_factory=list)
    disabled: bool = False
    form_action: str = ""          # where the enclosing form posts, for risk classification
    css: str = ""                  # structural path; brittle, recorded only as a last resort

    INTERACTIVE = ("link", "button", "textbox", "checkbox", "radio", "combobox")

    def interactive(self) -> bool:
        return self.role in self.INTERACTIVE

    def short(self) -> str:
        bits = [f"[{self.ref}] {self.role} \"{self.name}\""]
        if self.frame:
            bits.append(f"frame={'/'.join(self.frame)}")
        if self.value and self.role in ("textbox", "combobox"):
            bits.append(f"value=\"{self.value}\"")
        if self.row_label and self.row_label != self.name:
            bits.append(f"row=\"{self.row_label}\"")
        if self.col_header:
            bits.append(f"column=\"{self.col_header}\"")
        if self.options:
            bits.append("options=" + json.dumps(self.options))
        if self.disabled:
            bits.append("disabled")
        return " ".join(bits)


@dataclass
class Observation:
    url: str                       # main frame URL
    path: str                      # main frame path (+query), what checkpoints compare against
    title: str
    heading: str | None
    text: str                      # visible text of the main frame, whitespace collapsed
    elements: list[Element]
    http_status: int | None
    frames: list[list[str]]
    taken_at: str
    screenshot: bytes | None = None

    def find(self, ref: int) -> Element:
        for el in self.elements:
            if el.ref == ref:
                return el
        raise KeyError(f"no element with ref {ref} in the current observation")

    def to_prompt(self, max_text: int = 3000) -> str:
        lines = [f"URL: {self.path}" + (f"   [HTTP {self.http_status}]" if self.http_status else "")]
        if self.heading:
            lines.append(f"Heading: {self.heading}")
        controls = [el for el in self.elements if el.interactive()]
        rest = [el for el in self.elements if not el.interactive()]
        lines.append("Controls you can act on (use the ref number):")
        lines.extend(el.short() for el in controls)
        if rest:
            lines.append("Text and table cells on the page (read only, extract by ref):")
            lines.extend(el.short() for el in rest)
        text = self.text if len(self.text) <= max_text else self.text[:max_text] + " ..."
        lines.append("Visible text of the main frame:")
        lines.append(text)
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "screenshot"}
        return d


@dataclass
class Resolved:
    """A control found on the live surface, and how it was found."""

    handle: Any
    strategy: str
    drift: bool                    # True when a fallback locator had to be used
    tried: list[str]               # strategies that missed before this one


@runtime_checkable
class Surface(Protocol):
    def observe(self, screenshot: bool = True) -> Observation: ...
    def resolve(self, target: Target) -> Resolved | None: ...
    def click(self, control: Resolved) -> None: ...
    def fill(self, control: Resolved, text: str) -> None: ...
    def select(self, control: Resolved, option: str) -> None: ...
    def read(self, control: Resolved) -> str: ...
    def press(self, key: str) -> None: ...
    def navigate(self, url: str) -> None: ...
    def wait(self, ms: int) -> None: ...
    def login(self) -> None: ...
    def screenshot(self, path: str) -> None: ...
    def close(self) -> None: ...
