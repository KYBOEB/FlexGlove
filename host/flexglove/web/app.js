/* FlexGlove — интерфейс. Состояние приходит по WebSocket (/ws) ~30 раз в секунду, команды уходят через REST (/api).
   Этап 4б: главный экран «Рука» — большая 3D-рука (hand3d.js) и узкая панель справа: пальцы процентами и словами,
   жест словами, связь, IMU и калибровка. «Рабочая зона» и карточка контроллера убраны из интерфейса (D-26), ядро
   по-прежнему ведёт контроллер и команды. Двоичная строка комбинации осталась только в «Отладке потока». */
(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const MODE_TEXT = { calibrated: "калибровка", auto: "автокалибровка", uncalibrated: "нет калибровки", bits: "биты 0/1" };

  let META = { actions: [], fingers: [], version: "" };
  let S = null;
  let lastEventTs = 0;
  let bindKey = "", mapKey = "", tableKey = "", rowsKey = "";

  const handSettings = new HandView($("#handSettings"));
  const handMain = new HandView($("#handMain"), { onFinger: (f) => demoToggleFinger(f) });   // 2D, если нет WebGL
  const gestureMini = new HandView($("#gestureMini"), { static: true });
  const wzHand = new HandView($("#wzHand"), { static: true });
  const chart = new LaneChart($("#chart"));
  let hand3d = null;                                   // 3D-рука (hand3d.js, ES-модуль); null — 2D-силуэт
  function init3d() {
    if (hand3d || !window.FlexHand3D) return;
    const box = $("#hand3d");
    box.hidden = false;
    hand3d = window.FlexHand3D.create(box);
    if (hand3d) $("#hand2d").hidden = true;
    else box.hidden = true;                            // нет WebGL — остаётся 2D-силуэт
  }
  window.addEventListener("hand3d-ready", init3d);

  // ---------------- утилиты ----------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
    return data;
  }
  let toastTimer;
  function toast(msg, err = false) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3500);
  }
  const act = (id) => META.actions.find((a) => a.id === id) || { title: id, on_label: "вкл", off_label: "выкл" };
  const fname = (f) => window.FINGER_NAMES[f] || f;
  const pct = (x) => `${Math.round((x || 0) * 100)} %`;
  const bendWord = (b) => (b < 0.25 ? "разогнут" : b < 0.65 ? "полусогнут" : "согнут");
  const combosFor = (n) => Array.from({ length: 2 ** n }, (_, i) => i.toString(2).padStart(n, "0").split("").reverse().join(""))
    .sort((a, b) => (a.split("1").length - b.split("1").length) || (a < b ? 1 : -1));
  function bendsByFinger(values, map) {
    const out = {};
    values.forEach((v, i) => { if (map[i]) out[map[i]] = v; });
    return out;
  }
  /** Комбинация словами: «указательный + средний», «ладонь раскрыта», «кулак». */
  function comboWords(combo, map) {
    if (!combo) return "—";
    const bits = [...combo];
    if (!bits.includes("1")) return "ладонь раскрыта";
    if (bits.length >= 5 && !bits.includes("0")) return "кулак";        // кулак — только если сенсоры на всех пальцах
    return bits.map((c, i) => (c === "1" ? fname(map[i]) : null)).filter(Boolean).join(" + ");
  }

  // ---------------- вкладки ----------------
  function showTab() {
    const tab = ["#signals", "#settings"].includes(location.hash) ? location.hash.slice(1) : "hand";
    $$(".page").forEach((p) => (p.hidden = p.id !== "tab-" + tab));
    $$(".tab").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
    document.body.classList.toggle("on-hand", tab === "hand");
    if (tab === "signals") refreshRecordings();
  }
  window.addEventListener("hashchange", showTab);

  // ---------------- WebSocket ----------------
  function connectWS() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (e) => { S = JSON.parse(e.data); render(); };
    ws.onclose = () => {
      $("#connDot").className = "dot"; $("#connText").textContent = "Нет связи с приложением — переподключаюсь…";
      setTimeout(connectWS, 1000);
    };
  }

  // ---------------- отрисовка ----------------
  function render() {
    const src = S.source || {}, connected = !!src.connected, n = S.n_channels, map = S.finger_map;
    const chans = S.channels || [];

    $("#connDot").className = "dot" + (connected ? " ok" : src.kind ? " wait" : "");
    $("#connText").textContent = connected ? `${src.name || "Устройство"} подключено` : (src.message || "Не подключено");
    $("#connRate").textContent = connected ? `${S.stats.rate} сообщ./с` : "";
    $("#noDeviceBanner").hidden = connected;
    const warns = S.warnings || [];
    const wbox = $("#streamWarn");
    const whtml = warns.map((w) => `<div>⚠ ${w}</div>`).join("");
    if (wbox.innerHTML !== whtml) wbox.innerHTML = whtml;
    wbox.hidden = !warns.length;

    // рука
    const bends = bendsByFinger(chans.map((c) => (c.stale ? 0 : c.bend)), map);
    handMain.set(bends); handSettings.set(bends);
    const ori = S.orientation;
    if (hand3d) hand3d.setTarget({ q: ori && !ori.stale ? ori.q_world : null, bends, sensed: new Set(map.slice(0, n)), hand: S.hand });
    renderSide(src, connected, chans, map, ori);
    renderLog();

    const isMock = src.kind === "mock" && connected;
    $("#demoStrip").hidden = !isMock;
    document.body.classList.toggle("demo", isMock);

    // сигналы
    if (S.mode !== "none") {
      chart.labels = chans.map((_, i) => `S${i + 1} · ${fname(map[i] || "")}`);
      chart.cal = chans.map((c) => c.cal);
      chart.push(performance.now() / 1000, chans.map((x) => x.raw), chans.map((x) => (x.filtered ?? x.bit)),
        chans.map((x) => (x.stale ? null : x.bend)));
    }
    renderChannelTable(chans, map);
    renderSignalsToolbar();
    renderProtoChannels();
    renderPipeline();
    renderWizard();

    // настройки
    $("#srcDot").className = "dot" + (connected ? " ok" : src.kind ? " wait" : "");
    $("#srcName").textContent = connected ? (src.name || "Подключено") : "Не подключено";
    $("#srcMsg").textContent = src.message || "";
    $("#disconnectBtn").disabled = !src.kind;
    $("#dbgRate").textContent = S.stats.rate;
    $("#dbgTotal").textContent = `${S.stats.total} / ${S.stats.unparsed}`;
    $("#dbgFormat").textContent = `${S.mode} · ${S.stats.format || "—"}`;
    $("#dbgRaw").textContent = S.stats.last_raw || "—";
    $("#dbgN").textContent = n;
    $("#dbgCombo").textContent = S.combo || "—";
    $("#dbgWarn").textContent = warns.length ? warns.join(" · ") : "нет";
    const imu = S.imu;
    $("#dbgImu").textContent = !imu ? "нет данных (протокол v0.2)"
      : `a: ${imu.ax}; ${imu.ay}; ${imu.az} мг · g: ${(imu.gx / 10).toFixed(1)}; ${(imu.gy / 10).toFixed(1)}; ${(imu.gz / 10).toFixed(1)} °/с` +
        (imu.stale ? " · устарело" : "");
    renderFingerMap(Math.max(n, 5), map, S.roles || []);
    renderBindings(n, map);
    const hs = $("#handSide"); if (document.activeElement !== hs) hs.value = S.hand || "right";
    const mt = $("#imuMount"); if (document.activeElement !== mt && ori) mt.value = ori.mount;
    const deb = $("#debounce");
    if (document.activeElement !== deb) deb.value = S.debounce_ms;
  }

  /** Статус калибровки для главного экрана и «Сигналов». */
  function calibrationStatus(chans) {
    const live = chans.filter((c) => !c.stale && c.mode !== "bits");
    if (!live.length) return { cls: "", text: S.mode === "bits" ? "Перчатка присылает готовые 0/1 — калибровка не нужна" : "" };
    const cal = live.filter((c) => c.mode === "calibrated").length;
    if (cal === live.length) return { cls: "ok", text: `✓ Калибровка выполнена (${cal} из ${live.length})` };
    if (cal) return { cls: "warn", text: `Калибровка частичная: ${cal} из ${live.length} — нажмите «Калибровка»` };
    return { cls: "warn", text: "⚠ Нужна калибровка — сгиб сейчас приблизительный. Нажмите «Калибровка»" };
  }

  function renderSide(src, connected, chans, map, ori) {
    $("#sideDot").className = "dot" + (connected ? " ok" : src.kind ? " wait" : "");
    $("#sideSrc").textContent = connected ? `${src.name || "Перчатка"} · ${S.stats.rate} Гц` : (src.message || "Не подключено");
    $("#sideImu").textContent = !connected ? "" : !ori ? "Без датчика ориентации — рука показана пальцами вверх"
      : ori.calibrating ? "⏳ Калибровка гироскопа: держите руку неподвижно 2 с"
      : ori.stale ? "Нет данных MPU6050" : `MPU6050: ${ori.bias_ok ? "гироскоп откалиброван" : "гироскоп калибруется в покое"} · ${ori.at_rest ? "покой" : "движение"}`;
    const cs = calibrationStatus(chans);
    const sc = $("#sideCal"); sc.textContent = cs.text; sc.className = "small cal-status " + cs.cls;

    // жест словами + мини-силуэт
    $("#gestureWords").textContent = S.n_channels ? comboWords(S.combo, map) : "—";
    gestureMini.set(S.combo ? bendsByFinger([...S.combo].map(Number), map) : {});
    const bound = S.committed && S.bindings[S.committed];
    $("#mainTarget").textContent = bound ? `→ ${act(bound).title}` : "";

    // пальцы: название, полоса, слово; мелко — сырое и «Δ от ладони»
    const key = chans.length + "|" + map.join();
    const box = $("#fingerRows");
    if (key !== rowsKey) {
      rowsKey = key;
      box.innerHTML = chans.map((_, i) => `<div class="finger-row">
        <div class="fr-top"><span class="fr-name"></span><b class="fr-pct"></b></div>
        <div class="bar"><div class="fill"></div></div>
        <div class="fr-bottom"><span class="fr-word"></span><span class="fr-raw"></span></div></div>`).join("");
    }
    chans.forEach((ch, i) => {
      const row = box.children[i];
      const b = ch.stale ? 0 : ch.bend;
      row.querySelector(".fr-name").textContent = `${fname(map[i] || "")}`;
      row.querySelector(".fr-pct").textContent = ch.stale ? "—" : pct(b);
      const fill = row.querySelector(".fill");
      fill.style.width = pct(b).replace(" ", "");
      fill.className = "fill " + (ch.stale ? "" : b >= 0.65 ? "bent" : b >= 0.25 ? "half" : "");
      row.querySelector(".fr-word").textContent = ch.stale ? "нет данных" : bendWord(b);
      const base = ch.cal.open ?? ch.rest;
      row.querySelector(".fr-raw").textContent = ch.raw == null ? (S.mode === "bits" ? "0/1" : "")
        : `сырое ${Math.round(ch.raw)}` + (base != null ? ` · Δ от ладони ${fmtDelta(ch.raw - base)}` : "");
    });
    const missing = 5 - Math.min(5, S.n_channels || 0);
    $("#fingerNote").textContent = !S.n_channels ? "Подключите перчатку или демо"
      : missing ? `Остальные пальцы без сенсора — на 3D-руке показаны светлыми, в спокойной позе` : "";

    // ориентация
    $("#imuBox").hidden = !ori;
    if (ori) {
      $("#imuPitch").textContent = `${deg(ori.pitch)} ${ori.pitch > 25 ? "вверх" : ori.pitch < -25 ? "вниз" : ""}`;
      $("#imuRoll").textContent = deg(ori.roll); $("#imuYaw").textContent = deg(ori.yaw);
      $("#imuStatus").textContent = ori.message && ori.message.includes("не принята") ? ori.message
        : `Поворот вокруг вертикали у MPU6050 медленно уходит (≈ ${String(ori.yaw_drift_deg_min).replace(".", ",")}°/мин) — направьте пальцы на экран и нажмите «Обнулить поворот».`;
    }
    $("#imuZero").disabled = $("#imuCal").disabled = !ori || !!ori.calibrating;
    $("#stagePose").textContent = !ori ? "" : `пальцы ${deg(ori.pitch)} · крен ${deg(ori.roll)} · поворот ${deg(ori.yaw)}`;
  }
  const fmtDelta = (d) => `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(Math.round(d))}`;
  const deg = (x) => `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(Math.round(x || 0))}°`;

  function renderLog() {
    const ev = S.events || [];
    const newest = ev.length ? ev[0].ts : 0;
    if (newest === lastEventTs && $("#eventLog").children.length) return;
    lastEventTs = newest;
    $("#eventLog").innerHTML = ev.length ? ev.map((e) => {
      const t = new Date(e.ts * 1000).toLocaleTimeString("ru-RU");
      const how = e.source === "gesture" ? ` · жест «${comboWords(e.combo, S.finger_map)}»` : e.source === "manual" ? " · вручную" : "";
      return `<li class="${e.source === "system" ? "sys" : ""}"><span class="time">${t}</span><span>${e.text}${how}</span></li>`;
    }).join("") : `<li class="muted">Пока ничего не произошло</li>`;
  }

  function renderChannelTable(chans, map) {
    const key = chans.length + "|" + map.join();
    if (key !== tableKey) {
      tableKey = key;
      $("#chBody").innerHTML = chans.map((_, i) => `<tr><td><span class="swatch" style="background:${CHANNEL_COLORS[i]}"></span>S${i + 1}</td>
        <td>${fname(map[i] || "")}</td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>`).join("");
    }
    const rows = $("#chBody").children;
    chans.forEach((ch, i) => {
      const td = rows[i].children;
      td[2].textContent = ch.raw ?? "—";
      td[3].textContent = ch.filtered ?? "—";
      const base = ch.cal.open ?? ch.rest;
      td[4].textContent = ch.raw != null && base != null ? fmtDelta(ch.raw - base) : "—";
      td[5].textContent = ch.stale ? "—" : pct(ch.bend);
      td[6].textContent = ch.stale ? "—" : bendWord(ch.bend);
      const mode = ch.stale ? "stale" : ch.mode;
      const html = `<span class="pill ${mode}">${ch.stale ? "нет данных" : MODE_TEXT[ch.mode] || ch.mode}</span>`;
      if (td[7].innerHTML !== html) td[7].innerHTML = html;
      td[8].textContent = ch.cal.open != null || ch.cal.fist != null
        ? `${ch.cal.open != null ? Math.round(ch.cal.open) : "—"} / ${ch.cal.fist != null ? Math.round(ch.cal.fist) : "—"}` : "—";
    });
    const cs = calibrationStatus(chans);
    $("#sigHint").textContent = S.mode === "values"
      ? (cs.cls === "warn" ? "Без калибровки сгиб считается по автокалибровке (от уровня при подключении) — это приблизительно. Нажмите «Калибровка…»: 3 с ладонь, 3 с кулак." : "")
      : S.mode === "bits" ? "Перчатка присылает готовые 0/1 — графики показывают только состояния." : "";
  }

  function renderSignalsToolbar() {
    const r = S.recording;
    const btn = $("#recBtn");
    btn.textContent = r.active ? "■ Остановить запись" : "● Начать запись";
    btn.classList.toggle("rec-on", r.active);
    $("#recStatus").textContent = r.active ? `${r.file} · строк: ${r.rows}` : (r.file ? `последняя: ${r.file}` : "");
    const calOk = S.mode === "values";
    $("#calWizardBtn").disabled = $("#openWizard").disabled = !calOk;
    const cs = calibrationStatus(S.channels || []);
    $("#calStatus").textContent = calOk ? cs.text : "нужен поток значений АЦП";
  }

  function renderFingerMap(n, map, roles) {
    const shown = S.n_channels || n;
    const key = shown + "|" + map.join() + "|" + JSON.stringify(roles);
    if (key === mapKey) return;
    mapKey = key;
    const opts = (sel) => META.fingers.map((f) => `<option value="${f}" ${f === sel ? "selected" : ""}>${fname(f)}</option>`).join("");
    const role = (i) => roles[i] || { grip: true, cmd: true };
    $("#fingerMap").innerHTML = Array.from({ length: shown }, (_, i) =>
      `<div class="fm-row"><span><span class="swatch" style="background:${CHANNEL_COLORS[i]}"></span>Канал S${i + 1}</span>
        <select data-ch="${i}">${opts(map[i])}</select>
        <label class="check"><input type="checkbox" data-role="grip" data-ch="${i}" ${role(i).grip ? "checked" : ""}> сжатие</label>
        <label class="check"><input type="checkbox" data-role="cmd" data-ch="${i}" ${role(i).cmd ? "checked" : ""}> команды</label></div>`).join("");
    const r = roles.slice(0, shown);
    const warn = !r.length ? "" : !r.some((x) => x.grip) ? "Нет ни одного пальца сжатия — захват не будет следовать за рукой."
      : !r.some((x) => x.cmd && !x.grip) ? "Нет пальцев «только команды» — пока управление активно, жесты не работают." : "";
    $("#rolesWarn").textContent = warn; $("#rolesWarn").hidden = !warn;
  }

  function renderBindings(n, map) {
    const key = JSON.stringify([n, map, S.bindings, META.actions.length]);
    if (key !== bindKey) {
      bindKey = key;
      const opts = (sel) => `<option value="">— нет команды —</option>` +
        META.actions.map((a) => `<option value="${a.id}" ${a.id === sel ? "selected" : ""}>${a.title}</option>`).join("");
      const grid = $("#bindGrid");
      grid.innerHTML = n ? combosFor(n).map((c) => {
        const rest = !c.includes("1");
        return `<div class="bind-card ${S.bindings[c] ? "bound" : ""}" data-combo="${c}"><svg></svg>
          <div class="bc-words">${comboWords(c, map)}</div>
          ${rest ? `<div class="bc-rest">покой — без команды</div>` : `<select data-combo="${c}">${opts(S.bindings[c])}</select>`}</div>`;
      }).join("") : `<p class="muted">Подключите перчатку или демо — появятся все сочетания для числа каналов.</p>`;
      $$("#bindGrid .bind-card").forEach((card) => {
        const hv = new HandView(card.querySelector("svg"), { static: true });
        hv.set(bendsByFinger([...card.dataset.combo].map(Number), map));
      });
    }
    $$("#bindGrid .bind-card").forEach((c) => c.classList.toggle("live", c.dataset.combo === S.committed));
  }

  // ---------------- мастер калибровки ----------------
  // шаги: 1 «ладонь» (отсчёт 3-2-1 → замер 3 с) → 2 «кулак» (так же) → 3 итог по каналам → «Сохранить» / «Повторить»
  const WZ = { open: false, phase: "intro", countdown: 0, timer: null };
  const OPEN_POSE = { thumb: 0, index: 0, middle: 0, ring: 0, pinky: 0 };
  const FIST_POSE = { thumb: 1, index: 1, middle: 1, ring: 1, pinky: 1 };
  function openWizard() {
    if (!S || S.mode !== "values") return toast("Калибровка возможна только при потоке значений АЦП — подключите перчатку", true);
    WZ.open = true; WZ.phase = "intro"; $("#wizard").hidden = false;
    api("/api/calibration/wizard-cancel", { method: "POST" }).catch(() => {});
    renderWizard();
  }
  function closeWizard() {
    clearInterval(WZ.timer); WZ.open = false; $("#wizard").hidden = true;
    api("/api/calibration/wizard-cancel", { method: "POST" }).catch(() => {});
  }
  function startStep(target) {
    clearInterval(WZ.timer);
    WZ.phase = "count-" + target; WZ.countdown = 3;
    if (S.source.kind === "mock") {                    // в демо «рука» сама принимает позу — мастер проверяется без перчатки
      api("/api/mock", { method: "POST", body: { bends: Array(S.n_channels).fill(target === "fist" ? 1 : 0) } }).catch(() => {});
      $("#mockAuto").checked = $("#mockTour").checked = false;
    }
    renderWizard();
    WZ.timer = setInterval(async () => {
      WZ.countdown -= 1;
      if (WZ.countdown <= 0) {
        clearInterval(WZ.timer);
        WZ.phase = "capture-" + target;
        try { await api(`/api/calibration/wizard/${target}`, { method: "POST" }); }
        catch (e) { toast(e.message, true); WZ.phase = "intro"; }
      }
      renderWizard();
    }, 1000);
  }
  function renderWizard() {
    if (!WZ.open || !S) return;
    const w = S.wizard || { step: "idle" }, map = S.finger_map;
    const fist = WZ.phase.endsWith("fist") || w.step === "open_done";
    if (WZ.phase.startsWith("capture") && !w.capturing) WZ.phase = w.step === "review" ? "review" : w.step === "open_done" ? "between" : WZ.phase;
    const review = w.step === "review";
    const step = review ? 3 : fist ? 2 : 1;
    $$(".wz-steps span").forEach((s) => s.classList.toggle("on", +s.dataset.step === step));
    $("#wzPose").hidden = review; $("#wzReview").hidden = !review;
    $("#wzGo").hidden = review || WZ.phase.startsWith("count") || WZ.phase.startsWith("capture");
    $("#wzSave").hidden = $("#wzRetry").hidden = !review;
    if (!review) {
      wzHand.set(fist ? FIST_POSE : OPEN_POSE);
      $("#wzTitle").textContent = fist ? "Сожмите кулак" : "Раскройте ладонь";
      $("#wzSub").textContent = fist ? "Согните все пальцы с сенсорами до упора и держите 3 секунды, не шевелясь."
        : "Выпрямите пальцы (ладонь расслаблена, пальцы прямые) и держите 3 секунды, не шевелясь.";
      const capturing = WZ.phase.startsWith("capture") && w.capturing;
      $("#wzCount").textContent = WZ.phase.startsWith("count") ? `через ${WZ.countdown}…` : capturing ? "держите…" : "";
      $("#wzBar").style.width = capturing ? `${Math.round((w.progress || 0) * 100)}%` : "0%";
      $("#wzGo").textContent = fist ? "Шаг 2: кулак" : "Начать";
      return;
    }
    const rows = w.summary || [];
    const html = rows.map((r, i) => `<tr class="g-${r.grade}"><td>S${i + 1}</td><td>${fname(map[i] || "")}</td>
      <td>${r.open ?? "—"}</td><td>${r.fist ?? "—"}</td><td>${r.span == null ? "—" : fmtDelta(r.span)}</td><td>${r.noise}</td>
      <td><span class="grade ${r.grade}">${r.grade_text}</span></td></tr>`).join("");
    if ($("#wzRows").innerHTML !== html) $("#wzRows").innerHTML = html;
    const bad = rows.filter((r) => r.grade === "flat" || r.grade === "no_data").length;
    const weak = rows.filter((r) => r.grade === "weak").length;
    $("#wzHint").textContent = bad ? `Каналов без изменения: ${bad}. Проверьте сенсор и провод (у такого канала сгиб будет приблизительным). Можно сохранить остальные или повторить.`
      : weak ? "Есть каналы со слабым сигналом — сгиб будет шумнее. Попробуйте согнуть палец сильнее и повторить."
      : "Все каналы в порядке. Размах — насколько меняется значение от ладони к кулаку; шум — разброс в неподвижной позе.";
  }
  $("#openWizard").addEventListener("click", openWizard);
  $("#calWizardBtn").addEventListener("click", openWizard);
  $("#wzCancel").addEventListener("click", closeWizard);
  $("#wzGo").addEventListener("click", () => startStep(S.wizard && S.wizard.step === "open_done" ? "fist" : "open"));
  $("#wzRetry").addEventListener("click", () => { api("/api/calibration/wizard-cancel", { method: "POST" }).catch(() => {}); WZ.phase = "intro"; startStep("open"); });
  $("#wzSave").addEventListener("click", async () => {
    try { const r = await api("/api/calibration/wizard-save", { method: "POST" }); toast(`Калибровка сохранена: каналов ${r.valid_channels} из ${S.n_channels}`); closeWizard(); }
    catch (e) { toast(e.message, true); }
  });

  // ---------------- демо ----------------
  function demoBits() { return (S.channels || []).map((c) => (c.bend >= 0.5 ? "1" : "0")).join(""); }
  function demoToggleFinger(finger) {
    if (!S || S.source.kind !== "mock") return;
    const i = S.finger_map.indexOf(finger);
    if (i < 0 || i >= S.n_channels) return;
    const bits = [...demoBits()];
    bits[i] = bits[i] === "1" ? "0" : "1";
    api("/api/mock", { method: "POST", body: { bits: bits.join("") } }).catch((e) => toast(e.message, true));
    $("#mockAuto").checked = $("#mockTour").checked = false;
  }
  function demoAngles(body) {
    api("/api/mock", { method: "POST", body }).catch(() => {});
    $("#mockTour").checked = false;
  }
  const SLIDERS = { roll: "#rollSlider", pitch: "#pitchSlider", yaw: "#yawSlider" };
  for (const [axis, sel] of Object.entries(SLIDERS)) {
    $(sel).addEventListener("input", (e) => {
      $(sel.replace("Slider", "Val")).textContent = deg(+e.target.value);
      demoAngles({ [axis]: +e.target.value });
    });
  }

  // ---------------- обработчики ----------------
  document.addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.matches("select[data-combo]")) {
        await api("/api/bindings", { method: "POST", body: { combo: t.dataset.combo, action_id: t.value || null } });
        toast(t.value ? `«${comboWords(t.dataset.combo, S.finger_map)}» → ${act(t.value).title}` : "Привязка снята");
      } else if (t.matches("input[data-role]")) {
        const roles = (S.roles || []).map((r) => ({ ...r }));
        roles[+t.dataset.ch][t.dataset.role] = t.checked;
        await api("/api/settings", { method: "POST", body: { channel_roles: roles } });
      } else if (t.matches("select[data-ch]")) {
        const map = [...S.finger_map];
        map[+t.dataset.ch] = t.value;
        await api("/api/settings", { method: "POST", body: { finger_map: map } });
      } else if (t.id === "handSide") {
        await api("/api/settings", { method: "POST", body: { hand: t.value } });
      } else if (t.id === "imuMount") {
        await api("/api/settings", { method: "POST", body: { imu_mount: t.value } });
        toast("Крепление модуля изменено — ориентация начата заново (калибровка 2 с)");
      } else if (t.id === "debounce") {
        await api("/api/settings", { method: "POST", body: { debounce_ms: +t.value } });
      } else if (t.id === "mockAuto") {
        await api("/api/mock", { method: "POST", body: { auto: t.checked } });
        if (t.checked) $("#mockTour").checked = false;
      } else if (t.id === "mockTour") {
        await api("/api/mock", { method: "POST", body: { tour: t.checked } });
        if (t.checked) $("#mockAuto").checked = false;
      } else if (t.id === "bleOnlyNamed") {
        renderBle();
      } else if (t.id === "showRaw") {
        chart.showRaw = t.checked;
      } else if (t.id === "chartSpan") {
        chart.span = +t.value;
      } else if (t.id === "replayFile" && t.files.length) {
        const f = t.files[0];
        await api("/api/connect", { method: "POST", body: { kind: "replay", text: await f.text(), name: f.name } });
        chart.clear(); toast(`Воспроизведение: ${f.name}`); t.value = "";
      } else if (t.id === "recList") {
        $("#recDownload").hidden = !t.value;
        $("#recDownload").href = t.value ? `/api/recordings/${encodeURIComponent(t.value)}` : "#";
      }
    } catch (err) { toast(err.message, true); }
  });

  $$("#chartMode button").forEach((b) => b.addEventListener("click", () => {
    $$("#chartMode button").forEach((x) => x.classList.toggle("active", x === b));
    chart.mode = b.dataset.mode;
    $("#showRawLabel").hidden = chart.mode !== "raw";
  }));

  $("#gripSlider").addEventListener("input", (e) => {
    const v = +e.target.value / 100;
    api("/api/mock", { method: "POST", body: { bends: Array(S.n_channels || 5).fill(v) } }).catch(() => {});
    $("#mockAuto").checked = $("#mockTour").checked = false;
  });

  $("#imuZero").addEventListener("click", () => api("/api/imu/zero", { method: "POST" }).then(() => toast("Поворот обнулён: текущее направление пальцев — «в экран»")).catch((e) => toast(e.message, true)));
  $("#imuCal").addEventListener("click", () => api("/api/imu/calibrate", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#viewReset").addEventListener("click", () => { if (hand3d) hand3d.resetView(); });

  $$("#srcKind button").forEach((btn) => btn.addEventListener("click", () => {
    $$("#srcKind button").forEach((x) => x.classList.toggle("active", x === btn));
    $$(".src-panel").forEach((p) => (p.hidden = p.dataset.panel !== btn.dataset.kind));
    if (btn.dataset.kind === "serial") refreshPorts();
  }));

  // BLE
  let bleDevices = [];
  function renderBle() {
    const list = bleDevices.filter((d) => !$("#bleOnlyNamed").checked || d.name);
    $("#bleList").innerHTML = list.length
      ? list.map((d) => `<li><div><div class="dname">${d.name || "(без имени)"} ${d.is_glove ? '<span class="glove">перчатка</span>' : ""}</div><div class="daddr">${d.address}</div></div>
          <span class="rssi">${d.rssi ?? ""} dBm</span><button class="small" data-ble="${d.address}" data-name="${d.name || ""}">Подключить</button></li>`).join("")
      : `<li class="muted">Ничего не найдено. Проверьте, что перчатка включена и Bluetooth на компьютере включён.</li>`;
  }
  $("#bleScan").addEventListener("click", async (e) => {
    const b = e.currentTarget; b.disabled = true; b.textContent = "Ищу… (5 с)";
    try { bleDevices = await api("/api/ble/scan?timeout=5"); renderBle(); }
    catch (err) { toast(err.message, true); }
    finally { b.disabled = false; b.textContent = "Найти устройства"; }
  });
  $("#bleList").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ble]"); if (!b) return;
    b.disabled = true; b.textContent = "Подключаюсь…";
    try { await api("/api/connect", { method: "POST", body: { kind: "ble", address: b.dataset.ble, name: b.dataset.name || null } }); chart.clear(); toast("Перчатка подключена"); location.hash = "#hand"; }
    catch (err) { toast(err.message, true); }
    finally { b.disabled = false; b.textContent = "Подключить"; }
  });

  // порт
  async function refreshPorts() {
    try {
      const ports = await api("/api/serial/ports");
      $("#serialPort").innerHTML = `<option value="">— порт —</option>` +
        ports.map((p) => `<option value="${p.port}">${p.port}${p.description ? " — " + p.description : ""}</option>`).join("");
    } catch (err) { toast(err.message, true); }
  }
  $("#serialRefresh").addEventListener("click", refreshPorts);
  $("#serialConnect").addEventListener("click", async () => {
    const port = $("#serialPort").value;
    if (!port) return toast("Выберите порт", true);
    try { await api("/api/connect", { method: "POST", body: { kind: "serial", port, baud: +$("#serialBaud").value } }); chart.clear(); toast("Порт открыт"); }
    catch (err) { toast(err.message, true); }
  });

  $("#mockStart").addEventListener("click", async () => {
    try { await api("/api/connect", { method: "POST", body: { kind: "mock", n_channels: +$("#mockN").value } }); chart.clear(); toast("Демо-режим включён"); location.hash = "#hand"; }
    catch (err) { toast(err.message, true); }
  });
  $("#disconnectBtn").addEventListener("click", () => api("/api/disconnect", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#clearBindings").addEventListener("click", () => api("/api/bindings", { method: "DELETE" }).then(() => toast("Привязки удалены")).catch((e) => toast(e.message, true)));

  // калибровка (сброс), запись, воспроизведение
  $("#calReset").addEventListener("click", () => api("/api/calibration", { method: "DELETE" }).then(() => toast("Калибровка сброшена")).catch((e) => toast(e.message, true)));
  $("#recBtn").addEventListener("click", async () => {
    try {
      if (S.recording.active) { const r = await api("/api/recording/stop", { method: "POST" }); toast(`Сохранено: data/recordings/${r.file}`); refreshRecordings(); }
      else await api("/api/recording/start", { method: "POST" });
    } catch (err) { toast(err.message, true); }
  });
  // фильтрация: пресеты конвейера и свой JSON (dsp, этап 3) — в «Настройки → Дополнительно»
  let PIPE = { presets: [], filters: [] }, pipeKey = "";
  async function loadPipelines() {
    try { PIPE = await api("/api/pipelines"); } catch (e) { return; }
    $("#pipePreset").innerHTML = PIPE.presets.map((p) => `<option value="${p.id}">${p.title}</option>`).join("") +
      `<option value="custom">Свой конвейер (JSON)…</option>`;
    $("#pipeTypes").textContent = PIPE.filters.join(", ");
    pipeKey = "";
  }
  function renderPipeline() {
    const cur = S.pipeline || {}, key = JSON.stringify(cur);
    const mp = $("#motionPill"); mp.textContent = S.motion ? "движение" : "покой"; mp.className = "pill" + (S.motion ? " motion" : "");
    if (key === pipeKey || !PIPE.presets.length) return;
    pipeKey = key;
    const sel = $("#pipePreset");
    if (document.activeElement !== sel) sel.value = cur.preset;
    showPipeHint(sel.value);
    if (cur.preset === "custom" && document.activeElement !== $("#pipeJson")) $("#pipeJson").value = JSON.stringify(cur.config);
  }
  function showPipeHint(id) {
    const p = PIPE.presets.find((x) => x.id === id);
    $("#pipeHint").textContent = p ? p.hint : "Свой конвейер: опишите блоки и нажмите «Применить».";
    $("#pipeCustom").hidden = id !== "custom";
    $("#pipeApply").hidden = id !== "custom";
    if (id === "custom" && !$("#pipeJson").value) $("#pipeJson").value = JSON.stringify((S.pipeline || {}).config || []);
  }
  async function applyPipeline(preset, config) {
    try {
      const r = await api("/api/pipeline", { method: "POST", body: { preset, config } });
      $("#pipeJson").classList.remove("bad");
      toast(`Фильтрация: ${r.title}`); chart.clear();
    } catch (e) { $("#pipeJson").classList.add("bad"); toast(e.message, true); }
  }
  $("#pipePreset").addEventListener("change", (e) => {
    showPipeHint(e.target.value);
    if (e.target.value !== "custom") applyPipeline(e.target.value);
  });
  $("#pipeApply").addEventListener("click", () => {
    let cfg;
    try { cfg = JSON.parse($("#pipeJson").value); } catch (e) { $("#pipeJson").classList.add("bad"); return toast("Ошибка JSON: " + e.message, true); }
    applyPipeline("custom", cfg);
  });

  // запись по протоколу этапа 2: 5 с покой → 5 × (сгиб 2 с / разгиб 2 с) → 5 с покой
  const PROTO = [["rest", 5]].concat(...Array.from({ length: 5 }, () => [["bend", 2], ["release", 2]]), [["rest", 5]]);
  let protoTimer = null;
  function renderProtoChannels() {
    const sel = $("#protoCh"), n = S ? S.n_channels : 0;
    const key = n + "|" + (S ? S.finger_map.join() : "");
    if (sel.dataset.key === key) return;
    sel.dataset.key = key;
    sel.innerHTML = Array.from({ length: n }, (_, i) => `<option value="${i}">S${i + 1} · ${fname(S.finger_map[i])}</option>`).join("") +
      (n > 1 ? `<option value="all">все пальцы вместе</option>` : "");
  }
  async function protoStop(save) {
    clearInterval(protoTimer); protoTimer = null;
    $("#proto").hidden = true;
    if (S && S.source.kind === "mock") api("/api/mock", { method: "POST", body: { bends: Array(S.n_channels).fill(0) } }).catch(() => {});
    if (S && S.recording.active) {
      const r = await api("/api/recording/stop", { method: "POST" }).catch(() => null);
      if (r) toast(save ? `Запись по протоколу сохранена: data/recordings/${r.file} — разбор: python tools/analyze_recording.py` : `Запись прервана: ${r.file}`, !save);
      refreshRecordings();
    }
  }
  $("#protoBtn").addEventListener("click", async () => {
    if (!S || S.mode !== "values") return toast("Нужен поток значений АЦП (подключите перчатку)", true);
    if (S.recording.active) return toast("Сначала остановите текущую запись", true);
    const ch = $("#protoCh").value;
    const who = ch === "all" ? "все пальцы" : `S${+ch + 1} (${fname(S.finger_map[+ch])})`;
    try { await api("/api/recording/start", { method: "POST" }); } catch (e) { return toast(e.message, true); }
    const total = PROTO.reduce((a, [, d]) => a + d, 0), t0 = performance.now();
    let lastPhase = -1;
    $("#proto").hidden = false;
    const tick = () => {
      const el = (performance.now() - t0) / 1000;
      if (el >= total) return protoStop(true);
      let acc = 0, i = 0;
      while (acc + PROTO[i][1] <= el) acc += PROTO[i++][1];
      const [kind, dur] = PROTO[i], bendNo = Math.floor((i + 1) / 2);
      $("#protoStep").textContent = `Запись по протоколу · ${who} · шаг ${i + 1} из ${PROTO.length}`;
      $("#protoCmd").textContent = kind === "rest" ? "Покой — ладонь раскрыта" : kind === "bend" ? `Согнуть ${who} (${bendNo} из 5)` : "Разогнуть";
      $("#protoCmd").className = "proto-cmd" + (kind === "bend" ? " bend" : "");
      $("#protoLeft").textContent = Math.ceil(acc + dur - el);
      $("#protoBar").style.width = `${Math.round((el / total) * 100)}%`;
      if (i !== lastPhase && S.source.kind === "mock") {        // в демо пальцы сгибаются сами — проверка без перчатки
        const b = Array(S.n_channels).fill(0);
        if (kind === "bend") (ch === "all" ? b.fill(1) : (b[+ch] = 1));
        api("/api/mock", { method: "POST", body: { bends: b } }).catch(() => {});
        $("#mockAuto").checked = $("#mockTour").checked = false;
      }
      lastPhase = i;
    };
    tick(); protoTimer = setInterval(tick, 100);
  });
  $("#protoCancel").addEventListener("click", () => protoStop(false));

  async function refreshRecordings() {
    try {
      const list = await api("/api/recordings");
      const sel = $("#recList"), cur = sel.value;
      sel.innerHTML = `<option value="">— записи приложения —</option>` + list.map((f) => `<option ${f.name === cur ? "selected" : ""}>${f.name}</option>`).join("");
    } catch (e) { /* ignore */ }
  }
  $("#recPlay").addEventListener("click", async () => {
    const name = $("#recList").value;
    if (!name) return toast("Выберите запись", true);
    try { await api("/api/connect", { method: "POST", body: { kind: "replay", recording: name } }); chart.clear(); toast(`Воспроизведение: ${name}`); }
    catch (err) { toast(err.message, true); }
  });
  $("#chartPause").addEventListener("click", (e) => { chart.paused = !chart.paused; e.target.textContent = chart.paused ? "Продолжить" : "Пауза"; });

  // клавиши: 1..5 — согнуть/разогнуть палец канала в демо, стрелки — кисть, Esc — закрыть мастер
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && WZ.open) return closeWizard();
    if (!S || S.source.kind !== "mock" || WZ.open || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const k = parseInt(e.key, 10);
    if (k >= 1 && k <= S.n_channels) demoToggleFinger(S.finger_map[k - 1]);
    const arrows = { ArrowLeft: ["rollSlider", -15], ArrowRight: ["rollSlider", 15], ArrowUp: ["pitchSlider", 15], ArrowDown: ["pitchSlider", -15] };
    if (arrows[e.key] && !["#signals", "#settings"].includes(location.hash)) {
      e.preventDefault();
      const [id, step] = arrows[e.key], sl = $("#" + id);
      sl.value = Math.max(+sl.min, Math.min(+sl.max, +sl.value + step));
      sl.dispatchEvent(new Event("input"));
    }
  });

  // ---------------- старт ----------------
  // высота шапки — для экрана «Рука» на всю высоту окна
  const setTopbar = () => document.documentElement.style.setProperty("--topbar-h", `${$(".topbar").offsetHeight}px`);
  new ResizeObserver(setTopbar).observe($(".topbar"));

  (async function init() {
    showTab();
    try { META = await api("/api/meta"); $("#ver").textContent = "v" + META.version; } catch (e) { /* сервер стартует */ }
    $("#imuMount").innerHTML = (META.imu_mounts || []).map((m) => `<option value="${m.id}">${m.title}</option>`).join("");
    init3d();
    await loadPipelines();
    connectWS();
  })();
})();
