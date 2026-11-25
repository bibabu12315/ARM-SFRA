"""通信协议测试（基线：docs/03_通信协议.md）。

这组测试的一个隐藏价值：**把 payload 字节数锁死**。
D20 禁止结构体 memcpy 的原因就是"编译器填充不可控"，而填充是
编译期决定的、运行时看不出异常——只有断言 `len(payload) == N` 才挡得住。
S3 gate 要求"帧编解码双向 100% 一致"，靠的就是这里。
"""
import struct

import pytest

from app.comm.protocol import (CAPS_SIZE, CONFIG_SIZE, COND_SIZE, DATA_HDR_SIZE,
                               DATA_MAX_SIZE, MAX_POINTS_PER_FRAME,
                               POINT_BYTES, STATUS_SIZE, Caps, Cmd, DataChunk,
                               ErrCode, Flags, Frame, FrameParser, State,
                               StatusInfo, build_frame, crc16, pack_coeff,
                               pack_conditions, pack_config, pack_get_data,
                               pack_ping_ack, unpack_coeff, unpack_conditions,
                               unpack_config, unpack_get_data, unpack_ping_ack)


# ---------------------------------------------------------------- CRC
def test_crc16_modbus_known_value():
    """CRC16-Modbus 标准测试向量：'123456789' → 0x4B37。"""
    assert crc16(b"123456789") == 0x4B37


def test_crc16_empty():
    assert crc16(b"") == 0xFFFF


def test_crc16_changes_with_data():
    assert crc16(b"\x01\x02") != crc16(b"\x01\x03")


# ---------------------------------------------------------------- 组帧
def test_build_frame_layout():
    f = build_frame(Cmd.PING)
    # SOF(2) + LEN(1) + CMD(1) + CRC(2) = 6 字节
    assert len(f) == 6
    assert f[:2] == b"\xaa\x55"
    assert f[2] == 1                 # LEN = CMD + payload = 1
    assert f[3] == Cmd.PING


def test_build_frame_with_payload():
    payload = b"\x10\x20\x30\x40"
    f = build_frame(Cmd.SET_CONFIG, payload)
    # SOF(2) + LEN(1) + CMD(1) + payload(4) + CRC(2)
    assert len(f) == 2 + 1 + 1 + 4 + 2
    assert f[2] == 5                 # LEN = CMD + payload = 1 + 4
    assert f[3] == Cmd.SET_CONFIG
    assert f[4:8] == payload


def test_build_frame_crc_covers_len_not_sof():
    """CRC 范围 = LEN..PAYLOAD 末，**不含 AA 55**（03 文档 §3.6）。"""
    payload = b"\x11\x22"
    f = build_frame(Cmd.PING, payload)
    body = f[2:-2]                   # 从 LEN 到 payload 末
    assert crc16(body) == struct.unpack_from("<H", f, len(f) - 2)[0]
    # 反证：把 SOF 也算进去就得不到同一个值
    assert crc16(f[:2] + body) != struct.unpack_from("<H", f, len(f) - 2)[0]


def test_build_frame_rejects_oversize():
    with pytest.raises(ValueError):
        build_frame(Cmd.SET_CONFIG, b"\x00" * 300)


# ---------------------------------------------------------------- 解帧
def test_roundtrip():
    p = FrameParser()
    frames = p.feed(build_frame(Cmd.DATA, b"\x01\x02\x03"))
    assert len(frames) == 1
    assert frames[0].cmd == Cmd.DATA
    assert frames[0].payload == b"\x01\x02\x03"


def test_half_frame_then_rest():
    """921600 下最常见：一次只来半条。"""
    p = FrameParser()
    raw = build_frame(Cmd.ACK, b"\x11\x00")
    assert p.feed(raw[:3]) == []
    frames = p.feed(raw[3:])
    assert len(frames) == 1
    assert frames[0].payload == b"\x11\x00"


def test_sticky_packets():
    """粘包：一次收到 3 条完整帧。"""
    p = FrameParser()
    data = b"".join(build_frame(Cmd.PING) for _ in range(3))
    assert len(p.feed(data)) == 3


def test_garbage_prefix_is_skipped():
    p = FrameParser()
    frames = p.feed(b"\x00\x11\x22" + build_frame(Cmd.PING))
    assert len(frames) == 1
    assert p.resyncs >= 1


def test_bad_crc_is_rejected():
    p = FrameParser()
    raw = bytearray(build_frame(Cmd.PING))
    raw[-1] ^= 0xFF
    assert p.feed(bytes(raw)) == []
    assert p.crc_errors >= 1


def test_fake_sof_inside_payload():
    """载荷里出现 AA 55 不能误判成帧头。"""
    p = FrameParser()
    payload = b"\xaa\x55\xaa\x55"
    frames = p.feed(build_frame(Cmd.DATA, payload))
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_aa_aa_55_resync():
    """`AA AA 55` 序列：第二个 AA 才是真帧头（03 文档 §3.6 第 3 条）。"""
    p = FrameParser()
    good = build_frame(Cmd.PING)
    frames = p.feed(b"\xaa" + good)         # 前导一个假 AA
    assert len(frames) == 1
    assert frames[0].cmd == Cmd.PING


def test_byte_by_byte():
    """最严苛：一个字节一个字节喂。"""
    p = FrameParser()
    raw = build_frame(Cmd.STATUS, b"\x02" * 10)
    got = []
    for i in range(len(raw)):
        got += p.feed(raw[i:i + 1])
    assert len(got) == 1


def test_large_frame():
    """DATA 帧最大 143 B，要能一次解出来。"""
    p = FrameParser()
    ch = DataChunk(0, [(f * 1.0, 1.0, 2.0, 3.0, 4.0)
                       for f in range(MAX_POINTS_PER_FRAME)])
    payload = ch.pack()
    assert len(payload) == DATA_MAX_SIZE
    frames = p.feed(build_frame(Cmd.DATA, payload))
    assert len(frames) == 1
    back = DataChunk.unpack(frames[0].payload)
    assert back is not None and len(back.points) == MAX_POINTS_PER_FRAME


def test_reset_clears_buffer():
    p = FrameParser()
    p.feed(b"\xaa\x55\x10")
    p.reset()
    assert len(p.buf) == 0


# ---------------------------------------------------------------- SET_CONFIG
def test_config_size_is_16():
    """03 文档 §3.3 定版 17 B（3×f32 + u16 + 3×u8）。这个断言就是 D20 的守门员。"""
    raw = pack_config(100.0, 1.122, 0.01, 68, 1, 0)
    assert len(raw) == CONFIG_SIZE == 17


def test_pack_unpack_config():
    raw = pack_config(freq_start=100.0, freq_step=10 ** 0.05,
                      amplitude=0.01, vec_len=68, cycles_mode=1, mode=0)
    d = unpack_config(raw)
    assert d is not None
    assert d["freq_start"] == pytest.approx(100.0)
    assert d["freq_step"] == pytest.approx(10 ** 0.05, rel=1e-6)
    assert d["amplitude"] == pytest.approx(0.01)
    assert d["vec_len"] == 68
    assert d["cycles_mode"] == 1
    assert d["mode"] == 0


def test_config_field_offsets():
    """逐字段断言偏移，防止有人"顺手"调换顺序。"""
    raw = pack_config(1.0, 2.0, 3.0, 4, 1, 0)
    assert struct.unpack_from("<f", raw, 0)[0] == pytest.approx(1.0)
    assert struct.unpack_from("<f", raw, 4)[0] == pytest.approx(2.0)
    assert struct.unpack_from("<f", raw, 8)[0] == pytest.approx(3.0)
    assert struct.unpack_from("<H", raw, 12)[0] == 4
    assert raw[14] == 1              # cycles_mode
    assert raw[15] == 0              # mode


def test_pack_config_rejects_bad_args():
    with pytest.raises(ValueError):
        pack_config(100, 1.1, 0.01, 0, 1, 0)          # vec_len=0
    with pytest.raises(ValueError):
        pack_config(100, 1.1, 0.01, 68, 3, 0)         # cycles_mode 越界
    with pytest.raises(ValueError):
        pack_config(100, 1.1, 0.01, 68, 1, 2)         # mode 越界
    with pytest.raises(ValueError):
        pack_config(100, 0.0, 0.01, 68, 1, 0)         # freq_step 必须 > 0


def test_unpack_config_short():
    assert unpack_config(b"\x00") is None


# ---------------------------------------------------------------- CAPS
def test_caps_size_is_16():
    c = Caps(isr_freq=100000.0, max_pts_per_dec=20, max_vec_len=256,
             default_amp=0.01, fw_ver=b"\x01\x00\x01\x00")
    assert len(c.pack()) == CAPS_SIZE == 16


def test_caps_roundtrip():
    c = Caps(100000.0, 20, 0, 256, 0.01, b"\x01\x00\x01\x00")
    b = Caps.unpack(c.pack())
    assert b is not None
    assert b.isr_freq == pytest.approx(100000.0)
    assert b.max_pts_per_dec == 20
    assert b.max_vec_len == 256
    assert b.default_amp == pytest.approx(0.01)
    assert b.version_str == "v1.0.1"


def test_caps_field_offsets():
    c = Caps(1000.0, 7, 0, 99, 0.5, b"ABCD")
    raw = c.pack()
    assert struct.unpack_from("<f", raw, 0)[0] == pytest.approx(1000.0)
    assert raw[4] == 7
    assert raw[5] == 0
    assert struct.unpack_from("<H", raw, 6)[0] == 99
    assert struct.unpack_from("<f", raw, 8)[0] == pytest.approx(0.5)
    assert raw[12:16] == b"ABCD"


# ---------------------------------------------------------------- PING_ACK
def test_ping_ack_roundtrip():
    raw = pack_ping_ack(1, 1, 0b0101)
    assert len(raw) == 4
    assert unpack_ping_ack(raw) == (1, 1, 0b0101)


def test_ping_ack_short():
    assert unpack_ping_ack(b"\x01") is None


# ---------------------------------------------------------------- STATUS
def test_status_size_is_10():
    st = StatusInfo(state=2, flags=0, freq_index=5, pass_idx=0,
                    reserved=0, cur_f_act=1234.5)
    assert len(st.pack()) == STATUS_SIZE == 10


def test_status_roundtrip():
    st = StatusInfo(State.COLLECT, Flags.INJ_OFF, 42, 0, 0, 1234.5)
    back = StatusInfo.unpack(st.pack())
    assert back is not None
    assert back.state == State.COLLECT
    assert back.flags == Flags.INJ_OFF
    assert back.freq_index == 42
    assert back.cur_f_act == pytest.approx(1234.5)
    assert back.state_name == "COLLECT"


def test_status_done_and_saturated():
    assert StatusInfo(flags=Flags.DONE).done
    assert StatusInfo(state=State.DONE).done
    assert StatusInfo(flags=Flags.DUTY_SAT).saturated
    assert StatusInfo(flags=Flags.ADC_SAT).saturated
    assert not StatusInfo(flags=Flags.INJ_OFF).saturated


def test_status_field_offsets():
    st = StatusInfo(3, 0x08, 0x1234, 0, 0, 2.5)
    raw = st.pack()
    assert raw[0] == 3
    assert raw[1] == 0x08
    assert struct.unpack_from("<H", raw, 2)[0] == 0x1234
    assert raw[4] == 0
    assert raw[5] == 0
    assert struct.unpack_from("<f", raw, 6)[0] == pytest.approx(2.5)


# ---------------------------------------------------------------- DATA
def test_data_chunk_roundtrip():
    pts = [(100.0, 1.0, 2.0, 3.0, 4.0),
           (110.0, -1.0, -2.0, -3.0, -4.0)]
    ch = DataChunk(start_idx=10, points=pts)
    back = DataChunk.unpack(ch.pack())
    assert back is not None
    assert back.start_idx == 10
    assert back.end_idx == 12
    assert len(back.points) == 2
    assert back.points[0][0] == pytest.approx(100.0)
    assert back.points[1][4] == pytest.approx(-4.0)


def test_data_chunk_header_offsets():
    ch = DataChunk(start_idx=0x1234, points=[(1.0, 2.0, 3.0, 4.0, 5.0)])
    raw = ch.pack()
    assert struct.unpack_from("<H", raw, 0)[0] == 0x1234
    assert raw[2] == 1
    assert len(raw) == DATA_HDR_SIZE + POINT_BYTES == 23


def test_data_chunk_rejects_too_many_points():
    ch = DataChunk(0, [(0.0,) * 5] * (MAX_POINTS_PER_FRAME + 1))
    with pytest.raises(ValueError):
        ch.pack()


def test_data_chunk_unpack_rejects_bad_count():
    """count 字段被篡改成超过上限时必须拒绝，否则会越界读。"""
    raw = struct.pack("<HB", 0, MAX_POINTS_PER_FRAME + 3) + b"\x00" * 200
    assert DataChunk.unpack(raw) is None


def test_get_data_roundtrip():
    raw = pack_get_data(3, 7)
    assert len(raw) == DATA_HDR_SIZE == 3
    assert unpack_get_data(raw) == (3, 7)


def test_get_data_rejects_bad_count():
    with pytest.raises(ValueError):
        pack_get_data(0, 0)
    with pytest.raises(ValueError):
        pack_get_data(0, MAX_POINTS_PER_FRAME + 1)


# ---------------------------------------------------------------- COEFF
def test_coeff_roundtrip():
    co = [0.5, 0.01, 1e-4]
    raw = pack_coeff(co)
    assert raw[0] == 3
    assert len(raw) == 1 + 3 * 4
    assert unpack_coeff(raw) == pytest.approx(co)


def test_coeff_rejects_bad_order():
    with pytest.raises(ValueError):
        pack_coeff([])
    with pytest.raises(ValueError):
        pack_coeff([1.0] * 5)


def test_coeff_unpack_short():
    assert unpack_coeff(b"") is None
    assert unpack_coeff(b"\x04" + b"\x00" * 8) is None   # 声称 4 个但不够


# ---------------------------------------------------------------- CONDITIONS
def test_conditions_size_is_32():
    raw = pack_conditions(vin=24.0, vout=12.0, iout=2.0, fs=100000.0)
    assert len(raw) == COND_SIZE == 32


def test_conditions_roundtrip():
    d = unpack_conditions(pack_conditions(
        vin=24.0, vout=12.0, iout=2.0, fs=100000.0,
        deadtime=100e-9, temp=45.0, adc_fs_v=3.3, shunt_gain=0.01))
    assert d is not None
    assert d["vin"] == pytest.approx(24.0)
    assert d["vout"] == pytest.approx(12.0)
    assert d["shunt_gain"] == pytest.approx(0.01)


# ---------------------------------------------------------------- 枚举
def test_cmd_names_unique():
    vals = [c.value for c in Cmd]
    assert len(vals) == len(set(vals))


def test_ack_cmd_is_request_or_0x80():
    """应答命令的编号约定：请求 | 0x80（03 文档 §3.2）。"""
    assert Cmd.PING_ACK == (Cmd.PING | 0x80)
    assert Cmd.CAPS == (Cmd.GET_CAPS | 0x80)
    assert Cmd.STATUS == (Cmd.GET_STATUS | 0x80)
    assert Cmd.DATA == (Cmd.GET_DATA | 0x80)
    assert Cmd.COEFF == (Cmd.SET_COEFF | 0x80)
    assert Cmd.CONDITIONS == (Cmd.GET_CONDITIONS | 0x80)


def test_frame_cmd_name():
    assert Frame(cmd=Cmd.ACK, payload=b"").cmd_name == "ACK"
    assert Frame(cmd=0xEE, payload=b"").cmd_name == "0xEE"


def test_err_code_names():
    assert ErrCode.name_of(0x00) == "OK"
    assert ErrCode.name_of(0x05) == "INDEX_RANGE"
    assert ErrCode.name_of(0x99) == "0x99"
