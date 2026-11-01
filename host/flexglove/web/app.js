/* FlexGlove — интерфейс пульта. Состояние приходит по WebSocket (/ws) ~30 раз в секунду,
   команды уходят через REST (/api). */
(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const STATE_TEXT = {
    active: "Управление активно", disarmed: "Управление заблокировано",
    hold: "Захват зафиксирован", no_link: "Нет связи с перчаткой",
  };
  const MODE_TEXT = { calibrated: "калибровка", auto: "автокалибровка", uncalibrated: "нет калибровки", bits: "биты 0/1" };

  let META = { actions: [], fingers: [], version: "" };
  let S = null;
  let lastEventTs = 0;
  let bindKey = "", mapKey = "", tableKey = "";

  const handMain = new HandView($("#handMain"), { onFinger: (f) => demoToggleFinger(f) });
  const handSettings = new HandView($("#handSettings"));
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
  const pct = (x) => `${Math.round((x || 0) * 100)}%`;
  const combosFor = (n) => Array.from({ length: 2 ** n }, (_, i) => i.toString(2).padStart(n, "0").split("").reverse().join(""))
    .sort((a, b) => (a.split("1").length - b.split("1").length) || (a < b ? 1 : -1));
  function bendsByFinger(values, map) {
    const out = {};
    values.forEach((v, i) => { if (map[i]) out[map[i]] = v; });
    return out;
  }
  function bentNames(combo, map) {
    const names = [...combo].map((c, i) => (c === "1" ? fname(map[i]) : null)).filter(Boolean);
    return names.length ? names.join(", ") : "все разогнуты";
  }

  // ---------------- вкладки ----------------
  function showTab() {
    const tab = ["#signals", "#settings"].includes(location.hash) ? location.hash.slice(1) : "control";
    $$(".page").forEach((p) => (p.hidden = p.id !== "tab-" + tab));
    $$(".tab").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
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
    if (hand3d) hand3d.setTarget({ q: ori && !ori.stale ? ori.q : null, bends, sensed: new Set(map.slice(0, n)), hand: S.hand });
    renderOrientation(ori);
    $("#mainCombo").textContent = S.combo || "—";
    const bound = S.committed && S.bindings[S.committed];
    $("#mainTarget").textContent = bound ? `→ ${act(bound).title}` : "";
    renderBendBars(chans, map);

    // контроллер и сцена
    const c = S.controller;
    const st = $("#ctlState"); st.className = "ctl-state " + c.state; st.textContent = STATE_TEXT[c.state] || c.state;
    $("#tgtVal").textContent = pct(c.target); $("#tgtBar").style.width = pct(c.target);
    $("#cmdVal").textContent = pct(c.command); $("#cmdBar").style.width = pct(c.command);
    $("#limitMark").style.left = pct(c.max_close);
    renderScene(c, S.actions.light);
    renderRotation(c, ori);
    renderToggles();
    renderCommandStatus(map);
    renderLog();

    const isMock = src.kind === "mock" && connected;
    $("#demoStrip").hidden = !isMock;
    document.body.classList.toggle("demo", isMock);

    // сигналы
    if (S.mode !== "none") {
      chart.labels = chans.map((_, i) => `S${i + 1} · ${fname(map[i] || "")}`);
      chart.push(performance.now() / 1000, chans.map((x) => x.raw), chans.map((x) => (x.filtered ?? x.bit)));
    }
    renderChannelTable(chans, map);
    renderSignalsToolbar();
    renderProtoChannels();
    renderPipeline();

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

  function renderBendBars(chans, map) {
    const box = $("#bendBars");
    if (box.children.length !== chans.length) {
      box.innerHTML = chans.map((_, i) => `<div class="bend-row"><span></span><div class="bar"><div class="fill"></div></div><b></b></div>`).join("");
    }
    chans.forEach((ch, i) => {
      const row = box.children[i];
      row.children[0].textContent = `${i + 1} · ${fname(map[i] || "")}`;
      row.querySelector(".fill").style.width = pct(ch.stale ? 0 : ch.bend);
      row.children[2].textContent = ch.stale ? "—" : pct(ch.bend);
    });
  }

  function renderScene(c, light) {
    const scene = $("#scene");
    const gapMax = 100, sampleW = 36;
    const gap = gapMax * (1 - c.command);
    const vis = Math.max(gap, sampleW);
    const grabbed = gap <= sampleW + 2;
    $("#jawL").setAttribute("transform", `translate(${180 - vis / 2} 0)`);
    $("#jawR").setAttribute("transform", `translate(${180 + vis / 2} 0)`);
    scene.classList.toggle("grabbed", grabbed);
    scene.classList.toggle("light-on", !!light);
    $("#sceneStatus").textContent = grabbed ? "Образец захвачен" : (c.state === "active" ? "Захват следует за рукой" : STATE_TEXT[c.state]);
  }

  const deg = (x) => `${x > 0 ? "+" : ""}${Math.round(x || 0)}°`;
  function renderOrientation(o) {
    $("#imuBox").hidden = !o;
    if (!o) return;
    $("#imuRoll").textContent = deg(o.roll); $("#imuPitch").textContent = deg(o.pitch); $("#imuYaw").textContent = deg(o.yaw);
    const st = o.calibrating ? "⏳ Калибровка гироскопа: держите руку неподвижно 2 с"
      : !o.bias_ok ? "Гироскоп не откалиброван — подержите руку неподвижно"
      : `Гироскоп откалиброван (${o.bias_source}) · ${o.at_rest ? "рука в покое" : "движение"}` + (o.stale ? " · нет данных IMU" : "");
    $("#imuStatus").textContent = o.message && !o.calibrating && o.message.includes("не принята") ? o.message : st;
    $("#imuDrift").textContent = `Поворот вокруг вертикали (рысканье) у MPU6050 медленно уходит: ≈ ${String(o.yaw_drift_deg_min).replace(".", ",")}°/мин в покое, ` +
      "в движении больше — нажимайте «Обнулить положение». Наклоны держатся по силе тяжести.";
    $("#imuZero").disabled = $("#imuCal").disabled = !!o.calibrating;
  }

  function renderRotation(c, o) {
    $("#rotMeter").hidden = !o;
    $("#rotDial").style.display = o ? "" : "none";
    if (!o) return;
    $("#rotVal").textContent = `${deg(c.rot_target)} → ${deg(c.rot_command)}`;
    // вид сверху: губки повёрнуты на команду, пунктир — куда тянет кисть
    $("#rdJaws").setAttribute("transform", `rotate(${-c.rot_command})`);
    $("#rdTarget").setAttribute("transform", `rotate(${-c.rot_target})`);
    $("#rdLabel").textContent = `поворот ${deg(c.rot_command)}`;
  }

  function renderToggles() {
    const box = $("#toggles");
    const comboOf = {};
    for (const [combo, a] of Object.entries(S.bindings)) comboOf[a] = combo;
    if (!box.children.length && META.actions.length) {
      box.innerHTML = META.actions.map((a) => `<button class="toggle" data-action="${a.id}" title="${a.hint}">
        <span class="t-title">${a.title}</span><span class="t-bind"></span><span class="t-state"></span></button>`).join("");
    }
    $$("#toggles .toggle").forEach((b) => {
      const id = b.dataset.action, on = !!S.actions[id], a = act(id);
      b.classList.toggle("on", on);
      b.querySelector(".t-state").textContent = on ? a.on_label : a.off_label;
      const html = comboOf[id] ? `<span class="chip">${comboOf[id]}</span>` : `<span class="chip none">нет жеста</span>`;
      const tb = b.querySelector(".t-bind"); if (tb.innerHTML !== html) tb.innerHTML = html;
    });
  }

  function renderCommandStatus(map) {
    const cs = S.command || {}, roles = S.roles || [], box = $("#cmdStatus");
    const pure = roles.map((r, i) => (r.cmd && !r.grip ? i : -1)).filter((i) => i >= 0);
    const armed = cs.armed_mode;
    let text;
    if (!S.n_channels) text = "";
    else if (!armed) text = "Управление не активно: жесты — по всем пальцам команд.";
    else if (!pure.length) text = "Управление активно. Жестовых команд нет: все пальцы ведут захват (снимите ARM кнопкой).";
    else {
      const items = Object.entries(cs.armed_bindings || {}).map(([combo, a]) => `${bentNames(combo, map)} → ${act(a).title}`);
      text = `Управление активно. Команды удержанием ${cs.hold_s || 1} с: ${items.length ? items.join("; ") : "нет привязок"}.`;
    }
    if (cs.blocked) text = "Жест не выполнен: пальцы команд двигались вместе со сжатием.";
    $("#cmdText").textContent = text;
    box.classList.toggle("blocked", !!cs.blocked);
    $("#cmdHoldBar").style.width = pct(cs.blocked ? 0 : cs.progress);
  }

  function renderLog() {
    const ev = S.events || [];
    const newest = ev.length ? ev[0].ts : 0;
    if (newest === lastEventTs && $("#eventLog").children.length) return;
    if (lastEventTs && newest > lastEventTs && ev[0].action) {
      const b = document.querySelector(`.toggle[data-action="${ev[0].action}"]`);
      if (b) { b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash"); }
    }
    lastEventTs = newest;
    $("#eventLog").innerHTML = ev.length ? ev.map((e) => {
      const t = new Date(e.ts * 1000).toLocaleTimeString("ru-RU");
      const how = e.source === "gesture" ? ` · жест <span class="chip">${e.combo}</span>` : e.source === "manual" ? " · вручную" : "";
      return `<li class="${e.source === "system" ? "sys" : ""}"><span class="time">${t}</span><span>${e.text}${how}</span></li>`;
    }).join("") : `<li class="muted">Пока ничего не произошло</li>`;
  }

  function renderChannelTable(chans, map) {
    const key = chans.length + "|" + map.join();
    if (key !== tableKey) {
      tableKey = key;
      $("#chBody").innerHTML = chans.map((_, i) => `<tr><td><span class="swatch" style="background:${CHANNEL_COLORS[i]}"></span>S${i + 1}</td>
        <td>${fname(map[i] || "")}</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>`).join("");
    }
    const rows = $("#chBody").children;
    chans.forEach((ch, i) => {
      const td = rows[i].children;
      td[2].textContent = ch.raw ?? "—";
      td[3].textContent = ch.filtered ?? "—";
      td[4].textContent = ch.stale ? "—" : pct(ch.bend);
      td[5].textContent = ch.bit;
      const mode = ch.stale ? "stale" : ch.mode;
      const html = `<span class="pill ${mode}">${ch.stale ? "нет данных" : MODE_TEXT[ch.mode] || ch.mode}</span>`;
      if (td[6].innerHTML !== html) td[6].innerHTML = html;
      td[7].textContent = ch.cal.open != null || ch.cal.fist != null
        ? `${ch.cal.open != null ? Math.round(ch.cal.open) : "—"} / ${ch.cal.fist != null ? Math.round(ch.cal.fist) : "—"}` : "—";
    });
    const auto = chans.some((c) => c.mode === "uncalibrated" && !c.stale);
    $("#sigHint").textContent = S.mode === "values"
      ? (auto ? "Каналы без калибровки: держите ладонь раскрытой при подключении, затем согните пальцы — включится автокалибровка. Точнее — кнопки «Ладонь раскрыта» и «Кулак сжат»." : "")
      : S.mode === "bits" ? "Перчатка присылает готовые биты 0/1 — графики показывают только состояния." : "";
  }

  function renderSignalsToolbar() {
    const r = S.recording;
    const btn = $("#recBtn");
    btn.textContent = r.active ? "■ Остановить запись" : "● Начать запись";
    btn.classList.toggle("rec-on", r.active);
    $("#recStatus").textContent = r.active ? `${r.file} · строк: ${r.rows}` : (r.file ? `последняя: ${r.file}` : "");
    const calOk = S.mode === "values";
    $("#calOpen").disabled = $("#calFist").disabled = !calOk || !!S.calibrating;
    $("#calStatus").textContent = S.calibrating
      ? `Замер «${S.calibrating === "open" ? "ладонь" : "кулак"}»… держите руку неподвижно`
      : S.last_calibration ? `Откалибровано каналов: ${S.last_calibration.valid_channels} из ${S.n_channels}` : (calOk ? "" : "нужен поток значений АЦП");
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
      : !r.some((x) => x.cmd && !x.grip) ? "Нет пальцев «только команды» — пока управление активно, жесты не работают (ARM снимается кнопкой)." : "";
    $("#rolesWarn").textContent = warn; $("#rolesWarn").hidden = !warn;
  }

  function armedLabel(combo) {
    const a = S.bindings[combo];
    if (!a) return "";
    const ab = (S.command || {}).armed_bindings || {};
    const ok = Object.entries(ab).some(([proj, act_]) => act_ === a && [...proj].every((ch, i) => ch === "0" || combo[i] === "1"));
    return `<div class="bc-armed ${ok ? "on" : ""}">${ok ? "✓ работает при ARM" : "при ARM — нет"}</div>`;
  }

  function renderBindings(n, map) {
    const key = JSON.stringify([n, map, S.bindings, META.actions.length, (S.command || {}).armed_bindings]);
    if (key !== bindKey) {
      bindKey = key;
      const opts = (sel) => `<option value="">— нет команды —</option>` +
        META.actions.map((a) => `<option value="${a.id}" ${a.id === sel ? "selected" : ""}>${a.title}</option>`).join("");
      const grid = $("#bindGrid");
      grid.innerHTML = n ? combosFor(n).map((c) => {
        const rest = !c.includes("1");
        return `<div class="bind-card ${S.bindings[c] ? "bound" : ""}" data-combo="${c}"><svg></svg>
          <div class="bc-combo">${c}</div><div class="small muted">${bentNames(c, map)}</div>
          ${rest ? `<div class="bc-rest">покой — без команды</div>` : `<select data-combo="${c}">${opts(S.bindings[c])}</select>${armedLabel(c)}`}</div>`;
      }).join("") : `<p class="muted">Подключите перчатку или демо — появятся все комбинации для числа каналов.</p>`;
      $$("#bindGrid .bind-card").forEach((card) => {
        const hv = new HandView(card.querySelector("svg"), { static: true });
        hv.set(bendsByFinger([...card.dataset.combo].map(Number), map));
      });
    }
    $$("#bindGrid .bind-card").forEach((c) => c.classList.toggle("live", c.dataset.combo === S.committed));
  }

  // ---------------- демо ----------------
  function demoBits() { return (S.channels || []).map((c) => (c.bend >= 0.5 ? "1" : "0")).join(""); }
  function demoToggleFinger(finger) {
    if (!S || S.source.kind !== "mock") return;
    const i = S.finger_map.indexOf(finger);
    if (i < 0 || i >= S.n_channels) return;
    const bits = [...demoBits()];
    bits[i] = bits[i] === "1" ? "0" : "1";
    api("/api/mock", { method: "POST", body: { bits: bits.join("") } }).catch((e) => toast(e.message, true));
    $("#mockAuto").checked = false;
  }

  // ---------------- обработчики ----------------
  document.addEventListener("click", async (e) => {
    const t = e.target.closest(".toggle");
    if (t) { try { await api(`/api/actions/${t.dataset.action}/toggle`, { method: "POST" }); } catch (err) { toast(err.message, true); } }
  });

  document.addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.matches("select[data-combo]")) {
        await api("/api/bindings", { method: "POST", body: { combo: t.dataset.combo, action_id: t.value || null } });
        toast(t.value ? `Жест ${t.dataset.combo} → ${act(t.value).title}` : `Привязка ${t.dataset.combo} снята`);
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

  $("#gripSlider").addEventListener("input", (e) => {
    const v = +e.target.value / 100;
    api("/api/mock", { method: "POST", body: { bends: Array(S.n_channels || 5).fill(v) } }).catch(() => {});
    $("#mockAuto").checked = false;
  });

  $("#imuZero").addEventListener("click", () => api("/api/imu/zero", { method: "POST" }).then(() => toast("Положение обнулено")).catch((e) => toast(e.message, true)));
  $("#imuCal").addEventListener("click", () => api("/api/imu/calibrate", { method: "POST" }).catch((e) => toast(e.message, true)));
  function demoAngles(body) {
    api("/api/mock", { method: "POST", body }).catch(() => {});
    $("#mockAuto").checked = false;
  }
  $("#rollSlider").addEventListener("input", (e) => demoAngles({ roll: +e.target.value }));
  $("#pitchSlider").addEventListener("input", (e) => demoAngles({ pitch: +e.target.value }));

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
    try { await api("/api/connect", { method: "POST", body: { kind: "ble", address: b.dataset.ble, name: b.dataset.name || null } }); chart.clear(); toast("Перчатка подключена"); }
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
    try { await api("/api/connect", { method: "POST", body: { kind: "mock", n_channels: +$("#mockN").value } }); chart.clear(); toast("Демо-режим включён"); location.hash = "#control"; }
    catch (err) { toast(err.message, true); }
  });
  $("#disconnectBtn").addEventListener("click", () => api("/api/disconnect", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#resetActions").addEventListener("click", () => api("/api/actions/reset", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#clearBindings").addEventListener("click", () => api("/api/bindings", { method: "DELETE" }).then(() => toast("Привязки удалены")).catch((e) => toast(e.message, true)));

  // калибровка, запись, воспроизведение
  $("#calOpen").addEventListener("click", () => api("/api/calibration/open", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#calFist").addEventListener("click", () => api("/api/calibration/fist", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#calReset").addEventListener("click", () => api("/api/calibration", { method: "DELETE" }).catch((e) => toast(e.message, true)));
  $("#recBtn").addEventListener("click", async () => {
    try {
      if (S.recording.active) { const r = await api("/api/recording/stop", { method: "POST" }); toast(`Сохранено: data/recordings/${r.file}`); refreshRecordings(); }
      else await api("/api/recording/start", { method: "POST" });
    } catch (err) { toast(err.message, true); }
  });
  // фильтрация: пресеты конвейера и свой JSON (dsp, этап 3)
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
      $("#protoBar").style.width = pct(el / total);
      if (i !== lastPhase && S.source.kind === "mock") {        // в демо пальцы сгибаются сами — проверка без перчатки
        const b = Array(S.n_channels).fill(0);
        if (kind === "bend") (ch === "all" ? b.fill(1) : (b[+ch] = 1));
        api("/api/mock", { method: "POST", body: { bends: b } }).catch(() => {});
        $("#mockAuto").checked = false;
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

  // клавиши 1..5 — согнуть/разогнуть палец канала в демо-режиме
  document.addEventListener("keydown", (e) => {
    if (!S || S.source.kind !== "mock" || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const k = parseInt(e.key, 10);
    if (k >= 1 && k <= S.n_channels) demoToggleFinger(S.finger_map[k - 1]);
    const arrows = { ArrowLeft: ["rollSlider", -10], ArrowRight: ["rollSlider", 10], ArrowUp: ["pitchSlider", 10], ArrowDown: ["pitchSlider", -10] };
    if (arrows[e.key] && location.hash !== "#signals" && location.hash !== "#settings") {
      e.preventDefault();
      const [id, step] = arrows[e.key], sl = $("#" + id);
      sl.value = Math.max(+sl.min, Math.min(+sl.max, +sl.value + step));
      demoAngles(id === "rollSlider" ? { roll: +sl.value } : { pitch: +sl.value });
    }
  });

  // ---------------- старт ----------------
  (async function init() {
    showTab();
    try { META = await api("/api/meta"); $("#ver").textContent = "v" + META.version; } catch (e) { /* сервер стартует */ }
    $("#imuMount").innerHTML = (META.imu_mounts || []).map((m) => `<option value="${m.id}">${m.title}</option>`).join("");
    init3d();
    await loadPipelines();
    connectWS();
  })();
})();
