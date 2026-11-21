/* FlexGlove: четыре экрана, единый поток состояния и обработчики REST. */
(function () {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const MODE_TEXT = { calibrated: "калибровка", auto: "автокалибровка", uncalibrated: "нет калибровки", bits: "биты 0/1" };

  let META = { actions: [], fingers: [], version: "" };
  let S = null;
  let lastEventTs = 0;
  let gestureKey = "", mapKey = "", tableKey = "";

  const handSettings = new HandView($("#handSettings"));
  const handMain = new HandView($("#handMain"), { onFinger: (f) => demoToggleFinger(f) });   // 2D, если нет WebGL
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
    const tab = ["#gestures", "#signals", "#settings"].includes(location.hash) ? location.hash.slice(1) : "hand";
    $$(".page").forEach((p) => (p.hidden = p.id !== "tab-" + tab));
    $$(".tab").forEach((a) => {
      a.classList.toggle("active", a.dataset.tab === tab);
      if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    document.body.classList.toggle("on-hand", tab === "hand");
    if (tab === "signals") refreshRecordings();
  }
  window.addEventListener("hashchange", showTab);

  // ---------------- WebSocket ----------------
  let wsLive = false, lastSnapshot = 0, activeWS = null;
  function connectionLost() {
    wsLive = false;
    $("#sideDot").className = "dot";
    $("#sideSrc").textContent = "Нет связи с приложением. Переподключение…";
    $("#sideImu").textContent = "Данные IMU устарели";
    $("#gestureWords").textContent = "Нет связи с приложением";
    $("#gestureProgress").hidden = true;
    $("#gestureReason").hidden = true;
    $("#sideErrors").textContent = "Поток состояния недоступен";
    $("#sideErrors").hidden = false;
    $("#srcDot").className = "dot";
    $("#srcName").textContent = "Нет связи с приложением";
    $("#demoStrip").hidden = true;
  }
  function connectWS() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    activeWS = ws;
    ws.onmessage = (e) => { S = JSON.parse(e.data); wsLive = true; lastSnapshot = performance.now(); render(); };
    ws.onerror = connectionLost;
    ws.onclose = () => { connectionLost(); setTimeout(connectWS, 1000); };
  }
  setInterval(() => {
    if (wsLive && performance.now() - lastSnapshot > 2500) {
      connectionLost();
      activeWS?.close();                              // onclose запускает переподключение
    }
  }, 500);

  // ---------------- отрисовка ----------------
  function render() {
    const src = S.source || {}, connected = wsLive && !!src.connected, n = S.n_channels, map = S.finger_map;
    const chans = S.channels || [];

    $("#noDeviceBanner").hidden = connected;
    const warns = S.warnings || [];
    const wbox = $("#streamWarn");
    const whtml = warns.map((w) => `<div>${esc(w)}</div>`).join("");
    if (wbox.innerHTML !== whtml) wbox.innerHTML = whtml;
    wbox.hidden = !warns.length;

    // рука
    const bends = bendsByFinger(chans.map((c) => (c.stale ? 0 : c.bend)), map);
    handMain.set(bends); handSettings.set(bends);
    const ori = S.orientation;
    if (hand3d) hand3d.setTarget({ q: ori && !ori.stale ? ori.q_world : null, bends, sensed: new Set(map.slice(0, n)), hand: S.hand });
    if (gesturePreview) {
      gesturePreview.setTarget({ q: ori && !ori.stale ? ori.q_world : null, bends, sensed: new Set(map.slice(0, n)), hand: S.hand });
      gesturePreview.mirror = hand3d;
    }
    renderSide(src, connected, chans, map, ori);
    renderLog();

    const isMock = src.kind === "mock" && connected;
    $("#demoStrip").hidden = !isMock;
    document.body.classList.toggle("demo", isMock);
    if (isMock) syncDemo(src.demo);
    // Полоса демо исключена из доступной сцены; центр ладони остаётся неподвижным.
    if (hand3d) hand3d.setInsets({ top: 36, bottom: isMock ? $("#demoStrip").offsetHeight + 24 : 12 });

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
    $("#srcDot").className = "dot" + (connected && !chans.some(c => c.stale) ? " ok" : src.kind ? " wait" : "");
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
    renderGestures();
    const hs = $("#handSide"); if (document.activeElement !== hs) hs.value = S.hand || "right";
    const mt = $("#imuMount"); if (document.activeElement !== mt && ori) mt.value = ori.mount;
  }

  /** Статус сохранённой калибровки для настроек и диагностики. */
  function calibrationStatus(chans) {
    if (S.source?.kind === "mock") return { cls: "ok", text: "Демо: известные диапазоны генератора" };
    const live = chans.filter((c) => !c.stale && c.mode !== "bits");
    if (!live.length) return { cls: "", text: S.mode === "bits" ? "Перчатка присылает готовые 0/1, калибровка не нужна" : "" };
    const cal = live.filter((c) => c.mode === "calibrated").length;
    if (cal === live.length) return { cls: "ok", text: `Калибровка выполнена (${cal} из ${live.length})` };
    if (cal) return { cls: "warn", text: `Калибровка частичная: ${cal} из ${live.length}. Откройте калибровку в настройках` };
    return { cls: "warn", text: "Нужна калибровка. Сгиб приблизительный. Откройте настройки" };
  }

  function renderSide(src, connected, chans, map, ori) {
    $("#sideDot").className = "dot" + (connected && !chans.some(c => c.stale) ? " ok" : src.kind ? " wait" : "");
    $("#sideSrc").textContent = connected ? `${src.name || "Перчатка"}${chans.some(c => c.stale) ? ": данные устарели" : ""}` : (src.message || "Не подключено");
    $("#sideImu").textContent = !connected ? "" : !ori ? "Без датчика ориентации. Пальцы направлены вверх"
      : ori.calibrating ? "Калибровка гироскопа: держите руку неподвижно 2 с"
      : ori.stale ? "Нет данных MPU6050" : `MPU6050: ${ori.bias_ok ? "гироскоп откалиброван" : "гироскоп калибруется в покое"} · ${ori.at_rest ? "покой" : "движение"}`;
    const errors = [...(S.warnings || [])];
    if (S.stats.unparsed) errors.push(`Ошибки разбора: ${S.stats.unparsed}`);
    if (ori?.message?.includes("не принята")) errors.push(ori.message);
    $("#sideErrors").textContent = errors.join(" · ");
    $("#sideErrors").hidden = !errors.length;

    const gs = S.gesture_system || {}, ts = gs.templates || [];
    const fresh = connected && chans.length > 0 && chans.every(c => !c.stale)
      && !gs.reason?.startsWith("Недостаточно свежих данных");
    const candidate = fresh && ts.find(t => t.id === gs.candidate && t.compatible);
    const calibrated = chans.length > 0 && chans.every(c => c.mode === "calibrated");
    let main = "Покажите жест";
    if (!connected) main = "Подключите источник";
    else if (!fresh) main = "Нет свежих данных";
    else if (gs.training) main = `Обучение: ${gs.training.name}`;
    else if (S.mode !== "values") main = "Нужны непрерывные значения АЦП";
    else if (S.calibrating || (S.wizard?.step && S.wizard.step !== "idle") || ori?.calibrating
      || gs.reason?.startsWith("Во время калибровки"))
      main = "Дождитесь окончания калибровки";
    else if (!calibrated) main = "Откалибруйте все каналы в настройках";
    else if (!ts.length) main = "Создайте жест на экране «Жесты»";
    else if (!ts.some(t => t.compatible)) main = "Переобучите жесты для текущих настроек";
    else if (candidate) main = candidate.name;
    if ($("#gestureWords").textContent !== main) $("#gestureWords").textContent = main;
    const holding = !!candidate && main === candidate.name && !gs.training;
    $("#gestureProgress").hidden = !holding;
    $("#gestureProgress").value = holding ? gs.hold_progress || 0 : 0;
    $("#gestureReason").hidden = !holding;
    $("#gestureReason").textContent = holding
      ? (gs.latched === candidate.id || gs.needs_exit ? "Для повтора выйдите из позы" : "Удерживайте позу") : "";

    // ориентация
    $("#imuBox").hidden = !ori;
    // при почти вертикальных пальцах крен (поворот вокруг оси пальцев) по силе тяжести не определён — число скачет
    const rollText = ori && Math.abs(ori.pitch) > 80 ? "—" : deg(ori?.roll);
    if (ori) {
      $("#imuPitch").textContent = `${deg(ori.pitch)} ${ori.pitch > 25 ? "вверх" : ori.pitch < -25 ? "вниз" : ""}`;
      $("#imuRoll").textContent = rollText; $("#imuYaw").textContent = deg(ori.yaw);
      $("#imuStatus").textContent = ori.message && ori.message.includes("не принята") ? ori.message
        : `Поворот вокруг вертикали у MPU6050 медленно уходит (≈ ${String(ori.yaw_drift_deg_min).replace(".", ",")}°/мин). Направьте пальцы на экран и нажмите «Обнулить поворот».`;
    }
    $("#imuZero").disabled = $("#imuCal").disabled = !ori || !!ori.calibrating;
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
      const how = e.source === "gesture" && e.combo ? ` · жест «${comboWords(e.combo, S.finger_map)}»` : e.source === "manual" ? " · вручную" : "";
      return `<li class="${e.source === "system" ? "sys" : ""}"><span class="time">${t}</span><span>${esc(e.text.replace(/ \(событие \d+\)/g, ""))}${how}</span></li>`;
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
      ? (cs.cls === "warn" ? "Без калибровки сгиб считается по автокалибровке (от уровня при подключении), это приблизительно. Выполните калибровку в настройках." : "")
      : S.mode === "bits" ? "Перчатка присылает готовые 0/1, графики показывают только состояния." : "";
  }

  function renderSignalsToolbar() {
    const r = S.recording;
    const btn = $("#recBtn");
    btn.textContent = r.active ? "Остановить запись" : "Начать запись";
    btn.classList.toggle("rec-on", r.active);
    $("#recStatus").textContent = r.active ? `${r.file} · строк: ${r.rows}` : (r.file ? `последняя: ${r.file}` : "");
    const calOk = S.mode === "values" && S.source?.kind !== "mock";
    $("#calReset").disabled = S.source?.kind === "mock";
    $("#calWizardBtn").disabled = !calOk;
    const cs = calibrationStatus(S.channels || []);
    $("#calStatus").textContent = S.source?.kind === "mock" || calOk ? cs.text : "нужен поток значений АЦП";
  }

  function renderFingerMap(n, map, roles) {
    const shown = S.n_channels || n;
    const key = shown + "|" + map.join() + "|" + JSON.stringify(roles);
    if (key === mapKey) return;
    mapKey = key;
    const opts = (sel) => META.fingers.map((f) => `<option value="${f}" ${f === sel ? "selected" : ""}>${fname(f)}</option>`).join("");
    $("#fingerMap").innerHTML = Array.from({ length: shown }, (_, i) =>
      `<div class="fm-row"><label for="finger-${i}"><span class="swatch" style="background:${CHANNEL_COLORS[i]}"></span>Канал S${i + 1}</label>
        <select id="finger-${i}" data-ch="${i}">${opts(map[i])}</select></div>`).join("");
  }

  // Имена и названия файлов приходят из пользовательских данных; всегда экранируем HTML.
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  let audioFiles = [], audioFolder = "", audioRefreshing = false;
  async function refreshAudioFiles() {
    if (audioRefreshing) return;
    audioRefreshing = true;
    try {
      const result = await api("/api/audio/files");
      audioFiles = result.files; audioFolder = result.folder;
      if (S) renderGestures();
    } catch (e) { toast(e.message, true); }
    finally { audioRefreshing = false; }
  }
  setInterval(refreshAudioFiles, 3000);

  function compatibilityReason(t, gs) {
    if (t.compatible) return "Готов к распознаванию";
    if (t.context.profile !== gs.profile) return "Подключите источник, с которым обучали жест";
    const reasons = [];
    if (t.context.channels.length !== S.n_channels) reasons.push("число каналов");
    else {
      if (t.context.channels.some((c, i) => c.finger !== S.finger_map[i])) reasons.push("соответствие пальцам");
      if (t.context.channels.some((c, i) => c.calibration.open !== S.channels[i].cal.open || c.calibration.fist !== S.channels[i].cal.fist)) reasons.push("калибровка");
    }
    if (t.context.hand !== S.hand) reasons.push("рука");
    if (JSON.stringify(t.context.pipeline) !== JSON.stringify(S.pipeline.config)) reasons.push("обработка");
    return reasons.length ? `Изменены настройки: ${reasons.join(", ")}. Обучите жест заново.`
      : "Изменился формат каналов. Обучите жест заново.";
  }

  function renderGestures() {
    const gs = S.gesture_system || {}, templates = gs.templates || [];
    $("#audioFolder").textContent = `Папка MP3: ${audioFolder || "audio рядом с FlexGlove.pyw"}`;
    const tr = gs.training, result = gs.training_result;
    $("#trainingStatus").textContent = tr ? `Обучение «${tr.name}»: ${Math.round(tr.progress * 100)} %. Не двигайтесь`
      : result ? `${result.message}${result.similar?.length ? " · Внимание: похожие эталоны: " + result.similar.join(", ") + ". Выберите более разные позы." : ""}` : "";
    $("#trainingStatus").classList.toggle("error-text", result?.status === "error" && !tr);
    $("#cancelTraining").hidden = !tr;
    $("#createGesture").disabled = !!tr;
    const key = JSON.stringify([templates, audioFiles, gs.profile]);
    if (key !== gestureKey && !$("#gestureList select:focus")) {
      gestureKey = key;
      $("#gestureList").innerHTML = templates.length ? templates.map(t => {
        const files = [...new Set([...audioFiles, ...(t.audio ? [t.audio] : [])])];
        return `<div class="learned-gesture" data-id="${esc(t.id)}">
          <button class="gesture-thumbnail-button" data-gesture-action="view" aria-label="Посмотреть жест ${esc(t.name)}"><img class="gesture-thumbnail" data-thumbnail="${esc(t.id)}" alt="3D-эталон: ${esc(t.name)}" hidden><span class="small">Готовится 3D-вид…</span></button>
          <strong>${esc(t.name)}</strong><span class="small">${esc(compatibilityReason(t, gs))}</span>
          <label>Аудиофраза <select data-audio="${esc(t.id)}"><option value="">Аудио не назначено</option>${files.map(f => `<option value="${esc(f)}" ${f === t.audio ? "selected" : ""}>${esc(f)}${audioFiles.includes(f) ? "" : " (файл отсутствует)"}</option>`).join("")}</select></label>
          <span class="small">${t.audio_available ? "MP3 доступен" : t.audio ? "Файл MP3 отсутствует в папке" : "Аудио не назначено"}</span>
          <div class="row"><button class="ghost small" data-gesture-action="rename">Переименовать</button>
          <button class="ghost small" data-gesture-action="retrain" ${t.context.profile !== gs.profile ? 'disabled title="Переключите источник на тот, с которым обучали жест"' : ""}>Обучить заново</button>
          <button class="ghost small" data-gesture-action="test">Проверить аудио</button>
          <button class="ghost small" data-gesture-action="delete">Удалить</button></div></div>`;
      }).join("") : `<p class="muted">Жестов пока нет. Покажите позу и нажмите «Создать жест».</p>`;
    }
    updateGestureDialog(gs);
    fillThumbnails(templates);
    const r = S.audio?.results?.[0];
    const status = {queued:"поставлено в очередь", playing:"воспроизводится", finished:"воспроизведение завершено", unavailable:"аудио не назначено/недоступно", error:"ошибка проигрывателя", cancelled:"отменено", duplicate:"уже в очереди или играет", full:"очередь заполнена", stopped:"остановлено"};
    $("#audioResult").hidden = !r;
    $("#audioResult").textContent = r ? `${status[r.status] || r.status}${r.message ? ": " + r.message : ""}` : "";
    $("#audioResult").classList.toggle("error-text", ["error", "unavailable"].includes(r?.status));
    const audio = S.audio || {};
    $("#audioStatus").hidden = !audio.active && !audio.queue?.length;
    $("#audioStatus").textContent = audio.active ? `Играет: ${audio.active.file}`
      : audio.queue?.length ? `В очереди: ${audio.queue.length}` : "";
  }
  let gesturePreview = null, gestureDialog = null, thumbnailBusy = false;
  const thumbnails = new Map();
  const thumbnailKey = t => JSON.stringify([t.context.hand, t.context.channels.map(c => c.finger), t.reference]);

  function applyThumbnails(templates) {
    for (const t of templates) {
      const image = $$("[data-thumbnail]").find(el => el.dataset.thumbnail === t.id);
      if (!image) continue;
      const url = thumbnails.get(thumbnailKey(t));
      image.hidden = !url;
      if (url && image.getAttribute("src") !== url) image.src = url;
      image.nextElementSibling.textContent = url ? "Посмотреть" : "3D-вид недоступен";
    }
  }
  async function fillThumbnails(templates) {
    applyThumbnails(templates);
    if (thumbnailBusy || !window.FlexHand3D) return;
    const missing = templates.filter(t => !thumbnails.has(thumbnailKey(t)));
    if (!missing.length) return;
    thumbnailBusy = true;
    const host = document.createElement("div");
    host.className = "thumbnail-render-host";
    document.body.append(host);
    let renderer;
    try {
      renderer = window.FlexHand3D.create(host, { animate: false });
      for (const t of missing) {
        const key = thumbnailKey(t);
        thumbnails.set(key, renderer ? await renderer.snapshot(t) : null);
      }
    } catch (error) {
      console.warn("Не удалось подготовить 3D-вид жеста:", error);
      for (const t of missing) if (!thumbnails.has(thumbnailKey(t))) thumbnails.set(thumbnailKey(t), null);
    } finally {
      renderer?.dispose(); host.remove(); thumbnailBusy = false;
      const current = S?.gesture_system?.templates || [];
      const keys = new Set(current.map(thumbnailKey));
      for (const key of thumbnails.keys()) if (!keys.has(key)) thumbnails.delete(key);
      applyThumbnails(current);
    }
  }

  function stopGesturePreview() {
    gesturePreview?.dispose(); gesturePreview = null;
  }
  function openGestureDialog(template = null, viewOnly = false) {
    gestureDialog = { id: template?.id, viewOnly, started: false, saved: viewOnly };
    $("#gestureDialog").hidden = false;
    $("#gestureDialogTitle").textContent = viewOnly ? "Сохранённый жест" : template ? "Обучить жест заново" : "Создать жест";
    $("#gestureName").value = template?.name || "";
    $("#gestureName").disabled = viewOnly;
    $("#gestureDialogStatus").textContent = "";
    $("#gesturePreview").hidden = viewOnly;
    $("#gestureSaved").hidden = !viewOnly;
    $("#gestureTrain").hidden = viewOnly;
    $("#gesturePreviewHint").textContent = viewOnly ? "Сохранённый эталон. Светлые пальцы — без сенсоров." : "Покажите позу и держите её неподвижно около секунды. Светлые пальцы — без сенсоров.";
    if (!viewOnly) {
      gesturePreview = window.FlexHand3D?.create($("#gesturePreview"));
      if (!gesturePreview) $("#gesturePreview").textContent = "3D-предпросмотр недоступен: требуется WebGL";
      if (S) render();
    } else updateGestureDialog(S.gesture_system);
    $("#gestureName").focus();
  }
  function updateGestureDialog(gs) {
    const dialog = gestureDialog;
    if (!dialog) return;
    const tr = gs.training, result = gs.training_result;
    if (dialog.started && tr) dialog.sawTraining = true;
    // WS мог пропустить всю секундную запись. После подтверждённого POST сверяем
    // терминальный снимок через REST, чтобы не принять результат предыдущей записи.
    if (dialog.started && dialog.startConfirmed && !dialog.sawTraining && !tr && result && !dialog.checking) {
      dialog.checking = true;
      api("/api/state").then(state => {
        if (gestureDialog === dialog) {
          dialog.sawTraining = true;
          updateGestureDialog(state.gesture_system);
        }
      }).catch(e => toast(e.message, true)).finally(() => { dialog.checking = false; });
    }
    if (dialog.started && dialog.sawTraining && !tr && result) {
      dialog.started = false;
      $("#gestureDialogStatus").textContent = result.message;
      if (result.status === "saved") { dialog.saved = true; dialog.id = result.id; stopGesturePreview(); }
    }
    if (dialog.saved) {
      const template = gs.templates.find(t => t.id === dialog.id);
      const url = template && thumbnails.get(thumbnailKey(template));
      $("#gesturePreview").hidden = true;
      $("#gestureSaved").hidden = !url;
      if (url) $("#gestureSaved").src = url;
      $("#gestureTrain").hidden = true;
      $("#gesturePreviewHint").textContent = "Сохранённый эталон из записи. Светлые пальцы — без сенсоров. Ракурс фиксирован и не используется при распознавании.";
    }
    $("#gestureTrain").disabled = !!tr || dialog.started;
    $("#gestureName").disabled = !!tr || dialog.started || dialog.saved;
    $("#gestureClose").textContent = dialog.started ? "Отменить обучение" : "Закрыть";
    if (dialog.started && tr) $("#gestureDialogStatus").textContent = `Запись: ${Math.round(tr.progress * 100)} %. Не двигайтесь`;
  }
  async function closeGestureDialog() {
    const dialog = gestureDialog;
    if (!dialog || dialog.closing) return;
    dialog.closing = true;
    await dialog.request;
    if (gestureDialog?.started) {
      try { await api("/api/gestures/cancel", {method:"POST"}); }
      catch (e) { dialog.closing = false; toast(e.message, true); return; }
    }
    stopGesturePreview(); gestureDialog = null;
    $("#gestureDialog").hidden = true;
    $("#createGesture").focus();
  }
  $("#createGesture").addEventListener("click", () => openGestureDialog());
  $("#gestureClose").addEventListener("click", closeGestureDialog);
  $("#gestureTrain").addEventListener("click", async () => {
    if (!gestureDialog || gestureDialog.started) return;
    const name = $("#gestureName").value.trim();
    if (!name) { $("#gestureName").focus(); return; }
    const dialog = gestureDialog;
    dialog.started = true;
    dialog.sawTraining = false;
    dialog.startConfirmed = false;
    $("#gestureTrain").disabled = true;
    dialog.request = api("/api/gestures/train", {method:"POST", body:{name, template_id:dialog.id}})
      .then(() => { dialog.startConfirmed = true; })
      .catch(e => { dialog.started = false; $("#gestureDialogStatus").textContent = e.message; $("#gestureTrain").disabled = false; });
    await dialog.request;
  });
  $("#cancelTraining").addEventListener("click", () => api("/api/gestures/cancel", {method:"POST"}).catch(e => toast(e.message, true)));
  $("#gestureDialog").addEventListener("keydown", e => {
    if (e.key === "Escape") { e.stopPropagation(); closeGestureDialog(); }
    if (e.key === "Tab") {
      const controls = $$("#gestureDialog button, #gestureDialog input").filter(el => !el.disabled && !el.hidden);
      const first = controls[0], last = controls[controls.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  $("#stopAudio").addEventListener("click", () => api("/api/audio/stop", {method:"POST"}).catch(e => toast(e.message, true)));
  $("#gestureList").addEventListener("click", async e => {
    const button = e.target.closest("button[data-gesture-action]");
    if (!button) return;
    const id = button.closest("[data-id]").dataset.id;
    const t = S.gesture_system.templates.find(t => t.id === id);
    try {
      if (button.dataset.gestureAction === "rename") {
        const name = prompt("Новое название", t.name);
        if (name?.trim()) await api(`/api/gestures/${id}`, {method:"PATCH", body:{name}});
      } else if (button.dataset.gestureAction === "retrain") {
        openGestureDialog(t);
      } else if (button.dataset.gestureAction === "view") {
        openGestureDialog(t, true);
      } else if (button.dataset.gestureAction === "delete") {
        if (confirm(`Удалить жест «${t.name}»?`)) await api(`/api/gestures/${id}`, {method:"DELETE"});
      } else {
        const r = await api(`/api/audio/test/${id}`, {method:"POST"});
        toast(r.status === "queued" ? "Аудио поставлено в очередь" : r.message, r.status !== "queued");
      }
    } catch(e) { toast(e.message, true); }
  });

  // ---------------- мастер калибровки ----------------
  // шаги: 1 «ладонь» (отсчёт 3-2-1 → замер 3 с) → 2 «кулак» (так же) → 3 итог по каналам → «Сохранить» / «Повторить»
  const WZ = { open: false, phase: "intro", countdown: 0, timer: null };
  const OPEN_POSE = { thumb: 0, index: 0, middle: 0, ring: 0, pinky: 0 };
  const FIST_POSE = { thumb: 1, index: 1, middle: 1, ring: 1, pinky: 1 };
  function openWizard() {
    if (!S || S.mode !== "values") return toast("Подключите источник значений АЦП для калибровки", true);
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
      : weak ? "Слабый сигнал. Согните палец сильнее и повторите замер."
      : "Все каналы в порядке.";
  }
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
  }
  function demoAngles(body) {
    api("/api/mock", { method: "POST", body }).catch(() => {});
  }
  const SLIDERS = { roll: "#rollSlider", pitch: "#pitchSlider", yaw: "#yawSlider" };
  const sliderTouched = {};                              // когда ползунок двигали в последний раз (мс)
  for (const [axis, sel] of Object.entries(SLIDERS)) {
    $(sel).addEventListener("input", (e) => {
      sliderTouched[axis] = performance.now();
      $(sel.replace("Slider", "Val")).textContent = deg(+e.target.value);
      demoAngles({ [axis]: +e.target.value });
    });
  }
  // галочки и ползунки демо показывают фактический режим из состояния (в т. ч. включённый через API, этап 4в)
  function syncDemo(demo) {
    if (!demo) return;
    $("#mockTour").checked = !!demo.tour;
    $("#mockAuto").checked = !!demo.auto;
    ["roll", "pitch", "yaw"].forEach((axis, i) => {
      const sl = $(SLIDERS[axis]), v = Math.round(demo.angles?.[i] ?? 0);
      if (performance.now() - (sliderTouched[axis] || 0) < 800 || +sl.value === v) return;   // не мешаем, пока двигают
      sl.value = v;
      $(SLIDERS[axis].replace("Slider", "Val")).textContent = deg(v);
    });
  }

  // ---------------- обработчики ----------------
  document.addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.matches("select[data-audio]")) {
        await api(`/api/gestures/${t.dataset.audio}`, {method:"PATCH", body:{audio:t.value || null}});
        t.blur();
      } else if (t.matches("select[data-ch]")) {
        const map = [...S.finger_map];
        map[+t.dataset.ch] = t.value;
        await api("/api/settings", { method: "POST", body: { finger_map: map } });
      } else if (t.id === "handSide") {
        await api("/api/settings", { method: "POST", body: { hand: t.value } });
      } else if (t.id === "imuMount") {
        await api("/api/settings", { method: "POST", body: { imu_mount: t.value } });
        toast("Крепление изменено. Держите руку неподвижно 2 с");
      } else if (t.id === "mockAuto") {
        await api("/api/mock", { method: "POST", body: { auto: t.checked } });
      } else if (t.id === "mockTour") {
        await api("/api/mock", { method: "POST", body: { tour: t.checked } });
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
  });

  $("#imuZero").addEventListener("click", () => api("/api/imu/zero", { method: "POST" }).then(() => toast("Поворот обнулён")).catch((e) => toast(e.message, true)));
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
      $("#serialPort").innerHTML = `<option value="">Не выбран</option>` +
        ports.map((p) => `<option value="${p.port}">${p.port}${p.description ? ": " + p.description : ""}</option>`).join("");
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

  // калибровка (сброс), запись, воспроизведение
  $("#calReset").addEventListener("click", () => api("/api/calibration", { method: "DELETE" }).then(() => toast("Калибровка сброшена")).catch((e) => toast(e.message, true)));
  $("#recBtn").addEventListener("click", async () => {
    try {
      if (S.recording.active) { const r = await api("/api/recording/stop", { method: "POST" }); toast(`Сохранено: data/recordings/${r.file}`); refreshRecordings(); }
      else await api("/api/recording/start", { method: "POST" });
    } catch (err) { toast(err.message, true); }
  });
  // Обработка: пресеты и свой JSON в закрытом разделе экрана «Сигналы».
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
    $("#pipeCurrent").textContent = `Действует: ${cur.title || "нет данных"}`;
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
    if (!confirm("Изменение обработки сделает сохранённые жесты несовместимыми. Потребуется переобучение. Применить обработку?")) {
      $("#pipePreset").value = S.pipeline.preset;
      showPipeHint(S.pipeline.preset);
      return;
    }
    try {
      const r = await api("/api/pipeline", { method: "POST", body: { preset, config } });
      $("#pipeJson").classList.remove("bad");
      toast(`Фильтрация: ${r.title}`); chart.clear();
    } catch (e) { $("#pipeJson").classList.add("bad"); $("#pipePreset").value = S.pipeline.preset; showPipeHint(S.pipeline.preset); toast(e.message, true); }
  }
  $("#pipePreset").addEventListener("change", (e) => {
    showPipeHint(e.target.value);
    if (e.target.value !== "custom" && e.target.value !== S.pipeline.preset) applyPipeline(e.target.value);
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
      if (r) toast(save ? `Запись по протоколу сохранена: data/recordings/${r.file}` : `Запись прервана: ${r.file}`, !save);
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
      $("#protoCmd").textContent = kind === "rest" ? "Покой: ладонь раскрыта" : kind === "bend" ? `Согнуть ${who} (${bendNo} из 5)` : "Разогнуть";
      $("#protoCmd").className = "proto-cmd" + (kind === "bend" ? " bend" : "");
      $("#protoLeft").textContent = Math.ceil(acc + dur - el);
      $("#protoBar").style.width = `${Math.round((el / total) * 100)}%`;
      if (i !== lastPhase && S.source.kind === "mock") {        // в демо пальцы сгибаются сами — проверка без перчатки
        const b = Array(S.n_channels).fill(0);
        if (kind === "bend") (ch === "all" ? b.fill(1) : (b[+ch] = 1));
        api("/api/mock", { method: "POST", body: { bends: b } }).catch(() => {});
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
      sel.innerHTML = `<option value="">Не выбрана</option>` + list.map((f) => `<option ${f.name === cur ? "selected" : ""}>${f.name}</option>`).join("");
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
    if (!S || S.source.kind !== "mock" || WZ.open || gestureDialog || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const k = parseInt(e.key, 10);
    if (k >= 1 && k <= S.n_channels) demoToggleFinger(S.finger_map[k - 1]);
    const arrows = { ArrowLeft: ["rollSlider", -15], ArrowRight: ["rollSlider", 15], ArrowUp: ["pitchSlider", 15], ArrowDown: ["pitchSlider", -15] };
    if (arrows[e.key] && !["#gestures", "#signals", "#settings"].includes(location.hash)) {
      e.preventDefault();
      const [id, step] = arrows[e.key], sl = $("#" + id);
      sl.value = Math.max(+sl.min, Math.min(+sl.max, +sl.value + step));
      sl.dispatchEvent(new Event("input"));
    }
  });

  window.addEventListener("pagehide", event => {
    if (!event.persisted) { stopGesturePreview(); hand3d?.dispose(); }
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
    await refreshAudioFiles();
    connectWS();
  })();
})();
