"""
Run directories and the structured log.

Every run (discovery or replay) gets its own folder:

    runs/<run_id>/
      log.jsonl        one event per line: what happened, why, in order
      screens/         screenshots, one per observation, plus the failure shot
      snapshots/       the observation the decision was made on (json)
      result.json      the ReplayResult or discovery summary

Everything passes through the Redactor before it is written.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teller.policy import Redactor
from teller.surface.base import Observation


def new_run_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{kind}-{stamp}-{secrets.token_hex(2)}"


class RunLog:
    def __init__(self, root: str | Path, run_id: str, redactor: Redactor | None = None) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        (self.dir / "screens").mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self._seq = 0
        self._t0 = time.monotonic()
        self._fh = open(self.dir / "log.jsonl", "a", encoding="utf-8")

    def event(self, name: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        rec = {"seq": self._seq, "t_ms": int((time.monotonic() - self._t0) * 1000),
               "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": name}
        rec.update(self.redactor.any(fields))
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        return rec

    def save_observation(self, obs: Observation, tag: str) -> tuple[str | None, str]:
        shot = None
        if obs.screenshot:
            shot = str(self.dir / "screens" / f"{tag}.png")
            with open(shot, "wb") as f:
                f.write(obs.screenshot)
        snap = str(self.dir / "snapshots" / f"{tag}.json")
        with open(snap, "w", encoding="utf-8") as f:
            json.dump(self.redactor.any(obs.to_json()), f, indent=1, ensure_ascii=False)
        return shot, snap

    def save_json(self, name: str, data: Any) -> str:
        path = self.dir / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.redactor.any(data), f, indent=2, ensure_ascii=False)
        return str(path)

    def path(self, *parts: str) -> str:
        return str(self.dir.joinpath(*parts))

    def close(self) -> None:
        self._fh.close()
