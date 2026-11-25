"""SFRA Studio - 链路层（串口 + 假 MCU 管道）。

两种后端共用同一套"发命令 → 收帧 → 分发信号"的语义，界面不需要区分：

    SerialLink  : 真串口（QSerialPort，readyRead 事件驱动）
    SimMcuLink  : S3 阶段的本机回环，把 C 端假 MCU 当子进程，
                  用 stdin/stdout 搬字节（协议逐位相同）

**为什么 SimMcuLink 值得单独做一套**：
    它跑的是同一份 `sfra_core.c` + `sfra_proto.c`，只是字节流换了介质。
    协议实现、状态机、扫频算法的 bug 会在这里就暴露，不必等真板子。
    S3 的全部 DoD 都靠它验收。

线程模型：全部在主线程 + Qt 事件驱动。921600 下一个频点只有 20 字节，
不需要子线程；SimMcuLink 用 QProcess 的 readyReadStandardOutput 信号，
与串口的事件模型完全一致，所以两条路径的时序行为也接近。
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import List, Optional

from PyQt5 import QtCore

from .protocol import (Caps, Cmd, DataChunk, ErrCode, Frame, FrameParser,
                       StatusInfo, build_frame, pack_config, pack_get_data,
                       unpack_conditions, unpack_ping_ack)

#: 工程根（本文件在 src/app/comm/ 下）
ROOT = Path(__file__).resolve().parents[3]


# ================================================================= 基类
class BaseLink(QtCore.QObject):
    """收发语义的公共部分：组帧、解帧、超时重发、信号分发。

    子类只需实现 `_write_bytes()` / `_open_io()` / `_close_io()`。
    """

    connected = QtCore.pyqtSignal(str)               # 描述串
    disconnected = QtCore.pyqtSignal()
    frameIn = QtCore.pyqtSignal(object)              # Frame
    #: 发出去的帧（纯旁听，只给日志面板用）。带 `wait_ack` 与 `is_poll`
    #: 两个布尔，面板据此决定"这条要不要显示"——200 ms 的 GET_STATUS
    #: 轮询如果全记，会把有用信息冲掉。
    frameOut = QtCore.pyqtSignal(int, bytes, bool)   # cmd, payload, is_poll
    ackReceived = QtCore.pyqtSignal(int, int, int)   # req_cmd, err, detail
    statusUpdate = QtCore.pyqtSignal(object)         # StatusInfo
    dataChunk = QtCore.pyqtSignal(object)            # DataChunk
    capsReceived = QtCore.pyqtSignal(object)         # Caps
    pingAck = QtCore.pyqtSignal(int, int, int)       # ver, proto, cap_bits
    coeffReceived = QtCore.pyqtSignal(list)
    conditionsReceived = QtCore.pyqtSignal(dict)
    errorText = QtCore.pyqtSignal(str)
    logText = QtCore.pyqtSignal(str)

    #: 单命令应答超时（03 文档 §3.4：500 ms，重发 3 次）
    ACK_TIMEOUT_MS = 500
    MAX_RETRY = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parser = FrameParser()
        self._pending: List[bytes] = []
        self._pending_cmd: int = -1
        self._retry = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.ACK_TIMEOUT_MS)
        self._timer.timeout.connect(self._on_timeout)
        self.rx_bytes = 0
        self.tx_bytes = 0

    # ---- 子类实现 ----
    def _write_bytes(self, data: bytes) -> bool:
        raise NotImplementedError

    def is_open(self) -> bool:
        raise NotImplementedError

    def open(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        self._timer.stop()
        self._pending.clear()

    # ---- 发送 ----
    def send(self, cmd: int, payload: bytes = b"", wait_ack: bool = True) -> None:
        if not self.is_open():
            self.errorText.emit("链路未连接")
            return
        frame = build_frame(cmd, payload)
        if not self._write_bytes(frame):
            self.errorText.emit(f"发送 0x{cmd:02X} 失败")
            return
        self.tx_bytes += len(frame)
        # 旁听通知（日志面板）。**必须在 write 成功之后**：发失败的帧
        # 如果先报出去，日志里会出现"发了"但线上没有的记录。
        # `is_poll` 让面板能把高频轮询折叠掉。
        self.frameOut.emit(int(cmd), payload, not wait_ack)
        if wait_ack:
            # 上一笔未完成就覆盖：串口是全双工的，但一个命令序列里
            # 同时挂两个待答命令本身就是调用方的错。覆盖而不是排队，
            # 这样"卡住的旧命令"不会永远占着重发定时器。
            self._pending = [frame]
            self._pending_cmd = cmd
            self._retry = 0
            self._timer.start()

    def _on_timeout(self) -> None:
        if not self._pending:
            return
        if self._retry >= self.MAX_RETRY:
            self.errorText.emit(
                f"命令 0x{self._pending_cmd:02X} 无应答"
                f"（重发 {self.MAX_RETRY} 次），请检查连接")
            self._pending.clear()
            self._pending_cmd = -1
            return
        self._retry += 1
        self.logText.emit(
            f"重发 0x{self._pending_cmd:02X}（第 {self._retry} 次）")
        self._write_bytes(self._pending[0])
        self._timer.start()

    # ---- 接收 ----
    def feed(self, data: bytes) -> None:
        self.rx_bytes += len(data)
        for f in self.parser.feed(data):
            self._dispatch(f)

    def _dispatch(self, f: Frame) -> None:
        self.frameIn.emit(f)

        # ---- 先做"待答命令已完成"的判定 ----
        # 03 文档：应答 CMD = 请求 | 0x80。但 ACK(0x80) 是通用应答，
        # 它的 payload 首字节才是被应答的请求号。所以分两种情况：
        #   f.cmd == 0x80 → 看 payload[0]
        #   f.cmd == req|0x80 → 直接对上
        # 少了这一步的表现就是"每帧都收到、却还在重发"，因为重发定时器
        # 只有 ACK 分支会停——PING/CAPS/STATUS/DATA 应答永远停不掉它。
        req_cmd = -1
        if f.cmd == Cmd.ACK:
            if len(f.payload) >= 1:
                req_cmd = f.payload[0]
        elif f.cmd == Cmd.NACK:
            if len(f.payload) >= 1:
                req_cmd = f.payload[0]
        elif f.cmd & 0x80:
            req_cmd = f.cmd & 0x7F

        if req_cmd >= 0 and req_cmd == self._pending_cmd:
            self._pending.clear()
            self._timer.stop()
            self._pending_cmd = -1

        if f.cmd == Cmd.ACK:
            # ACK: req_cmd(u8) err(u8) [detail(u8)]，另带一个 reserved（03 §3.3）
            if len(f.payload) < 2:
                return
            req, err = f.payload[0], f.payload[1]
            detail = f.payload[2] if len(f.payload) >= 3 else 0
            self.ackReceived.emit(req, err, detail)
            if err != ErrCode.OK:
                self.errorText.emit(
                    f"MCU 拒绝 0x{req:02X}：{ErrCode.name_of(err)}"
                    + (f"（detail={detail}）" if detail else ""))

        elif f.cmd == Cmd.PING_ACK:
            r = unpack_ping_ack(f.payload)
            if r is not None:
                self.pingAck.emit(*r)

        elif f.cmd == Cmd.CAPS:
            c = Caps.unpack(f.payload)
            if c is not None:
                self.capsReceived.emit(c)

        elif f.cmd == Cmd.STATUS:
            st = StatusInfo.unpack(f.payload)
            if st is not None:
                self.statusUpdate.emit(st)

        elif f.cmd == Cmd.DATA:
            ch = DataChunk.unpack(f.payload)
            if ch is not None:
                self.dataChunk.emit(ch)

        elif f.cmd == Cmd.COEFF:
            from .protocol import unpack_coeff
            co = unpack_coeff(f.payload)
            if co is not None:
                self.coeffReceived.emit(co)

        elif f.cmd == Cmd.CONDITIONS:
            d = unpack_conditions(f.payload)
            if d is not None:
                self.conditionsReceived.emit(d)

    # ---- 便捷命令 ----
    def ping(self) -> None:
        self.send(Cmd.PING)

    def get_caps(self) -> None:
        self.send(Cmd.GET_CAPS)

    def set_config(self, freq_start: float, freq_step: float,
                   amplitude: float, vec_len: int,
                   cycles_mode: int, mode: int) -> None:
        self.send(Cmd.SET_CONFIG,
                  pack_config(freq_start, freq_step, amplitude,
                              vec_len, cycles_mode, mode))

    def start(self) -> None:
        self.send(Cmd.START)

    def abort(self) -> None:
        self.send(Cmd.ABORT)

    def get_status(self) -> None:
        # 状态轮询不占用重发通道：STATUS 的应答也是无条件返回的，
        # 但高频轮询会不断重置重发定时器，把真正的命令超时掩盖掉。
        self.send(Cmd.GET_STATUS, wait_ack=False)

    def get_data(self, start_idx: int, count: int) -> None:
        self.send(Cmd.GET_DATA, pack_get_data(start_idx, count),
                  wait_ack=False)

    def set_coeff(self, coeffs: List[float]) -> None:
        from .protocol import pack_coeff
        self.send(Cmd.SET_COEFF, pack_coeff(coeffs))

    def get_coeff(self) -> None:
        """读回 MCU 中当前生效的系数（应答为 COEFF 帧，同 SET_COEFF 回显）。"""
        self.send(Cmd.GET_COEFF)

    def get_conditions(self) -> None:
        self.send(Cmd.GET_CONDITIONS)


# ================================================================= 假 MCU
class SimMcuLink(BaseLink):
    """把 C 端假 MCU 跑成子进程，用管道搬协议字节。

    为什么不是纯 Python 假 MCU：
        只有跑真 C 代码才能验证 `sfra_proto.c` —— 而它才是要塞进
        MCU 的那一份。Python 版只能验 PC 侧，等于一半没验。
    """

    #: 可执行文件位置（`tools/build_sim_mcu.py` 或 cross_check 构建）
    EXE = ROOT / "tools" / "sfra_sim_mcu.exe"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: Optional[QtCore.QProcess] = None
        self._extra_args: List[str] = []

    def set_args(self, *args: str) -> None:
        """额外命令行参数，例如 `--plant lp --isr 50000`。"""
        self._extra_args = list(args)

    @classmethod
    def build(cls, force: bool = False) -> Path:
        """编译假 MCU（需要 x86 gcc）。

        复用 `tools/cross_check.py` 的找 gcc 逻辑，不再抄一遍。
        """
        import sys
        tools_dir = ROOT / "tools"
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        import cross_check as cc          # noqa: PLC0415

        exe = cls.EXE
        if exe.exists() and not force:
            srcs = [ROOT / "firmware" / "sfra_src" / "sfra_core.c",
                    ROOT / "firmware" / "sfra_src" / "sfra_proto.c",
                    ROOT / "firmware" / "examples" / "pc" / "sfra_sim_mcu.c",
                    ROOT / "firmware" / "examples" / "pc" / "sfra_port_pc.c",
                    ROOT / "firmware" / "sfra_inc" / "sfra_proto.h",
                    ROOT / "firmware" / "sfra_inc" / "sfra_core.h",
                    ROOT / "firmware" / "sfra_inc" / "sfra_port.h"]
            if all(s.stat().st_mtime <= exe.stat().st_mtime for s in srcs):
                return exe

        gcc = cc.find_toolchain()
        if gcc is None:
            raise RuntimeError("找不到 x86 gcc，无法编译假 MCU")
        import os
        import subprocess
        # `-ffp-contract=off`：**不许编译器把 a*b+c 熔成 FMA**。
        #
        # x86 默认 `-mfpmath=sse` 且 `-ffp-contract=fast`，gcc 会把
        # `y_c = acc_mac(y_c, y, q15c)` 里的乘加合成一条 FMA 指令，
        # 中间结果不落盘、舍入一次而不是两次。ARM Cortex-M0+ 没有 FMA
        # （也不生成 VFP 指令，见 sfra_local_cfg.h 的定点开关），
        # 于是**同一份 C 代码在假 MCU 和真板上的数值会走不同的舍入路径**。
        #
        # 这不是精度洁癖：DFT 累加器的每一步都乘加，最坏情况下两种
        # 舍入会让同一个频点的幅值差到 1e-3 量级，而 MT6835 那类对照
        # 判据是按 1e-4 卡的。更要紧的是"假 MCU 通过 ≠ 真板通过"——
        # 这条 flag 让两边跑的是同一条浮点语义。
        cmd = [str(gcc), "-std=c99", "-O2", "-ffp-contract=off",
               "-Wall", "-Wextra",
               f"-I{ROOT / 'firmware' / 'sfra_inc'}",
               str(ROOT / "firmware" / "sfra_src" / "sfra_core.c"),
               str(ROOT / "firmware" / "sfra_src" / "sfra_proto.c"),
               str(ROOT / "firmware" / "examples" / "pc" / "sfra_sim_mcu.c"),
               str(ROOT / "firmware" / "examples" / "pc" / "sfra_port_pc.c"),
               "-o", str(exe), "-lm"]
        env = dict(os.environ)
        env["PATH"] = str(gcc.parent) + os.pathsep + env.get("PATH", "")
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"编译假 MCU 失败：\n{r.stdout}\n{r.stderr}")
        return exe

    def is_open(self) -> bool:
        return self.proc is not None and \
            self.proc.state() == QtCore.QProcess.Running

    def open(self) -> bool:
        self.close()
        try:
            exe = self.build()
        except Exception as e:                     # noqa: BLE001
            self.errorText.emit(str(e))
            return False

        self.proc = QtCore.QProcess(self)
        self.proc.setProcessChannelMode(QtCore.QProcess.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        # started / errorOccurred 用来替代 waitForStarted()：见下方注释。
        self.proc.started.connect(self._on_started)
        self.proc.errorOccurred.connect(self._on_proc_error)
        self._exe = exe
        self.parser.reset()

        # 启动超时兜底：QProcess 在某些环境下既不 started 也不 error
        # （例如可执行文件被杀毒软件挂起）。3 s 后自查一次。
        self._start_guard = QtCore.QTimer(self)
        self._start_guard.setSingleShot(True)
        self._start_guard.setInterval(3000)
        self._start_guard.timeout.connect(self._on_start_timeout)
        self._start_guard.start()

        self.proc.start(str(exe), self._extra_args)
        return True

    def _on_started(self) -> None:
        """子进程真正跑起来了。**只有在这里才发 connected**。

        为什么必须异步：`waitForStarted()` 是"阻塞 + 内部起嵌套事件
        循环"。如果 open() 是在某个信号槽里被调用的（点"连接"按钮就是），
        那个槽本身可能处在 Windows 的 COM 输入同步调用上下文中，此时
        waitForStarted() 再起嵌套循环就会触发原生崩溃：
            Windows fatal exception: code 0x8001010d
        即 RPC_E_CANTCALLOUT_ININPUTSYNCCALL —— 已实测复现。
        改成信号驱动后，open() 立刻返回，子进程起来由 started 通知。
        """
        if hasattr(self, "_start_guard"):
            self._start_guard.stop()
        exe_name = self._exe.name if getattr(self, "_exe", None) else "sim_mcu"
        self.connected.emit(f"SimMCU:{exe_name}")
        self.logText.emit(f"假 MCU 已启动（{exe_name}）")

    def _on_proc_error(self, err) -> None:
        """启动失败（找不到 exe、无权限等）。"""
        if hasattr(self, "_start_guard"):
            self._start_guard.stop()
        if self.proc is not None:
            self.errorText.emit(f"假 MCU 启动失败：{self.proc.errorString()}")
            self.proc = None
        self.disconnected.emit()

    def _on_start_timeout(self) -> None:
        if self.proc is None:
            return
        if self.proc.state() == QtCore.QProcess.Running:
            return                      # 其实起来了，跳过（started 会自己发）
        self.errorText.emit("假 MCU 启动超时（3 s 未见进程）")
        self.proc.deleteLater()
        self.proc = None
        self.disconnected.emit()

    def close(self) -> None:
        super().close()
        if hasattr(self, "_start_guard"):
            self._start_guard.stop()
        if self.proc is not None:
            # 先屏蔽信号，避免 kill() 触发的 finished 回调又去动已清空的
            # self.proc。**不再调 waitForFinished()**：同 waitForStarted，
            # 它在信号槽里会起嵌套事件循环，是同一个崩溃来源。
            # kill() 之后由操作系统回收，本端立刻可视为已断开。
            self.proc.blockSignals(True)
            self.proc.kill()
            self.proc.deleteLater()
            self.proc = None
            self.disconnected.emit()

    def _write_bytes(self, data: bytes) -> bool:
        if self.proc is None or self.proc.state() != QtCore.QProcess.Running:
            return False
        return self.proc.write(data) == len(data)

    def _on_stdout(self) -> None:
        if self.proc is None:
            return
        data = bytes(self.proc.readAllStandardOutput())
        if data:
            self.feed(data)

    def _on_stderr(self) -> None:
        if self.proc is None:
            return
        txt = bytes(self.proc.readAllStandardError()).decode(
            "utf-8", errors="replace").strip()
        for line in txt.splitlines():
            if line:
                self.logText.emit(line)

    def _on_finished(self, code, status) -> None:
        # blockSignals(True) 本该挡住这条回调，但 deleteLater() 是延迟的，
        # 期间仍可能有一次 finished 挤进来。self.proc 已置空时直接忽略，
        # 否则会在正常关闭流程里多弹一次 disconnected。
        if self.proc is None:
            return
        if hasattr(self, "_start_guard"):
            self._start_guard.stop()
        self.logText.emit(f"假 MCU 退出（code={code}）")
        self.proc = None
        self.disconnected.emit()


# ================================================================= 真串口
class SerialLink(BaseLink):
    """真串口。只在装了 PyQt5.QtSerialPort 时可用。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._port = None
        self._name = ""
        self._baud = 921600

    def is_open(self) -> bool:
        return self._port is not None and self._port.isOpen()

    def open(self, port_name: str = "", baud: int = 921600) -> bool:
        try:
            from PyQt5 import QtSerialPort
        except ImportError:
            self.errorText.emit("未安装 PyQt5.QtSerialPort，无法连接真串口")
            return False

        self.close()
        p = QtSerialPort.QSerialPort(self)
        p.setPortName(port_name)
        p.setBaudRate(baud)
        p.setDataBits(QtSerialPort.QSerialPort.Data8)
        p.setParity(QtSerialPort.QSerialPort.NoParity)
        p.setStopBits(QtSerialPort.QSerialPort.OneStop)
        p.setFlowControl(QtSerialPort.QSerialPort.NoFlowControl)
        p.readyRead.connect(self._on_ready_read)
        p.errorOccurred.connect(self._on_error)
        if not p.open(QtCore.QIODevice.ReadWrite):
            self.errorText.emit(f"打开 {port_name} 失败：{p.errorString()}")
            return False
        self._port = p
        self._name = port_name
        self._baud = baud
        self.parser.reset()
        self.connected.emit(f"{port_name}@{baud}")
        self.logText.emit(f"已连接 {port_name} @ {baud}")
        return True

    def close(self) -> None:
        super().close()
        if self._port is not None:
            self._port.blockSignals(True)
            self._port.close()
            self._port = None
            self.disconnected.emit()

    def _write_bytes(self, data: bytes) -> bool:
        if self._port is None:
            return False
        n = self._port.write(data)
        self._port.flush()
        return n == len(data)

    def _on_ready_read(self) -> None:
        if self._port is None:
            return
        data = bytes(self._port.readAll())
        if data:
            self.feed(data)

    def _on_error(self, err) -> None:
        from PyQt5 import QtSerialPort
        fatal = (QtSerialPort.QSerialPort.ResourceError,
                 QtSerialPort.QSerialPort.DeviceNotFoundError,
                 QtSerialPort.QSerialPort.PermissionError)
        if err in fatal:
            self.errorText.emit(f"串口错误：{self._port.errorString() if self._port else ''}")
            self.close()
