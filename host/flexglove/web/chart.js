/* Лёгкий график каналов на canvas без внешних библиотек (этап 4б: ровные графики).
   Каждый канал — своя полоса. Два режима:
   - «bend» (по умолчанию): сгиб 0–100 % с ОДНОЙ фиксированной шкалой для всех полос. Без автомасштаба: прежний
     автомасштаб растягивал шум покоя (± 3 ед.) на всю высоту полосы — линии выглядели «кривыми»;
   - «raw»: значения АЦП (после фильтра — яркая линия, до фильтра — светлая). Шкала — от уровня «ладонь» до «кулак»
     с запасом 15 %; без калибровки — фактический диапазон окна, но не уже MIN_RAW_SPAN единиц (шум остаётся мелким).
   Подписи: деления шкалы с единицами слева, время снизу, справа — палец и текущее значение. */
(function () {
  const COLORS = ["#3b6fd8", "#e07a1f", "#2a9d5b", "#b4489a", "#c43d3d", "#6a5acd", "#1f8a9e", "#8a6d1f"];
  const MIN_RAW_SPAN = 200;     // ед. АЦП
  const CAL_MARGIN = 0.15;

  class LaneChart {
    constructor(canvas) {
      this.c = canvas;
      this.ctx = canvas.getContext("2d");
      this.buf = [];          // {t, raw:[], filt:[], bend:[]}
      this.span = 10;         // секунд на экране
      this.mode = "bend";     // bend | raw
      this.showRaw = true;
      this.paused = false;
      this.labels = [];
      this.cal = [];          // [{open, fist}] по каналам — для шкалы «сырые АЦП»
      new ResizeObserver(() => this._resize()).observe(canvas);
      this._resize();
      const loop = () => { if (!this.paused && this.c.offsetParent) this.draw(); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    _resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = this.c.clientWidth, h = this.c.clientHeight;
      this.c.width = Math.max(1, w * dpr); this.c.height = Math.max(1, h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    push(t, raw, filt, bend) {
      if (this.paused) return;
      this.buf.push({ t, raw, filt, bend });
      const keep = 31;
      while (this.buf.length && t - this.buf[0].t > keep) this.buf.shift();
    }

    clear() { this.buf = []; }

    /** Диапазон шкалы полосы i в режиме «сырые АЦП». */
    rawRange(i, pts) {
      const c = this.cal[i] || {};
      if (c.open != null && c.fist != null && Math.abs(c.fist - c.open) >= 1) {
        const lo = Math.min(c.open, c.fist), hi = Math.max(c.open, c.fist), m = (hi - lo) * CAL_MARGIN;
        return [lo - m, hi + m];
      }
      let lo = Infinity, hi = -Infinity;
      for (const p of pts) for (const v of [p.filt[i], this.showRaw ? p.raw[i] : null]) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      if (!isFinite(lo)) return [0, MIN_RAW_SPAN];
      if (hi - lo < MIN_RAW_SPAN) { const mid = (hi + lo) / 2; lo = mid - MIN_RAW_SPAN / 2; hi = mid + MIN_RAW_SPAN / 2; }
      const pad = (hi - lo) * 0.08;
      return [lo - pad, hi + pad];
    }

    draw() {
      const ctx = this.ctx, W = this.c.clientWidth, H = this.c.clientHeight;
      ctx.clearRect(0, 0, W, H);
      const last = this.buf.length ? this.buf[this.buf.length - 1] : null;
      const n = last ? Math.max(last.raw.length, last.filt.length, (last.bend || []).length) : 0;
      if (!n) {
        ctx.fillStyle = "#6b7489"; ctx.font = "14px sans-serif";
        ctx.fillText("Нет данных: подключите перчатку, включите демо или откройте запись CSV", 16, 28);
        return;
      }
      const tEnd = last.t, tStart = tEnd - this.span;
      const pts = this.buf.filter((p) => p.t >= tStart);
      const padL = 58, padR = 150, padB = 20;
      const laneH = (H - padB) / n;
      const plotW = W - padL - padR;
      const X = (t) => padL + ((t - tStart) / this.span) * plotW;
      const bendMode = this.mode === "bend";

      for (let i = 0; i < n; i++) {
        const top = i * laneH, color = COLORS[i % COLORS.length];
        const [lo, hi] = bendMode ? [-0.05, 1.05] : this.rawRange(i, pts);
        const Y = (v) => top + 8 + (1 - (v - lo) / (hi - lo)) * (laneH - 16);

        ctx.fillStyle = i % 2 ? "#fafbfd" : "#ffffff";
        ctx.fillRect(0, top, W, laneH);
        // деления шкалы с подписями
        const ticks = bendMode ? [0, 0.5, 1] : niceTicks(lo, hi, 3);
        ctx.font = "11px sans-serif"; ctx.textAlign = "right";
        for (const v of ticks) {
          const y = Math.round(Y(v)) + 0.5;
          ctx.strokeStyle = v === 0 && bendMode ? "#d5dbe7" : "#eceff5"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
          ctx.fillStyle = "#8a93a6";
          ctx.fillText(bendMode ? `${Math.round(v * 100)} %` : Math.round(v).toString(), padL - 6, y + 4);
        }
        ctx.textAlign = "left";
        ctx.strokeStyle = "#e2e6ef";
        ctx.beginPath(); ctx.moveTo(0, top + laneH - 0.5); ctx.lineTo(W, top + laneH - 0.5); ctx.stroke();

        const line = (key, alpha, width) => {
          ctx.strokeStyle = color; ctx.globalAlpha = alpha; ctx.lineWidth = width;
          ctx.beginPath(); let started = false;
          for (const p of pts) {
            const arr = p[key]; const v = arr ? arr[i] : null;
            if (v == null) { started = false; continue; }
            const x = X(p.t), y = Y(Math.min(hi, Math.max(lo, v)));
            if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
          }
          ctx.stroke(); ctx.globalAlpha = 1;
        };
        if (bendMode) line("bend", 1, 2.2);
        else { if (this.showRaw) line("raw", 0.28, 1); line("filt", 1, 2); }

        // подписи справа: палец, текущее значение, единицы
        const lbl = this.labels[i] || `S${i + 1}`;
        ctx.fillStyle = color; ctx.font = "600 13px sans-serif";
        ctx.fillText(lbl, W - padR + 12, top + 18);
        ctx.fillStyle = "#1d2433"; ctx.font = "600 18px ui-monospace, Menlo, monospace";
        const cur = bendMode ? (last.bend || [])[i] : (last.filt[i] ?? last.raw[i]);
        ctx.fillText(cur == null ? "—" : bendMode ? `${Math.round(cur * 100)} %` : Math.round(cur).toString(), W - padR + 12, top + 40);
        ctx.fillStyle = "#6b7489"; ctx.font = "11px sans-serif";
        ctx.fillText(bendMode ? "сгиб" : (this.cal[i] && this.cal[i].open != null && this.cal[i].fist != null ? "ед. АЦП · шкала по калибровке" : "ед. АЦП"),
          W - padR + 12, top + 56);
      }
      // ось времени
      ctx.fillStyle = "#6b7489"; ctx.font = "11px sans-serif";
      const step = this.span <= 5 ? 1 : this.span <= 10 ? 2 : 5;
      for (let s = 0; s <= this.span; s += step) {
        const x = X(tEnd - s);
        ctx.textAlign = s === 0 ? "right" : "center";
        ctx.fillText(s === 0 ? "сейчас" : `−${s} с`, x, H - 5);
      }
      ctx.textAlign = "left";
    }
  }

  function niceTicks(lo, hi, count) {
    const raw = (hi - lo) / count;
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map((k) => k * p).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
    return out;
  }

  window.LaneChart = LaneChart;
  window.CHANNEL_COLORS = COLORS;
})();
