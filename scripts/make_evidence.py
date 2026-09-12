"""
Regenerate everything under evidence/ from scratch.

    python scripts/make_evidence.py --provider ollama --model qwen2.5:7b   local model, no cost
    python scripts/make_evidence.py                                        default provider (anthropic)
    python scripts/make_evidence.py --scripted                             stand-in model, pipeline check only

Assumes the sample app is running (python -m meridian). Each run directory is
copied into evidence/<name>/ and evidence/README.md is rewritten with the results.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"
RUNS = ROOT / "runs"
CAPS = ROOT / "capabilities"


def sh(*args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "-m", "teller", *args], cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    print(f"$ teller {' '.join(args)}\n{out.strip()[-1200:]}\n")
    return proc.returncode, out


def newest_run() -> Path:
    return sorted(RUNS.iterdir(), key=lambda p: p.stat().st_mtime)[-1]


def keep(name: str, note: str, index: list[str]) -> Path:
    src = newest_run()
    dst = EVIDENCE / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.tmp"))
    result = dst / "result.json"
    summary = ""
    if not result.exists() and (dst / "log.jsonl").exists():
        # a discovery run: summarize from its last log event
        events = [json.loads(l) for l in (dst / "log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        end = next((e for e in reversed(events) if e.get("event") == "discovery.end"), None)
        start = next((e for e in events if e.get("event") == "discovery.start"), None)
        if end:
            summary = f"{end['status']} after {end.get('actions')} actions"
            if start:
                summary += f" ({start.get('model')})"
            if end.get("extracted"):
                summary += " " + json.dumps(end["extracted"])
    if result.exists():
        r = json.loads(result.read_text(encoding="utf-8"))
        summary = r["status"]
        if r.get("outputs"):
            summary += " " + json.dumps(r["outputs"])
        if r.get("outcome"):
            summary += f" {r['outcome']['code']}: {r['outcome']['message']}"
        if r.get("failure"):
            summary += f" {r['failure']['code']} at {r['failure'].get('step_id')}: {r['failure']['observed']}"
        if r.get("handoffs"):
            h = r["handoffs"][0]
            summary += f" | handoff {h['kind']} -> {h['resolution']}"
    index.append(f"| [{name}]({name}/) | {note} | {summary} |")
    return dst


def reset_app(base_url: str) -> None:
    for path, data in (("/__fault", b"mode=clear"), ("/__reset", b"")):
        urllib.request.urlopen(urllib.request.Request(base_url + path, data=data), timeout=5).read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripted", action="store_true", help="use the scripted stand-in model instead of an LLM")
    ap.add_argument("--provider", default=None, help="model provider for the discovery runs")
    ap.add_argument("--model", default=None, help="model id for the discovery runs")
    args = ap.parse_args()
    base_url = os.environ.get("MERIDIAN_URL", "http://127.0.0.1:5057")
    if args.scripted:
        model_args = ["--scripted", "scripts/scripted_model_lookup.json"]
    else:
        model_args = []
        if args.provider:
            model_args += ["--provider", args.provider]
        if args.model:
            model_args += ["--model", args.model]
    reset_app(base_url)
    shutil.rmtree(RUNS, ignore_errors=True)
    EVIDENCE.mkdir(exist_ok=True)
    index: list[str] = []

    # 1. discovery: the model works out the lookup flow and the recorder turns it into a capability
    code, _ = sh("discover", "contracts/member_savings_balance.json", "--input", "member_id=100234", *model_args)
    if code != 0:
        print("discovery failed; stopping")
        return 1
    keep("discovery-member-savings-balance", "LLM-driven run that recorded the capability", index)
    shutil.copy(CAPS / "member_savings_balance.json", EVIDENCE / "member_savings_balance.json")
    sh("approve", "capabilities/member_savings_balance.json", "--by", "sushant")

    # 2. replays of that capability
    sh("replay", "capabilities/member_savings_balance.json", "--input", "member_id=101502")
    keep("replay-success", "different member than the recording; Savings sits in a different table row", index)
    sh("replay", "capabilities/member_savings_balance.json", "--input", "member_id=999999")
    keep("replay-outcome-not-found", "a member that does not exist", index)
    sh("replay", "capabilities/member_savings_balance.json", "--input", "member_id=12")
    keep("replay-bad-input", "input rejected by the contract before the app is touched", index)
    for fault, note in (("session_expired", "session dropped mid-flow; re-auth and re-run the page"),
                        ("notice", "system notice interstitial dismissed by the profile handler"),
                        ("slow", "core host busy page; wait and re-check"),
                        ("permission", "permission denial surfaced as an outcome"),
                        ("error", "application error stops the run with a screenshot and snapshot")):
        sh("replay", "capabilities/member_savings_balance.json", "--input", "member_id=100234", "--fault", fault)
        keep(f"replay-fault-{fault.replace('_', '-')}", note, index)
    sh("replay", "capabilities/member_savings_balance.json", "--input", "member_id=100234", "--fault", "surprise",
       "--operator", "scripts/operator_dismiss_alert.json")
    keep("replay-escalation-unknown-screen", "unknown alert; a person takes the live session over CDP and hands it back", index)

    # 3. a risky capability: opening a sub-account posts to the core
    if args.scripted:
        model_args = ["--scripted", "scripts/scripted_model_subaccount.json"]
    code, _ = sh("discover", "contracts/open_savings_subaccount.json", "--input", "member_id=100234",
                 "--input", "nickname=Vacation fund", "--input", "deposit=25.00", "--operator", "scripts/operator_approve.json",
                 *model_args)
    if code == 0:
        keep("discovery-open-savings-subaccount", "LLM-driven run; the confirm click needed operator approval", index)
        shutil.copy(CAPS / "open_savings_subaccount.json", EVIDENCE / "open_savings_subaccount.json")
        sh("approve", "capabilities/open_savings_subaccount.json", "--by", "sushant")
        sh("replay", "capabilities/open_savings_subaccount.json", "--input", "member_id=100234",
           "--input", "nickname=Rainy day", "--input", "deposit=40.00", "--operator", "scripts/operator_approve.json")
        keep("replay-risky-approved", "operator approves the confirm step; new account number returned", index)
        sh("replay", "capabilities/open_savings_subaccount.json", "--input", "member_id=100877",
           "--input", "nickname=Holiday", "--input", "deposit=10.00", "--operator", "scripts/operator_decline.json")
        keep("replay-risky-declined", "operator declines; run stops before anything is posted", index)

    model_note = ("the scripted stand-in model (pipeline check only)" if args.scripted
                  else f"{args.model or 'the default model'}" + (f" on {args.provider}" if args.provider else ""))
    readme = ["# Evidence", "",
              f"Generated by `python scripts/make_evidence.py{' --scripted' if args.scripted else ''}` on the sample app, "
              f"with discovery driven by {model_note}.", "",
              "Each folder is a complete run directory: `log.jsonl` (every observation, decision, action, checkpoint, "
              "condition, recovery and handoff in order), `screens/` (a screenshot per step, plus failure and handoff "
              "shots), `snapshots/` (what the surface reported at each step), and `result.json`. Discovery runs also "
              "carry `transcript.json` (the model conversation, screenshots removed, secrets redacted) and "
              "`capability.json` (the artifact as recorded, before approval).", "",
              "| run | what it shows | result |", "|---|---|---|", *index, ""]
    (EVIDENCE / "README.md").write_text("\n".join(readme), encoding="utf-8")
    print(f"wrote {EVIDENCE / 'README.md'} with {len(index)} runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
