/* Лёгкий график каналов на canvas без внешних библиотек.
   Каждый канал — своя полоса с автомасштабом: сырые значения (светлая линия)
   и отфильтрованные (яркая линия), справа — текущее значение. */
(function () {
  const COLORS = ["#3b6fd8", "#e07a1f", "#2a9d5b", "#b4489a", "#c43d3d", "#6a5acd", "#1f8a9e", "#8a6d1f"];

  class LaneChart {
    constructor(canvas) {
      this.c = canvas;
      this.ctx = canvas.getContext("2d");
      this.buf = [];          // {t, raw:[], filt:[]}
      this.span = 10;         // секунд на экране
      this.showRaw = true;
      this.paused = false;
      this.labels = [];
      new ResizeObserver(() => this._resize()).observe(canvas);
      this._resize();
      const loop = () => { if (!this.paused) this.draw(); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    _resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = this.c.clientWidth, h = this.c.clientHeight;
      this.c.width = Math.max(1, w * dpr); this.c.height = Math.max(1, h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    push(t, raw, filt) {
      if (this.paused) return;
      this.buf.push({ t, raw, filt });
      const keep = 31;
      while (this.buf.length && t - this.buf[0].t > keep) this.buf.shift();
    }

    clear() { this.buf = []; }

    draw() {
      const ctx = this.ctx, W = this.c.clientWidth, H = this.c.clientHeight;
      ctx.clearRect(0, 0, W, H);
      const last = this.buf.length ? this.buf[this.buf.length - 1] : null;
      const n = last ? Math.max(last.raw.length, last.filt.length) : 0;
      if (!n) {
        ctx.fillStyle = "#6b7489"; ctx.font = "14px sans-serif";
        ctx.fillText("Нет данных: подключите перчатку, включите демо или откройте запись CSV", 16, 28);
        return;
      }
      const tEnd = last.t, tStart = tEnd - this.span;
      const pts = this.buf.filter((p) => p.t >= tStart);
      const laneH = H / n, padL = 8, padR = 150;
      const plotW = W - padL - padR;
      const X = (t) => padL + ((t - tStart) / this.span) * plotW;

      for (let i = 0; i < n; i++) {
        const top = i * laneH, color = COLORS[i % COLORS.length];
        let lo = Infinity, hi = -Infinity;
        for (const p of pts) {
          for (const v of [p.filt[i], this.showRaw ? p.raw[i] : null]) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
        }
        if (!isFinite(lo)) { lo = 0; hi = 1; }
        const minSpan = 20;
        if (hi - lo < minSpan) { const m = (hi + lo) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; }
        const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
        const Y = (v) => top + 6 + (1 - (v - lo) / (hi - lo)) * (laneH - 12);

        ctx.fillStyle = i % 2 ? "#fafbfd" : "#ffffff";
        ctx.fillRect(0, top, W, laneH);
        ctx.strokeStyle = "#e2e6ef"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, top + laneH - 0.5); ctx.lineTo(W, top + laneH - 0.5); ctx.stroke();

        const line = (key, alpha, width) => {
          ctx.strokeStyle = color; ctx.globalAlpha = alpha; ctx.lineWidth = width;
          ctx.beginPath(); let started = false;
          for (const p of pts) {
            const v = p[key][i]; if (v == null) { started = false; continue; }
            const x = X(p.t), y = Y(v);
            if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
          }
          ctx.stroke(); ctx.globalAlpha = 1;
        };
        if (this.showRaw) line("raw", 0.28, 1);
        line("filt", 1, 2);

        // подписи
        const lbl = this.labels[i] || `S${i + 1}`;
        ctx.fillStyle = color; ctx.font = "600 13px sans-serif";
        ctx.fillText(lbl, W - padR + 12, top + 18);
        ctx.fillStyle = "#1d2433"; ctx.font = "600 18px ui-monospace, Menlo, monospace";
        const cur = last.filt[i] ?? last.raw[i];
        ctx.fillText(cur == null ? "—" : Math.round(cur).toString(), W - padR + 12, top + 40);
        ctx.fillStyle = "#6b7489"; ctx.font = "11px sans-serif";
        ctx.fillText(`${Math.round(lo + pad)}…${Math.round(hi - pad)}`, W - padR + 12, top + 56);
      }
      ctx.fillStyle = "#6b7489"; ctx.font = "11px sans-serif";
      ctx.fillText(`−${this.span} с`, padL + 2, H - 4);
      ctx.fillText("сейчас", padL + plotW - 36, H - 4);
    }
  }

  window.LaneChart = LaneChart;
  window.CHANNEL_COLORS = COLORS;
})();
