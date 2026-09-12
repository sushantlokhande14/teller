"""
Human-in-the-loop control transfer.

There is exactly one live session per run and exactly one controller at a time.
The ControlBroker owns that fact. When automation cannot safely continue it:

1. writes an intervention request (why, which step, what the screen shows, a
   screenshot, and how to reach the live browser) to the run directory,
2. flips the controller to "human" and refuses to act until it gets it back,
3. waits for a resume or abort message in the same directory, draining what
   the person does in the browser into the run log while it waits,
4. records the whole exchange as a Handoff on the result.

The mailbox is a directory because it is the simplest thing that works across
processes: the operator CLI (teller/operator.py) reads the request and writes
the reply, and a person can do the same with a text editor if they have to.
The live browser exposes a CDP endpoint so the operator can attach to the same
session from another process instead of getting a fresh one.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from teller.evidence import RunLog
from teller.schema import Handoff, HumanAction, now_iso

REQUEST_FILE = "intervention.json"
REPLY_FILE = "resume.json"
STATE_FILE = "control.json"


class ControlError(RuntimeError):
    pass


class ControlBroker:
    def __init__(self, run_dir: str | Path, surface: Any, log: RunLog, timeout_s: int = 600,
                 in_process_operator: Callable[[dict, Any], dict] | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.surface = surface
        self.log = log
        self.timeout_s = timeout_s
        self.controller = "automation"
        self.handoffs: list[Handoff] = []
        self._in_process_operator = in_process_operator
        self._write_state()

    # -- state ---------------------------------------------------------------------

    def _write_state(self, request: dict | None = None) -> None:
        state = {"controller": self.controller, "since": now_iso(), "request": request}
        (self.run_dir / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")

    def assert_automation(self) -> None:
        if self.controller != "automation":
            raise ControlError("a person holds the session; automation must not act")

    # -- the handoff ----------------------------------------------------------------

    def request(self, kind: str, reason: str, step_id: str | None, context: dict[str, Any]) -> Handoff:
        hid = f"h{len(self.handoffs) + 1}"
        shot = self.log.path("screens", f"handoff-{hid}.png")
        try:
            self.surface.screenshot(shot)
        except Exception:  # evidence is best effort; the handoff itself must still happen
            shot = None
        request = {
            "id": hid, "kind": kind, "reason": reason, "step_id": step_id, "requested_at": now_iso(),
            "screenshot": shot, "cdp_endpoint": getattr(self.surface, "cdp_endpoint", lambda: None)(),
            "reply_file": str(self.run_dir / REPLY_FILE),
            "instructions": ("Take over the live browser window (or attach via cdp_endpoint), do what is "
                             "needed, then reply with resume.json: {\"resolution\": \"resumed\"|\"aborted\", "
                             "\"note\": \"...\", \"resume_from\": \"verify\"|\"retry_step\"|\"next_step\"}"),
            **context,
        }
        self.surface.drain_human_actions()  # drop anything buffered before the person took over
        self.controller = "human"
        (self.run_dir / REPLY_FILE).unlink(missing_ok=True)
        (self.run_dir / REQUEST_FILE).write_text(json.dumps(request, indent=2), encoding="utf-8")
        self._write_state(request)
        handoff = Handoff(id=hid, kind=kind, reason=reason, step_id=step_id, requested_at=request["requested_at"])
        self.handoffs.append(handoff)
        self.log.event("handoff.requested", id=hid, kind=kind, reason=reason, step_id=step_id,
                       screenshot=shot, request_file=str(self.run_dir / REQUEST_FILE))

        reply = self._wait_for_reply(request, handoff)

        handoff.resolved_at = now_iso()
        handoff.resolution = reply.get("resolution", "timeout")
        handoff.operator_note = str(reply.get("note", ""))
        handoff.human_actions.extend(self._drain(handoff))
        self.controller = "automation"
        (self.run_dir / REQUEST_FILE).unlink(missing_ok=True)
        self._write_state()
        self.log.event("handoff.resolved", id=hid, resolution=handoff.resolution, note=handoff.operator_note,
                       resume_from=reply.get("resume_from", "verify"), actions=len(handoff.human_actions))
        handoff_reply = dict(reply)
        handoff_reply.setdefault("resume_from", "verify")
        self._last_reply = handoff_reply
        return handoff

    @property
    def last_reply(self) -> dict:
        return getattr(self, "_last_reply", {"resolution": "timeout", "resume_from": "verify"})

    def _wait_for_reply(self, request: dict, handoff: Handoff) -> dict:
        if self._in_process_operator is not None:
            # Tests and unattended evidence runs: a scripted stand-in for the person.
            reply = self._in_process_operator(request, self.surface)
            handoff.human_actions.extend(self._drain(handoff))
            return reply
        reply_path = self.run_dir / REPLY_FILE
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if reply_path.exists():
                try:
                    reply = json.loads(reply_path.read_text(encoding="utf-8"))
                except ValueError:
                    time.sleep(0.2)
                    continue
                reply_path.unlink(missing_ok=True)
                return reply
            handoff.human_actions.extend(self._drain(handoff))
            time.sleep(0.5)
        return {"resolution": "timeout", "note": f"no reply within {self.timeout_s}s"}

    def _drain(self, handoff: Handoff) -> list[HumanAction]:
        out = []
        for a in self.surface.drain_human_actions():
            act = HumanAction(at=a.get("at", now_iso()), kind=a.get("kind", "?"), detail=str(a.get("detail", "")))
            self.log.event("human.action", handoff=handoff.id, action=act.kind, detail=act.detail)
            out.append(act)
        return out
