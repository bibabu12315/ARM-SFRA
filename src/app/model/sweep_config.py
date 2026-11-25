"""SFRA Studio —— 扫频配置（纯数据 + 纯计算，无 Qt 依赖）。

02 文档 §2.3 要求这个模块存在，之前相关逻辑散在两处：
    tab_measure._recalc_estimate()   ← 界面上的"预计 N 点 / 约 xx s"
    engine.SimWorker.run()           ← 仿真后端自己造频率表
两条路各算一遍，就容易出现"界面说 68 点、实际扫 48 点"这类分歧。

本模块把三件事收敛到一处，界面与两种后端（仿真 / 真链路）**都调这里**：

1. 频率表生成（对数等距）
2. **f_s/5 奈奎斯特实用上限截断**（01 文档 1.10 / 固件 SFRA_NYQ_DIV=5）
3. 按 **f_act** 而非名义频率估耗时（D7）

关于第 2 条为什么必须放在 PC 侧也做一遍：
    固件 `sfra_effective_points()` 会把点表裁到 f_s/5，但**配置阶段**
    用户看到的是 PC 算出来的点数。若 PC 不裁，就会承诺 68 点、实际只
    扫出 48 点，用户会以为工具坏了（05 文档典型坑表里那条）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Tuple

import numpy as np

#: 实用上限 = f_s / NYQ_DIV。与固件 `SFRA_NYQ_DIV` 必须同值。
NYQ_DIV = 5

#: 固件 SET_CONFIG 校验的幅值上限（`sfra_proto.c` do_set_config，0.25 pu）
FW_AMP_MAX = 0.25
#: 固件 SET_CONFIG 校验的点数上限（vec_length > 256 → BAD_PARAM）
FW_VEC_MAX = 256

#: 周期数档位（与固件 SFRA_CYC_* 同值）
CYC_FAST = 0
CYC_NORMAL = 1
CYC_PRECISE = 2

#: 模式（与 model.bode_data / 固件 SFRA_MODE_* 同值）
MODE_PLANT = 0
MODE_LOOP = 1
MODE_NAMES = {MODE_PLANT: "Plant", MODE_LOOP: "Loop Gain"}


# ----------------------------------------------------------------- 周期数
def calc_cycles(foi_hz: float, cyc_mode: int, isr_freq: float) -> int:
    """选周期数——**委托给 `sim.sfra_sim.calc_cycles`，不另写一份**。

    曾经想在这里抄一份，立刻踩到 MIN_SPC=16 与 8 的分歧：两份实现各自
    演化正是 A11 的教训。周期数策略必须只有一处，PC 估耗时和 C 端
    `sfra_calc_cycles` 才对得上。
    """
    from ..sim import sfra_sim            # 延迟导入，避免 sim↔model 循环
    return int(sfra_sim.calc_cycles(float(foi_hz), int(cyc_mode),
                                    float(isr_freq)))


# ----------------------------------------------------------------- 频率表
def make_freqs(f_start: float, f_stop: float, ppd: int,
               isr_freq: float | None = None) -> np.ndarray:
    """按每 decade 点数生成对数等距频率表，**已含 f_s/5 截断**。

    ppd=20 → 每点 1.122 倍。点数 = floor(log10(stop/start) * ppd) + 1。
    `isr_freq` 给了就裁到 f_s/NYQ_DIV；不给则只按用户终点裁（预扫等
    内部调用需要完整表时用）。
    """
    if f_stop <= f_start or ppd <= 0:
        return np.asarray([], dtype=float)
    decades = math.log10(f_stop / f_start)
    n = int(math.floor(decades * ppd)) + 1
    freqs = np.logspace(math.log10(f_start),
                        math.log10(f_start) + (n - 1) / ppd, n)
    if isr_freq:
        f_nyq = float(isr_freq) / NYQ_DIV
        freqs = freqs[freqs <= f_nyq * (1.0 + 1e-9)]
    return freqs


def nominal_to_actual(isr_freq: float, freqs, cyc_mode: int):
    """名义频率 → (f_act, 单点秒数, data_count)。

    每一行对应固件 `sfra_point_geometry()` 的结果：整周期取整后
    f_act = cycles/data_count · f_s，**与名义值有偏离**（D7）。
    耗时只能按 f_act 算，否则低频段误差能到 10%（cycles 少、
    data_count 向上取整，偏差最明显）。
    """
    out: List[Tuple[float, float, int]] = []
    for f_nom in np.asarray(freqs, dtype=float):
        if f_nom <= 0:
            continue
        cyc = calc_cycles(float(f_nom), cyc_mode, isr_freq)
        # 与固件 sfra_point_geometry 同式：data_count = ceil(f_s·cycles/f)
        dc = int(math.ceil(float(isr_freq) * float(cyc) / float(f_nom)))
        if dc <= 0:
            continue
        f_act = float(cyc) / float(dc) * float(isr_freq)
        out.append((f_act, float(cyc) / max(f_act, 1e-9), dc))
    return out


def estimate_seconds(isr_freq: float, freqs, cyc_mode: int,
                     mode: int = MODE_LOOP) -> float:
    """预估总耗时（单 pass，D19：LOOP 与 PLANT 相同，不再 ×2）。"""
    del mode                       # 保留参数只为调用处语义清楚
    return float(sum(row[1] for row in nominal_to_actual(isr_freq, freqs,
                                                         cyc_mode)))


def effective_points(f_start: float, f_stop: float, ppd: int,
                     isr_freq: float) -> int:
    return len(make_freqs(f_start, f_stop, ppd, isr_freq))


def freq_step_ratio(ppd: int) -> float:
    """每 decade 点数 → 相邻点倍数（= 10^(1/ppd)），下发 SET_CONFIG 用。"""
    return float(10.0 ** (1.0 / max(1, int(ppd))))


# ----------------------------------------------------------------- 配置对象
@dataclass
class SweepConfig:
    """一次扫频的完整配置。界面填、后端读，中间不传裸 dict。"""

    isr_freq: float = 100_000.0
    f_start: float = 100.0
    f_stop: float = 20_000.0
    ppd: int = 20
    cyc_mode: int = CYC_NORMAL
    mode: int = MODE_LOOP
    amp_pu: float = 0.01
    #: 仿真后端专用（被测对象），真链路忽略
    dut: str = "lc"
    f0c: float = 1300.0
    q: float = 2.0
    kp: float = 0.5
    ki: float = 1200.0
    ref_dc: float = 0.5

    # ---- 派生 ----
    @property
    def freqs(self) -> np.ndarray:
        return make_freqs(self.f_start, self.f_stop, self.ppd, self.isr_freq)

    @property
    def n_points(self) -> int:
        return len(self.freqs)

    @property
    def f_nyq(self) -> float:
        """实用上限 f_s/NYQ_DIV。超过的点会被裁掉。"""
        return self.isr_freq / NYQ_DIV

    @property
    def f_nyq_requested(self) -> bool:
        """用户填的终点是否被 f_s/5 截过——界面要据此提示。"""
        return self.f_stop > self.f_nyq * (1.0 + 1e-9)

    def estimate_seconds(self) -> float:
        return estimate_seconds(self.isr_freq, self.freqs, self.cyc_mode,
                                self.mode)

    @property
    def freq_step(self) -> float:
        return freq_step_ratio(self.ppd)

    def validate(self):
        """返回 [(提示文字, 是否错误)]。错误 = 不能开扫；警告 = 能扫但不可信。

        这些判据与固件 `do_set_config` 的校验**必须同源**：PC 先挡下来，
        用户看到的是可读的中文；让固件回 BAD_PARAM(2) 时只剩一个错误码。
        """
        out = []
        if self.n_points == 0:
            out.append(("终点频率必须大于起点频率", True))
            return out
        if self.f_nyq_requested:
            out.append((f"终点 {self.f_stop:.0f} Hz 超过实用上限 f_s/5 = "
                        f"{self.f_nyq:.0f} Hz，已自动截到 {self.n_points} 点", False))
        if self.amp_pu <= 0 or self.amp_pu > FW_AMP_MAX:
            out.append((f"注入幅值必须在 0 ~ {FW_AMP_MAX*100:.0f} %pu"
                        f"（固件上限 0.25 pu）", True))
        if self.n_points > FW_VEC_MAX:
            out.append((f"点数 {self.n_points} 超过固件上限 {FW_VEC_MAX}，"
                        f"请降低每 decade 点数", True))
        spc_min = self.isr_freq / max(float(self.freqs[-1]), 1e-9)
        if spc_min < 4:
            out.append((f"最高频点每周期只有 {spc_min:.1f} 个采样点（建议 ≥ 4），"
                        f"高频段不可信", False))
        return out

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, str(self.mode))

    def to_sim_cfg(self) -> dict:
        """转成 `sim.engine.SimWorker` 需要的 dict（兼容旧调用）。

        **必须带 ppd**：`freqs` 已经按 ppd 生成好了，但接收端
        （`LinkController._from_dict`）会用 ppd **重新生成**一遍 freqs。
        漏掉这一项，接收端就会退回它自己的默认值 20，界面填 40 也会
        被覆盖成 20——表现是"界面显示 93 点、实际扫 47 点"。
        """
        return {
            "isr_freq": self.isr_freq,
            "mode": self.mode,
            "amp_pu": self.amp_pu,
            "cyc": self.cyc_mode,
            "ppd": self.ppd,
            "freqs": self.freqs,
            "dut": self.dut,
            "f0": self.f0c,
            "fc": self.f0c,
            "q": self.q,
            "ref_dc": self.ref_dc,
            "comp": {"kp": self.kp, "ki": self.ki},
        }
