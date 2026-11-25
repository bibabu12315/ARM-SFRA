"""pytest 全局夹具：**在导入任何测试模块之前**建好唯一的一个 QApplication。

为什么非要有这个文件（踩过的坑，别删）：
    三个测试模块原先各自决定用哪个 Qt 应用对象：
        test_link_controller.py  → `QCoreApplication`
        test_e2e_loop.py         → `QApplication`（在 fixture 里）
        test_coeff_link.py       → `QApplication`（也在模块级建过一次）
    Qt 里 `QCoreApplication` 和 `QApplication` **共用同一个单例槽位**：
    先建了 QCoreApplication，再建 QApplication 是未定义行为。
    Windows 上的实际表现是进程直接 `Fatal Python error: Aborted`，
    没有 Python 栈（崩在 Qt 的 C++ 里），只看到 `exit code 3`，
    极难定位到"是哪个测试建的 app"。

    统一在这里建 **QApplication**（它是 QCoreApplication 的子类，
    纯逻辑测试用它完全没问题），所有测试模块的
    `QApplication.instance()` 都能取到同一个实例，不再各自为政。

`QT_QPA_PLATFORM=offscreen` 请由外部环境变量提供（CI 与本地一致），
这里不强行设置——万一有人要真的弹窗口调试，别被悄悄改成离屏。
"""
from __future__ import annotations

import pytest

PyQt5 = pytest.importorskip("PyQt5")          # 没有 PyQt5 时整包跳过
from PyQt5 import QtWidgets                    # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _qt_application():
    """会话级唯一 QApplication。

    autouse = 不需要任何测试显式索取，保证**第一个测试开始前**就已建好。
    """
    inst = QtWidgets.QApplication.instance()
    if inst is None:
        inst = QtWidgets.QApplication([])
    yield inst
    # 不主动 quit/del：pytest 进程结束时 Qt 自行收尾。
    # 手动删除反而会让后续模块拿到悬空指针。
