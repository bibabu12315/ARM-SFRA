"""条纹取证 v2：让 Qt 自己报告绘图区在截图中的像素矩形，
只在该矩形内部做条纹检测，避免把面板背景色误判成填充。

用法：
    .venv\\Scripts\\python.exe tools\\stripe_capture2.py
"""
from __future__ import annotations

import csv
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

OUT = os.path.join(_ROOT, "tools", "_stripe_shots2")
CSV = os.path.join(_ROOT, "tools", "_v1_out", "v1_120319.csv")


def load_curve():
    freq, mag, phase = [], [], []
    with open(CSV, encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            freq.append(float(row["Freq(Hz)"]))
            mag.append(float(row["Mag(dB)"]))
            phase.append(float(row["Phase(deg)"]))
    return (np.array(freq), np.array(mag), np.array(phase))


def wheel(glw, gpos, delta, n=1):
    for _ in range(n):
        ev = QtGui.QWheelEvent(
            QtCore.QPointF(gpos), QtCore.QPointF(glw.mapToGlobal(gpos)),
            QtCore.QPoint(0, 0), QtCore.QPoint(0, delta),
            QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
            QtCore.Qt.NoScrollPhase, False)
        QtWidgets.QApplication.sendEvent(glw, ev)
        QtWidgets.QApplication.processEvents()


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)

    from app import theme
    from app.main_window import MainWindow
    from app.ui.widgets.bode_plot import Curve

    f = QtGui.QFont()
    f.setFamily("Microsoft YaHei UI")
    f.setPointSizeF(9.0)
    app.setFont(f)
    app.setStyleSheet(theme.qss())
    app.setStyle("Fusion")

    w = MainWindow()
    w.resize(1360, 860)
    w.show()
    for _ in range(8):
        app.processEvents()

    freq, mag, phase = load_curve()
    plot = w.tab_measure.plot
    plot.set_curves([Curve(freq=freq, mag=mag, phase=phase, name="real")])
    for _ in range(10):
        app.processEvents()

    # ---- 取绘图区在 glw 中的像素矩形 ----
    def vb_rect_px(viewbox):
        """ViewBox 的绘制区（不含坐标轴）在 glw 坐标系下的矩形。"""
        r = viewbox.sceneBoundingRect()
        tl = viewbox.scene().itemsBoundingRect()   # noqa: F841
        p_tl = plot.glw.mapFromScene(r.topLeft())
        p_br = plot.glw.mapFromScene(r.bottomRight())
        return QtCore.QRect(p_tl, p_br).normalized()

    info = {}
    pix = plot.glw.grab()
    dpr = pix.width() / max(1, plot.glw.width())
    print(f"glw={plot.glw.width()}x{plot.glw.height()}  "
          f"grab={pix.width()}x{pix.height()}  dpr={dpr}")

    for name, vb in (("mag", plot.pm.vb), ("phase", plot.pp.vb)):
        r = vb_rect_px(vb)
        info[name] = r
        print(f"  {name} viewport px (glw coords) = {r}")

    def shot(tag):
        app.processEvents()
        p = os.path.join(OUT, f"{tag}.png")
        plot.glw.grab().save(p)
        return p

    def analyse(path, tag):
        from PIL import Image
        im = Image.open(path).convert("RGB")
        a = np.asarray(im).astype(np.int16)
        for name, r in info.items():
            # glw 坐标 -> 截图像素坐标（DPR 缩放）
            x0 = int(r.x() * dpr) + 2
            x1 = int((r.x() + r.width()) * dpr) - 2
            y0 = int(r.y() * dpr) + 2
            y1 = int((r.y() + r.height()) * dpr) - 2
            if x1 <= x0 or y1 <= y0:
                continue
            sub = a[y0:y1, x0:x1]
            # 相对纯白与相对淡蓝填充的判断
            nonwhite = np.abs(sub - 255).max(axis=2) > 6
            # 淡蓝填充近似色 (239,246,253)，允许抗锯齿过渡
            bl = (np.abs(sub[:, :, 2] - 253) < 12) & \
                 (sub[:, :, 0] < 245) & (sub[:, :, 2] - sub[:, :, 0] > 6)
            ch, cw = nonwhite.shape
            # 贯穿判定：某列/行在 60% 以上高度/宽度上被"着色"
            colf = nonwhite.sum(axis=0) / ch
            rowf = nonwhite.sum(axis=1) / cw
            v = np.where(colf > 0.60)[0]
            h = np.where(rowf > 0.60)[0]
            print(f"    [{tag}/{name}] crop={cw}x{ch} "
                  f"fill%={bl.mean()*100:5.1f} "
                  f"col_full={v.size} row_full={h.size}")
            if v.size or h.size:
                print(f"        cols={v[:20]} rows={h[:20]}")

    p = shot("00_initial")
    print("saved", p)
    analyse(p, "init")

    centre = plot.pm.vb.sceneBoundingRect().center()
    gpos = plot.glw.mapFromScene(centre)
    for i, d in enumerate((120, 240, 480), start=1):
        wheel(plot.glw, gpos, d, n=3)
        p = shot(f"1{i}_in")
        analyse(p, f"in{i}")

    for i, d in enumerate((-120, -240), start=1):
        wheel(plot.glw, gpos, d, n=3)
        p = shot(f"2{i}_out")
        analyse(p, f"out{i}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
