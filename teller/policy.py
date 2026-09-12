"""
Guardrails: what the automation may touch, which actions count as risky, and
what must never reach a log or an artifact.

The policy is plain YAML so a reviewer at the institution can read and change
it without touching code. It is enforced in one place (Policy.check) that both
discovery and replay call before every action.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from teller.schema import RiskLevel

RiskyMode = Literal["block", "confirm", "allow"]


class RiskyPolicy(BaseModel):
    mode: RiskyMode = "confirm"
    button_names: list[str] = Field(default_factory=list)   # regexes on the control's name
    url_patterns: list[str] = Field(default_factory=list)   # regexes on form actions / navigation targets


class DiscoveryPolicy(BaseModel):
    max_steps: int = 25
    step_timeout_ms: int = 10_000
    may_navigate: bool = False   # let the model type URLs instead of clicking what it sees


class RedactionPolicy(BaseModel):
    patterns: dict[str, str] = Field(default_factory=dict)


class Policy(BaseModel):
    allowed_origins: list[str]
    allowed_paths: list[str] = Field(default_factory=list)   # empty means any path on an allowed origin
    blocked_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["navigate", "click", "type", "select", "press", "extract"])
    risky: RiskyPolicy = Field(default_factory=RiskyPolicy)
    coordinate_fallback: bool = False
    escalate_on_unknown: bool = True
    require_approval: bool = True    # unattended replay only runs approved, unmodified capabilities
    handoff_timeout_s: int = 600
    replay_step_timeout_ms: int = 10_000
    discovery: DiscoveryPolicy = Field(default_factory=DiscoveryPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    # -- urls ---------------------------------------------------------------------

    def url_allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin} is not on the allowlist"
        path = parts.path
        for pat in self.blocked_paths:
            if re.search(pat, path):
                return False, f"path {path} is blocked by {pat!r}"
        if self.allowed_paths and not any(re.search(p, path) for p in self.allowed_paths):
            return False, f"path {path} is not on the allowlist"
        return True, ""

    # -- actions -------------------------------------------------------------------

    def classify(self, action: str, name: str = "", href_or_form: str = "", recorded: RiskLevel = "safe") -> RiskLevel:
        """Risk is decided from what a person would read on the control, plus where it posts."""
        if recorded == "risky":
            return "risky"
        if action in ("click", "press", "navigate"):
            if any(re.search(p, name or "") for p in self.risky.button_names):
                return "risky"
            if href_or_form and any(re.search(p, href_or_form) for p in self.risky.url_patterns):
                return "risky"
        return "safe"

    def check(self, action: str, current_url: str, name: str = "", href_or_form: str = "",
              recorded: RiskLevel = "safe", target_url: str | None = None) -> "Verdict":
        if action not in self.allowed_actions:
            return Verdict(False, "safe", f"action {action!r} is not permitted by policy")
        ok, why = self.url_allowed(current_url)
        if not ok:
            return Verdict(False, "safe", why)
        if action == "navigate" and target_url:
            ok, why = self.url_allowed(target_url)
            if not ok:
                return Verdict(False, "safe", why)
        risk = self.classify(action, name, href_or_form, recorded)
        if risk == "risky":
            if self.risky.mode == "block":
                return Verdict(False, "risky", f"{action} on {name!r} is risky and policy mode is block")
            if self.risky.mode == "confirm":
                return Verdict(True, "risky", f"{action} on {name!r} is risky; a person must confirm", needs_confirmation=True)
        return Verdict(True, risk, "")


@dataclass
class Verdict:
    allowed: bool
    risk: RiskLevel
    reason: str
    needs_confirmation: bool = False


# ------------------------------------------------------------------------- redaction

class Redactor:
    """Scrubs secrets and regulated data from anything that gets written down.

    Two layers: exact values we know are sensitive (declared sensitive inputs,
    the credentials the surface signed in with) and regex patterns from policy
    (SSNs, card numbers). Applied to every log line, every observation
    snapshot, and the model transcript before it hits disk.
    """

    def __init__(self, patterns: dict[str, str] | None = None, secrets: dict[str, str] | None = None) -> None:
        self._patterns = {k: re.compile(v) for k, v in (patterns or {}).items()}
        self._secrets = {k: v for k, v in (secrets or {}).items() if v}

    def add_secret(self, name: str, value: str) -> None:
        if value:
            self._secrets[name] = value

    def text(self, s: str) -> str:
        for name, value in self._secrets.items():
            if value in s:
                s = s.replace(value, f"[redacted:{name}]")
        for name, rx in self._patterns.items():
            s = rx.sub(f"[redacted:{name}]", s)
        return s

    def any(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.any(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.any(v) for v in obj]
        return obj
