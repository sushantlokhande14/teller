"""
The operator side of a handoff. Deliberately minimal: a terminal, not a console.

    teller operator runs/<run_id>                    interactive: show the request, wait for a reply
    teller operator runs/<run_id> --script s.json    scripted: attach to the live browser over CDP,
                                                     perform the listed actions, then resume

Both modes touch the same live session the automation was driving. In the
interactive mode the person simply uses the browser window that is already
open (or attaches DevTools to the CDP endpoint printed in the request).

The scripted mode exists for tests and unattended evidence runs. It answers
every request the run raises: approvals with the script's `approve` flag,
unknown states with the script's `actions`, and anything it has no answer for
with an abort, so an unattended run never hangs waiting for a person.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from teller.handoff import REPLY_FILE, REQUEST_FILE


def wait_for_request(run_dir: Path, timeout_s: float) -> dict | None:
    """Wait for the next intervention request. Returns None when the run has ended."""
    deadline = time.monotonic() + timeout_s
    path = run_dir / REQUEST_FILE
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass  # half-written; try again
        elif (run_dir / "result.json").exists():
            return None
        time.sleep(0.3)
    return None


def reply(run_dir: Path, resolution: str, note: str = "", resume_from: str = "verify") -> None:
    body = {"resolution": resolution, "note": note, "resume_from": resume_from,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = run_dir / (REPLY_FILE + ".tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    tmp.replace(run_dir / REPLY_FILE)


def show(req: dict) -> None:
    print("=" * 70)
    print(f"INTERVENTION {req['id']}  kind={req['kind']}  step={req.get('step_id')}")
    print(f"reason:      {req['reason']}")
    for k in ("capability", "goal", "url", "heading", "screenshot", "cdp_endpoint"):
        if req.get(k):
            print(f"{k + ':':13}{req[k]}")
    if req.get("expected"):
        print(f"expected:    {req['expected']}")
    if req.get("observed"):
        print(f"observed:    {req['observed']}")
    print("=" * 70)


def perform_script(actions: list[dict[str, Any]], page, main_frame: list[str]) -> list[str]:
    """Run scripted operator actions on the live page. Returns notes for the reply."""
    notes = []

    def frame_at(path: list[str]):
        f = page.main_frame
        for name in path:
            f = next((c for c in f.child_frames if c.name == name), None)
            if f is None:
                raise RuntimeError(f"frame {path} not found on the live page")
        return f

    for a in actions:
        kind = next(iter(a))
        spec = a[kind]
        if kind == "wait":
            time.sleep(float(spec))
            continue
        frame = frame_at(spec.get("frame", main_frame))
        if kind == "navigate":
            frame.goto(spec["url"])
            notes.append(f"navigated to {spec['url']}")
            continue
        loc = frame.get_by_role(spec["role"], name=spec["name"], exact=True)
        if kind == "click":
            loc.click(timeout=10000)
            notes.append(f"clicked {spec['role']} \"{spec['name']}\"")
        elif kind == "fill":
            loc.fill(spec["value"], timeout=10000)
            notes.append(f"filled {spec['role']} \"{spec['name']}\"")
        else:
            raise ValueError(f"unknown scripted action {kind}")
        time.sleep(0.5)
    return notes


def answer_scripted(run_dir: Path, req: dict, script: dict) -> None:
    if req.get("kind") == "approval":
        if script.get("approve") is None:
            reply(run_dir, "aborted", "scripted operator has no approval decision for this run")
            print("replied: aborted (no approval decision in the script)")
            return
        resolution = "resumed" if script["approve"] else "aborted"
        reply(run_dir, resolution, script.get("note", "scripted approval"), "retry_step")
        print(f"replied: {resolution}")
        return
    actions = script.get("actions") or []
    endpoint = req.get("cdp_endpoint")
    if not actions or not endpoint:
        why = "no CDP endpoint (start the run with --cdp-port)" if actions else "scripted operator has no actions for this state"
        reply(run_dir, "aborted", why)
        print(f"replied: aborted ({why})")
        return
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].pages[0]
        notes = perform_script(actions, page, script.get("main_frame", ["main"]))
        browser.close()
    note = script.get("note", "") or "; ".join(notes)
    reply(run_dir, script.get("resolution", "resumed"), note, script.get("resume_from", "verify"))
    print(f"replied: resumed ({note})")


def run_scripted(run_dir: Path, script_path: Path, timeout_s: float) -> int:
    script = json.loads(script_path.read_text(encoding="utf-8"))
    handled = 0
    while True:
        req = wait_for_request(run_dir, timeout_s)
        if req is None:
            print(f"run finished; handled {handled} request(s)")
            return 0
        show(req)
        answer_scripted(run_dir, req, script)
        handled += 1
        # wait until the run has consumed this request before looking for the next one
        deadline = time.monotonic() + 30
        while (run_dir / REQUEST_FILE).exists() and time.monotonic() < deadline:
            time.sleep(0.2)


def run_interactive(run_dir: Path, timeout_s: float) -> int:
    print(f"waiting for an intervention request in {run_dir} ...")
    req = wait_for_request(run_dir, timeout_s)
    if req is None:
        print("nothing came in", file=sys.stderr)
        return 2
    show(req)
    print("Use the live browser window to sort it out, then type one of:")
    print("  resume [note]        automation re-checks the current step and carries on")
    print("  next [note]          you completed the step yourself; go to the next one")
    print("  retry [note]         re-run the current step from the top")
    print("  abort [note]         stop the run")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            line = "abort operator closed the terminal"
        cmd, _, note = line.partition(" ")
        if cmd in ("resume", "next", "retry"):
            reply(run_dir, "resumed", note, {"resume": "verify", "next": "next_step", "retry": "retry_step"}[cmd])
            return 0
        if cmd == "abort":
            reply(run_dir, "aborted", note)
            return 0
        print("unknown command")
