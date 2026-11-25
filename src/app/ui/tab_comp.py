"""SFRA Studio - Tab2 补偿器。

三条曲线：
    1. G(z) 补偿器自身响应（琥珀色）
    2. 实测 GH（蓝，来自 Tab1）
    3. 预测 GH = 实测 Plant × 当前 G（紫虚线）

关键点：**改 Kp/Ki 不用重扫**。只要 Tab1 有一份 Plant 数据，
就能在 PC 上直接把新 G 乘上去，实时看 PM 怎么变——这正是用户
"想合成成一个上位机"的核心诉求。
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from .. import theme
from ..model.analysis import analyze
from ..model.bode_data import MODE_LOOP, MODE_PLANT, SweepResult, mag_db, phase_deg
from .widgets.bode_plot import BodePlot, Curve
from .widgets.common import (BigValue, Card, Group, KVRow, Note, NumEdit, Row,
                              SegmentedControl, Switch, section_label,
                              num_with_unit)


# ---------------------------------------------------------------- 补偿器数学
def pi_z(kp: float, ki: float, fs: float, f: np.ndarray,
         ui_gain: float = 1.0) -> np.ndarray:
    """增量式 PI 的离散传递函数  G(z) = Kp + Ki·Ts·z/(z-1)。

    推导（ISR 里的写法）：
        u[n] = u[n-1] + Kp(e[n]-e[n-1]) + Ki·e[n]
    取 z 变换：
        U(1-z⁻¹) = [Kp(1-z⁻¹) + Ki]·E
        G(z) = Kp + Ki/(1-z⁻¹) = Kp + Ki·Ts·z/(z-1)

    在 z = e^{jωTs} 处取值即频率响应。
    """
    ts = 1.0 / fs
    w = 2.0 * np.pi * f / fs
    z = np.exp(1j * w)
    return ui_gain * (kp + ki * ts * z / (z - 1.0))


# ---------------------------------------------------------------- 页面
class TabComp(QtWidgets.QWidget):
    """补偿器页。"""

    coeffChanged = QtCore.pyqtSignal(list)   # 只在"下发"时发
    readbackRequested = QtCore.pyqtSignal()  # 只在"读回"时发

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plant: Optional[np.ndarray] = None      # 实测 Plant 复数数组
        self._plant_f: Optional[np.ndarray] = None
        self._gh: Optional[SweepResult] = None
        self._fs = 100_000.0
        self._coeffs: list[float] = [0.0, 0.0, 0.0, -1.0, 0.0]
        self._trusted = None

        self._build()
        self._wire()
        self.refresh()

    # ================================================================ 构建
    def _build(self) -> None:
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_left())
        root.addWidget(self._build_mid(), 1)

    def _build_left(self) -> QtWidgets.QWidget:
        col = QtWidgets.QWidget()
        col.setObjectName("ColLeft")
        col.setFixedWidth(320)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(0)

        # ---- 类型 ----
        lay.addWidget(section_label("补偿器类型"))
        g = Group()
        lay.addWidget(g)
        lay.addSpacing(6)
        self.seg_type = SegmentedControl(["PI", "2P2Z"], 0, pad=(4, 10))
        g.add_row(Row("结构", self.seg_type))

        # ---- 参数 ----
        lay.addSpacing(16)
        lay.addWidget(section_label("参数"))
        gp = Group()
        lay.addWidget(gp)
        lay.addSpacing(6)

        self.in_kp = NumEdit(0.5, 4, 0.0, 1e5)
        gp.add_row(Row("Kp", self.in_kp))
        self.in_ki = NumEdit(1200, 1, 0.0, 1e7)
        gp.add_row(Row("Ki", self.in_ki))
        self.in_kd = NumEdit(0.0, 4, 0.0, 1e5)
        gp.add_row(Row("Kd（2P2Z）", self.in_kd))
        self.in_fs = NumEdit(100, 1, 1, 500)
        gp.add_row(Row("控制频率", num_with_unit(self.in_fs, "kHz")))
        self.in_ug = NumEdit(1.0, 3, 0.001, 1e3)
        gp.add_row(Row("执行量增益", self.in_ug))

        # ---- 等效差分方程 ----
        lay.addSpacing(16)
        lay.addWidget(section_label("等效差分方程"))
        ge = Group()
        lay.addWidget(ge)
        lay.addSpacing(6)
        self.kv_b0 = KVRow("b0", "—")
        self.kv_b1 = KVRow("b1", "—")
        self.kv_b2 = KVRow("b2", "—")
        self.kv_a1 = KVRow("a1", "—")
        self.kv_a2 = KVRow("a2", "—")
        for w in (self.kv_b0, self.kv_b1, self.kv_b2, self.kv_a1, self.kv_a2):
            ge.add_row(w)

        lay.addStretch(1)

        # ---- 动作 ----
        lay.addSpacing(10)
        self.btn_apply = QtWidgets.QPushButton("下发系数到 MCU")
        self.btn_apply.setObjectName("Primary")
        self.btn_apply.setMinimumHeight(36)
        self.btn_apply.setEnabled(False)
        self.btn_apply.setToolTip(
            "把当前的 b0/b1/b2/a1/a2 写入 MCU 的补偿器。\n"
            "直接改 Kp/Ki 不会影响 MCU，必须点这里才生效。")
        lay.addWidget(self.btn_apply)
        lay.addSpacing(6)
        self.btn_read = QtWidgets.QPushButton("从 MCU 读回")
        self.btn_read.setEnabled(False)
        self.btn_read.setToolTip(
            "读回 MCU 中当前生效的系数，并覆盖上面的输入框。\n"
            "用于确认「界面显示」与「固件实际」是否一致。")
        lay.addWidget(self.btn_read)

        lay.addSpacing(16)
        lay.addWidget(section_label("预测"))
        sw_row = QtWidgets.QWidget()
        sl = QtWidgets.QHBoxLayout(sw_row)
        sl.setContentsMargins(10, 8, 10, 8)
        sl.setSpacing(8)
        sw_row.setStyleSheet(
            f"background:{theme.INSET}; border-radius:9px;")
        self.sw_pred = Switch(True)
        t = QtWidgets.QLabel("实时预览 实测 Plant × 当前 G")
        t.setStyleSheet(f"color:{theme.T2}; font-size:11.5px;")
        t.setWordWrap(True)
        sl.addWidget(self.sw_pred)
        sl.addWidget(t, 1)
        lay.addWidget(sw_row)

        scroll.setWidget(inner)
        outer = QtWidgets.QVBoxLayout(col)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        return col

    def _build_mid(self) -> QtWidgets.QWidget:
        col = QtWidgets.QWidget()
        col.setObjectName("ColMid")
        lay = QtWidgets.QVBoxLayout(col)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(0)

        # ---- 补偿器响应 ----
        c1 = Card("补偿器响应", "|G| dB")
        c1.add_legend("G(z)", theme.AMBER)
        self.plot_g = BodePlot()
        self.plot_g.setMinimumHeight(200)
        c1.body_layout.addWidget(self.plot_g, 1)
        lay.addWidget(c1, 2)
        lay.addSpacing(11)

        # ---- 预测环路 ----
        c2 = Card("环路增益", "实测 vs 预测")
        c2.add_legend("实测 GH", theme.BLUE)
        c2.add_legend("预测 Plant×G", theme.PURPLE)
        self.plot_gh = BodePlot()
        self.plot_gh.setMinimumHeight(220)
        c2.body_layout.addWidget(self.plot_gh, 1)
        lay.addWidget(c2, 3)
        lay.addSpacing(11)

        # ---- 预测读数 ----
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self._box_pm = self._metric_box("预测 PM", "°")
        self._box_fc = self._metric_box("预测 fc", "Hz")
        self._box_d = self._metric_box("与实测差", "°")
        for b in (self._box_pm, self._box_fc, self._box_d):
            row.addWidget(b[0], 1)
        lay.addLayout(row)

        return col

    def _metric_box(self, title: str, unit: str):
        box = QtWidgets.QFrame()
        box.setObjectName("Card")
        bl = QtWidgets.QVBoxLayout(box)
        bl.setContentsMargins(11, 9, 11, 9)
        bl.setSpacing(1)
        t = QtWidgets.QLabel(title)
        t.setStyleSheet(f"color:{theme.T3}; font-size:10.5px;")
        v = BigValue(unit)
        v.num.setStyleSheet(f"font-size:19px; font-weight:600;")
        v.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        bl.addWidget(t)
        bl.addWidget(v)
        return box, v

    # ================================================================ 信号
    def _wire(self) -> None:
        for w in (self.in_kp, self.in_ki, self.in_kd, self.in_fs, self.in_ug):
            w.valueChanged.connect(self.refresh)
        self.seg_type.changed.connect(lambda _: self.refresh())
        self.sw_pred.toggled.connect(lambda _: self.refresh())
        # 这两个按钮此前**从未被 connect**——信号发了没人收，
        # "下发系数"按下去等于什么都没发生（W1）。这里补上。
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_read.clicked.connect(self._on_read)

    def _on_apply(self) -> None:
        # 先刷一次，确保 _coeffs 与界面上的 Kp/Ki 同步（用户可能刚改完没失焦）
        self._update_equiv()
        self.coeffChanged.emit(self.coefficients())

    def _on_read(self) -> None:
        self.readbackRequested.emit()

    # ================================================================ 外部注入
    def set_context(self, result: Optional[SweepResult], fs: float,
                    kp_used: Optional[float] = None,
                    ki_used: Optional[float] = None) -> None:
        """Tab1 扫完后调用。

        如果给的是 Loop Gain 结果，要反推出 Plant：
            GH = Plant · G  →  Plant = GH / G

        **必须用"扫频当时"的 Kp/Ki**（kp_used/ki_used），不能用界面当前的——
        否则用户改了 Kp 之后再 set_context，反推和乘回用的是同一组系数，
        预测值与实测永远相等（这个 bug 就是循环依赖）。

        反推只在收到新数据的这一刻做一次；之后改 Kp 只改 G，
        Plant 保持实测值不动。
        """
        self._fs = float(fs)
        self.in_fs.set_value(fs / 1000.0)
        self._gh = None
        self._plant = None
        self._plant_f = None
        self._trusted = None

        if result is None or len(result) < 2:
            self.refresh()
            return

        # 可信掩码：Loop 模式下高频段 GH 不可靠，反推的 Plant 也不可靠
        keep = result.trusted_mask()
        if result.mode == MODE_LOOP:
            self._trusted = keep
        else:
            self._trusted = ~result.invalid_mask()

        if result.mode == MODE_LOOP:
            self._gh = result
            kp = self.in_kp.value() if kp_used is None else float(kp_used)
            ki = self.in_ki.value() if ki_used is None else float(ki_used)
            g = pi_z(kp, ki, self._fs, result.freq, self.in_ug.value())
            self._plant_f = result.freq
            self._plant = result.h / g
        else:
            self._plant_f = result.freq
            self._plant = result.h.copy()

        self.refresh()

    def set_serial(self, ok: bool) -> None:
        self.btn_apply.setEnabled(ok)
        self.btn_read.setEnabled(ok)

    def clear_context(self) -> None:
        """丢掉上一轮实测，避免改参数时误用过期 Plant。"""
        self._gh = None
        self._plant = None
        self._plant_f = None
        self._trusted = None
        self.plot_g.clear()
        self.plot_gh.clear()
        for b in (self._box_pm, self._box_fc, self._box_d):
            b[1].set("—", theme.T3)

    # ================================================================ 重算
    def _g_at(self, f: np.ndarray) -> np.ndarray:
        kp = self.in_kp.value()
        ki = self.in_ki.value()
        kd = self.in_kd.value()
        ug = self.in_ug.value()
        fs = self._fs_hz()
        if self.seg_type.current() == 0:
            return pi_z(kp, ki, fs, f, ug)
        # 2P2Z：PI + 一阶低通（Kd 项），近似离散
        ts = 1.0 / fs
        w = 2 * np.pi * f / fs
        z = np.exp(1j * w)
        base = kp + ki * ts * z / (z - 1.0)
        if kd > 0:
            # 高通项：Kd·(1-z⁻¹)/Ts
            base = base + kd * (1 - 1 / z) / ts
        return ug * base

    def refresh(self) -> None:
        self._update_equiv()
        if self._plant_f is None or self._plant is None:
            self.plot_g.clear()
            self.plot_gh.clear()
            self._box_pm[1].set("—", theme.T3)
            self._box_fc[1].set("—", theme.T3)
            self._box_d[1].set("—", theme.T3)
            return

        # 只算可信频段：高频点 GH 本身不可靠，反推出来的 Plant 也是垃圾，
        # 再乘上去只会让预测曲线在右边长出一条混乱的尾巴。
        keep = self._trusted
        f = self._plant_f[keep]
        plant = self._plant[keep]
        if f.size < 2:
            return

        g = self._g_at(f)

        # ---- 曲线 1：G(z) ----
        cg = Curve(freq=f, mag=np.array([mag_db(v) for v in g]),
                   phase=np.array([phase_deg(v) for v in g]),
                   name="G(z)", color=theme.AMBER, fill=False, width=2.0)
        self.plot_g.set_curves([cg])
        self.plot_g.set_stability(None)

        # ---- 曲线 2/3：实测 GH + 预测 ----
        curves = []
        if self._gh is not None:
            curves.append(Curve(
                freq=self._gh.freq[keep], mag=self._gh.mag[keep],
                phase=self._gh.phase[keep], name="实测 GH",
                color=theme.BLUE))
        pred = plant * g
        pred_mag = np.array([mag_db(v) for v in pred])
        pred_ph = np.array([phase_deg(v) for v in pred])

        if self.sw_pred.isChecked():
            curves.append(Curve(freq=f, mag=pred_mag, phase=pred_ph,
                                name="预测", color=theme.PURPLE,
                                dashed=True, fill=False, width=1.8))
        self.plot_gh.set_curves(curves)

        st_pred = analyze(f, pred_mag, pred_ph)
        self.plot_gh.set_stability(st_pred)

        # ---- 读数 ----
        if st_pred.pm is None:
            self._box_pm[1].set("—", theme.T3)
        else:
            self._box_pm[1].set(f"{st_pred.pm:.1f}",
                                theme.GREEN_T if st_pred.pm_ok else theme.AMBER_T)
        if st_pred.fc is None:
            self._box_fc[1].set("—", theme.T3)
        elif st_pred.fc >= 1000:
            self._box_fc[1].set(f"{st_pred.fc/1000:.2f}k", theme.T1)
        else:
            self._box_fc[1].set(f"{st_pred.fc:.0f}", theme.T1)

        if self._gh is not None and st_pred.pm is not None:
            gh = self._gh
            st_meas = analyze(gh.freq[keep], gh.mag[keep], gh.phase[keep])
            if st_meas.pm is not None:
                d = st_pred.pm - st_meas.pm
                self._box_d[1].set(f"{d:+.1f}",
                                   theme.GREEN_T if abs(d) < 5 else theme.AMBER_T)
            else:
                self._box_d[1].set("—", theme.T3)
        else:
            self._box_d[1].set("—", theme.T3)

    def _fs_hz(self) -> float:
        """当前控制频率（Hz），**始终读输入框**。

        以前 `self._fs` 只在 `set_context()` 里赋值，用户在右侧
        「控制频率」框里手改数字时 `_fs` 不动，PI 的离散化仍用旧值，
        界面显示与实际下发**不一致**——而这条恰恰是 W1 读回要对账的量。
        改为单一来源：输入框。`_fs` 仅作 set_context 的镜像保留给外部读。
        """
        khz = self.in_fs.value()
        return khz * 1000.0 if khz > 0 else self._fs

    def _update_equiv(self) -> None:
        """把 Kp/Ki 换算成 TI 差分方程的 b0/b1/b2/a1/a2，写回界面。"""
        fs = self._fs_hz()
        kp, ki, kd = (self.in_kp.value(), self.in_ki.value(),
                      self.in_kd.value())
        ug = self.in_ug.value()
        ts = 1.0 / fs
        if self.seg_type.current() == 0:
            # G = ug·(Kp + Ki·Ts·z/(z-1)) = ug·[(Kp+Ki·Ts) - Kp·z⁻¹] / (1 - z⁻¹)
            #
            # **ug 必须乘进来**。`coefficients()` 的返回值会被下发到 MCU，
            # MCU 拿到 b/a 就直接跑差分方程，不会再乘一次 ug——
            # 界面上'等效差分方程'那几行显示的是**真正要执行的**差分方程，
            # 少了 ug 就与实际控制律差一个倍数（W1 才发现的老 bug）。
            self._coeffs = [ug * (kp + ki * ts), -ug * kp, 0.0, -1.0, 0.0]
        else:
            # 2P2Z：PI 再叠 Kd·(1-z⁻¹)/Ts
            self._coeffs = [ug * (kp + ki * ts + kd / ts),
                            -ug * (kp + 2 * kd / ts),
                            ug * kd / ts, -1.0, 0.0]

        b0, b1, b2, a1, a2 = self._coeffs
        self.kv_b0.set_value(f"{b0:.6f}")
        self.kv_b1.set_value(f"{b1:.6f}")
        self.kv_b2.set_value(f"{b2:.6f}")
        self.kv_a1.set_value(f"{a1:.6f}")
        self.kv_a2.set_value(f"{a2:.6f}")

    # ================================================================ 对外
    def coefficients(self) -> list[float]:
        """当前 b0,b1,b2,a1,a2（2P2Z 顺序），下发 MCU 用。

        **必须现场重算，不能返回 `self._coeffs` 缓存。**
        `NumEdit.set_value()` 默认不 emit（`emit=False`），所以程序化改
        Kp/Ki（比如扫频结束后回填"扫频当时的系数"）不会触发 refresh，
        `_coeffs` 会停留在旧值上——直接返回缓存就会把**上一次**的系数
        下发给 MCU，而界面显示的是新的。这种"界面与下发不一致"极难发现：
        两边都不报错，只是控制律变了。
        """
        self._update_equiv()
        return list(self._coeffs)

    def kp_ki(self) -> tuple[float, float]:
        return self.in_kp.value(), self.in_ki.value()

    # ---------------------------------------------------------------- 读回
    def set_coefficients(self, coeffs: list[float]) -> bool:
        """把 MCU 读回的系数填回界面（W1）。

        输入是 `b0,b1,b2,a1,a2`（与 `coefficients()` 同序）。
        协议里 `order` 就是系数个数，所以读回的可能只有 2 个（PI 无 a2 时
        也可能省掉尾部 0），这里补齐到 5 个再解析。

        逆向映射（PI，见 `_update_equiv` 的正向式
        `b0 = Kp + Ki·Ts`、`b1 = -Kp`）::

            Kp = -b1
            Ki = (b0 - Kp) / Ts = (b0 + b1) / Ts

        必须除回 `ug`（执行量增益），因为下发时乘过它。
        """
        vals = [float(v) for v in coeffs]
        if len(vals) < 2:
            return False
        while len(vals) < 5:
            vals.append(0.0)
        b0, b1, b2, _a1, _a2 = vals[:5]

        ts = 1.0 / self._fs_hz()
        ug = self.in_ug.value()
        if ug == 0.0 or not math.isfinite(ug):
            return False

        # 式子里 ug 是整体乘在 G 上的：b 系数都含 ug，a 系数不含。
        kp = -b1 / ug
        ki = (b0 + b1) / ug / ts
        kd = b2 * ts / ug

        # 用 blockSignals 避免每设一个值就 refresh 一次（三次重算）
        for w, v in ((self.in_kp, kp), (self.in_ki, ki), (self.in_kd, kd)):
            w.blockSignals(True)
            w.set_value(v)
            w.blockSignals(False)
        self.refresh()
        return True
