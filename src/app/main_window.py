"""SFRA Studio - 主窗口。

结构（照设计稿）：
    顶栏：连接指示 + 标题 | 分页（测量/补偿器，居中） | 串口 pill + 固件/Fs pill
    内容：QStackedWidget

**顶栏为什么是三段一格**（v4.7 改动）：
原先是两行——第一行 LED/标题/串口，第二行只放一个居中的 Tab。
第二行除中间 220px 外全是空白，白扔掉 46px 高度。
合并成一行之后，用 QGridLayout 分左/中/右三格，
左右两格 stretch 相同、中间格 AlignCenter，这样 Tab 的**几何中线
始终落在窗口中线**，与左右两侧内容宽度无关。
"""
from __future__ import annotations

import os
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from . import theme
from .comm.controller import SerialLinkController, SimLinkController
from .comm.protocol import list_ports
from .exporters import export as ex
from .ui.tab_comp import TabComp
from .ui.tab_measure import TabMeasure
from .ui.widgets.common import SegmentedControl

#: 后端选择：环境变量 SFRA_BACKEND=sim|hw|py（**不设则自动判断**）
#  sim = 假 MCU 子进程，跑真 C 固件代码，走完整协议路径
#  hw  = 真串口
#  py  = 纯 Python 仿真，不经过协议层
#
#  不设时的自动规则（见 _make_backend）：能枚举到串口 → hw，
#  否则 → sim。原先默认写死 sim，导致插了 USB-TTL 也一直显示
#  "假 MCU（本机）"、下拉框里看不到任何端口，必须靠设环境变量
#  才能切到真串口——这个默认值是错的。
ENV_BACKEND = "SFRA_BACKEND"

#: 顶栏高度。46 → 52 是为了给 Tab 的 28px 胶囊留垂直余量。
#: 合并后净省 46×2 − 52 = 40px。
TITLEBAR_H = 52


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SFRA Studio")
        self.resize(1360, 860)
        self.setMinimumSize(1120, 720)

        # 真实链路后端（S4 起界面走这条路径）。
        # 想回到纯 Python 仿真的老路径（不经过协议层），设 SFRA_BACKEND=py。
        self.link_backend = "sim"
        self._py_ctrl = None
        self.ctrl = self._make_backend()

        self._build()
        self._wire_serial()
        self._refresh_ports()

    def _make_backend(self):
        want = os.environ.get(ENV_BACKEND, "").strip().lower()
        if want not in ("py", "hw", "sim"):
            # 自动判断：能枚举到串口就当有硬件，走真串口；
            # 一个口都没有才退回假 MCU。这样插上 USB-TTL 再启动，
            # 下拉框里立刻能看到端口，不必先想着去设环境变量。
            try:
                want = "hw" if list_ports() else "sim"
            except Exception:                 # noqa: BLE001
                want = "sim"
        self.link_backend = want
        if want == "py":
            from .sim.engine import SweepController
            self._py_ctrl = SweepController(self)
            self._py_ctrl.sigDone.connect(self._on_sweep_done)
            return self._py_ctrl
        if want == "hw":
            ctrl = SerialLinkController(self)
            ctrl.sigLog.connect(lambda s: self.status.showMessage(s, 8000))
            return ctrl
        ctrl = SimLinkController(self)
        ctrl.sigLog.connect(lambda s: self.status.showMessage(s, 8000))
        return ctrl

    # ================================================================ 构建
    def _build(self) -> None:
        root = QtWidgets.QWidget()
        root.setObjectName("Root")
        lay = QtWidgets.QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lay.addWidget(self._build_titlebar())

        self.stack = QtWidgets.QStackedWidget()
        self.tab_measure = TabMeasure(self.ctrl)
        # 真串口后端下"被测对象"整组没有意义（MCU 里才是被测对象，
        # PC 侧这几个字段不进任何帧），把它隐藏，免得用户以为必填。
        self.tab_measure.set_hardware_mode(self.link_backend == "hw")
        self.tab_comp = TabComp()
        self.stack.addWidget(self.tab_measure)
        self.stack.addWidget(self.tab_comp)
        self.seg_tab.changed.connect(self.stack.setCurrentIndex)
        lay.addWidget(self.stack, 1)

        # 底部状态栏
        self.status = QtWidgets.QStatusBar()
        self.status.setSizeGripEnabled(True)
        self.status.setStyleSheet(
            f"QStatusBar{{background:{theme.TOOLBAR};"
            f"border-top:1px solid {theme.LINE2};"
            f"color:{theme.T2}; font-size:11px;}}"
            f"QStatusBar::item{{border:0;}}")
        self.setStatusBar(self.status)
        self.status.showMessage(
            "就绪 · " + ("纯 Python 仿真（不经过协议层）"
                         if self.link_backend == "py"
                         else "假 MCU（真 C 固件 + 真协议，未连接）"
                         if self.link_backend == "sim" else "真串口（未连接）"))

        self.setCentralWidget(root)

        # ---- 连接 ----
        self.tab_measure.exportRequested.connect(self._on_export)
        self.tab_measure.statusMessage.connect(
            lambda s: self.status.showMessage(s, 8000))
        self.ctrl.sigDone.connect(self._on_sweep_done)

        # ---- 补偿器下发 / 读回（W1）----
        # 此前 TabComp.coeffChanged 从未被 connect，按钮按下后信号没人接，
        # 界面上"下发成功"和"什么都没发生"长得一模一样。
        self.tab_comp.coeffChanged.connect(self._on_coeff_apply)
        self.tab_comp.readbackRequested.connect(self._on_coeff_read)
        if hasattr(self.ctrl, "sigCoeff"):
            self.ctrl.sigCoeff.connect(self._on_coeff_received)

    def _build_titlebar(self) -> QtWidgets.QWidget:
        """顶栏：左（LED + 标题）| 中（Tab，居中）| 右（串口组）。

        **为什么用 QGridLayout 而不是 QHBoxLayout + 弹簧**

        目标：Tab 的几何中线永远落在窗口中线，且与左右内容宽度无关。
        QHBoxLayout 做不到——它的弹簧按"剩余空间"分配，而左区（标题）
        和右区（串口组）宽度差很多（88px vs 400+px），弹簧容量不同，
        Tab 会被推离中线。

        QGridLayout 的做法：三个格子各占 1 列，列 stretch 全是 0（不抢
        空间），然后**给整行左右各加一个 stretch=1 的空列**……
        不行，那样中格还是会随左右宽度漂移。

        真正的解法是**让左右两格的 stretch 相同**：
        左格 stretch=1、中格 stretch=0、右格 stretch=1。
        这样"左格宽 + 中格宽 + 右格宽 = 总宽"里，中格是固定的
        （sizeHint），剩余的宽度被左右两格**平分**——左右各拿到的
        富余量相等，于是中格的中线正好落在总宽的中线上。

        这个结论不依赖左右内容的自然宽度，所以窗口怎么缩放都成立。
        **代价**：左区内容多出来的部分会被推挤（比如串口组变宽时，
        右格需要的自然宽度超过平分量，就会挤左格）。窄窗口下先挤
        左边标题，而不是挤 Tab——这正是我们想要的优先级。

        注意：右格里用 QHBoxLayout 让控件从**右端**开始排（前置弹簧），
        这样串口组始终贴右边缘，与原来视觉一致。
        """
        bar = QtWidgets.QWidget()
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(TITLEBAR_H)

        grid = QtWidgets.QGridLayout(bar)
        # 左右边距沿用原值，垂直边距给 0（高度由 fixedHeight 定）
        grid.setContentsMargins(16, 0, 14, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(0)
        # 关键：中格 stretch=0（尺寸固定），左右各 1（平分富余）
        grid.setColumnStretch(0, 1)      # 左
        grid.setColumnStretch(1, 0)      # 中
        grid.setColumnStretch(2, 1)      # 右

        # ---------------- 左格：LED + 应用名 ----------------
        left = QtWidgets.QWidget()
        ll = QtWidgets.QHBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)

        self.led = QtWidgets.QFrame()
        self.led.setFixedSize(7, 7)
        self._set_led(False)
        ll.addWidget(self.led)

        name = QtWidgets.QLabel("SFRA Studio")
        name.setObjectName("AppName")
        ll.addWidget(name)
        ll.addStretch(1)                 # 左区内容靠左，富余留在右边
        grid.addWidget(left, 0, 0)

        # ---------------- 中格：Tab（测量 / 补偿器） ----------------
        # 用 AlignCenter 让它在中格内水平垂直双居中。
        # 中格宽度 = Tab 的 sizeHint（列 stretch=0），所以 AlignCenter
        # 等价于"中格本身已是最紧"，多余的富余都被左右两格吃掉了。
        self.seg_tab = SegmentedControl(["测量", "补偿器"], 0, pad=(8, 30))
        self.seg_tab.setMinimumWidth(self.seg_tab.minimumSizeHint().width())
        grid.addWidget(self.seg_tab, 0, 1, QtCore.Qt.AlignCenter)

        # ---------------- 右格：串口组（贴右） ----------------
        right = QtWidgets.QWidget()
        rl = QtWidgets.QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        rl.addStretch(1)                 # 前置弹簧：内容从右端开始排

        # 串口选择
        self.cmb_port = QtWidgets.QComboBox()
        self.cmb_port.setMinimumWidth(120)
        self.cmb_port.setToolTip(
            "选择串口（921600 8N1）。点开下拉框会自动重新枚举，"
            "插拔 USB-TTL 后不需要重启程序。")
        # 展开时重新枚举：插上/拔掉 USB-TTL 不必重启程序。
        # 用事件过滤器而不是覆写 showPopup——后者是 C++ 绑定的方法，
        # 在 PyQt5 里给实例赋值不一定生效，事件过滤器才是稳的。
        self.cmb_port.installEventFilter(self)
        rl.addWidget(self.cmb_port)

        self.btn_conn = QtWidgets.QPushButton("连接")
        self.btn_conn.setObjectName("Small")
        self.btn_conn.setFixedHeight(24)
        self.btn_conn.clicked.connect(self._toggle_conn)
        rl.addWidget(self.btn_conn)

        self.pill_port = QtWidgets.QLabel("未连接")
        self.pill_port.setObjectName("Pill")
        self._fix_pill_height(self.pill_port)
        rl.addWidget(self.pill_port)

        self.pill_fw = QtWidgets.QLabel("未握手")
        self.pill_fw.setObjectName("Pill")
        self.pill_fw.setToolTip("固件版本与真实 ISR 频率（握手后由 CAPS 帧给出）")
        self._fix_pill_height(self.pill_fw)
        rl.addWidget(self.pill_fw)

        grid.addWidget(right, 0, 2)

        # 空间不够时的降级由 _relayout_titlebar 处理（见 resizeEvent）
        self._titlebar = bar
        self._title_right = right
        return bar

    @staticmethod
    def _fix_pill_height(pill: QtWidgets.QLabel) -> None:
        """让胶囊保持自身上下居中，而不是被撑满顶栏整高。

        QLabel 默认纵向策略是 `Minimum`，意思是"最小 height 是 sizeHint，
        但可以变高"。放进 `QHBoxLayout` 且父容器有固定高度时，
        它就真的被拉伸到 52px —— 而 `#Pill` 的 QSS 是
        `border-radius: 11px` + `padding: 3px 10px`，21px 高才是胶囊，
        被拉成 52px 就变成一个竖长的圆角矩形，两端圆角全变形。

        改成 `Fixed`：高度锁在 `sizeHint`（21px），
        由布局自动上下居中（默认对齐就是垂直居中）。
        """
        pill.setSizePolicy(QtWidgets.QSizePolicy.Preferred,
                           QtWidgets.QSizePolicy.Fixed)

    def _relayout_titlebar(self) -> None:
        """窄窗口时先牺牲右侧次要 pill，保住 Tab 的中线位置。

        实测宽度需求（真机 + 固件握手完成，1120px 窗口下）：
            LED 7 + 间距 + 标题 88 + Tab 129 + 串口组(114+42+51+165+30)
            + 边距 30 ≈ 708px
        1120px（窗口最小宽）仍富余 412px，所以**正常情况下不需要降级**。

        那为什么还要这个函数？因为 pill 文案是动态的：
        `固件 v1.2.3 · Fs 100 kHz` 可能比估算更长，用户也可能改系统字号。
        留一条自动兜底，免得"文字挤出边界"再次出现。

        **踩过的坑**：第一版用 `layout().minimumSize().width()` 判是否放得
        下，结果 `pill_fw` 在 1920px 下也被永久隐藏——因为隐藏之后
        `minimumSize` 跟着变小，条件永远成立，形成**自我强化**：
        一旦隐藏就再也回不来。而且 `resizeEvent` 当场调用时布局还没跑完，
        拿到的是上一轮的尺寸。

        改法：按时**内容自然宽度之和**判断（与可见性无关），
        且用 `QTimer.singleShot(0)` 等布局跑完再判。
        """
        if not hasattr(self, "_titlebar"):
            return
        QtCore.QTimer.singleShot(0, self._apply_titlebar_fit)

    def _apply_titlebar_fit(self) -> None:
        """真正执行降级判断（事件循环下一轮，布局已定）。"""
        bar = getattr(self, "_titlebar", None)
        if bar is None:
            return
        avail = bar.width()
        if avail <= 0:
            return
        # 用自然宽度求和，**不用 minimumSize**（后者随可见性变化，
        # 会让隐藏状态自我强化，见 _relayout_titlebar 说明）。
        parts = [self.led.sizeHint().width()]
        parts.append(bar.findChild(QtWidgets.QLabel, "AppName")
                     .sizeHint().width())
        parts.append(self.seg_tab.minimumSizeHint().width())
        parts.append(self.cmb_port.minimumSizeHint().width())
        parts.append(self.btn_conn.sizeHint().width())
        parts.append(self.pill_port.sizeHint().width())
        # 间距：左格内部 1 个、中格两侧各 1 个、右格内部 3 个 = 6 个
        gaps = 6 * bar.layout().horizontalSpacing()
        margins = bar.layout().contentsMargins()
        base = sum(parts) + gaps + margins.left() + margins.right()
        need_full = base + self.pill_fw.sizeHint().width() + \
            bar.layout().horizontalSpacing()
        # 8px 余量给 DPI 取整
        self.pill_fw.setVisible(need_full + 8 <= avail)

    def resizeEvent(self, ev):                # noqa: N802
        super().resizeEvent(ev)
        self._relayout_titlebar()

    # ================================================================ 串口
    def eventFilter(self, obj, ev):
        """点开串口下拉框时重新枚举端口。

        这样插拔 USB-TTL 后不必重启程序——用户点一下下拉框就能看到新口。
        只在 hw 后端、且当前已连接时不动（连接中重新枚举会打断用户操作）。
        """
        if obj is self.cmb_port and ev.type() == QtCore.QEvent.MouseButtonPress:
            if self.link_backend == "hw" and not getattr(self.ctrl, "is_open", False):
                self._refresh_ports()
        return super().eventFilter(obj, ev)

    def _refresh_ports(self) -> None:
        # 记住当前选中的口，重建后尽量选回去（插拔别的设备时不要跳走）
        keep = self.cmb_port.currentText()
        self.cmb_port.clear()
        # 假 MCU 后端不需要串口：把下拉框换成一条说明，免得用户以为必须选口
        if self.link_backend == "sim":
            self.cmb_port.addItem("假 MCU（本机）")
            self.cmb_port.setEnabled(False)
            self.btn_conn.setEnabled(True)
            return
        if self.link_backend == "py":
            self.cmb_port.addItem("无（纯仿真）")
            self.cmb_port.setEnabled(False)
            self.btn_conn.setEnabled(False)
            return
        ports = list_ports()
        if not ports:
            # 注意：hw 后端但一个口都没有，也说清楚怎么办，
            # 而不是只显示"无可用串口"让人无从下手。
            self.cmb_port.addItem("无可用串口")
            self.cmb_port.setEnabled(True)      # 仍可点开，触发重新枚举
            self.cmb_port.setToolTip(
                "没有检测到串口。请确认 USB-TTL 已插入且驱动已装；"
                "再点开这个下拉框会自动重新枚举。")
            self.btn_conn.setEnabled(False)
            return
        self.cmb_port.setEnabled(True)
        self.cmb_port.setToolTip("选择串口（921600 8N1）")
        self.btn_conn.setEnabled(True)
        self.cmb_port.addItems(ports)
        if keep in ports:
            self.cmb_port.setCurrentIndex(ports.index(keep))

    def _toggle_conn(self) -> None:
        # 纯 Python 仿真后端不碰串口，按钮只提示
        if self.link_backend == "py":
            QtWidgets.QMessageBox.information(
                self, "连接",
                "当前是纯 Python 仿真后端（环境变量 SFRA_BACKEND=py），"
                "不经过串口。\n去掉该变量重启即可回到假 MCU / 真串口路径。")
            return

        ctrl = self.ctrl
        if getattr(ctrl, "is_open", False):
            # 真串口：断开
            ctrl.disconnect_port()
            return

        if self.link_backend == "sim":
            # 假 MCU：没有端口可选，直接启动子进程（open 里会发 connected）
            if not ctrl.link.is_open():
                ctrl.link.open()
            return

        port = self.cmb_port.currentText()
        if not port:
            self._refresh_ports()
            return
        ctrl.connect_port(port, 921600)

    def _wire_serial(self) -> None:
        link = getattr(self.ctrl, "link", None)
        if link is None:
            return
        link.connected.connect(self._on_connected)
        link.disconnected.connect(self._on_disconnected)
        link.errorText.connect(self._on_serial_error)
        link.logText.connect(
            lambda s: self.status.showMessage(s, 6000))
        # 握手完成后把固件版本 / ISR 频率显示到顶栏 pill
        if hasattr(self.ctrl, "caps"):
            link.capsReceived.connect(self._on_caps)

    def _set_led(self, on: bool) -> None:
        color = theme.GREEN if on else theme.T3
        self.led.setStyleSheet(
            f"background:{color}; border-radius:3.5px;"
            + ("" if not on else ""))

    def _on_connected(self, desc: str, baud: int = 0) -> None:
        self._set_led(True)
        self.btn_conn.setText("断开")
        self.pill_port.setText(desc if desc.startswith("Sim")
                               else (f"{desc} · {baud}" if baud else desc))
        self.tab_comp.set_serial(True)
        self.status.showMessage(f"已连接 {desc}")

    def _on_disconnected(self) -> None:
        self._set_led(False)
        self.btn_conn.setText("连接")
        self.pill_port.setText("未连接")
        self.tab_comp.set_serial(False)

    def _on_caps(self, caps) -> None:
        """握手拿到 CAPS：显示固件版本与真实 ISR 频率（03 §3.4）。

        点数校正由 TabMeasure 接 `ctrl.sigFwEffPoints` 自己完成——这里
        不要再调一次，否则两处都改控件，出现"收了又弹回"的怪现象。
        """
        self.pill_fw.setText(
            f"固件 {caps.version_str} · Fs {caps.isr_freq/1000:.0f} kHz")
        # MCU 的 isr_freq 是硬件事实，界面按它校正，避免用户填错后
        # 所有"每周期采样点 / 预计耗时"都跟着错
        if caps.isr_freq > 0:
            self.tab_measure.in_fs.blockSignals(True)
            self.tab_measure.in_fs.set_value(caps.isr_freq / 1000.0)
            self.tab_measure.in_fs.blockSignals(False)
            self.tab_measure._recalc_estimate()
        if getattr(caps, "eff_points", 0) <= 0:
            # 旧固件不报真实点数：至少把终点的 f_s/5 提示刷出来
            self.tab_measure._recalc_estimate()
        self.status.showMessage(
            f"握手完成：固件 {caps.version_str}，最多 {caps.max_vec_len} 点", 8000)

    def _on_serial_error(self, msg: str) -> None:
        self.status.showMessage(msg, 10000)
        # "无应答/拒绝"这类运行时错误只进状态栏，不弹模态框——
        # 扫频若在后台进行，弹窗会挡住用户看进度。
        if any(k in msg for k in ("无应答", "拒绝", "重发")):
            return
        QtWidgets.QMessageBox.warning(self, "串口", msg)

    # ================================================================ 系数
    def _on_coeff_apply(self, coeffs: list) -> None:
        """Tab2 → MCU：把差分方程系数发下去。"""
        # 协议只认 1~4 个系数，而 Tab2 给的是 5 个 b0,b1,b2,a1,a2。
        # PI 时 a2/b2 恒为 0，可以安全裁掉尾部零；2P2Z 时 b2 可能非零，
        # 裁到 4 个刚好是 b0,b1,b2,a1——a2 在两种结构里都是 0。
        vals = [float(v) for v in coeffs]
        while len(vals) > 1 and vals[-1] == 0.0:
            vals.pop()
        if not hasattr(self.ctrl, "set_coeff"):
            self.status.showMessage("当前后端不支持下发系数", 8000)
            return
        ok = self.ctrl.set_coeff(vals)
        if ok:
            self.status.showMessage(
                "系数已下发，等待 MCU 回显校验…", 8000)

    def _on_coeff_read(self) -> None:
        """MCU → Tab2：读回当前生效的系数并填回输入框。"""
        if not hasattr(self.ctrl, "get_coeff"):
            self.status.showMessage("当前后端不支持读回系数", 8000)
            return
        if not self.ctrl.get_coeff():
            self.status.showMessage("读回系数失败（链路未连接）", 8000)

    def _on_coeff_received(self, vals: list) -> None:
        if not vals:
            self.status.showMessage(
                "MCU 不支持系数读回（固件未实现 sfra_proto_get_coeff）", 10000)
            self.tab_comp.btn_read.setEnabled(False)
            return
        if self.tab_comp.set_coefficients(vals):
            self.status.showMessage(
                "已读回 MCU 系数：" + ", ".join(f"{v:.6f}" for v in vals), 10000)
        else:
            self.status.showMessage("系数读回成功但无法解析到 Kp/Ki", 10000)

    # ================================================================ 扫频结束
    # ================================================================ 收尾
    def shutdown(self) -> None:
        """退出前收尾：停计时器、关链路、杀假 MCU 子进程。

        为什么要显式做：假 MCU 是 QProcess 起的真子进程。如果只是让
        Python 解释器退出，子进程可能变孤儿（一直占着管道），而 Qt 在
        COM 清理阶段也可能卡住/报错。显式 close() 之后，disconnected
        信号会让控制器把自己的两个 QTimer 停掉，退出路径就干净了。
        """
        ctrl = getattr(self, "ctrl", None)
        if ctrl is not None:
            for tname in ("_poll", "_ack_guard"):
                t = getattr(ctrl, tname, None)
                if t is not None:
                    try:
                        t.stop()
                    except RuntimeError:
                        pass
            link = getattr(ctrl, "link", None)
            if link is not None:
                try:
                    link.close()
                except Exception:             # noqa: BLE001
                    pass
        # 给被杀的假 MCU 一点时间让 QProcess 完成回收。这里用
        # processEvents 而**不用** waitForFinished()：后者会起嵌套
        # 事件循环，在信号上下文里可能触发原生崩溃。
        for _ in range(20):
            QtCore.QCoreApplication.processEvents()

    def _on_sweep_done(self, ok: bool, msg: str) -> None:
        # 真链路后端的结果对象在 ctrl.result 里，和仿真后端同一字段
        res = getattr(self.ctrl, "result", None)
        if not ok or res is None or len(res) < 2:
            if not ok and msg:
                self.status.showMessage(f"扫频未完成：{msg}", 10000)
            return
        # 把结果喂给补偿器页。必须带"扫频当时"的 Kp/Ki，
        # 否则反推 Plant 与乘回 G 用同一组系数，预测恒等于实测。
        cfg = self.ctrl.cfg or {}
        comp = cfg.get("comp", {})
        kp_used = comp.get("kp")
        ki_used = comp.get("ki")
        # 先清掉旧上下文，避免 set_value 触发 refresh 时用到过期的 Plant
        self.tab_comp.clear_context()
        if kp_used is not None:
            self.tab_comp.in_kp.set_value(float(kp_used))
        if ki_used is not None:
            self.tab_comp.in_ki.set_value(float(ki_used))
        self.tab_comp.set_context(res, self.tab_measure.isr_freq,
                                  kp_used=kp_used, ki_used=ki_used)
        self._last_result = res

    # ================================================================ 导出
    def _on_export(self, kind: str) -> None:
        res = self.tab_measure.current_result()
        if res is None or len(res) < 2:
            QtWidgets.QMessageBox.information(self, "导出", "还没有可导出的数据。")
            return

        home = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.DesktopLocation)
        ext = {"csv": "csv", "png": "png", "json": "json", "md": "md"}[kind]
        prefix = {"csv": "sfra_data", "png": "sfra_bode",
                  "json": "sfra_session", "md": "sfra_report"}[kind]
        default = os.path.join(home, ex.default_name(prefix, ext))

        filters = {
            "csv": "CSV 文件 (*.csv)",
            "png": "PNG 图片 (*.png)",
            "json": "JSON 会话 (*.json)",
            "md": "Markdown 报告 (*.md)",
        }
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出", default, filters[kind])
        if not path:
            return
        path = ex.ensure_ext(path, ext)

        try:
            if kind == "csv":
                ex.to_csv(res, path)
            elif kind == "json":
                ex.to_json(res, path, extra={"meta": self.tab_measure.meta()})
            elif kind == "md":
                ex.to_markdown(res, path, meta=self.tab_measure.meta(),
                               curves=self.tab_measure.curves_for_report())
            elif kind == "png":
                ok = self.tab_measure.plot.snapshot(path)
                if not ok:
                    raise IOError("图片写入失败")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", str(e))
            return

        self.status.showMessage(f"已导出 {path}", 8000)
        QtWidgets.QToolTip.showText(
            self.mapToGlobal(QtCore.QPoint(self.width() // 2, 60)),
            f"已导出到\n{path}")

    # ================================================================ 关闭
    def closeEvent(self, e) -> None:
        if self.ctrl.running:
            self.ctrl.stop()
        link = getattr(self.ctrl, "link", None)
        if link is not None:
            link.close()
        elif hasattr(self.ctrl, "link") and self.link_backend == "py":
            pass
        super().closeEvent(e)
