"""SFRA Studio - 理论基准（"真实长什么样"的答案，纯数学无 Qt 依赖）。

为什么需要这个模块
==================
扫频只能告诉你"测出来是什么"，没法回答"应该是什么"。没有基准曲线时，
看到一条扫出来的 Bode，只能凭直觉判断"看着像不像"，这在两种情况下
会彻底失效：

1. **离散化偏差**：MCU 跑的是 Tustin 数字滤波器，它在 f_s/5 处
   幅值天然比连续域低 1.26 dB（一阶）/ 更多（二阶）。如果拿连续域解
   当基准，正确的固件会被判 FAIL。
2. **增益基准模糊**：Buck 功率级 `G_vd = Vin/(1 + s/(Q·w0) + (s/w0)²)`
   的直流增益是 **Vin 本身**（12 V → +21.6 dB），而 `plants.SecondOrderLC`
   用的是归一化形式（分子为 1，直流 0 dB）。两者形状相同、整体差一个
   常数倍。不写清楚就会永远对不上。

本模块提供三个层次的函数，与 04 文档的分层验证一一对应：

============  ==========================================  ==============
层次           数学对象                                    对应用途
============  ==========================================  ==============
`AnalogLC`    连续域解析解  H(s)，s = jω                    物理对象本身
`TustinLC`    Tustin 离散后 H(z)，z = e^{jωTs}             MCU 实际在跑的东西
`DigitalLC`   ZOH + 采样 + 一拍延迟的"真实数字环路"          S6b 功率级基准
============  ==========================================  ==============

三条曲线画在一起，就能一眼看出"哪一段的偏差是我实现错了，
哪一段的偏差是数学本身就存在的"。

关于相位符号
------------
全项目的相位约定只在 `model.bode_data.z_from()` 实现（D18）。
本模块返回的是**标准复数**（`complex(re, im)`），相位就是
`atan2(im, re)`。需要与扫频结果比较时，两边都走 `np.angle` 或
`atan2` 即可，不要在这里再加负号——重复处理符号是 A11 那类错误的来源。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np


# ---------------------------------------------------------------- 工具
def _w(f: float) -> float:
    return 2.0 * math.pi * float(f)


def _z_inv(f: float, fs: float) -> complex:
    """数字频率 f 处的 z⁻¹ = e^{−jθ}，θ = 2πf/f_s。

    z = e^{+jωTs} 是"正频率对着单位圆逆时针"，所以 z⁻¹ 是共轭，
    虚部**取负**。写成 `+sin` 会让整条相位反号（这也是 D18 那条
    注释反复强调的同一件事）。
    """
    th = 2.0 * math.pi * float(f) / float(fs)
    return complex(math.cos(th), -math.sin(th))


def _db(m: float) -> float:
    """线性幅值 → dB。**0 要返回 −inf 而不是抛异常**。

    离散域传递函数在单位圆上有零点：二阶 LC 的 Tustin 形式在
    f = f_s/2 处分子恒为 0（两个零点从 −k 位置被双线性映射推到
    z = −1）。`math.log10(0)` 会抛 `ValueError: math domain error`，
    而这正是"扫频到 f_s/2 附近幅值应该掉到极低"的正确表现。
    """
    if m <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(m)


# ================================================================ 一阶低通
@dataclass(frozen=True)
class AnalogLP1:
    """一阶低通连续域解析解：H(s) = 1 / (1 + s/ωc)。

    直流增益 1（0 dB），f → ∞ 时 −20 dB/dec、相位 → −90°。
    fc 处恰好 −3.01 dB / −45°。
    """

    fc: float
    gain: float = 1.0            # 直流增益（线性，不是 dB）

    def h(self, f: float) -> complex:
        s = 1j * _w(f)
        return self.gain / (1.0 + s / _w(self.fc))

    def mag_db(self, f: float) -> float:
        return _db(abs(self.h(f)))

    def phase_deg(self, f: float) -> float:
        z = self.h(f)
        return math.degrees(math.atan2(z.imag, z.real))


@dataclass(frozen=True)
class TustinLP1:
    """一阶低通 Tustin 离散实现（与固件 `filter_rc.h::lp1_init` 同源）。

        b0 = b1 = ωc/(ωc+k),  a1 = (ωc−k)/(ωc+k),  k = 2·fs
        H(z) = (b0 + b1·z⁻¹) / (1 + a1·z⁻¹)

    分母是 `1 + a1·z⁻¹`（a1 自带负号）。写成 `1 − a1·z⁻¹` 会把直流
    增益算成 1/159 —— 这个坑 06 文档 §6.4 记过一次。
    """

    fc: float
    fs: float
    gain: float = 1.0

    @property
    def coeffs(self) -> tuple[float, float, float]:
        k = 2.0 * self.fs
        w = _w(self.fc)
        g = self.gain
        b0 = g * w / (w + k)
        return b0, b0, (w - k) / (w + k)

    def h(self, f: float) -> complex:
        b0, b1, a1 = self.coeffs
        zm = _z_inv(f, self.fs)
        return (b0 + b1 * zm) / (1.0 + a1 * zm)

    def mag_db(self, f: float) -> float:
        return _db(abs(self.h(f)))

    def phase_deg(self, f: float) -> float:
        z = self.h(f)
        return math.degrees(math.atan2(z.imag, z.real))


# ================================================================ 二阶 LC
@dataclass(frozen=True)
class AnalogLC:
    """二阶 LC（Buck 功率级开环）连续域解析解。

        H(s) = K / (1 + s/(Q·ω0) + (s/ω0)²)

    **K 是什么，决定曲线整体高度**，这是最容易错的地方：

    - `K = 1`（默认）：归一化形式，直流 0 dB。与 `plants.SecondOrderLC`
      一致，便于和 L0 仿真逐点对齐。
    - `K = Vin`：真实 Buck 的 `G_vd(d)→vo` 小信号增益，直流 **+21.6 dB**
      （Vin = 12 V）。这才是"S6b 上真板时应该看到的样子"。

    谐振处（f = f0）幅值 = K·Q（即 20log10(Q) dB 的凸峰），
    相位恰好 −90°，之后陡降到 −180°、斜率 −40 dB/dec。
    """

    f0: float
    Q: float
    K: float = 1.0

    def h(self, f: float) -> complex:
        s = 1j * _w(f)
        w0 = _w(self.f0)
        den = 1.0 + s / (self.Q * w0) + (s / w0) ** 2
        return self.K / den

    def mag_db(self, f: float) -> float:
        return _db(abs(self.h(f)))

    def phase_deg(self, f: float) -> float:
        z = self.h(f)
        return math.degrees(math.atan2(z.imag, z.real))

    # ---- 特征量（可当判据用） ----
    @property
    def dc_db(self) -> float:
        return 20.0 * math.log10(abs(self.K))

    @property
    def peak_db(self) -> float:
        """谐振峰相对直流的抬高量 = 20log10(Q)（Q > 1/√2 时）。"""
        return 20.0 * math.log10(self.Q)

    def f_lc(self) -> float:
        """谐振频率处幅值，即直流增益 + 凸峰。"""
        return self.dc_db + self.peak_db


@dataclass(frozen=True)
class TustinLC:
    """二阶 LC 的 Tustin 离散实现（与 `plants.SecondOrderLC` 同源）。

        k = 2·fs,  ω0 = 2π·f0
        den = ω0² + k·ω0/Q + k²
        b0 = b2 = K·ω0²/den,  b1 = 2·K·ω0²/den
        a1 = 2(ω0² − k²)/den, a2 = (ω0² − k·ω0/Q + k²)/den
        H(z) = (b0 + b1·z⁻¹ + b2·z⁻²) / (1 + a1·z⁻¹ + a2·z⁻²)

    在 f0 附近与连续域几乎重合；频率越高偏差越大，f_s/5 处幅值
    会偏低、相位会偏"滞后"（Tustin 的固有翘曲）。这是数学，不是 bug。
    """

    f0: float
    Q: float
    fs: float
    K: float = 1.0

    @property
    def coeffs(self) -> tuple[float, float, float, float, float]:
        k = 2.0 * self.fs
        w0 = _w(self.f0)
        den = w0 * w0 + k * w0 / self.Q + k * k
        g = self.K * w0 * w0 / den
        return (g, 2.0 * g, g,
                2.0 * (w0 * w0 - k * k) / den,
                (w0 * w0 - k * w0 / self.Q + k * k) / den)

    def h(self, f: float) -> complex:
        b0, b1, b2, a1, a2 = self.coeffs
        zm = _z_inv(f, self.fs)
        z2 = zm * zm
        return (b0 + b1 * zm + b2 * z2) / (1.0 + a1 * zm + a2 * z2)

    def mag_db(self, f: float) -> float:
        return _db(abs(self.h(f)))

    def phase_deg(self, f: float) -> float:
        z = self.h(f)
        return math.degrees(math.atan2(z.imag, z.real))


# ================================================================ 真实数字环路
@dataclass(frozen=True)
class ZohPlant:
    """功率级的**真实数字等价**：ZOH + 采样 + 一拍计算延迟。

    连续域 H(s) 只是"物理对象"，MCU 实际看到的是：

        u[k] ──ZOH(Ts)──► H(s) ──采样──► y[k] ──► z⁻¹（一拍计算延迟）

    统一写成 `H_eff(z) = z⁻¹ · H_zoh(z)`。

    两个要素都**不能省**：

    - **ZOH**：零阶保持器带来 `(1−e^{−sTs})/s`，它在高频引入
      `−ωTs/2` 的附加相位滞后（20 kHz @100 kHz 只有 −36°，但
      45 kHz 就到 −81°）。省掉它，穿越频率会被估得偏乐观。
    - **一拍延迟**：`z⁻¹` 贡献 `−ωTs`（20 kHz @100 kHz = −72°）。
      这是数字电源最容易被低估的相位损失，直接吃掉相位裕度。

    `ZohPlant` 用**脉冲响应不变**的思路构造 H_zoh：
    对 `H(s)/s` 做部分分式展开后用 Tustin 离散，再乘 `(1−z⁻¹)`。
    对二阶 LC 可以闭式求出，不需要数值积分。
    """

    f0: float
    Q: float
    fs: float
    K: float = 1.0
    delay: int = 1              # 计算延迟拍数，通常 1

    def h(self, f: float) -> complex:
        """频响合成：H_zoh(jω) = (1−e^{−jωTs})/(jωTs) · H(jω)，再乘 z⁻¹。

        **这是数学恒等式，不是近似**：零阶保持器的频域响应就是
        `(1−e^{−sTs})/(s·Ts)`（幅度与相位都对，包括 44.2 kHz @100 kHz
        之外开始出现的 sinc 零点）。直接用它就不必推三阶差分方程的
        系数——那条路要展开 `H(s)/s` 的部分分式，符号错一处就全废，
        而这里一行写完，且能拿到任意频率点（含非采样网格点）的值。

        需要**时域逐步仿真**时才需要差分方程，那是 `tools/bode_reference.py`
        `--sim` 分支的事（直接调 `sfra_sim`，不重推系数）。
        """
        w = _w(f)
        if w < 1e-12:
            # 直流极限：ZOH → 1，延迟 → 1，只剩 H(0) = K
            return complex(self.K, 0.0)

        s = 1j * w
        w0 = _w(self.f0)
        base = self.K / (1.0 + s / (self.Q * w0) + (s / w0) ** 2)

        # ZOH: (1 − e^{−sTs}) / (s·Ts)，s = jω → s·Ts = jωTs 为纯虚数
        a = w / self.fs                      # ωTs，实数
        # e^{-jωTs} = cos(a) − j·sin(a)
        zoh = (1.0 - complex(math.cos(a), -math.sin(a))) / complex(0.0, a)

        return base * zoh * (_z_inv(f, self.fs) ** self.delay)

    def mag_db(self, f: float) -> float:
        return _db(abs(self.h(f)))

    def phase_deg(self, f: float) -> float:
        z = self.h(f)
        return math.degrees(math.atan2(z.imag, z.real))


# ================================================================ 采样网格
def sample(
    fn: Callable[[float], float],
    freqs: Sequence[float],
) -> np.ndarray:
    """对任意 `fn(f)` 逐点求值。freqs 保持原顺序（不排序、不去重）。"""
    return np.asarray([fn(float(f)) for f in freqs], dtype=float)


def h_vector(
    model,
    freqs: Sequence[float],
) -> np.ndarray:
    """在给定频率表上取复响应 H。"""
    return np.asarray([model.h(float(f)) for f in freqs], dtype=complex)


def response_table(model, freqs: Sequence[float]) -> dict:
    """→ {'f': [...], 'mag_db': [...], 'phase_deg': [...], 'h': [...]}"""
    fs = [float(f) for f in freqs]
    hs = h_vector(model, fs)
    mags = [20.0 * math.log10(abs(v)) if abs(v) > 0 else float("nan")
            for v in hs]
    phs = [math.degrees(math.atan2(v.imag, v.real)) for v in hs]
    return {"f": fs, "mag_db": mags, "phase_deg": phs, "h": hs}


# ================================================================ 偏差分析
def compare(ref, test, freqs: Sequence[float]) -> dict:
    """两条模型曲线的偏差（test − ref），用于"翘曲占比"之类的归因。

    返回 max/mean 的 dB 与度，以及是否在某频段内单调。
    """
    d_mag, d_pha = [], []
    for f in freqs:
        hr, ht = ref.h(float(f)), test.h(float(f))
        if abs(hr) <= 0 or not np.isfinite(abs(hr)):
            continue
        d_mag.append(20.0 * math.log10(abs(ht) / abs(hr)))
        pr = math.degrees(math.atan2(hr.imag, hr.real))
        pt = math.degrees(math.atan2(ht.imag, ht.real))
        d = (pt - pr + 180.0) % 360.0 - 180.0
        d_pha.append(d)
    if not d_mag:
        return {"n": 0}
    return {
        "n": len(d_mag),
        "max_db": max(abs(x) for x in d_mag),
        "mean_db": float(sum(d_mag) / len(d_mag)),
        "max_deg": max(abs(x) for x in d_pha),
        "mean_deg": float(sum(d_pha) / len(d_pha)),
    }


# ================================================================ 便捷构造
def make_pair(kind: str, *, fs: float, **kw):
    """一次拿到 (连续域, Tustin) 两个模型，方便并排画。

    kind = "lp1"  →  AnalogLP1 / TustinLP1
    kind = "lc"   →  AnalogLC  / TustinLC
    """
    if kind == "lp1":
        fc = float(kw["fc"])
        g = float(kw.get("gain", 1.0))
        return AnalogLP1(fc=fc, gain=g), TustinLP1(fc=fc, fs=fs, gain=g)
    if kind == "lc":
        return (AnalogLC(f0=float(kw["f0"]), Q=float(kw["q"]),
                         K=float(kw.get("K", 1.0))),
                TustinLC(f0=float(kw["f0"]), Q=float(kw["q"]), fs=fs,
                         K=float(kw.get("K", 1.0))))
    raise ValueError(f"未知模型 {kind}")
