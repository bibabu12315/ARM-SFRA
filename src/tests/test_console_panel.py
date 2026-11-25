"""串口日志面板（`app.ui.widgets.console`）的行为锁。

为什么单独成文件：**只碰 QWidget，不起任何子进程**。
项目里已经有一条硬约束（见 `test_coeff_link.py` 顶部说明）：
同进程里只要创建过 QWidget，后续 `QProcess.start()` 就会崩在 Qt 内部。
所以界面用例归界面文件、链路用例归链路文件，pytest 按文件名顺序跑
才不会踩这个陷阱。**别把这个文件里的用例挪去和链路测试同文件。**

这些用例锁的都是"坏了也不报错、只是悄悄不对"的行为：
过滤不生效、环形缓冲没上限、扫频中还能发命令打断时序。
"""
from __future__ import annotations

import pytest

QtWidgets = pytest.importorskip("PyQt5.QtWidgets")

from app.comm import protocol as P                                    # noqa: E402
from app.ui.widgets import console as C                               # noqa: E402


@pytest.fixture
def con():
    w = C.SerialConsole()
    yield w
    w.close()


# ------------------------------------------------------------------ 解析
def test_parse_command_known_names():
    assert C.parse_command("ping") == (P.Cmd.PING, b"")
    assert C.parse_command("caps") == (P.Cmd.GET_CAPS, b"")
    assert C.parse_command("abort") == (P.Cmd.ABORT, b"")


def test_parse_command_accepts_enum_name_and_hex():
    """调试时手写枚举名或命令号应该都能用。"""
    assert C.parse_command("GET_STATUS") == (P.Cmd.GET_STATUS, b"")
    assert C.parse_command("get_status") == (P.Cmd.GET_STATUS, b"")
    assert C.parse_command("0x06") == (6, b"")


def test_parse_command_rejects_junk():
    assert C.parse_command("") is None
    assert C.parse_command("help") is None        # 固件没有 ASCII shell
    assert C.parse_command("sfra cost") is None
    assert C.parse_command("0x99") is None        # 0x80 以上是应答号，不能发


def test_parse_command_does_not_allow_sending_response_codes():
    """0x80 以上是 MCU→PC 的应答号，上位机发出去没有意义。
    这里守住 `< 0x80` 这条线，防止有人顺手放宽。"""
    for v in (0x80, 0x81, 0x86, 0xFF):
        assert C.parse_command(f"0x{v:02X}") is None


# ------------------------------------------------------------------ 缓冲
def test_ring_buffer_is_bounded(con):
    """长跑不撑爆内存：超出 MAX_LINES 丢最旧的。"""
    con.clear()
    for i in range(C.MAX_LINES + 120):
        con.log_info(f"line {i}")
    assert len(con._lines) == C.MAX_LINES
    assert con._lines[-1].msg == f"line {C.MAX_LINES + 119}"
    # 最旧的那批必须真的被丢掉了，而不是只是不显示
    assert con._lines[0].msg == f"line {120}"


def test_ring_buffer_keeps_counts_consistent(con):
    """丢旧行时计数也要跟着减，否则 chip 上的数字会越跑越大。"""
    con.clear()
    con.log_err("第一个错误")
    for i in range(C.MAX_LINES + 10):
        con.log_info(f"x{i}")
    assert con._counts[C.K_ERR] == 0, "被挤掉的错误仍被计数"


# ------------------------------------------------------------------ 过滤
def test_filter_shows_only_matching_kind(con):
    con.clear()
    con.log_tx("→ PING")
    con.log_rx("← PING_ACK")
    con.log_err("← NAK")
    con.log_sys("系统提示")

    con._set_filter(C.K_ERR)
    text = con._log.toPlainText()
    assert "NAK" in text
    assert "PING_ACK" not in text
    assert "系统提示" not in text

    con._set_filter(None)
    text = con._log.toPlainText()
    assert "NAK" in text and "PING_ACK" in text


def test_filter_chips_are_mutually_exclusive(con):
    con.clear()
    con._set_filter(C.K_TX)
    checked = [k for k, b in con._chips.items() if b.isChecked()]
    assert checked == [C.K_TX]


def test_filter_applies_to_lines_arriving_after_switch(con):
    """切到 ERR 之后再来的 RX 行不该冒出来——这是"忘记判断"的典型漏法。"""
    con.clear()
    con._set_filter(C.K_ERR)
    con.log_rx("← 不该出现")
    con.log_err("← 应该出现")
    text = con._log.toPlainText()
    assert "应该出现" in text
    assert "不该出现" not in text


# ------------------------------------------------------------------ 扫频保护
def test_scanning_disables_send(con):
    """扫频中必须禁发：额外帧会打乱 200 ms 轮询节奏，
    固件 500 ms 收不到有效帧就 ABORT。"""
    con.set_scanning(True)
    assert not con._in.isEnabled()
    assert not con._btn_send.isEnabled()
    assert "禁用" in con._in.placeholderText()

    con.set_scanning(False)
    assert con._in.isEnabled()
    assert con._btn_send.isEnabled()


def test_scanning_rejects_programmatic_submit(con):
    """光禁用输入框不够：`_submit` 也可能被快捷键/代码调用，必须自己拦。"""
    got = []
    con.commandSubmitted.connect(lambda c, p: got.append(c))
    con.set_scanning(True)
    con._submit("ping")
    assert got == [], "扫频中仍发出了命令"
    assert "忽略" in con._lines[-1].msg


def test_unknown_command_logs_error_and_does_not_emit(con):
    got = []
    con.commandSubmitted.connect(lambda c, p: got.append(c))
    con._submit("nonexistent")
    assert got == []
    assert con._lines[-1].kind == C.K_ERR


def test_known_command_emits_and_echoes_tx(con):
    got = []
    con.commandSubmitted.connect(lambda c, p: got.append((c, p)))
    con._submit("ping")
    assert got == [(int(P.Cmd.PING), b"")]
    assert con._lines[-1].kind == C.K_TX
    assert "ping" in con._lines[-1].msg


# ------------------------------------------------------------------ 统计与折叠
def test_fold_hides_body_but_keeps_counting(con):
    """注意：测试里窗口没 show，`isVisible()` 恒为 False。
    要判断"被折叠藏起来了"必须看 `isHidden()`（显式 hide 过）。"""
    con.clear()
    con.log_rx("a")
    con.set_folded(True)
    assert con._body.isHidden()
    con.log_err("b")            # 折叠时仍要记账，否则展开就丢了
    assert len(con._lines) == 2
    con.set_folded(False)
    assert not con._body.isHidden()
    assert "b" in con._log.toPlainText()


def test_folded_count_highlights_errors(con):
    """折叠态下标题栏是唯一可见处，有错误必须在那里看出来。"""
    con.clear()
    con.log_info("正常")
    con.set_folded(True)
    assert con._lb_cnt.property("hasErr") in (False, 0, None) or \
        not con._lb_cnt.property("hasErr")
    con.log_err("出错了")
    assert con._lb_cnt.property("hasErr") is True
    assert "错误" in con._lb_cnt.text()


def test_clear_resets_everything(con):
    con.clear()
    con.log_err("x")
    con.log_rx("y")
    con.clear()
    assert con._lines == []
    assert all(v == 0 for v in con._counts.values())
    assert con._log.toPlainText() == ""


# ------------------------------------------------------------------ 帧渲染
def test_describe_frame_skips_data_frames():
    """DATA 帧不记：一次扫频几十帧，会把有意义的信息冲掉。"""
    f = P.Frame(cmd=int(P.Cmd.DATA), payload=b"\x00" * 20)
    assert C.describe_frame(f) is None


def test_describe_frame_renders_ack_ok_and_nak():
    ok = P.Frame(cmd=int(P.Cmd.ACK),
                 payload=bytes([int(P.Cmd.START), int(P.ErrCode.OK)]))
    kind, text = C.describe_frame(ok)
    assert kind == C.K_RX and "START" in text

    nak = P.Frame(cmd=int(P.Cmd.ACK),
                  payload=bytes([int(P.Cmd.GET_DATA),
                                 int(P.ErrCode.INDEX_RANGE)]))
    kind, text = C.describe_frame(nak)
    assert kind == C.K_ERR, "NACK 必须按错误着色"
    assert "INDEX_RANGE" in text
    assert "GET_DATA" in text, "要指明是哪条命令被拒，否则日志没法定位"


def test_describe_frame_renders_status_with_state_name():
    st = P.StatusInfo(state=int(P.State.COLLECT), flags=0,
                      freq_index=12, cur_f_act=1234.5)
    f = P.Frame(cmd=int(P.Cmd.STATUS), payload=st.pack())
    kind, text = C.describe_frame(f, n_points_total=47)
    assert kind == C.K_RX
    assert "COLLECT" in text
    assert "12/47" in text
    assert "1234.5" in text


def test_describe_frame_does_not_crash_on_short_payload():
    """半帧/畸形帧在真机上一定出现，渲染层不能因此抛异常。"""
    for cmd in (P.Cmd.ACK, P.Cmd.NACK, P.Cmd.PING_ACK, P.Cmd.CAPS,
                P.Cmd.STATUS, P.Cmd.COEFF, P.Cmd.CONDITIONS):
        f = P.Frame(cmd=int(cmd), payload=b"")
        r = C.describe_frame(f)
        assert r is None or isinstance(r, tuple)


# ------------------------------------------------------------------ TX 过滤
def test_poll_frames_hidden_by_default(con):
    """GET_STATUS/GET_DATA 每 200 ms 一次，默认不记。"""
    con.clear()
    con.log_frame_out(int(P.Cmd.GET_STATUS), b"", True)
    assert con._lines == []


def test_poll_frames_shown_when_tx_enabled(con):
    con.clear()
    con._btn_tx.setChecked(True)
    con._on_toggle_tx()          # 会额外记一条 SYS 说明"开始记录 TX"
    n = len(con._lines)
    con.log_frame_out(int(P.Cmd.GET_DATA), b"\x00\x00\x07", True)
    assert len(con._lines) == n + 1
    assert con._lines[-1].kind == C.K_TX
    assert "idx=0 n=7" in con._lines[-1].msg


def test_non_poll_frames_always_shown(con):
    """SET_CONFIG/START/ABORT 是扫频骨架，不开 TX 也该记。"""
    con.clear()
    con.log_frame_out(int(P.Cmd.SET_CONFIG), b"", False)
    con.log_frame_out(int(P.Cmd.START), b"", False)
    assert len(con._lines) == 2
    assert all(l.kind == C.K_TX for l in con._lines)


# ------------------------------------------------------------------ 端口
def test_set_port_updates_dot(con):
    con.set_port("COM14 · 921600", True)
    assert con._lb_port.text() == "COM14 · 921600"
    assert con._dot.property("on") is True
    con.set_port("未连接", False)
    assert con._dot.property("on") is False
