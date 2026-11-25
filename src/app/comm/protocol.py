"""SFRA Studio —— 串口协议（**唯一基线：docs/03_通信协议.md**）。

设计约束（都是踩过坑总结的，改之前先看 03 文档 §3.6）：

1. **不做结构体 memcpy**（D20）。编译器会在 `u8` 和 `f32` 之间插填充，
   PC 端 MSVC 与 MCU 端 GCC 的填充规则还可能不同。这里每个字段显式
   按小端 pack/unpack，字节数在 `test_protocol.py` 里逐个断言。
2. **LEN 是 1 字节**，LEN = 1 + payload_len（只含 CMD 与 payload，
   **不含帧头也不含 CRC**）。整帧长度 = 2 + 1 + LEN + 2。
   早期写成 u16 LEN 且含 reserved，与固件对不上——这是重写修掉的主要问题。
3. **CRC 只覆盖 LEN..PAYLOAD**，不含 `AA 55` 帧头，初值 0xFFFF，
   多项式 0xA001（反射），LSB 先发。
4. **相位约定不在这里实现**：本模块只搬字节，复数怎么算全部在
   `model/bode_data.py`（D18）。

帧格式：

        0xAA  0x55  LEN  CMD  payload[0:LEN-1]  CRC16(2, LSB first)
        |---- 不计入 CRC ----|      |---- CRC 范围 ----|
                     |<-------- LEN -------->|
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Tuple

# ---------------------------------------------------------------- 常量
SOF_BYTES = b"\xaa\x55"
#: 帧头 SOF(2) + LEN(1)；CMD 属于 body，与固件 sfra_proto.c 口径一致
HEADER_LEN = 3
LEN_MIN = 1                    # 只有 CMD
LEN_MAX = 255
#: payload 上限 = LEN 上限 - CMD 1 字节
MAX_PAYLOAD = LEN_MAX - 1      # 254


class Cmd(IntEnum):
    """命令表（03 文档 §3.3）。应答 CMD = 请求 | 0x80。"""

    # PC → MCU
    PING = 0x01
    GET_CAPS = 0x02
    SET_CONFIG = 0x03
    START = 0x04
    ABORT = 0x05
    GET_STATUS = 0x06
    GET_DATA = 0x07
    SET_COEFF = 0x08
    GET_COEFF = 0x09
    GET_CONDITIONS = 0x0A
    # MCU → PC
    PING_ACK = 0x81
    CAPS = 0x82
    STATUS = 0x86
    DATA = 0x87
    COEFF = 0x88
    CONDITIONS = 0x8A
    # 通用应答（任何请求都可能收到）
    ACK = 0x80
    NACK = 0xFF


class ErrCode(IntEnum):
    """错误码（03 文档 §3.3 表）。"""
    OK = 0x00
    BAD_CRC = 0x01
    BAD_PARAM = 0x02
    BUSY = 0x03
    NOT_CONFIGURED = 0x04
    INDEX_RANGE = 0x05
    WRONG_STATE = 0x06

    @classmethod
    def name_of(cls, code: int) -> str:
        try:
            return cls(code).name
        except ValueError:
            return f"0x{code:02X}"


class State(IntEnum):
    """MCU 状态机（02 文档 §2.2.1）。"""
    IDLE = 0
    SETTLE = 1
    COLLECT = 2
    NEXT = 3
    DONE = 4
    ABORT = 5

    @classmethod
    def name_of(cls, code: int) -> str:
        try:
            return cls(code).name
        except ValueError:
            return f"?{code}"


class Flags(IntEnum):
    """STATUS.flags 的位（03 文档 §3.3）。"""
    DUTY_SAT = 0x01
    ADC_SAT = 0x02
    INJ_OFF = 0x04
    DONE = 0x08


class Mode(IntEnum):
    PLANT = 0
    LOOP = 1


class CycMode(IntEnum):
    FAST = 0
    NORMAL = 1
    PRECISE = 2


# ---------------------------------------------------------------- CRC
def _crc_table() -> Tuple[int, ...]:
    tab = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        tab.append(c)
    return tuple(tab)


_CRC_TAB = _crc_table()


def crc16(data: bytes) -> int:
    """CRC16-Modbus：初值 0xFFFF，反射多项式 0xA001。

    标准测试向量：`crc16(b"123456789") == 0x4B37`。
    """
    c = 0xFFFF
    for b in data:
        c = (c >> 8) ^ _CRC_TAB[(c ^ b) & 0xFF]
    return c


# ---------------------------------------------------------------- 组帧
def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """组一帧。

        AA 55 | LEN(1B) | CMD | payload | CRC_lo CRC_hi
        └2B┘   └──────── CRC 覆盖范围 ────────┘

    LEN = 1 + payload_len，即从 CMD 起到 payload 末的字节数。
    最小 LEN = 1（只有 CMD，无 payload），此时整帧 7 字节。

    **CRC 不含 `AA 55` 帧头**，只算 LEN + CMD + payload。
    这一点两边如果理解不一致，表现是"偶发误判错帧"——因为帧头字节
    参与/不参与 CRC 只在某些数据下才碰巧对上，很难查（03 文档 §3.6）。
    """
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload 过长：{len(payload)} > {MAX_PAYLOAD}")
    body = struct.pack("<B", int(cmd)) + payload
    length = len(body)                       # = 1 + payload_len
    if length > 255:
        raise ValueError("LEN 溢出：payload 超过 LEN 字段能表示的范围")
    crc = crc16(struct.pack("<B", length) + body)
    return SOF_BYTES + struct.pack("<B", length) + body + struct.pack("<H", crc)


@dataclass
class Frame:
    cmd: int
    payload: bytes
    raw: bytes = b""

    @property
    def cmd_name(self) -> str:
        try:
            return Cmd(self.cmd).name
        except ValueError:
            return f"0x{self.cmd:02X}"


# ---------------------------------------------------------------- 解帧
@dataclass
class FrameParser:
    """增量解帧器，喂多少字节都行。

    三个必须处理的情况（03 文档 §3.6）：
      1. **半帧**：921600 一次 readyRead 可能只到半条，必须缓冲。
      2. **粘包**：一次来好几条，循环取到取不出为止。
      3. **假帧头**：`AA AA 55` 这种序列，第二字节不是 55 时要把当前
         字节当新的 HEAD1 再判一次，否则会漏掉真头（这是 v1 的 bug）。

    这里的实现用 `find(SOF_BYTES)` 代替逐字节状态机，天然满足第 3 条；
    丢帧时只前进 1 字节而不是跳过整条，保证"假头后面的真头"能被找到。
    """
    buf: bytearray = field(default_factory=bytearray)
    crc_errors: int = 0
    resyncs: int = 0
    oversize: int = 0

    def reset(self) -> None:
        self.buf.clear()

    def feed(self, data: bytes) -> List[Frame]:
        self.buf.extend(data)
        out: List[Frame] = []
        while True:
            f = self._try_one()
            if f is None:
                break
            if f is not False:
                out.append(f)
        return out

    def _try_one(self):
        """返回 Frame / None（数据不够）/ False（丢了一字节，继续找）。"""
        b = self.buf
        if len(b) < 2:
            return None

        idx = b.find(SOF_BYTES)
        if idx < 0:
            # 没有帧头。保留最后 1 字节（可能是 AA），其余全是垃圾。
            if len(b) > 1:
                del b[: len(b) - 1]
                self.resyncs += 1
            return None
        if idx > 0:
            del b[:idx]
            self.resyncs += 1

        if len(b) < HEADER_LEN:
            return None

        length = b[2]                      # 1 字节 LEN
        if length < 1:
            del b[:1]
            self.resyncs += 1
            return False

        total = HEADER_LEN + length + 2    # + CRC
        if len(b) < total:
            return None

        body = bytes(b[HEADER_LEN:HEADER_LEN + length])
        crc_rx = struct.unpack_from("<H", b, HEADER_LEN + length)[0]
        if crc16(struct.pack("<B", length) + body) != crc_rx:
            self.crc_errors += 1
            del b[:1]                      # 假帧头，只前进一字节
            self.resyncs += 1
            return False

        raw = bytes(b[:total])
        del b[:total]
        return Frame(cmd=body[0], payload=body[1:], raw=raw)


# ---------------------------------------------------------------- 载荷：SET_CONFIG
# 03 文档 §3.3：freq_start(f32) freq_step(f32) amplitude(f32) vec_len(u16)
#                cycles_mode(u8) mode(u8) reserved(u8)     共 17 B
#
# **为什么是 17 B 而不是文档原表的 12 B**：3×f32(12) + u16(2) + u8(1) = 15，
# 原表写 12 是算错了；再加 `reserved(u8)` 得 17。**payload 逐字段
# unpack（struct.unpack_from），不做整块 memcpy，所以 17 这个奇数长度
# 完全没问题**——这也是 D20 禁止 memcpy 之后顺带得到的自由度。
# 固件 sfra_proto.c 的 do_set_config 已按 17 B 校验，文档同步见 03 §3.3。
_CONFIG_FMT = "<fffHBBB"
CONFIG_SIZE = struct.calcsize(_CONFIG_FMT)      # 17


def pack_config(freq_start: float, freq_step: float, amplitude: float,
                vec_len: int, cycles_mode: int, mode: int,
                reserved: int = 0) -> bytes:
    """打包 SET_CONFIG（17 字节，小端，无填充）。

    不要按结构体顺序直觉写 `u16, u8, u8, f32...`——那会引入 3 字节填充。
    这里的顺序与 03 文档表逐字一致：三个 f32 在前，然后 u16、三个 u8。
    """
    if not 1 <= vec_len <= 4096:
        raise ValueError("vec_len 必须在 1~4096")
    if cycles_mode not in (0, 1, 2):
        raise ValueError("cycles_mode 必须是 0/1/2")
    if mode not in (0, 1):
        raise ValueError("mode 必须是 0(PLANT)/1(LOOP)")
    if freq_step <= 0.0:
        raise ValueError("freq_step 必须 > 0")
    return struct.pack(_CONFIG_FMT, float(freq_start), float(freq_step),
                       float(amplitude), int(vec_len), int(cycles_mode),
                       int(mode), int(reserved))


def unpack_config(payload: bytes) -> Optional[dict]:
    if len(payload) < CONFIG_SIZE:
        return None
    fs, st, amp, n, cyc, mode, rsv = struct.unpack_from(_CONFIG_FMT, payload, 0)
    return {"freq_start": fs, "freq_step": st, "amplitude": amp,
            "vec_len": n, "cycles_mode": cyc, "mode": mode, "reserved": rsv}


# ---------------------------------------------------------------- 载荷：CAPS
_CAPS_FMT = "<fBBHf4s"
CAPS_SIZE = struct.calcsize(_CAPS_FMT)          # 16


@dataclass
class Caps:
    """MCU 能力与默认参数（03 文档 §3.3，16 B payload，无填充）。

    字段顺序刻意是 `f32, u8, u8, u16, f32, 4B`：
    **不能**按 C 结构体 `u8+u16` 的顺序写，否则编译器会插 1 字节填充，
    PC 解出来的 `max_vec_len` 就是垃圾——这正是 D20 要禁止 memcpy 的原因。

    cap 位定义（PING_ACK 的 cap 字段同义）：
        bit0 = 支持 float 路径
        bit1 = 支持 Q15 定点路径
        bit2 = 有 Vin/Vout/Iout 回读
        bit3 = 保留（D19 改单 pass 后"双 pass"位已无意义）
    """
    isr_freq: float = 0.0
    max_pts_per_dec: int = 0
    reserved: int = 0               # 旧固件恒 0；新固件此字节 = eff_points
    max_vec_len: int = 0
    default_amp: float = 0.0
    fw_ver: bytes = b"\x00\x00\x00\x00"

    @property
    def eff_points(self) -> int:
        """固件按默认配置扫一遍的真实点数（0 = 旧固件，不报这个值）。

        为什么需要它：上位机按**界面填的终点**算点数、按 f_s/5 截断，
        固件在 START 里还会用 `sfra_effective_points()` 再裁一次。两处
        口径只要差一点，就会重演"界面承诺 47 点、MCU 只扫 28 点"，
        然后上位机死循环请求第 28 点之后的数据（满屏 INDEX_RANGE）。
        所以以固件报的值为准。
        """
        return int(self.reserved)

    def pack(self) -> bytes:
        return struct.pack(_CAPS_FMT, self.isr_freq, self.max_pts_per_dec,
                           self.reserved, self.max_vec_len,
                           self.default_amp, self.fw_ver[:4].ljust(4, b"\x00"))

    @staticmethod
    def unpack(p: bytes) -> Optional["Caps"]:
        if len(p) < CAPS_SIZE:
            return None
        isr, ppd, rsv, mlen, amp, ver = struct.unpack_from(_CAPS_FMT, p, 0)
        return Caps(isr, ppd, rsv, mlen, amp, ver)

    @property
    def version_str(self) -> str:
        v = self.fw_ver
        return f"v{v[0]}.{v[1]}.{v[2]}"


# ---------------------------------------------------------------- 载荷：PING_ACK
_PING_ACK_FMT = "<BBH"
PING_ACK_SIZE = struct.calcsize(_PING_ACK_FMT)   # 4


def pack_ping_ack(ver: int, proto: int, caps_bits: int) -> bytes:
    return struct.pack(_PING_ACK_FMT, int(ver), int(proto), int(caps_bits))


def unpack_ping_ack(p: bytes) -> Optional[Tuple[int, int, int]]:
    if len(p) < PING_ACK_SIZE:
        return None
    return struct.unpack_from(_PING_ACK_FMT, p, 0)


# ---------------------------------------------------------------- 载荷：STATUS
# state(u8) flags(u8) freq_index(u16) pass(u8) reserved(u8) cur_f_act(f32) = 10 B
_STATUS_FMT = "<BBHBBf"
STATUS_SIZE = struct.calcsize(_STATUS_FMT)       # 10


@dataclass
class StatusInfo:
    """扫频进度（03 文档 §3.3）。

    `pass` 字段在 D19 改单 pass 后已经失去意义，恒为 0，但协议保留
    （固件版本混用时不至于错位）。上位机进度直接用 `freq_index / vec_len`，
    不要再乘 2——那是 v2 双 pass 时代的公式。
    """
    state: int = 0
    flags: int = 0
    freq_index: int = 0
    pass_idx: int = 0
    reserved: int = 0
    cur_f_act: float = 0.0

    def pack(self) -> bytes:
        return struct.pack(_STATUS_FMT, self.state, self.flags,
                           self.freq_index, self.pass_idx,
                           self.reserved, self.cur_f_act)

    @staticmethod
    def unpack(p: bytes) -> Optional["StatusInfo"]:
        if len(p) < STATUS_SIZE:
            return None
        st, fl, idx, pi, rsv, f = struct.unpack_from(_STATUS_FMT, p, 0)
        return StatusInfo(st, fl, idx, pi, rsv, f)

    @property
    def done(self) -> bool:
        return bool(self.flags & Flags.DONE) or self.state == State.DONE

    @property
    def saturated(self) -> bool:
        return bool(self.flags & (Flags.DUTY_SAT | Flags.ADC_SAT))

    @property
    def state_name(self) -> str:
        return State.name_of(self.state)


# ---------------------------------------------------------------- 载荷：DATA
# start_idx(u16) count(u8) then count × {f_act, a_c, a_s, b_c, b_s} × f32
DATA_HDR_FMT = "<HB"
DATA_HDR_SIZE = struct.calcsize(DATA_HDR_FMT)     # 3
POINT_BYTES = 20                                  # 5 × f32
#: 一帧最多 7 点：3 + 7×20 = 143 B，留出裕量给以后加字段
MAX_POINTS_PER_FRAME = 7
DATA_MAX_SIZE = DATA_HDR_SIZE + MAX_POINTS_PER_FRAME * POINT_BYTES


@dataclass
class DataChunk:
    start_idx: int
    points: List[Tuple[float, float, float, float, float]] = field(
        default_factory=list)

    def pack(self) -> bytes:
        if len(self.points) > MAX_POINTS_PER_FRAME:
            raise ValueError(
                f"一帧最多 {MAX_POINTS_PER_FRAME} 点，收到 {len(self.points)}")
        out = bytearray(struct.pack(DATA_HDR_FMT, self.start_idx,
                                    len(self.points)))
        for p in self.points:
            out += struct.pack("<5f", *p)
        return bytes(out)

    @staticmethod
    def unpack(p: bytes) -> Optional["DataChunk"]:
        if len(p) < DATA_HDR_SIZE:
            return None
        idx, n = struct.unpack_from(DATA_HDR_FMT, p, 0)
        if n > MAX_POINTS_PER_FRAME:
            return None
        need = DATA_HDR_SIZE + n * POINT_BYTES
        if len(p) < need:
            return None
        pts = [struct.unpack_from("<5f", p, DATA_HDR_SIZE + i * POINT_BYTES)
               for i in range(n)]
        return DataChunk(idx, pts)

    @property
    def end_idx(self) -> int:
        """独占上界：本帧最后一点的 idx + 1。"""
        return self.start_idx + len(self.points)


def pack_get_data(start_idx: int, count: int) -> bytes:
    if not 0 <= start_idx <= 0xFFFF:
        raise ValueError("start_idx 超出 u16")
    if not 1 <= count <= MAX_POINTS_PER_FRAME:
        raise ValueError(f"count 必须在 1~{MAX_POINTS_PER_FRAME}")
    return struct.pack("<HB", start_idx, count)


def unpack_get_data(p: bytes) -> Optional[Tuple[int, int]]:
    if len(p) < DATA_HDR_SIZE:
        return None
    return struct.unpack_from(DATA_HDR_FMT, p, 0)


# ---------------------------------------------------------------- 载荷：COEFF
# order(u8) b[0..3](f32)  —— 变长：order+1 个系数
def pack_coeff(coeffs: List[float]) -> bytes:
    n = len(coeffs)
    if not 1 <= n <= 4:
        raise ValueError("补偿器阶数 1~4")
    return struct.pack("<B", n) + struct.pack(f"<{n}f", *coeffs)


def unpack_coeff(p: bytes) -> Optional[List[float]]:
    if len(p) < 1:
        return None
    n = p[0]
    if not 1 <= n <= 4 or len(p) < 1 + 4 * n:
        return None
    return list(struct.unpack_from(f"<{n}f", p, 1))


# ---------------------------------------------------------------- 载荷：CONDITIONS
# vin, vout, iout, fs, deadtime, temp, adc_fs_v, shunt_gain 各 f32 = 32 B
_COND_FMT = "<8f"
COND_SIZE = struct.calcsize(_COND_FMT)           # 32

COND_NAMES = ("vin", "vout", "iout", "fs", "deadtime", "temp",
              "adc_fs_v", "shunt_gain")


def pack_conditions(**kw) -> bytes:
    return struct.pack(_COND_FMT, *[float(kw.get(k, 0.0)) for k in COND_NAMES])


def unpack_conditions(p: bytes) -> Optional[dict]:
    if len(p) < COND_SIZE:
        return None
    vals = struct.unpack_from(_COND_FMT, p, 0)
    return dict(zip(COND_NAMES, vals))


# ---------------------------------------------------------------- 端口枚举
def _import_serial():
    """延迟导入：没装 PyQt5.QtSerialPort 时仿真模式仍然可用。"""
    try:
        from PyQt5 import QtSerialPort
        return QtSerialPort
    except ImportError:
        return None


def list_ports() -> List[str]:
    QSP = _import_serial()
    if QSP is None:
        return []
    return [i.portName() for i in QSP.QSerialPortInfo.availablePorts()]
