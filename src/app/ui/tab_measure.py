"""SFRA Studio - Tab1 测量。

布局严格照设计稿：左 300px 配置 / 中 Bode / 右 268px 判据。
"""
from __future__ import annotations

import time
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from .. import theme
from ..model.analysis import analyze
from ..model.bode_data import MODE_LOOP, MODE_PLANT, SweepResult
from ..model.sweep_config import SweepConfig, CYC_FAST, NYQ_DIV
from ..sim import sfra_sim
from .widgets.bode_plot import BodePlot, Curve
from .widgets.common import (BigValue, Card, Group, KVRow, Note, NumEdit, Row,
                              SegmentedControl, num_with_unit,
                              section_label)
from .widgets.console import SerialConsole, describe_frame


class TabMeasure(QtWidgets.QWidget):
    """测量页。"""

    #: 请求导出 (kind)；由主窗口弹文件对话框
    exportRequested = QtCore.pyqtSignal(str)
    #: 状态栏文字
    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.ctrl = controller
        self.result: Optional[SweepResult] = None
        self._pred_result: Optional[SweepResult] = None
        #: 停滞检测状态（见 `_note_progress`）
        self._last_done = -1
        self._last_progress_t = 0.0
        self._stall_noted = False

        self._build()
        self._wire()
        self._recalc_estimate()
        self._update_ptext_idle()

    # ================================================================ 构建
    def _build(self) -> None:
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_left())
        root.addWidget(self._build_mid(), 1)
        root.addWidget(self._build_right())

    # ------------------------------------------------------------ 左栏
    def _build_left(self) -> QtWidgets.QWidget:
        col = QtWidgets.QWidget()
        col.setObjectName("ColLeft")
        col.setFixedWidth(theme.SIDEBAR_L)
        lay = QtWidgets.QVBoxLayout(col)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(0)

        # ---- 扫频配置 ----
        lay.addWidget(section_label("扫频配置"))
        g = Group()
        lay.addWidget(g)
        lay.addSpacing(6)

        self.seg_mode = SegmentedControl(["Loop Gain", "Plant"], 0, pad=(4, 14))
        r = Row("模式")
        r.set_value_widget(self.seg_mode)
        g.add_row(r)

        self.in_f0 = NumEdit(100, 1, 0.01, 99999)
        g.add_row(Row("起点", num_with_unit(self.in_f0, "Hz")))

        self.in_f1 = NumEdit(20000, 0, 1, 200000)
        g.add_row(Row("终点", num_with_unit(self.in_f1, "Hz")))

        # 默认 40 点/decade。
        #
        # 原来是 20。20 点/dec 在 1.3 decade（100 Hz~2 kHz）只有 27 点，
        # 出图后曲线是"折线感"明显：谐振峰、拐点全靠插值，看不出细节。
        # 40 点/dec 是 54 点，代价只有扫频时间翻倍——按 21 ms/点算，
        # 27 点 0.6 s、54 点 1.2 s，用户根本感觉不到；
        # 而协议侧 GET_DATA 一次 7 点、轮询 200 ms，也完全跟得上。
        #
        # 上限 200 是协议允许的 vec_length 上限之内（固件 256），
        # 但 100 点/dec 在 2 kHz 档就是 136 点、耗时 ~3 s，
        # 收益已不明显，所以默认值取 40。
        self.in_ppd = NumEdit(40, 0, 1, 200)
        self.in_ppd.setFixedWidth(52)
        g.add_row(Row("点数 / decade", self.in_ppd))

        # 标签用短名：侧栏只有 300px，「Fast / Normal / Precise」三项
        # 加起来要 246px，而控件实际只有 212px —— 「Precise」必被截断。
        # 改成「快 / 标准 / 精细」后总宽降到约 150px，任何窗口下都完整。
        # 完整含义放 tooltip（代码按索引取值，改标签不影响逻辑）。
        self.seg_cyc = SegmentedControl(["快", "标准", "精细"], 1, pad=(4, 8))
        self.seg_cyc.setToolTip(
            "扫频精度档（每点停留周期数）：\n"
            "快 = Fast，最快但噪声大\n"
            "标准 = Normal，日常用\n"
            "精细 = Precise，慢但相位最稳")
        g.add_row(Row("精度", self.seg_cyc))

        self.in_amp = NumEdit(1.0, 2, 0.02, 20)
        g.add_row(Row("注入幅值", num_with_unit(self.in_amp, "% pu")))

        self.in_fs = NumEdit(100, 1, 1, 500)
        g.add_row(Row("ISR 频率", num_with_unit(self.in_fs, "kHz")))

        # 终点超 f_s/5 的提示（不占固定高度，只在越界时出现）。
        # 这条提示是"承诺 47 点、MCU 只扫 28 点"事故的界面侧护栏。
        self.lb_nyq = QtWidgets.QLabel("")
        self.lb_nyq.setWordWrap(True)
        self.lb_nyq.setStyleSheet("font-size:11px; color:#854F0B;")
        self.lb_nyq.setVisible(False)
        lay.addSpacing(4)
        lay.addWidget(self.lb_nyq)

        # 预扫
        self.btn_pre = QtWidgets.QPushButton("预扫")
        self.btn_pre.setObjectName("Small")
        self.btn_pre.setToolTip("在谐振附近找峰值，自动下调注入幅值防止饱和")
        r = Row("上限反推")
        r.set_value_widget(self.btn_pre)
        g.add_row(r)

        # ---- 被测对象（**只在仿真后端显示**） ----
        #
        # 这整块只对 `SimLinkController`/`SweepController` 有意义：
        # 真串口后端下 `SerialLinkController` 从不读这些字段，被测对象
        # 在 MCU 内部。留着它会让用户以为"必须填"，并误以为填错会导致
        # 测量失败（2026-11-24 用户反馈的困惑）。所以真硬件下整组隐藏。
        self.dut_title = section_label("被测对象")
        self.dut_group = Group()
        lay.addSpacing(16)
        lay.addWidget(self.dut_title)
        lay.addWidget(self.dut_group)
        lay.addSpacing(6)
        gird = self.dut_group

        self.seg_dut = SegmentedControl(["二阶 LC", "一阶 RC"], 0)
        gird.add_row(Row("仿真模型", self.seg_dut))

        self.in_f0c = NumEdit(1300, 0, 1, 100000)
        gird.add_row(Row("谐振 / 截止", num_with_unit(self.in_f0c, "Hz")))

        self.in_q = NumEdit(2.0, 2, 0.1, 50)
        gird.add_row(Row("品质因数 Q", self.in_q))

        self.in_kp = NumEdit(0.5, 3, 0.0, 1e5)
        gird.add_row(Row("Kp", self.in_kp))

        self.in_ki = NumEdit(1200, 0, 0.0, 1e7)
        gird.add_row(Row("Ki", self.in_ki))

        lay.addStretch(1)

        # ---- 底部动作区 ----
        est = QtWidgets.QWidget()
        el = QtWidgets.QHBoxLayout(est)
        el.setContentsMargins(2, 0, 2, 0)
        self.lb_npts = QtWidgets.QLabel("68 点")
        self.lb_npts.setObjectName("Key")
        self.lb_etime = QtWidgets.QLabel("—")
        self.lb_etime.setObjectName("ValSm")
        self.lb_etime.setStyleSheet("font-size:13px; font-weight:600;")
        el.addWidget(QtWidgets.QLabel("预计"))
        el.addWidget(self.lb_npts)
        el.addStretch(1)
        el.addWidget(self.lb_etime)
        lay.addWidget(est)
        lay.addSpacing(8)

        self.btn_run = QtWidgets.QPushButton("开始扫频")
        self.btn_run.setObjectName("Primary")
        self.btn_run.setMinimumHeight(36)
        lay.addWidget(self.btn_run)
        lay.addSpacing(6)

        h = QtWidgets.QHBoxLayout()
        h.setSpacing(6)
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_exp = QtWidgets.QPushButton("导出")
        self.btn_exp.setFixedWidth(64)
        self.btn_exp.setEnabled(False)
        h.addWidget(self.btn_stop, 1)
        h.addWidget(self.btn_exp)
        lay.addLayout(h)
        lay.addSpacing(10)

        # 进度
        top = QtWidgets.QHBoxLayout()
        self.lb_pstate = QtWidgets.QLabel("Loop Gain · 就绪")
        self.lb_pstate.setObjectName("Key")
        self.lb_ppct = QtWidgets.QLabel("0%")
        self.lb_ppct.setObjectName("ValSm")
        top.addWidget(self.lb_pstate)
        top.addStretch(1)
        top.addWidget(self.lb_ppct)
        lay.addLayout(top)
        lay.addSpacing(5)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        lay.addWidget(self.bar)
        lay.addSpacing(5)

        bot = QtWidgets.QHBoxLayout()
        self.lb_fact = QtWidgets.QLabel("f_act —")
        self.lb_samp = QtWidgets.QLabel("—")
        for lb in (self.lb_fact, self.lb_samp):
            lb.setStyleSheet(f"color:{theme.T3}; font-size:10.5px;"
                             f"font-family:{theme.FONT_MONO};")
        bot.addWidget(self.lb_fact)
        bot.addStretch(1)
        bot.addWidget(self.lb_samp)
        lay.addLayout(bot)

        return col

    # ------------------------------------------------------------ 中栏
    def _build_mid(self) -> QtWidgets.QWidget:
        col = QtWidgets.QWidget()
        col.setObjectName("ColMid")
        lay = QtWidgets.QVBoxLayout(col)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(0)

        # 标题行：增益 + 相位两块图共用同一个 BodePlot（X 轴已联动）
        card = Card("幅频 / 相频", "dB · deg（相位已 unwrap）")
        card.add_legend("Loop Gain", theme.BLUE)
        card.add_legend("预测", theme.PURPLE)
        self.plot = BodePlot()
        card.body_layout.addWidget(self.plot, 1)
        lay.addWidget(card, 1)

        # ---- 饱和提示条（W2）----
        # 固件置了 STATUS.flags 的饱和位、PC 也解了，但界面从来不显示。
        # 后果：用户看到一条顶到水平线的曲线，分不清是"被测对象饱和"
        # 还是"注入幅值太大"——这两者处置完全相反（前者查对象，后者降幅值）。
        self.sat_bar = QtWidgets.QFrame()
        self.sat_bar.setObjectName("SatBar")
        self.sat_bar.setStyleSheet(
            f"#SatBar{{background:{theme.AMBER}1A;"
            f"border:1px solid {theme.AMBER};border-radius:7px;}}")
        sb = QtWidgets.QHBoxLayout(self.sat_bar)
        sb.setContentsMargins(10, 6, 10, 6)
        sb.setSpacing(8)
        self.lb_sat = QtWidgets.QLabel("")
        self.lb_sat.setStyleSheet(
            f"color:{theme.AMBER_T}; font-size:11.5px;")
        self.lb_sat.setWordWrap(True)
        sb.addWidget(self.lb_sat, 1)
        self.btn_sat_detail = QtWidgets.QPushButton("查看频点")
        self.btn_sat_detail.setObjectName("Small")
        self.btn_sat_detail.setFixedHeight(22)
        self.btn_sat_detail.clicked.connect(self._on_sat_detail)
        sb.addWidget(self.btn_sat_detail)
        self.sat_bar.setVisible(False)      # 没饱和就不占版面
        lay.addWidget(self.sat_bar)

        lay.addSpacing(4)
        foot = QtWidgets.QWidget()
        fl = QtWidgets.QHBoxLayout(foot)
        fl.setContentsMargins(4, 4, 4, 0)
        fl.setSpacing(16)
        self.lb_cursor = QtWidgets.QLabel("悬停曲线查看读数")
        self.lb_cursor.setStyleSheet(
            f"color:{theme.T2}; font-size:11px; font-family:{theme.FONT_MONO};")
        fl.addWidget(self.lb_cursor)
        fl.addStretch(1)
        self.lb_zoom = QtWidgets.QLabel("滚轮缩放 · 双击复位 · 拖动平移")
        self.lb_zoom.setStyleSheet(f"color:{theme.T3}; font-size:10.5px;")
        fl.addWidget(self.lb_zoom)
        lay.addWidget(foot)

        return col

    # ------------------------------------------------------------ 右栏
    def _build_right(self) -> QtWidgets.QWidget:
        col = QtWidgets.QWidget()
        col.setObjectName("ColRight")
        col.setFixedWidth(theme.SIDEBAR_R)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(14, 14, 12, 14)
        lay.setSpacing(0)

        # ---- 稳定性判据 ----
        lay.addWidget(section_label("稳定性判据"))
        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        cl = QtWidgets.QVBoxLayout(card)
        cl.setContentsMargins(12, 8, 12, 8)
        cl.setSpacing(0)

        self.v_pm = BigValue("°")
        self.v_gm = BigValue("dB")
        self.v_fc = BigValue("Hz")
        for key, w in (("相位裕度 PM", self.v_pm),
                       ("增益裕度 GM", self.v_gm),
                       ("穿越频率 fc", self.v_fc)):
            row = QtWidgets.QWidget()
            rl = QtWidgets.QHBoxLayout(row)
            rl.setContentsMargins(0, 7, 0, 7)
            rl.setSpacing(8)
            k = QtWidgets.QLabel(key)
            k.setObjectName("Key")
            rl.addWidget(k)
            rl.addStretch(1)
            rl.addWidget(w)
            cl.addWidget(row)
            if key != "穿越频率 fc":
                line = QtWidgets.QFrame()
                line.setFixedHeight(1)
                line.setStyleSheet(f"background:{theme.LINE2};")
                cl.addWidget(line)

        lay.addWidget(card)
        lay.addSpacing(7)

        # 判据 chip
        chips = QtWidgets.QWidget()
        chl = QtWidgets.QHBoxLayout(chips)
        chl.setContentsMargins(0, 0, 0, 0)
        chl.setSpacing(5)
        self.chip_pm = QtWidgets.QLabel("等待数据")
        self.chip_pm.setObjectName("ChipMute")
        self.chip_gm = QtWidgets.QLabel("等待数据")
        self.chip_gm.setObjectName("ChipMute")
        chl.addWidget(self.chip_pm)
        chl.addWidget(self.chip_gm)
        chl.addStretch(1)
        lay.addWidget(chips)

        # ---- 曲线信息 ----
        lay.addSpacing(16)
        lay.addWidget(section_label("曲线信息"))
        g = Group()
        lay.addWidget(g)
        self.kv_n = KVRow("频点", "0 / —")
        self.kv_amp = KVRow("注入幅值", "1.00 %pu")
        self.kv_spc = KVRow("每周期采样", "—")
        self.kv_noise = KVRow("估噪底", "—")
        for w in (self.kv_n, self.kv_amp, self.kv_spc, self.kv_noise):
            g.add_row(w)

        # ---- 告警 ----
        lay.addSpacing(16)
        lay.addWidget(section_label("告警"))
        self._notes_w = QtWidgets.QWidget()
        self._notes_lay = QtWidgets.QVBoxLayout(self._notes_w)
        self._notes_lay.setContentsMargins(0, 0, 0, 0)
        self._notes_lay.setSpacing(5)
        lay.addWidget(self._notes_w)
        self._note_items = []
        self.set_notes([("尚未扫频", False)])

        lay.addStretch(1)

        scroll.setWidget(inner)

        # ---- 串口日志面板（设计稿 design/sfra-console-mockup.html）----
        # **放在滚动区之外**：设计稿要求它贴在右列底部（`margin-top:auto`），
        # 折叠时只占一行、不挤压图表。如果塞进 scroll 里，它会跟着
        # 内容一起被卷走，就不满足"始终可见"了。
        outer = QtWidgets.QVBoxLayout(col)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll, 1)

        wrap = QtWidgets.QWidget()
        wl = QtWidgets.QVBoxLayout(wrap)
        # 下边距与右列其它内容对齐（左边 14 / 右边 12，见上面 lay 的 margins）
        wl.setContentsMargins(14, 0, 12, 12)
        wl.setSpacing(0)
        self.console = SerialConsole()
        self.console.set_port("未连接", False)
        self.console.setMaximumHeight(340)
        wl.addWidget(self.console)
        outer.addWidget(wrap)
        return col

    # ================================================================ 信号
    def _wire(self) -> None:
        self.ctrl.sigPoint.connect(self._on_point)
        self.ctrl.sigProgress.connect(self._on_progress)
        self.ctrl.sigDone.connect(self._on_done)
        sig_eff = getattr(self.ctrl, "sigFwEffPoints", None)
        if sig_eff is not None:
            sig_eff.connect(self.apply_fw_eff_points)

        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_pre.clicked.connect(self._on_prescan)
        self.btn_exp.clicked.connect(self._on_export)

        for w in (self.in_f0, self.in_f1, self.in_ppd, self.in_amp):
            w.valueChanged.connect(self._on_cfg_changed)
        self.in_fs.valueChanged.connect(self._on_fs_changed)
        self.seg_cyc.changed.connect(lambda _: self._recalc_estimate())
        self.seg_mode.changed.connect(self._on_mode_changed)
        self.seg_dut.changed.connect(lambda _: None)

        self.plot.cursorMoved.connect(self._on_cursor)

        # 双击复位
        self.plot.glw.scene().sigMouseClicked.connect(self._on_plot_click)

        self._wire_console()

    # ------------------------------------------------------------ 串口日志
    def _wire_console(self) -> None:
        """把链路的所有可观测信号接到日志面板。

        统一挂在这里而不是主窗口，是因为**面板在 Tab1 里**：
        用户看着哪一页，日志就在哪一页。主窗口只管状态栏的短提示。

        `getattr` 兜底：纯 Python 仿真后端（`SFRA_BACKEND=py`）根本没有
        协议层，没有这些信号——那种模式下面板只显示 `sigLog` 文案。
        """
        con = self.console

        # 控制器自己的日志（"开始扫频 n 点"、"已发送 ABORT"、停滞提示…）
        self.ctrl.sigLog.connect(con.log_info)

        link = getattr(self.ctrl, "link", None)
        if link is None:
            con.log_sys("纯 Python 仿真后端：无协议层，日志仅显示控制流程")
            return

        # 链路层已有的信号，全部旁听一遍。
        # **不改 BaseLink**：面板只是信号的又一个接收者，随时可以摘掉，
        # 不会影响协议层的任何判断。
        for sig, slot in (
                (getattr(link, "logText", None), con.log_info),
                (getattr(link, "errorText", None), con.log_err),
                (getattr(link, "connected", None),
                 lambda d: (con.log_sys(f"已连接 {d}"),
                            con.set_port(d, True))),
                (getattr(link, "disconnected", None),
                 lambda: (con.log_sys("链路已断开"),
                          con.set_port("未连接", False))),
        ):
            if sig is None:
                continue
            try:
                sig.connect(slot)
            except TypeError:
                pass

        # 每一帧收到的数据都过一遍，由 `describe_frame` 决定记不记。
        # DATA 帧不记（见该函数说明）。
        link.frameIn.connect(self._on_frame)
        # 发出的帧同理——面板内部再按"是否轮询"过滤一次。
        sig_out = getattr(link, "frameOut", None)
        if sig_out is not None:
            sig_out.connect(con.log_frame_out)

        # 命令输入框 → 链路。只放行"不打断扫频"的命令，
        # 真正的时序保护在 `SerialConsole.set_scanning()`。
        con.commandSubmitted.connect(self._on_console_command)

        # 握手成功后把端口信息补全（connected 只带描述串）
        caps = getattr(self.ctrl, "caps", None)
        if caps is not None:
            con.log_sys(
                f"CAPS：fs={caps.isr_freq:.0f} Hz，最大点数 {caps.max_vec_len}")

    def _on_frame(self, f) -> None:
        d = describe_frame(f, getattr(self.ctrl, "_n_total", 0))
        if d is None:
            return
        kind, text = d
        if kind == "err":
            self.console.log_err(text)
        else:
            self.console.log_rx(text)

    def _on_console_command(self, cmd: int, payload: bytes) -> None:
        """面板发命令。走 `link.send()`，与扫频命令同一条链路。

        `wait_ack=False`：这条命令是**旁路**的，不能占用 BaseLink 那个
        单槽位的重发定时器——否则一次手点 `ping` 就会把扫频中挂着的
        `SET_CONFIG` 应答超时逻辑顶掉。
        """
        link = getattr(self.ctrl, "link", None)
        if link is None or not link.is_open():
            self.console.log_err("链路未连接，命令未发送")
            return
        try:
            link.send(cmd, payload, wait_ack=False)
        except Exception as e:                      # noqa: BLE001
            self.console.log_err(f"发送失败：{e}")

    # ================================================================ 配置
    @property
    def mode(self) -> int:
        return MODE_LOOP if self.seg_mode.current() == 0 else MODE_PLANT

    @property
    def cyc_mode(self) -> int:
        return self.seg_cyc.current()

    @property
    def isr_freq(self) -> float:
        return self.in_fs.value() * 1000.0

    @property
    def amp_pu(self) -> float:
        return self.in_amp.value() / 100.0

    def _on_mode_changed(self, _i: int) -> None:
        self._recalc_estimate()
        # 切模式就清掉旧曲线（两种模式量纲不同，混在一起会误读）
        self.plot.clear()
        self.result = None
        self._reset_readout()
        self._update_chips(None)
        self._update_ptext_idle()

    def _on_fs_changed(self, _v: float = 0.0) -> None:
        """改 ISR 频率后，把 f_s/5 之外的点数收掉。

        只在**降频**时才需要收：升到 100 kHz 时 f_s/5 变大，原来的终点
        本来就合法，不该动用户输入。反之从 100 kHz 降到 10 kHz 而不收，
        界面就又会承诺 47 点、固件只扫 28 点（2026-11-24 的事故）。
        """
        self._on_cfg_changed(_v)
        n = self.fw_eff_points()
        if n <= 0:
            return
        cfg = self.sweep_cfg()
        if cfg.n_points > n:
            # 用固件的 f_s/5 上限重收终点（不依赖 cfg.n_points，因为 PC 侧
            # 已经把终点截到 2000，n_points 恒等于 eff，永远判不出越界）
            self._clamp_stop_to_nyq()

    def _clamp_stop_to_nyq(self, silent: bool = False) -> None:
        """把终点收到 f_s/5 内，取"每 decade ppd 点"能落到的最后一档。"""
        import math
        fs = self.isr_freq
        if fs <= 0:
            return
        f_nyq = fs / NYQ_DIV
        start = max(self.in_f0.value(), 1e-6)
        ppd = max(1, int(self.in_ppd.value()))
        if start >= f_nyq or self.in_f1.value() <= f_nyq * (1.0 + 1e-9):
            return
        n = max(1, int(math.floor(math.log10(f_nyq / start) * ppd)) + 1)
        f_last = start * (10.0 ** ((n - 1) / ppd))
        self.in_f1.blockSignals(True)
        self.in_f1.set_value(round(f_last, 3))
        self.in_f1.blockSignals(False)
        if not silent:
            self._recalc_estimate()
            self.statusMessage.emit(
                f"终点已按 f_s/5 自动收到 {f_last:.0f} Hz"
                f"（{self.sweep_cfg().n_points} 点）")

    def _on_cfg_changed(self, _v: float = 0.0) -> None:
        self._recalc_estimate()
        self.kv_amp.set_value(f"{self.in_amp.value():.2f} %pu")

    def set_hardware_mode(self, is_hw: bool) -> None:
        """真串口后端下隐藏"被测对象"整组（见 _build_left 的说明）。

        被测对象在 MCU 内部，PC 侧这几个字段不参与任何帧，留着只会
        误导用户"必须填得很准"。
        """
        self.dut_title.setVisible(not is_hw)
        self.dut_group.setVisible(not is_hw)

    def fw_eff_points(self) -> int:
        """固件 CAPS 上报的真实点数（0 = 未知，按上位机自己算的来）。"""
        return int(getattr(self, "_fw_eff_points", 0) or 0)

    def apply_fw_eff_points(self, n: int) -> None:
        """记下固件上报的真实点数，并把"终点"收到 f_s/5 内。

        为什么要在握手后立刻做：上位机按界面终点算点数，固件在 START 里
        还会用 `sfra_effective_points()` 再裁一次。只要两处口径差一个点，
        就会重现"界面承诺 47 点、MCU 只扫 28 点"——上位机死循环请求不
        存在的点，满屏 INDEX_RANGE。
        """
        self._fw_eff_points = max(0, int(n))
        if self._fw_eff_points <= 0:
            return
        self._clamp_stop_to_nyq()
        self._recalc_estimate()

    def _freqs(self):
        # 统一走 model/sweep_config：f_s/5 截断、点数、耗时必须是同一套算法
        return self.sweep_cfg().freqs

    def sweep_cfg(self) -> SweepConfig:
        """把界面当前值收成一个 SweepConfig（估耗时、开扫、真链路都用它）。"""
        return SweepConfig(
            isr_freq=self.isr_freq,
            f_start=self.in_f0.value(),
            f_stop=self.in_f1.value(),
            ppd=int(self.in_ppd.value()),
            cyc_mode=self.cyc_mode,
            mode=self.mode,
            amp_pu=self.amp_pu,
            dut="lc" if self.seg_dut.current() == 0 else "first",
            f0c=self.in_f0c.value(),
            q=self.in_q.value(),
            kp=self.in_kp.value(),
            ki=self.in_ki.value(),
        )

    def _recalc_estimate(self, sync_stop: bool = False) -> None:
        """刷新"预计 N 点 / 耗时"，并把终点与 f_s/5 对齐。

        `sync_stop=True` 时**顺手把终点改成 f_s/5 内的最后一档**。
        必须这么做，而不是只在握手时收一次：`SweepConfig.n_points` 只受
        `isr_freq` 影响、与「终点」输入框无关，所以松开终点后它又会被
        当成"用户填的 20000"→ `sweep_cfg()` 里 f_stop 仍是 20000，
        但 `n_points` 已经是截断后的 27。真机上就出现"界面显示 27 点、
        日志却写 47 点"的分歧（2026-11-24）。
        """
        if sync_stop:
            self._clamp_stop_to_nyq(silent=True)
        cfg = self.sweep_cfg()
        n = cfg.n_points
        self.lb_npts.setText(f"{n} 点")
        if n == 0:
            self.lb_etime.setText("—")
            self.lb_nyq.setVisible(False)
            return
        sec = cfg.estimate_seconds()
        self.lb_etime.setText(self._fmt_sec(sec))
        # 判据不能再用 cfg.f_nyq_requested：PC 自己的 freqs 已经被 f_s/5
        # 截过，n_points 恒等于截断后的点数，拿它判越界永远为 False
        # （2026-11-24：fs=10k、终点填 20000，提示一行都没出）。
        # 直接比较用户填的终点与 f_s/5。
        over = cfg.f_stop > cfg.f_nyq * (1.0 + 1e-9) and cfg.n_points > 0
        if over:
            self.lb_npts.setToolTip(
                f"终点被 f_s/5 上限截断：{cfg.f_stop:.0f} Hz → {cfg.f_nyq:.0f} Hz"
                f"（每周期至少 5 个采样点才可信）")
            self.lb_npts.setStyleSheet(f"color:{theme.AMBER_T};")
        else:
            self.lb_npts.setToolTip("")
            self.lb_npts.setStyleSheet("")
        if not over or cfg.n_points == 0:
            self.lb_nyq.setVisible(False)
            self.lb_nyq.setText("")
            return
        self.lb_nyq.setText(
            f"终点 {cfg.f_stop:.0f} Hz 超过 f_s/5 = {cfg.f_nyq:.0f} Hz，"
            f"MCU 只会扫 {cfg.n_points} 点（到 {cfg.freqs[-1]:.0f} Hz）。"
            f"建议把终点改成 {cfg.freqs[-1]:.0f}。")
        self.lb_nyq.setVisible(True)

    @staticmethod
    def _fmt_sec(s: float) -> str:
        if s < 60:
            return f"约 {s:.0f} s"
        m = int(s // 60)
        return f"约 {m} min {int(s % 60)} s"

    # ================================================================ 流程
    def _on_run(self) -> None:
        cfg_obj = self.sweep_cfg()
        freqs = cfg_obj.freqs
        if len(freqs) == 0:
            QtWidgets.QMessageBox.warning(self, "参数错误",
                                          "终点频率必须大于起点频率。")
            return

        # 统一校验（与固件 do_set_config 同源）：先给可读中文，别等固件回错误码
        for text, hard in cfg_obj.validate():
            if hard:
                QtWidgets.QMessageBox.warning(self, "参数错误", text)
                return
        warns = [(t, False) for t, hard in cfg_obj.validate() if not hard]
        if warns:
            self.set_notes(warns)

        cfg = cfg_obj.to_sim_cfg()

        ok = self.ctrl.start(cfg)
        if not ok:
            return

        self.plot.clear()
        self.plot.set_stability(None)
        self.result = None
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_exp.setEnabled(False)
        self.btn_pre.setEnabled(False)
        self.bar.setValue(0)
        self.lb_ppct.setText("0%")
        self._last_done = -1
        self._last_progress_t = 0.0
        self._stall_noted = False
        mode_name = "Loop Gain" if self.mode == MODE_LOOP else "Plant"
        self.lb_pstate.setText(f"{mode_name} · 扫描中")
        self.set_notes([("扫描中，请勿断开串口", False)])
        self._update_chips(None)
        # 扫描中禁用手动发命令：额外帧会打乱 200 ms 的轮询节奏
        # （固件 500 ms 收不到有效帧就 ABORT，而 GET_DATA 与 GET_STATUS
        #   在 MCU 主循环里是同一次 poll 的竞争者）。
        self.console.set_scanning(True)

    def _on_stop(self) -> None:
        self.ctrl.stop()
        self._set_pstate("已中止")

    #: `lb_pstate` 的文案格式是 `"Loop Gain · 扫描中"`。
    #: **分隔符是 `" ·"`（空格 + 中点），不是 `"·"`** ——
    #: 之前两处都写 `text().split(" ·")[0]`，取到的是空串，
    #: 于是状态栏变成 `" · 已中止"`，开头什么都没有。
    #: 这里统一从模式字段重建，不再切字符串。
    def _set_pstate(self, what: str) -> None:
        mode_name = "Loop Gain" if self.mode == MODE_LOOP else "Plant"
        self.lb_pstate.setText(f"{mode_name} · {what}")

    def _on_point(self, p) -> None:
        self.result = self.ctrl.result
        if self.result is None or len(self.result) < 2:
            return
        # 实时重画（点数不多，68 点重绘 < 2ms，不必节流）
        self._redraw_live()
        self._refresh_sat()      # 饱和条随扫频实时出现，不用等扫完

    def _redraw_live(self) -> None:
        if self.result is None or len(self.result) < 2:
            return
        c = self._make_curve(self.result)
        self.plot.set_curves([c])
        self.plot.set_stability(None)     # 扫完才标穿越点，避免半截误标
        self.kv_n.set_value(f"{len(self.result)} / {len(self._freqs())}")

    @staticmethod
    def _make_curve(res: SweepResult) -> Curve:
        """按可信掩码裁剪曲线：低置信度的点不画（否则相位图会被压扁）。"""
        keep = res.trusted_mask()
        if keep.sum() < 2:
            keep = ~res.invalid_mask()
        return Curve(freq=res.freq[keep], mag=res.mag[keep],
                     phase=res.phase[keep], name=res.mode_name,
                     color=theme.BLUE)

    def _on_progress(self, done: int, total: int, f_act: float) -> None:
        pct = int(done * 100 / max(total, 1))
        self.bar.setValue(pct)
        self.lb_ppct.setText(f"{pct}%")
        if f_act >= 1000:
            self.lb_fact.setText(f"f_act {f_act/1000:.2f} kHz")
        else:
            self.lb_fact.setText(f"f_act {f_act:.1f} Hz")
        spc = self.isr_freq / max(f_act, 1e-9)
        self.lb_samp.setText(f"{spc:.0f} 样本/周期")
        self.kv_spc.set_value(
            f"{spc:.0f} 点" if spc >= 4 else f"{spc:.0f} 点 · 偏低")
        self._note_progress(done, total, f_act)

    # ------------------------------------------------------------ 停滞检测
    #: 进度多久不动就判为"卡住了"（毫秒）。真链路轮询周期 200 ms，
    #: 一个低频点最长可能停留 2~3 s（settle 2 个周期 + 100 周期采集）。
    #: 8 s 足以跨过最慢的频点，又不会让用户干等太久才发现。
    STALL_MS = 8000

    def _note_progress(self, done: int, total: int, f_act: float) -> None:
        """记录进度心跳，并在"停止前进"时把证据显示出来。

        **为什么需要这个**：用户反馈"进度条停在 57%"，而画面已经画到了
        终点频率。这两种现象同时出现只有一个解释——**曲线上的点是全的，
        但进度计数没走到 total**，于是 `sigDone` 没发，界面停在中间。

        旧版本到这里就只剩一句"停在 57%"，什么线索都没有。现在把
        `done/total`、`f_act`、`状态`、`不动了多久` 一起写进右栏告警，
        下一次再出现就能直接看出是哪一种：
          · done 不动但 f_act 在变 → MCU 还在跑，只是上位机没收到 DATA
          · done 和 f_act 都不动      → MCU 侧停了（或链路断了）
        """
        now = time.monotonic()
        if done != self._last_done:
            self._last_done = done
            self._last_progress_t = now
            self._stall_noted = False
            return
        if self._last_progress_t <= 0.0:
            self._last_progress_t = now
            return
        idle_ms = (now - self._last_progress_t) * 1000.0
        if idle_ms < self.STALL_MS or self._stall_noted:
            return
        self._stall_noted = True
        spc = self.isr_freq / max(f_act, 1e-9)
        self.set_notes([(
            f"进度停在 {done}/{total}（{int(done*100/max(total,1))}%）已 "
            f"{idle_ms/1000:.0f} s。当前 f_act {f_act:.0f} Hz、"
            f"每周期 {spc:.0f} 点。若是真机，多为该频点采集超时或 DATA 帧"
            f"丢失；可「停止」后重连续传。", True)])
        self._set_pstate("疑似停滞")

    def _on_done(self, ok: bool, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_pre.setEnabled(True)
        self.btn_exp.setEnabled(self.result is not None and len(self.result) > 1)
        # 扫描结束（无论成败）立刻解禁手动发命令。放在最前面，
        # 因为下面两个分支都会 return，写在后半段会漏掉失败路径。
        self.console.set_scanning(False)
        mode_name = "Loop Gain" if self.mode == MODE_LOOP else "Plant"

        if not ok:
            # **失败也要按实际拿到手的点数把进度条摆正**。
            # 旧版本这里直接 return，进度条就永远停在"最后一个完成的点"
            # ——用户看到"57% + 曲线画完了"，分不清是没有点还是显示错了。
            # 现在把真实情况写进状态与告警，百分比按实际点数算。
            got = len(self.result) if self.result is not None else 0
            total = self._n_total_hint()
            pct = int(got * 100 / max(total, 1))
            self.bar.setValue(pct)
            self.lb_ppct.setText(f"{got}/{total} · {pct}%")
            self.lb_pstate.setText(f"{mode_name} · 未完成")
            # 先把已有数据定格（`_finalize` 内部会 set_notes 覆盖告警），
            # **然后**再写失败原因，顺序反了用户就只看得到"未检出穿越点"，
            # 完全不知道是扫频中途断了。
            if got >= 2:
                self._finalize()
            self.set_notes([
                (f"扫频未完成：{msg}", True),
                (f"已收到 {got} / {total} 点。曲线可看，但 PM/GM 不完整"
                 f"（可能缺穿越点），不要据此判定稳定性。", False),
            ])
            return

        self.lb_pstate.setText(f"{mode_name} · 完成")
        self.bar.setValue(100)
        self.lb_ppct.setText("100%")
        self._finalize()

    def _n_total_hint(self) -> int:
        """总点数兜底：优先后端记录，其次按当前界面配置现算。"""
        n = int(getattr(self.ctrl, "_n_total", 0) or 0)
        return n if n > 0 else max(len(self._freqs()), 1)

    def _finalize(self) -> None:
        """曲线定格 + 算稳定性 + 出告警。"""
        if self.result is None or len(self.result) < 2:
            return
        res = self.result
        self._refresh_sat()          # W2：先把饱和条刷出来
        keep = res.trusted_mask()
        # 饱和点必须从 PM/GM 里剔除：顶到轨之后输出不再线性跟随注入，
        # 此刻测到的"增益"只是限幅器的增益，与真实环路无关。
        keep = keep & ~res.sat_mask()
        if keep.sum() < 2:
            # 全饱和/全剔除时不能硬算，退回原始可信掩码并明确告警
            keep = res.trusted_mask()
        f, m, p = res.freq[keep], res.mag[keep], res.phase[keep]
        st = analyze(f, m, p)

        self.plot.set_curves([self._make_curve(res)])
        self.plot.set_stability(st)
        self._update_readout(st)

        notes = []
        if st.pm_note:
            notes.append((st.pm_note, True))
        if st.gm_note:
            notes.append((st.gm_note, False))
        if st.pm is not None and st.pm < 45:
            notes.append((f"PM {st.pm:.1f}° 低于 45°，环路偏激进", True))
        if st.gm is not None and st.gm < 6:
            notes.append((f"GM {st.gm:.1f} dB 低于 6 dB", True))

        n_bad = int(res.invalid_mask().sum())
        if n_bad:
            notes.append((f"{n_bad} 个频点每周期采样 < 2，已剔除", True))
        if res.mode == MODE_LOOP:
            n_low = int(res.loop_confidence().sum())
            n_glitch = int(res.phase_glitch_mask().sum())
            if n_low or n_glitch:
                why = []
                if n_low:
                    why.append(f"{n_low} 点反馈占比过低")
                if n_glitch:
                    why.append(f"{n_glitch} 点相位跳变")
                notes.append(
                    (f"高频段 {'、'.join(why)}，GH 数值不可信，已不参与判据", False))
        n_warn = int(res.warn_mask().sum())
        if n_warn:
            notes.append((f"{n_warn} 个频点每周期采样 < 4，高频段可信度低", False))
        n_sat = res.sat_count
        if n_sat:
            notes.append(
                (f"{n_sat} 个频点饱和，已从 PM/GM 计算中剔除"
                 + ("（全部频点）" if n_sat >= len(res) else ""), True))
        if not notes:
            notes = [("曲线质量良好", False)]
        self.set_notes(notes)

        self.kv_n.set_value(f"{len(res)} / {len(res)}")
        self.statusMessage.emit(
            f"扫频完成：{len(res)} 点，PM "
            f"{'—' if st.pm is None else f'{st.pm:.1f}°'}")

    # ================================================================ 读数
    def _reset_readout(self) -> None:
        self.sat_bar.setVisible(False)
        self.v_pm.set("—", theme.T3)
        self.v_gm.set("—", theme.T3)
        self.v_fc.set("—", theme.T3)
        self.kv_n.set_value("0 / —")
        self.kv_spc.set_value("—")
        self.kv_noise.set_value("—")

    def _update_readout(self, st=None) -> None:
        if st is None:
            st = analyze(self.result.freq, self.result.mag, self.result.phase)
        self.v_pm.set("—" if st.pm is None else f"{st.pm:.1f}",
                      theme.GREEN_T if st.pm_ok else
                      (theme.AMBER_T if st.pm is not None else theme.T3))
        self.v_gm.set("—" if st.gm is None else f"{st.gm:.1f}",
                      theme.GREEN_T if st.gm_ok else
                      (theme.AMBER_T if st.gm is not None else theme.T3))
        if st.fc is None:
            self.v_fc.set("—", theme.T3)
        elif st.fc >= 1000:
            self.v_fc.set(f"{st.fc/1000:.2f}", theme.T1)
        else:
            self.v_fc.set(f"{st.fc:.0f}", theme.T1)
        self._update_chips(st)
        self._update_quant_row()

    def _update_quant_row(self) -> None:
        """"估噪底" → 改成**幅值-量化对照**（01 §1.10 降级后的口径）。

        原来显示的是 20log10((A/2)/(LSB/√N)) 算出来的"预计 SNR"。
        那个数是编的：它假设噪声只有 ADC 量化、且与信号完全独立，
        真机上的开关纹波、ADC 参考噪声、注入路径失真全没算进去。
        显示一个看起来很精确、实际没有依据的 SNR，比不显示更危险。

        现在只报**能算准的量**：
            1 LSB 对应的 pu（12bit → 1/4096 = 0.024 %pu）
            当前注入幅值占几个 LSB
            以及一句"低于这个就别信"的门槛判断。
        """
        if self.result is None or len(self.result) == 0:
            self.kv_noise.set_value("—")
            return
        amp = self.result.amplitude_pu
        adc_bits = 12
        lsb = 1.0 / (1 << adc_bits)
        n_lsb = amp / lsb
        # 单周期峰值近似 = 幅值；量化可用性门槛：注入峰值 ≥ 10 LSB
        ok = n_lsb >= 10.0
        self.kv_noise.set_value(
            f"{n_lsb:.0f} LSB" if ok else f"{n_lsb:.0f} LSB · 偏低")
        self.kv_noise.set_tooltip(
            f"1 LSB = {lsb*100:.3f} %pu（{adc_bits} bit ADC）\n"
            f"注入 {amp*100:.2f} %pu = {n_lsb:.1f} LSB\n"
            f"经验门槛：< 10 LSB 时噪声会盖住谐振峰，建议加大幅值或提高精度档")

    def _update_chips(self, st) -> None:
        if st is None:
            self.chip_pm.setObjectName("ChipMute")
            self.chip_pm.setText("等待数据")
            self.chip_gm.setObjectName("ChipMute")
            self.chip_gm.setText("等待数据")
        else:
            if st.pm is None:
                self.chip_pm.setObjectName("ChipMute")
                self.chip_pm.setText("PM 不可信")
            elif st.pm > 45:
                self.chip_pm.setObjectName("ChipOk")
                self.chip_pm.setText("PM 充足")
            else:
                self.chip_pm.setObjectName("ChipWarn")
                self.chip_pm.setText("PM 偏低")
            if st.gm is None:
                self.chip_gm.setObjectName("ChipMute")
                self.chip_gm.setText("GM 不可信")
            elif st.gm > 6:
                self.chip_gm.setObjectName("ChipOk")
                self.chip_gm.setText("GM 充足")
            else:
                self.chip_gm.setObjectName("ChipWarn")
                self.chip_gm.setText("GM 偏低")
        for c in (self.chip_pm, self.chip_gm):
            c.style().unpolish(c)
            c.style().polish(c)

    def set_notes(self, items) -> None:
        for w in self._note_items:
            self._notes_lay.removeWidget(w)
            w.deleteLater()
        self._note_items.clear()
        for text, warn in items:
            n = Note(text, warn)
            self._notes_lay.addWidget(n)
            self._note_items.append(n)

    def _on_cursor(self, f: float, m: float, p: float) -> None:
        fs = f"{f/1000:.2f} kHz" if f >= 1000 else f"{f:.1f} Hz"
        self.lb_cursor.setText(f"{fs}   {m:+.1f} dB   {p:.1f}°")

    # ------------------------------------------------------------ 饱和（W2）
    def _refresh_sat(self) -> None:
        """按当前 result 刷新饱和提示条。无饱和则整条隐藏。"""
        res = self.result
        n = 0 if res is None else res.sat_count
        if not n:
            self.sat_bar.setVisible(False)
            return

        total = len(res)
        self.sat_bar.setVisible(True)
        if n >= total:
            tip = "本次扫频**全部频点**饱和，整趟数据不可用。"
            sug = "注入幅值整体偏大（或被测对象已进限幅/断续），请降低幅值重扫。"
        elif n > total * 0.3:
            tip = f"本次扫频有 {n}/{total} 点饱和（超过三成）。"
            sug = "多为幅值偏大，建议降低注入幅值重扫。"
        else:
            tip = f"本次扫频有 {n}/{total} 点饱和。"
            sug = "这几个点已从 PM/GM 计算中剔除；若要补测可只重扫该频段。"
        self.lb_sat.setText(f"⚠ {tip}{sug}")

    def _on_sat_detail(self) -> None:
        """列出饱和的频点，便于用户决定重扫哪一段。"""
        res = self.result
        if res is None or res.sat_count == 0:
            return
        m = res.sat_mask()
        f = res.freq[m]
        lines = [("f_act (Hz)", "|H| (dB)", "相位 (°)")]
        mag = res.mag[m]
        ph = res.phase[m]
        for i in range(len(f)):
            lines.append((f"{f[i]:.1f}", f"{mag[i]:+.2f}", f"{ph[i]:+.1f}"))
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"饱和频点（{len(f)} 个）")
        dlg.resize(340, 420)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(12, 12, 12, 12)
        header = QtWidgets.QLabel(
            "以下频点在采集时 STATUS.flags 的饱和位置位，\n"
            "测量值不反映真实线性增益，**未参与 PM/GM 计算**。")
        header.setWordWrap(True)
        header.setStyleSheet(f"color:{theme.T2}; font-size:11.5px;")
        lay.addWidget(header)
        table = QtWidgets.QTableWidget(len(f), 3)
        table.setHorizontalHeaderLabels(list(lines[0]))
        for r in range(len(f)):
            for c in range(3):
                it = QtWidgets.QTableWidgetItem(lines[r + 1][c])
                it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                table.setItem(r, c, it)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        lay.addWidget(table, 1)
        btn = QtWidgets.QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec_()

    def _on_plot_click(self, ev) -> None:
        if ev.double():
            self.plot.autoscale()

    def _update_ptext_idle(self) -> None:
        mode_name = "Loop Gain" if self.mode == MODE_LOOP else "Plant"
        self.lb_pstate.setText(f"{mode_name} · 就绪")

    # ================================================================ 预扫
    def _on_prescan(self) -> None:
        """在谐振附近粗扫，找峰值并据此下调注入幅值（02 文档 2.4）。"""
        freqs = self._freqs()
        if len(freqs) == 0:
            return
        # 用超快档只扫顶部 1/3 段，够找谐振峰
        sub = freqs[max(0, int(len(freqs) * 0.2)):]
        cfg = {
            "isr_freq": self.isr_freq,
            "mode": MODE_PLANT,
            "amp_pu": self.amp_pu,
            "cyc": sfra_sim.CYC_FAST,
            "freqs": sub,
            "dut": "lc" if self.seg_dut.current() == 0 else "first",
            "f0": self.in_f0c.value(),
            "fc": self.in_f0c.value(),
            "q": self.in_q.value(),
            "comp": {"kp": self.in_kp.value(), "ki": self.in_ki.value()},
        }

        self.btn_pre.setEnabled(False)
        self.btn_pre.setText("扫描中…")
        self.statusMessage.emit("预扫中…")

        # 用一次性 worker，结果直接收在本地
        from ..sim.engine import SimWorker
        collected = []
        w = SimWorker(cfg, self)

        def on_pt(p):
            from ..model.bode_data import mag_db, h_from_raw
            collected.append((p.f_act, mag_db(h_from_raw(p, MODE_PLANT))))

        def on_done():
            self.btn_pre.setEnabled(True)
            if not collected:
                self.btn_pre.setText("预扫")
                return
            f_peak, m_peak = max(collected, key=lambda x: x[1])
            # 峰值处把注入降到让 |H|·amp ≈ 0.2 pu
            target = 0.2
            new_amp = min(self.amp_pu,
                          target / (10 ** (m_peak / 20.0)))
            new_amp = max(new_amp, 0.001)
            self.in_amp.set_value(new_amp * 100.0)
            self.btn_pre.setText(f"峰值 {m_peak:+.1f} dB")
            self.statusMessage.emit(
                f"预扫完成：{f_peak:.0f} Hz 处 {m_peak:+.1f} dB，"
                f"幅值调至 {new_amp*100:.2f} %pu")
            self.set_notes([
                (f"预扫：{f_peak:.0f} Hz 峰值 {m_peak:+.1f} dB，"
                 f"注入已自动降至 {new_amp*100:.2f} %pu", False)])
            QtCore.QTimer.singleShot(2400, lambda: self.btn_pre.setText("预扫"))

        w.pointReady.connect(on_pt)
        w.finishedOk.connect(on_done)
        w.failed.connect(lambda m: (self.btn_pre.setEnabled(True),
                                    self.btn_pre.setText("预扫"),
                                    self.statusMessage.emit(f"预扫失败：{m}")))
        w.finished.connect(w.deleteLater)
        w.start()
        self._prescan_worker = w      # 保引用，别被 GC

    # ================================================================ 导出
    def _on_export(self) -> None:
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(theme.qss())
        for key, label in (("csv", "CSV · TI 列名"),
                           ("png", "PNG · 图表快照"),
                           ("json", "JSON · 完整会话"),
                           ("md", "Markdown · 测试报告")):
            act = menu.addAction(label)
            act.setData(key)
            act.triggered.connect(
                lambda _=False, k=key: self.exportRequested.emit(k))
        btn = self.btn_exp
        menu.exec_(btn.mapToGlobal(QtCore.QPoint(0, btn.height() + 4)))

    # ================================================================ 外部设置
    def current_result(self) -> Optional[SweepResult]:
        return self.result

    def curves_for_report(self):
        if self.result is None:
            return []
        return [(self.result.mode_name, self.result)]

    def meta(self) -> dict:
        return {
            "拓扑": "Buck（仿真二阶 LC）" if self.seg_dut.current() == 0
                    else "一阶 RC",
            "谐振/截止": f"{self.in_f0c.value():.0f} Hz",
            "ISR 频率": f"{self.isr_freq/1000:.1f} kHz",
            "扫频范围": f"{self.in_f0.value():.0f} Hz ~ "
                        f"{self.in_f1.value():.0f} Hz",
            "每 decade 点数": f"{int(self.in_ppd.value())}",
            "Kp / Ki": f"{self.in_kp.value():.3f} / {self.in_ki.value():.0f}",
        }
