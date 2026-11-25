"""bode_data 相位约定的单元测试（04 文档 L0 gate）。

**这个文件最重要的测试是 test_phase_sign**：
它锁死 Z = S_c - j·S_s 这个约定。如果哪天有人把它改成 +j，
全项目相位反号，PM 会算出一个看着很漂亮但完全错误的数，
而且所有曲线肉眼看起来都还算"正常"——这是最难发现的一类 bug。
"""
import math

import numpy as np
import pytest

from app.model.bode_data import (MODE_LOOP, MODE_PLANT, RawPoint, h_from_raw,
                                 mag_db, phase_deg, z_from)


def synth(c_amp: float, phase_rad: float, n: int = 1000) -> tuple[float, float]:
    """按 01 文档 1.3 的关系造一对累加器：

        S_c = (N·A/2)·cos φ
        S_s = -(N·A/2)·sin φ
    """
    k = n * c_amp / 2.0
    return k * math.cos(phase_rad), -k * math.sin(phase_rad)


# ---------------------------------------------------------------- 符号
def test_phase_sign():
    """核心测试：正的相位必须算出正的相位角。

    libsfra 用 Z = S_c - j·S_s；把 S_s 定义成负的 sin，
    两次负号抵消，结果就是 e^{+jφ}。
    """
    for deg in (0, 30, 45, 90, 135, 179):
        c, s = synth(1.0, math.radians(deg))
        z = z_from(c, s)
        got = math.degrees(math.atan2(z.imag, z.real))
        assert abs(got - deg) < 1e-9, f"相位 {deg}° 算成了 {got:.3f}°"


def test_z_from_does_not_swap():
    """z_from 必须用 -s，不能是 +s。"""
    assert z_from(3.0, 4.0) == complex(3.0, -4.0)


def test_negative_phase():
    c, s = synth(1.0, math.radians(-90))
    z = z_from(c, s)
    assert abs(math.degrees(math.atan2(z.imag, z.real)) + 90) < 1e-9


# ---------------------------------------------------------------- 幅值
def test_mag_is_20log10():
    assert abs(mag_db(complex(10, 0)) - 20.0) < 1e-12
    assert abs(mag_db(complex(0.1, 0)) + 20.0) < 1e-12
    assert abs(mag_db(complex(1, 0))) < 1e-12


def test_mag_nan_on_zero():
    assert math.isnan(mag_db(complex(0, 0)))


# ---------------------------------------------------------------- H 计算
def test_h_plant():
    """PLANT：H = Y/X，同相位同幅值时 H = 1∠0°。"""
    cy, sy = synth(2.0, math.radians(30))
    cx, sx = synth(2.0, math.radians(30))
    p = RawPoint(1000.0, cy, sy, cx, sx)
    h = h_from_raw(p, MODE_PLANT)
    assert abs(abs(h) - 1.0) < 1e-9
    assert abs(phase_deg(h) - 0.0) < 1e-9


def test_h_loop():
    """LOOP：GH = Y/(Inj−Y)。

    构造 |GH|=2∠-60°：先定 Inj = 1∠0°，
    由 Y = GH·(Inj−Y) 得 Y = GH·Inj/(1+GH)。
    """
    gh = 2.0 * np.exp(-1j * math.radians(60))
    inj = complex(1.0, 0.0)
    y = gh * inj / (1 + gh)

    # 反推累加器（幅值随意，比值不变）
    n = 1000
    def pack(v: complex) -> tuple[float, float]:
        a = abs(v) * n / 2.0
        thr = np.angle(v)
        return a * math.cos(thr), -a * math.sin(thr)

    yc, ys = pack(y)
    ic, is_ = pack(inj)
    p = RawPoint(1000.0, yc, ys, ic, is_)

    h = h_from_raw(p, MODE_LOOP)
    assert abs(abs(h) - 2.0) < 1e-9
    assert abs(phase_deg(h) + 60.0) < 1e-9


def test_h_loop_den_zero():
    """Inj == Y（环路增益为 0 的极端）时应返回 NaN，不能崩。"""
    p = RawPoint(1000.0, 1.0, 2.0, 1.0, 2.0)
    h = h_from_raw(p, MODE_LOOP)
    assert math.isnan(h.real)


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        h_from_raw(RawPoint(1, 1, 1, 1, 1), 99)


# ---------------------------------------------------------------- 打包
def test_pack_unpack_roundtrip():
    p = RawPoint(1234.5, 1.25, -3.75, 0.5, 2.0)
    q = RawPoint.unpack(p.pack())
    assert q.f_act == pytest.approx(p.f_act)
    assert q.a_c == pytest.approx(p.a_c)
    assert q.b_s == pytest.approx(p.b_s)


def test_unpack_bad_length():
    with pytest.raises(ValueError):
        RawPoint.unpack(b"\x00" * 19)
