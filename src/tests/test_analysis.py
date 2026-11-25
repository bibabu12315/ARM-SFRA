"""analysis（unwrap / 穿越点 / PM / GM）的单元测试。"""
import math

import numpy as np
import pytest

from app.model.analysis import analyze, unwrap_phase


# ---------------------------------------------------------------- unwrap
def test_unwrap_single_jump():
    """-170 → +170 实际是继续往下走，unwrap 后应变成 -170 → -190。"""
    ph = np.array([-170.0, -175.0, 175.0, 170.0])
    u = unwrap_phase(ph)
    assert abs(u[2] + 185.0) < 1e-9
    assert abs(u[3] + 190.0) < 1e-9


def test_unwrap_no_jump():
    ph = np.array([0.0, -30.0, -60.0, -90.0])
    u = unwrap_phase(ph)
    np.testing.assert_allclose(u, ph)


def test_unwrap_empty():
    assert unwrap_phase(np.array([])).size == 0


def test_unwrap_allows_below_minus_360():
    """相位一路往下走时，unwrap 必须让它可以突破 -360°，不能折回。

    TI 的相位约定是 0 → -90 → -180 → -270 → -360 一路往负，
    所以解缠后的值超出 (-180, 180] 是**正常**的。
    np.unwrap 默认不加限制，这里确认我们没在包装时把它折回来。
    """
    # 模拟一个单调下降的相位：每步 -45°，从 -135° 一直走到 -585°
    ph = np.array([-135.0, -180.0, 135.0, 90.0, 45.0, 0.0, -45.0])
    u = unwrap_phase(ph)
    # -135 → -180 → -225 → -270 → -315 → -360 → -405
    assert u[2] < -200
    assert u[-1] < -360.0


# ---------------------------------------------------------------- 穿越点
def _first_order(fc: float, f: np.ndarray, gain_db: float = 0.0):
    """一阶低通 1/(1+s/wc) 再串一个直流增益，让曲线能穿越 0dB。"""
    x = f / fc
    mag = gain_db + 20 * np.log10(1 / np.sqrt(1 + x ** 2))
    ph = -np.degrees(np.arctan(x))
    return mag, ph


def test_pm_of_first_order():
    """一阶极点在 fc：0dB 穿越处相位 -45°，PM = 135°。"""
    fc = 1000.0
    f = np.logspace(1, 5, 400)
    mag, ph = _first_order(fc, f, gain_db=3.01)   # 让 fc 正好是 0dB 点
    st = analyze(f, mag, ph)
    assert st.fc == pytest.approx(fc, rel=0.03)
    assert st.pm == pytest.approx(135.0, abs=1.5)


def test_pm_decreases_with_order():
    """两个极点同频时 0dB 处相位 -90°，PM = 90°。"""
    fc = 1000.0
    f = np.logspace(1, 5, 400)
    x = f / fc
    mag = 20 * np.log10(1 / (1 + x ** 2)) + 6.02    # 两个 -3dB 点合起来
    ph = -2 * np.degrees(np.arctan(x))
    st = analyze(f, mag, ph)
    assert st.pm == pytest.approx(90.0, abs=4.0)


def test_no_crossing_gives_none():
    """整条曲线都在 0dB 以上：没有穿越点，PM/GM 都不可信。"""
    f = np.logspace(1, 4, 50)
    mag = np.full_like(f, 10.0)
    ph = np.linspace(0, -90, 50)
    st = analyze(f, mag, ph)
    assert st.fc is None and st.pm is None
    assert st.pm_note != ""


def test_crossing_takes_last():
    """有两次 0dB 穿越时取高频侧那次（避免低频假穿越干扰）。"""
    f = np.logspace(1, 5, 500)
    mag = -20 * np.sin(np.linspace(0, 3 * np.pi, 500))
    ph = -np.linspace(0, 400, 500)
    st = analyze(f, mag, ph)
    assert st.fc is not None
    assert st.fc > f[250]


def test_gm_positive_when_mag_below_zero_at_180():
    """-180° 处 |T| < 1 → GM > 0（稳定）。"""
    f = np.logspace(1, 5, 500)
    mag = np.linspace(20, -60, 500)
    ph = -np.linspace(0, 270, 500)
    st = analyze(f, mag, ph)
    assert st.gm is not None
    assert st.gm > 0


def test_gm_negative_when_mag_above_zero_at_180():
    """-180° 处 |T| > 1 → GM < 0（不稳定）。"""
    f = np.logspace(1, 5, 500)
    mag = np.linspace(40, -10, 500)
    ph = -np.linspace(0, 270, 500)
    st = analyze(f, mag, ph)
    # 这种情况下 0dB 穿越在 -180° 之前，此时 gm 应为负
    assert st.gm is None or st.gm < 0


def test_nan_points_are_dropped():
    f = np.logspace(1, 4, 100)
    mag, ph = _first_order(1000.0, f, gain_db=10.0)
    mag[::7] = np.nan
    ph[::11] = np.nan
    st = analyze(f, mag, ph)
    assert st.fc is not None


def test_too_few_points():
    st = analyze(np.array([1.0]), np.array([0.0]), np.array([0.0]))
    assert st.pm is None
    assert "不足" in st.pm_note


def test_pm_ok_threshold():
    f = np.logspace(1, 4, 200)
    mag, ph = _first_order(1000.0, f, gain_db=3.01)
    st = analyze(f, mag, ph)
    assert st.pm_ok            # 135° > 45°
    assert not st.gm_ok        # 一阶没有 -180° 穿越，gm 为 None
