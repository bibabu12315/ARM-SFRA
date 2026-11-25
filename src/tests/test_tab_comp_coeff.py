"""W1（界面半边）：TabComp 的「下发系数 / 从 MCU 读回」两个按钮。

**为什么单独一个文件，而不是并进 `test_coeff_link.py`**：
    这个文件会 `import TabComp` 并实例化 QWidget。
    实测发现：一旦本进程里创建过 `TabComp`（QWidget），同进程里
    后续再 `QProcess.start()`（假 MCU）会在 Qt 的 C++ 内部
    `Fatal Python error: Aborted` —— 无 Python 栈，极难定位。
    反过来（先起 QProcess 再建 QWidget）则正常。
    所以把"要 QWidget"的用例和"要 QProcess"的用例分到两个文件，
    pytest 按文件名顺序跑时就不会踩到那个顺序陷阱。
    （`test_coeff_link.py` 里只有纯协议/控制器用例，不建任何 QWidget。）

跑法：不需要 `-m e2e`（不起子进程），默认测试集就会跑。
"""
from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from app.ui.tab_comp import TabComp     # noqa: E402


#: 本文件不依赖假 MCU，但 TabComp 要 QWidget，必须有 QApplication。
#: conftest.py 已建好会话级 QApplication，这里显式取一下做断言。
def qapp() -> QtWidgets.QApplication:
    inst = QtWidgets.QApplication.instance()
    assert inst is not None, "conftest.py 的 QApplication 夹具未生效"
    return inst


# ------------------------------------------------------------------ 按钮接线
def test_tabcomp_buttons_emit_signals():
    """按钮按下必须发信号。

    这是 W1 的核心：此前 `coeffChanged` 定义在、`emit()` 也调了，
    但**全工程没有任何 connect**；而两个按钮本身连 clicked 都没接，
    所以"按下去毫无反应"这件事在代码里根本看不出来。
    """
    qapp()
    tab = TabComp()
    applied, reads = [], []
    tab.coeffChanged.connect(applied.append)
    # readbackRequested 无参，不能直接接 list.append
    # （会报 "append() takes exactly one argument"），必须包一层 lambda。
    tab.readbackRequested.connect(lambda: reads.append(True))

    tab.btn_apply.setEnabled(True)
    tab.btn_read.setEnabled(True)
    tab.btn_apply.click()
    tab.btn_read.click()

    assert applied, "「下发系数」没有发 coeffChanged"
    assert len(applied[0]) == 5, "应带 b0,b1,b2,a1,a2 共 5 个"
    assert len(reads) == 1, "「从 MCU 读回」没有发 readbackRequested"


# ------------------------------------------------------------------ 系数往返
def test_tabcomp_coefficients_reflect_current_inputs():
    """`coefficients()` 必须现场重算，不能返回缓存。

    `NumEdit.set_value()` 默认 `emit=False`，程序化改 Kp/Ki 不会触发
    refresh。若 `coefficients()` 直接返回 `self._coeffs`，就会把
    **上一次**的系数下发出去，而界面显示的是新值——两边都不报错，
    只是控制律悄悄变了。
    """
    qapp()
    tab = TabComp()
    tab.in_fs.set_value(50.0)
    tab.in_ug.set_value(1.0)
    tab.in_kp.set_value(0.5)
    tab.in_ki.set_value(1200.0)
    tab.seg_type.set_current(0)

    got = tab.coefficients()
    ts = 1.0 / 50_000.0
    assert got[0] == pytest.approx(0.5 + 1200.0 * ts, rel=1e-6)   # b0
    assert got[1] == pytest.approx(-0.5, rel=1e-6)                # b1
    assert got[3] == pytest.approx(-1.0, rel=1e-6)                # a1


def test_tabcomp_coefficients_include_ui_gain():
    """执行量增益必须乘进 b 系数。

    下发到 MCU 的 b/a 是**真正要执行的**差分方程，MCU 不会再乘一次 ug。
    少了 ug 就与实际控制律差一个倍数。
    """
    qapp()
    tab = TabComp()
    tab.in_fs.set_value(50.0)
    tab.in_kp.set_value(0.8)
    tab.in_ki.set_value(300.0)
    tab.in_ug.set_value(1.0)
    tab.seg_type.set_current(0)
    unit = tab.coefficients()

    tab.in_ug.set_value(2.5)
    scaled = tab.coefficients()
    assert scaled[0] == pytest.approx(unit[0] * 2.5, rel=1e-6)
    assert scaled[1] == pytest.approx(unit[1] * 2.5, rel=1e-6)
    assert scaled[3] == pytest.approx(-1.0, rel=1e-6), "a1 不含 ug"


def test_tabcomp_set_coefficients_restores_kp_ki():
    """读回的系数填回界面后，再由界面正推回去必须还原。"""
    qapp()
    tab = TabComp()
    tab.in_fs.set_value(100.0)
    tab.in_ug.set_value(1.0)
    tab.in_kp.set_value(0.5)
    tab.in_ki.set_value(1200.0)
    tab.in_kd.set_value(0.0)
    tab.seg_type.set_current(0)
    sent = tab.coefficients()

    assert tab.set_coefficients(sent)
    assert tab.in_kp.value() == pytest.approx(0.5, rel=1e-6)
    assert tab.in_ki.value() == pytest.approx(1200.0, rel=1e-6)
    assert tab.coefficients() == pytest.approx(sent, rel=1e-6)


def test_tabcomp_readback_respects_ui_gain():
    """带执行量增益时也要还原对——ug 是整体乘上去的。"""
    qapp()
    tab = TabComp()
    tab.in_fs.set_value(50.0)
    tab.in_ug.set_value(2.5)
    tab.in_kp.set_value(0.8)
    tab.in_ki.set_value(300.0)
    tab.seg_type.set_current(0)
    sent = tab.coefficients()

    assert tab.set_coefficients(sent)
    assert tab.in_kp.value() == pytest.approx(0.8, rel=1e-6)
    assert tab.in_ki.value() == pytest.approx(300.0, rel=1e-6)


def test_tabcomp_fs_reads_from_input_not_stale_mirror():
    """控制频率必须读输入框，不能读 `set_context` 留下的 `_fs`。

    否则用户在「控制频率」框里手改数字，PI 离散化仍用旧值，
    界面显示与实际下发不一致。
    """
    qapp()
    tab = TabComp()
    tab.in_kp.set_value(1.0)
    tab.in_ki.set_value(1000.0)
    tab.in_ug.set_value(1.0)
    tab.seg_type.set_current(0)

    tab.in_fs.set_value(100.0)
    c100 = tab.coefficients()
    tab.in_fs.set_value(50.0)
    c50 = tab.coefficients()

    # Ki·Ts 项：频率减半 → Ts 翻倍 → b0 增大
    assert c50[0] > c100[0], "改频率没有影响系数，说明还在用旧 _fs"


def test_tabcomp_set_coefficients_rejects_garbage():
    """不足 2 个系数无法解析，必须返回 False 而不是填一堆 0。"""
    qapp()
    tab = TabComp()
    assert not tab.set_coefficients([])
    assert not tab.set_coefficients([1.0])
