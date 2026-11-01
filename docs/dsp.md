# Модуль `dsp/` — как устроена обработка сигналов и как добавить свой блок

Код: `host/flexglove/dsp/`. Решения: D-18 (блоки и конвейеры), D-22 (время отсчётов), D-23 (пресеты).

```
сырое АЦП ─▶ Pipeline (filters.py) ─▶ калибровка + сгиб 0..1 ─▶ Hysteresis ─▶ биты ─▶ распознаватели ─▶ события
             median / hampel / ema /    (calibration.py,           (recognizers/       (recognizers/: combo — D-21,
             one_euro / biquad_lp /      normalize.py)              threshold.py)       threshold, будущие knn_pose,
             deadband                                                                    dynamic)
                    └──▶ features.py / segmentation.py (покой / движение)
IMU v0.3 ─▶ orientation.py: смещение гироскопа ─▶ комплементарный фильтр / Madgwick ─▶ кватернион, углы
```

| Файл | Что внутри |
|---|---|
| `filters.py` | интерфейс `Filter`, реестр `@register`, блоки, `Pipeline.from_config / to_config`, `SampleClock` |
| `presets.py` | пресеты «Базовый», «One-Euro», «Сильное сглаживание», `resolve()` |
| `calibration.py`, `normalize.py` | ручная и автоматическая калибровка, сгиб 0..1 |
| `features.py`, `segmentation.py` | признаки по окну, детектор «покой / движение» |
| `recognizers/` | `Sample` → `update()` → `[Event]`; threshold, combo (D-21), заготовки knn_pose, dynamic |
| `orientation.py` | кватернионы, `GyroBiasEstimator`, `ComplementaryFilter`, `MadgwickFilter`, `OrientationEstimator` |
| `synthetic.py` | генератор сигналов и IMU для тестов и `tools/evaluate.py` |

`processing.py` (канал целиком) и `commands.py` — тонкие обёртки с прежним интерфейсом.

## Свой фильтр за 10 минут

**1. Класс** — в `dsp/filters.py` (или в своём файле, который импортируется из `dsp/__init__.py`):

```python
@register("slew")                       # имя для JSON
class SlewLimit(Filter):
    """Ограничение скорости: выход меняется не быстрее max_rate единиц в секунду."""

    def __init__(self, max_rate: float = 2000.0):        # все параметры — именованные, с умолчаниями
        if float(max_rate) <= 0:
            raise ValueError("slew: max_rate > 0")       # понятная ошибка → увидит пользователь в интерфейсе
        self._params = {"max_rate": float(max_rate)}     # попадёт в to_config() и в отчёт evaluate
        self._clock = SampleClock()                      # шаг времени — оценка периода, не интервалы BLE (D-22)
        self.reset()

    def reset(self) -> None:
        self._y = None
        self._clock.reset()

    def process(self, x: float, t: float) -> float:
        dt = self._clock.tick(t)
        if self._y is None:
            self._y = x
        else:
            step = self._params["max_rate"] * dt
            self._y += max(-step, min(step, x - self._y))
        return self._y
```

Правила:
- на постоянном входе выход должен совпадать со входом (коэффициент передачи 1), иначе сдвинется калибровка.
  Это автоматически проверяет `test_constant_input_passes_unchanged` для всех зарегистрированных фильтров;
- никакого numpy/scipy в `host/flexglove` — только стандартная библиотека (приложение ставится офлайн);
- время `t` — секунды приёма на ПК; для шага используйте `SampleClock`, окна задавайте в отсчётах.

**2. Тест** — `host/tests/test_dsp_filters.py`: известный вход → известный выход, плюс свойство фильтра
(«ступенька проходит не быстрее max_rate»). Общие тесты (постоянный вход, повторяемость после `reset`)
подхватят новый фильтр сами.

**3. Проверка на записи:**

```
python tools/evaluate.py data/recordings/запись.csv --compare '[{"type":"hampel","n":7},{"type":"slew","max_rate":3000}]'
```

**4. Попробовать в приложении:** «Сигналы → Фильтрация сигнала → Свой конвейер (JSON)» → вставить то же описание →
«Применить». Если блок хорош для всех — добавить пресет в `dsp/presets.py` (название, подсказка, описание).

## Свой распознаватель

```python
from flexglove.dsp.recognizers.base import Event, Recognizer, Sample

class LongFistRecognizer(Recognizer):
    """Событие "long_fist", если все пальцы согнуты дольше hold_s."""
    name = "long_fist"

    def __init__(self, hold_s: float = 2.0):
        self.hold_s = hold_s
        self.reset()

    def reset(self) -> None:
        self._since = None
        self._fired = False

    def update(self, sample: Sample) -> list[Event]:
        fist = bool(sample.bits) and all(sample.bits)
        if not fist:
            self._since, self._fired = None, False
            return []
        if self._since is None:
            self._since = sample.t
        if not self._fired and sample.t - self._since >= self.hold_s:
            self._fired = True
            return [Event("long_fist", sample.t)]
        return []
```

`Sample` содержит `t`, `bends`, `bits`, `grip`, `imu`, `context` (например `armed`, `bindings`). Тест — список
`Sample` с известным временем → ожидаемые события (пример — `host/tests/test_dsp_recognizers.py`).
Подключение в ядро — в `engine.py` рядом с `self.commands` (там строится `Sample` каждый такт).

## Как выбирать параметры
- **Медиана vs Хампель.** Медиана надёжно убирает выбросы, но задерживает фронт на (n − 1)/2 отсчёта. Хампель
  трогает только выбросы — фронт без задержки (`docs/analysis/2026-11-01-dsp-presets.md`).
- **One-Euro:** `min_cutoff` (Гц) — гладкость в покое (меньше — глаже), `beta` — насколько быстро растёт частота
  среза со скоростью (больше — меньше задержка, больше дрожание при движении). `beta` зависит от масштаба сигнала
  в единицах АЦП: для размаха ≈ 450 хороша 0,02, для слабых сенсоров её стоит поднять.
- **Биквад** — классический ФНЧ 2-го порядка с предсказуемой частотной характеристикой (−3 дБ на `cutoff_hz`,
  −12 дБ на октаву), но с постоянной задержкой ≈ 1/(2π·fc)·√2.
