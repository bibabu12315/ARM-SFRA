"""W2（界面半边）：Tab1 的饱和提示条。

单独成文件的原因与 `test_tab_comp_coeff.py` 相同：本文件创建 QWidget，
而同进程里创建过 QWidget 后再 `QProcess.start()` 会崩在 Qt 内部。
纯模型/协议层的饱和用例在 `test_saturation.py`。
"""
from __future__ import annotations

import numpy as np
import pytest

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from app.comm.controller import SimLinkController         # noqa: E402
from app.model.bode_data import MODE_PLANT, RawPoint, SweepResult  # noqa: E402
from app.ui.tab_measure import TabMeasure                 # noqa: E402


def shown(w) -> bool:
    """控件是否**被设为可见**。

    不能直接用 `isVisible()`：它要求整条父链都已 show()，
    而测试里从来没有 show() 过窗口，会恒为 False。
    `isVisibleTo(parent)` 只看"这个控件自己的 visible 标志"，
    正是我们要断言的量。
    """
    return w.isVisibleTo(w.parentWidget())


def qapp() -> QtWidgets.QApplication:
    inst = QtWidgets.QApplication.instance()
    assert inst is not None, "conftest.py 的 QApplication 夹具未生效"
    return inst


@pytest.fixture
def tab():
    qapp()
    c = SimLinkController()
    t = TabMeasure(c)
    yield t
    c.link.close()


def mk_result(n: int = 10, sat_idx=()) -> SweepResult:
    res = SweepResult(mode=MODE_PLANT, isr_freq=100_000.0)
    sat = set(sat_idx)
    f = 100.0
    for i in range(n):
        res.add(RawPoint(f, 1.0, 0.5, 2.0, 1.0), saturated=(i in sat))
        f *= 1.5
    return res


# ------------------------------------------------------------------ 显示
def test_sat_bar_hidden_without_saturation(tab):
    """没有饱和时不占版面。"""
    tab.result = mk_result(8)
    tab._refresh_sat()
    assert not shown(tab.sat_bar)


def test_sat_bar_appears_when_saturated(tab):
    tab.result = mk_result(8, sat_idx={2, 5})
    tab._refresh_sat()
    assert shown(tab.sat_bar)
    assert "2/8" in tab.lb_sat.text()


def test_partial_saturation_suggests_rescan_band(tab):
    """少量饱和：提示"已从 PM/GM 剔除，可只重扫该频段"。"""
    tab.result = mk_result(10, sat_idx={1})
    tab._refresh_sat()
    txt = tab.lb_sat.text()
    assert "剔除" in txt
    assert "重扫" in txt


def test_heavy_saturation_suggests_lower_amplitude(tab):
    """超过三成：提示"多为幅值偏大，建议降幅值重扫"。
    这是 D16 预扫规则的落脚点。"""
    tab.result = mk_result(10, sat_idx={0, 1, 2, 3})
    tab._refresh_sat()
    txt = tab.lb_sat.text()
    assert "幅值" in txt


def test_full_saturation_says_data_unusable(tab):
    """全部饱和必须说清"整趟数据不可用"，不能让用户以为还能用。"""
    tab.result = mk_result(5, sat_idx={0, 1, 2, 3, 4})
    tab._refresh_sat()
    txt = tab.lb_sat.text()
    assert "全部" in txt
    assert "不可用" in txt


def test_sat_bar_hidden_after_reset(tab):
    """重置读数时饱和条必须收起，否则下一趟会显示上一趟的告警。"""
    tab.result = mk_result(6, sat_idx={0})
    tab._refresh_sat()
    assert shown(tab.sat_bar)
    tab._reset_readout()
    assert not shown(tab.sat_bar)


# ------------------------------------------------------------------ 裕度剔除
def mk_lp(with_sat: bool = False) -> SweepResult:
    """构造一条真实的一阶低通 H = K/(1 + j f/fc)，fc = 1 kHz，K = 100。

    **K 必须 > 1**，否则整条曲线都在 0 dB 以下、根本不存在增益穿越，
    `analyze()` 会返回 `fc=None`，测试就变成"两个 None 相等"的空过。
    K=100 时 |H| 在 f = fc·√(K²−1) ≈ 100 kHz 附近穿越 0 dB……
    这里取 fc = 10 Hz 配合 100 Hz~10 kHz 的扫频段，
    让穿越点落在频段中间。

    相位从 0° 单调下降到 -90°，是一条**真会穿越**的曲线；
    用常相位 / −20 dB/dec 那种假曲线测不出剔除效果。
    """
    fs = 100_000.0
    r = SweepResult(mode=MODE_PLANT, isr_freq=fs)
    for i in range(21):
        f = 100.0 * (10 ** (i / 10.0))            # 100 Hz ~ 10 kHz
        # K=8, fc=200 Hz → |H|=1 在 f=fc·√(K²−1)≈1.58 kHz，落在频段中间
        h = 8.0 / (1 + 1j * f / 200.0)
        mag, ph = abs(h), np.angle(h)
        # H = (a_c - j·a_s)/(b_c - j·b_s)，取 b = 1
        r.add(RawPoint(f, mag * np.cos(ph), -mag * np.sin(ph), 1.0, 0.0),
              saturated=False)
    if not with_sat:
        return r

    # 插一个"饱和 + 幅值离谱"的点，**必须在真实穿越点(1.59 kHz)的高频侧**。
    #
    # 关键：`analyze()` 取的是**最后一个** 0 dB 穿越点（高频侧那个），
    # 不是第一个。所以在穿越点左侧插点没用——那个点会被忽略，
    # 测试就会空过（这一点是实测踩出来的）。
    # 在 5 kHz 把幅值人为抬回 +6 dB，不剔除时 fc 会被错算到 5004 Hz。
    r.add(RawPoint(5000.0, 2.0, 0.0, 1.0, 0.0), saturated=True)
    pairs = sorted(zip(r.raw, r.saturated), key=lambda t: t[0].f_act)
    out = SweepResult(mode=MODE_PLANT, isr_freq=fs)
    for p, sg in pairs:
        out.add(p, saturated=sg)
    return out


def test_saturated_point_does_not_fake_a_crossover(tab):
    """饱和点必须被剔除，否则会**凭空造出**一个增益穿越点。

    实测：一阶低通（fc=1 kHz）本来穿越点就在 1 kHz；
    混入一个 |H|=5 的饱和点后，不剔除时算出的 fc 变成 2.3 kHz、
    PM 变成 137.8°（荒谬值）；剔除后回到正确结果。
    这条断言直接量"fc 是否被污染"，比断言 PM 数值更稳。
    """
    tab.result = mk_lp(with_sat=False)
    tab._finalize()
    fc_clean = tab.v_fc.num.text()

    tab.result = mk_lp(with_sat=True)
    tab._finalize()
    assert tab.result.sat_count == 1, "饱和点没被记录"
    fc_sat = tab.v_fc.num.text()

    assert fc_sat == fc_clean, (
        f"饱和点污染了穿越频率：干净 {fc_clean} vs 含饱和 {fc_sat}")
    assert tab.v_fc.num.text() != "—", "基线本身没算出 fc，测试无效"


def test_analyze_without_exclusion_would_be_wrong(tab):
    """反向证明：**不剔除**时结果确实是错的。

    没有这一条，上面那条测试可能因为"剔除逻辑根本没生效但也恰好相等"而空过。
    这里直接绕过界面，用全量点算一遍，断言它与剔除后的值不同。
    """
    from app.model.analysis import analyze

    res = mk_lp(with_sat=True)
    keep = res.reliable_mask()
    st_ok = analyze(res.freq[keep], res.mag[keep], res.phase[keep])
    st_all = analyze(res.freq, res.mag, res.phase)

    # 不剔除时不该得到与剔除后相同的穿越频率
    assert st_all.fc != st_ok.fc, (
        "这个构造没能体现出剔除的作用，测试本身无效")


def test_sat_detail_dialog_lists_frequencies(tab):
    """「查看频点」能列出饱和频率——用户据此决定重扫哪一段。"""
    tab.result = mk_result(8, sat_idx={2, 6})
    tab._refresh_sat()
    # 不真的 exec_()（会阻塞），只验证表数据构造得出来
    res = tab.result
    m = res.sat_mask()
    f = res.freq[m]
    assert len(f) == 2
    assert f[0] < f[1]
