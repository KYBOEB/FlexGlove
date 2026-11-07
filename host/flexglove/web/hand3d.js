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

   Кадрирование (этап 4в): по модели считается ограничивающая сфера кисти (центр — середина рамки вершин, радиус —
   до самой дальней вершины). Кисть вращается вокруг центра сферы, поэтому при любом повороте остаётся внутри неё;
   камера ставится на расстояние, при котором сфера занимает FRAME_FILL высоты видимой части сцены:
   d = r / (FRAME_FILL · tg(fov/2)) (и не ближе, чем нужно по ширине). Видимая часть — окно без строки углов сверху
   и полосы демо снизу (setInsets); центр кадра смещается в её середину (camera.setViewOffset).
   Пересчёт — при загрузке модели, смене руки и изменении размера окна; «Вид по умолчанию» — тот же кадр.

   Тень (этап 4в): мягкое пятно прямо под рукой. Плоскость пятна каждый кадр опускается под нижнюю точку кисти
   (по костям, с запасом на толщину пальцев), размер и поворот пятна — по «следу» костей на полу (главные оси
   облака точек). Самозатенение пальцев на ладони даёт основной свет — так сгибы читаются на белом материале.

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
// камера «Вида по умолчанию»: правее на VIEW_AZIMUTH и сверху на VIEW_ELEVATION — в стартовой позе демо
// (пальцы вверх, тыльной стороной к зрителю) видны все 5 пальцев и их сгиб к ладони.
const VIEW_AZIMUTH = 25, VIEW_ELEVATION = 18;            // градусы
const FRAME_FILL = 0.78;       // доля высоты видимой части, которую занимает сфера кисти (сама кисть ≈ 0,6–0,7)
const FRAME_FILL_W = 0.9;      // и не больше этой доли ширины
const FOV = 30;
const KEY_DIR = new THREE.Vector3(-35, 40, 28).normalize();   // основной свет: слева-сверху-спереди
const SHADOW_GAP = 1.1;        // см: кости идут по оси пальцев, поверхность ниже примерно на столько

class Hand3D {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });   // бросит исключение без WebGL
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.15;          // белый матовый, как на референсе
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(FOV, 1, 1, 600);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enablePan = false;
    this.controls.enableDamping = true;
    this.radius = 10;                          // радиус сферы кисти, см (уточняется по модели)
    this.insets = { top: 0, bottom: 0 };
    this._viewDir(VIEW_AZIMUTH, VIEW_ELEVATION);

    // рассеянный свет слабее, основной (сверху-слева, сбоку) сильнее — у белой руки тени между пальцами и у суставов
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xaeb3bc, 0.7));
    const key = (this.key = new THREE.DirectionalLight(0xffffff, 3.0));
    key.castShadow = true;                     // только самозатенение руки: пола, который ловит тень, нет
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.radius = 4;
    key.shadow.bias = -0.0005;
    key.shadow.normalBias = 0.03;
    this.scene.add(key, key.target);
    const fill = new THREE.DirectionalLight(0xffffff, 0.45);
    fill.position.set(26, 6, 24);
    this.scene.add(fill);
    const rim = new THREE.DirectionalLight(0xffffff, 0.4);
    rim.position.set(0, 10, -40);
    this.scene.add(rim);
    this._fitLight();

    this.blob = this._buildBlobShadow();
    this.scene.add(this.blob);

    this.material = new THREE.MeshStandardMaterial({ color: 0xf4f4f2, roughness: 0.86, metalness: 0.0 });

    this.pivot = new THREE.Group();            // оси pivot = оси руки; центр — середина кисти
    this.scene.add(this.pivot);
    this.handGroup = new THREE.Group();        // запястье в начале координат этой группы; центр сферы — в pivot
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
    this.model = model;
    this.handGroup.add(model.root);
    // центр вращения — центр сферы кисти; камера — под её размер
    const sphere = model.sphere || this._sphereOf(model.root);
    this.handGroup.position.copy(sphere.center).negate();
    this.radius = sphere.radius;
    this._applyGhost();
    this._fit();
    this._fitLight();
  }

  // сфера по рамке объекта в осях руки (для запасной руки из примитивов)
  _sphereOf(obj) {
    this.handGroup.updateMatrixWorld(true);
    const inv = this.handGroup.matrixWorld.clone().invert();
    const box = new THREE.Box3().setFromObject(obj).applyMatrix4(inv);
    return box.getBoundingSphere(new THREE.Sphere());
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
    mat.side = THREE.DoubleSide;              // срез у запястья: изнутри видна поверхность, а не «дыра»
    mesh.material = mat;
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.frustumCulled = false;               // кости двигаются — исходная рамка модели не годится

    // ограничивающая сфера кисти в осях руки: центр — середина рамки вершин, радиус — до самой дальней вершины
    // (пальцы в модели прямые — это самая «длинная» поза, при сгибе кисть только помещается в сферу лучше)
    const pos = mesh.geometry.attributes.position, v = new THREE.Vector3();
    const M = toHand.clone().multiply(mesh.matrixWorld);
    const pts = [];
    for (let i = 0; i < pos.count; i++) pts.push(v.fromBufferAttribute(pos, i).applyMatrix4(M).clone());
    const center = new THREE.Box3().setFromPoints(pts).getCenter(new THREE.Vector3());
    let r2 = 0;
    for (const p of pts) r2 = Math.max(r2, p.distanceToSquared(center));
    const sphere = new THREE.Sphere(center, Math.sqrt(r2));
    // точки для тени: все кости пальцев и запястье ± полширины ладони
    const wristSide = (pts.reduce((m, p) => (p.x < 1.5 ? Math.max(m, Math.abs(p.y)) : m), 0) || 3) * 0.8;
    const shadowBones = [byName.wrist, ...Object.values(chains).flatMap((c) => c.bones)];
    return { root, chains, mesh, boneGhost, boneIndex, sphere, shadowBones, wristSide, kind: "gltf" };
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

  /** «Вид по умолчанию»: камера сверху-сбоку на расстоянии кадрирования (вращение мышью сбрасывается). */
  resetView() {
    this._viewDir(VIEW_AZIMUTH, VIEW_ELEVATION);
    this._fit();
  }

  /** Отступы сверху и снизу (px), которые закрыты подписями и полосой демо: рука центрируется в остальной части. */
  setInsets({ top = 0, bottom = 0 } = {}) {
    if (top === this.insets.top && bottom === this.insets.bottom) return;
    this.insets = { top, bottom };
    this._resize();
  }

  _viewDir(azimuthDeg, elevationDeg) {
    const az = rad(azimuthDeg), el = rad(elevationDeg);
    this.controls.target.set(0, 0, 0);
    this.camera.position.set(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el));
  }

  /** Расстояние камеры, при котором сфера кисти занимает FRAME_FILL высоты видимой части (и влезает по ширине). */
  fitDistance() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    const hs = Math.max(80, h - this.insets.top - this.insets.bottom);
    const tanV = Math.tan(rad(FOV / 2)), tanH = tanV * (w / h);
    // экран по высоте при расстоянии d вмещает 2·d·tgV; видимой части нужна доля FRAME_FILL·hs/h
    return Math.max(this.radius / (FRAME_FILL * (hs / h) * tanV), this.radius / (FRAME_FILL_W * tanH));
  }

  // направление взгляда сохраняется (повёрнутый мышью вид не сбрасывается), меняется только расстояние
  _fit() {
    const d = this.fitDistance();
    const dir = this.camera.position.clone().sub(this.controls.target);
    if (dir.lengthSq() < 1e-9) dir.set(0, 0, 1);
    this.camera.position.copy(this.controls.target).addScaledVector(dir.normalize(), d);
    this.controls.minDistance = d * 0.55;
    this.controls.maxDistance = d * 2.5;
    this.camera.near = Math.max(0.5, d - this.radius * 3);
    this.camera.far = d + this.radius * 6;
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  // теневая камера основного света — вплотную вокруг сферы кисти (чётче самозатенение, без обрезанных краёв)
  _fitLight() {
    const r = this.radius + 1.5, k = this.key;
    k.position.copy(KEY_DIR).multiplyScalar(r * 4);
    k.target.position.set(0, 0, 0);
    Object.assign(k.shadow.camera, { left: -r, right: r, top: r, bottom: -r, near: r, far: r * 7 });
    k.shadow.camera.updateProjectionMatrix();
  }

  _resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    // центр кадра — середина видимой части: окно проекции сдвигается на половину разницы отступов
    const dy = Math.round((this.insets.bottom - this.insets.top) / 2);
    this.camera.setViewOffset(w, h, 0, dy, w, h);
    this._fit();
  }

  // ---------------- тень-пятно ----------------
  _buildBlobShadow() {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const g = c.getContext("2d"), grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    grad.addColorStop(0, "rgba(0,0,0,1)"); grad.addColorStop(0.45, "rgba(0,0,0,0.55)"); grad.addColorStop(1, "rgba(0,0,0,0)");
    g.fillStyle = grad; g.fillRect(0, 0, 128, 128);
    const mat = new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, opacity: 0.3,
      depthWrite: false, color: 0x30343c, toneMapped: false });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mat);
    m.rotation.x = -Math.PI / 2;
    m.renderOrder = -2;
    return m;
  }

  // пятно под нижней точкой кисти; размер и поворот — по главным осям «следа» костей на полу
  _updateBlob() {
    const m = this.model, blob = this.blob;
    this.scene.updateMatrixWorld();
    const pts = [], v = new THREE.Vector3();
    if (m && m.kind === "gltf") {
      for (const b of m.shadowBones) pts.push(b.getWorldPosition(new THREE.Vector3()));
      for (const s of [-1, 1]) pts.push(v.set(0, s * m.wristSide, 0).applyMatrix4(this.handGroup.matrixWorld).clone());
    } else if (m) {
      const box = new THREE.Box3().setFromObject(m.root);
      for (const x of [box.min.x, box.max.x]) for (const z of [box.min.z, box.max.z]) pts.push(new THREE.Vector3(x, box.min.y + SHADOW_GAP, z));
    }
    if (!pts.length) { blob.visible = false; return; }
    let minY = Infinity, cx = 0, cz = 0;
    for (const p of pts) { minY = Math.min(minY, p.y); cx += p.x; cz += p.z; }
    cx /= pts.length; cz /= pts.length;
    let sxx = 0, szz = 0, sxz = 0;
    for (const p of pts) { const dx = p.x - cx, dz = p.z - cz; sxx += dx * dx; szz += dz * dz; sxz += dx * dz; }
    const ang = 0.5 * Math.atan2(2 * sxz, sxx - szz);          // главная ось следа на полу
    const ux = Math.cos(ang), uz = Math.sin(ang);
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    for (const p of pts) {
      const dx = p.x - cx, dz = p.z - cz, a = dx * ux + dz * uz, b = -dx * uz + dz * ux;
      a0 = Math.min(a0, a); a1 = Math.max(a1, a); b0 = Math.min(b0, b); b1 = Math.max(b1, b);
    }
    const pad = 2.2;                                             // толщина пальцев и мягкий край
    blob.visible = true;
    blob.position.set(cx + ux * (a0 + a1) / 2 - uz * (b0 + b1) / 2, minY - SHADOW_GAP, cz + uz * (a0 + a1) / 2 + ux * (b0 + b1) / 2);
    blob.rotation.set(-Math.PI / 2, 0, -ang);
    blob.scale.set(a1 - a0 + pad * 2, b1 - b0 + pad * 2, 1);
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
    this._updateBlob();
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
