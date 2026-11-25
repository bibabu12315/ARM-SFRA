"""SFRA Studio - 采集数据模型（纯数据，无 Qt 依赖）。

这个模块是整个项目**相位约定的唯一实现处**（00 文档 D18 / 02 文档 2.3）。
任何其他地方都不许再写 atan2、不许再写 -j 或 +j。

约定（复刻 libsfra 的"负虚部"写法）：
    Z = S_c - j * S_s
    H = Z_y / Z_x
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np


# ---------------------------------------------------------------- 模式常量
MODE_PLANT = 0   # 开环扫 Plant：b 通道 = X(in) 累加器
MODE_LOOP = 1    # 闭环扫 Loop Gain：b 通道 = Inj 累加器

MODE_NAMES = {MODE_PLANT: "Plant", MODE_LOOP: "Loop Gain"}


# ---------------------------------------------------------------- 单点原始数据
@dataclass
class RawPoint:
    """MCU 上报的一个频点的累加器原值（DATA 帧 20 字节）。

    f_act : 实际频率 Hz（固件按整周期取整后反算，00 文档 D7），不是名义值
    a_c/a_s : Y(out) 的累加器  Σy·cosθ / Σy·sinθ
    b_c/b_s : PLANT 时是 X(in) 的累加器；LOOP 时是 Inj 的累加器
    """
    f_act: float
    a_c: float
    a_s: float
    b_c: float
    b_s: float

    # ---- 由协议帧解出 ----
    @staticmethod
    def unpack(buf: bytes) -> "RawPoint":
        if len(buf) != 20:
            raise ValueError(f"RawPoint 需要 20 字节，实际 {len(buf)}")
        v = np.frombuffer(buf, dtype="<f4")  # 小端 float32
        return RawPoint(float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4]))

    def pack(self) -> bytes:
        return np.asarray([self.f_act, self.a_c, self.a_s, self.b_c, self.b_s],
                          dtype="<f4").tobytes()


# ---------------------------------------------------------------- 单点复数
def z_from(c: float, s: float) -> complex:
    """(S_c, S_s) → 复数。**这是全项目唯一的符号来源。**

    libsfra 用 Z = S_c - j·S_s，推导见 01 文档 1.3：
        S_xc = (N·A/2)·cos φ ,  S_xs = -(N·A/2)·sin φ
    代入 Z = S_c - j·S_s 得 Z = (N·A/2)·e^{+jφ}，相位即真实相位。

    若这里写成 +j，全项目相位反号，PM 会算出"很高"但其实很危险。
    """
    return complex(c, -s)


def h_from_raw(p: RawPoint, mode: int) -> complex:
    """累加器原值 → 传递函数复数（含正确的相位）。"""
    zy = z_from(p.a_c, p.a_s)

    if mode == MODE_PLANT:
        # H = Y / U
        zx = z_from(p.b_c, p.b_s)
        if zx == 0:
            return complex(np.nan, np.nan)
        return zy / zx

    if mode == MODE_LOOP:
        # GH = Y / (Inj - Y)
        # Z_inj - Z_y = (b_c - a_c) - j(b_s - a_s)
        # 分子分母同乘 -1 → (b_c-a_c) + j(a_s-b_s)，一次复数除法即可
        den = complex(p.b_c - p.a_c, p.a_s - p.b_s)
        if den == 0:
            return complex(np.nan, np.nan)
        return zy / den

    raise ValueError(f"未知模式 {mode}")


def mag_db(h: complex) -> float:
    m = abs(h)
    if m <= 0 or not np.isfinite(m):
        return float("nan")
    return 20.0 * np.log10(m)


def phase_deg(h: complex) -> float:
    """返回 (-180, 180] 的相位。unwrap 在 analysis.py 做。"""
    if not np.isfinite(h.real) or not np.isfinite(h.imag):
        return float("nan")
    return float(np.degrees(np.arctan2(h.imag, h.real)))


# ---------------------------------------------------------------- 整条曲线
@dataclass
class SweepResult:
    """一次扫频的全部结果。

    原始累加器与派生量都留着：算错了能重算，不用重扫（02 文档 2.5）。
    """
    mode: int
    raw: List[RawPoint] = field(default_factory=list)
    amplitude_pu: float = 0.01
    isr_freq: float = 100_000.0
    timestamp: str = ""
    notes: List[str] = field(default_factory=list)

    #: 逐点饱和标记（与 `raw` 等长）。True = 该点在采集时
    #: STATUS.flags 的 DUTY_SAT/ADC_SAT 置位过。
    #:
    #: **为什么需要它，而 STATUS 里已经有 flags**：
    #: DATA 帧（20 B/点）里没有饱和位，flags 是 STATUS 层面的"当前状态"。
    #: 上位机每 200 ms 轮询一次 STATUS、每次可能拉 7 个点，所以
    #: "哪个点饱和"必须由**上位机在拉数时记录当时的 flags**得到。
    #: 不记的话，扫完只知道"这一趟出过饱和"，不知道是哪几个频点——
    #: 而处置完全依赖定位：只有个别点饱和 → 那几个点作废重扫；
    #: 全程饱和 → 注入幅值整体偏大，整趟重来（D16）。
    saturated: List[bool] = field(default_factory=list)

    # ---- 派生量（惰性计算，缓存） ----
    _freq: np.ndarray | None = None
    _h: np.ndarray | None = None

    def add(self, p: RawPoint, saturated: bool = False) -> None:
        self.raw.append(p)
        self.saturated.append(bool(saturated))
        self._freq = None
        self._h = None

    def add_many(self, pts: List[RawPoint],
                 saturated: bool = False) -> None:
        self.raw.extend(pts)
        self.saturated.extend([bool(saturated)] * len(pts))
        self._freq = None
        self._h = None

    def truncate(self, n: int) -> None:
        del self.raw[n:]
        del self.saturated[n:]
        self._freq = None
        self._h = None

    @property
    def freq(self) -> np.ndarray:
        if self._freq is None:
            self._freq = np.asarray([p.f_act for p in self.raw], dtype=float)
        return self._freq

    @property
    def h(self) -> np.ndarray:
        if self._h is None:
            self._h = np.asarray([h_from_raw(p, self.mode) for p in self.raw],
                                 dtype=complex)
        return self._h

    @property
    def mag(self) -> np.ndarray:
        return np.asarray([mag_db(v) for v in self.h], dtype=float)

    @property
    def phase(self) -> np.ndarray:
        return np.asarray([phase_deg(v) for v in self.h], dtype=float)

    def __len__(self) -> int:
        return len(self.raw)

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, str(self.mode))

    # ---- 质量检查（01 文档 1.10） ----
    def samples_per_cycle(self) -> np.ndarray:
        """每周期采样点数 = f_s / f。低于 4 点标黄，低于 2 点剔除。"""
        f = self.freq
        with np.errstate(divide="ignore"):
            return np.where(f > 0, self.isr_freq / np.maximum(f, 1e-9), np.inf)

    def invalid_mask(self) -> np.ndarray:
        """需要剔除的点：每周期采样 < 2，或幅相为 NaN。"""
        spc = self.samples_per_cycle()
        return (spc < 2) | ~np.isfinite(self.mag) | ~np.isfinite(self.phase)

    def warn_mask(self) -> np.ndarray:
        """需要标黄的点：每周期采样 < 4。"""
        spc = self.samples_per_cycle()
        return (spc < 4) & (spc >= 2)

    # ---- 饱和（STATUS.flags bit0/bit1，见 `saturated` 字段说明） ----
    def sat_mask(self) -> np.ndarray:
        """逐点饱和掩码，长度与 `raw` 一致。

        老数据（`saturated` 为空，比如从 v4.0 的 JSON 导入）返回全 False，
        不因为"没记过"就当成"全部饱和"。
        """
        n = len(self.raw)
        if not self.saturated:
            return np.zeros(n, dtype=bool)
        m = np.asarray(self.saturated, dtype=bool)
        if m.size != n:                 # 理论上不该发生，防御一下
            out = np.zeros(n, dtype=bool)
            k = min(n, m.size)
            out[:k] = m[:k]
            return out
        return m

    @property
    def sat_count(self) -> int:
        return int(self.sat_mask().sum())

    def reliable_mask(self) -> np.ndarray:
        """**可用于算 PM/GM 的点**：排除采样不足 + 饱和。

        饱和点必须剔除，否则裕度会被算歪：
        被测对象顶到轨之后，输出幅值不再线性跟随注入幅值，
        此刻测到的"增益"只是限幅器的增益，与真实环路无关。
        """
        return ~self.invalid_mask() & ~self.sat_mask()

    # ---- LOOP 模式专属：Inj−Y 的数值可信度 ----
    def loop_confidence(self) -> np.ndarray:
        """LOOP 模式下每点的**不可信判据**（True = 不可信）。

        GH = Y/(Inj−Y)，分母是 "注入复振幅 - 反馈复振幅"。
        注入是可控的常量，反馈 Y 是真机读数（float32 上报）。

        |Inj−Y| / |Inj| = 1/|1+GH|，它随 |GH| 增大而减小：
        当 |GH| >> 1（穿越点以上很远的频段），|Inj−Y| 趋近 0，
        等于两个几乎相等的量相减，float32 只有约 7 位有效数字，
        相对误差会被放大到无法接受，相位彻底乱跳。

        **判据是"分母太小"，不是"反馈太小"**：早期写成 |Y|/|Inj| < 0.12，
        方向反了——它把 |GH| 很小的低频段（数据其实很准）全砍掉，
        却放过了 |GH| 很大、分母下溢的高频段（实测 50kHz 出 -194dB）。

        默认阈值 0.05 约对应 |GH| > 26dB、float32 相对误差 > 20 倍放大，
        到这一步相位已经没有意义了。
        """
        if self.mode != MODE_LOOP or not self.raw:
            return np.zeros(len(self.raw), dtype=bool)

        bad = np.empty(len(self.raw), dtype=bool)
        for i, p in enumerate(self.raw):
            inj = abs(z_from(p.b_c, p.b_s))
            err = abs(complex(p.b_c - p.a_c, p.a_s - p.b_s))   # |Inj − Y|
            bad[i] = (inj <= 0.0) or (err / inj < 0.05)
        return bad

    def phase_glitch_mask(self, max_jump_deg: float = 75.0) -> np.ndarray:
        """相位跳变检测：相邻点相位突变超过阈值就判为不可信。

        真实环路的相位是连续变化的，一个频点内跳 180° 只可能是数值崩了。
        这是比 |Y|/|Inj| 更直接的证据——实测里 5kHz 以上的点
        原始相位在 -70°/+175°/-141° 之间来回跳，一眼就是坏的。

        从第一个跳变点起，之后全部判为不可信（高频段整体崩掉，
        不是零星几个点的问题）。
        """
        n = len(self.phase)
        bad = np.zeros(n, dtype=bool)
        if n < 3:
            return bad
        ph = self.phase
        for i in range(1, n):
            d = abs(ph[i] - ph[i - 1])
            if d > 180.0:                 # 跨 ±180 的正常情况
                d = 360.0 - d
            if d > max_jump_deg:
                bad[i:] = True            # 之后全部不可信
                break
        return bad

    def trusted_mask(self) -> np.ndarray:
        """画图和算 PM/GM 时真正该用的点。"""
        bad = self.invalid_mask()
        if self.mode == MODE_LOOP:
            bad = bad | self.loop_confidence() | self.phase_glitch_mask()
        return ~bad
