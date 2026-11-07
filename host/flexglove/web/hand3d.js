/* FlexGlove — 3D-рука на главном экране (этап 4б, решения D-27, D-28).

   Модель: generic-hand из WebXR Input Profiles (MIT, web/vendor/models/generic-hand) — анатомическая рука
   со скелетом по стандарту WebXR (wrist, <палец>-metacarpal / -phalanx-proximal / -intermediate / -distal / -tip).
   Загрузчик GLTFLoader и OrbitControls — из three r170 (web/vendor/three/addons), без CDN. Если модель не загрузилась,
   рисуется запасная рука из примитивов (как в v0.5.0), если нет WebGL — 2D-силуэт (app.js).

   Кости в модели не вложены друг в друга (каждая задана относительно «Armature»), поэтому цепочку пальца строим сами:
   запоминаем позы костей в покое, считаем относительное положение соседних L_i = M_(i−1)⁻¹ · M_i и при сгибе
   собираем P_i = P_(i−1) · L_i · R_i, где R_i — поворот сустава i (handpose.js: сгиб 0..1 → углы с пределами).
   В системе сустава WebXR ось −Z идёт вдоль кости к кончику, +Y — от тыльной стороны, поэтому сгиб к ладони —
   поворот вокруг X на отрицательный угол.

   Системы координат:
   - оси руки (imu.py, D-24): X — к пальцам, Z — от тыльной стороны, Y = Z × X. Модель ставится так, чтобы её
     запястье совпало с этими осями (матрица B: x_руки = −z_зап, y_руки = −x_зап, z_руки = y_зап, масштаб м → см);
   - мир (ядро, D-28): Z — вверх, X — «вперёд» (курс, принятый при обнулении рысканья), Y — влево;
   - сцена three.js: Y — вверх, −Z — в экран, X — вправо. Мир → сцена: X_сц = −Y_мир, Y_сц = Z_мир, Z_сц = −X_мир.
   Камера стоит неподвижно в мире (верх экрана = верх комнаты) и смотрит на руку немного сверху-сбоку, как на свою:
   рука опущена → на экране пальцы вниз; ладонь вверх → ладонь вверх. Мышь вращает только вид (OrbitControls).

   Плавность: поза догоняет цель с k = 1 − exp(−dt/τ), τ = 50 мс (не зависит от частоты кадров). */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { FINGERS, NEUTRAL_BEND, jointAngles } from "flexglove/handpose";

const TAU_S = 0.05;
const VERSION = new URL(import.meta.url).search;            // ?v=0.6.0 — та же версия для файлов модели
const MODEL_URL = (hand) => `/static/vendor/models/generic-hand/${hand}.glb${VERSION}`;
const CHAINS = {
  thumb: ["thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip"],
  index: ["index-finger-metacarpal", "index-finger-phalanx-proximal", "index-finger-phalanx-intermediate", "index-finger-phalanx-distal", "index-finger-tip"],
  middle: ["middle-finger-metacarpal", "middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate", "middle-finger-phalanx-distal", "middle-finger-tip"],
  ring: ["ring-finger-metacarpal", "ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate", "ring-finger-phalanx-distal", "ring-finger-tip"],
  pinky: ["pinky-finger-metacarpal", "pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate", "pinky-finger-phalanx-distal", "pinky-finger-tip"],
};
const rad = THREE.MathUtils.degToRad;
const AX = new THREE.Vector3(1, 0, 0), AY = new THREE.Vector3(0, 1, 0);

// мир (Z вверх, X вперёд, Y влево) → сцена three.js (Y вверх, −Z в экран)
const Q_WORLD_TO_SCENE = new THREE.Quaternion().setFromRotationMatrix(
  new THREE.Matrix4().set(0, -1, 0, 0, 0, 0, 1, 0, -1, 0, 0, 0, 0, 0, 0, 1));
// поза без данных IMU: пальцы вверх, тыльной стороной к зрителю (поворот вокруг Y мира на −90°)
const Q_DEFAULT_WORLD = [Math.SQRT1_2, 0, -Math.SQRT1_2, 0];
// камера: чуть сверху (≈ 18°) и правее (≈ 25°) — рука объёмная, но видна «как своя»; кисть с предплечьем
// целиком в кадре при любом наклоне. Центр вращения на экране — середина кисти (PIVOT_X см от запястья к пальцам):
// тогда рука не уезжает за край кадра, а повороты те же — ориентация не зависит от выбора центра.
const CAMERA_POS = new THREE.Vector3(31, 22, 66);
const CAMERA_TARGET = new THREE.Vector3(0, -2, 0);
const PIVOT_X = 4.5;
const FOREARM_LEN = 10;

class Hand3D {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });   // бросит исключение без WebGL
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(30, 1, 1, 400);
    this.camera.position.copy(CAMERA_POS);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(CAMERA_TARGET);
    this.controls.enablePan = false;
    this.controls.enableDamping = true;
    this.controls.minDistance = 30;
    this.controls.maxDistance = 140;
    this.controls.update();
    this.controls.saveState();

    // мягкий рассеянный свет + основной источник сверху-слева с мягкой тенью + заполняющий справа
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xc9ccd2, 1.7));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(-12, 60, 22);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.radius = 8;
    key.shadow.bias = -0.0004;
    key.shadow.normalBias = 0.02;
    Object.assign(key.shadow.camera, { left: -30, right: 30, top: 30, bottom: -30, near: 1, far: 140 });
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.5);
    fill.position.set(26, 6, 24);
    this.scene.add(fill);
    const rim = new THREE.DirectionalLight(0xffffff, 0.35);
    rim.position.set(0, 10, -40);
    this.scene.add(rim);

    // невидимый «пол» под рукой: на нём видна только мягкая тень
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(220, 220), new THREE.ShadowMaterial({ opacity: 0.13 }));
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = -22;
    floor.receiveShadow = true;
    this.scene.add(floor);

    this.material = new THREE.MeshStandardMaterial({ color: 0xf4f4f2, roughness: 0.86, metalness: 0.0 });

    this.pivot = new THREE.Group();            // оси pivot = оси руки; центр — середина кисти
    this.scene.add(this.pivot);
    this.handGroup = new THREE.Group();        // запястье в начале координат этой группы
    this.handGroup.position.x = -PIVOT_X;
    this.pivot.add(this.handGroup);

    this.qTarget = new THREE.Quaternion();
    this.qCurrent = new THREE.Quaternion();
    this.bendTarget = Object.fromEntries(FINGERS.map((f) => [f, NEUTRAL_BEND]));
    this.bendCurrent = { ...this.bendTarget };
    this.sensed = new Set(FINGERS);
    this.hand = null;
    this.model = null;                         // {root, bones, chains, ghost uniforms} или запасная рука
    this._loading = null;
    this._setWorldQuat(Q_DEFAULT_WORLD);
    this.qCurrent.copy(this.qTarget);
    this.setHand("right");

    this._resize();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(container);
    this._last = performance.now();
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  // ---------------- модель ----------------
  setHand(hand) {
    hand = hand === "left" ? "left" : "right";
    if (hand === this.hand) return;
    this.hand = hand;
    const token = (this._loading = {});
    new GLTFLoader().load(MODEL_URL(hand), (gltf) => {
      if (this._loading !== token) return;     // пока грузилась — переключили руку
      try { this._useModel(this._prepareGltf(gltf)); }
      catch (e) { console.warn("Модель руки не подошла, рисую запасную:", e); this._useModel(this._buildPrimitive()); }
    }, undefined, (err) => {
      if (this._loading !== token) return;
      console.warn("Модель руки не загрузилась, рисую запасную:", err);
      this._useModel(this._buildPrimitive());
    });
  }

  _useModel(model) {
    if (this.model) this.handGroup.remove(this.model.root);
    if (this.forearm) this.handGroup.remove(this.forearm);
    this.model = model;
    this.handGroup.add(model.root);
    this.forearm = this._buildForearm(model.wristSize || { y: 6.0, z: 4.0 });
    this.handGroup.add(this.forearm);
    this._applyGhost();
  }

  // короткое предплечье, плавно растворяющееся к локтю: закрывает срез модели у запястья и показывает,
  // откуда «растёт» рука (без резкого торца, который в позе «пальцы от себя» смотрел бы прямо в камеру)
  _buildForearm({ y, z }) {
    const r0 = (y / 2) * 0.97, r1 = r0 * 1.1;
    const geo = new THREE.CylinderGeometry(r0, r1, FOREARM_LEN, 48, 8, true);
    geo.rotateZ(Math.PI / 2);                  // ось цилиндра → −X руки: от запястья к локтю
    geo.translate(-FOREARM_LEN / 2 + 1.0, 0, 0);
    const c = document.createElement("canvas");
    c.width = 4; c.height = 64;
    const g = c.getContext("2d"), grad = g.createLinearGradient(0, 0, 0, 64);
    grad.addColorStop(0, "#000"); grad.addColorStop(0.75, "#fff"); grad.addColorStop(1, "#fff");
    g.fillStyle = grad; g.fillRect(0, 0, 4, 64);
    const mat = this.material.clone();
    mat.alphaMap = new THREE.CanvasTexture(c);
    mat.transparent = true;               // глубину пишет: срез модели у запястья не просвечивает
    const m = new THREE.Mesh(geo, mat);
    m.scale.z = Math.min(1, (z / 2) / r0);      // предплечье сплюснуто, как запястье модели
    m.renderOrder = -1;
    return m;
  }

  _prepareGltf(gltf) {
    const scene = gltf.scene;
    scene.updateMatrixWorld(true);
    let mesh = null;
    scene.traverse((o) => { if (o.isSkinnedMesh) mesh = o; });
    if (!mesh) throw new Error("в модели нет SkinnedMesh");
    const byName = {};
    for (const b of mesh.skeleton.bones) byName[b.name] = b;
    const wrist = byName.wrist;
    if (!wrist) throw new Error("в модели нет кости wrist");

    // модель → оси руки: запястье в начало координат, x_руки = −z_зап, y_руки = −x_зап, z_руки = y_зап; м → см
    const B = new THREE.Matrix4().set(0, 0, -1, 0, -1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1);
    const toHand = new THREE.Matrix4().makeScale(100, 100, 100).multiply(B).multiply(wrist.matrixWorld.clone().invert());
    const root = new THREE.Group();
    root.matrixAutoUpdate = false;
    root.matrix.copy(toHand);
    root.add(scene);

    // цепочки пальцев: позы костей в покое (относительно их общего родителя) и относительные переходы
    const chains = {};
    for (const [finger, names] of Object.entries(CHAINS)) {
      const bones = names.map((n) => byName[n]);
      if (bones.some((b) => !b)) throw new Error(`нет костей пальца ${finger}`);
      for (const b of bones) b.updateMatrix();
      const rest = bones.map((b) => b.matrix.clone());
      const rel = rest.map((m, i) => (i === 0 ? m.clone() : rest[i - 1].clone().invert().multiply(m)));
      chains[finger] = { bones, rel };
    }

    // материал: белый матовый; пальцы без сенсора — светлее и полупрозрачные (вес кости → «призрачность»)
    const boneGhost = { value: new Float32Array(64) };
    const boneIndex = Object.fromEntries(mesh.skeleton.bones.map((b, i) => [b.name, i]));
    const mat = this.material.clone();
    mat.onBeforeCompile = (shader) => {
      shader.uniforms.boneGhost = boneGhost;
      shader.vertexShader = shader.vertexShader
        .replace("#include <common>", "#include <common>\nuniform float boneGhost[64];\nvarying float vGhost;")
        .replace("#include <skinning_vertex>", `#include <skinning_vertex>
          vGhost = skinWeight.x * boneGhost[int(skinIndex.x)] + skinWeight.y * boneGhost[int(skinIndex.y)]
                 + skinWeight.z * boneGhost[int(skinIndex.z)] + skinWeight.w * boneGhost[int(skinIndex.w)];`);
      shader.fragmentShader = shader.fragmentShader
        .replace("#include <common>", "#include <common>\nvarying float vGhost;")
        .replace("#include <color_fragment>", `#include <color_fragment>
          diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.86, 0.90, 0.97), vGhost * 0.7);
          diffuseColor.a *= 1.0 - 0.35 * vGhost;`);
    };
    mesh.material = mat;
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.frustumCulled = false;               // кости двигаются — исходная рамка модели не годится

    // толщина запястья (для предплечья): вершины модели в осях руки ближе 1,2 см к запястью
    const pos = mesh.geometry.attributes.position, v = new THREE.Vector3();
    const M = toHand.clone().multiply(mesh.matrixWorld);
    let y0 = Infinity, y1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (let i = 0; i < pos.count; i++) {
      v.fromBufferAttribute(pos, i).applyMatrix4(M);
      if (v.x < 1.2) { y0 = Math.min(y0, v.y); y1 = Math.max(y1, v.y); z0 = Math.min(z0, v.z); z1 = Math.max(z1, v.z); }
    }
    const wristSize = isFinite(y0) ? { y: y1 - y0, z: z1 - z0 } : null;
    return { root, chains, mesh, boneGhost, boneIndex, wristSize, kind: "gltf" };
  }

  _applyGhost() {
    const m = this.model;
    if (!m) return;
    if (m.kind === "gltf") {
      m.boneGhost.value.fill(0);
      let any = false;
      for (const f of FINGERS) {
        if (this.sensed.has(f)) continue;
        any = true;
        // пястная кость — часть ладони, «призрачными» делаем только фаланги
        for (const b of m.chains[f].bones.slice(1)) m.boneGhost.value[m.boneIndex[b.name]] = 1;
      }
      m.mesh.material.transparent = any;
      m.mesh.material.needsUpdate = true;
    } else {
      for (const f of FINGERS) for (const s of m.fingers[f].meshes) s.material = this.sensed.has(f) ? m.material : m.ghost;
    }
  }

  // поза пальца: сгиб 0..1 → углы суставов (handpose.js) → кости
  _poseFinger(finger, bend) {
    const m = this.model;
    if (!m) return;
    const a = jointAngles(finger, bend);
    if (m.kind !== "gltf") return m.pose(finger, a);
    const { bones, rel } = m.chains[finger];
    const turns = finger === "thumb"
      ? [new THREE.Quaternion().setFromAxisAngle(AX, -rad(a.metacarpalAcross)).multiply(
          new THREE.Quaternion().setFromAxisAngle(AY, rad(a.metacarpalOppose))),
         new THREE.Quaternion().setFromAxisAngle(AX, -rad(a.proximal)),
         new THREE.Quaternion().setFromAxisAngle(AX, -rad(a.distal)), null]
      : [null, new THREE.Quaternion().setFromAxisAngle(AX, -rad(a.proximal)),
         new THREE.Quaternion().setFromAxisAngle(AX, -rad(a.intermediate)),
         new THREE.Quaternion().setFromAxisAngle(AX, -rad(a.distal)), null];
    const P = new THREE.Matrix4(), R = new THREE.Matrix4();
    for (let i = 0; i < bones.length; i++) {
      if (i === 0) P.copy(rel[0]); else P.multiply(rel[i]);
      if (turns[i]) P.multiply(R.makeRotationFromQuaternion(turns[i]));
      P.decompose(bones[i].position, bones[i].quaternion, bones[i].scale);
    }
  }

  // ---------------- запасная рука из примитивов (v0.5.0) ----------------
  _buildPrimitive() {
    const material = this.material;
    const ghost = new THREE.MeshStandardMaterial({ color: 0xdfe5ef, roughness: 0.86, transparent: true, opacity: 0.45 });
    const root = new THREE.Group();
    const mesh = (geo) => { const m = new THREE.Mesh(geo, material); m.castShadow = m.receiveShadow = true; return m; };
    const s = new THREE.Shape();
    const wW = 3.3, wK = 4.0, L = 9.0, r = 2.2;
    s.moveTo(0, -wW + r); s.quadraticCurveTo(0, -wW, r, -wW); s.lineTo(L - r, -wK); s.quadraticCurveTo(L, -wK, L, -wK + r);
    s.lineTo(L, wK - r); s.quadraticCurveTo(L, wK, L - r, wK); s.lineTo(r, wW); s.quadraticCurveTo(0, wW, 0, wW - r); s.lineTo(0, -wW + r);
    const palm = new THREE.ExtrudeGeometry(s, { depth: 1.0, bevelEnabled: true, bevelThickness: 1.0, bevelSize: 0.9, bevelSegments: 8, curveSegments: 16 });
    palm.translate(0, 0, -0.5);
    root.add(mesh(palm));
    const segment = (parent, len, rad_) => {
      const geo = new THREE.CapsuleGeometry(rad_, Math.max(0.01, len - rad_ * 0.9), 6, 16);
      geo.rotateZ(-Math.PI / 2);
      const m = mesh(geo); m.position.x = len / 2; m.scale.z = 0.86; parent.add(m);
      const next = new THREE.Group(); next.position.x = len; parent.add(next);
      return { m, next };
    };
    const SPEC = {
      index: { base: [8.9, 2.9], len: [4.0, 2.4, 1.9], r: 1.04, splay: 4 },
      middle: { base: [9.2, 0.95], len: [4.4, 2.8, 2.0], r: 1.08, splay: 0 },
      ring: { base: [8.9, -0.98], len: [4.1, 2.6, 1.9], r: 1.02, splay: -3 },
      pinky: { base: [8.2, -2.85], len: [3.2, 2.1, 1.7], r: 0.90, splay: -8 },
    };
    const fingers = {};
    for (const [name, sp] of Object.entries(SPEC)) {
      const base = new THREE.Group(); base.position.set(sp.base[0], sp.base[1], 0.15); base.rotation.z = rad(sp.splay); root.add(base);
      const j0 = new THREE.Group(); base.add(j0);
      const a = segment(j0, sp.len[0], sp.r), b = segment(a.next, sp.len[1], sp.r * 0.9), c = segment(b.next, sp.len[2], sp.r * 0.82);
      fingers[name] = { joints: [j0, a.next, b.next], meshes: [a.m, b.m, c.m] };
    }
    const troot = new THREE.Group(); troot.position.set(2.4, 2.9, -0.5); troot.rotation.set(0, rad(22), rad(34), "ZYX"); root.add(troot);
    const cmc = new THREE.Group(); troot.add(cmc);
    const t0 = segment(cmc, 3.4, 1.18), t1 = segment(t0.next, 2.8, 1.06), t2 = segment(t1.next, 2.3, 0.94);
    fingers.thumb = { joints: [cmc, t0.next, t1.next], meshes: [t0.m, t1.m, t2.m] };
    if (this.hand === "left") root.scale.y = -1;
    const pose = (finger, a) => {
      const j = fingers[finger].joints;
      if (finger === "thumb") {
        j[0].rotation.set(0, rad(a.metacarpalOppose), rad(-a.metacarpalAcross));
        j[1].rotation.y = rad(a.proximal); j[2].rotation.y = rad(a.distal);
      } else {
        j[0].rotation.y = rad(a.proximal); j[1].rotation.y = rad(a.intermediate); j[2].rotation.y = rad(a.distal);
      }
    };
    return { root, fingers, material, ghost, pose, kind: "primitive" };
  }

  // ---------------- данные ----------------
  _setWorldQuat(q) {
    const w = new THREE.Quaternion(q[1], q[2], q[3], q[0]).normalize();
    this.qTarget.copy(Q_WORLD_TO_SCENE).multiply(w);
  }

  /** q — ориентация руки в мире [w, x, y, z] (ядро: orientation.q_world) или null — исходная поза «пальцы вверх»;
      bends — {палец: 0..1}; sensed — множество пальцев с сенсором; hand — "right" | "left". */
  setTarget({ q, bends, sensed, hand }) {
    if (hand) this.setHand(hand);
    this._setWorldQuat(q || Q_DEFAULT_WORLD);
    if (sensed) {
      const changed = sensed.size !== this.sensed.size || [...sensed].some((f) => !this.sensed.has(f));
      this.sensed = new Set(sensed);
      if (changed) this._applyGhost();
    }
    for (const f of FINGERS) this.bendTarget[f] = this.sensed.has(f) ? Math.min(1, Math.max(0, bends?.[f] ?? 0)) : NEUTRAL_BEND;
  }

  /** «Вид по умолчанию»: камера на исходное место (вращение мышью сбрасывается). */
  resetView() {
    this.controls.reset();
  }

  _resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    // в узком окне отодвигаем камеру, чтобы рука помещалась и по ширине
    this.camera.zoom = Math.min(1, (w / h) / 0.9);
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
      this._poseFinger(f, this.bendCurrent[f]);
    }
    this.controls.update();
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
