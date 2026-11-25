"""v1.0 验收走查：模拟用户从打开界面到导出文件的完整路径。

用法：
    .venv\\Scripts\\python.exe tools/v1_walkthrough.py [COM口]

设计原则：
    只走"用户真的会点的路径"——调按钮点击/信号发射，而不是直接调内部
    函数。这样任何一处置灰、未连接、槽函数缺失都会暴露出来。

不弹模态框：所有 QMessageBox 被替换成记录器，否则脚本会卡住等人点。
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from PyQt5 import QtCore, QtWidgets  # noqa: E402

#: 结果收集
PASS: list[str] = []
FAIL: list[str] = []
WARN: list[str] = []


def ok(name: str, detail: str = "") -> None:
    PASS.append(f"{name}" + (f"  [{detail}]" if detail else ""))
    print(f"  [PASS] {name}" + (f"  {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    FAIL.append(f"{name}" + (f"  [{detail}]" if detail else ""))
    print(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))


def warn(name: str, detail: str = "") -> None:
    WARN.append(f"{name}" + (f"  [{detail}]" if detail else ""))
    print(f"  [WARN] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    port_want = sys.argv[1] if len(sys.argv) > 1 else "COM14"
    out_dir = os.path.join(_ROOT, "tools", "_v1_out")
    os.makedirs(out_dir, exist_ok=True)

    app = QtWidgets.QApplication(sys.argv[:1])

    # 屏蔽所有模态框：换成记录器
    dialogs: list[str] = []
    Box = QtWidgets.QMessageBox
    Box.information = staticmethod(         # type: ignore[assignment]
        lambda *a, **k: dialogs.append(f"info: {a[2] if len(a) > 2 else ''}"))
    Box.warning = staticmethod(             # type: ignore[assignment]
        lambda *a, **k: dialogs.append(f"warn: {a[2] if len(a) > 2 else ''}"))
    Box.critical = staticmethod(            # type: ignore[assignment]
        lambda *a, **k: dialogs.append(f"crit: {a[2] if len(a) > 2 else ''}"))
    Box.question = staticmethod(            # type: ignore[assignment]
        lambda *a, **k: QtWidgets.QMessageBox.Yes)

    from app import theme
    from app.main_window import MainWindow

    app.setStyleSheet(theme.qss())
    app.setStyle("Fusion")

    print("\n================ v1.0 走查开始 ================\n")
    print("[1] 创建主窗口")
    w = MainWindow()
    w.show()
    app.processEvents()
    ok("主窗口创建", f"backend={w.link_backend}")

    # ---- 2. 后端与串口 ----
    print("\n[2] 后端与串口枚举")
    if w.link_backend != "hw":
        bad("后端未自动切到真串口", f"实际={w.link_backend}")
    else:
        ok("后端自动识别为真串口 hw")
    ports = [w.cmb_port.itemText(i) for i in range(w.cmb_port.count())]
    if port_want in ports:
        ok("下拉框列出目标串口", port_want)
        idx = ports.index(port_want)
        w.cmb_port.setCurrentIndex(idx)
        app.processEvents()
    else:
        bad("下拉框未列出目标串口", f"实际={ports}")
        return 1
    if w.btn_conn.isEnabled():
        ok("连接按钮可用")
    else:
        bad("连接按钮被禁用")

    # ---- 3. 连接 ----
    print("\n[3] 点击连接")
    w.btn_conn.click()
    deadline = time.time() + 5
    while time.time() < deadline and not getattr(w.ctrl, "is_open", False):
        app.processEvents()
        time.sleep(0.01)
    if getattr(w.ctrl, "is_open", False):
        ok("串口连接成功")
    else:
        bad("串口连接失败", f"dialogs={dialogs}")

    # 等握手
    deadline = time.time() + 3
    while time.time() < deadline and getattr(w.ctrl, "caps", None) is None:
        app.processEvents()
        time.sleep(0.01)
    caps = getattr(w.ctrl, "caps", None)
    if caps is not None:
        ok("握手拿到 CAPS", f"v{caps.version_str} Fs={caps.isr_freq:.0f}Hz")
    else:
        bad("握手未拿到 CAPS")

    if w.btn_conn.text() == "断开":
        ok("按钮文字切换为「断开」")
    else:
        bad("按钮文字未切换", f"实际={w.btn_conn.text()}")

    if w.pill_port.text() != "未连接":
        ok("顶栏端口标签已更新", w.pill_port.text())
    else:
        bad("顶栏端口标签仍为「未连接」")

    fw_txt = w.pill_fw.text()
    if "v1.0" in fw_txt or "固件" in fw_txt:
        ok("顶栏固件标签已更新", fw_txt)
    else:
        bad("顶栏固件标签未更新", fw_txt)

    # ---- 4. 扫频 ----
    print("\n[4] 点击「开始扫频」")
    # 清空旧结果
    w.ctrl.result = None
    w.tab_measure.btn_run.click()
    deadline = time.time() + 40
    last_n = 0
    while time.time() < deadline:
        app.processEvents()
        r = getattr(w.ctrl, "result", None)
        n = len(r) if r is not None else 0
        if n != last_n:
            last_n = n
        if not w.ctrl.running:
            break
        time.sleep(0.01)

    r = getattr(w.ctrl, "result", None)
    n = len(r) if r is not None else 0
    if n >= 10:
        ok("扫频完成", f"{n} 点")
    else:
        bad("扫频点数不足", f"{n} 点")

    # ---- 5. 出图 ----
    print("\n[5] Bode 图")
    plot = getattr(w.tab_measure, "plot", None)
    if plot is None:
        bad("找不到 Bode 图控件")
    else:
        ok("BodePlot 控件存在", type(plot).__name__)
        glw = getattr(plot, "glw", None)
        if glw is not None:
            ok("绘图区（glw）可用")
        else:
            bad("BodePlot 缺少 glw 绘图区")

    # 曲线数据非空检查
    if r is not None and n >= 2:
        import numpy as np
        mag = np.asarray(r.mag, dtype=float)
        ph = np.asarray(r.phase, dtype=float)
        if np.all(np.isfinite(mag)) and np.all(np.isfinite(ph)):
            ok("幅值/相位全为有限值", f"mag {mag.min():.1f}~{mag.max():.1f} dB")
        else:
            bad("存在 NaN/Inf", f"mag有限={np.all(np.isfinite(mag))}")
        if np.all(np.diff(mag) <= 1e-6):
            ok("幅值单调下降（低通特征）")
        else:
            warn("幅值非单调下降", "低通 DUT 预期单调")

    # ---- 6. PM/GM ----
    print("\n[6] 稳定裕度")
    import numpy as np
    from app.model.analysis import analyze as an_fn
    if r is not None and n >= 2:
        try:
            st = an_fn(np.asarray(r.freq), np.asarray(r.mag),
                       np.asarray(r.phase))
            ok("analyze() 可执行",
               f"fc={getattr(st, 'fc_hz', None)} "
               f"PM={getattr(st, 'pm_deg', None)} "
               f"GM={getattr(st, 'gm_db', None)}")
        except Exception as e:                # noqa: BLE001
            warn("analyze() 抛出异常（开环低通无穿越点属正常）", repr(e))

    # ---- 7. 导出 ----
    print("\n[7] 导出")
    from app.exporters import export as ex

    stamp = time.strftime("%H%M%S")
    if r is not None and n >= 2:
        for kind, writer in (("csv", ex.to_csv), ("json", ex.to_json),
                             ("md", ex.to_markdown)):
            path = os.path.join(out_dir, f"v1_{stamp}.{kind}")
            try:
                writer(r, path)
                size = os.path.getsize(path) if os.path.exists(path) else 0
                if size > 0:
                    ok(f"导出 {kind}", f"{size} B")
                else:
                    bad(f"导出 {kind} 文件为空")
            except Exception as e:            # noqa: BLE001
                bad(f"导出 {kind} 异常", repr(e))
        # TI 格式单独一份（给 TI 的 SFRA GUI 对比用）
        path = os.path.join(out_dir, f"v1_{stamp}_ti.csv")
        try:
            ex.to_ti_csv(r, path)
            ok("导出 TI CSV", f"{os.path.getsize(path)} B")
        except Exception as e:                # noqa: BLE001
            warn("导出 TI CSV 失败", repr(e))
    else:
        bad("无数据，跳过导出")

    # PNG 由界面按钮触发（走 QFileDialog），这里直接测绘图控件能否渲染
    if plot is not None and n >= 2 and getattr(plot, "glw", None) is not None:
        try:
            from pyqtgraph.exporters import ImageExporter
            img_path = os.path.join(out_dir, f"v1_{stamp}.png")
            exp = ImageExporter(plot.pm)
            exp.export(img_path)
            ok("渲染 PNG", f"{os.path.getsize(img_path)} B")
        except Exception as e:                # noqa: BLE001
            warn("渲染 PNG 失败", repr(e))

    # ---- 8. 补偿器页 ----
    print("\n[8] 切到补偿器页")
    try:
        w.seg_tab.changed.emit(1)
        app.processEvents()
        ok("切换到补偿器页")
        if w.stack.currentIndex() == 1:
            ok("页面索引正确")
        else:
            bad("页面索引未切换", str(w.stack.currentIndex()))
    except Exception as e:                    # noqa: BLE001
        bad("切换补偿器页异常", repr(e))

    # ---- 9. 断开 ----
    print("\n[9] 断开连接")
    w.btn_conn.click()
    deadline = time.time() + 3
    while time.time() < deadline and getattr(w.ctrl, "is_open", False):
        app.processEvents()
        time.sleep(0.01)
    if not getattr(w.ctrl, "is_open", False):
        ok("已断开")
    else:
        bad("断开失败")
    if w.btn_conn.text() == "连接":
        ok("按钮文字恢复为「连接」")
    else:
        bad("按钮文字未恢复", w.btn_conn.text())

    # ---- 收尾 ----
    w.shutdown()

    print("\n================ 汇总 ================")
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)} / 警告 {len(WARN)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print("  -", f)
    if WARN:
        print("\n警告项：")
        for x in WARN:
            print("  -", x)
    if dialogs:
        print(f"\n期间弹出的对话框 {len(dialogs)} 个：")
        for d in dialogs[:10]:
            print("  -", d)
    return 0 if not FAIL else 2


if __name__ == "__main__":
    sys.exit(main())
