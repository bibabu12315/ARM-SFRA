"""用真机导出的 CSV 数据精确复现条纹问题。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

os.environ["SFRA_BACKEND"] = "sim"

import numpy as np
from PyQt5 import QtCore, QtWidgets

from app import theme
from app.main_window import MainWindow
from app.ui.widgets.bode_plot import Curve

OUT = os.path.join(_ROOT, "tools", "_zoom_out")
CSV = os.path.join(_ROOT, "tools", "_v1_out", "v1_120319.csv")


def main():
    # 读真机数据
    raw = np.genfromtxt(CSV, delimiter=",", names=True,
                        dtype=None, encoding="utf-8-sig")
    f = raw["FreqHz"].astype(float)
    mag = raw["MagdB"].astype(float)
    ph = raw["Phasedeg"].astype(float)
    print(f"loaded {len(f)} pts, mag {mag.min():.1f}~{mag.max():.1f} dB")

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss())
    app.setStyle("Fusion")

    w = MainWindow()
    w.resize(1360, 880)
    w.show()
    app.processEvents()
    plot = w.tab_measure.plot

    plot.set_curves([Curve(freq=f, mag=mag, phase=ph, name="real",
                           color=theme.BLUE)])
    app.processEvents()
    w.grab().save(os.path.join(OUT, "R1_real.png")); print("saved R1")

    vb = plot.pm.vb
    c = vb.viewRect().center()
    vb.scaleBy((1 / 3.0, 1 / 3.0), center=c)
    app.processEvents()
    w.grab().save(os.path.join(OUT, "R2_real_zoomed.png")); print("saved R2")

    vb.scaleBy((1 / 3.0, 1 / 3.0), center=c)
    app.processEvents()
    w.grab().save(os.path.join(OUT, "R3_real_zoom2.png")); print("saved R3")


if __name__ == "__main__":
    main()
