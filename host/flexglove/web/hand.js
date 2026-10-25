/* Минималистичный силуэт кисти (тыльная сторона правой руки, вид сверху).
   Каждый палец принимает степень сгиба 0..1 или null (палец без сенсора — светлый).
   Согнутый палец укорачивается до «костяшки», согнутый большой палец уходит на ладонь.
   new HandView(svg)                 — живая рука с плавной анимацией
   new HandView(svg, {static:true})  — статичная мини-рука (таблица комбинаций)            */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const ORDER = ["thumb", "index", "middle", "ring", "pinky"];
  const NAMES = { thumb: "большой", index: "указательный", middle: "средний", ring: "безымянный", pinky: "мизинец" };
  // основание пальца (x, y), длина, ширина, угол наклона
  const G = {
    index:  { x: 66,  y: 112, len: 82, w: 21, a: -8 },
    middle: { x: 92,  y: 108, len: 92, w: 22, a: -2 },
    ring:   { x: 117, y: 111, len: 85, w: 21, a: 5 },
    pinky:  { x: 140, y: 120, len: 64, w: 18, a: 12 },
    thumb:  { x: 58,  y: 172, len: 66, w: 23, a: -48 },
  };
  const PALM = "M50,124 Q50,104 68,104 L144,108 Q158,110 158,126 L156,178 Q154,204 134,214 L133,250 L73,250 L71,214 Q49,203 47,178 Z";

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  class HandView {
    constructor(svg, opts = {}) {
      this.svg = svg;
      this.static = !!opts.static;
      this.onFinger = opts.onFinger || null;
      svg.setAttribute("viewBox", "6 2 164 250");
      svg.innerHTML = "";
      svg.classList.add("hand-svg");
      this.parts = {}; this.cur = {}; this.target = {};
      const add = (f) => {
        const r = el("rect", { class: "h-finger", rx: G[f].w / 2, "data-finger": f }, svg);
        el("title", {}, r).textContent = NAMES[f];
        if (this.onFinger) r.addEventListener("click", () => this.onFinger(f));
        this.parts[f] = r; this.cur[f] = 0; this.target[f] = null;
      };
      ["index", "middle", "ring", "pinky"].forEach(add);
      el("path", { d: PALM, class: "h-palm" }, svg);
      add("thumb");
      this._raf = null;
      this._draw();
    }

    /** bends: { finger: 0..1 | null } */
    set(bends) {
      for (const f of ORDER) this.target[f] = f in bends ? bends[f] : null;
      if (this.static) { for (const f of ORDER) this.cur[f] = this.target[f] ?? 0; this._draw(); return; }
      if (!this._raf) this._raf = requestAnimationFrame(() => this._step());
    }

    _step() {
      let moving = false;
      for (const f of ORDER) {
        const t = this.target[f] ?? 0, d = t - this.cur[f];
        if (Math.abs(d) > 0.005) { this.cur[f] += d * 0.3; moving = true; } else this.cur[f] = t;
      }
      this._draw();
      this._raf = moving ? requestAnimationFrame(() => this._step()) : null;
    }

    _draw() {
      for (const f of ORDER) {
        const g = G[f], b = this.cur[f], r = this.parts[f];
        let len, ang = g.a;
        if (f === "thumb") { len = g.len * (1 - 0.35 * b); ang = g.a + 64 * b; }
        else len = g.len * (1 - 0.74 * b);
        r.setAttribute("x", -g.w / 2);
        r.setAttribute("y", -len);
        r.setAttribute("width", g.w);
        r.setAttribute("height", len + 16);
        r.setAttribute("transform", `translate(${g.x} ${g.y}) rotate(${ang.toFixed(2)})`);
        r.classList.toggle("off", this.target[f] == null);
      }
    }
  }

  window.HandView = HandView;
  window.FINGER_NAMES = NAMES;
  window.FINGER_ORDER = ORDER;
})();
