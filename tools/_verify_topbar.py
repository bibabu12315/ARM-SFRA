"""验证顶栏合并后 Tab 的居中精度 + 顶栏组件文字可见性。

对应设计稿 `design/sfra-topbar-merge.html` 方案 A 的两个承诺：

  1. **Tab 几何中线落在窗口中线**（不依赖左右内容宽度）
  2. **顶栏所有控件文字不被截断**（含 pill / 按钮 / 下拉框）

## 为什么必须程序化验证

方案 A 的核心风险是"合并后 Tab 被左右内容推离中线"。这个偏移
在 1~2px 时肉眼完全看不出，但用户一旦把窗口拉到某个特定宽度，
可能突然变成 20~30px 的视觉失衡。逐档扫描才能确认。

## 判据

  居中偏差 = |Tab 中心 x - 窗口中线 x|，要求 <= 2px
  （1px 是 Qt 布局的取整误差，2px 给一点余量）

  文字可见：与 test_ui_text_fit.py 同一套 QStyle 判据
"""
import os
import sys

os.environ.setdefault("SFRA_BACKEND", "sim")
sys.path.insert(0, "src")

from PyQt5 import QtCore, QtGui, QtWidgets                     # noqa: E402

from app import theme                                          # noqa: E402
from app.main_window import MainWindow                         # noqa: E402

app = QtWidgets.QApplication([])
app.setStyleSheet(theme.qss())


def settle(n=10):
    for _ in range(n):
        app.processEvents()


def need_w(wdg, bold_if_checkable=True):
    """控件文字需要的宽度。

    **只对可勾选控件**取"常规 vs 粗体"的较大值——因为只有
    `QPushButton#SegItem:checked { font-weight:600 }` 会在选中时加粗。
    `#Small` 按钮、`#Pill` 标签在任何状态下都不加粗，用粗体去量会
    虚高 10~15%，误报成"截断"（第一版就是这样：`btn_conn` 实际文字
    22px，按粗体量成 24px，`contentsRect` 42px 明明够）。
    """
    txt = wdg.text() if hasattr(wdg, "text") else ""
    f = wdg.font()
    fm = QtGui.QFontMetrics(f)
    plain = fm.horizontalAdvance(txt)
    if bold_if_checkable and getattr(wdg, "isCheckable", lambda: False)():
        fb = QtGui.QFont(f)
        fb.setWeight(QtGui.QFont.DemiBold)
        return max(plain, QtGui.QFontMetrics(fb).horizontalAdvance(txt))
    return plain


def have_w(wdg):
    """控件内部真正能画字的宽度。

    QPushButton 用 QStyle 的 `SE_PushButtonContents`（与
    test_ui_text_fit.py 同一判据）；QLabel 用 `contentsRect()`
    ——**它已经把 QSS 的 padding 扣掉了**，但没扣 border，
    而 pill 是 `border: 0`，所以够用。
    """
    if isinstance(wdg, (QtWidgets.QPushButton, QtWidgets.QToolButton)):
        opt = QtWidgets.QStyleOptionButton()
        wdg.initStyleOption(opt)
        return wdg.style().subElementRect(
            QtWidgets.QStyle.SE_PushButtonContents, opt, wdg).width()
    return wdg.contentsRect().width()


print("=" * 74)
print("一、Tab 居中精度（逐档窗口宽度）")
print("=" * 74)
bad_center = 0
for wd in (1120, 1160, 1200, 1240, 1280, 1320, 1360, 1400, 1440,
           1520, 1600, 1760, 1920, 2200, 2560):
    win = MainWindow()
    win.resize(wd, 860)
    win.show()
    settle()
    bar = win._titlebar
    g = win.seg_tab.geometry()
    # seg_tab 的父控件就是顶栏，geometry 已是顶栏坐标系
    cx = g.x() + g.width() / 2
    mid = bar.width() / 2
    off = cx - mid
    ok = abs(off) <= 2
    if not ok:
        bad_center += 1
    print(f"  {wd:5d}px  顶栏{bar.width():5d}  Tab[{g.x():4d},{g.x()+g.width():4d}] "
          f"中心{cx:7.1f}  中线{mid:7.1f}  偏差{off:+6.1f}  "
          f"{'OK' if ok else '!! 偏移'}")
    win.close()

print()
print("=" * 74)
print("二、顶栏控件文字可见性（多档宽度 × 全连接态）")
print("=" * 74)
bad_text = 0
for wd in (1120, 1280, 1360, 1600, 1920):
    win = MainWindow()
    win.resize(wd, 860)
    win.show()
    settle()
    # 造最坏文案：真机 + 固件握手完成
    win.pill_port.setText("COM12 · 921600")
    win.pill_fw.setText("固件 v1.2.3 · Fs 100 kHz")
    win.btn_conn.setText("断开")
    bar = win._titlebar
    bar.layout().activate()
    settle()

    widgets = [
        ("appName", bar.findChild(QtWidgets.QLabel, "AppName")),
        ("cmb_port", win.cmb_port),
        ("btn_conn", win.btn_conn),
        ("pill_port", win.pill_port),
        ("pill_fw", win.pill_fw),
    ] + [(f"seg:{b.text()}", b) for b in win.seg_tab._group.buttons()]

    line = []
    for name, wdg in widgets:
        if wdg is None or not wdg.isVisible():
            line.append(f"{name}=隐藏")
            continue
        n, h = need_w(wdg), have_w(wdg)
        if h < n:
            bad_text += 1
            line.append(f"!!{name} 需{n}有{h}")
        else:
            line.append(f"{name} {h - n:+d}px余")
    status = "OK" if not any("!!" in x for x in line) else "!!"
    print(f"  {wd:5d}px  {status}")
    print(f"         {' | '.join(line)}")
    win.close()

print()
print("=" * 74)
print("三、顶栏总高度与内容区可用高度")
print("=" * 74)
win = MainWindow()
win.resize(1360, 860)
win.show()
settle()
h_bar = win._titlebar.height()
h_stack = win.stack.height()
h_status = win.status.height()
print(f"  顶栏 {h_bar}px + 内容 {h_stack}px + 状态栏 {h_status}px "
      f"= {h_bar + h_stack + h_status}px")
print(f"  旧版：顶栏 46 + 分页条 46 = 92px；新版顶栏 {h_bar}px，"
      f"净省 {92 - h_bar}px")
win.close()

print()
print("=" * 74)
if bad_center == 0 and bad_text == 0:
    print("结论：Tab 全部居中（偏差 <= 2px），顶栏文字全部完整")
else:
    print(f"结论：居中偏移 {bad_center} 档，文字截断 {bad_text} 处")
sys.exit(0 if bad_center == 0 and bad_text == 0 else 1)
