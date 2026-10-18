/* FlexGlove — интерфейс. Получает состояние по WebSocket (/ws) и отправляет команды через REST (/api). */
(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let META = { actions: [], fingers: [] };
  let S = null;                 // последнее состояние от сервера
  let srcKindUI = "ble";        // выбранная вкладка источника
  let renderedBindKey = "";     // чтобы не перерисовывать таблицу без изменений
  let renderedMapKey = "";
  let lastEventTs = 0;

  const handMain = new HandView($("#handMain"));
  const handSettings = new HandView($("#handSettings"));

  // ---------------- утилиты ----------------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
    return data;
  }
  let toastTimer;
  function toast(msg, err = false) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3500);
  }
  const actionTitle = (id) => (META.actions.find((a) => a.id === id) || {}).title || id;
  const fingerName = (f) => (window.FINGER_NAMES[f] || f);
  function combosFor(n) {
    const out = [];
    for (let i = 0; i < 2 ** n; i++) out.push(i.toString(2).padStart(n, "0"));
    return out;
  }
  function bentFingers(combo, map) {
    const names = [];
    [...combo].forEach((c, i) => { if (c === "1" && map[i]) names.push(fingerName(map[i])); });
    return names.length ? names.join(", ") : "нет (покой)";
  }
  function bendsFrom(combo, map) {
    const bends = {};
    if (!combo) return bends;
    [...combo].forEach((c, i) => { if (map[i]) bends[map[i]] = c === "1" ? 1 : 0; });
    return bends;
  }

  // ---------------- вкладки ----------------
  function showTab() {
    const tab = location.hash === "#settings" ? "settings" : "main";
    $$(".page").forEach((p) => (p.hidden = p.id !== "tab-" + tab));
    $$(".tab").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  }
  window.addEventListener("hashchange", showTab);

  // ---------------- WebSocket ----------------
  function connectWS() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (e) => { S = JSON.parse(e.data); render(); };
    ws.onopen = () => { $("#connText").textContent = "Приложение запущено"; };
    ws.onclose = () => {
      $("#connDot").className = "dot"; $("#connText").textContent = "Нет связи с приложением — переподключаюсь…";
      setTimeout(connectWS, 1000);
    };
  }

  // ---------------- отрисовка ----------------
  let srcTabSynced = false;
  function render() {
    if (!S) return;
    const src = S.source || {};
    if (!srcTabSynced && src.kind) {  // открыть вкладку того источника, который уже подключён
      srcTabSynced = true;
      const btn = document.querySelector(`#srcKind button[data-kind="${src.kind}"]`);
      if (btn) btn.click();
    }
    const connected = !!src.connected;
    const map = S.finger_map_full || [];
    const live = S.raw;

    // шапка
    $("#connDot").className = "dot" + (connected ? " ok" : src.kind ? " wait" : "");
    $("#connText").textContent = connected ? `${src.name || "Устройство"} подключено` : (src.message || "Не подключено");
    $("#connRate").textContent = connected ? `${S.stats.rate} сообщ./с` : "";
    $("#noDeviceBanner").hidden = connected;

    // рука и комбинация
    const n = S.n_channels;
    const mapN = map.slice(0, n);
    const bends = bendsFrom(live, mapN);
    if (!live) mapN.forEach((f) => (bends[f] = 0));
    handMain.set(bends); handSettings.set(bends);
    $("#mainCombo").textContent = live || "—";
    $("#setCombo").textContent = live || "—";
    const bound = S.committed && S.bindings[S.committed];
    $("#mainTarget").textContent = bound ? `→ ${actionTitle(bound)}` : (S.committed ? "жест не привязан" : "");

    // объекты
    const comboOf = {};
    for (const [combo, a] of Object.entries(S.bindings)) comboOf[a] = combo;
    $$(".action").forEach((card) => {
      const id = card.dataset.action;
      const on = !!S.actions[id];
      const meta = META.actions.find((a) => a.id === id) || {};
      card.classList.toggle("on", on);
      card.querySelector(".a-state").textContent = on ? meta.on_label : meta.off_label;
      const c = comboOf[id];
      const html = c ? `жест <span class="chip">${c}</span>` : `<span class="chip none">жест не привязан</span>`;
      const bindEl = card.querySelector(".a-bind");
      if (bindEl.innerHTML !== html) bindEl.innerHTML = html;
    });

    // журнал
    renderLog();

    // демо-кнопки
    const isMock = src.kind === "mock" && connected;
    $("#demoStripMain").hidden = !isMock;
    renderDemoButtons(n, live);

    // статус источника
    $("#srcDot").className = "dot" + (connected ? " ok" : src.kind ? " wait" : "");
    $("#srcName").textContent = connected ? (src.name || "Подключено") : "Не подключено";
    $("#srcMsg").textContent = src.message || "";
    $("#disconnectBtn").disabled = !src.kind;

    // отладка
    $("#dbgRate").textContent = S.stats.rate;
    $("#dbgTotal").textContent = `${S.stats.total} / ${S.stats.unparsed}`;
    $("#dbgFormat").textContent = S.stats.format || "—";
    $("#dbgRaw").textContent = S.stats.last_raw || "—";
    $("#dbgN").textContent = n;

    renderFingerMap(n, map);
    renderBindings(n, mapN, live);
    const deb = $("#debounce");
    if (document.activeElement !== deb) deb.value = S.debounce_ms;
  }

  function renderLog() {
    const ev = S.events || [];
    const newest = ev.length ? ev[0].ts : 0;
    if (newest === lastEventTs && $("#eventLog").children.length) return;
    if (newest > lastEventTs && lastEventTs !== 0 && ev[0]) {
      const card = document.querySelector(`.action[data-action="${ev[0].action}"]`);
      if (card) { card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash"); }
    }
    lastEventTs = newest;
    $("#eventLog").innerHTML = ev.length
      ? ev.map((e) => {
          const t = new Date(e.ts * 1000).toLocaleTimeString("ru-RU");
          const how = e.source === "gesture" ? `жест <span class="chip">${e.combo}</span>` : "вручную";
          return `<li><span class="time">${t}</span><span><b>${e.title}</b> — ${e.label.toLowerCase()} · ${how}</span></li>`;
        }).join("")
      : `<li class="muted">Пока ничего не произошло</li>`;
  }

  function renderDemoButtons(n, live) {
    const key = n + "|" + live;
    $$("[data-demo-buttons]").forEach((box) => {
      if (box.dataset.key === key) return;
      box.dataset.key = key;
      box.innerHTML = combosFor(n).map((c) => `<button data-bits="${c}" class="${c === live ? "active" : ""}">${c}</button>`).join("");
    });
  }

  function renderFingerMap(n, map) {
    const key = n + "|" + map.join(",");
    if (key === renderedMapKey) return;
    renderedMapKey = key;
    const opts = (sel) => META.fingers.map((f) => `<option value="${f}" ${f === sel ? "selected" : ""}>${fingerName(f)}</option>`).join("");
    let html = "";
    for (let i = 0; i < n; i++) html += `<label>Канал ${i + 1} (${i + 1}-я цифра)<select data-ch="${i}">${opts(map[i])}</select></label>`;
    $("#fingerMap").innerHTML = html;
  }

  function renderBindings(n, mapN, live) {
    const key = JSON.stringify([n, mapN, S.bindings, META.actions.length]);
    if (key !== renderedBindKey) {
      renderedBindKey = key;
      const opts = (sel) => `<option value="">— не привязано —</option>` +
        META.actions.map((a) => `<option value="${a.id}" ${a.id === sel ? "selected" : ""}>${a.title}</option>`).join("");
      $("#bindBody").innerHTML = combosFor(n).map((c) => {
        const rest = !c.includes("1");
        const cell = rest
          ? `<span class="muted">состояние покоя — не привязывается</span>`
          : `<select data-combo="${c}">${opts(S.bindings[c])}</select>`;
        return `<tr data-row="${c}"><td>${c}</td><td>${bentFingers(c, mapN)}</td><td>${cell}</td></tr>`;
      }).join("");
    }
    $$("#bindBody tr").forEach((tr) => tr.classList.toggle("live", tr.dataset.row === live));
  }

  // ---------------- обработчики ----------------
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-bits]");
    if (b) { try { await api("/api/mock", { method: "POST", body: { bits: b.dataset.bits } }); $("#mockAuto").checked = false; } catch (err) { toast(err.message, true); } return; }
    const card = e.target.closest(".action");
    if (card) { try { await api(`/api/actions/${card.dataset.action}/toggle`, { method: "POST" }); } catch (err) { toast(err.message, true); } }
  });

  document.addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.matches("select[data-combo]")) {
        await api("/api/bindings", { method: "POST", body: { combo: t.dataset.combo, action_id: t.value || null } });
        toast(t.value ? `Жест ${t.dataset.combo} → ${actionTitle(t.value)}` : `Привязка ${t.dataset.combo} снята`);
      } else if (t.matches("select[data-ch]")) {
        const map = [...(S.finger_map_full || [])];
        map[+t.dataset.ch] = t.value;
        await api("/api/settings", { method: "POST", body: { finger_map: map } });
      } else if (t.id === "debounce") {
        await api("/api/settings", { method: "POST", body: { debounce_ms: +t.value } });
        toast(`Удержание: ${t.value} мс`);
      } else if (t.id === "mockAuto") {
        await api("/api/mock", { method: "POST", body: { auto: t.checked } });
      } else if (t.id === "bleOnlyNamed") {
        renderBle();
      }
    } catch (err) { toast(err.message, true); }
  });

  $$("#srcKind button").forEach((btn) => btn.addEventListener("click", () => {
    srcKindUI = btn.dataset.kind;
    $$("#srcKind button").forEach((x) => x.classList.toggle("active", x === btn));
    $$(".src-panel").forEach((p) => (p.hidden = p.dataset.panel !== srcKindUI));
    if (srcKindUI === "serial") refreshPorts();
  }));

  // BLE
  let bleDevices = [];
  function renderBle() {
    const onlyNamed = $("#bleOnlyNamed").checked;
    const list = bleDevices.filter((d) => !onlyNamed || d.name);
    $("#bleList").innerHTML = list.length
      ? list.map((d) => `<li><div><div class="dname">${d.name || "(без имени)"} ${d.is_glove ? '<span class="glove">перчатка</span>' : ""}</div><div class="daddr">${d.address}</div></div>
          <span class="rssi">${d.rssi ?? ""} dBm</span><button class="small" data-ble="${d.address}" data-name="${d.name || ""}">Подключить</button></li>`).join("")
      : `<li class="muted">Ничего не найдено. Проверьте, что перчатка включена и Bluetooth на компьютере включён.</li>`;
  }
  $("#bleScan").addEventListener("click", async (e) => {
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Ищу… (5 с)";
    try { bleDevices = await api("/api/ble/scan?timeout=5"); renderBle(); }
    catch (err) { toast(err.message, true); }
    finally { btn.disabled = false; btn.textContent = "Найти устройства"; }
  });
  $("#bleList").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ble]"); if (!b) return;
    b.disabled = true; b.textContent = "Подключаюсь…";
    try { await api("/api/connect", { method: "POST", body: { kind: "ble", address: b.dataset.ble, name: b.dataset.name || null } }); toast("Перчатка подключена"); }
    catch (err) { toast(err.message, true); }
    finally { b.disabled = false; b.textContent = "Подключить"; }
  });

  // Serial
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
    try { await api("/api/connect", { method: "POST", body: { kind: "serial", port, baud: +$("#serialBaud").value } }); toast("Порт открыт"); }
    catch (err) { toast(err.message, true); }
  });

  // Mock
  $("#mockStart").addEventListener("click", async () => {
    try { await api("/api/connect", { method: "POST", body: { kind: "mock" } }); toast("Демо-режим включён"); }
    catch (err) { toast(err.message, true); }
  });

  $("#disconnectBtn").addEventListener("click", async () => { try { await api("/api/disconnect", { method: "POST" }); } catch (err) { toast(err.message, true); } });
  $("#resetActions").addEventListener("click", () => api("/api/actions/reset", { method: "POST" }).catch((e) => toast(e.message, true)));
  $("#clearBindings").addEventListener("click", async () => { try { await api("/api/bindings", { method: "DELETE" }); toast("Привязки удалены"); } catch (err) { toast(err.message, true); } });

  // клавиши 0..(2^n − 1) в демо-режиме
  document.addEventListener("keydown", (e) => {
    if (!S || S.source.kind !== "mock" || !S.source.connected) return;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const k = parseInt(e.key, 10);
    if (Number.isNaN(k) || k >= 2 ** S.n_channels) return;
    api("/api/mock", { method: "POST", body: { bits: k.toString(2).padStart(S.n_channels, "0") } }).catch(() => {});
    $("#mockAuto").checked = false;
  });

  // ---------------- старт ----------------
  (async function init() {
    showTab();
    try { META = await api("/api/meta"); } catch (e) { /* сервер ещё стартует */ }
    connectWS();
  })();
})();
