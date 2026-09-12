"""
Turn a run directory into a captioned animated GIF, for the README.

    python scripts/make_filmstrip.py runs/<run id> docs/discovery.gif

Every frame is a screenshot teller already saved during the run, and every caption
is read out of that run's log, so the animation is the evidence rather than a
staged recreation. Needs Pillow only; no ffmpeg, no screen recorder.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BAR = 80          # title line plus up to two caption lines
LINE = 23
PAD = 12
BG = (24, 26, 32)
FG = (238, 240, 245)
DIM = (150, 156, 170)
ACCENT = {"handoff": (255, 196, 86), "failure": (255, 128, 128), "outcome": (130, 200, 255),
          "final": (140, 220, 160), "condition": (255, 196, 86)}


def font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def captions_from_log(run_dir: Path) -> dict[str, tuple[str, str]]:
    """screenshot tag -> (kind, caption), derived from what the run recorded."""
    events = [json.loads(l) for l in (run_dir / "log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out: dict[str, tuple[str, str]] = {}
    pending_decide: dict[str, str] = {}
    for e in events:
        name = e.get("event")
        if name == "discovery.start":
            out["step-00"] = ("", f"goal: {e.get('goal')}")
        elif name == "replay.start":
            out["__start__"] = ("", f"replaying {e.get('capability')} v{e.get('version')} with {json.dumps(e.get('inputs'))}")
        elif name == "decide" and e.get("tool"):
            why = (e.get("args") or {}).get("why") or (e.get("args") or {}).get("summary") or ""
            pending_decide[str(e.get("step"))] = f"model: {e['tool']} - {why}"
        elif name == "act" and e.get("step") is not None:
            step = str(e["step"])
            if step.isdigit():  # discovery numbers its observations step-NN
                out[f"step-{int(step):02d}"] = ("act", pending_decide.get(step, f"{e.get('tool')}"))
            else:
                target = e.get("target") or ""
                value = f' "{e["value"]}"' if e.get("value") else ""
                risky = "  [risky]" if e.get("risk") == "risky" else ""
                out[f"after-{step}"] = ("act", f"{step}: {e.get('action')}{value} on {target}{risky}")
        elif name == "condition":
            tag = f"outcome-{e.get('step')}"
            out[tag] = ("outcome", f"{e.get('kind')} condition '{e.get('id')}': {e.get('message') or ''}".strip())
        elif name == "handoff.requested":
            out[f"handoff-{e.get('id')}"] = ("handoff", f"stopped and asked a person: {e.get('reason')}")
        elif name == "failure":
            out[f"failure-{e.get('step') or 'final'}"] = ("failure", f"{e.get('code')}: {e.get('observed')}")
        elif name == "replay.end":
            bits = [f"result: {e.get('status')}"]
            if e.get("outputs"):
                bits.append(json.dumps(e["outputs"]))
            out["final"] = ("final", "  ".join(bits))
        elif name == "discovery.end":
            extracted = json.dumps(e.get("extracted")) if e.get("extracted") else ""
            out["__end__"] = ("final", f"discovery {e.get('status')}: {extracted}")
    return out


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.ImageFont, width: int) -> str:
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=f) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
        if len(lines) == 2:
            break
    if line and len(lines) < 2:
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=900)
    ap.add_argument("--ms", type=int, default=2000, help="milliseconds per frame")
    ap.add_argument("--last-ms", type=int, default=3500)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    shots = sorted((run_dir / "screens").glob("*.png"), key=lambda p: p.stat().st_mtime)
    if not shots:
        print(f"no screenshots in {run_dir}/screens", file=sys.stderr)
        return 2
    captions = captions_from_log(run_dir)
    title = run_dir.name
    big, small = font(19), font(15)

    frames = []
    for shot in shots:
        kind, text = captions.get(shot.stem, ("", shot.stem.replace("-", " ")))
        if shot.stem == shots[-1].stem and "__end__" in captions:
            kind, text = captions["__end__"]
        img = Image.open(shot).convert("RGB")
        w = args.width
        h = round(img.height * w / img.width)
        img = img.resize((w, h), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h + BAR), BG)
        canvas.paste(img, (0, BAR))
        draw = ImageDraw.Draw(canvas)
        draw.text((PAD, 6), title, font=small, fill=DIM)
        draw.multiline_text((PAD, 26), wrap(draw, text, big, w - 2 * PAD), font=big,
                            fill=ACCENT.get(kind, FG), spacing=LINE - 19)
        frames.append(canvas.convert("P", palette=Image.ADAPTIVE, colors=128))

    durations = [args.ms] * (len(frames) - 1) + [args.last_ms]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   disposal=2, optimize=False)  # each frame replaces the last, no ghosting
    print(f"{out}  {len(frames)} frames  {out.stat().st_size / 1_000_000:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
