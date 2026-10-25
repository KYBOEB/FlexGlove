import struct

import pytest

from flexglove.parser import parse_message


# ---------- значения АЦП (основной формат) ----------
@pytest.mark.parametrize("msg, values", [
    (b"213", (213,)), (b"213\r\n", (213,)), (b"213.0", (213,)),
    (b"213;215;210;220;218", (213, 215, 210, 220, 218)),
    (b"213,215,210,220,218\n", (213, 215, 210, 220, 218)),
    (b"213 215\t210", (213, 215, 210)), (b"adc: 213", (213,)),
])
def test_text_values(msg, values):
    (f,) = parse_message(msg)
    assert f.kind == "values" and f.values == tuple(float(v) for v in values)


def test_binary_uint16_values():
    (f,) = parse_message(struct.pack("<5H", 213, 900, 210, 220, 218))
    assert f.kind == "values" and f.values == (213, 900, 210, 220, 218)


def test_binary_single_uint16_and_int32():
    assert parse_message(struct.pack("<H", 213))[0].values == (213,)
    assert parse_message(struct.pack("<i", 213))[0].values == (213,)


def test_binary_v01():
    pkt = struct.pack("<BBHIB", 1, 2, 7, 1000, 0b10) + struct.pack("<2H", 1500, 2500)
    (f,) = parse_message(pkt)
    assert f.fmt == "v0.1-bin" and f.values == (1500, 2500) and f.combo == "01" and f.t_ms == 1000


# ---------- метки каналов ----------
@pytest.mark.parametrize("msg, tags", [
    (b"S1:213", {1: 213}), (b"ch2=215", {2: 215}), (b"A0: 210", {0: 210}), (b"3:220", {3: 220}),
    (b"S1:213 S2:215", {1: 213, 2: 215}),
])
def test_tagged(msg, tags):
    (f,) = parse_message(msg)
    assert f.kind == "tagged" and f.tags == {k: float(v) for k, v in tags.items()}


# ---------- биты (совместимость с этапом 0) ----------
@pytest.mark.parametrize("msg, combo", [(b"01", "01"), (b"1 0", "10"), (b"[1,0]", "10"), ("bits=01", "01"), (b"11111", "11111")])
def test_bits(msg, combo):
    (f,) = parse_message(msg)
    assert f.kind == "bits" and f.combo == combo


def test_ambiguous_01_becomes_values_when_stream_is_values():
    assert parse_message(b"1", prefer_values=True)[0].kind == "values"
    assert parse_message(b"10", prefer_values=True)[0].values == (10,)


def test_single_byte_mask():
    assert parse_message(bytes([1]))[0].combo == "10"


@pytest.mark.parametrize("msg", [b"", b"hello", b"\n\n", b"# debug ch1=213", b"1;2;3;4;5;6;7;8;9"])
def test_garbage(msg):
    assert parse_message(msg) == []
