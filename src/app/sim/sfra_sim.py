"""SFRA Studio - 扫频算法核心（Python 版，逐行对齐 libsfra.c）。

这一层只负责"产生注入、累加相关量、给出原始累加器"，
**不做任何 atan2 / log10**——幅相全部交给 PC 端 bode_data.py（00 文档 D5）。
这样 S2 阶段把 C 库搬进来时可以逐位比对累加器原值，而不是比对 mag/phase。

对应关系：
    libsfra.c  sfra_monitor_int32()   →  本例 SimSfra.monitor()
    libsfra.c  process_foi_data()     →  **不放这里**，在 bode_data.py
    libsfra.c  calc_cycles()          →  本例 calc_cycles()
    libsfra 快速正弦 fast_sin         →  本例用 np.cos（精度更高，见下）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List

import numpy as np

from ..model.bode_data import MODE_LOOP, MODE_PLANT, RawPoint

# ---------------------------------------------------------------- 周期数策略
CYC_FAST = 0
CYC_NORMAL = 1
CYC_PRECISE = 2

#: Fast 档每点最少周期数；同时保证每点时长不低于 20 ms（01 文档 1.5）
FAST_MIN_CYCLES = 8
FAST_MIN_SECONDS = 0.020

#: 每周期采样点下限。低于 8 点时，单点 DFT 对"换频后残留"和噪声极其敏感：
#: 实测同一台设备在 12 点/周期时 |GH| 会平白虚高 15~20 dB（30 点/周期就正常）。
#: 多积周期就能把点数补回来，代价只是多花一点时间。
MIN_SPC = 16.0


def min_cycles_for(isr_freq: float, foi_hz: float) -> int:
    """为满足每周期采样点下限所需的最少周期数。

    单个注入周期内的采样点数 spc = isr_freq / foi_hz，与选了多少周期无关。
    这里返回的是"至少还要积多少个周期"，才能让总点数 / 周期数 >= MIN_SPC。
    """
    spc = isr_freq / max(foi_hz, 1e-9)
    if spc >= MIN_SPC:
        return 1
    return int(math.ceil(MIN_SPC / spc))


def calc_cycles(foi_hz: float, mode: int = CYC_NORMAL,
                isr_freq: float = 100_000.0) -> int:
    """选周期数（对齐 libsfra calc_cycles，三档 + 每周期采样点下限）。"""
    if mode == CYC_FAST:
        n = FAST_MIN_CYCLES
        if foi_hz > 0:
            n = max(n, int(math.ceil(FAST_MIN_SECONDS * foi_hz)))
    elif mode == CYC_PRECISE:
        n = 20 if foi_hz < 10.0 else 200
    else:
        n = 10 if foi_hz < 10.0 else (max(10, int(round(foi_hz)))
                                      if foi_hz <= 100.0 else 100)

    # 每周期采样点下限：spc < MIN_SPC 时，周期数至少要到 MIN_SPC/spc
    lo = min_cycles_for(isr_freq, foi_hz)
    return lo if lo > n else n


def point_geometry(isr_freq: float, foi_hz: float, cycles: int) -> tuple[int, float]:
    """整周期取整（libsfra 的核心技巧，01 文档 1.4）。

    先把采样点数向上取整，再反算角步进，保证
        data_count × foi_rad == cycles × 2π   （精确）
    泄漏被消掉，代价是实际频率偏离名义值：

        f_act = cycles / data_count × f_s

    返回 (data_count, foi_rad)。
    """
    if foi_hz <= 0 or isr_freq <= 0 or cycles <= 0:
        raise ValueError("频率与周期数必须为正")
    data_count = int(math.ceil(isr_freq * cycles / foi_hz))
    data_count = max(data_count, cycles)          # 至少每周期 1 点
    foi_rad = cycles / data_count * 2.0 * math.pi
    return data_count, foi_rad


def actual_freq(isr_freq: float, data_count: int, cycles: int) -> float:
    """f_act = cycles / data_count × f_s（00 文档 D7，全链路只认它）。"""
    return cycles / data_count * isr_freq


def freq_vector(freq_start: float, freq_step: float, vec_length: int,
                f_max: float | None = None) -> np.ndarray:
    """名义频率表。freq_step 是每点倍数 = 10^(1/pts_per_dec)。

    f_max 用于把扫频上限钳在奈奎斯特频率（isr_freq/2）以内：注入频率
    一旦达到或超过 f_s，point_geometry 的整周期取整会退化（多个频点
    算出同一个 f_act），DFT 结果毫无意义。缺省不钳，由调用方决定。
    """
    f = freq_start * np.power(freq_step, np.arange(vec_length, dtype=float))
    if f_max is not None:
        f = f[f < f_max]
    return f


def estimate_total_seconds(isr_freq: float, freqs: np.ndarray, cyc_mode: int,
                           mode: int) -> float:
    """预估总耗时（02 文档 2.2.1）。

    LOOP 模式单 pass 同时采 Y 与 Inj（D19 修正），耗时不再 ×2。
    旧版本按两趟算，进度条会比实际慢一倍后突然走完。
    """
    total = 0.0
    for f in freqs:
        cyc = calc_cycles(float(f), cyc_mode, isr_freq)
        dc, _ = point_geometry(isr_freq, float(f), cyc)
        total += (dc / isr_freq) + 0.0
    return total


# ---------------------------------------------------------------- 单点采集
@dataclass
class PointAccumulator:
    """一个频点、一个 pass 的累加器（6 个，Y/X/Inj 各 2）。"""
    y_c: float = 0.0
    y_s: float = 0.0
    x_c: float = 0.0
    x_s: float = 0.0
    inj_c: float = 0.0
    inj_s: float = 0.0
    n: int = 0

    def clear(self) -> None:
        self.y_c = self.y_s = 0.0
        self.x_c = self.x_s = 0.0
        self.inj_c = self.inj_s = 0.0
        self.n = 0


@dataclass
class SimSfra:
    """单频点扫频的等价实现（仅用于 PC 仿真与调试，固件里是 C 版）。

    用法（对应固件 ISR 里的两行）：
        inj = sfra.inject()
        sfra.monitor(x, y)          # PLANT
        sfra.monitor(0.0, y)        # LOOP
    """

    isr_freq: float = 100_000.0
    amplitude: float = 0.01          # pu
    mode: int = MODE_LOOP
    theta: float = 0.0
    step: float = 0.0
    acc: PointAccumulator = field(default_factory=PointAccumulator)
    _collecting: bool = False
    _settle_left: int = 0
    _total: int = 0
    _channel: int = 0                # 0=采 X/Y  1=采 Inj
    #: 三角函数入口。默认用 math 精确值；S2 交叉验证时替换为
    #: 与固件相同的 1024 点查表，才能逐位比对累加器（见 tools/cross_check.py）。
    _cos: object = None
    _sin: object = None

    def _cs(self, theta: float) -> tuple[float, float]:
        """取 (cosθ, sinθ)。"""
        if self._cos is not None:
            c = self._cos(theta)                    # type: ignore[misc]
            s = self._sin(theta) if self._sin else math.sin(theta)  # type: ignore[misc]
            return c, s
        return math.cos(theta), math.sin(theta)

    # ---- 频点切换 ----
    SETTLE_SECONDS = 0.020      # 换频后的静默时间（秒）
    #: settle 的下限：至少 2 个注入周期（低频一个周期就很长，要按周期给足）
    SETTLE_MIN_PERIODS = 2.0

    def begin_point(self, foi_hz: float, cycles: int) -> tuple[int, float]:
        """开始一个新频点。返回 (data_count, f_act)。

        settle 时长取"20ms 与 2 个注入周期"的较大者，再换算成采样点数。

        **settle 不能按采集窗比例截断**：早期写成 min(settle_n, data_count//2)，
        高频点采集窗短（20kHz×100 周期只有 500 点），settle 被压到 5ms，
        被测对象的初始过渡过程还在，误差高达 +27dB / -58°。
        实测要求至少 2000 个 ISR 周期（20ms @100kHz）误差才归零，
        所以 settle 只按时间/周期算，不跟采集窗挂钩。

        换算必须用采样率——拿周期数乘 data_count 会让高频算出比采集窗
        还长的 settle（采集点数归零，这是实测踩过的坑）。
        """
        data_count, self.step = point_geometry(self.isr_freq, foi_hz, cycles)
        self.acc.clear()
        self._total = data_count

        period = 1.0 / max(foi_hz, 1e-9)
        settle_sec = max(self.SETTLE_SECONDS, self.SETTLE_MIN_PERIODS * period)
        settle_n = int(math.ceil(settle_sec * self.isr_freq))
        self._settle_left = settle_n

        # theta 保持连续：每个频点之间不复位，避免注入波形出现相位台阶。
        # 整周期取整保证采集窗内是整数个周期，起点相位任意都成立。
        self._collecting = False
        self._channel = 0
        return data_count, actual_freq(self.isr_freq, data_count, cycles)

    @property
    def settle_left(self) -> int:
        """本频点还要静默多少个 ISR 周期（循环次数要用它）。"""
        return self._settle_left

    # ---- ISR 里的两行 ----
    def inject(self) -> float:
        """对应固件 sfra_inject()：取本周期注入值，并按需累加 Inj。

        LOOP 模式下注入复振幅必须和 Y 在**同一轮**采集，
        否则两次 pass 的激励不可能相同，Inj−Y 无意义（D19 修正）。

        **相位不在这里推进**，见 _advance()。
        """
        c, s = self._cs(self.theta)
        value = self.amplitude * c
        if self._settle_left > 0:
            self._settle_left -= 1
            if self._settle_left == 0:
                self._collecting = True
            # **不要在这里复位 theta**：point_geometry 的整周期取整
            # （dc × step == cycles × 2π）保证采集窗内恰好覆盖整数个周期，
            # 起点相位是多少都不影响 DFT 结果。强行复位 theta 反而在
            # 注入波形上造成一个相位不连续台阶，频谱泄漏到整个频带：
            # 实测 20kHz 时 |H| 偏 +28 dB、相位偏 +128°，且与 settle
            # 时长无关（说明不是过渡过程）。保持相位连续即可。
        elif self._collecting:
            # LOOP 模式：注入与自己相关，这就是 Inj 的复振幅
            #
            # **必须和 monitor() 用同一个上限**：collect 满了就不再累加。
            # 调用方惯常会多跑几拍收尾，若不判上限，Inj 会多积 2~5 点，
            # 而 Y 停在 data_count——D19 的"同一轮采集"前提就被破坏了，
            # Y 与 Inj 的点数不同，|Inj−Y| 的相位随之偏（实测 0.08°）。
            if self.acc.n < self._total:
                if self._channel == 1 or (self._channel == 0 and
                                          self.mode == MODE_LOOP):
                    self.acc.inj_c += value * c
                    self.acc.inj_s += value * s

        # **必须在同周期 monitor() 之后才推进 theta**（由 _advance 完成）：
        # 若在这里先加 step，monitor 用的是一个周期之后的 θ，累计相位偏差
        # 恒为 2π/data_count，等于给整条曲线强行加了 360/data_count 度的
        # 相位偏移。低频点只有几十个采样/周期，这个偏差能到 -6°，
        # 高频点 12 个采样/周期时到 -30°，表现为相位在高频段缓慢塌陷，
        # 用 GH 反推 Plant 时会把误差全算到 Plant 头上。
        return value

    _WRAP = 1e6 * 2.0 * math.pi                 # 相位回绕阈值

    def _advance(self) -> None:
        """推进注入相位（每个 ISR 周期一次，在 monitor 之后调用）。"""
        self.theta += self.step
        if self.theta > self._WRAP:              # 防浮点精度漂移
            self.theta -= self._WRAP

    def monitor(self, x: float, y: float) -> None:
        """对应固件 sfra_monitor()：channel 0 采 Y（PLANT 还采 X），channel 1 直接返回。

        **采满 data_count 点就必须停**：调用方惯常会多跑几拍收尾，
        若继续累加，窗口尾部就多出几个不属于整周期的采样点
        （实测 nacc 比 dc 多 2~3 点，20kHz 误差到 +32dB / +158°）。
        固件里 sfra_state 到 DONE 后同样不再累加，两边行为要一致。
        """
        if self._channel != 0 or not self._collecting:
            return
        if self.acc.n >= self._total:
            return
        c, s = self._cs(self.theta)
        self.acc.y_c += y * c
        self.acc.y_s += y * s
        if self.mode == MODE_PLANT:
            self.acc.x_c += x * c
            self.acc.x_s += x * s
        self.acc.n += 1          # 计数与模式无关，否则 LOOP 的 done 永远为假

    @property
    def done(self) -> bool:
        return self.acc.n >= self._total and self._collecting

    # ---- 收尾：打包成上报格式 ----
    def make_point(self, f_act: float) -> RawPoint:
        a = self.acc
        if self.mode == MODE_PLANT:
            return RawPoint(f_act, a.y_c, a.y_s, a.x_c, a.x_s)
        # LOOP：b 通道放 Inj 累加器（与 Y 同一轮采集）
        return RawPoint(f_act, a.y_c, a.y_s, a.inj_c, a.inj_s)


# ---------------------------------------------------------------- 扫频流程
def sweep(isr_freq: float,
          plant_fn,
          freqs,
          mode: int = MODE_LOOP,
          amplitude: float = 0.01,
          cyc_mode: int = CYC_NORMAL,
          compensate_fn=None,
          progress=None,
          ref_dc: float = 0.5) -> List[RawPoint]:
    """在 PC 上完整跑一次扫频（用于 L0 验证与无硬件演示）。

    plant_fn(x) -> y          被控功率级（一次调用 = 一个 ISR 周期）
    compensate_fn(e) -> u     补偿器，输入误差、输出执行量
    progress(done, total)     进度回调
    ref_dc                    闭环参考工作点（pu）

    **闭环拓扑**（与 05 文档的 ISR 模板一致）：
        y  = ADC 读反馈
        r  = ref_dc + inject()      ← 注入加在参考
        e  = r - y
        u  = compensate_fn(e)
        y  = plant(u)               ← 本周期执行、下周期才被采到（天然的 z^-1）
        monitor(u, y)

    **先跑到稳态再注入**：真实硬件也是这么做的（固件 SETTLE 状态）。
    跳过这一步，积分器会在一开始就把系统推到饱和，整条扫频全是垃圾数据。

    这里的 `plant_fn(u)` 返回的是**下一周期**才进入 monitor 的值：
    用 y_prev 变量错一拍，正好复刻真实数字控制的一拍计算延迟。少错这一拍，
    GH 高频段会比实测"好"十几度（04 文档 Step 3 的经典坑）。
    """
    sim = SimSfra(isr_freq=isr_freq, amplitude=amplitude, mode=mode)
    out: List[RawPoint] = []
    n_total = len(freqs)

    u_dc = 0.0
    y_prev = 0.0

    if mode == MODE_LOOP and compensate_fn is not None:
        # ---- 稳态预跑：让积分器收敛（约 2000 个 ISR 周期）----
        for _ in range(2000):
            e = ref_dc - y_prev
            u = compensate_fn(e)
            y_prev = plant_fn(u)
        u_dc = 0.0

    for i, f_nom in enumerate(freqs):
        cyc = calc_cycles(float(f_nom), cyc_mode, isr_freq)
        data_count, f_act = sim.begin_point(float(f_nom), cyc)
        settle = sim.settle_left          # inject() 会递减，先存下来

        # 单 pass 同时采 Y 与 Inj（D19 修正：两次 pass 激励不同步，Inj−Y 无意义）
        for _ in range(settle + data_count + 4):
            inj = sim.inject()

            if mode == MODE_PLANT:
                x = u_dc + inj           # 开环：注入直接叠加在执行量上
                y = plant_fn(x)          # 本周期算出、本周期就采（开环无延迟）
                sim.monitor(x, y)
            else:
                r = ref_dc + inj         # 闭环：注入加在参考
                e = r - y_prev           # 用**上一拍**的反馈
                u = compensate_fn(e) if compensate_fn else e
                # 本拍算出 u，先记录 u 与**当前**反馈 y_prev，
                # 再去推进功率级产出下一拍的 y。
                # 顺序写反（先 plant_fn 再 monitor）会得到 u[k] 与 y[k] 同拍，
                # 等于凭空少掉一拍计算延迟——GH 高频相位会偏乐观十几度。
                sim.monitor(u, y_prev)
                y_prev = plant_fn(u)     # y[k+1] = P(u[k])
            sim._advance()               # 采样之后再推进相位（见 _advance 注释）

        out.append(sim.make_point(f_act))

        if progress is not None:
            progress(i + 1, n_total)

    return out

