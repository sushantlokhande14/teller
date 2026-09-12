"""
Playwright implementation of the Surface protocol.

Perception is a script that runs in every frame (snapshot.js) and returns the
controls a person would see. Actions during discovery go through element
handles from the last snapshot; actions during replay go through locators built
from the recorded Target. The browser is launched with a CDP port so a human
operator (or another process) can attach to the very same session during a
handoff.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, sync_playwright

from teller.schema import MAIN_FRAME, AppProfile, Locator as LocatorSpec, Target, now_iso
from teller.surface.base import Element, Observation, Resolved

HERE = Path(__file__).parent
SNAPSHOT_JS = (HERE / "snapshot.js").read_text(encoding="utf-8")
MARKS_JS = (HERE / "marks.js").read_text(encoding="utf-8")
CAPTURE_JS = (HERE / "human_capture.js").read_text(encoding="utf-8")

# teller role -> Playwright ARIA role. "text" has no role and is matched by text.
ROLE_TO_ARIA = {"button": "button", "link": "link", "textbox": "textbox", "checkbox": "checkbox",
                "radio": "radio", "combobox": "combobox", "cell": "cell", "heading": "heading"}

TABLE_CELL_JS = """
({row, column}) => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName !== "HTML") {
      let part = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(el) + 1})`;
      }
      parts.unshift(part);
      el = parent;
    }
    return parts.join(" > ");
  };
  const wordMatch = (t, r) => t === r || new RegExp("(^|[^A-Za-z0-9])" + r.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&") + "([^A-Za-z0-9]|$)").test(t);
  const out = [];
  for (const table of document.querySelectorAll("table")) {
    const rows = Array.from(table.rows);
    let colIdx = -1;
    for (const r of rows) {
      const k = Array.from(r.cells).findIndex((c) => clean(c.innerText) === column);
      if (k >= 0) { colIdx = k; break; }
    }
    if (colIdx < 0) continue;
    for (const r of rows) {
      const cells = Array.from(r.cells);
      if (cells.length <= colIdx) continue;
      const texts = cells.map((c) => clean(c.innerText));
      if (texts[colIdx] === column) continue;
      if (texts.some((t, i) => i !== colIdx && wordMatch(t, row))) out.push(cssPath(cells[colIdx]));
    }
  }
  return out;
}
"""

DRAIN_HUMAN_JS = """() => {
  try {
    const l = JSON.parse(sessionStorage.getItem("__teller_human") || "[]");
    sessionStorage.removeItem("__teller_human");
    return l;
  } catch (e) { return []; }
}"""


class SurfaceError(RuntimeError):
    pass


def _stamp() -> str:
    """Millisecond UTC timestamp in the same shape the in-page capture script uses."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def xpath_quote(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", ".join(f"'{p}'" if "'" not in p else f'"{p}"' for p in re.split(r"(')", s) if p) + ")"


class PlaywrightSurface:
    def __init__(self, profile: AppProfile, headed: bool = False, cdp_port: int | None = None,
                 allow_bbox: bool = False, viewport: tuple[int, int] = (1280, 900),
                 video_dir: str | None = None) -> None:
        self.profile = profile
        self.allow_bbox = allow_bbox
        self.cdp_port = cdp_port
        self._pw = sync_playwright().start()
        args = [f"--remote-debugging-port={cdp_port}"] if cdp_port else []
        self._browser = self._pw.chromium.launch(headless=not headed, args=args)
        size = {"width": viewport[0], "height": viewport[1]}
        extra = {"record_video_dir": video_dir, "record_video_size": size} if video_dir else {}
        self._ctx = self._browser.new_context(viewport=size, **extra)
        self._ctx.add_init_script(CAPTURE_JS)
        self.page = self._ctx.new_page()
        self._status: int | None = None
        self._navigations: list[dict] = []
        self._dialogs: list[dict] = []
        self._last: Observation | None = None
        self._ref_map: dict[int, tuple[Frame, int]] = {}
        self.page.on("response", self._on_response)
        self.page.on("framenavigated", self._on_navigated)
        self.page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------------ frames

    def frame_path(self, frame: Frame) -> list[str]:
        path: list[str] = []
        f = frame
        while f.parent_frame is not None:
            path.append(f.name)
            f = f.parent_frame
        path.reverse()
        return path

    def frame_at(self, path: list[str]) -> Frame | None:
        f = self.page.main_frame
        for name in path:
            nxt = next((c for c in f.child_frames if c.name == name), None)
            if nxt is None:
                return None
            f = nxt
        return f

    def main_frame(self) -> Frame:
        """The content frame named by the profile, or the top document when the
        page has no frameset (the sign-in page, for instance)."""
        return self.frame_at(self.profile.main_frame) or self.page.main_frame

    def _frame_for(self, path: list[str]) -> Frame | None:
        """Resolve a target's frame path. The symbolic ["@main"] means this profile's
        content frame, whatever this tenant happens to call it."""
        if path == [MAIN_FRAME]:
            return self.main_frame()
        f = self.frame_at(path)
        if f is None and path == self.profile.main_frame:
            return self.page.main_frame
        return f

    def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        x = y = 0.0
        f = frame
        while f.parent_frame is not None:
            try:
                box = f.frame_element().bounding_box()
            except PlaywrightError:
                box = None
            if box:
                x += box["x"]
                y += box["y"]
            f = f.parent_frame
        return x, y

    # ------------------------------------------------------------------ events

    def _on_response(self, resp) -> None:
        try:
            req = resp.request
            if not req.is_navigation_request() or req.resource_type != "document":
                return
            path = self.frame_path(resp.frame)
            if path == self.profile.main_frame or not path:
                self._status = resp.status
        except PlaywrightError:
            pass

    def _on_navigated(self, frame: Frame) -> None:
        self._navigations.append({"at": _stamp(), "frame": "/".join(self.frame_path(frame)) or "top",
                                  "url": frame.url})

    def _on_dialog(self, dialog) -> None:
        # Unexpected JS dialogs are recorded and dismissed (the conservative choice).
        self._dialogs.append({"at": now_iso(), "type": dialog.type, "message": dialog.message})
        dialog.dismiss()

    def drain_navigations(self) -> list[dict]:
        out, self._navigations = self._navigations, []
        return out

    def drain_dialogs(self) -> list[dict]:
        out, self._dialogs = self._dialogs, []
        return out

    # ------------------------------------------------------------------ observe

    def settle(self, timeout_ms: int = 8000) -> None:
        time.sleep(0.15)
        for f in list(self.page.frames):
            try:
                f.wait_for_load_state("load", timeout=timeout_ms)
            except PlaywrightError:
                pass

    def observe(self, screenshot: bool = True) -> Observation:
        self.settle()
        elements: list[Element] = []
        ref_map: dict[int, tuple[Frame, int]] = {}
        frames: list[list[str]] = []
        main = self.main_frame()
        main_data: dict | None = None
        # Collect everything first, then number it. Controls a person can act on come
        # before the text they read, and the content frame comes before the surrounding
        # chrome, so the refs that matter for a task are the low ones. Without this the
        # menu of a frameset occupies the first ref numbers on every single screen.
        found: list[tuple[tuple, Element, Frame, int]] = []
        for order, frame in enumerate(list(self.page.frames)):
            path = self.frame_path(frame)
            data = self._snapshot(frame)
            if data is None:
                continue
            ox, oy = self._frame_offset(frame)
            frames.append(path)
            if frame == main:
                main_data = data
            for i, e in enumerate(data["elements"]):
                x, y, w, h = e["bbox"]
                el = Element(
                    ref=0, role=e["role"], name=e["name"], frame=path, bbox=(x + ox, y + oy, w, h),
                    tag=e["tag"], name_source=e.get("name_source", "content"),
                    value=e.get("value", ""), row_label=e.get("row_label", ""),
                    row_cells=e.get("row_cells", []), col_header=e.get("col_header", ""),
                    placeholder=e.get("placeholder", ""), options=e.get("options", []),
                    disabled=bool(e.get("disabled")), css=e.get("css", ""),
                    form_action=e.get("form_action", ""))
                found.append(((not el.interactive(), frame is not main, order, i), el, frame, e["idx"]))
        found.sort(key=lambda item: item[0])
        ref = 1
        for _, el, frame, idx in found:
            el.ref = ref
            elements.append(el)
            ref_map[ref] = (frame, idx)
            ref += 1
        if main_data is None:
            main_data = {"url": main.url, "title": "", "heading": None, "text": ""}
        shot = None
        if screenshot:
            self._draw_marks(elements)
            try:
                shot = self.page.screenshot(type="png")
            finally:
                self._draw_marks(None)
        obs = Observation(url=main_data["url"], path=self.path_of(main_data["url"]),
                          title=main_data.get("title", ""), heading=main_data.get("heading"),
                          text=main_data.get("text", ""), elements=elements, http_status=self._status,
                          frames=frames, taken_at=now_iso(), screenshot=shot)
        self._last = obs
        self._ref_map = ref_map
        return obs

    def _snapshot(self, frame: Frame) -> dict | None:
        for attempt in range(3):
            try:
                return frame.evaluate(SNAPSHOT_JS)
            except PlaywrightError as e:
                if "detached" in str(e).lower():
                    return None
                time.sleep(0.3 * (attempt + 1))  # execution context was replaced mid-navigation
        return None

    def _draw_marks(self, elements: list[Element] | None) -> None:
        for frame in list(self.page.frames):
            items = None
            if elements is not None:
                ox, oy = self._frame_offset(frame)
                path = self.frame_path(frame)
                items = [{"ref": el.ref, "x": el.bbox[0] - ox, "y": el.bbox[1] - oy, "w": el.bbox[2], "h": el.bbox[3]}
                         for el in elements if el.frame == path]
            try:
                frame.evaluate(MARKS_JS, items)
            except PlaywrightError:
                pass

    @staticmethod
    def path_of(url: str) -> str:
        parts = urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    # ------------------------------------------------------------------ act by ref (discovery)

    def _handle(self, ref: int):
        if ref not in self._ref_map:
            raise SurfaceError(f"ref {ref} is not in the current observation; observe again")
        frame, idx = self._ref_map[ref]
        try:
            h = frame.evaluate_handle("i => (window.__teller_refs || [])[i]", idx).as_element()
        except PlaywrightError as e:
            raise SurfaceError(f"the page changed since the last observation ({e.__class__.__name__})") from e
        if h is None:
            raise SurfaceError("the page changed since the last observation; observe again")
        return h

    def click_ref(self, ref: int) -> None:
        self._handle(ref).click(timeout=5000)
        self.settle()

    def fill_ref(self, ref: int, text: str, submit: bool = False) -> None:
        h = self._handle(ref)
        if h.evaluate("e => e.tagName") == "SELECT":
            h.select_option(label=text)
        else:
            h.fill(text, timeout=5000)
            if submit:
                h.press("Enter")
        self.settle()

    def select_ref(self, ref: int, option: str) -> None:
        self._handle(ref).select_option(label=option, timeout=5000)
        self.settle()

    def read_ref(self, ref: int) -> str:
        return self._read_handle(self._handle(ref))

    # ------------------------------------------------------------------ resolve (replay)

    def resolve(self, target: Target) -> Resolved | None:
        frame = self._frame_for(target.frame)
        if frame is None:
            return None
        tried: list[str] = []
        for i, spec in enumerate(target.locators):
            loc = self._build(frame, target, spec)
            if loc is None:
                tried.append(f"{spec.strategy}(unavailable)")
                continue
            if spec.strategy == "bbox":
                return Resolved(handle=loc, strategy="bbox", drift=i > 0, tried=tried)
            try:
                n = loc.count()
                visible = [k for k in range(n) if loc.nth(k).is_visible()] if n else []
            except PlaywrightError:
                n, visible = 0, []
            if len(visible) == 1:
                return Resolved(handle=loc.nth(visible[0]), strategy=spec.strategy, drift=i > 0, tried=tried)
            tried.append(f"{spec.strategy}({len(visible)} visible of {n})")
        return None

    def _build(self, frame: Frame, target: Target, spec: LocatorSpec):
        s, v = spec.strategy, spec.value
        if s == "role":
            aria = ROLE_TO_ARIA.get(target.role)
            if aria is None:
                return frame.get_by_text(v, exact=True)
            return frame.get_by_role(aria, name=v, exact=True)
        if s == "label":
            return frame.get_by_label(v, exact=True)
        if s == "placeholder":
            return frame.get_by_placeholder(v, exact=True)
        if s == "text":
            return frame.get_by_text(v, exact=True)
        if s == "row_label":
            # Legacy table forms: the label is the first cell of the row; the control (or the
            # value, for a cell target) sits in the cell after it.
            q = xpath_quote(v)
            row = f"//tr[*[self::td or self::th][1][normalize-space(.)={q}]]"
            if target.role in ("cell", "text"):
                return frame.locator(f"xpath={row}/*[self::td or self::th][2]")
            return frame.locator(f"xpath={row}//*[self::input or self::select or self::textarea or self::button or self::a]")
        if s == "table_cell":
            try:
                spec_d = json.loads(v)
                paths = frame.evaluate(TABLE_CELL_JS, spec_d)
            except (ValueError, PlaywrightError):
                return None
            if len(paths) != 1:
                return frame.locator("css=__teller_no_match__")
            return frame.locator("css=" + paths[0])
        if s == "css":
            return frame.locator("css=" + v)
        if s == "bbox":
            if not self.allow_bbox:
                return None
            x, y, w, h = json.loads(v)
            return ("bbox", x + w / 2, y + h / 2)
        return None

    # ------------------------------------------------------------------ act on resolved controls

    def click(self, control: Resolved) -> None:
        if control.strategy == "bbox":
            _, x, y = control.handle
            self.page.mouse.click(x, y)
        else:
            control.handle.click(timeout=5000)
        self.settle()

    def fill(self, control: Resolved, text: str) -> None:
        if control.strategy == "bbox":
            _, x, y = control.handle
            self.page.mouse.click(x, y)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.type(text)
        elif control.handle.evaluate("e => e.tagName") == "SELECT":
            control.handle.select_option(label=text, timeout=5000)
        else:
            control.handle.fill(text, timeout=5000)
        self.settle()

    def select(self, control: Resolved, option: str) -> None:
        control.handle.select_option(label=option, timeout=5000)
        self.settle()

    def read(self, control: Resolved) -> str:
        if control.strategy == "bbox":
            return ""
        return self._read_handle(control.handle)

    @staticmethod
    def _read_handle(h) -> str:
        tag = h.evaluate("e => e.tagName")
        if tag in ("INPUT", "TEXTAREA"):
            return h.input_value()
        if tag == "SELECT":
            return h.evaluate("e => e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : ''")
        return re.sub(r"\s+", " ", h.inner_text()).strip()

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self.settle()

    def navigate(self, url: str) -> None:
        if url.startswith("/"):
            url = self.profile.base_url.rstrip("/") + url
        frame = self.frame_at(self.profile.main_frame)
        try:
            if frame is not None:
                frame.goto(url, wait_until="load", timeout=15000)
            else:
                self.page.goto(url, wait_until="load", timeout=15000)
        except PlaywrightError as e:
            raise SurfaceError(f"navigation to {url} failed: {e.__class__.__name__}") from e
        self.settle()

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def is_visible(self, target: Target) -> bool:
        return self.resolve(target) is not None

    # ------------------------------------------------------------------ auth

    def login(self) -> None:
        """Sign in with credentials from the environment. The model never sees them."""
        auth = self.profile.auth
        if auth is None:
            return
        user = os.environ.get(auth.user_env, "")
        password = os.environ.get(auth.pass_env, "")
        if not user or not password:
            raise SurfaceError(f"credentials missing: set {auth.user_env} and {auth.pass_env}")
        if self.resolve(auth.user_target) is None:
            self.navigate(auth.login_url)
        u, p, s = (self.resolve(t) for t in (auth.user_target, auth.pass_target, auth.submit_target))
        if not (u and p and s):
            raise SurfaceError("sign-in form not found where the profile expects it")
        u.handle.fill(user)
        p.handle.fill(password)
        s.handle.click()
        self.settle()
        obs = self.observe(screenshot=False)
        from teller.conditions import detector_matches  # local import to avoid a cycle
        if not detector_matches(auth.signed_in_check, obs, self):
            raise SurfaceError("sign-in did not succeed; check the demo credentials")

    # ------------------------------------------------------------------ evidence and handoff

    def screenshot(self, path: str) -> None:
        self.page.screenshot(path=path, type="png")

    def drain_human_actions(self) -> list[dict]:
        out: list[dict] = []
        for frame in list(self.page.frames):
            try:
                out.extend(frame.evaluate(DRAIN_HUMAN_JS) or [])
            except PlaywrightError:
                pass
        for nav in self.drain_navigations():
            out.append({"at": nav["at"], "kind": "navigate", "detail": f"{nav['frame']} -> {self.path_of(nav['url'])}"})
        out.sort(key=lambda a: a["at"])
        return out

    def cdp_endpoint(self) -> str | None:
        return f"http://127.0.0.1:{self.cdp_port}" if self.cdp_port else None

    def close(self) -> None:
        try:
            self._ctx.close()
            self._browser.close()
        finally:
            self._pw.stop()
