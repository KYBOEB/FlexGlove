"""Разбор сообщений от перчатки.

Форматы определяются автоматически. Основной формат — **значения АЦП** (как в записи
Захара `sensor_data.csv`), битовые строки `00/01/10/11` поддерживаются для совместимости.

Значения (kind="values"):
    "213"                       — один сенсор
    "213;215;210;220;218"       — все сенсоры в одной строке (разделитель ; , пробел | или таб)
    "213.0"                     — дробные значения тоже принимаются
    2·k байт без текста         — k значений uint16 little-endian (например, setValue((uint8_t*)&v, 2))
    4 байта с нулевой старшей половиной — одно значение int32 LE (setValue(int))
    бинарный пакет протокола v0.1 — см. docs/protocol.md

Значения с метками каналов (kind="tagged") — когда каждый сенсор шлёт свою строку:
    "S1:213", "ch2=215", "A0: 210", "3:220", "S1:213 S2:215"
    Метка — номер; приложение упорядочивает встреченные метки и назначает каналы по порядку.

Биты (kind="bits") — первый символ = канал 1, 0 — разогнут, 1 — согнут:
    "01", "1 0", "[1,0]", "bits=10"

Неоднозначные сообщения из одних 0/1 ("1", "10") считаются битами, пока поток не показал,
что это значения (prefer_values=True) — этим управляет ядро (engine.py).
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

MAX_CHANNELS = 8

_NUM = r"-?\d+(?:\.\d+)?"
_TAG_RE = re.compile(r"([A-Za-zА-Яа-я_]*)\s*(\d{1,2})\s*[:=]\s*(" + _NUM + r")")
_SPLIT_RE = re.compile(r"[;,\s|\t]+")
_BITS_RE = re.compile(r"[01]{1,%d}" % MAX_CHANNELS)
_NUM_RE = re.compile(_NUM)


@dataclass(frozen=True)
class Frame:
    """Результат разбора одного сообщения."""

    kind: str                                   # "values" | "tagged" | "bits"
    values: tuple[float, ...] | None = None     # все каналы по порядку
    tags: dict[int, float] = field(default_factory=dict)  # метка -> значение
    bits: tuple[int, ...] | None = None
    seq: int | None = None
    t_ms: int | None = None
    fmt: str = ""

    @property
    def n(self) -> int:
        if self.values is not None:
            return len(self.values)
        if self.bits is not None:
            return len(self.bits)
        return len(self.tags)

    @property
    def combo(self) -> str | None:
        return "".join(str(b) for b in self.bits) if self.bits is not None else None


def bits_from_int(value: int, n: int) -> tuple[int, ...]:
    """Битовая маска -> кортеж состояний, бит 0 = канал 1."""
    return tuple((value >> i) & 1 for i in range(n))


# ---------------- бинарные форматы ----------------
def _parse_binary_v01(data: bytes) -> Frame | None:
    if len(data) < 9 or data[0] != 1:
        return None
    n = data[1]
    if not 1 <= n <= MAX_CHANNELS or len(data) != 9 + 2 * n:
        return None
    _ver, _n, seq, t_ms, bits = struct.unpack_from("<BBHIB", data, 0)
    values = struct.unpack_from(f"<{n}H", data, 9)
    return Frame(kind="values", values=tuple(float(v) for v in values), bits=bits_from_int(bits, n),
                 seq=seq, t_ms=t_ms, fmt="v0.1-bin")


def _is_text(raw: bytes) -> bool:
    if not raw:
        return False
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(c.isprintable() or c in "\r\n\t" for c in s)


def _parse_binary_values(raw: bytes) -> Frame | None:
    if len(raw) == 1:  # один байт — маска пальцев (совместимость с этапом 0)
        return Frame(kind="bits", bits=bits_from_int(raw[0], 2), fmt="byte-mask")
    if len(raw) == 4 and raw[2:] == b"\x00\x00":
        return Frame(kind="values", values=(float(struct.unpack("<i", raw)[0]),), fmt="int32")
    if len(raw) % 2 == 0 and 2 <= len(raw) <= 2 * MAX_CHANNELS:
        k = len(raw) // 2
        return Frame(kind="values", values=tuple(float(v) for v in struct.unpack(f"<{k}H", raw)), fmt=f"uint16x{k}")
    return None


# ---------------- текст ----------------
def _parse_text_line(line: str, prefer_values: bool) -> Frame | None:
    line = line.strip().strip("[](){}")
    if not line or line.startswith("#"):
        return None

    tags = {int(m.group(2)): float(m.group(3)) for m in _TAG_RE.finditer(line)}
    if tags:
        return Frame(kind="tagged", tags=tags, fmt="text-tagged")

    # подпись перед данными: "bits=01", "adc: 213", "value 213"
    body = re.sub(r"^[A-Za-zА-Яа-я_ ]+[:=]?\s*", "", line)
    tokens = [t for t in _SPLIT_RE.split(body) if t]
    if not tokens or not all(_NUM_RE.fullmatch(t) for t in tokens):
        return None

    only01 = all(t in ("0", "1") for t in tokens) or (len(tokens) == 1 and _BITS_RE.fullmatch(tokens[0]))
    if only01 and not prefer_values:
        bits = tuple(int(c) for c in "".join(tokens))
        if len(bits) <= MAX_CHANNELS:
            return Frame(kind="bits", bits=bits, fmt="text-bits")
    if len(tokens) > MAX_CHANNELS:
        return None
    return Frame(kind="values", values=tuple(float(t) for t in tokens), fmt="text-values")


def parse_message(data: bytes | bytearray | str, prefer_values: bool = False) -> list[Frame]:
    """Разбирает одно сообщение (уведомление BLE или строку порта) в список кадров."""
    if isinstance(data, str):
        text = data
    else:
        raw = bytes(data)
        binary = _parse_binary_v01(raw)
        if binary is not None:
            return [binary]
        if not _is_text(raw):
            frame = _parse_binary_values(raw)
            return [frame] if frame else []
        text = raw.decode("utf-8")

    frames = []
    for line in re.split(r"[\r\n]+", text):
        frame = _parse_text_line(line, prefer_values)
        if frame is not None:
            frames.append(frame)
    return frames


def describe_raw(data: bytes | bytearray | str, limit: int = 60) -> str:
    """Человекочитаемое представление сырого сообщения для панели отладки."""
    if isinstance(data, str):
        s = data
    else:
        b = bytes(data)
        s = b.decode("utf-8") if _is_text(b) else "hex: " + b.hex(" ")
    s = s.replace("\r", "\\r").replace("\n", "\\n")
    return s if len(s) <= limit else s[: limit - 1] + "…"
