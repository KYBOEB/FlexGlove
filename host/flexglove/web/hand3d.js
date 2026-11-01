/* FlexGlove — 3D-модель руки (этап 4, решение D-20).
   Three.js r170 лежит локально (vendor/three), рука собрана из примитивов: ладонь, запястье, 5 пальцев × 3 звена
   с шарнирами. Вид как на референсе (docs/background/hand_reference.png): белый матовый материал, мягкий свет,
   светло-серый фон, мягкая тень.

   Оси модели = оси руки (imu.py): X — к пальцам, Z — от тыльной стороны, Y = Z × X (у правой руки — к большому).
   Исходный вид (после «Обнулить положение»): пальцы вверх, тыльной стороной к зрителю — оператор видит руку
   на экране так же, как свою, и она поворачивается в те же стороны.

   Плавность: каждый кадр поза догоняет цель сферической интерполяцией с коэффициентом k = 1 − exp(−dt/τ),
   τ = 50 мс, — результат не зависит от частоты кадров, а обновления состояния 30 Гц не дают рывков. */
import * as THREE from "./vendor/three/three.module.min.js";

const TAU_S = 0.05;
const FINGERS = ["thumb", "index", "middle", "ring", "pinky"];
// углы суставов при полном сгибе, градусы: основание, средний, концевой
const MAX_ANGLES = { finger: [80, 100, 70], thumb: [50, 70] };
const NEUTRAL_BEND = 0.12;            // палец без сенсора — слегка расслаблен

// исходный вид: ось X руки → вверх экрана (+Y), ось Z руки (тыльная сторона) → к зрителю (+Z), Y руки → влево
const Q_NEUTRAL = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 2);

// размеры, см; позиции суставов в осях руки
const FINGER_SPEC = {
  index:  { base: [8.9, 2.9], len: [4.0, 2.4, 1.9], r: 1.04, splay: 4 },
  middle: { base: [9.2, 0.95], len: [4.4, 2.8, 2.0], r: 1.08, splay: 0 },
  ring:   { base: [8.9, -0.98], len: [4.1, 2.6, 1.9], r: 1.02, splay: -3 },
  pinky:  { base: [8.2, -2.85], len: [3.2, 2.1, 1.7], r: 0.90, splay: -8 },
};
const THUMB_SPEC = { base: [2.4, 2.9, -0.5], len: [3.4, 2.8, 2.3], r: 1.18, yaw: 34, toPalm: 22 };

const rad = THREE.MathUtils.degToRad;

function roundedPalmShape() {
  // ладонь в плоскости X–Y: у запястья уже (7 см), у костяшек шире (8,6 см), углы скруглены
  const s = new THREE.Shape();
  const wW = 3.3, wK = 4.0, L = 9.0, r = 2.2;
  s.moveTo(0, -wW + r);
  s.quadraticCurveTo(0, -wW, r, -wW);
  s.lineTo(L - r, -wK);
  s.quadraticCurveTo(L, -wK, L, -wK + r);
  s.lineTo(L, wK - r);
  s.quadraticCurveTo(L, wK, L - r, wK);
  s.lineTo(r, wW);
  s.quadraticCurveTo(0, wW, 0, wW - r);
  s.lineTo(0, -wW + r);
  return s;
}

class Hand3D {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });   // бросит исключение без WebGL
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(28, 1, 1, 200);
    this.camera.position.set(0, -1, 58);
    this.camera.lookAt(0, -3, 0);

    // мягкий рассеянный свет + основной источник сверху-слева с мягкой тенью
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xcfd2d8, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.5);
    key.position.set(-14, 30, 26);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    key.shadow.radius = 6;
    key.shadow.bias = -0.0005;
    Object.assign(key.shadow.camera, { left: -20, right: 20, top: 20, bottom: -20, near: 1, far: 90 });
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.45);
    fill.position.set(18, 4, 20);
    this.scene.add(fill);

    // невидимый «пол», на котором видна только тень
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(120, 120), new THREE.ShadowMaterial({ opacity: 0.12 }));
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -14;
    floor.receiveShadow = true;
    this.scene.add(floor);

    this.material = new THREE.MeshStandardMaterial({ color: 0xf3f3f1, roughness: 0.82, metalness: 0.0 });
    this.ghost = new THREE.MeshStandardMaterial({ color: 0xf3f3f1, roughness: 0.82, metalness: 0.0, transparent: true, opacity: 0.45 });

    this.pivot = new THREE.Group();            // точка вращения — запястье
    this.pivot.position.set(0, -10.5, 0);
    this.scene.add(this.pivot);
    this.mirror = new THREE.Group();           // левая рука — зеркально по оси Y руки
    this.pivot.add(this.mirror);
    this.joints = {};
    this._build();

    this.qTarget = new THREE.Quaternion();
    this.qCurrent = new THREE.Quaternion();
    this.bendTarget = Object.fromEntries(FINGERS.map((f) => [f, NEUTRAL_BEND]));
    this.bendCurrent = { ...this.bendTarget };
    this.sensed = new Set();
    this.setHand("right");
    this._resize();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(container);
    this._last = performance.now();
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  _mesh(geo) {
    const m = new THREE.Mesh(geo, this.material);
    m.castShadow = true;
    m.receiveShadow = true;
    return m;
  }

  _segment(parent, length, radius) {
    // звено пальца: капсула вдоль X от 0 до length; следующий сустав — в конце
    const geo = new THREE.CapsuleGeometry(radius, Math.max(0.01, length - radius * 0.9), 6, 16);
    geo.rotateZ(-Math.PI / 2);
    const m = this._mesh(geo);
    m.position.x = length / 2;
    m.scale.z = 0.86;                          // пальцы чуть сплющены (тыльная — ладонная сторона)
    parent.add(m);
    const next = new THREE.Group();
    next.position.x = length;
    parent.add(next);
    return { mesh: m, next };
  }

  _build() {
    const palmGeo = new THREE.ExtrudeGeometry(roundedPalmShape(), {
      depth: 1.0, bevelEnabled: true, bevelThickness: 1.0, bevelSize: 0.9, bevelSegments: 8, curveSegments: 16,
    });
    palmGeo.translate(0, 0, -0.5);
    this.mirror.add(this._mesh(palmGeo));

    const wristGeo = new THREE.CylinderGeometry(2.75, 3.0, 4.2, 28, 1);
    wristGeo.rotateZ(-Math.PI / 2);
    const wrist = this._mesh(wristGeo);
    wrist.scale.set(1, 1, 0.62);
    wrist.position.x = -1.6;
    this.mirror.add(wrist);

    for (const [name, sp] of Object.entries(FINGER_SPEC)) {
      const root = new THREE.Group();
      root.position.set(sp.base[0], sp.base[1], 0.15);
      root.rotation.z = rad(sp.splay);
      this.mirror.add(root);
      const j0 = new THREE.Group();
      root.add(j0);
      const s0 = this._segment(j0, sp.len[0], sp.r);
      const j1 = s0.next;
      const s1 = this._segment(j1, sp.len[1], sp.r * 0.9);
      const j2 = s1.next;
      const s2 = this._segment(j2, sp.len[2], sp.r * 0.82);
      this.joints[name] = { joints: [j0, j1, j2], meshes: [s0.mesh, s1.mesh, s2.mesh] };
    }

    // большой палец: своя кинематика — основание (пястно-запястный сустав) разворачивается к ладони и поперёк неё
    const t = THUMB_SPEC;
    const troot = new THREE.Group();
    troot.position.set(...t.base);
    troot.rotation.set(0, rad(t.toPalm), rad(t.yaw), "ZYX");
    this.mirror.add(troot);
    const cmc = new THREE.Group();
    troot.add(cmc);
    const m0 = this._segment(cmc, t.len[0], t.r);
    const mcp = m0.next;
    const m1 = this._segment(mcp, t.len[1], t.r * 0.9);
    const ip = m1.next;
    const m2 = this._segment(ip, t.len[2], t.r * 0.8);
    this.joints.thumb = { joints: [cmc, mcp, ip], meshes: [m0.mesh, m1.mesh, m2.mesh] };
  }

  _pose(finger, b) {
    const j = this.joints[finger].joints;
    if (finger === "thumb") {
      j[0].rotation.set(0, rad(28 * b), rad(-38 * b));       // к ладони и поперёк
      j[1].rotation.y = rad(MAX_ANGLES.thumb[0] * b);
      j[2].rotation.y = rad(MAX_ANGLES.thumb[1] * b);
    } else {
      const [a0, a1, a2] = MAX_ANGLES.finger;
      j[0].rotation.y = rad(a0 * b);
      j[1].rotation.y = rad(a1 * b);
      j[2].rotation.y = rad(a2 * b);
    }
  }

  setHand(hand) {
    this.hand = hand === "left" ? "left" : "right";
    this.mirror.scale.y = this.hand === "left" ? -1 : 1;
  }

  /** q — поворот руки от исходной позы [w, x, y, z] (или null); bends — {палец: 0..1}; sensed — пальцы с сенсором. */
  setTarget({ q, bends, sensed, hand }) {
    if (hand && hand !== this.hand) this.setHand(hand);
    const rel = q ? new THREE.Quaternion(q[1], q[2], q[3], q[0]).normalize() : new THREE.Quaternion();
    this.qTarget.copy(Q_NEUTRAL).multiply(rel);
    if (sensed) {
      const changed = sensed.size !== this.sensed.size || [...sensed].some((f) => !this.sensed.has(f));
      this.sensed = new Set(sensed);
      if (changed) {
        for (const f of FINGERS) for (const m of this.joints[f].meshes) m.material = this.sensed.has(f) ? this.material : this.ghost;
      }
    }
    for (const f of FINGERS) this.bendTarget[f] = this.sensed.has(f) ? Math.min(1, Math.max(0, bends?.[f] ?? 0)) : NEUTRAL_BEND;
  }

  _resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  _loop(now) {
    requestAnimationFrame(this._loop);
    const dt = Math.min(0.1, Math.max(0, (now - this._last) / 1000));
    this._last = now;
    if (document.hidden || !this.container.offsetParent) return;      // вкладка скрыта — не рисуем
    const k = 1 - Math.exp(-dt / TAU_S);
    this.qCurrent.slerp(this.qTarget, k);
    this.pivot.quaternion.copy(this.qCurrent);
    for (const f of FINGERS) {
      this.bendCurrent[f] += (this.bendTarget[f] - this.bendCurrent[f]) * k;
      this._pose(f, this.bendCurrent[f]);
    }
    this.renderer.render(this.scene, this.camera);
  }
}

function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch (e) {
    return false;
  }
}

window.FlexHand3D = {
  create(container) {
    if (!webglAvailable()) return null;
    try { return new Hand3D(container); } catch (e) { console.warn("3D-рука недоступна:", e); return null; }
  },
};
window.dispatchEvent(new Event("hand3d-ready"));
