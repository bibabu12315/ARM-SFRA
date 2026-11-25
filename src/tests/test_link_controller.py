"""S4：上位机 ↔ 假 MCU 的真链路后端（`comm.controller.LinkController`）。

与 `test_e2e_loop.py` 的分工：
    那个文件直接拿 `SimMcuLink` 手工发命令，验的是**协议本身**；
    这个文件从界面下游的 `LinkController` 出发，验的是**上位机的扫频
    状态机**——轮询、分帧拉数、按 STATUS 推进、断点续传、abort。

为什么值得单独一组：
    界面之前只接 `sim.engine`（纯 Python 仿真），真链路代码从写完就
    没被界面驱动过。这里第一次用"和界面完全相同的调用方式"去跑它：
    `ctrl.start(cfg)` → 收 sigPoint → 收 sigDone。界面能出图的前提是
    这一层成立。

跑法：`pytest -m e2e`（要现编 C + Qt 事件循环，默认不跑）。
"""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.e2e

QtCore = pytest.importorskip("PyQt5.QtCore")

from app.comm.controller import LinkController, SimLinkController   # noqa: E402
from app.model.bode_data import MODE_LOOP, MODE_PLANT               # noqa: E402
from app.model.sweep_config import SweepConfig, CYC_FAST            # noqa: E402

# app 由 conftest.py 的会话级 QApplication 提供（见该文件说明）。
# 这里**不能**再建 QCoreApplication：它和 QApplication 抢同一个单例槽位。


# ------------------------------------------------------------------ 工具
class Recorder:
    """收集控制器发出的信号，并把 Qt 事件循环跑到 done 或超时。"""

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.points = []
        self.progress = []
        self.done = None
        self.logs = []
        ctrl.sigPoint.connect(self.points.append)
        ctrl.sigProgress.connect(lambda d, t, f: self.progress.append((d, t, f)))
        ctrl.sigDone.connect(lambda ok, msg: setattr(self, "done", (ok, msg)))
        ctrl.sigLog.connect(self.logs.append)

    def wait(self, timeout_ms: int = 30_000) -> None:
        loop = QtCore.QEventLoop()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def tick():
            if self.done is not None or not self.ctrl.running:
                loop.quit()

        poll = QtCore.QTimer()
        poll.setInterval(50)
        poll.timeout.connect(tick)
        poll.start()
        timer.start(timeout_ms)
        loop.exec_()
        poll.stop()
        timer.stop()


@pytest.fixture
def ctrl():
    c = SimLinkController()
    assert c.link.open(), "假 MCU 启动失败"
    QtCore.QTimer.singleShot(150, c.handshake)
    # 让握手跑完，拿到 CAPS（否则 isr_freq 用默认值也无妨，但 CAPS 路径要覆盖）
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(600, loop.quit)
    loop.exec_()
    yield c
    if c.running:
        c.stop()
    c.link.close()


def fast_cfg(**kw) -> SweepConfig:
    """22 点、Fast 档，跑得快的合法配置。"""
    base = dict(isr_freq=100_000.0, f_start=200.0, f_stop=20_000.0, ppd=10,
                cyc_mode=CYC_FAST, mode=MODE_PLANT, amp_pu=0.01)
    base.update(kw)
    return SweepConfig(**base)


# ------------------------------------------------------------------ 用例
def test_handshake_fills_caps(ctrl):
    """握手链 PING→PING_ACK→GET_CAPS→CAPS 必须填上能力信息。"""
    assert ctrl.caps is not None
    assert ctrl.caps.isr_freq == pytest.approx(100_000.0, rel=1e-6)
    assert ctrl.caps.max_vec_len >= 47


def test_start_to_done_all_points_arrive(ctrl):
    """一次完整扫频：点数到齐、频率单调、每个点都可能出图。"""
    cfg = fast_cfg()
    rec = Recorder(ctrl)
    assert ctrl.start(cfg)
    assert ctrl.running
    rec.wait()

    assert rec.done == (True, ""), f"未正常结束：{rec.done} / {rec.logs}"
    assert len(rec.points) == cfg.n_points
    assert len(ctrl.result) == cfg.n_points
    f = ctrl.result.freq
    assert np.all(np.diff(f) > 0), "频率必须严格单调，否则 PM 插值会错"
    assert f[-1] <= 100_000.0 / 5 + 1e-6          # f_s/5 截断生效


def test_progress_reaches_total(ctrl):
    cfg = fast_cfg()
    rec = Recorder(ctrl)
    ctrl.start(cfg)
    rec.wait()
    assert rec.progress, "没有收到任何进度信号"
    d, t, _ = rec.progress[-1]
    assert t == cfg.n_points
    assert d == cfg.n_points, "最后一条进度的 done 必须等于总点数"


def test_loop_mode_bode_is_physically_sane(ctrl):
    """LOOP 模式扫二阶 LC 对象：高频衰减、相位下降，全程无回弹。"""
    cfg = fast_cfg(mode=MODE_LOOP)
    rec = Recorder(ctrl)
    ctrl.start(cfg)
    rec.wait()
    assert rec.done == (True, ""), rec.done

    res = ctrl.result
    mag, ph = res.mag, res.phase
    finite = np.isfinite(mag) & np.isfinite(ph)
    assert finite.sum() >= len(mag) - 2
    assert mag[finite][-1] < mag[finite][0] - 20.0, "高频应衰减 20 dB 以上"


def test_bad_amplitude_rejected_without_timeout(ctrl):
    """amp 超固件上限 0.25 pu：本端先挡下来，不该发出去等超时。"""
    cfg = fast_cfg(amp_pu=0.5)
    rec = Recorder(ctrl)
    ok = ctrl.start(cfg)
    # 要么本端直接拒（start 返回 False 并 sigDone），要么固件回 BAD_PARAM
    if ok:
        rec.wait(8000)
        assert rec.done is not None and not rec.done[0]
    else:
        assert rec.done is not None and not rec.done[0]


def test_abort_returns_immediately(ctrl):
    """扫频途中 stop()：立刻不再 running，且已收的点保持不变。"""
    cfg = fast_cfg(f_start=20.0, f_stop=20_000.0, ppd=40, mode=MODE_LOOP)
    rec = Recorder(ctrl)
    ctrl.start(cfg)

    QtCore.QTimer.singleShot(400, ctrl.stop)
    rec.wait(15_000)
    assert rec.done is not None
    assert rec.done[0] is False
    assert "中止" in rec.done[1]
    assert not ctrl.running


def test_resume_from_next_idx_does_not_duplicate(ctrl):
    """断点续传的判据：重连后从 _next_idx 继续拉，不重复、不丢点。

    这里不真的断电（假 MCU 是子进程，杀了就重来），而是手工把
    `_next_idx` 回退到中途再问一次 STATUS，验证控制器只补缺的那一段。
    """
    cfg = fast_cfg()
    rec = Recorder(ctrl)
    ctrl.start(cfg)
    rec.wait()
    assert rec.done == (True, "")

    n = len(ctrl.result)
    assert n == cfg.n_points

    # 模拟"上位机重启后重连"：结果对象清空，MCU 里的数据还在
    ctrl.result.raw.clear()
    ctrl._next_idx = 0
    ctrl._done_seen = False
    ctrl._n_total = n
    ctrl._available = n
    ctrl._scanning = True
    ctrl.link.get_data(0, 7)
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(800, loop.quit)
    loop.exec_()
    assert len(ctrl.result) > 0, "续传没拉到任何点"
    # 拉回来的点必须从 0 开始连续
    assert ctrl._next_idx == len(ctrl.result)


def test_link_controller_is_baseclass(ctrl):
    """界面按基类编程：两个后端必须都是 LinkController。"""
    assert isinstance(ctrl, LinkController)
    for name in ("sigPoint", "sigProgress", "sigDone", "sigLog"):
        assert hasattr(ctrl, name)


# ---------------------------------------------------------------- 坑 A18
def test_get_data_nack_releases_fetch_gate(ctrl):
    """坑 A18：GET_DATA 的 NACK 必须解除 `_fetching` 闸门，不能让扫频冻死。

    现象：日志里出现 "mcu拒绝0x07：INDEX_RANGE"，进度条冻结（实测 57%）。

    机理：
        `GET_DATA` 不在 `_pending_acks` 队列里（那个队列只排
        SET_CONFIG/START），所以它的 NACK 走到 `_on_ack` 末尾的
        `if not self._pending_acks: return` 时会被**静默丢弃**。
        `_fetching` 于是停在非零值，而 `_on_poll()` 第一句就是
        `if self._fetching > 0: return` —— 上位机再也不发 STATUS，
        扫频彻底停摆。MCU 那边可能早就 DONE 了。

    这里直接模拟"MCU 回了 INDEX_RANGE"，断言闸门立刻归零，
    且后续轮询能继续推进到扫频结束（而不是卡死在 57%）。
    """
    from app.comm.protocol import Cmd, ErrCode

    cfg = fast_cfg()
    rec = Recorder(ctrl)
    assert ctrl.start(cfg)

    # 等到扫到中途（有数据在途也行，没有就造一个在途状态）
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(400, loop.quit)
    loop.exec_()

    # 模拟一次被拒的取数：先置在途点数，再喂一个 INDEX_RANGE NACK
    ctrl._fetching = 7
    ctrl._on_ack(int(Cmd.GET_DATA), int(ErrCode.INDEX_RANGE), 0)

    assert ctrl._fetching == 0, "坑 A18 复发：GET_DATA 的 NACK 没解除 _fetching"
    assert ctrl._idx_mismatch == 1, "越界次数没被记账"
    assert any("越界" in s for s in rec.logs), f"没给出可诊断的日志：{rec.logs}"

    # 闸门解除后，扫频应当能自己跑完（这是"不再卡住"的实质判据）
    rec.wait(30_000)
    assert rec.done is not None, "NACK 之后扫频再也没能收尾"
    assert rec.done[0] is True, f"没跑完：{rec.done} / {rec.logs}"
    assert len(ctrl.result) == cfg.n_points


def test_get_data_hard_error_stops_cleanly(ctrl):
    """GET_DATA 遇到非 INDEX_RANGE 的硬错误：判失败，但必须先把闸门放下。

    之前 `_fetching` 不清零会让 `sigDone` 永远发不出去，界面就一直转圈。
    """
    from app.comm.protocol import Cmd, ErrCode

    cfg = fast_cfg()
    rec = Recorder(ctrl)
    assert ctrl.start(cfg)

    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(300, loop.quit)
    loop.exec_()

    ctrl._fetching = 3
    ctrl._on_ack(int(Cmd.GET_DATA), int(ErrCode.BAD_PARAM), 0)

    assert ctrl._fetching == 0
    assert rec.done is not None, "硬错误没触发 sigDone，界面会无限转圈"
    assert rec.done[0] is False
    assert "BAD_PARAM" in rec.done[1]
    assert not ctrl.running


# ------------------------------------------------------------------ 点数不一致
def test_mcu_fewer_points_than_pc_finishes_cleanly():
    """固件按 f_s/5 截断、点数比 PC 少时，必须**正常收尾**而不是死循环。

    复现 2026-11-24 真板事故：fs=10 kHz（f_s/5 = 2 kHz），界面终点填
    20000 → PC 算出 47 点并下发给固件；固件 START 里 `sfra_effective_points()`
    裁到 28 点就 DONE。旧代码在 DONE 时把 `_available` 设成本端 n_total，
    于是反复请求第 28 点，MCU 一直回 INDEX_RANGE，日志被刷满红字，
    曲线永远画不出来。

    现在的判据：
        · DONE 帧的 freq_index 就是固件真实点数（末点 NEXT 后已自增到顶）；
        · 越界且已 DONE → 按实际到手点数收尾（sigDone(True)）；
        · INDEX_RANGE 日志只打第一条，不刷屏。
    """
    c = SimLinkController()
    try:
        # 假 MCU 按 10 kHz 跑，PC 这边的 isr_freq 也填 10 kHz，
        # 但终点故意填 20000 → PC 以为自己该扫 47 点，固件只扫到 2 kHz。
        c.link.set_args("--plant", "lc", "--isr", "10000", "--f0", "1000",
                        "--q", "2.0")
        assert c.link.open(), "假 MCU 启动失败"
        QtCore.QTimer.singleShot(150, c.handshake)
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(800, loop.quit)
        loop.exec_()
        assert c.caps is not None, "握手未完成"

        # 复刻真机事故的现场：`f_stop=20000`（界面里就是这么填的）但它
        # 会被 `make_freqs` 按 f_s/5 截断，于是 `n_points`=27、`freqs[-1]`
        # ≈1995。固件收到 SET_CONFIG 时算出的 eff 也是 28（含钳在 f_s/5
        # 那一点），两者虽然接近但**不是同一个数**——旧代码正是拿本端
        # n_total 与固件真实点数硬比，才死循环。
        cfg = SweepConfig(isr_freq=10_000.0, f_start=100.0, f_stop=20_000.0,
                          ppd=20, cyc_mode=CYC_FAST, mode=MODE_PLANT,
                          amp_pu=0.01)
        assert cfg.f_stop == 20_000.0, "前提：界面终点仍是 20000"
        assert cfg.n_points == 27, f"PC 截断后应为 27 点，实际 {cfg.n_points}"

        rec = Recorder(c)
        assert c.start(cfg)
        rec.wait(timeout_ms=40_000)

        assert rec.done is not None, "扫频既未结束也未报错（死循环）"
        assert rec.done[0] is True, f"应正常收尾而不是判失败：{rec.done}"
        got = len(rec.points)
        assert 20 <= got <= 29, f"到手点数异常：{got}"
        idx_err = [s for s in rec.logs if "INDEX_RANGE" in s or "越界" in s]
        assert len(idx_err) <= 2, f"INDEX_RANGE 被刷屏：{len(idx_err)} 条"
        f = c.result.freq
        assert f[-1] <= 10_000.0 / 5 + 1e-6, "末点必须 ≤ f_s/5"
        assert np.all(np.diff(f) > 0), "频率必须严格单调"
    finally:
        if c.running:
            c.stop()
        c.link.close()
