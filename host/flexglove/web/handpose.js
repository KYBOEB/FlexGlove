/* FlexGlove — сгиб пальца 0..1 → углы суставов 3D-руки (этап 4б, D-27). Чистые функции без three.js:
   их проверяют тесты (host/tests/test_handpose.py) и использует hand3d.js.

   Пределы при полном сгибе (кулак) — по анатомии кисти:
   - пальцы: пястно-фаланговый сустав (проксимальная фаланга) до 85°, проксимальный межфаланговый (средняя)
     до 100°, дистальный межфаланговый (концевая) ≈ 2/3 средней — 67° (у живой руки они сгибаются вместе);
   - большой палец — своя кинематика: основание (пястная кость) уходит поперёк ладони до 35° и к ладони
     (оппозиция) до 30°, пястно-фаланговый до 50°, межфаланговый до 70°.
   Сгиб распределяется по суставам не строго поровну: средний сустав «ведёт» (как при естественном сжатии),
   основание догоняет — так полусогнутый палец выглядит округлым, а не сломанным в одной точке. */

export const LIMITS = {
  finger: { proximal: 85, intermediate: 100, distal: 67 },
  thumb: { metacarpalAcross: 35, metacarpalOppose: 30, proximal: 50, distal: 70 },
};

export const NEUTRAL_BEND = 0.12;      // палец без сенсора — слегка расслаблен
export const FINGERS = ["thumb", "index", "middle", "ring", "pinky"];

const clamp01 = (x) => Math.min(1, Math.max(0, Number.isFinite(x) ? x : 0));
// плавные кривые 0..1 → 0..1 (концы закреплены: 0 → 0, 1 → 1, монотонны)
const lead = (b) => 1 - (1 - b) * (1 - b) * (1 - 0.35 * b);   // чуть быстрее линии в начале
const lag = (b) => b * b * (1.35 - 0.35 * b);                  // чуть медленнее в начале

/** Углы суставов в градусах (≥ 0, сгиб к ладони) для пальца finger при сгибе bend 0..1. */
export function jointAngles(finger, bend) {
  const b = clamp01(bend);
  if (finger === "thumb") {
    const L = LIMITS.thumb;
    return {
      metacarpalAcross: L.metacarpalAcross * lag(b),
      metacarpalOppose: L.metacarpalOppose * b,
      proximal: L.proximal * b,
      distal: L.distal * lead(b),
    };
  }
  const L = LIMITS.finger;
  return { proximal: L.proximal * lag(b), intermediate: L.intermediate * lead(b), distal: L.distal * lead(b) };
}

/** Состояние пальца словами по сгибу 0..1. */
export function bendWord(bend) {
  return bend < 0.25 ? "разогнут" : bend < 0.65 ? "полусогнут" : "согнут";
}
