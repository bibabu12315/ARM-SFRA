"""扫频算法测试（04 文档 L0 gate 的核心）。

Plant 模式的判据是**与解析解逐点比对**：离散二阶 LC 的 H(z) 可以
精确算出来，所以能做到 0.05dB / 0.1° 的对齐要求。这是整个项目
"算法对不对"的唯一硬证据——比肉眼看曲线形状可靠得多。

这里同时把开发过程中踩过的几个坑固化成回归测试：
    1. LOOP 模式 n 不自增 → done 永假
    2. Inj 与 Y 分两个 pass 采 → Inj−Y 恒等于 0
    3. settle 用周期数换算 → 高频采集窗被吃光
    4. PI 的 Ki 忘了乘 Ts → 积分器发散
"""
import math

import numpy as np
import pytest

from app.model.bode_data import (MODE_LOOP, MODE_PLANT, SweepResult,
                                    h_from_raw, mag_db, phase_deg)
from app.model.analysis import analyze
from app.sim.plants import FirstOrderLP, PICompensator, SecondOrderLC
from app.sim.sfra_sim import SimSfra, point_geometry
from app.sim.sfra_sim import (CYC_FAST, CYC_NORMAL, CYC_PRECISE, SimSfra,
                              actual_freq, calc_cycles, point_geometry,
                              sweep)


FS = 100_000.0


# ---------------------------------------------------------------- 周期数
def test_calc_cycles_normal():
    assert calc_cycles(5.0, CYC_NORMAL) == 10
    assert calc_cycles(50.0, CYC_NORMAL) == 50
    assert calc_cycles(5000.0, CYC_NORMAL) == 100


def test_calc_cycles_fast_respects_min_seconds():
    """Fast 档也要保证每点 ≥20ms，否则低频信噪比不够。"""
    assert calc_cycles(10.0, CYC_FAST) >= 8
    assert calc_cycles(5000.0, CYC_FAST) >= 100      # 20ms × 5kHz


def test_calc_cycles_precise():
    assert calc_cycles(5.0, CYC_PRECISE) == 20
    assert calc_cycles(5000.0, CYC_PRECISE) == 200


# ---------------------------------------------------------------- 整周期取整
def test_point_geometry_is_exact():
    """data_count × step 必须精确等于 cycles×2π（无泄漏）。"""
    for f in (100.0, 1234.0, 7777.0, 19999.0):
        for cyc in (8, 20, 100):
            dc, step = point_geometry(FS, f, cyc)
            assert abs(dc * step - cyc * 2 * math.pi) < 1e-9


def test_actual_freq_near_nominal():
    for f in (100.0, 1000.0, 10000.0):
        dc, _ = point_geometry(FS, f, 100)
        fa = actual_freq(FS, dc, 100)
        assert abs(fa - f) / f < 0.02


# ---------------------------------------------------------------- 累加器自增
def test_monitor_counts_in_loop_mode():
    """回归：LOOP 模式下 monitor 也必须累加 n，否则 done 永远为假。"""
    sim = SimSfra(isr_freq=FS, amplitude=0.01, mode=MODE_LOOP)
    dc, _ = sim.begin_point(1000.0, 20)
    for _ in range(sim.settle_left + dc + 4):
        sim.inject()
        sim.monitor(0.0, 0.1)
    assert sim.acc.n >= dc
    assert sim.done or sim.acc.n > 0


def test_inj_collected_in_same_pass_as_y():
    """回归：LOOP 模式 Inj 和 Y 必须同一轮采集，否则 Inj−Y 无意义。"""
    sim = SimSfra(isr_freq=FS, amplitude=0.01, mode=MODE_LOOP)
    dc, _ = sim.begin_point(1000.0, 20)
    for _ in range(sim.settle_left + dc + 4):
        sim.inject()
        sim.monitor(0.0, 0.1)
    assert sim.acc.inj_c != 0.0 or sim.acc.inj_s != 0.0


def test_settle_is_time_based_and_independent_of_window():
    """settle 是物理过渡时间，按"20ms 与 2 个注入周期"取大，与采集窗无关。

    早期版本把 settle 截断成 data_count//2，导致高频点 settle 只有几毫秒，
    被测对象尚未进入稳态就开始采集。这里锁死正确语义：
    settle 只由时间和注入周期决定，不受 cycles / data_count 影响。
    """
    for f in (100.0, 1000.0, 10000.0, 19999.0):
        sim = SimSfra(isr_freq=FS, amplitude=0.01, mode=MODE_PLANT)
        sim.begin_point(f, 20)
        expect = int(math.ceil(max(SimSfra.SETTLE_SECONDS,
                                   SimSfra.SETTLE_MIN_PERIODS / f) * FS))
        assert sim.settle_left == expect, f"{f}Hz: {sim.settle_left} != {expect}"
        # 低频一个注入周期就超过 20ms，settle 要跟着周期走
        assert sim.settle_left >= SimSfra.SETTLE_MIN_PERIODS / f * FS


def test_plant_accuracy_full_band():
    """全频段 Plant 精度：整周期取整 + 相位连续下应达到机器精度。

    这条测试同时锁住三个踩过的坑：
      1. theta 必须在 monitor 之后推进（否则整条曲线多 360/data_count 度）
      2. settle 期间/采集窗起点**不能复位 theta**（会造成相位台阶、
         高频 |H| 偏 +28dB）
      3. 采满 data_count 点后 monitor 必须停止累加（否则窗口尾部
         多出非整周期采样点）
    """
    worst_db = 0.0
    for f in (100.0, 500.0, 1000.0, 2000.0, 5000.0, 8000.0, 13000.0, 20000.0):
        for cycles in (100, 400):
            plant = SecondOrderLC(f0=2000.0, Q=0.8, fs=FS)
            sim = SimSfra(isr_freq=FS, amplitude=0.01, mode=MODE_PLANT)
            dc, f_act = sim.begin_point(f, cycles)
            for _ in range(sim.settle_left + dc + 2):
                inj = sim.inject()
                x = 0.5 + inj
                y = plant(x)
                sim.monitor(x, y)
                sim._advance()
            got = h_from_raw(sim.make_point(f_act), MODE_PLANT)
            assert sim.acc.n == dc, f"{f}Hz: 采了 {sim.acc.n} 点, 应为 {dc}"
            _, rad = point_geometry(FS, f, cycles)
            z1 = np.exp(-1j * rad)
            want = ((plant.b0 + plant.b1 * z1 + plant.b2 * z1 ** 2)
                    / (1 + plant.a1 * z1 + plant.a2 * z1 ** 2))
            d_db = 20 * np.log10(abs(got)) - 20 * np.log10(abs(want))
            d_ph = np.degrees(np.angle(got / want))
            worst_db = max(worst_db, abs(d_db))
            assert abs(d_db) < 1e-6 and abs(d_ph) < 1e-4, \
                f"{f}Hz/{cycles}: {d_db:.4f}dB {d_ph:.4f}deg"
    assert worst_db < 1e-6


# ---------------------------------------------------------------- Plant 精度
def _lc(f0=1300.0, q=2.0):
    return SecondOrderLC(f0=f0, Q=q, fs=FS)


def _lc_analytic(plant, f):
    """按同一组 b/a 系数算 H(z)，作为解析基准。"""
    w = 2 * np.pi * np.asarray(f) / FS
    z1 = np.exp(-1j * w)
    num = plant.b0 + plant.b1 * z1 + plant.b2 * z1 ** 2
    den = 1 + plant.a1 * z1 + plant.a2 * z1 ** 2
    return num / den


def test_plant_matches_analytic():
    """核心测试：Plant 扫频结果与 H(z) 解析解逐点对齐（近频段）。"""
    freqs = np.logspace(2, np.log10(5000), 20)
    plant = _lc()
    pts = sweep(FS, plant, freqs, mode=MODE_PLANT, amplitude=0.02,
                cyc_mode=CYC_NORMAL)

    for p in pts:
        h = h_from_raw(p, MODE_PLANT)
        th = _lc_analytic(plant, p.f_act)
        assert abs(mag_db(h) - 20 * np.log10(abs(th))) < 0.05
        assert abs(phase_deg(h) - math.degrees(np.angle(th))) < 0.5


def test_plant_first_order_3db_point():
    """一阶低通：截止频率处必须 -3dB / -45°。"""
    fc = 1000.0
    plant = FirstOrderLP(fc=fc, fs=FS)
    pts = sweep(FS, plant, np.array([fc]), mode=MODE_PLANT,
                amplitude=0.02, cyc_mode=CYC_NORMAL)
    h = h_from_raw(pts[0], MODE_PLANT)
    assert abs(mag_db(h) + 3.01) < 0.1
    assert abs(phase_deg(h) + 45.0) < 1.0


def test_plant_lc_resonance_phase():
    """二阶 LC 在谐振点相位约 -90°、幅值约 Q（=6dB）。"""
    f0 = 1300.0
    plant = _lc(f0=f0, q=2.0)
    pts = sweep(FS, plant, np.array([f0]), mode=MODE_PLANT,
                amplitude=0.01, cyc_mode=CYC_NORMAL)
    h = h_from_raw(pts[0], MODE_PLANT)
    assert abs(phase_deg(h) + 90.0) < 8.0
    assert abs(mag_db(h) - 6.0) < 1.0


# ---------------------------------------------------------------- PI
def test_pi_dc_gain_is_infinite():
    """积分器在 DC 增益无穷大：Ki 再小，稳态误差也要归零。"""
    plant = _lc()
    comp = PICompensator(kp=0.5, ki=1200, fs=FS, u0=0.5, u_min=0, u_max=1)
    y = 0.0
    for _ in range(20000):
        e = 0.5 - y
        u = comp(e)
        y = plant(u)
    assert abs(0.5 - y) < 1e-6


def test_pi_ki_scaled_by_ts():
    """回归：Ki 是连续域增益，离散时要乘 Ts。忘了乘会放大 1/Ts 倍。

    100 周期、e=0.01 恒定：Δu = 100 × (Ki·Ts) × e
    Ki=1000, Ts=1e-5 → 100 × 0.01 × 0.01 = 0.01
    """
    comp = PICompensator(kp=0.0, ki=1000.0, fs=FS, u0=0.0,
                         u_min=-1e9, u_max=1e9)
    for _ in range(100):
        comp(0.01)
    assert abs(comp.u - 0.01) < 1e-12


def test_pi_anti_windup():
    """撞上限幅后，积分状态也要被钳住，不能继续涨。"""
    comp = PICompensator(kp=1.0, ki=1e6, fs=FS, u0=0.0, u_min=0, u_max=1)
    for _ in range(1000):
        comp(1.0)
    assert comp.u <= 1.0 + 1e-12
    # 反向误差要能立刻退出饱和
    for _ in range(1000):
        comp(-1.0)
    assert comp.u < 1.0


# ---------------------------------------------------------------- 闭环
def test_loop_matches_analytic():
    """核心测试：闭环 GH 与 H(z)·G(z)·z⁻¹ 对齐（1kHz 以下）。"""
    freqs = np.logspace(2, 3, 12)
    plant = _lc()
    comp = PICompensator(kp=0.5, ki=1200, fs=FS, u0=0.5, u_min=0, u_max=1)
    pts = sweep(FS, plant, freqs, mode=MODE_LOOP, amplitude=0.01,
                cyc_mode=CYC_NORMAL, compensate_fn=comp)

    plant2 = _lc()          # sweep 内部用掉了 plant 的状态，重开一份算基准
    for p in pts:
        h = h_from_raw(p, MODE_LOOP)

        # G(z) = (Kp+Ki·Ts - Kp·z⁻¹)/(1-z⁻¹)
        w = 2 * np.pi * p.f_act / FS
        z1 = np.exp(-1j * w)
        g = (0.5 + 1200 / FS - 0.5 * z1) / (1 - z1)
        th = _lc_analytic(plant2, np.array([p.f_act]))[0] * g * z1

        assert abs(mag_db(h) - 20 * np.log10(abs(th))) < 1.5
        assert abs(phase_deg(h) - math.degrees(np.angle(th))) < 8.0


def test_loop_matches_analytic():
    """闭环 GH 必须逐点对上解析 H(z)·G(z)·z^-1。

    这是 LOOP 模式的唯一硬判据。以前这里断言"PM 落在 40~75°"，
    那是照着错误数据反推出来的区间（错误判据把低频点全砍掉，
    剩下高频段算出一个假的穿越点）。正确做法是跟解析解比：
    只要每个点都对上，PM/GM 自然就是对的。

    闭环拓扑：r = ref + inj ; e = r - y ; u = C(e) ; y = P(u)
    所以 u→y 的传递是 P(z)·z^-1（本拍算出、下拍才被采到），
    GH = P(z)·C(z)·z^-1。
    """
    kp, ki = 0.5, 1200.0
    freqs = np.logspace(2, np.log10(20000), 30)
    plant = _lc()
    # 保存一份系数，扫频会推进对象内部状态
    b0, b1, b2, a1, a2 = plant.b0, plant.b1, plant.b2, plant.a1, plant.a2

    comp = PICompensator(kp=kp, ki=ki, fs=FS, u0=0.5, u_min=0, u_max=1)
    pts = sweep(FS, plant, freqs, mode=MODE_LOOP, amplitude=0.02,
                cyc_mode=CYC_NORMAL, compensate_fn=comp, ref_dc=0.5)
    got = np.array([h_from_raw(p, MODE_LOOP) for p in pts])
    f = np.array([p.f_act for p in pts])

    z1 = np.exp(-1j * 2 * np.pi * f / FS)
    P = (b0 + b1 * z1 + b2 * z1 ** 2) / (1 + a1 * z1 + a2 * z1 ** 2)
    # 并行式 PI：u[k] = Kp·e[k] + I[k]，I[k] += Ki·Ts·e[k]
    #   → C(z) = Kp + Ki·Ts/(1 − z⁻¹)
    # 写成增量式 Kp + Ki·Ts·z/(z-1) 会多一个零点，20kHz 相位差 70°
    C = kp + ki / FS / (1.0 - z1)
    want = P * C * z1                        # 一拍计算延迟

    d_db = 20 * np.log10(np.abs(got)) - 20 * np.log10(np.abs(want))
    d_ph = np.degrees(np.angle(got / want))
    d_ph = (d_ph + 180.0) % 360.0 - 180.0

    # |GH| 很小时分母 Inj−Y 接近 Inj，float32 精度会带来一点误差
    assert np.abs(d_db).max() < 1.0, f"幅值偏差过大 {np.abs(d_db).max():.3f}dB"
    assert np.abs(d_ph).max() < 12.0, f"相位偏差过大 {np.abs(d_ph).max():.3f}deg"
    assert np.median(np.abs(d_db)) < 0.5


def test_loop_confidence_is_high_gh_side():
    """可信判据必须砍"分母过小"的高频段，不能砍低频段。

    回归：旧版写成 |Y|/|Inj| < 0.12 判坏，方向反了——
    |GH| 很小（低频）时 |Y| 也小，被判坏；|GH| 很大（高频）时
    |Y|→|Inj|，反而放过，结果 50kHz 算出 -194dB 照样当有效点。
    """
    freqs = np.logspace(2, np.log10(20000), 30)
    plant = _lc()
    comp = PICompensator(kp=0.5, ki=1200, fs=FS, u0=0.5, u_min=0, u_max=1)
    pts = sweep(FS, plant, freqs, mode=MODE_LOOP, amplitude=0.02,
                cyc_mode=CYC_NORMAL, compensate_fn=comp, ref_dc=0.5)
    res = SweepResult(mode=MODE_LOOP, raw=pts, isr_freq=FS)
    tk = res.trusted_mask()
    # 低频段（|GH| 小、数据准）必须全部保留
    assert tk[0] and tk[1] and tk[2]
    # 至少七成点可用
    assert tk.sum() >= 0.7 * len(pts)


def test_loop_higher_kp_lowers_pm():
    """Kp 越大 PM 越小——这就是用户想在界面上看到的趋势。"""
    freqs = np.logspace(2, np.log10(20000), 30)

    def pm_for(kp):
        plant = _lc()
        comp = PICompensator(kp=kp, ki=1200, fs=FS, u0=0.5, u_min=0, u_max=1)
        pts = sweep(FS, plant, freqs, mode=MODE_LOOP, amplitude=0.02,
                    cyc_mode=CYC_NORMAL, compensate_fn=comp)
        f = np.array([p.f_act for p in pts])
        h = np.array([h_from_raw(p, MODE_LOOP) for p in pts])
        return analyze(f, np.array([mag_db(v) for v in h]),
                       np.array([phase_deg(v) for v in h])).pm

    pm_lo = pm_for(0.5)
    pm_hi = pm_for(2.0)
    assert pm_lo is not None and pm_hi is not None
    assert pm_hi < pm_lo


def test_loop_stable_reference_is_reached():
    """预跑稳态后，参考点应该真的被跟上了（否则扫出来全是垃圾）。"""
    plant = _lc()
    comp = PICompensator(kp=0.5, ki=1200, fs=FS, u0=0.5, u_min=0, u_max=1)
    y = 0.0
    for _ in range(20000):
        y = plant(comp(0.5 - y))
    assert abs(y - 0.5) < 1e-5


# ---------------------------------------------------------------- sweep 契约
def test_sweep_progress_called():
    calls = []
    sweep(FS, _lc(), np.logspace(2, 3, 5), mode=MODE_PLANT,
          amplitude=0.02, cyc_mode=CYC_FAST,
          progress=lambda d, t: calls.append((d, t)))
    assert calls == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_sweep_returns_one_point_per_freq():
    freqs = np.logspace(2, 3, 7)
    pts = sweep(FS, _lc(), freqs, mode=MODE_PLANT, amplitude=0.02,
                cyc_mode=CYC_FAST)
    assert len(pts) == 7
