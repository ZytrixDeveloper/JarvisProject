/* Shared site code: Supabase client, nav login state, animations. */
const sb = window.supabase.createClient(JARVIS.SUPABASE_URL, JARVIS.SUPABASE_KEY);
const DOWNLOAD_URL = `https://github.com/${JARVIS.GITHUB_REPO}/releases/latest/download/Jarvis-Setup.exe`;

function $(sel, root = document) { return root.querySelector(sel); }
function $all(sel, root = document) { return [...root.querySelectorAll(sel)]; }

function showMsg(el, text, kind = "error") {
  el.textContent = text;
  el.className = `msg show ${kind}`;
}

function displayName(user) {
  const meta = user?.user_metadata || {};
  return meta.full_name || meta.name || (user?.email || "").split("@")[0];
}

/* nav: login state + mobile menu */
async function initNav() {
  const nav = $(".nav");
  if (!nav) return;
  $(".menu-btn", nav)?.addEventListener("click", () => nav.classList.toggle("open"));
  $all(".nav-links a", nav).forEach((a) => a.addEventListener("click", () => nav.classList.remove("open")));
  const slot = $("#navAuth");
  if (!slot) return;
  const { data } = await sb.auth.getSession();
  const user = data.session?.user;
  slot.innerHTML = user
    ? `<a class="btn small" href="account.html">${escapeHtml(displayName(user))}</a>`
    : `<a class="btn small" href="login.html">Sign in</a><a class="btn small primary" href="login.html?mode=signup">Get Jarvis</a>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* social sign-in: only providers switched on in Supabase are shown */
const PROVIDERS = {
  google: {
    label: "Continue with Google",
    icon: '<svg viewBox="0 0 24 24" style="stroke:none"><path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5a5.6 5.6 0 0 1-2.4 3.6v3h3.9c2.2-2.1 3.5-5.1 3.5-8.8Z"/><path fill="#34A853" d="M12 24c3.2 0 6-1.1 8-2.9l-3.9-3c-1.1.7-2.5 1.2-4.1 1.2-3.1 0-5.8-2.1-6.7-5H1.3v3.1A12 12 0 0 0 12 24Z"/><path fill="#FBBC05" d="M5.3 14.3a7.2 7.2 0 0 1 0-4.6V6.6h-4a12 12 0 0 0 0 10.8l4-3.1Z"/><path fill="#EA4335" d="M12 4.8c1.8 0 3.3.6 4.6 1.8l3.4-3.4A12 12 0 0 0 1.3 6.6l4 3.1c1-2.9 3.6-4.9 6.7-4.9Z"/></svg>',
  },
  apple: {
    label: "Continue with Apple",
    icon: '<svg viewBox="0 0 24 24" style="stroke:none;fill:currentColor"><path d="M16.4 12.6c0-2.5 2-3.7 2.1-3.8-1.2-1.7-3-1.9-3.6-2-1.5-.2-3 .9-3.8.9-.8 0-2-.9-3.3-.9-1.7 0-3.3 1-4.2 2.5-1.8 3.1-.5 7.7 1.3 10.2.9 1.2 1.9 2.6 3.2 2.6 1.3-.1 1.8-.8 3.3-.8s2 .8 3.3.8c1.4 0 2.3-1.3 3.1-2.5 1-1.4 1.4-2.8 1.4-2.9 0 0-2.8-1.1-2.8-4.1ZM14 5.2c.7-.9 1.2-2 1-3.2-1 0-2.2.7-3 1.5-.6.7-1.2 1.9-1 3.1 1.1.1 2.3-.6 3-1.4Z"/></svg>',
  },
};

async function renderOAuthButtons(container, onPick) {
  if (!container) return;
  try {
    const r = await fetch(`${JARVIS.SUPABASE_URL}/auth/v1/settings`, { headers: { apikey: JARVIS.SUPABASE_KEY } });
    const settings = await r.json();
    const enabled = Object.keys(PROVIDERS).filter((p) => settings.external?.[p]);
    if (!enabled.length) return;
    container.innerHTML = "";
    for (const p of enabled) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn oauth-btn";
      btn.innerHTML = `${PROVIDERS[p].icon}<span>${PROVIDERS[p].label}</span>`;
      btn.addEventListener("click", () => { btn.disabled = true; onPick(p).finally?.(() => (btn.disabled = false)); });
      container.append(btn);
    }
    container.hidden = false;
    container.nextElementSibling?.removeAttribute("hidden");
  } catch (e) {
    console.warn("Could not load sign-in providers", e);
  }
}

/* 3D tilt on hover */
function initTilt() {
  if (!matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  for (const card of $all("[data-tilt]")) {
    card.addEventListener("mousemove", (e) => {
      const r = card.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - 0.5;
      const y = (e.clientY - r.top) / r.height - 0.5;
      card.style.transform = `perspective(900px) rotateX(${(-y * 10).toFixed(2)}deg) rotateY(${(x * 12).toFixed(2)}deg) translateZ(6px)`;
      card.style.setProperty("--mx", `${(x + 0.5) * 100}%`);
      card.style.setProperty("--my", `${(y + 0.5) * 100}%`);
    });
    card.addEventListener("mouseleave", () => { card.style.transform = ""; });
  }
}

/* live stats (online now, users, countries) from the public jarvis_stats() function */
async function loadStats() {
  try {
    const r = await fetch(`${JARVIS.SUPABASE_URL}/rest/v1/rpc/jarvis_stats`, {
      method: "POST", headers: { apikey: JARVIS.SUPABASE_KEY, "Content-Type": "application/json" }, body: "{}",
    });
    if (!r.ok) return null;
    const s = await r.json();
    for (const el of $all("[data-stat]")) {
      const v = s[el.dataset.stat];
      if (v === undefined) continue;
      const from = parseInt(el.textContent, 10) || 0;
      animateCount(el, from, v);
    }
    return s;
  } catch {
    return null;
  }
}

function animateCount(el, from, to) {
  const start = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - start) / 900);
    el.textContent = Math.round(from + (to - from) * (1 - (1 - k) ** 3));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* scroll reveal */
function initReveal() {
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { threshold: 0.12 });
  $all(".reveal").forEach((el) => io.observe(el));
}

/* hero arc reactor */
function initReactor(canvas) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  let t = 0;
  const fit = () => {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  fit();
  window.addEventListener("resize", fit);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const c = (a) => `rgba(41,211,255,${a})`;

  function frame() {
    const w = canvas.clientWidth, h = canvas.clientHeight, cx = w / 2, cy = h / 2, r = Math.min(w, h) / 2 - 8;
    t += reduce ? 0 : 0.008;
    const pulse = 0.5 + Math.sin(t * 4) * 0.5;
    ctx.clearRect(0, 0, w, h);
    ctx.lineCap = "round";

    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.5);
    for (let i = 0; i < 120; i++) {
      const a = (i / 120) * Math.PI * 2, long = i % 10 === 0;
      ctx.strokeStyle = c(long ? 0.7 : 0.2);
      ctx.lineWidth = long ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * r, Math.sin(a) * r);
      ctx.lineTo(Math.cos(a) * (r - (long ? 14 : 7)), Math.sin(a) * (r - (long ? 14 : 7)));
      ctx.stroke();
    }
    ctx.restore();

    for (const [k, segs, speed, width] of [[0.86, 3, 1.1, 5], [0.76, 4, -0.8, 3], [0.68, 6, 1.5, 2]]) {
      for (let i = 0; i < segs; i++) {
        const s = t * speed + (i / segs) * Math.PI * 2;
        ctx.strokeStyle = c(0.6);
        ctx.lineWidth = width;
        ctx.shadowColor = c(0.9);
        ctx.shadowBlur = 12;
        ctx.beginPath();
        ctx.arc(cx, cy, r * k, s, s + (Math.PI * 2 / segs) * 0.62);
        ctx.stroke();
      }
    }
    ctx.shadowBlur = 0;

    for (let i = 0; i < 10; i++) {
      const a0 = (i / 10) * Math.PI * 2 - t * 0.3, a1 = a0 + (Math.PI * 2 / 10) * 0.7;
      ctx.beginPath();
      ctx.arc(cx, cy, r * 0.5, a0, a1);
      ctx.arc(cx, cy, r * 0.38, a1, a0, true);
      ctx.closePath();
      ctx.fillStyle = c(0.12 + pulse * 0.12);
      ctx.strokeStyle = c(0.65);
      ctx.lineWidth = 1.2;
      ctx.fill();
      ctx.stroke();
    }

    const core = r * (0.25 + pulse * 0.03);
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, core * 1.9);
    g.addColorStop(0, "rgba(235,252,255,1)");
    g.addColorStop(0.35, c(0.95));
    g.addColorStop(0.7, c(0.22));
    g.addColorStop(1, c(0));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, core * 1.9, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = c(0.9);
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, core, 0, Math.PI * 2);
    ctx.stroke();
    requestAnimationFrame(frame);
  }
  frame();
}

/* animated demo conversation */
const DEMO = [
  ["you", "Hey Jarvis, what's the weather like?"],
  ["jarvis", "Bratislava is overcast at ten degrees, sir. I'd take a jacket.", ["get_weather"]],
  ["you", "Open Spotify and play something chill."],
  ["jarvis", "Spotify is open and lo-fi beats are playing.", ["open_app · spotify", "media_control · play"]],
  ["you", "What's this error on my screen?"],
  ["jarvis", "Python can't find the module named requests. Running pip install requests should fix it.", ["look_at_screen"]],
  ["you", "Delete the old screenshots folder."],
  ["jarvis", "That moves Pictures/Old Screenshots to the Recycle Bin. Shall I proceed, sir?", ["confirmation required"]],
];

function initDemo(body) {
  if (!body) return;
  let i = 0;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function play() {
    while (true) {
      body.innerHTML = "";
      for (i = 0; i < DEMO.length; i++) {
        const [who, text, tools] = DEMO[i];
        const line = document.createElement("div");
        line.className = `term-line ${who}`;
        line.innerHTML = `<div class="who">${who === "you" ? "YOU" : "JARVIS"}</div><div class="say"></div>`;
        body.append(line);
        const msg = line.lastChild;
        if (tools) for (const t of tools) {
          const chip = document.createElement("span");
          chip.className = "tool";
          chip.textContent = t;
          msg.append(chip);
          await sleep(350);
        }
        if (tools) msg.append(document.createElement("br"));
        const span = document.createElement("span");
        msg.append(span);
        const caret = document.createElement("span");
        caret.className = "caret";
        msg.append(caret);
        for (const ch of text) { span.textContent += ch; await sleep(who === "you" ? 28 : 18); }
        caret.remove();
        await sleep(who === "you" ? 500 : 1300);
      }
      await sleep(3500);
    }
  }
  play();
}

document.addEventListener("DOMContentLoaded", () => {
  initNav();
  initReveal();
  initTilt();
  initReactor($("#reactor"));
  initDemo($("#demo"));
  $all("[data-download]").forEach((a) => (a.href = DOWNLOAD_URL));
  $all("[data-version]").forEach((el) => (el.textContent = JARVIS.VERSION));
  // Show the newest published version automatically.
  fetch(`https://api.github.com/repos/${JARVIS.GITHUB_REPO}/releases/latest`)
    .then((r) => (r.ok ? r.json() : null))
    .then((rel) => rel?.tag_name && $all("[data-version]").forEach((el) => (el.textContent = rel.tag_name.replace(/^v/, ""))))
    .catch(() => {});
  const y = $("#year");
  if (y) y.textContent = new Date().getFullYear();
});
