"""SFRA Studio - 被测对象（L0 验证用，答案已知）。

每个对象都提供一个 `__call__`，输入一个 ISR 周期的激励，输出反馈，
内部自己维护积分状态（模拟真实的 power stage）。
"""
from __future__ import annotations

import math


class FirstOrderLP:
    """一阶低通  H(s) = 1 / (1 + s/wc)，w_c = 2*pi*fc。

    离散化用双线性（Tustin），与 libsfra filter_rc.h 同源。
    验证点：fc 处 -3 dB / -45°，相位 0 → -90°。
    """

    def __init__(self, fc: float, fs: float):
        self.fc = fc
        self.fs = fs
        k = 2.0 * fs
        w = 2.0 * math.pi * fc
        # 双线性：s → k(1-z^-1)/(1+z^-1)
        self.b0 = w / (w + k)
        self.b1 = w / (w + k)
        self.a1 = (w - k) / (w + k)
        self._x1 = 0.0
        self._y1 = 0.0

    def __call__(self, x: float) -> float:
        y = self.b0 * x + self.b1 * self._x1 - self.a1 * self._y1
        self._x1 = x
        self._y1 = y
        return y

    def reset(self) -> None:
        self._x1 = self._y1 = 0.0


class SecondOrderLC:
    """Buck 功率级（开环）  H(s) = 1 / (1 + s/(Q*w0) + (s/w0)^2)。

    用 Tustin 离散化。验证点：谐振处相位 -90°、之后 -180° 陡降。
    """

    def __init__(self, f0: float, Q: float, fs: float):
        self.f0 = f0
        self.Q = Q
        self.fs = fs
        k = 2.0 * fs
        w0 = 2.0 * math.pi * f0
        den = w0 * w0 + k * w0 / Q + k * k
        # 双线性展开
        self.b0 = w0 * w0 / den
        self.b1 = 2.0 * w0 * w0 / den
        self.b2 = self.b0
        self.a1 = 2.0 * (w0 * w0 - k * k) / den
        self.a2 = (w0 * w0 - k * w0 / Q + k * k) / den
        self._x1 = self._x2 = 0.0
        self._y1 = self._y2 = 0.0

    def __call__(self, x: float) -> float:
        y = (self.b0 * x + self.b1 * self._x1 + self.b2 * self._x2
             - self.a1 * self._y1 - self.a2 * self._y2)
        self._x2, self._x1 = self._x1, x
        self._y2, self._y1 = self._y1, y
        return y

    def reset(self) -> None:
        self._x1 = self._x2 = self._y1 = self._y2 = 0.0


class PICompensator:
    """数字 PI（并行式）：u[k] = Kp·e[k] + I[k]，I[k] += Ki·Ts·e[k]。

    对应 z 域  C(z) = Kp + Ki·Ts/(1 − z⁻¹)。

    **Ki 的单位是 1/s**（连续域积分增益），所以离散时要乘 Ts = 1/fs。
    如果直接写 `I += Ki*e`，Ki 的实际值是 Ki/Ts，在 fs=100kHz 下
    会放大 10^5 倍，必然发散——这是新手最常踩的坑。

    **不要用增量式** `u += Kp·(e−e1) + Ki·Ts·e`：它的 z 域是
    Kp + Ki·Ts·z/(z−1)，比并行式多一个零点相位，高频相位差可达
    70°（20kHz/100kHz 实测）。TI 的 SFRA 参考实现与常规数字电源
    代码都用并行式，这里跟它们齐平。

    界面输入的 Kp/Ki 也按这个约定：Ki=1200 表示积分时间常数 1/1200 s。
    """

    def __init__(self, kp: float, ki: float, fs: float,
                 u0: float = 0.0, u_min: float = 0.0, u_max: float = 1.0):
        self.kp = kp
        self.ki = ki
        self.fs = fs
        self.ts = 1.0 / fs
        self.u = u0                     # 当前输出（含积分状态）
        self.u_min = u_min
        self.u_max = u_max
        self._i = 0.0                   # 积分状态（与比例项分开存）
        self.saturated = False

    def __call__(self, e: float) -> float:
        self._i += self.ki * self.ts * e
        u = self.kp * e + self._i
        self.saturated = False
        if u > self.u_max:
            u = self.u_max
            # 抗积分饱和：把积分状态一并钳住，否则退出饱和要很久
            self._i = self.u_max - self.kp * e
            self.saturated = True
        elif u < self.u_min:
            u = self.u_min
            self._i = self.u_min - self.kp * e
            self.saturated = True
        self.u = u
        return u

    def reset(self) -> None:
        self._i = 0.0
        self.saturated = False


class ClosedLoopRig:
    """补偿器 + 功率级 组成的闭环，供 sweep() 的 compensate_fn 使用。

    reference 是注入后的参考值（含正弦），内部自动减反馈构成误差。
    这个类只在 L0 仿真里用，不参与固件。
    """

    def __init__(self, comp: PICompensator, plant):
        self.comp = comp
        self.plant = plant
        self._y = 0.0

    def __call__(self, reference: float, y: float | None = None) -> float:
        if y is None:
            y = self._y
        e = reference - y
        u = self.comp(e)
        self._y = self.plant(u)
        return u

    def reset(self) -> None:
        self.comp.reset()
        self.plant.reset()
        self._y = 0.0
