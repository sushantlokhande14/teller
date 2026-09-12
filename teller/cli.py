"""
Command line entry points.

    teller discover CONTRACT --input k=v ...     LLM-driven run, records a capability
    teller replay CAPABILITY --input k=v ...     deterministic replay, no model
    teller approve CAPABILITY --by NAME          draft -> approved (binds to a fingerprint)
    teller catalog [DIR]                         list capabilities; --tools prints tool schemas
    teller operator RUN_DIR [--script FILE]      the human side of a handoff
    teller fault KIND [--mode once|sticky|clear] inject a runtime fault into the sample app
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from teller.agent import ClaudeModel, DiscoveryRun, ScriptedModel
from teller.evidence import RunLog, new_run_id
from teller.handoff import ControlBroker
from teller.policy import Policy, Redactor
from teller.profile import load_profile
from teller.recorder import Contract, build_capability
from teller.replay import Replayer
from teller.schema import Capability, now_iso
from teller.surface.playwright_surface import PlaywrightSurface


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_inputs(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--input expects name=value, got {item!r}")
        k, _, v = item.partition("=")
        out[k.strip()] = v
    return out


def make_redactor(policy: Policy, specs: dict, inputs: dict[str, str], profile) -> Redactor:
    secrets = {k: v for k, v in inputs.items() if k in specs and specs[k].sensitive}
    if profile.auth:
        secrets["credential"] = os.environ.get(profile.auth.pass_env, "")
    return Redactor(policy.redaction.patterns, secrets)


def stop_operator(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()  # the run ended without needing a person


def start_operator(run_dir: Path, script: str, timeout: int) -> subprocess.Popen:
    """The operator runs in its own process and attaches to the live browser over CDP."""
    log = open(run_dir / "operator.log", "w", encoding="utf-8")
    return subprocess.Popen([sys.executable, "-m", "teller", "operator", str(run_dir), "--script", script,
                             "--timeout", str(timeout)], stdout=log, stderr=subprocess.STDOUT)


def set_fault(base_url: str, kind: str, mode: str) -> None:
    data = urllib.parse.urlencode({"kind": kind, "mode": mode}).encode()
    with urllib.request.urlopen(urllib.request.Request(base_url.rstrip("/") + "/__fault", data=data), timeout=5) as r:
        print(f"faults now: {r.read().decode()}")


# ------------------------------------------------------------------------- commands

def cmd_discover(args: argparse.Namespace) -> int:
    contract = Contract.load(args.contract)
    inputs = parse_inputs(args.input)
    missing = [k for k, s in contract.inputs.items() if s.required and k not in inputs]
    if missing:
        raise SystemExit(f"missing inputs: {missing}")
    profile = load_profile(args.app, args.tenant)
    policy = Policy.load(args.policy)
    redactor = make_redactor(policy, contract.inputs, inputs, profile)
    log = RunLog(args.runs, new_run_id("discovery"), redactor)
    model = ScriptedModel(json.loads(Path(args.scripted).read_text(encoding="utf-8"))) if args.scripted else ClaudeModel(args.model)
    surface = PlaywrightSurface(profile, headed=args.headed, cdp_port=args.cdp_port, allow_bbox=policy.coordinate_fallback)
    operator = start_operator(log.dir, args.operator, policy.handoff_timeout_s) if args.operator else None
    broker = ControlBroker(log.dir, surface, log, policy.handoff_timeout_s)
    print(f"run: {log.dir}")
    try:
        run = DiscoveryRun(contract, inputs, profile, policy, surface, model, log, broker, redactor)
        status = run.run()
        if status != "success":
            print(f"discovery {status}: {run.reason}")
            return 1
        cap = build_capability(run.trace, run.extractions, contract, inputs, profile.id, profile.version,
                               log.run_id, model.name, policy, redactor)
        out = Path(args.out) / f"{cap.id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        cap.save(str(out))
        log.save_json("capability.json", cap.model_dump(mode="json", exclude_none=True))
        print(f"capability saved: {out}  ({len(cap.steps)} steps, {len(cap.outputs)} outputs, risk={cap.risk}, status=draft)")
        print("extracted: " + json.dumps({k: v.raw for k, v in run.extractions.items()}))
        return 0
    finally:
        surface.close()
        log.close()
        stop_operator(operator)


def cmd_replay(args: argparse.Namespace) -> int:
    cap = Capability.load(args.capability)
    inputs = parse_inputs(args.input)
    profile = load_profile(cap.app.profile if args.app is None else args.app, args.tenant)
    policy = Policy.load(args.policy)
    redactor = make_redactor(policy, cap.inputs, inputs, profile)
    log = RunLog(args.runs, new_run_id("replay"), redactor)
    on_ready = None
    if args.fault:
        kind, _, mode = args.fault.partition(":")
        set_fault(profile.base_url, "", "clear")

        def on_ready() -> None:
            set_fault(profile.base_url, kind, mode or "once")
            log.event("fault.injected", kind=kind, mode=mode or "once")
    cdp_port = args.cdp_port or (9333 if args.operator else None)
    surface = PlaywrightSurface(profile, headed=args.headed, cdp_port=cdp_port, allow_bbox=policy.coordinate_fallback)
    operator = start_operator(log.dir, args.operator, policy.handoff_timeout_s) if args.operator else None
    broker = ControlBroker(log.dir, surface, log, policy.handoff_timeout_s)
    print(f"run: {log.dir}")
    try:
        result = Replayer(cap, profile, policy, surface, log, broker, redactor).run(inputs, allow_draft=args.allow_draft, on_ready=on_ready)
    finally:
        surface.close()
        log.close()
        stop_operator(operator)
    print(json.dumps(result.model_dump(mode="json", exclude_none=True), indent=2))
    return {"success": 0, "outcome": 3, "failure": 1}[result.status]


def cmd_approve(args: argparse.Namespace) -> int:
    cap = Capability.load(args.capability)
    cap.status = "approved"
    cap.provenance.approved_by = args.by
    cap.provenance.approved_at = now_iso()
    cap.provenance.approved_fingerprint = cap.fingerprint()
    cap.save(args.capability)
    print(f"{cap.id} v{cap.version} approved by {args.by} (fingerprint {cap.fingerprint()})")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    caps = [Capability.load(str(p)) for p in sorted(Path(args.dir).glob("*.json"))]
    if args.tools:
        print(json.dumps([c.to_tool_schema() for c in caps], indent=2))
        return 0
    for c in caps:
        current = "current" if c.approval_is_current() else ("stale" if c.status == "approved" else "-")
        print(f"{c.id}  v{c.version}  {c.status} ({current})  risk={c.risk}  app={c.app.profile} {c.app.version}")
        print(f"    {c.title}")
        print(f"    inputs:  " + ", ".join(f"{k}: {v.type}" for k, v in c.inputs.items()))
        print(f"    outputs: " + ", ".join(f"{k}: {v.type}" for k, v in c.outputs.items()))
        print(f"    outcomes: " + ", ".join(o.outcome for o in c.outcomes if o.outcome) + " (plus the app profile's)")
    return 0


def cmd_operator(args: argparse.Namespace) -> int:
    from teller.operator import run_interactive, run_scripted

    run_dir = Path(args.run_dir)
    if args.script:
        return run_scripted(run_dir, Path(args.script), args.timeout)
    return run_interactive(run_dir, args.timeout)


def cmd_fault(args: argparse.Namespace) -> int:
    profile = load_profile(args.app)
    set_fault(profile.base_url, args.kind, args.mode)
    return 0


# ------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="teller", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--app", default=None, help="app profile id or path (default: meridian / the capability's)")
        sp.add_argument("--tenant", default=None, help="tenant overlay yaml")
        sp.add_argument("--policy", default="policy.yaml")
        sp.add_argument("--runs", default="runs", help="where run directories go")
        sp.add_argument("--headed", action="store_true", help="show the browser")
        sp.add_argument("--cdp-port", type=int, default=None, help="expose the browser for operator attach")
        sp.add_argument("--operator", default=None, help="scripted operator json, run in a separate process")

    d = sub.add_parser("discover", help="LLM-driven run that records a capability")
    d.add_argument("contract")
    d.add_argument("--input", action="append")
    d.add_argument("--model", default=None)
    d.add_argument("--scripted", default=None, help="use a scripted stand-in model (no API key needed)")
    d.add_argument("--out", default="capabilities")
    common(d)
    d.set_defaults(func=cmd_discover, app_default="meridian")

    r = sub.add_parser("replay", help="deterministic replay of a saved capability")
    r.add_argument("capability")
    r.add_argument("--input", action="append")
    r.add_argument("--fault", default=None, help="inject a fault into the sample app first: kind[:once|sticky]")
    r.add_argument("--allow-draft", action="store_true")
    common(r)
    r.set_defaults(func=cmd_replay)

    a = sub.add_parser("approve", help="mark a capability approved for unattended replay")
    a.add_argument("capability")
    a.add_argument("--by", required=True)
    a.set_defaults(func=cmd_approve)

    c = sub.add_parser("catalog", help="list saved capabilities as an agent would see them")
    c.add_argument("dir", nargs="?", default="capabilities")
    c.add_argument("--tools", action="store_true", help="print function-calling tool definitions")
    c.set_defaults(func=cmd_catalog)

    o = sub.add_parser("operator", help="answer an intervention request")
    o.add_argument("run_dir")
    o.add_argument("--script", default=None)
    o.add_argument("--timeout", type=int, default=600)
    o.set_defaults(func=cmd_operator)

    f = sub.add_parser("fault", help="inject a runtime fault into the sample app")
    f.add_argument("kind")
    f.add_argument("--mode", default="once", choices=["once", "sticky", "clear"])
    f.add_argument("--app", default="meridian")
    f.set_defaults(func=cmd_fault)
    return p


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if getattr(args, "app", None) is None and args.cmd == "discover":
        args.app = "meridian"
    sys.exit(args.func(args))
