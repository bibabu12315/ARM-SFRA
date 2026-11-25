"""S6a 工具链自检：验证 `tools/selftest_s6a.py` 的判据逻辑本身是对的。

为什么需要这个文件
------------------
`tools/selftest_s6a.py` 是用来判真板 PASS/FAIL 的。**判据本身错了，
它就会对坏固件报 PASS、对好固件报 FAIL**——比没有工具更糟。

这个文件喂它四种**已知答案**的数据，锁死它必须给出的结论：

    1. 完美的 Tustin 响应      → PASS
    2. 相位符号全反            → FAIL（必须报 any_positive）
    3. 幅值整体偏 +2 dB        → FAIL（必须报 max_tus_mag ≈ 2）
    4. 相位非单调（上翘）      → FAIL（必须报 monotonic=False）

另外锁住一条**物理事实**：Tustin 离散化在 f_s/5 处比连续域低约 1.26 dB。
如果有人误把判据改成"全程跟连续域比"，测试 1 会红——这正是不该改的。

这些用例不需要 Qt、不需要 C 编译器、不需要串口，跑得快。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from selftest_s6a import Theory, build_report  # noqa: E402

FS = 100_000.0
FC = 200.0
FREQS = [200.0, 1000.0, 5000.0, 20000.0]


def _tustin_data(th: Theory, freqs=FREQS):
    mags = [th.tustin_mag_db(f, FS) for f in freqs]
    phas = [th.tustin_phase_deg(f, FS) for f in freqs]
    return mags, phas


def _continuous_data(th: Theory, freqs=FREQS):
    mags = [th.mag_db(f) for f in freqs]
    phas = [th.phase_deg(f) for f in freqs]
    return mags, phas


# ---------------------------------------------------------------- 正向
def test_perfect_tustin_response_passes():
    """完美的离散域响应必须通过——否则工具会把好固件判死。"""
    th = Theory(FC)
    mags, phas = _tustin_data(th)
    _, s = build_report(FREQS, mags, phas, th, FS)
    assert s["passed"], f"完美数据被误判 FAIL: {s}"
    assert s["max_tus_mag"] < 1e-6
    assert s["max_tus_pha"] < 1e-6


def test_fc_point_is_minus3db_minus45():
    """fc 处必须是 −3 dB / −45°，两个基准都成立（翘曲在此可忽略）。"""
    th = Theory(FC)
    assert abs(th.mag_db(FC) + 3.0103) < 1e-3
    assert abs(th.phase_deg(FC) + 45.0) < 1e-6
    assert abs(th.tustin_mag_db(FC, FS) - th.mag_db(FC)) < 0.01


# ---------------------------------------------------------------- 反向
def test_reversed_phase_is_caught():
    """相位符号反了（+90° 而不是 −90°）必须被抓。

    这是最危险的静默错误：曲线看起来"很稳"（PM 很大），实际已经反了。
    """
    th = Theory(FC)
    mags, phas = _tustin_data(th)
    _, s = build_report(FREQS, mags, [-p for p in phas], th, FS)
    assert not s["passed"]
    assert s["any_positive"], "反相位没被 any_positive 抓到"
    assert s["max_tus_pha"] > 100.0


def test_gain_offset_is_caught():
    """幅值整体偏 +2 dB 必须被抓（pu 归一化不一致的典型症状，D15）。"""
    th = Theory(FC)
    mags, phas = _tustin_data(th)
    _, s = build_report(FREQS, [m + 2.0 for m in mags], phas, th, FS)
    assert not s["passed"]
    assert abs(s["max_tus_mag"] - 2.0) < 0.05


def test_non_monotonic_phase_is_caught():
    """相位中途上翘（unwrap 坏了 / 混叠）必须被抓。"""
    th = Theory(FC)
    f = [100.0, 200.0, 500.0, 1000.0, 2000.0]
    mags = [th.tustin_mag_db(x, FS) for x in f]
    phas = [0.0, -10.0, -30.0, -20.0, -50.0]     # 第 4 点回翘
    _, s = build_report(f, mags, phas, th, FS)
    assert not s["passed"]
    assert not s["monotonic"]


def test_high_freq_slope_violation_is_caught():
    """高频斜率不是 −20 dB/dec（比如 −10 或 −40）必须被抓。"""
    th = Theory(FC)
    f = [1000.0, 2000.0, 4000.0, 8000.0]
    # 人为造一条 −10 dB/dec 的曲线
    mags = [-20.0 - 10.0 * math.log10(x / 1000.0) for x in f]
    phas = [th.tustin_phase_deg(x, FS) for x in f]
    _, s = build_report(f, mags, phas, th, FS)
    assert not s["slope_ok"], f"斜率应判失败，实际 {s['slope']:.2f}"


# ---------------------------------------------------------------- 物理事实
def test_tustin_warp_at_nyquist_is_about_1p26db():
    """Tustin 在 f_s/5 处比连续域低约 1.26 dB——这是数学，不是 bug。

    锁住这个数有两个作用：
      1. 提醒后来者别把判据改成"全程跟连续域比"（会让好固件判 FAIL）；
      2. 若有人改了固件的一阶低通离散化方式，这里会立刻报警。
    """
    th = Theory(FC)
    warp = th.warp_db(FS / 5.0, FS)
    assert -1.40 < warp < -1.10, f"f_s/5 处翘曲 {warp:.3f} dB 与预期不符"


def test_tustin_warp_is_negligible_below_10fc():
    """f ≤ 10·fc 时翘曲 < 0.02 dB，所以连续域判据只在此段生效。"""
    th = Theory(FC)
    warp = abs(th.warp_db(10.0 * FC, FS))
    assert warp < 0.02, f"10·fc 处翘曲 {warp:.4f} dB 偏大"


def test_tustin_dc_gain_is_unity():
    """离散模型的直流增益必须是 1——分母是 1+a1·z⁻¹，不是 1−a1·z⁻¹。

    写错符号会把直流增益算成 1/159，所有判据跟着全错。
    """
    th = Theory(FC)
    h0 = th._tustin_h(0.0, FS)
    assert abs(h0.real - 1.0) < 1e-9
    assert abs(h0.imag) < 1e-12


def test_continuous_check_skipped_when_asked():
    """`check_tustin=False` 时，喂连续域数据不该因 A 段失败。

    这是给"校准判据自身"场景用的：数据本身就是连续域模型算的，
    拿它跟 Tustin 比必然 FAIL，且失败没有信息量。
    """
    th = Theory(FC)
    mags, phas = _continuous_data(th)
    _, s = build_report(FREQS, mags, phas, th, FS, check_tustin=False)
    assert s["passed"], f"连续域数据在 A 段被跳过时仍应通过: {s}"
    assert not s["check_tustin"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
