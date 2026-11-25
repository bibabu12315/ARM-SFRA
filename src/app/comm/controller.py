"""SFRA Studio —— 扫频后端控制器（界面与算法/链路的唯一接缝）。

问题背景（S4 之前的状态）：
    Tab1 只认识 `sim.engine.SweepController`（子线程跑 Python 仿真），
    `comm.link` 里的 `SimMcuLink` / `SerialLink` 没有任何界面代码在用。
    也就是说"仿真界面"和"真链路"是两套互不认识的代码——S4 要补的
    不是"接线"，而是一个**后端接口**，让界面按接口编程。

本模块给出那个接口（`SweepControllerBase`）和它的两个实现：

    SimLinkController   : 走 `SimMcuLink`（C 端假 MCU 子进程）。
                          **这是最重要的一条**：它跑的是真正要塞进 MCU 的
                          `sfra_core.c` + `sfra_proto.c`，出图走的是和真机
                          完全相同的协议路径。S3 之前界面根本没走过这条。
    SerialLinkController: 走 `SerialLink`（真串口，921600 8N1）。

为什么不干脆只留仿真、真机上再改界面：
    界面上"扫频有多少点 / 进行到第几点 / 数据什么时候到"这些语义，
    在仿真后端和真链路后端里是**不一样的**（一个由线程推进，一个由
    上位机轮询 STATUS 推进）。接口一致、实现分清，界面才能两边通用，
    不必在真机第一次联调时才第一次跑这段代码。

轮询与超时（03 §3.4，真机联调必踩的坑）：
    固件 `SFRA_PROTO_TIMEOUT_MS = 500`：500 ms 收不到**任何有效帧**就
    自动 ABORT。所以上位机必须保证每 < 500 ms 至少发一次 GET_STATUS。
    本模块的轮询间隔 `POLL_MS = 200`，并且**只在扫描中轮询**——
    空闲时不发命令，也就不会误触发固件超时。
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PyQt5 import QtCore

from ..model.bode_data import MODE_LOOP, MODE_PLANT, RawPoint, SweepResult
from ..model.sweep_config import SweepConfig
from .link import BaseLink, SerialLink, SimMcuLink
from .protocol import (Caps, Cmd, ErrCode, Flags, State, StatusInfo,
                       pack_config)

#: STATUS 轮询间隔。必须 < 固件 SFRA_PROTO_TIMEOUT_MS(500)，留一半余量。
POLL_MS = 200
#: 等 ACK 的上限。link 自己会 500 ms × 3 重发，这里只做"总超时"兜底。
ACK_BUDGET_MS = 2500
#: 落后多少点开始拉数据（一个 DATA 帧最多 7 点）
FETCH_CHUNK = 7


# ================================================================= 接口
class SweepControllerBase(QtCore.QObject):
    """界面只认这套信号。两个后端行为一致，界面代码不必分支。"""

    sigPoint = QtCore.pyqtSignal(object)            # RawPoint
    sigProgress = QtCore.pyqtSignal(int, int, float)  # done, total, f_act
    sigDone = QtCore.pyqtSignal(bool, str)          # 正常结束?, 说明
    sigLog = QtCore.pyqtSignal(str)
    #: MCU 回读到的系数（b0,b1,b2,a1,a2 顺序）。空列表 = 读回失败。
    sigCoeff = QtCore.pyqtSignal(list)
    #: 固件在 CAPS 里报的"按默认配置真实能扫多少点"（0 = 旧固件没报）。
    #: 界面据此把终点收到 f_s/5 内，避免 PC 与 MCU 点数口径不一致。
    sigFwEffPoints = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result: Optional[SweepResult] = None
        self.cfg: dict = {}

    # ---- 子类实现 ----
    @property
    def running(self) -> bool:
        raise NotImplementedError

    def start(self, cfg) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    @property
    def can_reuse_connection(self) -> bool:
        """是否已在链路上（真机后端：连上就能扫；仿真后端：无意义）。"""
        return True

    # ---- 公用：造结果对象 ----
    def _new_result(self, cfg: SweepConfig) -> SweepResult:
        return SweepResult(
            mode=int(cfg.mode),
            amplitude_pu=float(cfg.amp_pu),
            isr_freq=float(cfg.isr_freq),
            timestamp=QtCore.QDateTime.currentDateTime()
                        .toString("yyyy-MM-dd HH:mm:ss"))


# ================================================================= 真链路
class LinkController(SweepControllerBase):
    """真链路后端：命令走协议帧，数据靠轮询 STATUS + GET_DATA。

    状态推进（03 §3.4 时序）::

        PING → PING_ACK → GET_CAPS → CAPS → GET_CONDITIONS → CONDITIONS
        SET_CONFIG → ACK → START → ACK
        loop 200ms: GET_STATUS → STATUS
                    可用点数涨了 → GET_DATA(idx, n) → DATA → 逐点出图
        STATUS.flags.done → GET_DATA 补齐 → sigDone

    **断点续传**：`self._next_idx` 是"已经拿到手的最大连续 idx+1"。
    重连后先问 STATUS 的 `freq_index`，若 MCU 里数据还在（没 ABORT、
    没重新 START），就从 `_next_idx` 继续拉，不重扫。这条是 S3 DoD 里
    "断电重连后断点续传"的落地——之前只有文档，没有代码。
    """

    def __init__(self, link: BaseLink, parent=None):
        super().__init__(parent)
        self.link = link
        self._cfg: Optional[SweepConfig] = None
        self._next_idx = 0            # 已收到并写入 result 的点的个数
        self._available = 0           # MCU 侧可读点数（STATUS.freq_index）
        self._fetching = 0            # 已请求但没回来的点数（在途）
        self._scanning = False
        self._done_seen = False
        self._last_f_act = 0.0
        self._state = State.IDLE
        self._flags = 0
        self._n_total = 0
        self._cap: Optional[Caps] = None
        self._conditions: dict = {}
        self._pending_acks: List[Tuple[int, float]] = []
        #: 最近一次 SET_COEFF 的入参，用来和回显比对（写进去的 vs 读回来的）
        self._coeff_sent: Optional[List[float]] = None
        #: 本轮扫频中标记为饱和的点数（W2）
        self._sat_points = 0
        #: 本轮 GET_DATA 被 MCU 回 INDEX_RANGE 的累计次数（坑 A18）。
        #: 正常竞态下它会零星增长（上位机按 STATUS 快照请求、而 MCU 的
        #: point_idx 又推进了），超过 _IDX_MISMATCH_MAX 说明两侧点数
        #: 真的不一致，此时判失败而不是无限重试。
        self._idx_mismatch = 0

        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)

        self._ack_guard = QtCore.QTimer(self)
        self._ack_guard.setSingleShot(True)
        self._ack_guard.setInterval(ACK_BUDGET_MS)
        self._ack_guard.timeout.connect(self._on_ack_timeout)

        self.link.statusUpdate.connect(self._on_status)
        self.link.dataChunk.connect(self._on_data)
        self.link.ackReceived.connect(self._on_ack)
        self.link.capsReceived.connect(self._on_caps)
        self.link.conditionsReceived.connect(self._on_conditions)
        self.link.coeffReceived.connect(self._on_coeff)
        self.link.disconnected.connect(self._on_link_lost)

    # ------------------------------------------------------------ 属性
    @property
    def running(self) -> bool:
        return self._scanning

    @property
    def caps(self) -> Optional[Caps]:
        return self._cap

    @property
    def conditions(self) -> dict:
        return dict(self._conditions)

    @property
    def state_name(self) -> str:
        return State.name_of(int(self._state))

    @property
    def saturated(self) -> bool:
        return bool(self._flags & (Flags.DUTY_SAT | Flags.ADC_SAT))

    # ------------------------------------------------------------ 握手
    def handshake(self) -> None:
        """PING → GET_CAPS → GET_CONDITIONS。连上之后立刻调一次。"""
        self.link.ping()
        self.link.get_caps()
        self.link.get_conditions()

    # ------------------------------------------------------------ 系数
    def set_coeff(self, coeffs: List[float]) -> bool:
        """把补偿器系数下发到 MCU。

        `coeffs` 是 Tab2 算出来的 `b0,b1,b2,a1,a2`（5 个）。
        协议 `SET_COEFF` 的 `order` 字段是**系数个数**，载荷
        `order(u8) b[0..order-1](f32)`。固件侧的 `sfra_proto_set_coeff()`
        期望收到的就是这串差分方程系数本身，不做二次换算——
        换算（Kp/Ki ↔ b/a）只在 PC 上做一次，有测试守着。
        """
        if not self.link.is_open():
            self.sigLog.emit("下发系数失败：链路未连接")
            return False
        vals = [float(v) for v in coeffs]
        if not 1 <= len(vals) <= 4:
            # 5 个 a/b 里 a2 恒为 0，PI/2P2Z 下 b2 也可能是 0；
            # 但协议只允许 1~4 个，这里必须显式报错而不是悄悄截断。
            self.sigLog.emit(
                f"下发系数失败：协议允许 1~4 个系数，收到 {len(vals)} 个")
            return False
        self._coeff_sent = list(vals)
        self.link.set_coeff(vals)
        self.sigLog.emit(
            "已下发系数：" + ", ".join(f"{v:.6f}" for v in vals))
        return True

    def get_coeff(self) -> bool:
        """读回 MCU 中**当前生效**的系数。

        应答是 `COEFF` 帧（与 `SET_COEFF` 的回显同格式），会在
        `_on_coeff()` 里转成 `sigCoeff` 发出去。
        """
        if not self.link.is_open():
            self.sigLog.emit("读回系数失败：链路未连接")
            return False
        self.link.get_coeff()
        return True

    def _on_coeff(self, vals: List[float]) -> None:
        if not vals:
            self.sigCoeff.emit([])
            return
        # 如果是 SET_COEFF 的回显，顺手做一次"写进去 vs 读回来"比对。
        # 这一步是免费的（帧本来就会来），但能立刻暴露传输层截断/字节序错误。
        if self._coeff_sent is not None and len(self._coeff_sent) == len(vals):
            bad = [i for i, (a, b) in enumerate(zip(self._coeff_sent, vals))
                   if abs(a - b) > max(1e-6, abs(a) * 1e-5)]
            if bad:
                self.sigLog.emit(
                    f"⚠ 系数回显不一致，位置 {bad}：" +
                    "，".join(f"发出 {self._coeff_sent[i]:.6f} "
                              f"读回 {vals[i]:.6f}" for i in bad[:3]))
            else:
                self.sigLog.emit("系数写入校验通过（回显一致）")
        self.sigCoeff.emit(list(vals))

    # ------------------------------------------------------------ 开始
    def start(self, cfg) -> bool:
        """cfg 可以是 SweepConfig，也可以是旧的 dict（兼容 Tab1 现值）。"""
        if self._scanning:
            return False
        if not self.link.is_open():
            self.sigDone.emit(False, "链路未连接")
            return False

        c = cfg if isinstance(cfg, SweepConfig) else self._from_dict(cfg)
        errs = [t for t, hard in c.validate() if hard]
        if errs:
            self.sigDone.emit(False, errs[0])
            return False
        for text, hard in c.validate():
            self.sigLog.emit(("错误：" if hard else "提示：") + text)

        # MCU 的 isr_freq 是硬件事实，PC 不能改。CAPS 里有就用来校正，
        # 否则用界面上填的值（用户必须自己填对）。
        if self._cap is not None and self._cap.isr_freq > 0:
            if abs(self._cap.isr_freq - c.isr_freq) > 1.0:
                self.sigLog.emit(
                    f"ISR 频率以固件为准：{self._cap.isr_freq:.0f} Hz"
                    f"（界面填的是 {c.isr_freq:.0f} Hz）")
                c.isr_freq = float(self._cap.isr_freq)

        self._cfg = c
        self.cfg = c.to_sim_cfg()
        self.result = self._new_result(c)
        self._next_idx = 0
        self._available = 0
        self._fetching = 0
        self._done_seen = False
        self._state = State.IDLE
        self._flags = 0
        self._n_total = c.n_points
        self._last_f_act = 0.0
        self._sat_points = 0
        self._idx_mismatch = 0

        n = int(c.n_points)
        if n > 256:
            self.sigDone.emit(False, f"点数 {n} 超过固件上限 256")
            return False

        # ---------------- 点数以固件为准（A19）
        #
        # 固件 START 里会把 vec_length 再裁一次（`sfra_effective_points()`，
        # f_s/5 上限）。本端点表必须与它一致，否则会出现
        # "PC 承诺 47 点、MCU 只扫 28 点 → 反复 INDEX_RANGE"。
        #
        # 为什么还要发 SET_CONFIG：此时 `_cfg.freqs` 已经按 PC 自己的
        # f_s/5 截过，`freq_start/freq_step/n_points` 三者自洽；直接丢给
        # 固件，它算出的 eff 与 `n` 相同（同一套公式、同一组参数）。
        # 真机事故的根因是**界面把 f_stop=20000 原样塞进 cfg.freqs[0..-1]
        # 之外的字段**——现在界面已按 CAPS 校正，这里再核对一次兜底。
        if self._cap is not None and self._cap.eff_points > 0:
            eff_fw = int(self._cap.eff_points)
            # eff_points 是"默认配置（100 Hz 起、20 点/dec）"下的点数，
            # 与本次配置不同就不能直接比，只作粗校验：本端明显更多时警告。
            if n > eff_fw * 2 and n > 32:
                self.sigLog.emit(
                    f"⚠ 本端 {n} 点可能是固件上限 {eff_fw} 点的两倍以上，"
                    f"固件会按 f_s/5 截断，按实际点数收尾")

        self._send_config_and_start()
        self._scanning = True
        self.sigLog.emit(
            f"开始扫频 {n} 点（{c.mode_name}，{c.freqs[0]:.0f}~"
            f"{c.freqs[-1]:.0f} Hz，幅值 {c.amp_pu*100:.2f} %pu）")
        self.sigProgress.emit(0, n, 0.0)
        self._poll.start()
        return True

    def _send_config_and_start(self) -> None:
        c = self._cfg
        assert c is not None
        self._pending_acks = [(int(Cmd.SET_CONFIG), 0.0), (int(Cmd.START), 0.0)]
        self.link.set_config(freq_start=float(c.freqs[0]),
                             freq_step=float(c.freq_step),
                             amplitude=float(c.amp_pu),
                             vec_len=int(c.n_points),
                             cycles_mode=int(c.cyc_mode),
                             mode=int(c.mode))
        self._ack_guard.start()

    @staticmethod
    def _from_dict(d: dict) -> SweepConfig:
        """旧 dict → SweepConfig。

        **ppd 必须从 dict 里读，不能写死。**
        这行原来是 `ppd=20`，导致界面填 40 点/dec 也被覆盖回 20：
        `to_sim_cfg()` 里 freqs 已按 40 生成，但这里用 ppd=20 又重算
        一遍，freqs 被替换成 20 那条表——界面承诺 93 点、实际扫 47 点。
        用户看到的是"改了点数没反应"。
        """
        comp = d.get("comp", {}) or {}
        return SweepConfig(
            isr_freq=float(d.get("isr_freq", 100_000.0)),
            f_start=float(d["freqs"][0]) if "freqs" in d and len(d["freqs"]) else 100.0,
            f_stop=float(d["freqs"][-1]) if "freqs" in d and len(d["freqs"]) else 20_000.0,
            ppd=int(d.get("ppd", 20)),
            cyc_mode=int(d.get("cyc", 1)),
            mode=int(d.get("mode", MODE_LOOP)),
            amp_pu=float(d.get("amp_pu", 0.01)),
            dut=d.get("dut", "lc"),
            f0c=float(d.get("f0", 1300.0)),
            q=float(d.get("q", 2.0)),
            kp=float(comp.get("kp", 0.5)),
            ki=float(comp.get("ki", 1200.0)),
            ref_dc=float(d.get("ref_dc", 0.5)))

    # ------------------------------------------------------------ 停止
    def stop(self) -> None:
        """发 ABORT 并立刻停轮询。注入在 MCU 端下个 ISR 即归零。"""
        if not self._scanning:
            return
        self._scanning = False
        self._poll.stop()
        self._ack_guard.stop()
        if self.link.is_open():
            self.link.abort()
        self.sigLog.emit("已发送 ABORT")
        self.sigDone.emit(False, "用户中止")

    def abort_link(self) -> None:
        """链路断开时调用：不发帧，只收拾本端状态。"""
        self._on_link_lost()

    # ------------------------------------------------------------ 轮询
    def _on_poll(self) -> None:
        if not self._scanning or not self.link.is_open():
            return
        # 数据请求在途时不发 STATUS：GET_DATA 与 GET_STATUS 都会占 MCU
        # 主循环的一次 poll，混在一起会让 STATUS 的 freq_index 忽大忽小，
        # 断点续传的判据就不稳了。等这一批数据回来再问进度。
        if self._fetching > 0:
            return
        self.link.get_status()

    def _on_status(self, st: StatusInfo) -> None:
        if not self._scanning or self._cfg is None:
            return
        self._state = st.state
        self._flags = st.flags

        # ---------------- `_available` 必须与固件的 `avail` **逐字对齐**
        #
        # 固件 `do_get_data()` 是这么判越界的：
        #
        #     avail = (state == SFRA_ST_DONE) ? cfg.vec_length : point_idx;
        #     if (start >= avail) -> SFRA_ERR_INDEX
        #
        # 而 `STATUS.freq_index` 报的就是 `point_idx`，含义是"**正在采集**
        # 的索引"，不是"已完成数"。所以非 DONE 时：
        #     · 可读区间是 [0, point_idx)  ——  共 point_idx 点；
        #     · 请求起点必须 `start < point_idx`。
        # 只做 `min(freq_index, n_total)` 在 DONE 后是错的（DONE 时
        # `point_idx` 停住不再涨，但 `avail` 已经是 vec_length），
        # 会让最后一批点永远拉不完；而非 DONE 时若恰好在
        # `point_idx` 边界上请求，又会稳定吃一次 INDEX_RANGE。
        #
        # 这里统一按固件语义算，`want` 因此天然不会越界。
        if st.done:
            # **DONE 时不能直接用本端 `_n_total`**。固件 START 会把
            # vec_length 裁成 `sfra_effective_points()`（f_s/5 上限），
            # 这个值可能比 PC 算的少（2026-11-24：PC 47 点、MCU 28 点），
            # 于是 `_available = 47` → 反复请求第 28 点 → 满屏 INDEX_RANGE。
            #
            # 实测发现 DONE 帧里 `freq_index` 恰好等于固件真正的
            # `vec_length`（末点 NEXT 时 point_idx 已自增到上限），所以
            # 取 max(freq_index, 已拿到点数) 当可读上界，两边都与固件一致。
            self._available = min(
                max(int(st.freq_index), self._next_idx), self._n_total)
            # 固件点数是权威值：本端点数多于固件时，直接把总数改小，
            # 否则 _done_seen 之后的补拉逻辑还会按旧 n_total 要数据。
            if 0 < int(st.freq_index) < self._n_total:
                self.sigLog.emit(
                    f"MCU 实际点数 {int(st.freq_index)} 少于本端 {self._n_total}"
                    f"（固件按 f_s/5 截断），按 MCU 点数收尾")
                self._n_total = int(st.freq_index)
        else:
            # point_idx 可能因为断点续传/固件上限而超出本端 n_total，夹一下
            self._available = min(int(st.freq_index), self._n_total)
        self._available = max(0, self._available)

        if st.cur_f_act > 0:
            self._last_f_act = float(st.cur_f_act)

        if st.flags & (Flags.DUTY_SAT | Flags.ADC_SAT):
            self.sigLog.emit("⚠ 饱和标志置位（占空比或 ADC），该频点数据不可信")

        # 数据落后就拉（一次 7 点，够跟上 200 ms 的扫频速度）
        want = self._available - self._next_idx
        if want > 0:
            n = min(want, FETCH_CHUNK)
            self._fetching = n
            self.link.get_data(self._next_idx, n)
            return

        if st.done and not self._done_seen:
            self._done_seen = True
            if self._next_idx >= self._n_total:
                self._finish_ok()
                return
            # done 了但还有尾点没拉回来，补一次请求
            need = self._n_total - self._next_idx
            n = min(need, FETCH_CHUNK)
            self._fetching = n
            self.link.get_data(self._next_idx, n)
            return

        # **点数拿全了就是完成，不必死等 `st.done`**。
        #
        # 这是"进度条停在 57%"那类现象的直接来源之一：
        # 固件的 `SFRA_ST_DONE` 只在 `sfra_background_task()` 里推进，
        # 而上位机是靠 200 ms 轮询 STATUS 才能看到它。如果某次 STATUS
        # 恰好落在 DONE 之前、下一次又因 `_fetching > 0` 被跳过，
        # 就出现"数据已全部到手，但 done 位没被观测到"——界面于是
        # 永远停在 `(_n_total-1)/_n_total`，看起来就是卡住了。
        #
        # 判据放在"数据侧"而不是"状态侧"：`_next_idx` 是**已经落到
        # result 里**的连续点数（由 `_on_data` 严格按 start_idx 推进），
        # 它到 `_n_total` 就说明曲线是完整的，与 STATUS 的时序无关。
        if self._next_idx >= self._n_total and self._n_total > 0:
            self._done_seen = True
            self._finish_ok()
            return

        self._emit_progress()

    def _on_data(self, ch) -> None:
        if self.result is None:
            return
        # 乱序到达时只接受"正好从 _next_idx 开始"的那一段，其余丢弃：
        # 宁可丢一帧再请一次，也不要把点序弄乱——频率必须严格单调，
        # 否则 PM/GM 的插值穿越点会算错。
        if ch.start_idx != self._next_idx:
            self._fetching = 0
            return
        pts = [RawPoint(*p) for p in ch.points]
        if not pts:
            self._fetching = 0
            return
        # 逐点饱和标记：DATA 帧里没有饱和位，用它**请求时**的 STATUS.flags。
        # `_flags` 在 `_on_status` 里更新，而取数是"先问 STATUS 再 GET_DATA"，
        # 所以此刻的 _flags 就是这批点的状态。误差窗口是一个轮询周期
        # （200 ms），相对整个频点停留时间（数十 ms~百 ms）够用；
        # 宁可多标一个点也别漏标——漏标会让限幅器数据混进裕度计算。
        sat = bool(self._flags & (Flags.DUTY_SAT | Flags.ADC_SAT))
        self.result.add_many(pts, saturated=sat)
        for p in pts:
            self.sigPoint.emit(p)
        self._next_idx = ch.end_idx
        self._fetching = 0

        if sat:
            self._sat_points += len(pts)

        if self._done_seen and self._next_idx >= self._n_total:
            self._finish_ok()
            return
        if self._done_seen and self._next_idx < self._n_total:
            # DONE 之后固件的 avail 就是它自己的 vec_length，尾点一定可读；
            # 只有本端 n_total 大于固件真实点数时才会一直 INDEX_RANGE。
            # 那种情况交给下面这条护栏收拾：连续 3 次越界就按已拿到的点数
            # 收尾，不要死循环 200 次再判失败（满屏红字，用户以为工具坏了）。
            if self._done_seen and self._idx_mismatch >= 3:
                self._finish_partial()
                return
            n = min(self._n_total - self._next_idx, FETCH_CHUNK)
            self._fetching = n
            self.link.get_data(self._next_idx, n)
            return
        self._emit_progress()

    def _emit_progress(self) -> None:
        if self._cfg is None:
            return
        self.sigProgress.emit(self._next_idx, self._n_total, self._last_f_act)

    def _finish_partial(self) -> None:
        """按"已经拿到的点数"收尾（固件点数比本端少时的正常出口）。

        触发场景：固件 START 里把 vec_length 裁到 f_s/5 之后就 DONE 了，
        而本端 n_total 更大。此时剩下的点**根本不存在**，继续请求只会被
        MCU 一直拒绝。所以把总数改成实际到手数、正常结束，让曲线能画、
        CSV 能导——数据本身是完整的。
        """
        if not self._scanning:
            return
        got = self._next_idx
        self._n_total = got
        self._scanning = False
        self._poll.stop()
        self._ack_guard.stop()
        self.sigLog.emit(
            f"MCU 只扫到 {got} 点（f_s/5 截断），按实际点数收尾")
        self.sigProgress.emit(got, got, self._last_f_act)
        self.sigDone.emit(True, "")

    # ------------------------------------------------------------ ACK
    def _on_ack(self, req: int, err: int, detail: int) -> None:
        # GET_COEFF 的失败走 NACK，不是"发给 MCU 的命令序列"，不该影响扫频。
        # 这里必须先处理：否则 _pending_acks 里排着 SET_CONFIG 时，
        # 一个读回 NACK 会把整次扫频打停。
        if req == Cmd.GET_COEFF:
            if err != ErrCode.OK:
                self.sigLog.emit(
                    "MCU 不支持系数读回"
                    + (f"（{ErrCode.name_of(err)}，detail={detail}）"
                       if detail else f"（{ErrCode.name_of(err)}）"))
                self.sigCoeff.emit([])      # 空列表 = 读回失败
            return

        # ---------------- GET_DATA 的 NACK：**必须救回来**
        #
        # 这就是"卡在 57%"的真凶（坑 A18）。`GET_DATA` 不是
        # `_pending_acks` 里的命令（那个队列只排 SET_CONFIG/START），
        # 所以它的 NACK 一旦走到下面 `if not self._pending_acks: return`
        # 就被**静默丢弃**，`_fetching` 永远停在非零值。
        # 而 `_on_poll()` 第一句是 `if self._fetching > 0: return`，
        # 于是上位机再也不发 STATUS —— 扫频彻底停摆，进度冻结在
        # 最后一次成功取数的百分比上。MCU 那边可能早就 DONE 了。
        #
        # 处理原则：
        #   · 无论什么错误，先无条件清 `_fetching`（解除轮询封锁）；
        #   · INDEX_RANGE 是"我要的点还没好"，属于正常竞态，
        #     记一条日志就够，不能判扫频失败；
        #   · 其它错误才算真的失败。
        if req == Cmd.GET_DATA:
            was = self._fetching
            self._fetching = 0
            if err == ErrCode.OK:
                return
            if err == ErrCode.INDEX_RANGE:
                # MCU 侧 avail 还没覆盖到 start_idx。两种可能：
                #   · 正常竞态——我要的点还没采完，下次 STATUS 会纠正；
                #   · 本端 n_total 大于固件真实点数——固件永远回越界。
                # 前者重试一两次就好；连续多次还越界就说明是后者，
                # 立刻按已拿到的点数收尾，而不是死循环几百次。
                self._idx_mismatch += 1
                if self._idx_mismatch == 1:
                    self.sigLog.emit(
                        f"GET_DATA 越界（请求 {self._next_idx}，在途 {was} 点），"
                        f"按 MCU 实际进度重试")
                if self._done_seen and self._idx_mismatch >= 3:
                    self._finish_partial()
                    return
                if self._idx_mismatch > 20:
                    self._scanning = False
                    self._poll.stop()
                    self.sigDone.emit(
                        False,
                        f"取数持续越界 {self._idx_mismatch} 次，"
                        f"上位机与 MCU 的点数不一致（{self._next_idx}/"
                        f"{self._n_total}），已停止")
                return
            # 其它错误：真失败，但也要先把轮询解除，让 sigDone 能正常收尾
            self.sigLog.emit(
                f"GET_DATA 失败：{ErrCode.name_of(err)}"
                + (f"（detail={detail}）" if detail else ""))
            self._scanning = False
            self._poll.stop()
            self.sigDone.emit(False, f"取数失败（{ErrCode.name_of(err)}）")
            return

        if not self._pending_acks:
            return
        head = self._pending_acks[0]
        if req != head[0]:
            return
        self._pending_acks.pop(0)

        if err != ErrCode.OK:
            self._ack_guard.stop()
            self._scanning = False
            self._poll.stop()
            msg = f"MCU 拒绝 0x{req:02X}：{ErrCode.name_of(err)}"
            if detail:
                msg += f"（detail={detail}）"
            self.sigDone.emit(False, msg)
            return

        if req == Cmd.SET_CONFIG and self._pending_acks:
            # 配置生效了，接着发 START
            self.link.start()
            return
        if req == Cmd.START:
            self._ack_guard.stop()

    def _on_ack_timeout(self) -> None:
        if not self._pending_acks:
            return
        cmd = self._pending_acks[0][0]
        self._pending_acks.clear()
        self._scanning = False
        self._poll.stop()
        self.sigDone.emit(False, f"命令 0x{cmd:02X} 无应答")

    # ------------------------------------------------------------ 其他帧
    def _on_caps(self, caps: Caps) -> None:
        self._cap = caps
        self.sigLog.emit(
            f"固件 {caps.version_str} · ISR {caps.isr_freq:.0f} Hz · "
            f"最多 {caps.max_vec_len} 点")
        # 界面上的 ISR 频率与本端点数也要跟着校正（主窗口只改控件文本，
        # 控制器自己得有同一份事实，否则 start() 里的点数仍按旧值算）。
        if caps.eff_points > 0:
            self.sigFwEffPoints.emit(int(caps.eff_points))

    def _on_conditions(self, d: dict) -> None:
        self._conditions = dict(d)

    # ------------------------------------------------------------ 结束/掉线
    def _finish_ok(self) -> None:
        self._scanning = False
        self._poll.stop()
        self._ack_guard.stop()
        n = len(self.result) if self.result is not None else 0
        self.sigLog.emit(f"扫频完成，{n} 点")
        self.sigProgress.emit(self._n_total, self._n_total, self._last_f_act)
        self.sigDone.emit(True, "")

    def _on_link_lost(self) -> None:
        if not self._scanning:
            return
        self._scanning = False
        self._poll.stop()
        self._ack_guard.stop()
        self._pending_acks.clear()
        got = self._next_idx
        self.sigDone.emit(
            False, f"链路断开（已收到 {got} 点，重连后可续传）")


# ================================================================= 假 MCU
class SimLinkController(LinkController):
    """走假 MCU 子进程的真链路后端。

    与 `sim.engine.SimWorker` 的区别（也是它存在的理由）：
        SimWorker 在 Python 里跑同一套**算法**，但不经过协议层；
        本类跑的是真 C 代码（sfra_core.c + sfra_proto.c），帧编解码、
        状态机、超时、断点续传全部走真路径。界面上"扫频正常结束"
        这件事，在真机上会不会成立，只有这条路径能给出证据。
    """

    def __init__(self, parent=None):
        link = SimMcuLink(parent)
        super().__init__(link, parent)
        #: 已请求扫频但子进程还没起来的配置。QProcess 异步启动，见 start()。
        self._pending_cfg = None

    @property
    def link(self) -> SimMcuLink:
        return self._link  # type: ignore[attr-defined]

    @link.setter
    def link(self, v: SimMcuLink) -> None:
        self._link = v

    def start(self, cfg) -> bool:
        if self._scanning:
            return False
        if not self.link.is_open():
            # QProcess 是异步启动的：open() 立刻返回，但子进程还没起来，
            # 此刻 is_open() 仍是 False。所以不能"open 完就接着 super().start()"，
            # 必须挂到 connected 信号上，等子进程真起来再走握手 + 扫频。
            # （同步 waitForStarted() 那条老路会在信号槽里触发
            #   Windows fatal exception 0x8001010d，已实测。）
            if self._pending_cfg is not None:
                return False            # 正在启动中，别重复点击
            self._pending_cfg = cfg
            # **把被测对象参数传给子进程**。此前假 MCU 里 f0/Q 是写死的
            # 2000/0.8，界面填 1300/2 完全不生效：拿 1300/2 的解析解去比
            # 一条 2000/0.8 的曲线，偏差能到 +7 dB / +60°，看着像算法错，
            # 实际只是参数没传下去。
            c = cfg if isinstance(cfg, SweepConfig) else self._from_dict(cfg)
            args = ["--plant", "first" if c.dut == "first" else "lc",
                    "--isr", f"{c.isr_freq:.1f}"]
            if c.dut == "first":
                args += ["--fc", f"{c.f0c:.4f}"]
            else:
                args += ["--f0", f"{c.f0c:.4f}", "--q", f"{c.q:.6f}"]
            self.link.set_args(*args)
            if not self.link.open():
                self._pending_cfg = None
                self.sigDone.emit(False, "假 MCU 启动失败")
                return False
            if self.link.is_open():     # 极端情况：同步就绪（真串口不会）
                self._run_pending()
            else:
                self.link.connected.connect(self._on_link_ready)
            return True
        return super().start(cfg)

    def _on_link_ready(self, _desc: str = "") -> None:
        try:
            self.link.connected.disconnect(self._on_link_ready)
        except TypeError:
            pass
        self._run_pending()

    def _run_pending(self) -> None:
        cfg, self._pending_cfg = self._pending_cfg, None
        if cfg is None:
            return
        # 握手（PING/CAPS/CONDITIONS）走真协议；给 MCU 一点时间先 ready。
        QtCore.QTimer.singleShot(200, self.handshake)
        super().start(cfg)

    def stop(self) -> None:
        super().stop()


# ================================================================= 真串口
class SerialLinkController(LinkController):
    """走真串口。多一个"连接/断开"动作，界面据此切换按钮文字。"""

    def __init__(self, parent=None):
        super().__init__(SerialLink(parent), parent)

    def connect_port(self, port: str, baud: int = 921600) -> bool:
        ok = self.link.open(port, baud)
        if ok:
            QtCore.QTimer.singleShot(120, self.handshake)
        else:
            # 打开失败（最常见：端口被 SecureCRT / 串口助手占着）必须显式
            # 报出来。原来这里只是 return False，界面毫无反应，用户以为
            # "点了没生效"，实际上是被独占拒绝了。
            #
            # 注意：只发 sigLog 是不够的——主窗口把它接到状态栏
            # showMessage，8 秒后被下一条顶掉，用户很可能正在低头插线，
            # 抬头时提示已经没了。所以额外发 errorText，让主窗口弹窗。
            # 串口打不开属于"必须让用户采取行动"的错误，值得打断一次。
            msg = (f"打开 {port} 失败：端口可能被其他程序占用。\n\n"
                   f"请关闭 SecureCRT / 串口调试助手 / PuTTY 后重试，"
                   f"或换一个 USB-TTL 端口。")
            self.sigLog.emit(
                f"打开 {port} 失败：端口可能被其他程序占用"
                f"（关闭 SecureCRT / 串口调试助手后重试）")
            self.link.errorText.emit(msg)
        return ok

    def disconnect_port(self) -> None:
        self.link.close()

    @property
    def is_open(self) -> bool:
        return bool(self.link.is_open())
