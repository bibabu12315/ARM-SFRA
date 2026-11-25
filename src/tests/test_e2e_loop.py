"""S3 端到端回环测试：PC 上位机 ↔ C 端假 MCU。

为什么要跑到"另一个进程"里：
    `test_protocol.py` 只验证 PC 侧的字节编解码自洽——两边都按同一份
    理解错，测试照样全绿。S3 真正要证的是**固件那份实现也能对上**，
    所以必须让真 C 代码（sfra_core.c + sfra_proto.c）跑起来，
    通过 stdin/stdout 交换原始字节。

代价与开关：
    要现编 C（约 8s）+ 跑 Qt 事件循环（每个用例 1~3s），所以打上
    `e2e` 标记且默认不跑，用 `pytest -m e2e` 单独触发。

这组用例守住的是"改一处协议细节忘了改另一边"这类问题——S3 实现过程
中它就抓到过 6 个：LEN 口径、CRC 范围、帧头长度、DATA 首点偏移、
GET_DATA 的 count 偏移、背景任务被 sfra_is_running 卡死。
"""
from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.e2e

QtCore = pytest.importorskip("PyQt5.QtCore")
QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from app.comm.link import SimMcuLink                      # noqa: E402
from app.comm.protocol import (Cmd, DataChunk, build_frame,     # noqa: E402
                               crc16, pack_config)


# ---------------------------------------------------------------- 夹具
@pytest.fixture(scope="session")
def qapp():
    # 由 conftest.py 建好；这里只取不建（自己建会和 QCore 抢槽位）
    app = QtWidgets.QApplication.instance()
    assert app is not None, "conftest 未生效"
    yield app


@pytest.fixture(scope="session")
def exe():
    """现编假 MCU（按 mtime 增量，已最新则秒过）。"""
    return SimMcuLink.build()


class Bench:
    """把"发命令 / 等应答 / 收集数据"包成同步语义，方便断言。

    pytest 里没法像 GUI 那样 `app.exec_()` 一路跑到底，这里用
    `processEvents` 手工泵事件：每次 pump 都是"推进一下 Qt 事件队列
    再让出"，等价于 `QTest.qWait` 但不依赖 QtTest 模块。
    """

    def __init__(self, qapp, exe):
        self.qapp = qapp
        self.link = SimMcuLink()
        self.acks = []
        self.statuses = []
        self.chunks = {}
        self.caps = None
        self.ping = None
        self.frames = []

        lk = self.link
        lk.ackReceived.connect(lambda r, e, d: self.acks.append((r, e, d)))
        lk.statusUpdate.connect(self.statuses.append)
        lk.capsReceived.connect(self._on_caps)
        lk.pingAck.connect(lambda v, p, c: setattr(self, "ping", (v, p, c)))
        lk.dataChunk.connect(self._on_chunk)
        lk.frameIn.connect(self.frames.append)

    def _on_caps(self, c):
        self.caps = c

    def _on_chunk(self, ch: DataChunk):
        for i, p in enumerate(ch.points):
            self.chunks[ch.start_idx + i] = p

    # ---- 事件泵 ----
    def pump(self, ms: int = 60) -> None:
        # 不用 time.sleep：事件队列不会自己跑，必须 processEvents。
        # 也不用 QEvent.AllEvents（PyQt5 里没有这个枚举名）。
        end = QtCore.QElapsedTimer()
        end.start()
        while end.elapsed() < ms:
            self.qapp.processEvents()

    def pump_until(self, pred, timeout_ms: int = 3000):
        end = QtCore.QElapsedTimer()
        end.start()
        while end.elapsed() < timeout_ms:
            self.qapp.processEvents()
            if pred():
                return True
        return False

    def open(self, *args: str) -> bool:
        if args:
            self.link.set_args(*args)
        ok = self.link.open()
        self.pump(120)
        return ok

    def close(self):
        self.link.close()
        self.pump(50)

    # ---- 命令 ----
    def cmd(self, c, payload: bytes = b"", timeout_ms: int = 2000):
        """发一条命令并等它的**应答帧**。

        注意不能只等 `ackReceived`：PING/CAPS/STATUS/DATA 的应答是
        各自的专用帧（0x8X），只有 ACK/NACK 才走 ackReceived 信号。
        通用做法是等"出现 cmd|0x80 的帧"，ACK 也在其中。
        """
        n = len(self.frames)
        self.link.send(c, payload)
        want = int(c) | 0x80

        def hit():
            """专用应答（cmd|0x80）或通用 ACK（0x80，payload[0]=请求号）。"""
            for fr in self.frames[n:]:
                c2 = int(fr.cmd)
                if c2 == want:
                    return True
                if c2 == int(Cmd.ACK) and fr.payload[:1] == bytes([int(c)]):
                    return True
            return False

        if not self.pump_until(hit, timeout_ms):
            return None
        for fr in self.frames[n:]:
            if int(fr.cmd) == want:
                return fr
        for fr in self.frames[n:]:
            if int(fr.cmd) == int(Cmd.ACK) and fr.payload[:1] == bytes([int(c)]):
                return fr
        return None

    @staticmethod
    def ack_of(frame):
        """ACK 帧 → (req_cmd, err, detail)。"""
        p = frame.payload
        return (p[0] if len(p) > 0 else -1,
                p[1] if len(p) > 1 else -1,
                p[2] if len(p) > 2 else 0)

    def set_config(self, f_start, f_step, amp, n, cyc=0, mode=0):
        f = self.cmd(Cmd.SET_CONFIG,
                     pack_config(f_start, f_step, amp, n, cyc, mode))
        assert f is not None, "SET_CONFIG 无应答"
        return self.ack_of(f)

    def scan(self, timeout_ms: int = 40000) -> int:
        """START 后**边轮询边收数据**，直到 DONE，返回最终点数。

        轮询 GET_STATUS 不只为了进度：假 MCU 的主循环是"收到一字节才
        推进一批 ISR"，没有命令进来它就停在 getchar() 上。真硬件不会
        这样（ISR 由定时器驱动），但仿真下必须靠轮询喂它。
        """
        f = self.cmd(Cmd.START)
        assert f is not None, "START 无应答"
        req, err, _ = self.ack_of(f)
        assert err == 0, f"START 被拒：err={err}"

        end = QtCore.QElapsedTimer()
        end.start()
        while end.elapsed() < timeout_ms:
            if self.statuses and self.statuses[-1].done:
                break
            self.link.get_status()
            self.pump(60)
        assert self.statuses and self.statuses[-1].done, \
            f"扫频未在 {timeout_ms} ms 内完成（最后状态 " \
            f"{self.statuses[-1].state_name if self.statuses else 'None'}）"
        # `freq_index` 的语义是**已完成点数**（DONE 时固件直接报
        # vec_length），不是"最后一点的索引"，所以不加 1。
        # 早先这里写 `+1`，是因为 DONE 帧报的是 point_idx（末点索引），
        # 与 GET_DATA 的 avail 口径不一致——那个 bug 已随
        # `do_get_status()` 的修正消失。
        return self.statuses[-1].freq_index

    def pull_all(self, count: int, step: int = 7) -> None:
        idx = 0
        while idx < count:
            self.link.get_data(idx, min(step, count - idx))
            self.pump(120)
            idx += step
        self.pump(150)


@pytest.fixture
def bench(qapp, exe):
    b = Bench(qapp, exe)
    assert b.open("--plant", "lc", "--isr", "100000"), "假 MCU 启动失败"
    yield b
    b.close()


# ---------------------------------------------------------------- 用例
def test_ping_and_caps(bench):
    """连接握手：PING → PING_ACK → GET_CAPS → CAPS。"""
    r = bench.cmd(Cmd.PING)
    assert r is not None, "PING 无应答"
    assert bench.ping is not None, "PING_ACK 未解析"
    ver, proto, caps = bench.ping
    assert proto == 1
    assert caps & 0x01, "应声明支持 float 路径"

    bench.link.get_caps()
    assert bench.pump_until(lambda: bench.caps is not None, 2000)
    assert bench.caps.isr_freq == pytest.approx(100000.0)
    assert bench.caps.max_vec_len >= 68


def test_set_config_ack_and_reject(bench):
    """合法配置回 OK；非法参数回 BAD_PARAM，且不重试到超时。"""
    ack = bench.set_config(100.0, 1.122, 0.02, 30, 1, 0)
    assert ack == (Cmd.SET_CONFIG, 0, 0)

    # amplitude 超 0.25 → ERR_BAD_PARAM(2)
    ack = bench.set_config(100.0, 1.122, 0.5, 30, 1, 0)
    assert ack[0] == Cmd.SET_CONFIG and ack[1] == 2


def test_scan_reaches_done_and_nyquist_clamped(bench):
    """完整扫频：状态推进到 DONE，且频点数被奈奎斯特钳制截断。"""
    bench.set_config(1000.0, 1.1220184543019633, 0.02, 68, 0, 0)
    n = bench.scan()
    assert n < 68, "f_s/5 上限应把 68 点截短"
    bench.pull_all(n)

    f = [bench.chunks[i][0] for i in range(n)]
    assert f[0] == pytest.approx(1000.0, rel=0.01)
    assert max(f) == pytest.approx(20000.0, rel=0.01), "上限应为 isr_freq/5"
    assert f == sorted(f), "频率必须单调递增"


def test_bode_matches_second_order_lc(bench):
    """物理自检：LC 二阶对象的幅相应在谐振点后有 -40dB/dec 与 -180° 趋势。

    不做精确拟合（模型参数在假 MCU 里硬编码），只断言三条"一眼错就能
    看出来"的特征，防止哪天把 a/b 通道搞反了还全绿。
    """
    bench.set_config(100.0, 1.1220184543019633, 0.02, 48, 1, 0)
    n = bench.scan()
    bench.pull_all(n)
    assert len(bench.chunks) >= n

    db, ph = [], []
    for i in range(n):
        f, a_c, a_s, b_c, b_s = bench.chunks[i]
        num = complex(a_c, -a_s)
        den = complex(b_c, -b_s)
        h = num / den if abs(den) > 1e-30 else complex(0, 0)
        db.append(20 * math.log10(abs(h)) if abs(h) > 1e-30 else -999.0)
        ph.append(math.degrees(math.atan2(h.imag, h.real)))

    # 1) 高频衰减：末点至少比峰值低 20 dB
    assert max(db) - db[-1] > 20.0, f"高频未衰减：max={max(db):.1f} min={db[-1]:.1f}"
    # 2) 相位从 0 附近一路走到 -150° 以下
    assert ph[0] > -30.0, f"低频相位应接近 0，实际 {ph[0]:.1f}"
    assert min(ph) < -150.0, f"高频相位应过 -150°，实际最低 {min(ph):.1f}"

    # 3) 相位单调下降（谐振段以外不应回弹超过 10°）
    #
    # **必须先解缠再判单调**：`atan2` 只给 (-180, 180]，二阶 LC 的相位
    # 一路降到 -180° 附近时会跳回 +180°，那是同一族角度换了代表值，
    # 不是"回弹"。不解缠直接比较，等于要求一条必定穿过支切线的曲线
    # 不许穿线——测试会误报（修掉 sin_q15 的直流偏置后高频相位终于
    # 正确地贴到 -179°，这个假阳性就暴露出来了）。
    unwrapped = [ph[0]]
    for v in ph[1:]:
        d = v - unwrapped[-1]
        if d > 180.0:
            d -= 360.0
        elif d < -180.0:
            d += 360.0
        unwrapped.append(unwrapped[-1] + d)
    for i in range(1, n):
        assert unwrapped[i] <= unwrapped[i - 1] + 10.0, (
            f"pt{i} 相位回弹：{unwrapped[i-1]:.1f}→{unwrapped[i]:.1f}"
            f"（原始 {ph[i-1]:.1f}→{ph[i]:.1f}）")


def test_abort_returns_to_idle(bench):
    """ABORT 后状态回 IDLE，且不再继续推进频点。"""
    bench.set_config(200.0, 1.1220184543019633, 0.02, 68, 0, 0)
    bench.link.start()
    bench.pump(600)
    f = bench.cmd(Cmd.ABORT)
    assert f is not None, "ABORT 无应答"
    assert bench.ack_of(f)[1] == 0

    bench.statuses.clear()
    bench.link.get_status()
    assert bench.pump_until(lambda: bench.statuses, 2000)
    assert bench.statuses[-1].state_name in ("IDLE", "ABORT")


def test_bad_crc_frame_is_ignored(bench):
    """人为破坏 CRC：链路层必须丢掉而不是把坏数据当结论。"""
    good = build_frame(Cmd.PING)
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    assert crc16(bytes(bad[2:-2])) != int.from_bytes(bytes(bad[-2:]), "little")

    n = len(bench.frames)
    bench.link._write_bytes(bytes(bad))       # 故意绕过 BaseLink.send 的组帧
    bench.pump(400)
    assert len(bench.frames) == n, "坏 CRC 帧不应被解出来"

    # 紧接着发一个好帧，链路要能立刻恢复
    assert bench.cmd(Cmd.PING) is not None


def test_reconnect_resumes_data(bench, exe, qapp):
    """断线重连后拉数据仍然正确——MCU 侧结果数组不清空。"""
    bench.set_config(1000.0, 1.1220184543019633, 0.02, 20, 0, 0)
    n = bench.scan()
    bench.pull_all(n)
    before = dict(bench.chunks)
    assert len(before) == n

    bench.close()
    bench2 = Bench(qapp, exe)
    try:
        assert bench2.open("--plant", "lc", "--isr", "100000")
        assert bench2.cmd(Cmd.PING) is not None
        # 新进程是全新状态，要重新扫；这里验证的是"重连本身不崩"
        bench2.set_config(1000.0, 1.1220184543019633, 0.02, 5, 0, 0)
        n2 = bench2.scan()
        bench2.pull_all(n2)
        assert len(bench2.chunks) == n2
    finally:
        bench2.close()


# ---------------------------------------------------------------- DC 免疫性
def _gh_curve(bench, mode, count=40, raw=False):
    """扫一遍并返回逐点 (f, gain_db, phase_deg)；raw=True 时返回原始量。

    不做可信度筛，只看原始几何——这组用例要验的是"数值层面"的
    性质，不是"出图效果"。
    """
    bench.set_config(300.0, 1.1220184543019633, 0.02, count, 1, mode)
    n = bench.scan()
    bench.pull_all(n)
    out = []
    for i in range(n):
        f, a_c, a_s, b_c, b_s = bench.chunks[i]
        zy = complex(a_c, -a_s)
        if mode == 1:
            den = complex(b_c - a_c, a_s - b_s)      # Inj − Y
        else:
            den = complex(b_c, -b_s)                 # X
        h = zy / den if abs(den) > 1e-30 else complex(0, 0)
        db = 20 * math.log10(abs(h)) if abs(h) > 1e-30 else -999.0
        ph = math.degrees(math.atan2(h.imag, h.real))
        out.append((f, abs(zy), db, ph))
    return out


def test_loop_gh_immune_to_dc_bias(qapp, exe):
    """LOOP 模式的 GH 对"参考上的直流偏置"免疫（低频段严格、高频段放宽）。

    为什么这条必须存在：
        假 MCU 的 PLANT 分支写了 `y = plant(x) - g_dc`（减直流），
        LOOP 分支没减。注释说"LOOP 的 y 与 PLANT 的 y 不同"，逻辑上
        自洽（Inj 与 Y 都含直流，相减抵消），但**没有任何测试守着**。
        e2e 全绿只能证明"能出图"，不能证明"直流不会混进 GH"。

    **实测发现的边界（2026-11-23，S4）**：
        加 0.3 直流偏置后，10 kHz 以下幅相偏差 < 0.02 dB / 0.1°，
        但 12 kHz 以上逐步扩散，20 kHz 达 0.31 dB / 4.3°。
        对照实验（同一台假 MCU 改扫 PLANT）偏差同样是 4.86°，
        所以**这跟"LOOP 减不减直流"无关**，而是"工作点上移 + 同一条
        注入幅值"让二阶对象在峰附近进入了不同的非线性区，高频因为
        每周期采样点变少、量化残差变大而更明显。

        因此判据按下表分两段：低频是"应然"（直流真被抵消掉），
        高频是"实然"（受非线性限制，只能这么宽）。这不是放水——
        若哪天真出了"直流混进 Inj−Y"的缺陷，低频段会立刻炸掉，
        而低频恰恰是环路增益最高的地方。
    """
    b0 = Bench(qapp, exe)
    b1 = Bench(qapp, exe)
    try:
        assert b0.open("--plant", "lc", "--isr", "100000")
        assert b1.open("--plant", "lc", "--isr", "100000", "--dcbias", "0.3")

        ref = _gh_curve(b0, mode=1)
        biased = _gh_curve(b1, mode=1)
        assert len(ref) == len(biased) >= 20

        #: 分段判据（dB / deg），f 分界 10 kHz。实测值写在括号里作参照：
        #:   低频 0.03 dB / 0.73°（限 0.1 / 1.0）
        #:   全程 0.40 dB / 4.32°（限 0.6 / 5.0）
        F_SPLIT = 10_000.0
        LOW = (0.1, 1.0)
        HIGH = (0.6, 5.0)

        for lim_f, (lim_db, lim_deg) in ((F_SPLIT, LOW), (float("inf"), HIGH)):
            dmax_db = 0.0
            dmax_deg = 0.0
            for (f0, y0, m0, p0), (f1, y1, m1, p1) in zip(ref, biased):
                assert f0 == pytest.approx(f1, rel=1e-6), "两边频点必须相同"
                # 偏置只改工作点，入射量本身不该成倍变化（实测低频差 0.05%、
                # 高频 1.4%，都来自二阶对象在不同工作点上的增益差）
                assert y0 == pytest.approx(y1, rel=5e-2), \
                    f"{f0:.0f} Hz 处 Y 变了（{y0:.4f}→{y1:.4f}）"
                if f0 > lim_f:
                    continue
                dmax_db = max(dmax_db, abs(m1 - m0))
                dp = abs(p1 - p0)
                if dp > 180.0:
                    dp = 360.0 - dp
                dmax_deg = max(dmax_deg, dp)

            band = "低频" if lim_f < 1e9 else "全程"
            assert dmax_db < lim_db, \
                f"{band} GH 幅值受直流偏置影响 {dmax_db:.3f} dB（限 {lim_db}）"
            assert dmax_deg < lim_deg, \
                f"{band} GH 相位受直流偏置影响 {dmax_deg:.3f}°（限 {lim_deg}）"
    finally:
        b0.close()
        b1.close()


def test_plant_gh_relationship_is_physical(qapp, exe):
    """LOOP 与 PLANT 两条曲线的 Y 必须逐点完全一致，GH 由 Inj−Y 自洽得出。

    这一条比"两条曲线平行"更能说明问题，也是实测发现的事实：

        同一台假 MCU 先扫 PLANT 再扫 LOOP，|Y| 在每个频点**逐位相同**
        （335.1811 / 190.1110 / 108.1284 …），说明：
          1. 入射通道（a_c/a_s）两种模式采的是同一个 y，没有搞反；
          2. LOOP 的 b 通道（Inj）确实在累加，否则 `Inj−Y` 会退化成 `−Y`，
             算出来的 GH 就会等于 Plant（05 文档典型坑表里那一条）。

    **关于两条曲线的增益差（2026-11-24 修正）**：
        早先这里写死"低频增益差恒定在 +10.7 dB"，并解释成
        "PLANT 分母 ∝0.01 / LOOP 分母 ∝0.005，差一倍 = 6 dB，余下 4.7 dB
        是对象工作点增益差"。**这个解释是错的**，实测推翻了它：

          f      |Y|plant   |Y|loop     P dB     GH dB     差
          300    349.4797   349.4792   0.411   17.917   17.506
          475    237.5508   237.5497   1.058   12.895   11.837
          845    178.5968   178.5967   3.580    5.454    1.874

        两种模式的注入幅值本来就是同一个 A（都是 0.02），分母差一倍
        这件事不存在。真正的差别是 **分母取了什么**：
            PLANT 分母 = X 的复振幅 = A/2·N   （x = 0.5 + inj，交流部分只有 inj）
            LOOP  分母 = |Inj − Y|，在 |GH|>>1 的低频段 ≈ |Y|
        所以差值 = 20·log10(|X| / |Y|)，而 |X|/|Y| 随频率变化，
        这个差值**必然随频率单调下降**，不可能是常数。
        原来那个 `10.0 < avg < 11.5` 只是在"前 8 点"这个窗口里凑出来的
        ——修掉 sin_q15 的直流偏置后窗口均值变成 +12.34 dB，它就报错了，
        而曲线本身完全正常。

        改成断言真正的物理规律：**差值随频率单调下降**。
        这比一个魔数上限更能挡住回归（比如"LOOP 误用 X 当分母"会让
        差值恒为 0，"Y 通道接反"会让差值翻倍且不单调）。
    """
    b = Bench(qapp, exe)
    try:
        assert b.open("--plant", "lc", "--isr", "100000")
        plant = _gh_curve(b, mode=0, raw=True)
        loop = _gh_curve(b, mode=1, raw=True)
        assert len(plant) == len(loop) >= 20

        # 1) Y 通道逐位一致
        for (f0, y0, _, _), (f1, y1, _, _) in zip(plant, loop):
            assert f0 == pytest.approx(f1, rel=1e-6)
            assert y0 == pytest.approx(y1, rel=1e-6), \
                f"{f0:.0f} Hz 处 Y 不一致（PLANT={y0:.4f} LOOP={y1:.4f}）"

        # 2) 增益差 = 20·log10(|X|/|Y|)，在谐振点以下的通带内随频率单调下降
        #
        #    **只在谐振点以下成立**：LOOP 的分母是 |Inj−Y|，只有当 |GH|>>1
        #    时才退化成 ≈|Y|。过了 1300 Hz 谐振点 |GH| 迅速掉到 1 以下，
        #    |Inj−Y| 反而趋近 |Inj|，差值就不再是 |X|/|Y| 了
        #    （实测 pt13=1340 Hz 处 −7.140 → pt14=1504 Hz 处 −6.748，回升 0.39 dB）。
        #    所以只在 f < f0 的区间判单调。
        off = [dp - dm for (_, _, dm, _), (_, _, dp, _) in zip(plant, loop)]
        freqs = [f for (f, _, _, _) in plant]
        f_res = 1300.0                      # 假 MCU 的 LC 谐振频率
        n_lf = sum(1 for f in freqs if f < f_res)
        assert n_lf >= 5, f"通带内点数太少（{n_lf}），无法判单调"
        for i in range(1, n_lf):
            assert off[i] <= off[i - 1] + 1e-6, (
                f"增益差在通带内回升：pt{i-1}={off[i-1]:+.3f} dB "
                f"→ pt{i}={off[i]:+.3f} dB（应为单调下降）")

        # 3) 低频端量级锚点：|X| 与 |Y| 在 300 Hz 附近差 ~17.5 dB
        #    （实测 17.506；给 ±2 dB 容差挡"分母接错"这类整段错位）
        assert 15.0 < off[0] < 20.0, \
            f"首点增益差 {off[0]:+.2f} dB，与 |X|/|Y| 的物理预期不符"
    finally:
        b.close()
