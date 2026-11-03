/* J.A.R.V.I.S. HUD: talks to Python through window.pywebview.api and receives events via hud.on(). */
(() => {
  const $ = (id) => document.getElementById(id);
  let api = null;
  let user = null;
  let accountRequired = false;

  const STATE_LABEL = {
    idle: "STANDBY", listening: "LISTENING", transcribing: "PROCESSING", thinking: "THINKING",
    speaking: "SPEAKING", scanning: "SCANNING", confirm: "AWAITING ORDERS", boot: "INITIALISING",
  };
  const TOOL_LABEL = {
    open_app: "Open app", close_app: "Close app", list_open_windows: "List windows",
    open_website: "Open website", google_search: "Google search", search_web: "Web search",
    play_youtube: "YouTube", system_status: "System status", get_weather: "Weather",
    volume: "Volume", media_control: "Media", set_brightness: "Brightness", power: "Power",
    set_timer: "Timer", type_text: "Type text", press_keys: "Keys", find_files: "Find files",
    open_path: "Open", read_file: "Read file", write_file: "Write file", create_folder: "New folder",
    move_file: "Move", delete_file: "Delete", run_command: "Command",
    look_at_screen: "Screen scan", look_through_camera: "Camera scan", take_screenshot: "Screenshot",
    get_news: "News Hub", read_clipboard: "Clipboard", remember: "Memory", forget: "Memory",
    set_reminder: "Reminder", design_3d: "3D Lab", build_project: "Code builder",
    now_playing: "Now playing", call_status: "Calls",
  };

  /* ------------------------------------------------------------------ reactor animation */
  const canvas = $("reactor");
  const ctx = canvas.getContext("2d");
  const R = { state: "boot", level: 0, target: 0, t: 0, speech: null, micHistory: new Array(64).fill(0) };

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", resize);

  function color(alpha) {
    const c = R.state === "confirm" ? "255,181,71" : R.state === "scanning" ? "61,255,168" : "41,211,255";
    return `rgba(${c},${alpha})`;
  }

  function draw() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 6;
    const busy = ["thinking", "transcribing", "scanning", "boot"].includes(R.state);
    R.t += busy ? 0.028 : R.state === "listening" ? 0.018 : 0.006;

    if (R.speech) {
      const idx = Math.floor((performance.now() - R.speech.start) / 50);
      R.target = idx < R.speech.env.length ? R.speech.env[idx] : 0;
      if (idx >= R.speech.env.length + 10) R.speech = null;
    } else if (R.state !== "listening") {
      R.target = 0.12 + Math.sin(R.t * 3) * 0.05;
    }
    R.level += (R.target - R.level) * 0.25;

    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = "round";

    // outer tick ring
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(R.t * 0.4);
    for (let i = 0; i < 120; i++) {
      const a = (i / 120) * Math.PI * 2;
      const long = i % 10 === 0;
      ctx.strokeStyle = color(long ? 0.7 : 0.22);
      ctx.lineWidth = long ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * r, Math.sin(a) * r);
      ctx.lineTo(Math.cos(a) * (r - (long ? 12 : 6)), Math.sin(a) * (r - (long ? 12 : 6)));
      ctx.stroke();
    }
    ctx.restore();

    // segmented arcs, counter-rotating
    const arcs = [[0.86, 3, 1.1, 5], [0.76, 4, -0.8, 3], [0.68, 6, 1.6, 2]];
    for (const [k, segs, speed, width] of arcs) {
      const rr = r * k;
      for (let i = 0; i < segs; i++) {
        const start = R.t * speed + (i / segs) * Math.PI * 2;
        ctx.strokeStyle = color(0.55 + (busy ? 0.25 : 0));
        ctx.lineWidth = width;
        ctx.shadowColor = color(0.9);
        ctx.shadowBlur = 10;
        ctx.beginPath();
        ctx.arc(cx, cy, rr, start, start + (Math.PI * 2) / segs * 0.62);
        ctx.stroke();
      }
    }
    ctx.shadowBlur = 0;

    // listening waveform ring
    if (R.state === "listening") {
      const rr = r * 0.56;
      ctx.beginPath();
      R.micHistory.forEach((v, i) => {
        const a = (i / R.micHistory.length) * Math.PI * 2 - Math.PI / 2;
        const d = rr + v * r * 0.22;
        const x = cx + Math.cos(a) * d, y = cy + Math.sin(a) * d;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.closePath();
      ctx.strokeStyle = color(0.9);
      ctx.lineWidth = 2;
      ctx.shadowColor = color(1);
      ctx.shadowBlur = 14;
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // reactor coils
    const coils = 10, inner = r * 0.38, outer = r * 0.5;
    for (let i = 0; i < coils; i++) {
      const a0 = (i / coils) * Math.PI * 2 - R.t * 0.3;
      const a1 = a0 + (Math.PI * 2) / coils * 0.7;
      ctx.beginPath();
      ctx.arc(cx, cy, outer, a0, a1);
      ctx.arc(cx, cy, inner, a1, a0, true);
      ctx.closePath();
      ctx.fillStyle = color(0.12 + R.level * 0.35);
      ctx.strokeStyle = color(0.65);
      ctx.lineWidth = 1.2;
      ctx.fill();
      ctx.stroke();
    }

    // glowing core
    const core = r * (0.24 + R.level * 0.09);
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, core * 1.9);
    g.addColorStop(0, "rgba(235,252,255,1)");
    g.addColorStop(0.35, color(0.95));
    g.addColorStop(0.7, color(0.25));
    g.addColorStop(1, color(0));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, core * 1.9, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = color(0.9);
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, core, 0, Math.PI * 2);
    ctx.stroke();

    requestAnimationFrame(draw);
  }

  /* ------------------------------------------------------------------ helpers */
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 3200);
  }

  function setState(state) {
    R.state = state;
    document.body.className = state;
    $("reactorLabel").textContent = STATE_LABEL[state] || state.toUpperCase();
    const chip = $("statusChip");
    chip.classList.toggle("warn", state === "confirm");
    $("statusText").textContent = state === "idle" ? "ONLINE" : (STATE_LABEL[state] || state.toUpperCase());
  }

  function typeOut(el, text) {
    clearInterval(el._typing);
    el.textContent = "";
    let i = 0;
    el._typing = setInterval(() => {
      el.textContent = text.slice(0, ++i);
      if (i >= text.length) clearInterval(el._typing);
    }, 14);
  }

  function timeNow() {
    return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function argSummary(args) {
    const vals = Object.values(args || {}).map((v) => String(v)).filter(Boolean);
    const s = vals.join(" · ");
    return s.length > 70 ? s.slice(0, 70) + "..." : s;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  /* ------------------------------------------------------------------ event handlers from Python */
  const pendingTools = [];

  const handlers = {
    boot({ key, label, status, detail }) {
      let li = document.querySelector(`#bootList li[data-key="${key}"]`);
      if (!li) {
        li = el("li");
        li.dataset.key = key;
        li.append(el("i", "ico"), el("div"));
        $("bootList").append(li);
      }
      li.firstChild.className = `ico ${status}`;
      const body = li.lastChild;
      body.textContent = label;
      if (detail) body.append(el("small", "", detail));
    },
    ready() { if (R.state === "boot") setState("idle"); },
    state({ state }) { setState(state); },
    level({ v }) {
      R.target = Math.min(1, v * 2.2);
      R.micHistory.push(Math.min(1, v * 2.5));
      R.micHistory.shift();
    },
    heard({ text }) {
      $("heard").textContent = text;
      $("reply").textContent = "";
    },
    reply_delta({ text }) {
      const el = $("reply");
      if (!el._streaming) { clearInterval(el._typing); el.textContent = ""; el._streaming = true; }
      el.textContent += text;
    },
    reply({ text }) {
      const el = $("reply");
      if (el._streaming) { el.textContent = text; el._streaming = false; } else typeOut(el, text);
    },
    camera_frame({ img, error }) {
      const feed = $("feed");
      if (error) { toast(error); setLiveCam(false); return; }
      let image = feed.querySelector("img.cam");
      if (!image) { feed.innerHTML = ""; image = new Image(); image.className = "cam"; feed.append(image); }
      image.src = `data:image/jpeg;base64,${img}`;
      $("feedKind").textContent = "LIVE";
    },
    speak({ env }) { R.speech = { env, start: performance.now() }; },
    speak_end() { R.speech = null; },
    notice({ text }) { toast(text); },
    mic({ on }) { toast(on ? "Microphone on" : "Microphone muted"); },
    tool({ name, args, state, result }) {
      const list = $("activity");
      list.querySelector(".empty")?.remove();
      if (state === "run") {
        const li = el("li");
        li.append(el("i", "ico run"));
        const body = el("div");
        body.append(el("b", "", TOOL_LABEL[name] || name), el("small", "", argSummary(args)));
        li.append(body, el("time", "", timeNow()));
        list.prepend(li);
        pendingTools.push(li);
        while (list.children.length > 60) list.lastChild.remove();
      } else {
        const li = pendingTools.shift();
        if (!li) return;
        li.firstChild.className = `ico ${state}`;
        if (result) li.title = result;
        if (state === "fail") li.querySelector("small").textContent = result;
      }
    },
    capture({ kind, img }) {
      const feed = $("feed");
      feed.innerHTML = "";
      const image = new Image();
      image.src = `data:image/jpeg;base64,${img}`;
      feed.append(image);
      feed.classList.add("scanning");
      $("feedKind").textContent = kind === "camera" ? "CAMERA" : "SCREEN";
      setTimeout(() => feed.classList.remove("scanning"), 4000);
    },
    confirm({ id, text, timeout }) {
      $("confirmText").textContent = text;
      $("confirmOverlay").hidden = false;
      $("confirmOverlay").dataset.id = id;
      const bar = $("confirmBar");
      bar.style.transition = "none";
      bar.style.width = "100%";
      requestAnimationFrame(() => {
        bar.style.transition = `width ${timeout}s linear`;
        bar.style.width = "0%";
      });
    },
    confirm_done({ yes }) {
      $("confirmOverlay").hidden = true;
      toast(yes ? "Confirmed" : "Cancelled");
    },
    stats(s) {
      gauge("cpu", s.cpu, `${s.cpu}%`);
      gauge("ram", s.ram, `${s.ram}%`);
      gauge("gpu", s.gpu ?? 0, s.gpu == null ? "--" : `${s.gpu}%`);
      gauge("battery", s.battery ?? 100, s.battery == null ? "AC" : `${s.battery}%`, s.battery != null && s.battery < 20 && !s.plugged);
      $("vram").textContent = s.vram || "--";
      $("disk").textContent = `${s.disk}% used`;
      $("uptime").textContent = s.uptime;
    },
    update({ version, current, notes }) {
      $("updVersion").textContent = version;
      $("updCurrent").textContent = current;
      $("updNotes").textContent = notes || "Improvements and fixes.";
      $("updateChip").hidden = false;
      $("updateOverlay").hidden = false;
    },
    update_progress({ pct, installing, error }) {
      $("updBarWrap").hidden = false;
      if (error) {
        $("updStatus").textContent = error;
        $("btnUpdate").disabled = false;
        return;
      }
      $("updBar").style.width = `${pct}%`;
      $("updStatus").textContent = installing ? "Installing... Jarvis will restart in a moment." : `Downloading update... ${pct}%`;
    },
    now_playing(info) {
      np = info.none ? null : { ...info, at: performance.now() };
      const card = $("npCard");
      card.classList.toggle("playing", !!(np && np.playing));
      $("npTitle").textContent = np ? np.title : "Nothing playing";
      $("npArtist").textContent = np ? (np.artist || np.album || "") : "Play music in Spotify, YouTube...";
      $("npApp").textContent = np ? np.app : "";
      if (np && np.art) $("npArt").style.backgroundImage = `url("${np.art}")`;
      if (!np) $("npArt").style.backgroundImage = "";
      $("npIcon").innerHTML = np && np.playing ? '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>' : '<path d="m8 5 11 7-11 7Z"/>';
    },
    call(info) {
      $("callChip").hidden = !info.in_call;
      if (info.in_call) $("callText").textContent = `IN CALL · ${info.app}`.toUpperCase();
    },
    weather({ text }) {
      const short = text.replace(/\. Today.*$/, "").replace(/, humidity.*$/, "");
      $("weather").textContent = short;
      $("weather").title = text;
    },
    news({ topic, items }) {
      const list = $("news");
      list.innerHTML = "";
      list.previousElementSibling.firstChild.textContent = topic ? `News: ${topic} ` : "Live news ";
      items.forEach((n, i) => {
        const li = el("li");
        li.style.animationDelay = `${i * 60}ms`;
        const a = el("a", "", n.title);
        a.addEventListener("click", () => api?.open_url(n.url));
        li.append(a, el("small", "", `${n.source} · ${n.age}`));
        list.append(li);
      });
    },
    account({ user: u, required }) {
      user = u;
      accountRequired = required;
      renderAccount();
    },
  };

  function gauge(key, pct, label, hot) {
    const g = document.querySelector(`.gauge[data-key="${key}"]`);
    g.querySelector(".value").style.strokeDashoffset = 201 - (201 * Math.max(0, Math.min(100, pct))) / 100;
    g.querySelector("b").textContent = label;
    g.classList.toggle("hot", hot ?? pct >= 85);
  }

  function renderAccount() {
    const chip = $("userChip");
    chip.hidden = !user;
    if (user) chip.textContent = user.name || user.email;
    $("btnSignOut").hidden = !user;
    $("loginOverlay").hidden = !(accountRequired && !user);
  }

  window.hud = {
    on(evt) {
      const h = handlers[evt.type];
      if (h) {
        try { h(evt); } catch (e) { console.error(evt.type, e); }
      }
    },
  };

  /* ------------------------------------------------------------------ settings drawer */
  function fillSettings(s, voices) {
    const form = $("settingsForm");
    const sel = $("voiceSelect");
    sel.innerHTML = "";
    for (const [id, label] of voices) sel.append(new Option(label, id));
    for (const [k, v] of Object.entries(s)) {
      const input = form.elements[k];
      if (!input) continue;
      if (input.type === "checkbox") input.checked = !!v;
      else input.value = Array.isArray(v) ? v.join(", ") : v;
    }
    $("sensOut").textContent = form.elements.wake_sensitivity.value;
  }

  let np = null;  // now playing; the progress bar moves locally between updates
  setInterval(() => {
    if (!np || !np.duration) { $("npBar").style.width = "0"; return; }
    const pos = np.position + (np.playing ? (performance.now() - np.at) / 1000 : 0);
    $("npBar").style.width = `${Math.min(100, (pos / np.duration) * 100)}%`;
  }, 500);

  let liveCam = false;
  function setLiveCam(on) {
    liveCam = on;
    $("btnLiveCam").classList.toggle("on", on);
    $("feed").classList.toggle("live", on);
    if (!on) { $("feed").querySelector("img.cam")?.remove(); $("feedKind").textContent = ""; }
    api?.camera_live(on);
  }

  async function fillCameras(selected) {
    const names = (await api.list_cameras()) || [];
    const sel = $("camSelect");
    sel.innerHTML = "";
    (names.length ? names : ["Default camera"]).forEach((n, i) => sel.append(new Option(n, String(i))));
    sel.value = String(selected || 0);
  }

  function openSettings(open) {
    $("settings").classList.toggle("open", open);
    $("settings").setAttribute("aria-hidden", String(!open));
  }

  /* ------------------------------------------------------------------ wiring */
  function send(text) {
    text = text.trim();
    if (!text || !api) return;
    api.send_text(text);
    $("cmdInput").value = "";
  }

  function bindUi() {
    $("cmdForm").addEventListener("submit", (e) => { e.preventDefault(); send($("cmdInput").value); });
    $("hints").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") send(e.target.textContent); });
    $("btnMic").addEventListener("click", () => api?.mic());
    $("btnYes").addEventListener("click", () => api?.confirm_answer($("confirmOverlay").dataset.id, true));
    $("btnNo").addEventListener("click", () => api?.confirm_answer($("confirmOverlay").dataset.id, false));
    $("btnLink").addEventListener("click", () => {
      api?.link_account();
      $("linkStatus").textContent = "Browser opened. Finish signing in there...";
    });
    $("btnMin").addEventListener("click", () => api?.minimize());
    $("btnMax").addEventListener("click", () => api?.toggle_maximize());
    // Double-click the title bar: fullscreen <-> window. The drag region swallows "dblclick",
    // so detect two quick presses instead.
    let lastDown = 0;
    window.addEventListener("mousedown", (e) => {  // capture phase: runs before the window-drag code
      if (!e.target.closest(".titlebar") || e.target.closest("button")) return;
      const now = Date.now();
      if (now - lastDown < 400) { lastDown = 0; api?.toggle_maximize(); } else lastDown = now;
    }, true);
    $("btnClose").addEventListener("click", () => api?.close());
    $("btnSettings").addEventListener("click", () => openSettings(!$("settings").classList.contains("open")));
    $("btnCloseSettings").addEventListener("click", () => openSettings(false));
    $("btnClearLog").addEventListener("click", () => { $("activity").innerHTML = '<li class="empty">No actions yet</li>'; });
    $("btnNews").addEventListener("click", () => api?.refresh_news(""));
    $("btnHub").addEventListener("click", () => api?.open_news_hub());
    $("btnUpdate").addEventListener("click", () => { $("btnUpdate").disabled = true; api?.install_update(); });
    $("btnUpdLater").addEventListener("click", () => { $("updateOverlay").hidden = true; });
    $("updateChip").addEventListener("click", () => { $("updateOverlay").hidden = false; });
    $("btnCheckUpdates").addEventListener("click", () => { api?.check_updates(); toast("Checking for updates..."); });
    $("btnLiveCam").addEventListener("click", () => setLiveCam(!liveCam));
    document.querySelectorAll("[data-media]").forEach((b) => b.addEventListener("click", () => api?.media(b.dataset.media)));
    $("btnLab").addEventListener("click", () => { api?.open_lab(); openSettings(false); });
    $("btnUninstall").addEventListener("click", () => { openSettings(false); $("unStatus").textContent = ""; $("uninstallOverlay").hidden = false; });
    $("btnUnNo").addEventListener("click", () => { $("uninstallOverlay").hidden = true; });
    $("btnUnYes").addEventListener("click", async () => {
      $("btnUnYes").disabled = true;
      $("unStatus").textContent = "Uninstalling... Jarvis will close.";
      const res = await api.uninstall($("unModels").checked);
      if (res !== "ok") { $("unStatus").textContent = res; $("btnUnYes").disabled = false; }
    });
    $("btnNewChat").addEventListener("click", () => { api?.new_conversation(); toast("Started a new conversation"); });
    $("btnDataFolder").addEventListener("click", () => api?.open_data_folder());
    $("btnSignOut").addEventListener("click", () => { api?.sign_out(); openSettings(false); });
    $("btnQuit").addEventListener("click", () => api?.quit());
    $("userChip").addEventListener("click", () => openSettings(true));
    $("settingsForm").elements.wake_sensitivity.addEventListener("input", (e) => { $("sensOut").textContent = e.target.value; });
    $("settingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const values = {};
      for (const input of form.elements) {
        if (!input.name) continue;
        values[input.name] = input.type === "checkbox" ? input.checked
          : input.type === "range" ? parseFloat(input.value) : input.value;
      }
      await api.save_settings(values);
      toast("Settings saved");
      openSettings(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        openSettings(false);
        api?.stop_speaking();
      }
    });
  }

  function tickClock() {
    const now = new Date();
    $("clock").textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    $("date").textContent = now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  }

  async function init() {
    api = window.pywebview.api;
    const info = await api.ready();
    $("ver").textContent = `v${info.version}`;
    user = info.user;
    accountRequired = info.accountConfigured;
    const langSel = $("langSelect");
    for (const [code, label] of info.languages) langSel.append(new Option(label, code));
    fillSettings(info.settings, info.voices);
    fillCameras(info.settings.camera_index);
    $("hotkeyNote").textContent = `Push-to-talk hotkey: ${info.hotkey.toUpperCase()}. Esc stops Jarvis talking.`;
    $("btnMic").title = `Listen now (${info.hotkey.toUpperCase()})`;
  }

  bindUi();
  resize();
  setState("boot");
  tickClock();
  setInterval(tickClock, 1000);
  requestAnimationFrame(draw);
  if (window.pywebview?.api) init();
  else window.addEventListener("pywebviewready", init);
})();
