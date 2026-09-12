"""Print the interesting part of a replay's JSON output (used by the demo commands)."""
import json
import sys

lines = sys.stdin.read().splitlines()
start = next((i for i, line in enumerate(lines) if line == "{"), None)
if start is None:
    print("\n".join(lines))
    sys.exit(1)
print("\n".join(lines[:start]).strip())
r = json.loads("\n".join(lines[start:]))
print("status:", r["status"])
if r.get("outputs"):
    print("outputs:", json.dumps(r["outputs"]))
if r.get("outcome"):
    print("outcome:", json.dumps(r["outcome"]))
if r.get("failure"):
    f = r["failure"]
    print("failure:", f["code"], "| step", f.get("step_id"), "| expected", f.get("expected"), "| observed", f.get("observed"))
    print("evidence:", json.dumps(f.get("evidence")))
for h in r.get("handoffs", []):
    print("handoff:", h["id"], h["kind"], "->", h["resolution"], "| note:", h["operator_note"],
          "| human actions:", [a["detail"] for a in h["human_actions"]])
print("steps:", [(s["id"], s["status"], s.get("locator_used"), s.get("recoveries")) for s in r["steps"]])
