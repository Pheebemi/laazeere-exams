// Keeps chemical equations and maths formulas readable when teachers paste
// them from Word into the question form, and adds a small symbol toolbar.
//
// The question fields are plain text, so formatting has to become real
// characters: Word subscripts/superscripts (H<sub>2</sub>O, x<sup>2</sup>)
// turn into Unicode ones (H₂O, x²), and Word Equation Editor formulas —
// which Word puts on the clipboard as OMML inside
// <!--[if gte msEquation 12]> … <![endif]--> — are written out as text
// (fractions as (a)/(b), roots as √(…), arrows and symbols kept).
(function () {
  if (window.__wordPasteReady) return;
  window.__wordPasteReady = true;

  const SUB = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
    a: "ₐ", e: "ₑ", h: "ₕ", i: "ᵢ", j: "ⱼ", k: "ₖ", l: "ₗ", m: "ₘ", n: "ₙ", o: "ₒ", p: "ₚ",
    r: "ᵣ", s: "ₛ", t: "ₜ", u: "ᵤ", v: "ᵥ", x: "ₓ",
  };
  const SUP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    a: "ᵃ", b: "ᵇ", c: "ᶜ", d: "ᵈ", e: "ᵉ", f: "ᶠ", g: "ᵍ", h: "ʰ", i: "ⁱ", j: "ʲ", k: "ᵏ", l: "ˡ", m: "ᵐ",
    n: "ⁿ", o: "ᵒ", p: "ᵖ", r: "ʳ", s: "ˢ", t: "ᵗ", u: "ᵘ", v: "ᵛ", w: "ʷ", x: "ˣ", y: "ʸ", z: "ᶻ",
  };

  // Map every character, or fall back to _(…) / ^(…) when one has no Unicode form.
  const shift = (text, map, marker) => {
    const clean = text.replace(/\s+/g, "");
    if (!clean) return "";
    const mapped = Array.from(clean).map((ch) => map[ch] || map[ch.toLowerCase()]);
    if (mapped.every(Boolean)) return mapped.join("");
    return clean.length === 1 ? marker + clean : `${marker}(${clean})`;
  };
  const toSub = (text) => shift(text, SUB, "_");
  const toSup = (text) => shift(text, SUP, "^");
  const wrap = (text) => (text.length > 1 && !/^\(.*\)$/.test(text) ? `(${text})` : text);

  // --- Word Equation Editor (OMML) -> text ---------------------------------
  const local = (el) => el.localName.toLowerCase().replace(/^m:/, "");
  const childrenNamed = (el, name) => Array.from(el.children).filter((c) => local(c) === name);
  const child = (el, name) => childrenNamed(el, name)[0];
  const prop = (el, prName, valName) => {
    const pr = child(el, prName);
    const node = pr && child(pr, valName);
    return node ? node.getAttribute("m:val") || node.getAttribute("val") : null;
  };

  // Word's HTML puts equation text straight inside <m:r> (often wrapped in
  // <span>/<i>), not only in <m:t>, so text nodes count too.
  const omml = (el) => {
    if (!el) return "";
    if (el.nodeType === Node.TEXT_NODE) return el.textContent.replace(/\s+/g, " ");
    if (el.nodeType !== Node.ELEMENT_NODE) return "";
    const name = local(el);
    const inner = (n) => omml(child(el, n)).trim();
    const all = () => Array.from(el.childNodes).map(omml).join("");
    if (name.endsWith("pr")) return "";  // property blocks (rPr, ctrlPr, …) hold no text
    switch (name) {
      case "t": return el.textContent;
      case "ssub": return inner("e") + toSub(inner("sub"));
      case "ssup": return inner("e") + toSup(inner("sup"));
      case "ssubsup": return inner("e") + toSub(inner("sub")) + toSup(inner("sup"));
      case "spre": return toSub(inner("sub")) + toSup(inner("sup")) + inner("e");
      case "f": return `${wrap(inner("num"))}/${wrap(inner("den"))}`;
      case "rad": {
        const degree = inner("deg");
        return (degree ? toSup(degree) : "") + "√" + wrap(inner("e"));
      }
      case "d": {
        const open = prop(el, "dpr", "begchr") ?? "(";
        const close = prop(el, "dpr", "endchr") ?? ")";
        const sep = prop(el, "dpr", "sepchr") ?? ",";
        return open + childrenNamed(el, "e").map(omml).join(sep) + close;
      }
      case "nary": {
        const symbol = prop(el, "narypr", "chr") ?? "∫";
        return symbol + toSub(inner("sub")) + toSup(inner("sup")) + " " + inner("e");
      }
      case "groupchr": {
        // e.g. an arrow with reaction conditions written over it
        const symbol = prop(el, "groupchrpr", "chr") ?? "⏟";
        const label = inner("e");
        return label ? ` ${symbol}[${label}] ` : ` ${symbol} `;
      }
      case "limlow": return inner("e") + toSub(inner("lim"));
      case "limupp": return inner("e") + toSup(inner("lim"));
      case "func": return inner("fname") + " " + inner("e");
      case "m": return childrenNamed(el, "mr").map((row) => childrenNamed(row, "e").map(omml).join(" ")).join("; ");
      default: return all();
    }
  };

  // Replace each Word equation with its text, and drop Word's picture fallback for it.
  const expandWordEquations = (html) =>
    html
      .replace(/<!--\[if gte msEquation 12\]>([\s\S]*?)<!\[endif\]-->/gi, (_, xml) => {
        // The HTML parser ignores "/>" on unknown tags, so close <m:x … /> explicitly.
        const closed = xml.replace(/<(m:[\w]+)([^<>]*?)\/>/gi, "<$1$2></$1>");
        const doc = new DOMParser().parseFromString(`<div>${closed}</div>`, "text/html");
        const roots = Array.from(doc.body.querySelectorAll("*")).filter((el) => /^m:omath$/i.test(el.localName));
        const text = roots.filter((el) => !roots.some((other) => other !== el && other.contains(el))).map(omml).join(" ");
        return `<span>${text.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</span>`;
      })
      .replace(/<!\[if !msEquation\]>[\s\S]*?<!\[endif\]>/gi, "");

  // --- HTML -> text, with <sub>/<sup> kept as characters ----------------------
  const BLOCKS = new Set(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]);
  const htmlToText = (html, multiline) => {
    const doc = new DOMParser().parseFromString(expandWordEquations(html), "text/html");
    doc.querySelectorAll("style, script, title, meta, xml").forEach((el) => el.remove());
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent.replace(/\s+/g, " ");
      if (node.nodeType !== Node.ELEMENT_NODE) return "";
      const tag = node.localName;
      const valign = (node.style && node.style.verticalAlign) || "";
      const inner = Array.from(node.childNodes).map(walk).join("");
      if (tag === "br") return "\n";
      if (tag === "sub" || valign === "sub") return toSub(inner);
      if (tag === "sup" || valign === "super") return toSup(inner);
      return BLOCKS.has(tag) ? `${inner}\n` : inner;
    };
    const text = walk(doc.body).replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    return multiline ? text : text.replace(/\s*\n\s*/g, " ");
  };

  const insertText = (field, text) => {
    const start = field.selectionStart ?? field.value.length;
    const end = field.selectionEnd ?? field.value.length;
    field.setRangeText(text, start, end, "end");
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
  };

  const FIELDS = "textarea[name=text], textarea[name=accepted_answers], input[name^=choice_]";

  document.addEventListener("paste", (event) => {
    const field = event.target.closest && event.target.closest(FIELDS);
    if (!field || !event.clipboardData) return;
    const html = event.clipboardData.getData("text/html");
    if (!html || !/<su[bp][\s>]|msEquation|vertical-align|urn:schemas-microsoft-com/i.test(html)) return;
    const text = htmlToText(html, field.localName === "textarea");
    if (!text) return;  // nothing usable (e.g. only a picture) — let the browser paste plain text
    event.preventDefault();
    insertText(field, text);
  });

  // --- Toolbar: x², x₂ and common symbols, acting on the last field used ----
  const SYMBOLS = ["→", "⇌", "↑", "↓", "°", "×", "÷", "±", "√", "π", "Δ", "θ", "µ", "≤", "≥", "≠", "≈", "∞"];
  document.querySelectorAll("[data-symbol-toolbar]").forEach((bar) => {
    const form = bar.closest("form");
    let lastField = form.querySelector("textarea[name=text]");
    form.addEventListener("focusin", (event) => {
      if (event.target.matches(FIELDS)) lastField = event.target;
    });
    const button = (label, title, onClick) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = title;
      b.className = "h-8 min-w-8 rounded-md border bg-card px-2 text-sm font-medium hover:bg-muted";
      b.addEventListener("mousedown", (e) => e.preventDefault());  // keep the cursor in the field
      b.addEventListener("click", onClick);
      bar.appendChild(b);
    };
    const convertSelection = (fn) => () => {
      const field = lastField;
      if (!field) return;
      const { selectionStart: s, selectionEnd: e } = field;
      if (s === e) return;
      field.setRangeText(fn(field.value.slice(s, e)), s, e, "end");
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.focus();
    };
    button("x²", "Superscript: select text first, e.g. the 2 in x2", convertSelection(toSup));
    button("x₂", "Subscript: select text first, e.g. the 2 in H2O", convertSelection(toSub));
    SYMBOLS.forEach((symbol) => button(symbol, `Insert ${symbol}`, () => lastField && insertText(lastField, symbol)));
  });

  window.wordPaste = { htmlToText, toSub, toSup };  // for tests
})();
