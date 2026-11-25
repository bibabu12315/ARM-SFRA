"""SFRA Studio - 复用小部件。

每个类都对应设计稿里的一个 CSS 类，命名保持一致，改样式只改 theme.py。
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from ... import theme


# ---------------------------------------------------------------- 标题（.h）
def section_label(text: str) -> QtWidgets.QLabel:
    """小号全大写分隔标题。"""
    lb = QtWidgets.QLabel(text)
    lb.setObjectName("H")
    lb.setContentsMargins(2, 0, 0, 0)
    return lb


# ---------------------------------------------------------------- 流式布局
class FlowLayout(QtWidgets.QLayout):
    """像文字排版一样从左往右摆，放不下就换行。

    **为什么需要它**：右栏只有 268px，串口日志面板只有 242px，而
    「ping / caps / conditions / status / coeff / abort」六个快捷按钮
    单是 `conditions` 就要 100px。用 `QHBoxLayout` 的结果是每个按钮被
    挤到 33px，文字全被截掉——这正是用户反馈"胶囊里的字看不全"的主因之一。

    `QHBoxLayout` 没有换行能力（省事的办法是嵌两层，但列数写死，
    窗口一变又错）。流式布局是唯一能同时满足"窗口可缩放"和
    "文字永远看得见"的方案。

    实现参照 Qt 官方 FlowLayout 示例，按需计算 `heightForWidth`。
    """

    def __init__(self, parent=None, margin: int = 0, hspacing: int = 4,
                 vspacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    # ---- 必须实现的三个纯虚函数 ----
    def addItem(self, item) -> None:              # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):                      # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):                      # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):                # noqa: N802
        return QtCore.Qt.Orientations()

    def hasHeightForWidth(self) -> bool:          # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QtCore.QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QtCore.QRect) -> None:   # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QtCore.QSize:           # noqa: N802
        """所有控件排成一行的理想尺寸（父容器会拿它当上限参考）。"""
        w = h = 0
        for it in self._items:
            s = it.sizeHint()
            w += s.width() + self._hspace
            h = max(h, s.height())
        m = self.contentsMargins()
        return QtCore.QSize(w + m.left() + m.right(),
                            h + m.top() + m.bottom())

    def minimumSize(self) -> QtCore.QSize:        # noqa: N802
        """最窄也要能放下**最宽的那一个**，否则它自己就被截断。"""
        w = h = 0
        for it in self._items:
            s = it.minimumSize()
            w = max(w, s.width())
            h = max(h, s.height())
        m = self.contentsMargins()
        return QtCore.QSize(w + m.left() + m.right(),
                            h + m.top() + m.bottom())

    # ---- 核心排布 ----
    def _do_layout(self, rect: QtCore.QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = eff.x(), eff.y(), 0
        for it in self._items:
            s = it.sizeHint()
            # 换行：这一行放不下了（且本行已经有东西）
            if x + s.width() > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y += line_h + self._vspace
                line_h = 0
            if not test_only:
                it.setGeometry(QtCore.QRect(x, y, s.width(), s.height()))
            x += s.width() + self._hspace
            line_h = max(line_h, s.height())
        return y + line_h - rect.y() + m.bottom()


class _FitFilter(QtCore.QObject):
    """给 `fit_button` 用的"字体/样式变了就重算"过滤器。

    为什么需要它：`fit_button` 在**构造期**被调用，那时 QSS 还没
    polish，`btn.font()` 拿到的是窗口默认字体（10pt），而 QSS 里写的是
    `font-size: 10px`（≈7.5pt）。实测 chip 的 `minimumWidth` 因此被算成
    74px，而它真实只需要 58px —— **白胖 16px**。

    这 16px 不会截断文字（可见性判据查不出来），但会撑挤同一行的兄弟
    控件，最终表现为"标题栏放不下、某某被挤窄"。所以必须纠。

    **两条触发路径都要有，且不能互相吃掉**：

    1. `QTimer.singleShot(0)` —— 事件循环第一轮。多数控件此时已 polish。
    2. `StyleChange` 事件 —— 有些控件（实测 `显示TX` 那个）的 polish
       发生在 singleShot **之后**，此时字体才是最终值。

    第一版用 `_done` 让两者互斥，结果 singleShot 先跑、把 `_done`
    置了位，后面真正的 `StyleChange` 被吃掉，`显示TX` 的宽度就永远
    停在 74px（应 48px）。所以这里**只用 `_done` 拦 `StyleChange` 的
    重复触发**，singleShot 那次不清 `_done`。
    """

    def __init__(self, btn: QtWidgets.QAbstractButton, pad_x: int,
                 pad_y: int, extra: int):
        super().__init__(btn)
        self._btn = btn
        self._pad_x = pad_x
        self._pad_y = pad_y
        self._extra = extra
        self._done = False
        QtCore.QTimer.singleShot(0, self._apply_early)

    def eventFilter(self, obj, ev):        # noqa: N802
        if ev.type() == QtCore.QEvent.StyleChange:
            self._apply_late()
        return False

    def _apply_early(self) -> None:
        """事件循环第一轮。**不动 `_done`**，留给 StyleChange 兜底。"""
        _set_fit_size(self._btn, self._pad_x, self._pad_y, self._extra)
        self._btn.updateGeometry()

    def _apply_late(self) -> None:
        """polish 之后（字体已最终）。这个才算定稿。"""
        if self._done:
            return
        self._done = True
        _set_fit_size(self._btn, self._pad_x, self._pad_y, self._extra)
        self._btn.updateGeometry()


def _set_fit_size(btn: QtWidgets.QAbstractButton, pad_x: int,
                  pad_y: int, extra: int) -> None:
    """按 `btn.font()`（当前生效字体）写死最小宽高。"""
    fm = QtGui.QFontMetrics(btn.font())
    bold = QtGui.QFont(btn.font())
    bold.setWeight(QtGui.QFont.DemiBold)          # QSS 的 font-weight:600
    fm_b = QtGui.QFontMetrics(bold)
    text_w = max(fm.horizontalAdvance(btn.text()),
                 fm_b.horizontalAdvance(btn.text()))
    btn.setMinimumWidth(text_w + 2 * pad_x + extra)
    btn.setMinimumHeight(max(fm.height(), fm_b.height()) + 2 * pad_y)


def fit_button(btn: QtWidgets.QAbstractButton,
               pad_x: int = 8, pad_y: int = 3,
               extra: int = 3) -> QtWidgets.QAbstractButton:
    """把按钮的最小宽度锁成"文本 + 内边距"，保证字不被截断但不过度膨胀。

    这是**文字可见性**的硬底线：Qt 不会把控件压到 `minimumWidth` 以下，
    宁可让父容器换行/滚动。

    三个必须注意的点：

    1. **必须用控件自己生效的字体**（`btn.font()`）。QSS 里的
       `font-size` 只有在控件被 polish 之后才反映到 `font()`，
       用 `QApplication.font()` 会算错——误差能到 20%，累计下来
       会让整组控件比容器还宽（第一版就是这样把 216px 的侧栏
       撑到 310px 需求的）。
    2. **调用时机决定了精度**。本函数通常在构造期被调，那时 QSS 还没
       polish，量到的是窗口默认字体，结果偏大（chip 实测多出 16px）。
       函数内部挂了 `_FitFilter`，等 `StyleChange` 到了会用真实字体
       重算一次，所以现在**不必**关心调用时机。
    3. **余量要给得克制但不能为零**。`extra` 默认 3px。为什么不是 0：
       QSS 模式下 `SE_PushButtonContents` = `w - 2*padding - 2*border`，
       而 `minimumWidth` 里每边已含 1px border，所以**净余量 = extra - 2**。
       `extra=2` 时净余量为 0 —— 实测 `全部 150` 的内容区正好 50px，
       恰好等于文字宽度，抗锯齿取整一进位就吃掉末尾像素。
       `extra=3` 留 1px，够用且不显著变胖。
       也不能给大：那是第一版把 216px 侧栏撑到 310px 的原因。
    """
    _set_fit_size(btn, pad_x, pad_y, extra)
    btn.setSizePolicy(QtWidgets.QSizePolicy.Minimum,
                      QtWidgets.QSizePolicy.Fixed)
    # 字体/样式变化后重算一次（见 _FitFilter 说明）。
    # 挂到按钮自己身上，随按钮销毁，不用手动清理。
    flt = _FitFilter(btn, pad_x, pad_y, extra)
    btn.installEventFilter(flt)
    btn._fit_filter = flt                # 持有引用，防被 GC
    return btn


# ---------------------------------------------------------------- 分组卡片（.grp）
class Group(QtWidgets.QFrame):
    """圆角卡片，内部竖向排列若干 Row，行间自动画分隔线。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Group")
        self._lay = QtWidgets.QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._rows: list[QtWidgets.QWidget] = []

    def add_row(self, row: QtWidgets.QWidget) -> QtWidgets.QWidget:
        self._lay.addWidget(row)
        self._rows.append(row)
        self._refresh_separators()
        return row

    def add_widget(self, w: QtWidgets.QWidget) -> QtWidgets.QWidget:
        self._lay.addWidget(w)
        return w

    def _refresh_separators(self) -> None:
        n = len(self._rows)
        for i, r in enumerate(self._rows):
            r.setObjectName("GroupRowLast" if i == n - 1 else "GroupRow")
            r.style().unpolish(r)
            r.style().polish(r)


class Row(QtWidgets.QFrame):
    """一行：左标签 + 右控件。"""

    def __init__(self, key: str, widget: QtWidgets.QWidget | None = None,
                 parent=None):
        super().__init__(parent)
        self.setObjectName("GroupRow")
        self.setMinimumHeight(theme.ROW_H)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(11, 5, 11, 5)
        lay.setSpacing(8)

        self.label = QtWidgets.QLabel(key)
        self.label.setObjectName("Key")
        lay.addWidget(self.label)
        lay.addStretch(1)

        self.value: Optional[QtWidgets.QWidget] = None
        if widget is not None:
            lay.addWidget(widget)
        self._lay = lay

    def set_value_widget(self, w: QtWidgets.QWidget) -> None:
        if self.value is not None:
            self._lay.removeWidget(self.value)
            self.value.deleteLater()
        self.value = w
        self._lay.addWidget(w)

    def set_text(self, text: str) -> None:
        """把右侧换成只读文本（.v）。"""
        lb = QtWidgets.QLabel(text)
        lb.setObjectName("Val")
        lb.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.set_value_widget(lb)


class KVRow(QtWidgets.QFrame):
    """紧凑键值行（.kv），Tab2 等效参数用。"""

    def __init__(self, key: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setObjectName("GroupRow")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(11, 6, 11, 6)
        lay.setSpacing(8)
        k = QtWidgets.QLabel(key)
        k.setObjectName("Key")
        self.v = QtWidgets.QLabel(value)
        self.v.setObjectName("ValSm")
        self.v.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        lay.addWidget(k)
        lay.addStretch(1)
        lay.addWidget(self.v)

    def set_value(self, text: str) -> None:
        self.v.setText(text)

    def set_tooltip(self, text: str) -> None:
        """给整行（键和值）挂同一个 tooltip。

        整行都挂而不是只挂值标签：用户要看到提示，鼠标得先停在行上，
        行高只有 26 px，要求停在右侧值标签上太苛刻。
        """
        self.setToolTip(text)


# ---------------------------------------------------------------- 数值输入
class NumEdit(QtWidgets.QLineEdit):
    """只收数字的输入框，失焦时校验并回调。

    用 QLineEdit 而不是 QDoubleSpinBox：设计稿里的输入框没有上下箭头，
    而且需要自定义 "0.01" 这种 4 位精度的 pu 值。
    """

    valueChanged = QtCore.pyqtSignal(float)

    def __init__(self, value: float, decimals: int = 2,
                 minimum: float = -1e18, maximum: float = 1e18,
                 suffix: str = "", parent=None):
        super().__init__(parent)
        self._dec = decimals
        self._min = minimum
        self._max = maximum
        self._suffix = suffix
        self._value = float(value)
        self._render()
        self.setFixedHeight(26)
        self.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.editingFinished.connect(self._commit)
        self.returnPressed.connect(self._commit)

    def _render(self) -> None:
        fx = f"{{:.{self._dec}f}}"
        self.setText(fx.format(self._value) + self._suffix)

    def value(self) -> float:
        return self._value

    def set_value(self, v: float, emit: bool = False) -> None:
        v = max(self._min, min(self._max, float(v)))
        changed = abs(v - self._value) > 1e-12
        self._value = v
        self._render()
        if emit and changed:
            self.valueChanged.emit(self._value)

    def _commit(self) -> None:
        txt = self.text().replace(self._suffix, "").replace(",", "").strip()
        try:
            v = float(txt)
        except ValueError:
            self._render()
            return
        if v < self._min:
            v = self._min
        if v > self._max:
            v = self._max
        changed = abs(v - self._value) > 1e-12
        self._value = v
        self._render()
        if changed:
            self.valueChanged.emit(self._value)


def num_with_unit(edit: QtWidgets.QWidget, unit: str) -> QtWidgets.QWidget:
    """输入框 + 右侧小单位。"""
    w = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(edit)
    if unit:
        u = QtWidgets.QLabel(unit)
        u.setObjectName("Unit")
        lay.addWidget(u)
    return w


# ---------------------------------------------------------------- 分段控件（.seg / .ps）
class SegmentedControl(QtWidgets.QWidget):
    """iOS 风格分段控件。互斥单选，切换时发 changed(index)。

    **尺寸红线：任何窗口宽度下都不许截断文字。**

    之前这里有两个错，导致「Plant」「Precise」选中后字只显示一半：

    1. `b.setStyleSheet(f"padding: 4px {pad[1]}px;")` —— 控件的局部
       QSS 会**整体覆盖** `theme.qss()` 里 `QPushButton#SegItem` 的规则，
       而那条规则里的 `:checked { border: 1px }` 被一起干掉又没补回来，
       Qt 算文本区域时边框/内边距对不上，文字被压掉。
    2. `lay.addWidget(b, 1)` 一律 stretch=1 **均分宽度**，
       完全不看 `sizeHint`。侧栏只有 300px，三项一均分每项 74px，
       而「Precise」需要 84px —— 必然截断。

    现在的做法：
      · 样式全部留在 `theme.qss()`（`padding` 按 `pad` 值由
        `setProperty("segPad", ...)` 传给 QSS，不写局部 styleSheet）；
      · 每项宽度用 `QFontMetrics` 算出"文本 + 2×左右内边距 + 边框"，
        作为 `setMinimumWidth`，Qt 就不可能把它压窄；
      · `sizeHint` 也按这个算，父布局才不会把它挤扁；
      · 剩余空间用 `addStretch` 吃掉，而不是均分给按钮
        （均分才是截断的根源）。
    """

    changed = QtCore.pyqtSignal(int)

    #: 每项左右额外留白（像素）。QSS 的 padding 由它推导。
    BORDER_PX = 2          # :checked 时左右各 1px 边框

    def __init__(self, items: list[str], current: int = 0,
                 pad: tuple[int, int] = (4, 10), parent=None):
        super().__init__(parent)
        self.setObjectName("SegTrack")
        self._items = items
        self._pad_x = int(pad[1])
        #: 当前**实际生效**的内边距（`_recalc` 可能把它收窄）
        self._active_pad = self._pad_x
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)

        for i, txt in enumerate(items):
            b = QtWidgets.QPushButton(txt)
            b.setObjectName("SegItem")
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setChecked(i == current)
            # 把内边距告诉 QSS（见 theme.qss 的 SegItem 规则），
            # **不要**再 setStyleSheet 局部覆盖。
            b.setProperty("segPadX", self._pad_x)
            b.setProperty("segPadY", int(pad[0]))
            b.setSizePolicy(QtWidgets.QSizePolicy.Minimum,
                            QtWidgets.QSizePolicy.Fixed)
            self._group.addButton(b, i)
            lay.addWidget(b)
        # 剩余空间给弹簧，不给按钮——均分会把"Precise"压成 74px。
        lay.addStretch(1)
        self._group.buttonClicked[int].connect(self.changed.emit)
        self._recalc()

    def _item_min_width(self, btn: QtWidgets.QPushButton,
                        pad_x: Optional[int] = None) -> int:
        """一项至少需要多宽。选中态的粗体也要算进去，否则选中才截断。"""
        px = self._pad_x if pad_x is None else pad_x
        fm = QtGui.QFontMetrics(btn.font())
        bold = QtGui.QFont(btn.font())
        bold.setWeight(QtGui.QFont.DemiBold)     # QSS: font-weight:600
        fm_bold = QtGui.QFontMetrics(bold)
        text_w = max(fm.horizontalAdvance(btn.text()),
                     fm_bold.horizontalAdvance(btn.text()))
        return text_w + 2 * px + self.BORDER_PX

    def _recalc(self) -> None:
        """按当前生效字体重算每项最小宽度，必要时**收窄内边距**。

        **必须在控件被 polish（QSS 生效）之后调用**：`QPushButton.font()`
        在应用样式表前拿到的是窗口默认字体，字宽差 20%，
        算出来的最小宽度会让整组控件比容器还宽。

        收窄内边距是这里唯一能让「三个按钮 + 窄侧栏」共存的让步：
        文字宽度是硬需求（不能截断），内边距是软需求（挤窄了只是
        看着紧一点）。可用宽度不够时逐级从 14px → 10px → 8px 试，
        仍不够才认命（此时父容器会出滚动条，文字依然完整）。
        """
        btns = self._group.buttons()
        if not btns:
            return
        avail = self._avail_width()
        # 优先用原内边距；不够就逐级收窄，找出第一个放得下的档位
        for px in self._pad_ladder():
            w_sum = sum(self._item_min_width(b, px) for b in btns)
            if avail <= 0 or w_sum <= avail:
                break
        else:
            px = self._pad_ladder()[-1]      # 全放不下，用最窄档（至少不更差）

        if px != self._active_pad:
            self._active_pad = px
            self._apply_qss_pad(px)
        for b in btns:
            b.setMinimumWidth(self._item_min_width(b, px))
        self.updateGeometry()

    def _pad_ladder(self) -> list:
        """可用的内边距档位（从宽到窄），必须都在 QSS 里有对应规则。"""
        cand = [14, 12, 10, 8, 6]
        ladder = [p for p in cand if p <= self._pad_x]
        return ladder or [self._pad_x]

    def _avail_width(self) -> int:
        """父容器实际能给我们的宽度。

        **以本控件自身宽度为准**：父容器在构造期间还没布局完，
        `parentWidget().width()` 拿到的是默认值（100px 之类），
        据此收窄内边距会算错。而 `self.width()` 是布局真正分给我们的，
        `resizeEvent` 里再算一次就能拿到最终值。
        """
        w = self.width()
        m = self.layout().contentsMargins()
        return max(0, w - m.left() - m.right())

    def _apply_qss_pad(self, px: int) -> None:
        """把新内边距同步给 QSS 属性（规则见 theme.qss 的 SegItem）。"""
        for b in self._group.buttons():
            b.setProperty("segPadX", int(px))
            b.style().unpolish(b)
            b.style().polish(b)

    def minimumSizeHint(self) -> QtCore.QSize:      # noqa: N802
        """所有项最小宽度之和 + 布局边距。父布局据此决定给多少空间，
        不会把我们挤到文字看不见。"""
        w = sum(b.minimumWidth() for b in self._group.buttons())
        m = self.layout().contentsMargins()
        return QtCore.QSize(
            w + m.left() + m.right(),
            max((b.sizeHint().height() for b in self._group.buttons()),
                default=24) + m.top() + m.bottom())
    def sizeHint(self) -> QtCore.QSize:             # noqa: N802
        return self.minimumSizeHint()

    def showEvent(self, ev) -> None:                # noqa: N802
        """QSS 生效后再算一次宽度。

        构造时 `btn.font()` 还是窗口默认字体，`font-size: 12px` 这类
        样式要到 polish 之后才反映进 `font()`。不在这里纠一次，
        最小宽度会按错误字体算出来，反而让控件比容器还宽。
        """
        super().showEvent(ev)
        self._recalc()

    def resizeEvent(self, ev) -> None:              # noqa: N802
        """窗口拉伸时重新适配内边距 —— **这是"缩放也不截断"的关键**。

        侧栏是固定宽（300px），但用户可以缩放整个窗口、也可以改系统
        字体大小，两种情况都会改变我们拿到的实际宽度。宽度一变就重新
        选内边距档位，保证文字始终完整。
        """
        super().resizeEvent(ev)
        self._recalc()

    def current(self) -> int:
        return self._group.checkedId()

    def set_current(self, i: int, emit: bool = False) -> None:
        btn = self._group.button(i)
        if btn is None:
            return
        was = self._group.checkedId()
        btn.setChecked(True)
        if emit and was != i:
            self.changed.emit(i)


# ---------------------------------------------------------------- 开关（.sw）
class Switch(QtWidgets.QCheckBox):
    """36×21 的 iOS 开关。用 QCheckBox 自绘，保留键盘可达性。"""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setChecked(checked)
        self.setFixedSize(36, 21)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self._t = 0.0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._on_toggle)

    def _on_toggle(self, on: bool) -> None:
        self._anim.setDirection(
            QtCore.QAbstractAnimation.Forward if on
            else QtCore.QAbstractAnimation.Backward)
        self._anim.start()

    def _on_anim(self, v) -> None:
        self._t = float(v)
        self.update()

    def paintEvent(self, e) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(0, 0, self.width(), self.height())
        rad = r.height() / 2

        off = QtGui.QColor("#e5e5ea")
        on = QtGui.QColor(theme.GREEN)
        c = QtGui.QColor(
            int(off.red() + (on.red() - off.red()) * self._t),
            int(off.green() + (on.green() - off.green()) * self._t),
            int(off.blue() + (on.blue() - off.blue()) * self._t))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(r, rad, rad)

        d = r.height() - 4
        x = 2 + (r.width() - d - 4) * self._t
        p.setBrush(QtGui.QColor("#ffffff"))
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 30), 0.5))
        p.drawEllipse(QtCore.QRectF(x, 2, d, d))


# ---------------------------------------------------------------- 说明块（.note）
class Note(QtWidgets.QFrame):
    """浅灰底的一行说明；kind='warn' 时变橙底。"""

    def __init__(self, text: str = "", warn: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("NoteWarn" if warn else "Note")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(7)
        self._dot = QtWidgets.QLabel("●")
        self._dot.setObjectName("NoteDotWarn" if warn else "NoteDot")
        self._dot.setFixedWidth(6)
        self._dot.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignHCenter)
        self._txt = QtWidgets.QLabel(text)
        self._txt.setObjectName("NoteTextWarn" if warn else "NoteText")
        self._txt.setWordWrap(True)
        lay.addWidget(self._dot)
        lay.addWidget(self._txt, 1)

    def set_text(self, text: str, warn: bool | None = None) -> None:
        self._txt.setText(text)
        if warn is not None and warn != (self.objectName() == "NoteWarn"):
            self.setObjectName("NoteWarn" if warn else "Note")
            self._dot.setObjectName("NoteDotWarn" if warn else "NoteDot")
            self._txt.setObjectName("NoteTextWarn" if warn else "NoteText")
            for w in (self, self._dot, self._txt):
                w.style().unpolish(w)
                w.style().polish(w)


def notes_list() -> tuple[QtWidgets.QWidget, Callable[[list[tuple[str, bool]]], None]]:
    """告警区：一个竖向容器 + 一个 set(items) 回调。"""
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    state = {"widgets": []}

    def set_items(items: list[tuple[str, bool]]) -> None:
        for x in state["widgets"]:
            lay.removeWidget(x)
            x.deleteLater()
        state["widgets"].clear()
        if not items:
            items = [("无告警", False)]
        for text, warn in items:
            n = Note(text, warn)
            lay.addWidget(n)
            state["widgets"].append(n)
        w.updateGeometry()

    set_items([])
    return w, set_items

# ---------------------------------------------------------------- 卡片（.cc）
class Card(QtWidgets.QFrame):
    """带标题栏的卡片容器，body 里放内容。"""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QtWidgets.QFrame()
        head.setObjectName("CardHeader")
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(13, 9, 13, 7)
        hl.setSpacing(6)

        t = QtWidgets.QLabel(title)
        t.setObjectName("CardTitle")
        hl.addWidget(t)
        if subtitle:
            s = QtWidgets.QLabel(f"  {subtitle}")
            s.setStyleSheet(f"color:{theme.T3}; font-size:10.5px;")
            hl.addWidget(s)
        hl.addStretch(1)
        self.head_layout = hl
        outer.addWidget(head)

        self.body = QtWidgets.QWidget()
        self.body_layout = QtWidgets.QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(6, 2, 6, 6)
        self.body_layout.setSpacing(0)
        outer.addWidget(self.body, 1)

    def add_legend(self, text: str, color: str) -> None:
        """在标题栏右侧加一个图例项。"""
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        bar = QtWidgets.QFrame()
        bar.setFixedSize(13, 3)
        bar.setStyleSheet(f"background:{color}; border-radius:2px;")
        lb = QtWidgets.QLabel(text)
        lb.setStyleSheet(f"color:{color}; font-size:10.5px;")
        h.addWidget(bar)
        h.addWidget(lb)
        self.head_layout.addWidget(w)


# ---------------------------------------------------------------- 大数字
class BigValue(QtWidgets.QWidget):
    """判据读数：数字 + 单位，颜色随状态变。"""

    def __init__(self, unit: str = "", parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._lay = lay
        self._align(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.num = QtWidgets.QLabel("—")
        self.num.setObjectName("BigVal")
        self.num.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        lay.addWidget(self.num)
        self.unit = None
        if unit:
            u = QtWidgets.QLabel(unit)
            u.setObjectName("BigUnit")
            u.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
            u.setContentsMargins(0, 0, 0, 3)
            lay.addWidget(u)
            self.unit = u

    def _align(self, a) -> None:
        self._lay.setAlignment(a)

    def setAlignment(self, a) -> None:
        """外层布局对齐 + 数字标签对齐一起改。"""
        self._align(a)
        if getattr(self, "num", None) is not None:
            self.num.setAlignment(a)

    def set(self, text: str, color: str | None = None) -> None:
        self.num.setText(text)
        c = color or theme.T1
        self.num.setStyleSheet(f"color:{c};")
