"""全界面「文字必须看得见」守门测试。

## 为什么需要这个文件

用户反馈：「按钮胶囊比较小，选中后导致里面的字只显示一半」，
点名的有两处（扫频配置的 `Plant`、串口日志面板几乎全部），
并且要求：**即使窗口放大缩小，内部胶囊/按钮的文字信息也要能看见。**

这是个"不会报错、只会悄悄难看"的缺陷——功能全对、测试全绿，
只有人肉看才看得出。所以必须把它变成**可自动判定**的断言。

## 判定方法（第一版写错过，务必看完）

对每个按钮比两个量：

    need = 文字宽度（常规与粗体取大，因为 :checked 会加粗到 600）
    have = **Qt 样式系统算出的内容矩形宽度**

    `b.style().subElementRect(QStyle.SE_PushButtonContents, opt, b)`

**内容矩形必须由 QStyle 算，不能自己套公式。** 第一版写的是
`avail = b.width() - 2 * pad_x - 2`，pad_x 还硬编码 7/8 —— 而
`fit_button()` 设最小宽度用的正是 `text_w + 2*pad_x + 2`。
两边同一个公式，`avail >= need` 恒成立，**任何缺陷都抓不到**。
当时的"注入缺陷验证"里 D1/D2/D3 全部漏检，就是被这个自证循环
掩盖了（见 tools/_verify_gate.py 的有效性自检）。

改用 `SE_PushButtonContents` 之后：它读的是 QSS 里真正生效的
border + padding，与生产代码零耦合。

## 判据有效性的自检

`tools/_verify_gate.py` 会人为把按钮压窄 6px，要求判据**全部报错**。
"压窄了仍报通过"就是判据失效的直接证据。改本文件后请一并跑它。

## 覆盖范围

七组控件 × 5 组窗口尺寸 × 4 档系统字号，外加 chip 计数增长、
以及"缩放后重新量"的动态场景。

## 踩过的坑（改这个文件前先读）

1. **必须用控件自己生效的字体**（`btn.font()`）量，不能用
   `QApplication.font()`。QSS 的 `font-size` 要到 polish 之后才进
   `font()`，用错字体算出来的宽度差 20%。
2. **必须 `processEvents()` 若干次**再量。Qt 的布局是延迟执行的，
   调完 `resize()` 立刻量拿到的是旧尺寸。
3. **不要 `show()` 窗口**。窗口首次显示会触发 Windows 的
   `0x8001010d`（RPC_E_CANTCALLOUT_ININPUTSYNCCALL），在输出里留一行
   "Windows fatal exception"。它不影响断言，但会让真正的崩溃淹没在
   噪音里。用 `ensurePolished()` + `layout().activate()` 代替。
4. **必须切到 :checked 再量一次**。用户原话是"**选中后**字只显示一半"，
   `:checked` 会加 `border: 1px` 和 `font-weight: 600`，两个都吃宽度。
5. **计数要造到 3 位数**。chip 上的 `全部 1` → `全部 150` 会宽 20px，
   这是最容易被忽略的截断来源。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5 import QtGui, QtWidgets                                # noqa: E402

from app import theme                                              # noqa: E402
from app.ui.tab_measure import TabMeasure                          # noqa: E402

pytestmark = pytest.mark.e2e


# ------------------------------------------------------------------ 判据
def _need_w(btn: QtWidgets.QAbstractButton) -> int:
    """按钮文字需要的宽度。粗体也算（:checked 的 font-weight:600）。"""
    f = btn.font()
    fb = QtGui.QFont(f)
    fb.setWeight(QtGui.QFont.DemiBold)
    return max(QtGui.QFontMetrics(f).horizontalAdvance(btn.text()),
               QtGui.QFontMetrics(fb).horizontalAdvance(btn.text()))


def _have_w(btn: QtWidgets.QAbstractButton) -> int:
    """按钮内部真正能画字的宽度 —— **由 QStyle 独立计算**。

    这就是 `SE_PushButtonContents` 的定义：按钮矩形去掉 border 和
    padding 之后剩下的区域。用它而不是自己减 pad_x，判据才独立于
    `fit_button()` 的算法（否则是拿假设证明假设）。
    """
    opt = QtWidgets.QStyleOptionButton()
    btn.initStyleOption(opt)
    return btn.style().subElementRect(
        QtWidgets.QStyle.SE_PushButtonContents, opt, btn).width()


def _check(b: QtWidgets.QAbstractButton) -> str | None:
    """返回 None 表示通过，否则返回一句问题描述。"""
    need, have = _need_w(b), _have_w(b)
    if have < need:
        return f"'{b.text().strip()}' 需 {need}px 只有 {have}px"
    return None


# ------------------------------------------------------------------ 取样
def _all_buttons(tab: TabMeasure):
    """返回 [(组名, [按钮], 是否 checkable), ...]。

    组名只用于报错定位。**内边距不再写死**——由 `_have_w` 从 QStyle 取。
    """
    con = tab.console
    return [
        ("模式", list(tab.seg_mode._group.buttons()), True),
        ("精度", list(tab.seg_cyc._group.buttons()), True),
        ("被测对象", list(tab.seg_dut._group.buttons()), True),
        ("过滤chip", list(con._chips.values()), True),
        ("显示TX", [con._btn_tx], True),
        ("快捷命令", con.findChildren(QtWidgets.QPushButton, "QBtn"), False),
        ("发送", [con._btn_send], False),
    ]


def _settle(app, n: int = 8) -> None:
    """把 Qt 的延迟布局跑完。**量尺寸前必须做。**

    只 `processEvents()`，**不 show 窗口**（原因见文件头坑 3）。
    `resize()` 会立刻改变 geometry，布局在下次 `processEvents()` 时
    执行，`width()` 就是真实分配值。
    """
    for _ in range(n):
        app.processEvents()


def _layout(tab: TabMeasure) -> None:
    """强制把 tab 的整棵控件树布局一遍（不依赖窗口是否可见）。"""
    for w in tab.findChildren(QtWidgets.QWidget):
        lay = w.layout()
        if lay is not None:
            lay.activate()


def _repolish(btn: QtWidgets.QAbstractButton) -> None:
    """切换 :checked 后重新应用 QSS，让 border/font 变化落到 style 里。"""
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def _scan(tab: TabMeasure, app, force_check: bool = True) -> list[str]:
    """扫一遍全部按钮。`force_check=True` 时每项都切到选中态量一次。

    用户报的是"**选中后**字只显示一半"，所以选中态必须单独量：
    `:checked` 多 1px 边框 + 加粗，两项都吃宽度。
    """
    bad = []
    for name, btns, checkable in _all_buttons(tab):
        for b in btns:
            if not checkable:
                msg = _check(b)
                if msg:
                    bad.append(f"{name}.{msg}")
                continue
            states = [False, True] if force_check else [b.isChecked()]
            for st in states:
                b.setChecked(st)
                _repolish(b)
                _settle(app, 2)
                msg = _check(b)
                if msg:
                    bad.append(f"{name}.{'[选中]' if st else ''}{msg}")
    return bad


@pytest.fixture
def make_tab():
    created = []

    def _make(w: int = 1360, h: int = 900):
        from app.comm.controller import SimLinkController
        app = QtWidgets.QApplication.instance()
        app.setStyleSheet(theme.qss())
        ctrl = SimLinkController()
        tab = TabMeasure(ctrl)
        tab.resize(w, h)
        created.append(tab)
        # ensurePolished: QSS 的 font-size 进到 font()；
        # _layout: 尺寸落位。两者都不需要窗口可见。
        tab.ensurePolished()
        _layout(tab)
        _settle(app)
        # 把 chip 计数造到 3 位数（最宽情况）
        con = tab.console
        con.clear()
        for i in range(150):
            con.log_info(f"x{i}")
        _layout(tab)
        _settle(app, 6)
        return tab

    yield _make
    for t in created:
        t.close()
        t.deleteLater()


# ------------------------------------------------------------------ 用例
SIZES = [(1120, 720), (1280, 860), (1360, 860), (1600, 1000), (1920, 1080)]


@pytest.mark.parametrize("w,h", SIZES)
def test_all_button_text_fits(make_tab, w, h):
    """**核心判据**：每组控件的每个按钮（含选中态）都要容得下文字。

    这个用例就是用户诉求的直接翻译——"窗口放大缩小，字也要看得见"。
    """
    app = QtWidgets.QApplication.instance()
    tab = make_tab(w, h)
    bad = _scan(tab, app)
    assert not bad, f"{w}x{h} 有 {len(bad)} 处文字被截断：\n  " + \
        "\n  ".join(bad)


def test_segmented_control_does_not_overflow_sidebar(make_tab):
    """分段控件不能要求比侧栏还给得多的宽度。

    这是"修好文字截断"的反面陷阱：为了不截断而把 `minimumWidth`
    算得过大，控件反而会撑破 300px 的侧栏（第一版就是这样，
    `seg_mode` 要 310px）。两头都必须守住。
    """
    tab = make_tab(1360, 900)
    for name, seg in (("模式", tab.seg_mode), ("精度", tab.seg_cyc),
                      ("被测对象", tab.seg_dut)):
        need = seg.minimumSizeHint().width()
        have = seg.width()
        assert need <= have + 1, (
            f"{name} 最小需求 {need}px 超过实际 {have}px —— "
            f"内边距档位没收到位，或标签太长")


def test_console_panel_not_squeezed(make_tab):
    """日志面板整体不能被右栏压扁。

    面板里任何子控件被压都会导致文字/图标被裁，所以这条是
    "各组按钮都没问题但整体仍然越界"的兜底判据。
    """
    tab = make_tab(1360, 900)
    con = tab.console
    assert con.minimumSizeHint().width() <= con.width() + 1, (
        f"面板最小需求 {con.minimumSizeHint().width()}px "
        f"超过实际 {con.width()}px")


@pytest.mark.parametrize("pt", [9, 10, 12, 14])
def test_text_fits_at_larger_ui_font(make_tab, pt):
    """用户把系统字体/DPI 调大时，文字仍要完整。

    这类场景最容易漏测：默认字体下一切正常，用户一放大就全截断。
    """
    app = QtWidgets.QApplication.instance()
    old = app.font()
    f = QtGui.QFont(old)
    f.setPointSize(pt)
    app.setFont(f)
    try:
        tab = make_tab(1360, 900)
        bad = _scan(tab, app)
        assert not bad, (
            f"字体 {pt}pt 时有 {len(bad)} 处截断：\n  " + "\n  ".join(bad))
    finally:
        app.setFont(old)


def test_chip_grows_when_count_reaches_three_digits(make_tab):
    """计数从 1 位涨到 3 位时，chip 必须跟着变宽。

    这是最隐蔽的一处：文字更新了但没重算 minimumWidth，
    结果 `全部 1` 好好的，`全部 150` 就被截成 `全部 15`。
    """
    app = QtWidgets.QApplication.instance()
    tab = make_tab(1360, 900)
    con = tab.console
    chip = con._chips[None]              # "全部"

    con.clear()
    _layout(tab)
    _settle(app, 4)
    w1 = chip.minimumWidth()

    for i in range(150):
        con.log_info(f"x{i}")
    _layout(tab)
    _settle(app, 4)
    w3 = chip.minimumWidth()

    assert "150" in chip.text()
    assert w3 > w1, (
        f"计数到 3 位后 chip 宽度没变（{w1}px）—— 文字会截断")
    assert not _check(chip), "变宽后仍放不下文字"


def test_no_button_is_narrower_than_its_text_after_resize(make_tab):
    """缩放窗口后再量一次：布局变化不能把已经排好的按钮又挤窄。"""
    app = QtWidgets.QApplication.instance()
    tab = make_tab(1600, 1000)
    for w in (1120, 1500, 1280, 1360):
        tab.resize(w, 900)
        _layout(tab)
        _settle(app)
        bad = _scan(tab, app)
        assert not bad, (
            f"缩放到 {w}px 后有 {len(bad)} 处截断：\n  " + "\n  ".join(bad))


def test_checked_state_is_measured_not_assumed():
    """自检：判据必须真的能报错。

    如果 `_check()` 永远返回 None，上面所有用例都是摆设。
    这里造一个"故意太窄"的按钮，要求判据报出来。
    """
    app = QtWidgets.QApplication.instance()
    app.setStyleSheet(theme.qss())
    b = QtWidgets.QPushButton("VeryLongButtonText")
    b.setObjectName("QBtn")
    b.ensurePolished()
    b.resize(10, 24)
    b.setFixedWidth(10)
    app.processEvents()
    assert _have_w(b) < _need_w(b), "造出来的按钮竟然够宽，用例失效"
    assert _check(b) is not None, "判据对明显过窄的按钮不报错 —— 它是死的"
