// Draws numbered labels next to the controls the model can act on ("set of marks"),
// so the screenshot and the text listing refer to the same ref numbers.
(items) => {
  const old = document.getElementById("__teller_marks");
  if (old) old.remove();
  if (!items || !document.body) return;
  const box = document.createElement("div");
  box.id = "__teller_marks";
  box.style.cssText = "position:absolute;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;";
  for (const it of items) {
    const tag = document.createElement("div");
    tag.textContent = String(it.ref);
    const x = it.x + window.scrollX;
    const y = it.y + window.scrollY;
    tag.style.cssText = `position:absolute;left:${Math.max(0, x - 2)}px;top:${Math.max(0, y - 13)}px;` +
      "background:#c00;color:#fff;font:bold 10px/12px monospace;padding:0 3px;border-radius:2px;" +
      "border:1px solid #fff;pointer-events:none;";
    box.appendChild(tag);
    const outline = document.createElement("div");
    outline.style.cssText = `position:absolute;left:${x}px;top:${y}px;width:${it.w}px;height:${it.h}px;` +
      "outline:1px solid rgba(204,0,0,.7);pointer-events:none;";
    box.appendChild(outline);
  }
  document.body.appendChild(box);
}
