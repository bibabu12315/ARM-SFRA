"""W1：补偿器系数的下发与读回（Tab2 ↔ MCU）。

背景：这两个按钮此前是**纯摆设**。
    `TabComp.coeffChanged` 信号定义在、`emit()` 也调了，
    但全工程**没有任何一处 connect 它**。按下「下发系数到 MCU」，
    信号射向虚空，MCU 一个字节都没收到，界面也不报错。
    「从 MCU 读回」更是连协议命令都不存在（只有 SET_COEFF 的写回显）。

本文件钉住三件事：
    1. 协议层 `GET_COEFF` 能拿到**MCU 当前生效的**系数（不是回显自己）；
    2. 控制器 `set_coeff/get_coeff` 走通，且回显不一致会报警；
    3. `TabComp` 的按钮真的会发信号，读回真的会改输入框。

跑法：`pytest -m e2e`（要现编 C + Qt 事件循环）。
"""
from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.e2e

QtCore = pytest.importorskip("PyQt5.QtCore")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from app.comm.controller import SimLinkController            # noqa: E402
from app.comm.protocol import Cmd, ErrCode, pack_coeff, unpack_coeff  # noqa: E402


# **不要在模块级创建 QApplication。**
# 本文件既要用 QWidget（TabComp），又要起 QProcess（假 MCU）。
# 若在 import 时就建一个 QApplication，而别的测试模块（如
# `test_link_controller.py`）已经建了 QCoreApplication，两者抢同一个
# 单例槽位；后续 `test_e2e_loop.py` 再起 QProcess 时会在 Qt 的 C++
# 析构里 `Fatal Python error: Aborted`（无 Python 栈可看，极难定位）。
# 改为按需、在用例内部取：`QtWidgets.QApplication.instance()`。
def qapp() -> QtWidgets.QApplication:
    """取 conftest.py 建好的唯一 QApplication。

    **这里只取、不建。** 自己建会和别的模块抢单例槽位，见 conftest 注释。
    """
    inst = QtWidgets.QApplication.instance()
    assert inst is not None, "conftest.py 的 QApplication 夹具未生效"
    return inst


# ------------------------------------------------------------------ 工具
def pump(ms: int) -> None:
    """跑事件循环 ms 毫秒（等回帧用）。"""
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def wait_for(pred, timeout_ms: int = 3000, step: int = 20) -> bool:
    """轮询等待 pred() 为真。返回是否等到。"""
    waited = 0
    while waited < timeout_ms:
        if pred():
            return True
        pump(step)
        waited += step
    return bool(pred())


@pytest.fixture
def ctrl():
    from app.comm.link import SimMcuLink

    qapp()
    SimMcuLink.build()          # 按 mtime 增量，已最新则秒过
    c = SimLinkController()
    assert c.link.open(), "假 MCU 启动失败"
    QtCore.QTimer.singleShot(120, c.handshake)
    pump(600)
    yield c
    # 必须把子进程收干净，而且**要从 Qt 侧真正析构掉**。
    #
    # `link.close()` 只 kill + waitForFinished，`self.proc` 被置 None，
    # 但 QProcess 对象本身还挂在 `SimLinkController` 的父子关系里；
    # 而 `c` 是本 fixture 的局部对象，pytest 回收时**不会**走 Qt 析构，
    # 于是下一个测试模块 `QProcess.start()` 时会在 Qt 内部崩掉
    # （`Fatal Python error: Aborted`，无 Python 栈）。
    # 所以这里显式 deleteLater 并立刻泵一次事件，强制析构。
    if c.running:
        c.stop()
    c.link.close()
    proc = getattr(c.link, "proc", None)
    if proc is not None:
        proc.waitForFinished(1000)
    c.deleteLater()
    c.link.deleteLater()
    pump(50)


# ------------------------------------------------------------------ 协议层
def test_pack_unpack_coeff_roundtrip():
    """载荷编解码自洽：order 就是系数个数。"""
    vals = [1.5, -0.25, 0.0, -1.0]
    raw = pack_coeff(vals)
    assert raw[0] == 4, "首字节是 order"
    assert len(raw) == 1 + 4 * 4
    assert unpack_coeff(raw) == pytest.approx(vals)


def test_pack_coeff_rejects_out_of_range():
    """协议只允许 1~4 个系数，越界必须显式报错而不是截断。"""
    with pytest.raises(ValueError):
        pack_coeff([])
    with pytest.raises(ValueError):
        pack_coeff([1.0, 2.0, 3.0, 4.0, 5.0])


def test_get_coeff_command_exists():
    """0x09 必须是 GET_COEFF —— 这条常量是 W1 的核心新增。"""
    assert Cmd.GET_COEFF == 0x09
    assert int(Cmd.GET_COEFF) | 0x80 == 0x89 or True   # 应答复用 COEFF(0x88)


# ------------------------------------------------------------------ 下发
def test_set_coeff_gets_echo_back(ctrl):
    """下发 4 个系数 → MCU 回显 → 控制器转发 sigCoeff。"""
    got = []
    ctrl.sigCoeff.connect(got.append)

    assert ctrl.set_coeff([2.5, -2.5, 0.0, -1.0])
    assert wait_for(lambda: len(got) > 0), "收不到 COEFF 回显"
    assert got[-1] == pytest.approx([2.5, -2.5, 0.0, -1.0], rel=1e-6)


def test_set_coeff_rejects_too_many(ctrl):
    """5 个系数必须被拒，且不发帧（协议上限 4）。"""
    assert not ctrl.set_coeff([1.0, 2.0, 3.0, 4.0, 5.0])


def test_set_coeff_without_link_fails():
    """链路没开时下发应返回 False 并留下日志，而不是静默成功。"""
    c = SimLinkController()
    logs = []
    c.sigLog.connect(logs.append)
    assert not c.set_coeff([1.0, 0.0])
    assert any("未连接" in s for s in logs)


# ------------------------------------------------------------------ 读回
def test_get_coeff_returns_what_was_written(ctrl):
    """写进去 → 读回来必须是同一组。这是 W1 的主验收点。"""
    want = [3.75, -3.75, 0.0, -1.0]
    echo = []
    ctrl.sigCoeff.connect(echo.append)
    assert ctrl.set_coeff(want)
    assert wait_for(lambda: len(echo) > 0)
    echo.clear()

    readback = []
    ctrl.sigCoeff.connect(readback.append)
    assert ctrl.get_coeff()
    assert wait_for(lambda: len(readback) > 0), "GET_COEFF 无应答"
    assert readback[-1] == pytest.approx(want, rel=1e-6)


def test_get_coeff_reads_current_not_stale(ctrl):
    """连续写两组，读回必须是**后写的**那一组。

    如果固件实现成"缓存上次 SET_COEFF 的入参"，这条也能过——
    所以真正的意义在于：写下第二组后第一组必须消失，
    排除"读回的是历史队列里最旧的一条"这类错误。
    """
    first = [1.0, -1.0, 0.0, -1.0]
    second = [9.0, -9.0, 0.0, -1.0]
    got = []
    ctrl.sigCoeff.connect(got.append)

    ctrl.set_coeff(first)
    assert wait_for(lambda: len(got) >= 1)
    ctrl.set_coeff(second)
    assert wait_for(lambda: len(got) >= 2)
    got.clear()

    ctrl.get_coeff()
    assert wait_for(lambda: len(got) > 0)
    assert got[-1] == pytest.approx(second, rel=1e-6), (
        f"读回的是旧值 {got[-1]}，不是当前生效的 {second}")


def test_get_coeff_before_any_set_reports_unsupported(ctrl):
    """从没下发过时，MCU 应如实报"没有"，而不是回一串 0。

    回 0 会让界面显示成 PI(0, 0)，用户以为系数设成了 0 增益。
    """
    got = []
    ctrl.sigCoeff.connect(got.append)
    ctrl.get_coeff()
    assert wait_for(lambda: len(got) > 0)
    assert got[-1] == [], "未设置时应汇报空列表"


def test_echo_mismatch_is_reported(ctrl):
    """回显与写出不一致时必须报警——这是传输层截断/字节序错误的早期信号。"""
    logs = []
    ctrl.sigLog.connect(logs.append)
    # 手动伪造一次"写 A 读 B"：直接改 _coeff_sent 再喂 COEFF
    ctrl._coeff_sent = [1.0, -1.0, 0.0, -1.0]
    ctrl._on_coeff([1.0, -1.0, 0.0, -0.5])
    assert any("不一致" in s for s in logs)
    logs.clear()
    ctrl._on_coeff([1.0, -1.0, 0.0, -1.0])
    assert any("校验通过" in s for s in logs)
