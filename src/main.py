"""SFRA Studio - 启动入口。

用法：
    .venv\\Scripts\\python.exe src\\main.py

依赖安装在 .venv 里（不要用系统 PATH 的 python，可能没有 PyQt5）。
"""
from __future__ import annotations

import os
import sys
import traceback

# 允许 `python src/main.py` 直接跑（把 src 加入 sys.path）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 高分屏：在创建 QApplication 之前设置
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from app import theme                        # noqa: E402
from app.main_window import MainWindow        # noqa: E402

#: 崩溃日志。放工程根目录，用户直接能看到，不必翻临时目录。
LOG_PATH = os.path.join(os.path.dirname(_ROOT), "sfra_crash.log")


def _install_excepthook() -> None:
    """把"闪退"变成"看得见的报错"。

    为什么必须有：PyQt5 里槽函数抛异常时，PyQt 默认只把 traceback 打到
    stderr 就继续（Python 3.8+ 的行为）。但从 .bat 双击启动时没有控制台，
    stderr 无处可去，用户看到的就是"点一下、窗口没了"——完全无从排查。
    装了这个钩子之后，任何未捕获异常都会写进 sfra_crash.log 并弹窗提示。
    """

    def _hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        sys.stderr.write(text)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fp:
                fp.write("\n" + "=" * 70 + "\n")
                fp.write(text)
        except OSError:
            pass
        # 弹窗不能指望 QApplication 还活着，所以只在还能用时弹
        try:
            if QtWidgets.QApplication.instance() is not None:
                QtWidgets.QMessageBox.critical(
                    None, "SFRA Studio 内部错误",
                    f"{exc_type.__name__}: {exc}\n\n"
                    f"详细信息已写入：\n{LOG_PATH}")
        except Exception:                     # noqa: BLE001
            pass

    sys.excepthook = _hook


def _center_on_primary(w) -> None:
    """把窗口摆到主屏可用区域的中间。

    为什么必须手动做：主屏是 4K（3840x2160）+ 高 DPI 缩放时，Qt 会把
    availableGeometry() 报成缩放后的逻辑尺寸（实测 1920x1044），而
    show() 里 Windows 又按物理像素去摆放窗口，两套坐标系混用，结果
    窗口被丢到 QRect(-238, -1080, ...)——整个窗口在主屏上方一屏的高度，
    任何显示器上都看不到。表现出来就是"双击后窗口闪一下就没了"：
    进程活着、isVisible() 为 True、退出码 0，但屏幕上什么都没有。

    这不是崩溃，所以 sfra_crash.log 永远是空的——这也是之前一直查
    不出原因的地方。

    做法：show() 之后显式 move 到主屏可用区域的中心，并夹紧到区域内，
    保证左上角一定落在屏幕里。
    """
    scr = QtWidgets.QApplication.primaryScreen()
    if scr is None:
        return
    avail = scr.availableGeometry()
    fg = w.frameGeometry()
    fw = fg.width() or w.width()
    fh = fg.height() or w.height()

    x = avail.x() + max(0, (avail.width() - fw) // 2)
    y = avail.y() + max(0, (avail.height() - fh) // 2)

    # 夹紧：窗口比屏幕大时贴左上角，而不是算出负坐标。
    x = max(avail.x(), min(x, avail.x() + avail.width() - 1))
    y = max(avail.y(), min(y, avail.y() + avail.height() - 1))
    w.move(x, y)
    w.resize(min(w.width(), avail.width()), min(w.height(), avail.height()))

    # 自检：夹紧之后仍然不在屏幕上，说明坐标系假设又错了。
    # 写进日志，免得下次再靠肉眼猜"窗口去哪了"。
    try:
        if not avail.intersects(w.frameGeometry()):
            with open(LOG_PATH, "a", encoding="utf-8") as fp:
                fp.write(
                    "\n[launcher] 窗口不在任何屏幕内 "
                    f"avail={avail.x()},{avail.y()},{avail.width()},"
                    f"{avail.height()} "
                    f"frame={w.frameGeometry().x()},{w.frameGeometry().y()},"
                    f"{w.frameGeometry().width()},"
                    f"{w.frameGeometry().height()}\n")
    except OSError:
        pass


def main() -> int:
    _install_excepthook()

    # Windows 任务栏图标分组
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SFRA.Studio.1")
        except Exception:
            pass

    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("SFRA Studio")
    app.setOrganizationName("SFRA")

    # 字体：优先用系统自带的中文无衬线，数字用等宽
    f = QtGui.QFont()
    for cand in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Segoe UI"):
        f.setFamily(cand)
        if QtGui.QFontInfo(f).family() == cand:
            break
    f.setPointSizeF(9.0)
    app.setFont(f)

    app.setStyleSheet(theme.qss())
    app.setStyle("Fusion")

    w = MainWindow()
    w.show()

    # 摆到主屏可用区域内。必须在 show() 之后——show() 之前窗口还没有
    # 原生句柄，move() 会被 Windows 的初始摆放覆盖掉。
    _center_on_primary(w)

    # 把窗口强制拉到前台一次。
    #
    # 为什么需要：从 .bat 启动时（尤其是换成 pythonw.exe 之后），进程不是
    # 用户"亲自"点出来的，Windows 的前台锁（foreground lock）会让新窗口
    # 只闪一下就退回任务栏——表现就是"点任务栏图标，窗口弹出又收回去"。
    # raise_() + activateWindow() 让本进程成为前台；再配合下面的
    # AllowSetForegroundWindow，可以绕开这个限制。
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
        except Exception:
            pass
    w.raise_()
    w.activateWindow()

    rc = app.exec_()

    # 退出前把窗口与后端子进程收拾干净。假 MCU 的 QProcess 如果还活着，
    # Python 解释器退出时可能留下孤儿进程，或者卡在 COM 清理上。
    try:
        w.shutdown()
    except Exception:                         # noqa: BLE001
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
