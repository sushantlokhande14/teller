"""
Matching observations against the error taxonomy (Condition entries).

A condition list is checked in order, so more specific entries go first. The
replay engine passes the capability's own outcomes followed by the profile's
conditions.
"""
from __future__ import annotations

import re

from teller.schema import Condition, Detector, Handler, fill
from teller.surface.base import Observation


def detector_matches(d: Detector, obs: Observation, surface=None) -> bool:
    if d.url and not re.search(d.url, obs.path):
        return False
    if d.http_status and obs.http_status != d.http_status:
        return False
    if d.text and not re.search(d.text, obs.text):
        return False
    if d.element is not None:
        if surface is None or not surface.is_visible(d.element):
            return False
    return True


def condition_message(c: Condition, obs: Observation) -> str:
    if not c.message:
        return ""
    m = re.search(c.message, obs.text)
    if not m:
        return ""
    return (m.group(1) if m.groups() else m.group(0)).strip()


def find_condition(conditions: list[Condition], obs: Observation, surface=None) -> tuple[Condition, str] | None:
    for c in conditions:
        if detector_matches(c.when, obs, surface):
            return c, condition_message(c, obs)
    return None


def apply_handler(handler: Handler | None, surface, inputs: dict[str, str]) -> str | None:
    """Apply a recoverable condition's handler. Returns a reason string if it could not."""
    if handler is None:
        return None
    if handler.kind == "dismiss":
        control = surface.resolve(handler.target) if handler.target else None
        if control is None:
            return f"dismiss control not found: {handler.target.describe if handler.target else ''}"
        surface.click(control)
    elif handler.kind == "reauth":
        surface.login()
    elif handler.kind == "wait":
        surface.wait(handler.wait_ms)
    elif handler.kind == "navigate":
        surface.navigate(fill(handler.url, inputs) or "/")
    return None
