"""判据有效性自检 —— 验证"文字可见性"判据不是摆设。

## 为什么需要它

`src/tests/test_ui_text_fit.py` 的判据第一版写错了：

    avail = b.width() - 2 * pad_x - 2      # pad_x 还硬编码 7/8

而 `fit_button()` 设最小宽度用的正是 `text_w + 2*pad_x + 2`。
两边同一个公式，`avail >= need` 恒成立 —— **任何缺陷都抓不到**。
当时做的"注入缺陷"验证里三类缺陷全部漏检，就是这个自证循环搞的。

## 这个脚本怎么验

不搞注入（项目现在没缺陷，注入也抓不到），而是**反向**：
人为把按钮压到 `need - 6`，要求判据**必须报错**。
"压窄了还报通过"就是判据失效的直接证据。

## 与其它脚本的分工

  _verify_gate.py    判据本身有没有效（本文件）
  _stress_resize.py  极端缩放下文字是否完整（29 组条件压力测试）
  inspect_ui.py      整体布局是否符合设计（栏宽、重叠、曲线点数）
  zoom_repro.py      复现条纹问题

改判据后请一并跑 _verify_gate.py。
"""
import sys

from PyQt5 import QtGui, QtWidgets

sys.path.insert(0, "src")

from app import theme
from app.ui.tab_measure import TabMeasure

app = QtWidgets.QApplication([])


def make(w=1360, h=900):
    app.setStyleSheet(theme.qss())
    from app.comm.controller import SimLinkController
    tab = TabMeasure(SimLinkController())
    tab.resize(w, h)
    tab.ensurePolished()
    for x in tab.findChildren(QtWidgets.QWidget):
        if x.layout():
            x.layout().activate()
    for _ in range(8):
        app.processEvents()
    con = tab.console
    con.clear()
    for i in range(150):
        con.log_info(f"x{i}")
    for x in tab.findChildren(QtWidgets.QWidget):
        if x.layout():
            x.layout().activate()
    for _ in range(6):
        app.processEvents()
    return tab


def content_w(b):
    opt = QtWidgets.QStyleOptionButton()
    b.initStyleOption(opt)
    return b.style().subElementRect(
        QtWidgets.QStyle.SE_PushButtonContents, opt, b).width()


def need_w(b):
    f = b.font()
    fb = QtGui.QFont(f)
    fb.setWeight(QtGui.QFont.DemiBold)
    return max(QtGui.QFontMetrics(f).horizontalAdvance(b.text()),
               QtGui.QFontMetrics(fb).horizontalAdvance(b.text()))


def check(b):
    return content_w(b) >= need_w(b)


print("=" * 66)
tab = make()
con = tab.console
# 关键：fit_button 的延迟重算会改 minimumWidth，必须让布局再跑一遍，
# 否则 width() 还是旧值，content 量出来偏大（上一版就误判成"贴边"）。
for x in tab.findChildren(QtWidgets.QWidget):
    if x.layout():
        x.layout().activate()
for _ in range(6):
    app.processEvents()

samples = [
    ("chip 全部150", con._chips[None]),
    ("chip ERR", con._chips["err"]),
    ("qbtn status", [b for b in con.findChildren(
        QtWidgets.QPushButton, "QBtn") if b.text() == "status"][0]),
    ("send", con._btn_send),
    ("seg 标准", tab.seg_cyc._group.button(1)),
    ("seg Plant", tab.seg_dut._group.button(0)),
]

print("【1】正常状态 —— 应为全部通过")
for name, b in samples:
    b.setChecked(True)
    b.style().unpolish(b)
    b.style().polish(b)
    for _ in range(2):
        app.processEvents()
    ok = check(b)
    print(f"  {name:14s} need={need_w(b):3d} content={content_w(b):3d} "
          f"{'通过' if ok else '!! 截断'}")

print()
print("【2】把每个按钮宽度强行压到 need-6 —— 判据必须全部报截断")
missed = []
for name, b in samples:
    w0 = b.width()
    b.setFixedWidth(max(4, need_w(b) - 6 + (w0 - content_w(b))))
    b.style().unpolish(b)
    b.style().polish(b)
    for _ in range(4):
        app.processEvents()
    ok = check(b)
    status = "抓到" if not ok else "!! 漏检"
    print(f"  {name:14s} need={need_w(b):3d} content={content_w(b):3d} "
          f"-> {status}")
    if ok:
        missed.append(name)

print()
print("=" * 66)
print("结论：" + ("判据有效：压窄后全部报截断" if not missed
                  else f"判据失效，漏检 {missed}"))
sys.exit(0 if not missed else 1)
