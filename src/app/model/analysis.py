"""SFRA Studio - 曲线分析：unwrap / 穿越点 / PM / GM / 带宽。

这里**不碰复数约定**（那是 bode_data.py 的职责），只对已经算好的
mag(dB) 与 phase(deg) 数组做数学处理。全部用 double（numpy float64）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def mag_smooth_db(mag_db_arr: np.ndarray, frac: float = 0.30) -> np.ndarray:
    """幅值沿频率做单调回归平滑（isotonic），用于取判据。

    **不能用中值/均值滤波**：实测 |GH| 在穿越点之后会打几个点的毛刺，
    均值/中值会把 -1.7dB 那段平均到 -2.5dB，导致低频区凭空多出一个
    0dB 穿越点，PM 算成 115°（这个坑踩过）。单调回归只抑制抖动、
    不改变整体下降趋势，穿越点就稳定了。

    只用于取判据，画图仍用原始值。
    """
    m = np.asarray(mag_db_arr, dtype=float)
    n = m.size
    if n < 4:
        return m.copy()

    # 用滑动平均先压掉单点跳变（宽度 3，不改变趋势）
    k = np.ones(3) / 3.0
    pad = np.concatenate([m[:1], m, m[-1:]])
    sm = np.convolve(pad, k, mode="same")[1:-1]

    # 再去掉有限点，做非增（PAVA）回归
    ok = np.isfinite(sm)
    if ok.sum() < 4:
        return m.copy()
    idx = np.flatnonzero(ok)
    vals = sm[idx]
    out = np.empty_like(sm)
    out[~ok] = np.nan
    out[idx] = _pava_nonincreasing(vals)
    return out


def _pava_nonincreasing(y: np.ndarray) -> np.ndarray:
    """Pool Adjacent Violators：求非增序列的最小二乘拟合。

    纯 numpy 实现，避免为这一处引入 sklearn。
    """
    y = np.asarray(y, dtype=float)
    n = y.size
    val = list(y)
    wt = [1.0] * n
    cnt = [1] * n
    i = 0
    while i < len(val) - 1:
        if val[i] < val[i + 1]:           # 违反非增 → 合并
            s = val[i] * wt[i] + val[i + 1] * wt[i + 1]
            w = wt[i] + wt[i + 1]
            val[i] = s / w
            wt[i] = w
            cnt[i] += cnt[i + 1]
            del val[i + 1], wt[i + 1], cnt[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    out = np.empty(n)
    p = 0
    for v, c in zip(val, cnt):
        out[p:p + c] = v
        p += c
    return out


def unwrap_phase(phase_deg: np.ndarray) -> np.ndarray:
    """把 ±180° 跳变解缠成连续曲线。

    TI 的约定相位从 0 往负方向走（0 → -90 → -180 → -270 → -360），
    所以解缠后允许一路降到 -360 以下，不折回来。
    """
    ph = np.asarray(phase_deg, dtype=float)
    if ph.size == 0:
        return ph
    return np.degrees(np.unwrap(np.radians(ph)))


def _log_interp(f1: float, f2: float, v1: float, v2: float, target: float) -> float:
    """在 log10(f) 上线性插值，求 value==target 处的频率。"""
    if v1 == v2:
        return f1
    t = (target - v1) / (v2 - v1)
    return float(10 ** (np.log10(f1) + t * (np.log10(f2) - np.log10(f1))))


@dataclass
class Stability:
    """稳定性判据。None 表示"该量不可信"（00 文档 D17）。"""
    fc: float | None = None          # 0 dB 穿越频率 Hz（多个取最后一个）
    pm: float | None = None          # 相位裕度 deg
    f180: float | None = None        # -180° 穿越频率 Hz
    gm: float | None = None          # 增益裕度 dB（正数 = 稳定）

    # 可信度说明，直接显示到界面
    pm_note: str = ""
    gm_note: str = ""

    @property
    def pm_ok(self) -> bool:
        return self.pm is not None and self.pm > 45.0

    @property
    def gm_ok(self) -> bool:
        return self.gm is not None and self.gm > 6.0


def analyze(freq: np.ndarray, mag: np.ndarray, phase: np.ndarray,
            unwrapped: bool = False, smooth: bool = False) -> Stability:
    """算 fc / PM / GM。

    phase 传**原始**相位（-180~180），本函数内部先 unwrap。
    smooth=True 时幅值先做中值平滑再找穿越点（实测数据抖动大，见 mag_smooth_db）。
    """
    f = np.asarray(freq, dtype=float)
    m = np.asarray(mag, dtype=float)
    p = np.asarray(phase, dtype=float)

    # 去掉 NaN 点
    ok = np.isfinite(f) & np.isfinite(m) & np.isfinite(p)
    f, m, p = f[ok], m[ok], p[ok]
    if f.size < 2:
        return Stability(pm_note="数据点不足")

    order = np.argsort(f)
    f, m, p = f[order], m[order], p[order]
    ph = p if unwrapped else unwrap_phase(p)
    if smooth:
        m = mag_smooth_db(m)

    res = Stability()

    # ---- 0 dB 穿越：取最后一个（高频侧） ----
    crossing = None
    for i in range(1, f.size):
        if m[i - 1] >= 0.0 > m[i]:
            crossing = i
    if crossing is not None:
        i = crossing
        res.fc = _log_interp(f[i - 1], f[i], m[i - 1], m[i], 0.0)
        t = (0.0 - m[i - 1]) / (m[i] - m[i - 1]) if m[i] != m[i - 1] else 0.0
        ph_at = ph[i - 1] + t * (ph[i] - ph[i - 1])
        res.pm = 180.0 + ph_at
        res.pm_note = ""
    else:
        res.pm_note = "未检出 0 dB 穿越，PM 不可信"

    # ---- -180° 穿越：取 0 dB 之后的第一个 ----
    start = crossing if crossing is not None else 1
    f180_idx = None
    for i in range(start, f.size):
        a, b = ph[i - 1], ph[i]
        if (a > -180.0 >= b) or (a >= -180.0 > b):
            f180_idx = i
            break
    if f180_idx is not None:
        i = f180_idx
        a, b = ph[i - 1], ph[i]
        fa, fb = f[i - 1], f[i]
        res.f180 = _log_interp(fa, fb, a, b, -180.0)
        t = (-180.0 - a) / (b - a) if b != a else 0.0
        mag180 = m[i - 1] + t * (m[i] - m[i - 1])
        res.gm = -mag180
        res.gm_note = ""
    else:
        res.gm_note = "未检出 -180° 穿越，GM 不可信"

    return res
