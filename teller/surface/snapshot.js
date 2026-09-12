// Perception script. Runs inside one frame and returns what a person would see:
// interactive controls, headings and table cells, each with a name computed the way
// a screen reader would compute it. No ids, classes or test hooks are used for
// identity; a structural CSS path is included only as a last-resort locator.
(() => {
  const MAX = 160;

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const ownText = (el) => clean(Array.from(el.childNodes)
    .filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" "));

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

  const rowInfo = (el) => {
    const tr = el.closest("tr");
    if (!tr) return {label: "", cells: [], col: ""};
    const cells = Array.from(tr.children).filter((c) => c.tagName === "TD" || c.tagName === "TH");
    const texts = cells.map((c) => clean(c.innerText));
    const myCell = el.tagName === "TD" || el.tagName === "TH" ? el : el.closest("td,th");
    const idx = cells.indexOf(myCell);
    let col = "";
    const table = tr.closest("table");
    if (table && idx >= 0 && cells.length >= 2) {
      const rows = Array.from(table.querySelectorAll(":scope > tbody > tr, :scope > tr, :scope > thead > tr"));
      for (const r of rows) {
        if (r === tr) break;
        const hc = Array.from(r.children).filter((c) => c.tagName === "TD" || c.tagName === "TH");
        if (hc.length === cells.length && hc[idx]) {
          const t = clean(hc[idx].innerText);
          const looksHeader = hc[idx].tagName === "TH" || hc[idx].querySelector("b,strong") !== null;
          if (t && looksHeader) col = t;
        }
      }
    }
    return {label: texts[0] || "", cells: texts, col: col};
  };

  // Returns [name, source]. The source says which rule produced the name, so the
  // recorder knows whether an ARIA role+name locator will actually match later.
  const accessibleName = (el) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return [clean(aria), "aria"];
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean)
        .map((n) => clean(n.innerText)).join(" ");
      if (t) return [t, "aria"];
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab && clean(lab.innerText)) return [clean(lab.innerText), "label"];
    }
    const wrap = el.closest("label");
    if (wrap && clean(wrap.innerText)) return [clean(wrap.innerText), "label"];
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "INPUT" && ["submit", "button", "reset"].includes(type)) return [clean(el.value) || type, "content"];
    if (tag === "BUTTON" || tag === "A" || el.getAttribute("role")) {
      const t = clean(el.innerText);
      if (t) return [t, "content"];
      const img = el.querySelector("img[alt]");
      if (img) return [clean(img.getAttribute("alt")), "content"];
    }
    if (el.placeholder) return [clean(el.placeholder), "placeholder"];
    if (el.title) return [clean(el.title), "attr"];
    // Legacy table forms: the label is the text of the previous cell in the row.
    const cell = el.closest("td,th");
    if (cell) {
      let prev = cell.previousElementSibling;
      while (prev && !clean(prev.innerText)) prev = prev.previousElementSibling;
      if (prev && clean(prev.innerText)) return [clean(prev.innerText), "row"];
    }
    return [el.getAttribute("name") || "", "attr"];
  };

  const roleOf = (el) => {
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "text").toLowerCase();
    const r = el.getAttribute("role");
    if (r) return r;
    if (tag === "A") return "link";
    if (tag === "BUTTON") return "button";
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT") {
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "TD" || tag === "TH") return "cell";
    if (/^H[1-6]$/.test(tag)) return "heading";
    return "text";
  };

  // A heading is bold text that is clearly larger than the text around it. "Around
  // it" means the nearest ancestor that holds other text, because legacy pages set
  // sizes with nested <font> tags rather than on the body.
  const contextFontSize = (el) => {
    const mine = clean(el.innerText).length;
    let a = el.parentElement;
    while (a && a !== document.documentElement) {
      if (clean(a.innerText).length > mine + 3) return parseFloat(getComputedStyle(a).fontSize);
      a = a.parentElement;
    }
    return parseFloat(getComputedStyle(document.body).fontSize) || 13;
  };
  const looksLikeHeading = (el) => {
    if (/^H[1-6]$/.test(el.tagName) || el.getAttribute("role") === "heading") return true;
    if (!["B", "STRONG", "FONT", "SPAN", "DIV"].includes(el.tagName)) return false;
    const t = ownText(el);
    if (!t || t.length > 80) return false;
    const cs = getComputedStyle(el);
    const big = parseFloat(cs.fontSize) >= contextFontSize(el) * 1.1;
    const bold = parseInt(cs.fontWeight, 10) >= 600 || cs.fontWeight === "bold";
    return big && bold;
  };

  const out = [];
  const refs = [];
  const push = (el, role, name, extra) => {
    if (out.length >= MAX) return;
    const r = el.getBoundingClientRect();
    refs.push(el);
    out.push(Object.assign({
      idx: refs.length - 1, role, name, tag: el.tagName.toLowerCase(),
      bbox: [r.left, r.top, r.width, r.height], css: cssPath(el),
    }, extra || {}));
  };

  // 1. interactive controls
  const interactive = document.querySelectorAll(
    "a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [onclick]");
  for (const el of interactive) {
    if (!visible(el)) continue;
    const role = roleOf(el);
    const info = rowInfo(el);
    let value = "";
    let options = [];
    if (el.tagName === "SELECT") {
      options = Array.from(el.options).map((o) => clean(o.text));
      value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
    } else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "password") value = el.value ? "(hidden)" : "";
      else if (type === "checkbox" || type === "radio") value = el.checked ? "checked" : "";
      else if (!["submit", "button", "reset"].includes(type)) value = el.value || "";
    }
    const form = el.closest("form");
    const [name, source] = accessibleName(el);
    push(el, role, name, {
      name_source: source,
      value, options, disabled: !!el.disabled, placeholder: el.placeholder || "",
      row_label: info.label, row_cells: info.cells, col_header: info.col,
      form_action: el.tagName === "A" ? (el.getAttribute("href") || "") : (form ? (form.getAttribute("action") || "") : ""),
    });
  }

  // 2. headings (real ones and things styled like them)
  let heading = null;
  const headingCandidates = document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role=heading],b,strong,font");
  for (const el of headingCandidates) {
    if (!visible(el) || !looksLikeHeading(el)) continue;
    const t = clean(el.innerText);
    if (!t) continue;
    if (heading === null) heading = t;
    push(el, "heading", t, {});
  }

  // 3. table cells and definition text that carry data, without controls inside
  const cells = document.querySelectorAll("td, th, dd, li");
  for (const el of cells) {
    if (!visible(el)) continue;
    if (el.querySelector("a[href],button,input,select,textarea,table")) continue;
    const t = clean(el.innerText);
    if (!t || t.length > 120) continue;
    // Skip cells that only wrap something already listed as a heading.
    const hd = Array.from(el.querySelectorAll("b,strong,font,h1,h2,h3,h4")).find(looksLikeHeading);
    if (hd && clean(hd.innerText) === t) continue;
    const info = rowInfo(el);
    push(el, roleOf(el), t, {name_source: "content", row_label: info.label, row_cells: info.cells, col_header: info.col});
  }

  window.__teller_refs = refs;
  return {
    url: location.href,
    title: document.title,
    heading,
    text: clean(document.body ? document.body.innerText : "").slice(0, 6000),
    elements: out,
  };
})()
