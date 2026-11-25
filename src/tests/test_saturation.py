"""W2：饱和标志的显示与剔除。

背景：固件每点测量时会置 `STATUS.flags` 的 `DUTY_SAT`/`ADC_SAT`，
`protocol.py` 也早就解出来了，但**界面从头到尾没读过这个字段**。
后果：用户看到一条顶到水平线的曲线，分不清是"被测对象饱和"还是
"注入幅值太大"——这两者的处置完全相反（前者查对象，后者降幅值）。

本文件钉住三件事：
    1. `SweepResult` 能记住**逐点**饱和（DATA 帧里没有饱和位，
       必须由上位机在拉数时记录当时的 STATUS.flags）；
    2. 饱和点**不参与** PM/GM 计算（否则裕度会被限幅器算歪）；
    3. Tab1 的饱和条只在该出现时出现。

**不建 QWidget 的用例放这里**（可起子进程）；要 QWidget 的放
`test_tab_measure_sat.py`——原因见 `test_coeff_link.py` 顶部的说明。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.comm.protocol import Flags
from app.model.bode_data import MODE_PLANT, RawPoint, SweepResult


# ------------------------------------------------------------------ 工具
def mk_result(n: int = 10, sat_idx=()) -> SweepResult:
    """造一个 n 点的假结果，raw 每点相位/幅度都随频率单调下降。

    频率取 100 Hz 起、每点 ×1.5，保证 f_s/f 每周期采样数足够，
    不会因为 `invalid_mask()` 把点全剔掉而干扰断言。
    """
    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0)
    sat = set(sat_idx)
    f = 100.0
    for i in range(n):
        # 幅值固定、相位固定，只为让 mag/phase 有定义
        res.add(RawPoint(f, 1.0, 0.5, 2.0, 1.0), saturated=(i in sat))
        f *= 1.5
    return res


# ------------------------------------------------------------------ 模型层
def test_saturated_list_stays_in_sync_with_raw():
    """`saturated` 必须与 `raw` 等长，否则掩码会错位。"""
    res = mk_result(6, sat_idx={1, 4})
    assert len(res.saturated) == len(res.raw) == 6
    assert res.sat_mask().tolist() == [False, True, False, False, True, False]


def test_sat_count_counts_only_true():
    res = mk_result(8, sat_idx={0, 2, 7})
    assert res.sat_count == 3


def test_empty_saturated_means_no_saturation():
    """老数据（没有 `saturated` 字段）必须当作"没饱和"，而不是"全饱和"。

    从 v4.0 导入的 JSON 里没有这个字段；若当成全饱和，
    历史数据会突然一个点都算不出裕度。
    """
    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0,
                      raw=[RawPoint(1000.0, 1.0, 0.0, 2.0, 0.0),
                           RawPoint(2000.0, 1.0, 0.0, 2.0, 0.0)])
    assert res.saturated == []
    assert res.sat_count == 0
    assert res.sat_mask().tolist() == [False, False]


def test_sat_mask_survives_length_mismatch():
    """`saturated` 比 `raw` 短时不能抛异常，只标能对齐的部分。"""
    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0,
                      raw=[RawPoint(1000.0, 1, 0, 2, 0),
                           RawPoint(2000.0, 1, 0, 2, 0),
                           RawPoint(4000.0, 1, 0, 2, 0)],
                      saturated=[True])
    m = res.sat_mask()
    assert m.tolist() == [True, False, False]


def test_reliable_mask_excludes_saturated():
    """`reliable_mask()` 必须同时排除采样不足和饱和。"""
    res = mk_result(6, sat_idx={1, 3})
    m = res.reliable_mask()
    assert m.tolist() == [True, False, True, False, True, True]


def test_reliable_mask_excludes_low_sample_rate_points():
    """每周期采样 < 2 的点本来就不可用，也要被排除。"""
    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0)
    # 10 Hz 有 10000 点/周期（好）；60 kHz 只有 1.67 点/周期（差）
    res.add(RawPoint(10.0, 1.0, 0.0, 2.0, 0.0))
    res.add(RawPoint(60_000.0, 1.0, 0.0, 2.0, 0.0))
    m = res.reliable_mask()
    assert bool(m[0]) is True
    assert bool(m[1]) is False


def test_truncate_keeps_lists_aligned():
    """`truncate()` 必须同时裁 `raw` 和 `saturated`。"""
    res = mk_result(6, sat_idx={0, 5})
    res.truncate(3)
    assert len(res.raw) == len(res.saturated) == 3
    assert res.sat_count == 1


def test_add_many_marks_all_with_same_flag():
    """`add_many` 把一批点打同一个饱和标记（对应一次 STATUS 轮询）。"""
    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0)
    pts = [RawPoint(1000.0, 1, 0, 2, 0), RawPoint(2000.0, 1, 0, 2, 0)]
    res.add_many(pts, saturated=True)
    assert res.sat_count == 2
    assert len(res.saturated) == 2


# ------------------------------------------------------------------ 标志位
def test_saturation_flags_detection():
    """饱和判定必须同时认 DUTY_SAT 和 ADC_SAT 两位。"""
    def is_sat(flags: int) -> bool:
        return bool(flags & (Flags.DUTY_SAT | Flags.ADC_SAT))

    assert is_sat(Flags.DUTY_SAT) is True
    assert is_sat(Flags.ADC_SAT) is True
    assert is_sat(Flags.DUTY_SAT | Flags.ADC_SAT) is True
    # 注入关闭 / done 不是饱和
    assert is_sat(Flags.INJ_OFF) is False
    assert is_sat(Flags.DONE) is False
    assert is_sat(0) is False


# ------------------------------------------------------------------ 端到端
pytest.importorskip("PyQt5.QtCore")


@pytest.mark.e2e
def test_controller_records_saturation_per_point():
    """真链路后端必须把 STATUS.flags 记进 result。

    直接伪造一段 DATA 来验：把 `_flags` 置成饱和，再喂一帧数据，
    该帧的点必须被标成饱和。
    """
    QtCore = pytest.importorskip("PyQt5.QtCore")
    from app.comm.controller import SimLinkController
    from app.comm.protocol import DataChunk
    from app.model.sweep_config import SweepConfig

    c = SimLinkController()
    cfg = SweepConfig(isr_freq=100_000.0, f_start=200.0, f_stop=2000.0,
                      ppd=10, mode=MODE_PLANT, amp_pu=0.01)
    c._cfg = cfg
    c._n_total = cfg.n_points
    c.result = c._new_result(cfg)
    c._next_idx = 0
    c._flags = int(Flags.DUTY_SAT)      # 模拟 STATUS 刚报过饱和

    pts = ((1000.0, 1.0, 0.0, 2.0, 0.0), (1200.0, 1.0, 0.0, 2.0, 0.0))
    c._on_data(DataChunk(start_idx=0, points=pts))
    assert c.result.sat_count == 2, "STATUS 饱和位没有传到点上"
    c.link.close()
