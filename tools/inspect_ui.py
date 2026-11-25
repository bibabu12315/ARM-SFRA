"""临时脚本：把截图的分析结果打成文字，便于无视觉能力时判断布局。

检查项：
  - 窗口尺寸、各栏宽度是否符合设计（左 300 / 右 268）
  - 关键控件是否可见、尺寸是否合理
  - 曲线是否真的画出来了（点数、范围）
  - 是否有控件超出父容器（重叠隐患）
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt5 import QtCore, QtWidgets
from app import theme
from app.main_window import MainWindow


def geom(w, name):
    g = w.geometry()
    return f"{name}: x={g.x()} y={g.y()} w={g.width()} h={g.height()} vis={w.isVisible()}"


def main():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss())
    app.setStyle("Fusion")

    w = MainWindow()
    w.resize(1360, 880)
    w.show()
    app.processEvents()

    tm = w.tab_measure
    print("=== 窗口 ===")
    print("size", w.size())
    print(geom(tm, "TabMeasure"))
    print(geom(tm.plot, "BodePlot"))
    print(geom(tm.btn_run, "开始扫频"))
    print(geom(tm.bar, "进度条"))
    print("左栏宽", tm.findChild(QtWidgets.QWidget, "ColLeft").width())
    print("右栏宽", tm.findChild(QtWidgets.QWidget, "ColRight").width())

    # 跑一次快扫
    tm.in_f0.set_value(100)
    tm.in_f1.set_value(20000)
    tm.in_ppd.set_value(20)
    tm.seg_cyc.set_current(1)
    tm.in_amp.set_value(2.0)
    tm._recalc_estimate()
    print("\n预计点数", tm.lb_npts.text(), "耗时", tm.lb_etime.text())

    done = {"v": False, "ok": False}
    w.ctrl.sigDone.connect(
        lambda ok, m: (done.__setitem__("v", True), done.__setitem__("ok", ok)))
    tm._on_run()

    import time
    t0 = time.time()
    while not done["v"] and time.time() - t0 < 60:
        app.processEvents()
        QtCore.QThread.msleep(3)
    app.processEvents()

    print("\n=== 扫频结果 ===")
    print("完成", done["ok"], f"耗时 {time.time()-t0:.1f}s")
    res = tm.current_result()
    print("点数", len(res) if res else 0)
    if res:
        import numpy as np
        print("频率范围", f"{res.freq[0]:.1f} ~ {res.freq[-1]:.1f} Hz")
        print("幅值范围", f"{np.nanmin(res.mag):.1f} ~ {np.nanmax(res.mag):.1f} dB")
        print("相位范围", f"{np.nanmin(res.phase):.1f} ~ {np.nanmax(res.phase):.1f} deg")
    print("PM", tm.v_pm.num.text(), "GM", tm.v_gm.num.text(),
          "fc", tm.v_fc.num.text())
    print("chip", tm.chip_pm.text(), "/", tm.chip_gm.text())
    print("图区曲线数", len(tm.plot._curves))
    print("图 X 范围", tm.plot.pm.vb.viewRange()[0])
    print("图 Y 范围 mag", tm.plot.pm.vb.viewRange()[1])
    print("图 Y 范围 ph", tm.plot.pp.vb.viewRange()[1])
    print("告警条数", len(tm._note_items))
    for n in tm._note_items[:3]:
        print("  -", n._txt.text())

    # Tab2
    w.seg_tab.set_current(1, emit=True)
    app.processEvents()
    tc = w.tab_comp
    print("\n=== 补偿器页 ===")
    print(geom(tc.plot_g, "G 图"), "曲线", len(tc.plot_g._curves))
    print(geom(tc.plot_gh, "GH 图"), "曲线", len(tc.plot_gh._curves))
    print("预测 PM", tc._box_pm[1].num.text(),
          "fc", tc._box_fc[1].num.text(),
          "差值", tc._box_d[1].num.text())
    print("等效系数", [f"{c:.4f}" for c in tc.coefficients()])

    # 改 Kp 看是否实时变（必须 emit=True，否则不会触发重算）
    before = tc._box_pm[1].num.text()
    tc.in_kp.set_value(4.0, emit=True)
    app.processEvents()
    print(f"Kp 0.5→4.0：预测 PM {before} → {tc._box_pm[1].num.text()}")

    # 截个图
    out = os.path.join(_ROOT, "design", "shots")
    os.makedirs(out, exist_ok=True)
    w.seg_tab.set_current(0, emit=True)
    app.processEvents()
    w.grab().save(os.path.join(out, "tab1_done.png"))
    w.seg_tab.set_current(1, emit=True)
    app.processEvents()
    w.grab().save(os.path.join(out, "tab2.png"))
    print("\n截图已保存到", out)

    w.close()


if __name__ == "__main__":
    main()
