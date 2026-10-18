"""Разбор сообщений от перчатки.

Поддерживаемые форматы (определяются автоматически):

1. Текст с битами пальцев: "01", "1 0", "1,1", "[1,0]\\n" — первый символ = канал 1.
2. Текстовая строка протокола v0.1 (USB-отладка): "seq,t_ms,bits,v1,...,vn".
3. Бинарный пакет протокола v0.1 (BLE): ver=1 | n_ch | seq u16 | t_ms u32 | bits u8 | values u16*n.
   В числовом поле bits бит 0 = канал 1.
4. Одиночное число 0..255 (текстом "3" или одним байтом 0x03) — битовая маска, бит 0 = канал 1.

Всё, что не распознано, возвращается как пустой список; вызывающая сторона учитывает
это в статистике и показывает «сырое» сообщение в интерфейсе для отладки.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

MAX_CHANNELS = 8

_SEP_RE = re.compile(r"[\s,;:|\[\]\(\)]")
_BITS_RE = re.compile(r"[01]{1,%d}" % MAX_CHANNELS)
_INT_RE = re.compile(r"\d{1,3}")


@dataclass(frozen=True)
class Frame:
    """Одно измерение: состояния пальцев (0 — разогнут, 1 — согнут) и, если есть, напряжения в мВ."""

    bits: tuple[int, ...]
    values: tuple[int, ...] | None = None
    seq: int | None = None
    t_ms: int | None = None
    fmt: str = "text-bits"

    @property
    def n(self) -> int:
        return len(self.bits)

    @property
    def combo(self) -> str:
        return "".join(str(b) for b in self.bits)


def bits_from_int(value: int, n: int) -> tuple[int, ...]:
    """Битовая маска -> кортеж состояний, бит 0 = канал 1."""
    return tuple((value >> i) & 1 for i in range(n))


def _parse_binary_v01(data: bytes) -> Frame | None:
    if len(data) < 9 or data[0] != 1:
        return None
    n = data[1]
    if not 1 <= n <= MAX_CHANNELS or len(data) != 9 + 2 * n:
        return None
    _ver, _n, seq, t_ms, bits = struct.unpack_from("<BBHIB", data, 0)
    values = struct.unpack_from(f"<{n}H", data, 9)
    return Frame(bits=bits_from_int(bits, n), values=tuple(values), seq=seq, t_ms=t_ms, fmt="v0.1-bin")


def _parse_text_line(line: str, default_n: int) -> Frame | None:
    line = line.strip()
    if not line:
        return None

    # Строка протокола v0.1: seq,t_ms,bits,v1..vn (не меньше 4 чисел)
    tokens = [t for t in re.split(r"[,\s;]+", line) if t]
    if len(tokens) >= 4 and all(t.isdigit() for t in tokens) and not all(t in ("0", "1") for t in tokens):
        n = len(tokens) - 3
        if n <= MAX_CHANNELS:
            nums = [int(t) for t in tokens]
            return Frame(
                bits=bits_from_int(nums[2], n),
                values=tuple(nums[3:]),
                seq=nums[0],
                t_ms=nums[1],
                fmt="v0.1-csv",
            )

    cleaned = _SEP_RE.sub("", line)
    # Допускаем префикс-подпись вида "bits=01" или "state01"
    cleaned = re.sub(r"^[A-Za-zА-Яа-я_=\-]+", "", cleaned)
    if _BITS_RE.fullmatch(cleaned):
        return Frame(bits=tuple(int(c) for c in cleaned), fmt="text-bits")
    if _INT_RE.fullmatch(cleaned) and int(cleaned) <= 255:
        return Frame(bits=bits_from_int(int(cleaned), default_n), fmt="text-int")
    return None


def parse_message(data: bytes | bytearray | str, default_n: int = 2) -> list[Frame]:
    """Разбирает одно сообщение (уведомление BLE или строку из порта) в список кадров."""
    if isinstance(data, str):
        text = data
        raw = data.encode("utf-8", "ignore")
    else:
        raw = bytes(data)
        binary = _parse_binary_v01(raw)
        if binary is not None:
            return [binary]
        if len(raw) == 1 and raw[0] < 32:  # один «непечатный» байт = маска
            return [Frame(bits=bits_from_int(raw[0], default_n), fmt="byte")]
        text = raw.decode("utf-8", "ignore")

    frames: list[Frame] = []
    for line in re.split(r"[\r\n]+", text):
        frame = _parse_text_line(line, default_n)
        if frame is not None:
            frames.append(frame)
    return frames


def describe_raw(data: bytes | bytearray | str, limit: int = 48) -> str:
    """Человекочитаемое представление сырого сообщения для панели отладки."""
    if isinstance(data, str):
        s = data
    else:
        b = bytes(data)
        try:
            s = b.decode("utf-8")
            if any(ord(c) < 32 and c not in "\r\n\t" for c in s):
                raise UnicodeDecodeError("utf-8", b, 0, 1, "control")
        except UnicodeDecodeError:
            s = "hex: " + b.hex(" ")
    s = s.replace("\r", "\\r").replace("\n", "\\n")
    return s if len(s) <= limit else s[: limit - 1] + "…"
