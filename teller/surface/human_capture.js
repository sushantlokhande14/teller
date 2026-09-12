// Installed in every frame before any page script runs. It only buffers what a
// person does while they hold control of the session; teller drains the buffer
// during a handoff and appends it to the run log. Typed values are never
// recorded, only which field was touched. The buffer lives in sessionStorage so
// it survives the navigation that a click usually causes.
(() => {
  const KEY = "__teller_human";
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const push = (entry) => {
    try {
      const log = JSON.parse(sessionStorage.getItem(KEY) || "[]");
      log.push(entry);
      sessionStorage.setItem(KEY, JSON.stringify(log));
    } catch (e) { /* storage unavailable: nothing to do */ }
  };
  const describe = (el) => {
    if (!el || el.nodeType !== 1) return "?";
    const tag = el.tagName.toLowerCase();
    const label = el.getAttribute("aria-label") || (el.value && ["submit", "button"].includes(el.type) && el.value)
      || ((tag === "a" || tag === "button") && el.innerText) || el.name || el.placeholder || el.innerText;
    return `${tag} "${clean(label)}"`;
  };
  const frame = () => (window.name ? `frame ${window.name}` : "top");
  document.addEventListener("click", (e) => {
    const t = e.target.closest("a,button,input,select,label") || e.target;
    push({at: new Date().toISOString(), kind: "click", detail: `${describe(t)} in ${frame()}`});
  }, true);
  document.addEventListener("change", (e) => {
    const t = e.target;
    const what = t.tagName === "SELECT" ? `chose "${clean(t.options[t.selectedIndex] && t.options[t.selectedIndex].text)}"`
      : `entered ${String(t.value || "").length} chars`;
    push({at: new Date().toISOString(), kind: "input", detail: `${describe(t)} ${what}`});
  }, true);
  document.addEventListener("submit", (e) => {
    push({at: new Date().toISOString(), kind: "submit", detail: `form action="${e.target.getAttribute("action") || ""}"`});
  }, true);
})();
