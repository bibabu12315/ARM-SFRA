"""顶栏守门测试：Tab 必须居中 + 顶栏文字必须完整。

## 为什么需要这个文件

v4.7 把「测量 / 补偿器」分页从独立一行（46px）合进了顶栏，
省下 40px。两个承诺需要守住：

  1. **Tab 的几何中线落在窗口中线**，且与左右内容宽度无关
  2. **顶栏所有控件文字完整**（含动态变化的 pill 文案）

第 1 条尤其容易退化：一旦有人把 `QGridLayout` 换成
`QHBoxLayout + addStretch`，在 1360px 下可能只偏 3~5px（肉眼看不出），
但拉到 2560px 就偏成几十像素。这种缺陷不会报错、不会崩，只会
"看着有点歪"，所以必须自动化。

## 判据

    居中偏差 = |Tab 中心 x − 顶栏中线 x|，要求 ≤ 2px

2px 的来历：Qt 布局是整数像素，而中线 `width/2` 可能是 .5，
取整误差最多 1px；留 1px 给 DPI 缩放（125% 时控件尺寸会被 round）。

## 不 show() 窗口能测吗

**不能**。`QGridLayout` 的列宽分配发生在 `show()` 之后的首次布局，
不 show 时 `geometry()` 全是默认值（实测 seg_tab 宽 640px）。
这与 `test_ui_text_fit.py` 的做法不同——那边测的是控件内部尺寸，
不依赖顶层布局；这边测的正是顶层布局本身。

代价是要 `show()`，会触发 Windows 的 `0x8001010d` 噪音一次。
本文件用 `setAttribute(Qt.WA_DontShowOnScreen)` 规避：窗口被"显示"
（走完整布局流程）但不真的绘制到屏幕。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5 import QtCore, QtGui, QtWidgets                      # noqa: E402

from app import theme                                              # noqa: E402
from app.main_window import MainWindow, TITLEBAR_H                 # noqa: E402

pytestmark = pytest.mark.e2e


#: 居中偏差上限（px）。见文件头"判据"一节。
CENTER_TOL = 2


def _settle(app, n: int = 12) -> None:
    for _ in range(n):
        app.processEvents()


def _need_w(wdg) -> int:
    """控件文字需要的宽度。

    **只对可勾选控件**取常规/粗体较大值：只有
    `QPushButton#SegItem:checked { font-weight:600 }` 会加粗。
    `#Small` 按钮和 `#Pill` 任何状态都不加粗，用粗体量会虚高 10%，
    把"够用"误报成"截断"（第一版就误报过 `btn_conn`）。
    """
    txt = wdg.text() if hasattr(wdg, "text") else ""
    f = wdg.font()
    plain = QtGui.QFontMetrics(f).horizontalAdvance(txt)
    if getattr(wdg, "isCheckable", lambda: False)():
        fb = QtGui.QFont(f)
        fb.setWeight(QtGui.QFont.DemiBold)
        return max(plain, QtGui.QFontMetrics(fb).horizontalAdvance(txt))
    return plain


def _have_w(wdg) -> int:
    """控件内部能画字的宽度（QStyle 独立计算，与生产代码零耦合）。"""
    if isinstance(wdg, (QtWidgets.QPushButton, QtWidgets.QToolButton)):
        opt = QtWidgets.QStyleOptionButton()
        wdg.initStyleOption(opt)
        return wdg.style().subElementRect(
            QtWidgets.QStyle.SE_PushButtonContents, opt, wdg).width()
    # QLabel：contentsRect 已扣掉 QSS padding（pill 无 border）
    return wdg.contentsRect().width()


@pytest.fixture
def make_win():
    created = []

    def _make(w: int = 1360, h: int = 860):
        app = QtWidgets.QApplication.instance()
        app.setStyleSheet(theme.qss())
        win = MainWindow()
        # WA_DontShowOnScreen：走完整布局流程但不绘制到屏幕。
        # 这样既拿到真实 geometry，又不触发屏幕合成相关的
        # Windows 0x8001010d 噪音（见文件头）。
        win.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
        win.resize(w, h)
        win.show()
        created.append(win)
        _settle(app)
        return win

    yield _make
    for x in created:
        x.close()
        x.deleteLater()


# ------------------------------------------------------------------ 用例
WIDTHS = [1120, 1160, 1200, 1280, 1320, 1360, 1440, 1600, 1920, 2560]


@pytest.mark.parametrize("w", WIDTHS)
def test_tab_is_horizontally_centered(make_win, w):
    """**核心判据**：Tab 中线 = 窗口中线（±2px），与左右内容宽度无关。

    这一条就是方案 A 的核心承诺。它在"改回 QHBoxLayout+弹簧"时会
    立刻变红——那种写法在 1120px 下可能只偏 4~6px，不测就发现不了。
    """
    win = make_win(w, 860)
    g = win.seg_tab.geometry()
    cx = g.x() + g.width() / 2.0
    mid = win._titlebar.width() / 2.0
    off = cx - mid
    assert abs(off) <= CENTER_TOL, (
        f"窗口 {w}px：Tab 中心 {cx} 偏离中线 {mid} 共 {off:+.1f}px "
        f"（容差 {CENTER_TOL}px）。左右两格的 columnStretch 是否仍相等？")


def test_tab_stays_centered_with_widest_pills(make_win):
    """把 pill 文案改成最坏情况后，Tab 仍然居中。

    `pill_fw` 的内容是动态的（`固件 v1.2.3 · Fs 100 kHz`），
    真机握手后比默认的"仿真模式"更长。右区变宽不能把 Tab 顶走。
    """
    win = make_win(1360, 860)
    win.pill_port.setText("COM12 · 921600")
    win.pill_fw.setText("固件 v1.2.3 · Fs 100 kHz")
    win.btn_conn.setText("断开")
    win._titlebar.layout().activate()
    _settle(QtWidgets.QApplication.instance())

    g = win.seg_tab.geometry()
    off = (g.x() + g.width() / 2.0) - win._titlebar.width() / 2.0
    assert abs(off) <= CENTER_TOL, (
        f"pill 换成最坏文案后 Tab 偏移 {off:+.1f}px")


@pytest.mark.parametrize("w", [1120, 1360, 1920])
def test_titlebar_text_all_visible(make_win, w):
    """顶栏每个控件的文字都要放得下（含最坏 pill 文案）。"""
    win = make_win(w, 860)
    win.pill_port.setText("COM12 · 921600")
    win.pill_fw.setText("固件 v1.2.3 · Fs 100 kHz")
    win.btn_conn.setText("断开")
    win._titlebar.layout().activate()
    _settle(QtWidgets.QApplication.instance())

    bar = win._titlebar
    widgets = [
        ("appName", bar.findChild(QtWidgets.QLabel, "AppName")),
        ("cmb_port", win.cmb_port),
        ("btn_conn", win.btn_conn),
        ("pill_port", win.pill_port),
        ("pill_fw", win.pill_fw),
    ] + [(f"seg:{b.text()}", b) for b in win.seg_tab._group.buttons()]

    bad = []
    for name, wdg in widgets:
        if wdg is None or not wdg.isVisible():
            continue                     # 窄窗口下 pill_fw 允许自动隐藏
        n, h = _need_w(wdg), _have_w(wdg)
        if h < n:
            bad.append(f"{name} 需 {n}px 只有 {h}px")
    assert not bad, f"{w}px 顶栏有文字截断：\n  " + "\n  ".join(bad)


def test_titlebar_is_single_row_not_two(make_win):
    """回归守门：顶栏必须是一行，不能又变回「顶栏 + 分页条」两行。

    v4.7 之前是两行共 92px，第二行只放一个 220px 的 Tab，白扔 46px。
    这个用例防止有人"改回去"。
    """
    win = make_win(1360, 860)
    bar = win._titlebar
    assert bar.height() == TITLEBAR_H, (
        f"顶栏高度 {bar.height()}，期望 {TITLEBAR_H}")
    # Tab 必须在顶栏里面（是它的子孙），不是独立的兄弟行
    assert bar.isAncestorOf(win.seg_tab), (
        "seg_tab 不在顶栏里 —— 分页条又被拆成独立一行了")
    # 顶栏底部要紧贴内容栈顶部，中间不能有别的行
    stack_top = win.stack.mapTo(win, win.stack.rect().topLeft()).y()
    assert stack_top == bar.y() + bar.height(), (
        f"顶栏底 {bar.y() + bar.height()} 与栈顶 {stack_top} 之间有夹层")


def test_pills_keep_capsule_height(make_win):
    """pill 高度必须锁在 sizeHint（21px），不能被撑满顶栏整高。

    QLabel 默认纵向策略是 Minimum（可变高），放进 52px 高的
    QHBoxLayout 里会被拉伸到 52px。而 `#Pill` 是
    `border-radius: 11px + padding: 3px 10px`，21px 才是胶囊，
    52px 就成了竖长圆角矩形、两端圆角全变形。
    """
    win = make_win(1360, 860)
    for name, pill in (("pill_port", win.pill_port), ("pill_fw", win.pill_fw)):
        assert pill.height() == pill.sizeHint().height(), (
            f"{name} 高 {pill.height()}px，应等于 sizeHint "
            f"{pill.sizeHint().height()}px")


def test_all_titlebar_widgets_vertically_centered(make_win):
    """所有顶栏控件围绕同一条水平中线居中（视觉对齐）。"""
    win = make_win(1360, 860)
    bar = win._titlebar
    mid = bar.height() / 2.0
    widgets = [win.led, win.seg_tab, win.cmb_port, win.btn_conn,
               win.pill_port, win.pill_fw]
    for wdg in widgets:
        cy = wdg.geometry().y() + wdg.height() / 2.0
        assert abs(cy - mid) <= 1.5, (
            f"{wdg.objectName() or type(wdg).__name__} 中心 y={cy:.1f}，"
            f"顶栏中线 {mid}，偏差 {cy - mid:+.1f}px")
