"""极端缩放压力测试：窗口一路压到最小、拉到大屏，逐档验证文字可见。

对应需求原话："**即使我上位机边框放大缩小，内部这些胶囊或者按钮内的
文字信息也能看见**"。所以必须覆盖两个极端：

  · 横向 960 → 3440（含超宽屏，此时右栏会被拉得很宽）
  · 纵向 600 → 1080（高度只影响滚动，不影响横向分配，但要确认）
  · 系统字号 8 → 16pt（用户调 DPI 的等效场景，最容易被漏测）

## 为什么不做成 pytest 用例

29 组条件每组都要建整个 TabMeasure，跑一遍 4 分钟。pytest 里只保留
5 组窗口尺寸 + 4 档字号（见 test_ui_text_fit.py），够当守门；
极端档位放这里，交付前手工跑一次。

## 判据

与 src/tests/test_ui_text_fit.py **完全一致**：
QStyle 的 `SE_PushButtonContents` 内容矩形 vs 文字宽度，
并且切到 :checked 再量一次（`:checked` 加 1px 边框 + 加粗）。

与其它脚本的分工见 _verify_gate.py 文件头。
"""
import sys

from PyQt5 import QtGui, QtWidgets

sys.path.insert(0, "src")

from app import theme
from app.ui.tab_measure import TabMeasure

app = QtWidgets.QApplication([])


def make(w, h, pt=None):
    if pt is not None:
        f = QtGui.QFont(app.font())
        f.setPointSize(pt)
        app.setFont(f)
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


def need_w(b):
    f = b.font()
    fb = QtGui.QFont(f)
    fb.setWeight(QtGui.QFont.DemiBold)
    return max(QtGui.QFontMetrics(f).horizontalAdvance(b.text()),
               QtGui.QFontMetrics(fb).horizontalAdvance(b.text()))


def have_w(b):
    opt = QtWidgets.QStyleOptionButton()
    b.initStyleOption(opt)
    return b.style().subElementRect(
        QtWidgets.QStyle.SE_PushButtonContents, opt, b).width()


def groups(tab):
    con = tab.console
    return [
        ("模式", list(tab.seg_mode._group.buttons())),
        ("精度", list(tab.seg_cyc._group.buttons())),
        ("被测对象", list(tab.seg_dut._group.buttons())),
        ("chip", list(con._chips.values())),
        ("显示TX", [con._btn_tx]),
        ("快捷", con.findChildren(QtWidgets.QPushButton, "QBtn")),
        ("发送", [con._btn_send]),
    ]


def scan(tab):
    bad = []
    for name, btns in groups(tab):
        for b in btns:
            chk = b.isCheckable()
            states = [False, True] if chk else [False]
            for st in states:
                if chk:
                    b.setChecked(st)
                    b.style().unpolish(b)
                    b.style().polish(b)
                    for _ in range(2):
                        app.processEvents()
                n, ha = need_w(b), have_w(b)
                if ha < n:
                    bad.append(f"{name}.'{b.text().strip()}'"
                               f"{'[选]' if st else ''} 需{n} 有{ha}")
    return bad


print("=" * 70)
print("一、窗口宽度逐档扫描（高度固定 860）")
print("=" * 70)
WIDTHS = [960, 1000, 1040, 1080, 1120, 1200, 1280, 1360, 1440,
          1600, 1760, 1920, 2560, 3440]
all_bad = 0
for w in WIDTHS:
    tab = make(w, 860)
    bad = scan(tab)
    all_bad += len(bad)
    mark = "OK " if not bad else f"!! {len(bad)}处"
    print(f"  {w:5d}×860  {mark}")
    for x in bad[:3]:
        print(f"           {x}")
    tab.close()

print()
print("=" * 70)
print("二、窗口高度逐档扫描（宽度固定 1360）")
print("=" * 70)
for h in [600, 640, 680, 720, 800, 900, 1080]:
    tab = make(1360, h)
    bad = scan(tab)
    all_bad += len(bad)
    print(f"  1360×{h:<5d}  {'OK ' if not bad else f'!! {len(bad)}处'}")
    for x in bad[:3]:
        print(f"           {x}")
    tab.close()

print()
print("=" * 70)
print("三、系统字号逐档扫描（窗口 1280×860，最窄的常用尺寸）")
print("=" * 70)
for pt in [8, 9, 10, 11, 12, 13, 14, 16]:
    tab = make(1280, 860, pt=pt)
    bad = scan(tab)
    all_bad += len(bad)
    print(f"  {pt:2d}pt  {'OK ' if not bad else f'!! {len(bad)}处'}")
    for x in bad[:3]:
        print(f"           {x}")
    tab.close()

print()
print("=" * 70)
if all_bad == 0:
    print(f"结论：{len(WIDTHS) + 7 + 8} 组条件下，全部按钮（含选中态）文字完整")
else:
    print(f"结论：共 {all_bad} 处截断")
sys.exit(0 if all_bad == 0 else 1)
