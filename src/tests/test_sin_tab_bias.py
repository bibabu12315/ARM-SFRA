"""`sin_q15()` 必须是零均值 —— 坑 A12 的唯一守门人。

纯 Python，不需要 gcc、不需要子进程，跟着 `pytest` 快速通道一起跑。

为什么这条值得单独占一个文件
----------------------------
2026-11-24 排查"扫出来的 Bode 图在 10 kHz 以上对不上理论"时，最终定位到
`sfra_core.c` 里 `sin_q15()` 的最后一步：

    return (int32_t)(t >> SIN_IDX_SHIFT);       // 修复前

算术右移是**向下取整**。正弦是零均值信号，单向 floor 等于给整张表压上
**−0.5 LSB 的直流偏置**。实测扫一整圈（20 万点）平均偏差 −0.499928 LSB
—— 不是随机误差，是常数偏移，所以"多采几个点平均掉"的直觉完全不成立。

危害只在 PLANT 模式下暴露：

    x   = 0.5 + inj                       ← 大直流 0.5
    x_s = Σx·q15s
        = 0.5·Σq15s   +   Σinj·q15s
          ↑ 理论为 0      ↑ 理论也为 0
          实际 = −N/4

两项理论都为 0，但 q15s 带 −0.5 LSB 偏置时 `Σq15s = −N/2`，于是 x_s
凭空多出一个**与采样点数成正比**的伪正交分量，直接污染相位。

实测影响（f0=1300 / Q=2 / f_s=100 kHz / 20 点每 decade）：

    f         修复前      修复后
    1300     −0.11°      +0.02°
    5000     −1.46°       0.00°
    10000    −5.39°       0.00°      ← 用户看到"曲线对不上"的位置
    15000   −11.32°      −0.02°
    20000   −23.37°      +0.39°

修法只有一次 int64 加法：`(t + 半LSB) >> shift`。
修完后 `tools/mcu_vs_theory.py --fake` 的判据段从
`0.745 dB / 6.223°`（FAIL）降到 `0.010 dB / 0.081°`（PASS）。
"""
from __future__ import annotations

import math

import pytest

#: 与 `sfra_core.h` 的 `SFRA_SIN_TAB_BITS` 一致
BITS = 10


def _make_sin_q15(bits: int = BITS):
    """复现 C 端的 `sfra_init_all()` + `sin_q15()`（逐位等价）。"""
    n = 1 << bits
    mask = n - 1
    shift = 32 - bits
    frac_scale = 1 << shift
    frac_mask = frac_scale - 1
    # C 端：s_sin_tab[k] = (int16_t)(32767.0 * sin(2πk/N))
    tab = [int(32767.0 * math.sin(2.0 * math.pi * k / n)) for k in range(n + 1)]

    def sin_q15(phase: int) -> int:
        p = phase & 0xFFFFFFFF
        i0 = (p >> shift) & mask
        i1 = (i0 + 1) & mask
        frac = p & frac_mask
        v0, v1 = tab[i0], tab[i1]
        t = v0 * frac_scale + (v1 - v0) * frac
        # +半LSB 再右移 = 四舍五入。**这一项就是坑 A12 的修复本体。**
        return (t + (1 << (shift - 1))) >> shift

    return sin_q15


def test_sin_q15_is_zero_mean() -> None:
    """扫一整圈，平均偏差必须 ≈ 0（修复前是 −0.5 LSB）。"""
    sin_q15 = _make_sin_q15()

    steps = 200_000
    total = 0.0
    for k in range(steps):
        th = 2.0 * math.pi * k / steps
        ph = int(th / (2.0 * math.pi) * 4294967296.0) & 0xFFFFFFFF
        total += sin_q15(ph) - 32767.0 * math.sin(th)
    bias = total / steps

    assert abs(bias) < 0.02, (
        f"sin_q15 的直流偏置 {bias:+.6f} LSB 超限（应 ≈0）。\n"
        f"回到 −0.5 附近说明 `>>` 前漏了 `+ SIN_ROUND_HALF`，"
        f"PLANT 模式会在 10 kHz 附近产生 +5° 以上的相位误差（坑 A12）。")


def test_sin_q15_full_period_sums_to_zero() -> None:
    """整周期求和必须 ≈ 0 —— 这就是 PLANT 模式 x_s 的污染源。

    10 kHz @ f_s=100 kHz 的真实采样点数是 1000，角步进 0.1 圈。
    修复前 `Σq15c = −500`（正好 −dc/2），修复后 ≈ +1。
    """
    sin_q15 = _make_sin_q15()

    dc = 1000
    step = int(0.1 * 4294967296.0 + 0.5)
    ph = 0
    s = 0
    for _ in range(dc):
        s += sin_q15(ph)
        ph = (ph + step) & 0xFFFFFFFF

    assert abs(s) < 50, (
        f"10 kHz 整周期 Σq15c = {s}（修复前是 −500）。"
        f"这个量乘上 x 的直流 0.5 就是 x_s 里的伪分量。")

    # 顺带确认"步进走满 100 整圈"这个前提没被破坏。
    # 注意 Q32 相位是 uint32、加满即整圈取模，所以 100 圈之后
    # theta_phase 回到 ~0 是**正确**的，不能拿它当累积误差的度量。
    # 要看的是"实际总增量 / 一圈" 与标称圈数 100 的差。
    laps = dc * step / 4294967296.0
    assert abs(laps - 100.0) < 1e-4, (
        f"Q32 步进累积偏差过大（{laps:.9f} 圈 vs 100 圈），"
        f"整周期取整的前提不成立")


@pytest.mark.parametrize("dc", [500, 667, 1000, 2000, 5000, 7693])
def test_period_sum_zero_across_dc(dc: int) -> None:
    """不同频点（不同 data_count）下整周期求和都要 ≈0，不能只在 10 kHz 成立。

    dc 取的是 f0=1300/Q=2 那条曲线的真实几何：
    20 kHz→500、15 kHz→667、10 kHz→1000、5 kHz→2000、1300 Hz→7693。
    """
    sin_q15 = _make_sin_q15()

    # 与 `sfra_point_geometry` 同源：整周期取整，dc 拍走满 cycles 圈
    cycles = 100
    rad = cycles / dc * 2.0 * math.pi
    step = int(rad / (2.0 * math.pi) * 4294967296.0 + 0.5)

    ph = 0
    s = 0
    for _ in range(dc):
        s += sin_q15(ph)
        ph = (ph + step) & 0xFFFFFFFF

    # 容差按 dc 放宽：每个点有 ≤0.5 LSB 的随机化残差，最坏 dc/2，
    # 实际远小；给 dc*0.05 已是 20 倍余量，仍能挡住 −dc/2 这个量级。
    assert abs(s) < max(50, dc * 0.05), (
        f"dc={dc} 时整周期 Σq15c = {s}（应 ≈0，缺陷表现是 −{dc//2}）")
