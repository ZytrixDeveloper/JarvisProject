// 3D Earth with a glowing marker per country where Jarvis is used. Online countries pulse.
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js";

const TEX = "https://cdn.jsdelivr.net/npm/three-globe@2.31.0/example/img/earth-night.jpg";
const R = 2;

function toXYZ(lat, lon, r = R) {
  const phi = THREE.MathUtils.degToRad(90 - lat), theta = THREE.MathUtils.degToRad(lon + 180);
  return new THREE.Vector3(-r * Math.sin(phi) * Math.cos(theta), r * Math.cos(phi), r * Math.sin(phi) * Math.sin(theta));
}

function dotTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d"), grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)"); grad.addColorStop(0.3, "rgba(41,211,255,.9)"); grad.addColorStop(1, "rgba(41,211,255,0)");
  g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

export function initEarth(canvas) {
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true }); } catch { return null; }
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  camera.position.set(0, 0, 7.4);
  const earth = new THREE.Group();
  scene.add(earth);
  earth.rotation.y = -1.2;  // start with Europe facing the viewer
  earth.rotation.x = 0.35;

  const globe = new THREE.Mesh(new THREE.SphereGeometry(R, 64, 48),
    new THREE.MeshBasicMaterial({ color: 0xc8f0ff }));
  new THREE.TextureLoader().load(TEX, (t) => { t.colorSpace = THREE.SRGBColorSpace; globe.material.map = t; globe.material.needsUpdate = true; });
  earth.add(globe);
  earth.add(new THREE.Mesh(new THREE.SphereGeometry(R * 1.002, 36, 24),
    new THREE.MeshBasicMaterial({ color: 0x29d3ff, wireframe: true, transparent: true, opacity: 0.07 })));
  // soft atmosphere: a radial-gradient sprite behind the globe
  const halo = document.createElement("canvas");
  halo.width = halo.height = 256;
  const hg = halo.getContext("2d"), grad = hg.createRadialGradient(128, 128, 88, 128, 128, 128);
  grad.addColorStop(0, "rgba(41,211,255,.45)"); grad.addColorStop(0.35, "rgba(41,211,255,.14)"); grad.addColorStop(1, "rgba(41,211,255,0)");
  hg.fillStyle = grad; hg.fillRect(0, 0, 256, 256);
  const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(halo), depthWrite: false,
    blending: THREE.AdditiveBlending, transparent: true }));
  glow.scale.setScalar(R * 2 * 1.45);
  glow.renderOrder = -1;
  scene.add(glow);

  const markers = new THREE.Group();
  earth.add(markers);
  const dot = dotTexture();
  let pulses = [];

  function setPlaces(places) {
    markers.clear();
    pulses = [];
    for (const p of places || []) {
      if (!Number.isFinite(+p.lat) || !Number.isFinite(+p.lon)) continue;
      const pos = toXYZ(+p.lat, +p.lon, R * 1.01);
      const size = 0.12 + Math.min(0.25, Math.log2(1 + p.users) * 0.05);
      const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: dot, color: p.online ? 0x3dffa8 : 0x29d3ff,
        blending: THREE.AdditiveBlending, depthWrite: false, transparent: true }));
      s.position.copy(pos); s.scale.setScalar(size);
      markers.add(s);
      const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.006, 0.006, 0.35, 6),
        new THREE.MeshBasicMaterial({ color: p.online ? 0x3dffa8 : 0x29d3ff, transparent: true, opacity: 0.7 }));
      beam.position.copy(toXYZ(+p.lat, +p.lon, R * 1.01 + 0.175));
      beam.lookAt(new THREE.Vector3(0, 0, 0)); beam.rotateX(Math.PI / 2);
      markers.add(beam);
      if (p.online) {
        const ring = new THREE.Mesh(new THREE.RingGeometry(0.05, 0.07, 32), new THREE.MeshBasicMaterial({
          color: 0x3dffa8, transparent: true, side: THREE.DoubleSide, depthWrite: false }));
        ring.position.copy(toXYZ(+p.lat, +p.lon, R * 1.012));
        ring.lookAt(new THREE.Vector3(0, 0, 0));
        markers.add(ring); pulses.push(ring);
      }
    }
  }

  // drag to spin
  let drag = null, vel = 0.0015;
  canvas.addEventListener("pointerdown", (e) => { drag = [e.clientX, e.clientY]; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    earth.rotation.y += (e.clientX - drag[0]) * 0.006;
    earth.rotation.x = Math.max(-0.9, Math.min(0.9, earth.rotation.x + (e.clientY - drag[1]) * 0.004));
    drag = [e.clientX, e.clientY];
  });
  addEventListener("pointerup", () => { drag = null; });

  const fit = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
  };
  new ResizeObserver(fit).observe(canvas); fit();
  let visible = true;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }).observe(canvas);
  const clock = new THREE.Clock(), reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  renderer.setAnimationLoop(() => {
    if (!visible) return;
    const t = clock.getElapsedTime();
    if (!drag && !reduce) earth.rotation.y += vel;
    for (const r of pulses) { const k = (t * 0.8) % 1; r.scale.setScalar(1 + k * 4); r.material.opacity = 1 - k; }
    renderer.render(scene, camera);
  });
  return { setPlaces };
}
