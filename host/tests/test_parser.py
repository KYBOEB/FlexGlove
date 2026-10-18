import struct

import pytest

from flexglove.parser import parse_message


@pytest.mark.parametrize("msg, combo", [
    (b"01", "01"), (b"10\n", "10"), (b"1 1\r\n", "11"), (b"0,0", "00"),
    (b"[1,0]", "10"), ("bits=01", "01"), (b"11111", "11111"),
])
def test_text_bits(msg, combo):
    frames = parse_message(msg)
    assert len(frames) == 1 and frames[0].combo == combo


def test_four_single_bits_are_not_csv():
    assert parse_message(b"1,0,1,0")[0].combo == "1010"


def test_text_int_mask_bit0_is_channel1():
    assert parse_message(b"2")[0].combo == "01"
    assert parse_message(b"3")[0].combo == "11"


def test_single_raw_byte():
    assert parse_message(bytes([1]))[0].combo == "10"


def test_several_lines_in_one_message():
    assert [f.combo for f in parse_message(b"00\n01\n11\n")] == ["00", "01", "11"]


def test_csv_v01():
    f = parse_message(b"17,12345,5,1200,2100,900\n")[0]
    assert f.fmt == "v0.1-csv" and f.combo == "101" and f.values == (1200, 2100, 900) and f.seq == 17


def test_binary_v01():
    pkt = struct.pack("<BBHIB", 1, 2, 7, 1000, 0b10) + struct.pack("<2H", 1500, 2500)
    f = parse_message(pkt)[0]
    assert f.fmt == "v0.1-bin" and f.combo == "01" and f.values == (1500, 2500) and f.t_ms == 1000


@pytest.mark.parametrize("msg", [b"", b"hello", b"\n\n", b"999"])
def test_garbage(msg):
    assert parse_message(msg) == []
