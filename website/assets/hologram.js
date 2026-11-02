// 3D holographic globe for the hero (Three.js). Falls back to the 2D reactor if WebGL is unavailable.
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js";

const CYAN = 0x29d3ff;

function glowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, "rgba(235,252,255,1)");
  grad.addColorStop(0.25, "rgba(41,211,255,.9)");
  grad.addColorStop(0.6, "rgba(41,211,255,.18)");
  grad.addColorStop(1, "rgba(41,211,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
}

function fibonacciSphere(n, r) {
  const pts = new Float32Array(n * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / (n - 1)) * 2;
    const rad = Math.sqrt(1 - y * y);
    const th = golden * i;
    pts.set([Math.cos(th) * rad * r, y * r, Math.sin(th) * rad * r], i * 3);
  }
  return pts;
}

export function initHologram(canvas) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  } catch {
    return false;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
  camera.position.set(0, 0, 7.2);

  const root = new THREE.Group();
  scene.add(root);
  const additive = { transparent: true, blending: THREE.AdditiveBlending, depthWrite: false };

  // wireframe globe + dotted surface
  const globe = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.75, 3),
    new THREE.MeshBasicMaterial({ color: CYAN, wireframe: true, opacity: 0.14, ...additive }),
  );
  root.add(globe);
  const dotsGeo = new THREE.BufferGeometry();
  dotsGeo.setAttribute("position", new THREE.BufferAttribute(fibonacciSphere(1400, 1.78), 3));
  const dots = new THREE.Points(dotsGeo, new THREE.PointsMaterial({ color: 0x8be9ff, size: 0.028, opacity: 0.85, ...additive }));
  root.add(dots);

  // glowing core
  const core = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture(), color: 0xffffff, ...additive }));
  core.scale.setScalar(2.4);
  root.add(core);
  const coreBall = new THREE.Mesh(new THREE.SphereGeometry(0.42, 32, 32),
    new THREE.MeshBasicMaterial({ color: 0xc9f6ff, opacity: 0.9, ...additive }));
  root.add(coreBall);

  // orbit rings with satellites
  const rings = [];
  for (const [r, tiltX, tiltY, speed] of [[2.35, 1.2, 0.2, 0.5], [2.75, 0.4, 0.9, -0.35], [3.15, 1.7, -0.6, 0.25]]) {
    const pivot = new THREE.Group();
    pivot.rotation.set(tiltX, tiltY, 0);
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r, 0.006, 8, 220),
      new THREE.MeshBasicMaterial({ color: CYAN, opacity: 0.55, ...additive }));
    pivot.add(ring);
    const arc = new THREE.Mesh(new THREE.TorusGeometry(r, 0.022, 8, 80, Math.PI * 0.45),
      new THREE.MeshBasicMaterial({ color: 0x8be9ff, opacity: 0.9, ...additive }));
    pivot.add(arc);
    const sat = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture(), ...additive }));
    sat.scale.setScalar(0.32);
    pivot.add(sat);
    root.add(pivot);
    rings.push({ pivot, arc, sat, r, speed, phase: Math.random() * Math.PI * 2 });
  }

  // hex scanner ring under the globe
  const scanner = new THREE.Mesh(new THREE.RingGeometry(2.0, 2.06, 6, 1),
    new THREE.MeshBasicMaterial({ color: CYAN, opacity: 0.5, side: THREE.DoubleSide, ...additive }));
  scanner.rotation.x = Math.PI / 2;
  root.add(scanner);

  // star / data particle field
  const N = 900;
  const field = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const r = 4 + Math.random() * 6, th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
    field.set([r * Math.sin(ph) * Math.cos(th), r * Math.cos(ph), r * Math.sin(ph) * Math.sin(th)], i * 3);
  }
  const fieldGeo = new THREE.BufferGeometry();
  fieldGeo.setAttribute("position", new THREE.BufferAttribute(field, 3));
  const stars = new THREE.Points(fieldGeo, new THREE.PointsMaterial({ color: 0x5fd8ff, size: 0.03, opacity: 0.55, ...additive }));
  scene.add(stars);

  // interaction
  const mouse = { x: 0, y: 0 };
  window.addEventListener("pointermove", (e) => {
    mouse.x = e.clientX / window.innerWidth - 0.5;
    mouse.y = e.clientY / window.innerHeight - 0.5;
  });
  const resize = () => {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  new ResizeObserver(resize).observe(canvas);
  resize();

  let visible = true;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }).observe(canvas);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clock = new THREE.Clock();

  function frame() {
    requestAnimationFrame(frame);
    if (!visible) return;
    const t = reduce ? 0 : clock.getElapsedTime();
    globe.rotation.y = t * 0.12;
    dots.rotation.y = t * 0.12;
    dots.rotation.x = Math.sin(t * 0.2) * 0.1;
    const pulse = 1 + Math.sin(t * 2.4) * 0.07;
    core.scale.setScalar(2.4 * pulse);
    coreBall.scale.setScalar(pulse);
    for (const r of rings) {
      const a = r.phase + t * r.speed;
      r.arc.rotation.z = a;
      r.sat.position.set(Math.cos(a) * r.r, Math.sin(a) * r.r, 0);
    }
    scanner.position.y = Math.sin(t * 0.9) * 1.7;
    scanner.scale.setScalar(Math.sqrt(Math.max(0.05, 1 - (scanner.position.y / 1.9) ** 2)) * 0.9);
    scanner.rotation.z = t * 0.4;
    stars.rotation.y = t * 0.02;
    root.rotation.x += ((mouse.y * 0.5) - root.rotation.x) * 0.04;
    root.rotation.y += ((mouse.x * 0.7) - root.rotation.y) * 0.04;
    renderer.render(scene, camera);
  }
  frame();
  return true;
}
