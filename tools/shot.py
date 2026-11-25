"""临时脚本：离屏渲染主窗口截图，用于检查 UI 效果。

用法：.venv\\Scripts\\python.exe tools\\shot.py [输出目录]
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5 import QtCore, QtGui, QtWidgets

from app import theme
from app.main_window import MainWindow


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "design", "shots")
    os.makedirs(out, exist_ok=True)

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss())
    app.setStyle("Fusion")

    w = MainWindow()
    w.resize(1360, 880)
    w.show()
    app.processEvents()

    def grab(name):
        app.processEvents()
        pix = w.grab()
        p = os.path.join(out, name)
        pix.save(p)
        print("saved", p, pix.width(), "x", pix.height())

    grab("tab1_empty.png")

    # 跑一次快扫，截有曲线的状态
    tm = w.tab_measure
    tm.in_f0.set_value(100)
    tm.in_f1.set_value(20000)
    tm.in_ppd.set_value(20)
    tm.seg_cyc.set_current(1)
    tm.in_amp.set_value(2.0)
    tm._recalc_estimate()

    done = {"v": False}
    w.ctrl.sigDone.connect(lambda ok, m: done.__setitem__("v", True))
    tm._on_run()

    import time
    t0 = time.time()
    while not done["v"] and time.time() - t0 < 40:
        app.processEvents()
        QtCore.QThread.msleep(5)

    app.processEvents()
    grab("tab1_done.png")

    # 切补偿器页
    w.seg_tab.set_current(1, emit=True)
    app.processEvents()
    grab("tab2.png")

    w.close()


if __name__ == "__main__":
    main()
