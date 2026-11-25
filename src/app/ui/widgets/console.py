"""SFRA Studio - 串口日志面板（设计稿 `design/sfra-console-mockup.html` 的落地）。

放在 Tab1「测量」右列底部，可折叠。**不新建串口**——它复用
`LinkController` 手里那个 `BaseLink` 实例，只做"旁听 + 追加发送"。

三条设计红线（都是踩过坑才定下来的）：

1. **不抢协议字节**。
   `BaseLink.feed()` 解出来的二进制帧由协议层处理，本面板**不参与**
   分帧、不消费字节流。它只看已经解好的 `Frame`，以及 ASCII 文本
   （`logText` / `errorText`）。这样面板出任何 bug 都不会影响扫频。

2. **环形缓冲有上限**。
   长跑（一次扫频几十秒、反复扫一整天）时日志会无限增长，
   QPlainTextEdit 的 document 撑到几十万行后 `appendPlainText` 会明显掉帧。
   这里固定 `MAX_LINES`，超出丢弃最旧的。

3. **扫频中禁用发送**。
   扫频期间上位机要严格按 200 ms 节奏轮询 STATUS，任何额外帧都可能
   打乱时序（固件 500 ms 无有效帧就 ABORT，插一条命令进去会让
   `GET_DATA` 与 `GET_STATUS` 抢主循环的一次 poll）。所以扫描中
   发送框禁用，与设计稿"扫频进行中禁用发送框"一致。

关于"命令输入框"的**实现边界**（与设计稿的差异，已与用户确认）：

    设计稿里的快捷命令 `help` / `sfra` / `sfra cost` 假设固件有 ASCII
    shell。**当前固件没有** —— `sfra_proto.c` 只认二进制帧，
    `SOF=0xAA55` 之外的字节全被丢进 resync。所以这里不假装发 ASCII
    文本（那只会得到超时），而是发**协议命令**：`ping` / `caps` /
    `conditions` / `status` / `abort` / `coeff`。

    等固件真加了 shell，只需在 `_build_command()` 里加一条
    "裸文本走 ASCII 通道"的分支，本面板其余部分不用动。
"""
from __future__ import annotations

import time
from typing import List, Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from ... import theme
from ...comm import protocol as P
from .common import FlowLayout, fit_button

#: 环形缓冲上限。500 行 ≈ 中等高度滚动两屏多一些，够回溯一次故障现场。
MAX_LINES = 500

#: 日志行的种类（决定颜色，见 theme.qss 的 `#ConsoleLog` 系列选择器）
K_SYS = "sys"      # 系统提示（打开/关闭串口）
K_TX = "tx"        # 发出
K_RX = "rx"        # 收到
K_INFO = "info"    # 中性信息（由 sigLog 转来的状态文案）
K_ERR = "err"      # 错误

#: 过滤 chip 的种类顺序
FILTERS = (("全部", None), ("TX", K_TX), ("RX", K_RX),
           ("ERR", K_ERR), ("SYS", K_SYS))


class _Line:
    """一行日志。用轻量对象而不是 QListWidgetItem，
    因为过滤时只需要重排一个 list，不必操作 view。"""

    __slots__ = ("ts", "kind", "tag", "msg")

    def __init__(self, kind: str, tag: str, msg: str):
        #: 时间戳只到毫秒。串口问题经常要看"这两帧差了几毫秒"，
        #: 秒级精度不够（一个 200 ms 轮询周期内能有好几帧）。
        self.ts = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        self.kind = kind
        self.tag = tag
        self.msg = msg


class SerialConsole(QtWidgets.QFrame):
    """串口日志面板。

    对外只有几个方法：`append_*()` 系列、`bind()`、`set_scanning()`。
    主窗口 / TabMeasure 不需要知道内部结构。
    """

    #: 用户提交了一条命令（已解析成 cmd + payload），由外部决定发给谁
    commandSubmitted = QtCore.pyqtSignal(int, bytes)
    #: 用户点了"清空"
    cleared = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Console")
        self._lines: List[_Line] = []
        self._filter: Optional[str] = None
        self._counts = {K_SYS: 0, K_TX: 0, K_RX: 0, K_INFO: 0, K_ERR: 0}
        self._folded = False
        self._autoscroll = True
        self._scanning = False
        self._link_open = False
        self._port_text = "未连接"
        #: 是否显示 TX 方向。默认关：一次扫频会发几十次 GET_DATA/FETCH，
        #: 全记下来会把真正有用的应答挤掉（用户要的是"它回了什么"）。
        #: 打开后能看到完整收发对照，用于查时序问题时必开。
        self._show_tx = False
        #: 命令历史（↑↓ 翻）
        self._hist: List[str] = []
        self._hist_pos = 0

        self._build()
        self._append(K_SYS, "SYS", "日志面板就绪")

    # ================================================================ 构建
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_head())
        #: 折叠时整体隐藏的容器
        self._body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(self._body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(self._build_filters())
        bl.addWidget(self._build_log(), 1)
        bl.addWidget(self._build_quick())
        bl.addWidget(self._build_input())
        outer.addWidget(self._body, 1)

    def _build_head(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setObjectName("ConsoleHead")
        w.setCursor(QtCore.Qt.PointingHandCursor)
        w.mousePressEvent = self._on_head_clicked        # type: ignore[assignment]

        lay = QtWidgets.QHBoxLayout(w)
        # 边距压到 9/6（原 11/8）：右栏只有 268px，标题栏里
        # 「串口日志 + 端口 + 计数 + 两个图标」是硬需求，边距是软需求。
        # 这 4px 正好补上 QToolButton 的 minimumSizeHint 余量。
        lay.setContentsMargins(9, 7, 6, 7)
        lay.setSpacing(5)

        self._caret = QtWidgets.QLabel("▾")
        self._caret.setObjectName("ConsoleCaret")
        self._caret.setFixedWidth(9)
        lay.addWidget(self._caret)

        self._lb_title = QtWidgets.QLabel("串口日志")
        self._lb_title.setObjectName("ConsoleTitle")
        lay.addWidget(self._lb_title)

        self._dot = QtWidgets.QLabel("●")
        self._dot.setObjectName("ConsoleDot")
        self._dot.setFixedWidth(10)
        lay.addWidget(self._dot)

        # **端口名是可压缩的**：标题栏宽度只有 240px，
        # 「串口日志」+ 端口 + 计数 + 两个图标按钮加起来要 258px，
        # 超出的部分会把整个面板撑到比右栏还宽（然后被裁掉）。
        # 端口名是这里唯一"信息可以退让"的元素——把最小宽度设小，
        # 由 Qt 用省略号截短，完整值始终在 tooltip 里。
        self._lb_port = QtWidgets.QLabel("未连接")
        self._lb_port.setObjectName("ConsolePort")
        self._lb_port.setMinimumWidth(24)
        self._lb_port.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                    QtWidgets.QSizePolicy.Preferred)
        lay.addWidget(self._lb_port, 1)

        self._lb_cnt = QtWidgets.QLabel("0 行")
        self._lb_cnt.setObjectName("ConsoleCount")
        lay.addWidget(self._lb_cnt)

        self._btn_pause = QtWidgets.QToolButton()
        self._btn_pause.setText("⏸")
        self._btn_pause.setObjectName("IconBtn")
        self._btn_pause.setToolTip("暂停自动滚动（往上翻日志时建议先暂停）")
        self._btn_pause.setCheckable(True)
        self._btn_pause.clicked.connect(self._on_pause)
        _fix_icon_btn(self._btn_pause)
        lay.addWidget(self._btn_pause)

        self._btn_clear = QtWidgets.QToolButton()
        self._btn_clear.setText("🗑")
        self._btn_clear.setObjectName("IconBtn")
        self._btn_clear.setToolTip("清空日志")
        self._btn_clear.clicked.connect(self.clear)
        _fix_icon_btn(self._btn_clear)
        lay.addWidget(self._btn_clear)

        return w

    def _build_filters(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setObjectName("ConsoleFilt")
        # **流式布局**：面板只有 242px，5 个 chip + 「显示TX」用
        # QHBoxLayout 会被均分到 34px，而 `全部 128` 需要 40px、
        # `SYS 128` 需要 50px —— 文字直接被截掉（用户反馈的问题）。
        # 流式布局放不下就换行，窗口怎么缩都不会压扁文字。
        lay = FlowLayout(w, margin=0, hspacing=4, vspacing=4)
        lay.setContentsMargins(9, 6, 9, 6)

        self._chips = {}
        for text, kind in FILTERS:
            b = QtWidgets.QPushButton(text)
            b.setObjectName("FChip")
            b.setCheckable(True)
            b.setChecked(kind is None)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=kind: self._set_filter(k))
            fit_button(b, pad_x=7, pad_y=1)
            lay.addWidget(b)
            self._chips[kind] = b

        # 单独的"显示发出帧"开关。默认关（见 `_show_tx` 说明）：
        # 扫频期间 GET_STATUS/GET_DATA 每 200 ms 一次，全记会把应答冲光。
        self._btn_tx = QtWidgets.QPushButton("显示TX")
        self._btn_tx.setObjectName("FChip")
        self._btn_tx.setCheckable(True)
        self._btn_tx.setChecked(False)
        self._btn_tx.setCursor(QtCore.Qt.PointingHandCursor)
        self._btn_tx.setToolTip(
            "把发出的帧也记进日志（扫频中会很多，查时序问题时再开）")
        self._btn_tx.clicked.connect(self._on_toggle_tx)
        fit_button(self._btn_tx, pad_x=7, pad_y=1)
        lay.addWidget(self._btn_tx)
        return w

    def _on_toggle_tx(self) -> None:
        self._show_tx = self._btn_tx.isChecked()
        if self._show_tx:
            self._append(K_SYS, "SYS", "开始记录发出的帧（TX）")

    def _build_log(self) -> QtWidgets.QWidget:
        # 用 QTextEdit 而不是 QPlainTextEdit：日志行要分色（TX 蓝 / RX 绿 /
        # ERR 红 / SYS 灰），纯文本只能有一个颜色。代价是 append 要走 HTML，
        # 但 500 行封顶，开销可忽略。
        self._log = QtWidgets.QTextEdit()
        self._log.setObjectName("ConsoleLog")
        self._log.setReadOnly(True)
        self._log.setAcceptRichText(True)
        self._log.setLineWrapMode(QtWidgets.QTextEdit.WidgetWidth)
        self._log.setMinimumHeight(120)
        self._log.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._log.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._log.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        #: 行数据与文档段同步维护，过滤重排时整段重建
        self._doc_ok = True
        return self._log

    def _build_quick(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setObjectName("ConsoleFilt")
        # 同样用流式布局：`conditions` 要 100px，六个按钮一行放不下，
        # QHBoxLayout 会把它们均分到 33px（字被截掉 2/3）。
        lay = FlowLayout(w, margin=0, hspacing=4, vspacing=4)
        lay.setContentsMargins(9, 0, 9, 7)

        # 这些是**真实存在的**协议命令（03 §3.3 命令表）。
        # 设计稿里的 help / sfra cost 属于固件还没实现的 ASCII shell，
        # 硬做出来只会超时，先不挂（见模块 docstring）。
        #
        # 按钮上显示的是**短名**：`conditions` 一个字就要 188px，占面板
        # 242px 的 78%，流式布局虽然放得下但会把整行挤满。短名 + tooltip
        # 说明完整命令，用户也可以直接在输入框敲完整名（`parse_command`
        # 两种都认）。
        for label, name, tip in (
                ("ping", "ping", "发 PING，看固件版本与能力位"),
                ("caps", "caps", "读 CAPS：ISR 频率 / 最大点数"),
                ("cond", "conditions", "读工作点：Vin/Vout/Iout/温度"
                                       "（完整命令：conditions）"),
                ("status", "status", "读一次 STATUS（扫频中用，会干扰轮询）"),
                ("coeff", "coeff", "读回补偿器系数"),
                ("abort", "abort", "发 ABORT 中止扫频"),
        ):
            b = QtWidgets.QPushButton(label)
            b.setObjectName("QBtn")
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, n=name: self._submit(n))
            fit_button(b, pad_x=8, pad_y=1)
            lay.addWidget(b)
        return w

    def _build_input(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setObjectName("ConsoleIn")
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(9, 6, 9, 7)
        lay.setSpacing(6)

        p = QtWidgets.QLabel("›")
        p.setObjectName("ConsolePrompt")
        lay.addWidget(p)

        self._in = _HistLineEdit()
        self._in.setObjectName("ConsoleInput")
        self._in.setPlaceholderText("输入命令回车发送（↑↓ 翻历史）")
        self._in.returnPressed.connect(self._on_send)
        self._in.histPrev.connect(self._on_hist_prev)
        self._in.histNext.connect(self._on_hist_next)
        lay.addWidget(self._in, 1)

        self._btn_send = QtWidgets.QPushButton("发送")
        self._btn_send.setObjectName("SendBtn")
        self._btn_send.clicked.connect(self._on_send)
        # 发送按钮宽度也按文本算，别让"发送"两个字被压成"发"。
        fit_button(self._btn_send, pad_x=11, pad_y=3)
        lay.addWidget(self._btn_send)

        return w

    # ================================================================ 折叠
    def _on_head_clicked(self, ev) -> None:
        # 点按钮时不折叠（按钮的 press 也会冒泡到头部）
        if ev.button() != QtCore.Qt.LeftButton:
            return
        self.set_folded(not self._folded)

    def set_folded(self, folded: bool) -> None:
        self._folded = bool(folded)
        self._body.setVisible(not self._folded)
        self._caret.setText("▸" if self._folded else "▾")
        if self._folded:
            # 折叠态强调"有错误"这件事：标题栏是唯一还看得到的地方
            self._refresh_count()

    def is_folded(self) -> bool:
        return self._folded

    # ================================================================ 过滤
    def _set_filter(self, kind: Optional[str]) -> None:
        self._filter = kind
        for k, b in self._chips.items():
            b.setChecked(k == kind)
        self._repaint()

    # ================================================================ 追加
    def _append(self, kind: str, tag: str, msg: str) -> None:
        line = _Line(kind, tag, msg)
        self._lines.append(line)
        self._counts[kind] = self._counts.get(kind, 0) + 1
        if len(self._lines) > MAX_LINES:
            dropped = self._lines[:len(self._lines) - MAX_LINES]
            self._lines = self._lines[-MAX_LINES:]
            for d in dropped:
                self._counts[d.kind] = max(0, self._counts.get(d.kind, 0) - 1)
        if self._visible(line):
            self._append_to_view(line)
        self._refresh_count()

    def _visible(self, line: _Line) -> bool:
        if self._filter is None:
            return True
        return line.kind == self._filter

    def _append_to_view(self, line: _Line) -> None:
        sb = self._log.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4

        cur = QtGui.QTextCursor(self._log.document())
        cur.movePosition(QtGui.QTextCursor.End)
        _insert_line(cur, line)

        if self._autoscroll and at_bottom:
            sb.setValue(sb.maximum())

    def _repaint(self) -> None:
        """过滤变化后整体重排。

        500 行以内直接重建最快也最不容易错。**必须用
        `beginEditBlock()` 包起来**：逐行插入会让 QTextEdit 每行都做一次
        布局+重绘，实测 500 行要 3.3 秒；包成一个编辑块后只重排一次。
        """
        doc = self._log.document()
        doc.clear()
        cur = QtGui.QTextCursor(doc)
        cur.beginEditBlock()
        first = True
        for line in self._lines:
            if not self._visible(line):
                continue
            if not first:
                cur.insertBlock()
            _insert_line(cur, line, new_block=False)
            first = False
        cur.endEditBlock()
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_count(self) -> None:
        n = len(self._lines)
        err = self._counts.get(K_ERR, 0)
        if self._folded and err:
            self._lb_cnt.setText(f"{n} 行 · {err} 错误")
            self._lb_cnt.setProperty("hasErr", True)
        else:
            self._lb_cnt.setText(f"{n} 行")
            self._lb_cnt.setProperty("hasErr", False)
        self._lb_cnt.style().unpolish(self._lb_cnt)
        self._lb_cnt.style().polish(self._lb_cnt)

        # 过滤 chip 上的角标（设计稿 `<b>128</b>`）
        for k, b in self._chips.items():
            base = b.text().split()[0]
            cnt = n if k is None else self._counts.get(k, 0)
            new = base if (k is not None and cnt == 0) else f"{base} {cnt}"
            if b.text() != new:
                b.setText(new)
                # **文字变了必须重算宽度**。计数从 1 位涨到 3 位时
                # （`全部 1` → `全部 128`）会多出约 20px，不重算就会截断。
                fit_button(b, pad_x=7, pad_y=1)
                # 流式布局的高度依赖换行数，宽度变了要让父布局重新算
                b.parentWidget().updateGeometry()

    # ---- 对外追加接口 ----
    def log_sys(self, msg: str) -> None:
        self._append(K_SYS, "SYS", msg)

    def log_info(self, msg: str) -> None:
        self._append(K_INFO, "INF", msg)

    def log_tx(self, msg: str) -> None:
        self._append(K_TX, "TX", msg)

    def log_rx(self, msg: str) -> None:
        self._append(K_RX, "RX", msg)

    def log_err(self, msg: str) -> None:
        self._append(K_ERR, "ERR", msg)
        # 错误发生时无论折叠与否都该被看见：折叠态下计数变红提醒
        if self._folded:
            self._refresh_count()

    def log_frame_out(self, cmd: int, payload: bytes, is_poll: bool) -> None:
        """链路发出了一帧（`BaseLink.frameOut`）。

        默认只记**非轮询**的帧：`SET_CONFIG` / `START` / `ABORT` 这类
        "人发起的动作"是一次扫频的骨架，没有它们日志读不出节奏。
        而 `GET_STATUS` / `GET_DATA` 是 200 ms 一次的内部轮询，全记会
        淹没真正想看的应答——但它们又是查时序问题的关键，所以留了
        「显示TX」开关，开了就全都记。
        """
        if is_poll and not self._show_tx:
            return
        try:
            name = P.Cmd(cmd).name
        except ValueError:
            name = f"0x{cmd:02X}"
        extra = ""
        if cmd == P.Cmd.GET_DATA and len(payload) >= 3:
            start = int.from_bytes(payload[0:2], "little")
            extra = f" idx={start} n={payload[2]}"
        self._append(K_TX, "TX", f"→ {name}{extra}")

    def clear(self) -> None:
        self._lines.clear()
        for k in self._counts:
            self._counts[k] = 0
        self._log.clear()
        self._refresh_count()
        self.cleared.emit()

    # ================================================================ 状态
    def set_port(self, text: str, open_: bool) -> None:
        """更新标题栏的"端口 · 波特率"与状态点。

        端口名在窄面板下会被 Qt 用省略号截短（见 `_build_head` 的说明），
        所以**完整值必须同时写进 tooltip**，否则用户就再也读不到波特率了。
        """
        self._port_text = text
        self._link_open = bool(open_)
        self._lb_port.setText(text)
        self._lb_port.setToolTip(text)
        self._dot.setProperty("on", bool(open_))
        self._dot.style().unpolish(self._dot)
        self._dot.style().polish(self._dot)

    def set_scanning(self, scanning: bool) -> None:
        """扫频中禁用发送：见模块 docstring 第 3 条。"""
        self._scanning = bool(scanning)
        en = not self._scanning
        self._in.setEnabled(en)
        self._btn_send.setEnabled(en)
        self._in.setPlaceholderText(
            "扫频进行中，发送已禁用（避免打断 200 ms 轮询）"
            if self._scanning else
            "输入命令回车发送（↑↓ 翻历史）")

    # ================================================================ 命令
    def _on_send(self) -> None:
        text = self._in.text().strip()
        if not text:
            return
        self._in.clear()
        if not self._hist or self._hist[-1] != text:
            self._hist.append(text)
        self._hist_pos = len(self._hist)
        self._submit(text)

    def _submit(self, text: str) -> None:
        if self._scanning:
            self.log_err("扫频进行中，命令已忽略")
            return
        parsed = parse_command(text)
        if parsed is None:
            self.log_err(f"未知命令：{text}（可用：{', '.join(COMMANDS)}）")
            return
        cmd, payload = parsed
        self.log_tx(f"→ {text}")
        self.commandSubmitted.emit(int(cmd), payload)

    def _on_hist_prev(self) -> None:
        if not self._hist:
            return
        self._hist_pos = max(0, self._hist_pos - 1)
        self._in.setText(self._hist[self._hist_pos])

    def _on_hist_next(self) -> None:
        if not self._hist:
            return
        self._hist_pos = min(len(self._hist), self._hist_pos + 1)
        self._in.setText("" if self._hist_pos >= len(self._hist)
                         else self._hist[self._hist_pos])

    def _on_pause(self) -> None:
        self._autoscroll = not self._btn_pause.isChecked()
        self._btn_pause.setToolTip(
            "继续自动滚动" if self._btn_pause.isChecked()
            else "暂停自动滚动（往上翻日志时建议先暂停）")


# ---------------------------------------------------------------- 图标按钮
def _fix_icon_btn(btn: QtWidgets.QToolButton, size: int = 20) -> None:
    """把图标按钮钉死成方形，并且**真正压住它的 minimumSizeHint**。

    只调 `setFixedSize()` 是不够的：QSS 里 `QToolButton#IconBtn` 的
    `padding` 会让 `minimumSizeHint()` 仍然返回 36px，而布局是照
    `minimumSizeHint` 分配空间的——于是标题栏会多出十几像素的需求，
    把最右侧的图标挤出去（面板整体被撑宽后又被父容器裁掉）。

    显式 `setMinimumSize` 才能让布局相信"这个按钮只要 20px"。
    """
    btn.setFixedSize(size, size)
    btn.setMinimumSize(size, size)
    btn.setMaximumSize(size, size)
    btn.setSizePolicy(QtWidgets.QSizePolicy.Fixed,
                      QtWidgets.QSizePolicy.Fixed)


# ---------------------------------------------------------------- 行渲染#: 每类日志的 (时间戳色, 标签色, 正文色)。取自 theme。
_COLORS = {
    K_SYS:  (theme.T3, theme.T3, theme.T3),
    K_INFO: (theme.T3, theme.T3, theme.T2),
    K_TX:   (theme.T3, theme.BLUE, "#0059b3"),
    K_RX:   (theme.T3, theme.GREEN, theme.GREEN_T),
    K_ERR:  (theme.T3, theme.RED, "#c0271d"),
}

#: 标签列宽（按字符对齐）。用不换行空格补位，保证 TX/RX/ERR 正文左边缘齐。
_TAG_PAD = 3

#: 格式对象按 kind 缓存。`QTextCharFormat` 构造有开销，
#: 而一次扫频要插几千行——每行新建 3 个对象会让日志明显拖慢扫频。
_FMT_CACHE: dict = {}


def _fmt(color: str, bold: bool = False) -> QtGui.QTextCharFormat:
    key = (color, bold)
    f = _FMT_CACHE.get(key)
    if f is None:
        f = QtGui.QTextCharFormat()
        f.setForeground(QtGui.QColor(color))
        f.setFontFamily("Consolas")
        f.setFontPointSize(8)
        if bold:
            f.setFontWeight(QtGui.QFont.DemiBold)
        _FMT_CACHE[key] = f
    return f


def _insert_line(cur: QtGui.QTextCursor, line: "_Line",
                 new_block: bool = True) -> None:
    """把一行日志插到 cursor 当前位置。

    为什么不用 `insertHtml`：HTML 解析 + 富文本布局在两三千行时是
    主要开销（实测 28 ms/行）。直接按段插入带格式的纯文本快两个数量级。
    """
    ts_c, tag_c, msg_c = _COLORS.get(line.kind, _COLORS[K_INFO])

    cur.insertText(line.ts, _fmt(ts_c))
    cur.insertText(" ", _fmt(ts_c))
    cur.insertText(line.tag.ljust(_TAG_PAD, "\u00a0"), _fmt(tag_c, bold=True))
    cur.insertText(" ", _fmt(ts_c))
    cur.insertText(line.msg, _fmt(msg_c))
    if new_block:
        # 另起一段：QTextEdit 的自动换行在段内生效，段间不换行
        cur.insertBlock()


# ---------------------------------------------------------------- 输入框
class _HistLineEdit(QtWidgets.QLineEdit):
    """把 ↑↓ 变成"翻历史"信号，而不是默认的光标移动。

    QLineEdit 的 ↑↓ 默认是"跳到行首/行尾"（单行模式下几乎无用），
    借来处理命令历史，符合设计稿"↑↓ 翻历史"的提示。
    """

    histPrev = QtCore.pyqtSignal()
    histNext = QtCore.pyqtSignal()

    def keyPressEvent(self, ev: QtGui.QKeyEvent) -> None:  # noqa: N802
        if ev.key() == QtCore.Qt.Key_Up:
            self.histPrev.emit()
            return
        if ev.key() == QtCore.Qt.Key_Down:
            self.histNext.emit()
            return
        super().keyPressEvent(ev)


# ---------------------------------------------------------------- 命令解析
#: 命令名 → (Cmd, 说明)。只放**固件真实支持**的命令（03 §3.3）。
COMMANDS = {
    "ping": (P.Cmd.PING, b""),
    "caps": (P.Cmd.GET_CAPS, b""),
    "conditions": (P.Cmd.GET_CONDITIONS, b""),
    "status": (P.Cmd.GET_STATUS, b""),
    "coeff": (P.Cmd.GET_COEFF, b""),
    "abort": (P.Cmd.ABORT, b""),
}


def parse_command(text: str) -> Optional[tuple]:
    """把用户输入的一行解析成 (cmd, payload)；不认识的返回 None。

    支持 `0x06` 这种直接写命令号的形式（调试时有用），
    也支持 `get_status` 这种下划线写法。
    """
    t = text.strip().lower()
    if not t:
        return None
    if t in COMMANDS:
        return COMMANDS[t]
    # 别名：允许写枚举名
    try:
        return P.Cmd[t.upper()], b""
    except KeyError:
        pass
    # 直接写数字
    if t.startswith("0x"):
        try:
            v = int(t, 16)
        except ValueError:
            return None
        if 0 < v < 0x80:
            return v, b""
    return None


# ---------------------------------------------------------------- 帧渲染
def describe_frame(f, n_points_total: int = 0) -> Optional[tuple]:
    """把收到的一帧渲染成 (kind, 文本)。返回 None 表示这一帧不记日志。

    **DATA 帧不记**：一次扫频有几十帧 DATA，全记会把有意义的信息冲掉，
    而且点数据已经画在图上了，没有回读价值。这与设计稿"不加噪声"一致。
    """
    cmd = f.cmd
    if cmd == P.Cmd.DATA:
        return None

    name = f.cmd_name
    if cmd == P.Cmd.ACK:
        if len(f.payload) < 2:
            return (K_RX, "← ACK (payload 过短)")
        req, err = f.payload[0], f.payload[1]
        detail = f.payload[2] if len(f.payload) >= 3 else 0
        req_name = P.Cmd(req).name if req < 0x80 else f"0x{req:02X}"
        if err == P.ErrCode.OK:
            return (K_RX, f"← ACK {req_name}")
        return (K_ERR,
                f"← NAK {req_name}：{P.ErrCode.name_of(err)}"
                + (f"（detail={detail}）" if detail else ""))

    if cmd == P.Cmd.NACK:
        if len(f.payload) >= 2:
            req, err = f.payload[0], f.payload[1]
            return (K_ERR,
                    f"← NACK 0x{req:02X}：{P.ErrCode.name_of(err)}")
        return (K_ERR, "← NACK")

    if cmd == P.Cmd.PING_ACK:
        r = P.unpack_ping_ack(f.payload)
        if r is None:
            return (K_RX, f"← {name} (解包失败)")
        ver, proto, caps = r
        return (K_RX, f"← {name} fw=v{ver} proto=v{proto} caps=0x{caps:02X}")

    if cmd == P.Cmd.CAPS:
        c = P.Caps.unpack(f.payload)
        if c is None:
            return (K_RX, f"← {name} (解包失败)")
        return (K_RX,
                f"← {name} fs={c.isr_freq:.0f} maxlen={c.max_vec_len} "
                f"maxppd={c.max_pts_per_dec}")

    if cmd == P.Cmd.STATUS:
        st = P.StatusInfo.unpack(f.payload)
        if st is None:
            return (K_RX, f"← {name} (解包失败)")
        tot = f"/{n_points_total}" if n_points_total else ""
        return (K_RX,
                f"← {name} {P.State.name_of(st.state)} idx={st.freq_index}{tot} "
                f"f={st.cur_f_act:.1f}Hz flags=0x{st.flags:02X}")

    if cmd == P.Cmd.COEFF:
        co = P.unpack_coeff(f.payload)
        if co is None:
            return (K_RX, f"← {name} (解包失败)")
        return (K_RX, "← COEFF " + " ".join(f"{v:+.4g}" for v in co))

    if cmd == P.Cmd.CONDITIONS:
        d = P.unpack_conditions(f.payload)
        if d is None:
            return (K_RX, f"← {name} (解包失败)")
        pretty = " ".join(f"{k}={v:.3g}" if isinstance(v, float) else f"{k}={v}"
                          for k, v in d.items())
        return (K_RX, f"← {name} {pretty}")

    # 其它（含未定义的应答号）：只报命令号与长度
    return (K_RX, f"← {name} ({len(f.payload)}B)")
