/* Схематичная кисть (вид на ладонь). Каждый палец принимает степень сгиба 0..1
   (сейчас 0 или 1, позже — непрерывные значения) или null, если палец без сенсора.
   Анимация сгиба — плавная интерполяция через requestAnimationFrame. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const FINGERS = {
    //        x центра, y основания, длина, ширина, угол
    thumb:  { x: 80,  y: 206, len: 66,  w: 26, angle: -60 },
    index:  { x: 72,  y: 128, len: 92,  w: 25, angle: -6 },
    middle: { x: 102, y: 122, len: 104, w: 26, angle: 0 },
    ring:   { x: 132, y: 126, len: 96,  w: 25, angle: 5 },
    pinky:  { x: 159, y: 140, len: 74,  w: 22, angle: 12 },
  };
  const NAMES = { thumb: "большой", index: "указательный", middle: "средний", ring: "безымянный", pinky: "мизинец" };

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  class HandView {
    constructor(svg) {
      this.svg = svg;
      this.cur = {};
      this.target = {};
      this.parts = {};
      svg.innerHTML = "";
      el("rect", { x: 40, y: 118, width: 140, height: 120, rx: 34, class: "palm" }, svg);
      const addFinger = (f) => {
        const g = FINGERS[f];
        const grp = el("g", { transform: `rotate(${g.angle} ${g.x} ${g.y})` }, svg);
        const body = el("rect", { class: "finger", rx: g.w / 2 }, grp);
        const fold = el("line", { class: "fold" }, grp);
        const label = el("title", {}, body);
        label.textContent = NAMES[f];
        this.parts[f] = { body, fold };
        this.cur[f] = 0;
        this.target[f] = null;
      };
      ["index", "middle", "ring", "pinky"].forEach(addFinger);
      el("rect", { x: 40, y: 150, width: 140, height: 88, rx: 34, class: "palm-front" }, svg);
      addFinger("thumb"); // большой палец рисуем поверх ладони
      this._raf = null;
      this._draw();
    }

    /** bends: { thumb: 0..1 | null, ... } */
    set(bends) {
      for (const f of Object.keys(FINGERS)) this.target[f] = f in bends ? bends[f] : null;
      if (!this._raf) this._raf = requestAnimationFrame(() => this._step());
    }

    _step() {
      let moving = false;
      for (const f of Object.keys(FINGERS)) {
        const t = this.target[f] == null ? 0 : this.target[f];
        const d = t - this.cur[f];
        if (Math.abs(d) > 0.01) { this.cur[f] += d * 0.28; moving = true; } else this.cur[f] = t;
      }
      this._draw();
      this._raf = moving ? requestAnimationFrame(() => this._step()) : null;
    }

    _draw() {
      for (const f of Object.keys(FINGERS)) {
        const g = FINGERS[f], p = this.parts[f], b = this.cur[f];
        const len = g.len * (1 - 0.62 * b);
        const x = g.x - g.w / 2, y = g.y - len;
        p.body.setAttribute("x", x);
        p.body.setAttribute("y", y);
        p.body.setAttribute("width", g.w);
        p.body.setAttribute("height", len + 14);
        const mapped = this.target[f] != null;
        p.body.setAttribute("class", "finger" + (mapped ? "" : " unmapped") + (mapped && b > 0.5 ? " bent" : ""));
        p.body.style.setProperty("--bend", b.toFixed(3));
        // линия сгиба (фаланга) видна, когда палец согнут
        p.fold.setAttribute("x1", x + 4); p.fold.setAttribute("x2", x + g.w - 4);
        p.fold.setAttribute("y1", y + 10); p.fold.setAttribute("y2", y + 10);
        p.fold.style.opacity = mapped ? b : 0;
      }
    }
  }

  window.HandView = HandView;
  window.FINGER_NAMES = NAMES;
})();
