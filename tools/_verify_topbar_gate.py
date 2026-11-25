"""判据有效性自检 —— 验证"Tab 居中"判据不是摆设。

## 为什么需要它

`src/tests/test_ui_topbar.py` 断言「Tab 中线 = 窗口中线 ±2px」。
但如果这个判据永远通过，它就没有价值。本脚本人为制造两种
布局退化，要求判据**必须报错**：

  D1 左右 columnStretch 不等（左 3 右 1）—— 模拟"弹簧容量不均"
     这是最真实的退化形态：有人觉得"左边内容少，多给点弹簧吧"，
     一改就把 Tab 顶走。实测偏差 72~436px。
  D2 中格也给 stretch=1 —— 模拟"把中格当弹簧用"
     这种写法在宽窗口下令 Tab **自身撑宽**（geometry 变宽但中线
     仍在中线上），所以宽窗口偏 0.5px 属于正常；
     窄窗口（1120px）才会暴露，偏 30.5px。

所以"能抓到"的判据是 D1 或 D2 任一被抓到，且基线必须全通过。

## 与其它脚本的分工

  _verify_topbar_gate.py  本文件：居中判据有没有判别力
  _verify_topbar.py       顶栏实测：逐档窗口宽度下的真实偏差 + 文字可见性
  test_ui_topbar.py       pytest 守门（同一套判据）
  _verify_gate.py         "文字可见性"判据的判别力（另一条红线）
"""
import os, sys
os.environ["SFRA_BACKEND"]="sim"
sys.path.insert(0,"src")
from PyQt5 import QtCore, QtWidgets
from app import theme
app=QtWidgets.QApplication([]); app.setStyleSheet(theme.qss())
from app.main_window import MainWindow

def make(w, stretch=(1,0,1)):
    win=MainWindow()
    win.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    win.resize(w,860); win.show()
    g=win._titlebar.layout()
    for i,s in enumerate(stretch): g.setColumnStretch(i,s)
    for _ in range(14): app.processEvents()
    return win

def off(win):
    g=win.seg_tab.geometry()
    return (g.x()+g.width()/2.0)-win._titlebar.width()/2.0

print("="*64)
print("[基线] stretch=(1,0,1)")
base=[]
for w in (1120,1360,1920):
    win=make(w); o=off(win); base.append(abs(o)>2)
    print(f"  {w:5d}px  偏差 {o:+7.2f}px  {'截断' if abs(o)>2 else 'OK'}")
    win.close()

print()
print("[D1] stretch=(3,0,1)  左右不均")
d1=[]
for w in (1120,1360,1920):
    win=make(w,(3,0,1)); o=off(win); d1.append(abs(o)>2)
    print(f"  {w:5d}px  偏差 {o:+7.2f}px  {'抓到' if abs(o)>2 else '漏检'}")
    win.close()

print()
print("[D2] stretch=(1,1,1)  中格也当弹簧")
d2=[]
for w in (1120,1360,1920):
    win=make(w,(1,1,1)); o=off(win); d2.append(abs(o)>2)
    print(f"  {w:5d}px  偏差 {o:+7.2f}px  {'抓到' if abs(o)>2 else '漏检'}")
    win.close()

print()
print("="*64)
print(f"基线全通过: {not any(base)}")
print(f"D1 被抓到:  {any(d1)}")
print(f"D2 被抓到:  {any(d2)}")
ok = (not any(base)) and (any(d1) or any(d2))
print("结论：" + ("判据有效，布局退化能被抓到" if ok else "!! 判据无判别力"))
sys.exit(0 if ok else 1)
