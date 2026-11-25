"""锁住 `sim.theory` 的数学 —— 这些数字是全部验收判据的地基。

为什么这个测试必须存在
----------------------
`theory.py` 是三件事的**唯一实现处**：连续域解析解、Tustin 离散、
ZOH+延迟。后续 S6a/S6b 的判据、`tools/bode_reference.py` 的基准线
全都引用它。这里算错一个常数，会用"看起来正确的基准"把所有真 bug
判成 PASS —— 正是 06 文档里 `test_high_freq_slope_violation_is_caught`
抓到的那类假判据。

所以每一条都对着**手算就能验的闭式结果**断言，而不是对着
"上一次跑出来的数"做快照。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from app.sim.theory import (AnalogLC, AnalogLP1, TustinLC, TustinLP1,  # noqa: E402
                            ZohPlant, compare, make_pair, response_table)

FS = 100_000.0


# ---------------------------------------------------------------- 一阶低通
class TestAnalogLP1:
    def test_fc_is_minus_3db_minus_45deg(self):
        """fc 处必须精确是 −3.0103 dB / −45°。"""
        m = AnalogLP1(fc=1000.0)
        assert m.mag_db(1000.0) == pytest.approx(-3.0103, abs=1e-3)
        assert m.phase_deg(1000.0) == pytest.approx(-45.0, abs=1e-6)

    def test_slope_is_minus_20db_per_decade(self):
        """高频斜率 −20 dB/dec。"""
        m = AnalogLP1(fc=100.0)
        slope = (m.mag_db(10000.0) - m.mag_db(1000.0)) / math.log10(10.0)
        assert slope == pytest.approx(-20.0, abs=0.2)

    def test_dc_gain_is_unity(self):
        assert AnalogLP1(fc=1000.0).mag_db(1e-9) == pytest.approx(0.0, abs=1e-6)


class TestTustinLP1:
    def test_dc_gain_is_exactly_unity(self):
        """Tustin 离散后直流增益必须精确为 1。

        这是最容易算错的一处：分母写成 `1 − a1·z⁻¹` 会得到 1/159
        （06 文档 §6.4 记过这个坑）。
        """
        t = TustinLP1(fc=1000.0, fs=FS)
        assert abs(t.h(0.0)) == pytest.approx(1.0, abs=1e-12)

    def test_fc_matches_analog(self):
        """fc 处离散与连续差 < 0.01 dB / 0.1°。"""
        a, t = AnalogLP1(fc=1000.0), TustinLP1(fc=1000.0, fs=FS)
        assert t.mag_db(1000.0) - a.mag_db(1000.0) == pytest.approx(0.0, abs=0.01)
        assert t.phase_deg(1000.0) - a.phase_deg(1000.0) == pytest.approx(
            0.0, abs=0.1)

    def test_nyquist_warp_is_minus_1p26db(self):
        """f_s/5 处 Tustin 比连续域低 1.26 dB。

        这个数字用 `test_tustin_warp_at_nyquist_is_about_1p26db`
        在 06 文档那边已锁过一次；这里从"理论模块"这一侧再锁一遍，
        因为基准线一旦漂移，S6a 判据就全线失效。
        """
        a, t = AnalogLP1(fc=1000.0), TustinLP1(fc=1000.0, fs=FS)
        assert t.mag_db(20000.0) - a.mag_db(20000.0) == pytest.approx(
            -1.259, abs=0.01)

    def test_phase_is_negative_everywhere(self):
        t = TustinLP1(fc=1000.0, fs=FS)
        for f in (1.0, 100.0, 1000.0, 10000.0, 20000.0):
            assert t.phase_deg(f) < 0.0


# ---------------------------------------------------------------- 二阶 LC
class TestAnalogLC:
    def test_dc_gain_is_K(self):
        assert AnalogLC(f0=1300.0, Q=2.0, K=1.0).dc_db == pytest.approx(
            0.0, abs=1e-12)
        assert AnalogLC(f0=1300.0, Q=2.0, K=12.0).dc_db == pytest.approx(
            20.0 * math.log10(12.0), abs=1e-12)

    def test_resonance_is_Q_and_minus_90(self):
        """f0 处幅值 = K·Q（6.02 dB @ Q=2），相位 −90°。"""
        m = AnalogLC(f0=1300.0, Q=2.0)
        assert m.mag_db(1300.0) == pytest.approx(20.0 * math.log10(2.0),
                                                 abs=1e-9)
        assert m.phase_deg(1300.0) == pytest.approx(-90.0, abs=1e-9)

    def test_high_freq_slope_is_minus_40db_per_decade(self):
        m = AnalogLC(f0=100.0, Q=0.707)
        slope = (m.mag_db(100000.0) - m.mag_db(10000.0)) / math.log10(10.0)
        assert slope == pytest.approx(-40.0, abs=1.0)

    def test_phase_approaches_minus_180(self):
        m = AnalogLC(f0=1300.0, Q=2.0)
        assert m.phase_deg(1e6) == pytest.approx(-180.0, abs=0.1)

    def test_vin_sets_dc_gain(self):
        """`K=Vin` 才是真实 Buck：12 V → +21.58 dB。

        这是用户"不知道真实长什么样"的直接答案之一：上位机的
        `plants.SecondOrderLC` 用归一化形式（直流 0 dB），
        与真实功率级整体差 Vin 倍，形状相同、高度不同。
        """
        m = AnalogLC(f0=1300.0, Q=2.0, K=12.0)
        assert m.mag_db(1e-6) == pytest.approx(21.5836, abs=1e-3)


class TestTustinLC:
    def test_low_freq_matches_analog(self):
        """f ≤ f0 段两域一致（差 < 0.1 dB / 0.2°）。"""
        a = AnalogLC(f0=1300.0, Q=2.0)
        t = TustinLC(f0=1300.0, Q=2.0, fs=FS)
        for f in (10.0, 100.0, 500.0, 1300.0):
            assert abs(t.mag_db(f) - a.mag_db(f)) < 0.1
            assert abs(t.phase_deg(f) - a.phase_deg(f)) < 0.2

    def test_nyquist_warp_is_negative(self):
        """f_s/5 处离散幅值应**低于**连续域（翘曲是单向的）。"""
        a = AnalogLC(f0=1300.0, Q=2.0)
        t = TustinLC(f0=1300.0, Q=2.0, fs=FS)
        d = t.mag_db(20000.0) - a.mag_db(20000.0)
        assert d < 0.0
        assert d == pytest.approx(-2.53, abs=0.05)

    def test_zero_at_nyquist_returns_neg_inf_not_exception(self):
        """f_s/2 处二阶 Tustin 有数字零点 → 幅值 −inf，不能抛异常。

        `math.log10(0)` 会抛 `ValueError: math domain error`，
        而"扫到 f_s/2 附近掉到极低"正是应有的表现。
        """
        t = TustinLC(f0=1300.0, Q=2.0, fs=FS)
        assert t.mag_db(FS / 2.0) == float("-inf")

    def test_coeffs_match_plants_module(self):
        """系数必须与 `sim.plants.SecondOrderLC` 完全一致。

        两处实现同一个滤波器是隐患（A11 的教训）；这里用数值锁住，
        一旦有人只改一边，测试立刻红。
        """
        from app.sim.plants import SecondOrderLC
        f0, Q = 1300.0, 2.0
        t = TustinLC(f0=f0, Q=Q, fs=FS)
        p = SecondOrderLC(f0=f0, Q=Q, fs=FS)
        b0, b1, b2, a1, a2 = t.coeffs
        assert b0 == pytest.approx(p.b0, rel=1e-12)
        assert b1 == pytest.approx(p.b1, rel=1e-12)
        assert b2 == pytest.approx(p.b2, rel=1e-12)
        assert a1 == pytest.approx(p.a1, rel=1e-12)
        assert a2 == pytest.approx(p.a2, rel=1e-12)


# ---------------------------------------------------------------- ZOH + 延迟
class TestZohPlant:
    def test_dc_gain_is_K(self):
        z = ZohPlant(f0=1300.0, Q=2.0, fs=FS, K=1.0)
        assert z.h(0.0).real == pytest.approx(1.0, abs=1e-12)
        assert z.h(0.0).imag == pytest.approx(0.0, abs=1e-12)

    def test_zoh_only_phase_is_minus_wTs_over_2(self):
        """无延迟时，ZOH 的附加相位应恰好是 −ωTs/2。

        这是 ZOH 频域 `(1−e^{−sTs})/(sTs)` 的闭式结果，
        在 f ≪ f_s/2 时误差 < 1°。
        """
        z = ZohPlant(f0=1300.0, Q=2.0, fs=FS, delay=0)
        a = AnalogLC(f0=1300.0, Q=2.0)
        for f in (1000.0, 5000.0, 10000.0):
            om = 2.0 * math.pi * f
            expect = -math.degrees(om / FS / 2.0)
            d = z.phase_deg(f) - a.phase_deg(f)
            # 相位可能跨 ±180，取最短路径
            d = (d + 180.0) % 360.0 - 180.0
            assert d == pytest.approx(expect, abs=2.0), f"f={f}"

    def test_one_sample_delay_adds_minus_wTs(self):
        """加一拍延迟，相位再滞后 ωTs（20 kHz @100 kHz = −72°）。"""
        z0 = ZohPlant(f0=1300.0, Q=2.0, fs=FS, delay=0)
        z1 = ZohPlant(f0=1300.0, Q=2.0, fs=FS, delay=1)
        f = 20000.0
        d = z1.phase_deg(f) - z0.phase_deg(f)
        d = (d + 180.0) % 360.0 - 180.0
        assert d == pytest.approx(-72.0, abs=1.0)

    def test_phase_lag_at_fs_over_5(self):
        """f_s/5 处总滞后应超过 90°，比裸连续域危险得多。

        ZOH(−36°) + 延迟(−72°) = −108°，扣掉 LC 自身 −178°…
        实际累加到 −286°（相当于 +74°）。这里只断言"比连续域更滞后"，
        防止有人把 ZOH/延迟漏掉而不自知。
        """
        z = ZohPlant(f0=1300.0, Q=2.0, fs=FS, delay=1)
        a = AnalogLC(f0=1300.0, Q=2.0)
        # 用未缠绕的等效相位比较：把连续域相位当作起点，累加理论滞后
        expect = a.phase_deg(20000.0) - 36.0 - 72.0
        got = z.phase_deg(20000.0)
        # 允许 ±360 的缠绕
        best = min(abs(got - expect + 360.0 * k) for k in (-1, 0, 1))
        assert best < 2.0, f"got {got:.2f}, expect {expect:.2f}"


# ---------------------------------------------------------------- 工具函数
class TestCompare:
    def test_identical_models_have_zero_deviation(self):
        a = AnalogLC(f0=1300.0, Q=2.0)
        r = compare(a, a, [100.0, 1000.0, 5000.0])
        assert r["max_db"] == pytest.approx(0.0, abs=1e-9)
        assert r["max_deg"] == pytest.approx(0.0, abs=1e-5)

    def test_tustin_deviation_is_reported(self):
        a, t = make_pair("lc", fs=FS, f0=1300.0, q=2.0)
        r = compare(a, t, [100.0, 20000.0])
        assert r["max_db"] > 1.0            # 20k 处约 2.5 dB
        assert r["n"] == 2


class TestResponseTable:
    def test_shapes_are_consistent(self):
        t = response_table(AnalogLC(f0=1300.0, Q=2.0), [100.0, 2000.0])
        assert len(t["f"]) == len(t["mag_db"]) == len(t["phase_deg"]) == 2

    def test_resonance_peak_is_higher_than_neighbours(self):
        """f0 两侧的幅值都必须低于 f0 本身（Q=2 时凸峰 6 dB）。

        注意别把"高频端"当参照：2000 Hz 已经越过 1300 Hz 的谐振点，
        幅值比直流还低——第一版测试就是这里想当然写错了。
        """
        m = AnalogLC(f0=1300.0, Q=2.0)
        t = response_table(m, [300.0, 1300.0, 5000.0])
        assert t["mag_db"][1] > t["mag_db"][0]
        assert t["mag_db"][1] > t["mag_db"][2]

    def test_phase_goes_through_minus_90_at_resonance(self):
        m = AnalogLC(f0=1300.0, Q=2.0)
        t = response_table(m, [300.0, 1300.0, 5000.0])
        assert t["phase_deg"][1] == pytest.approx(-90.0, abs=1e-6)
        assert t["phase_deg"][0] > -90.0
        assert t["phase_deg"][2] < -90.0

    def test_make_pair_lp1(self):
        a, t = make_pair("lp1", fs=FS, fc=1000.0)
        assert isinstance(a, AnalogLP1) and isinstance(t, TustinLP1)
        assert t.fs == FS

    def test_make_pair_rejects_unknown(self):
        with pytest.raises(ValueError):
            make_pair("foo", fs=FS)


# ---------------------------------------------------------------- 与 plants 对齐
class TestAgainstPlants:
    def test_theory_tustin_matches_plants_step_response(self):
        """`theory.TustinLC` 与 `plants.SecondOrderLC` 时域响应必须一致。

        系数相同（前一条测试已锁）不代表求值也对——这里喂同一个
        脉冲序列跑 200 拍，逐拍比对输出。
        """
        from app.sim.plants import SecondOrderLC
        f0, Q, K = 1300.0, 2.0, 3.0
        p = SecondOrderLC(f0=f0, Q=Q, fs=FS)
        t = TustinLC(f0=f0, Q=Q, fs=FS, K=1.0)

        # plants 是归一化形式（分子为 1），theory 用 K 缩放
        # → 把 K 归一化到 1 后系数应完全一致，时域输出也应一致
        xs = [1.0] + [0.0] * 199
        ys = [p(x) for x in xs]

        # 用差分方程复算（系数取自 theory）
        b0, b1, b2, a1, a2 = t.coeffs
        x1 = x2 = y1 = y2 = 0.0
        ys2 = []
        for x in xs:
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, x
            y2, y1 = y1, y
            ys2.append(y)
        for a, b in zip(ys, ys2):
            assert a == pytest.approx(b, abs=1e-12)
