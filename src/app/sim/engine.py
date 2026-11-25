"""SFRA Studio - 扫描引擎（界面与算法之间的适配层）。

为什么不直接用 sfra_sim.sweep()：
    它是阻塞式整条跑完，界面会假死，也画不出"边扫边长"的动画。
    这里拆成"每来一个频点 → emit 一次"，界面就能实时追加曲线。

两个后端共用同一套对外接口：
    SimBackend  : 无硬件时用 PC 仿真跑（用户点"开始扫频"就能看到完整效果）
    SerialBackend: 接真实 MCU（S3/S6 阶段），逐点等 DATA 帧

两者的对外信号一致，界面代码不需要区分。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
from PyQt5 import QtCore

from ..model.bode_data import MODE_LOOP, MODE_PLANT, RawPoint, SweepResult
from ..sim import sfra_sim
from ..sim.plants import FirstOrderLP, PICompensator, SecondOrderLC


# ---------------------------------------------------------------- 仿真后端
class SimWorker(QtCore.QThread):
    """在子线程里逐点跑仿真，emit 每个完成的频点。

    之所以放线程里：Normal 档扫 68 点要几十秒，主线程跑会卡死动画。
    """

    pointReady = QtCore.pyqtSignal(object)          # RawPoint
    progress = QtCore.pyqtSignal(int, int, float)   # done, total, f_act
    finishedOk = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        c = self.cfg
        try:
            isr = c["isr_freq"]
            mode = c["mode"]
            amp = c["amp_pu"]
            cyc_mode = c["cyc"]
            freqs = c["freqs"]

            plant, comp = _make_dut(c)

            sim = sfra_sim.SimSfra(isr_freq=isr, amplitude=amp, mode=mode)
            n_total = len(freqs)
            ref_dc = float(c.get("ref_dc", 0.5))
            y_prev = 0.0
            u_prev = 0.0

            # 稳态预跑（与 sfra_sim.sweep 一致，固件的 SETTLE 状态）
            if mode == MODE_LOOP:
                for _ in range(2000):
                    e = ref_dc - y_prev
                    u = comp(e)
                    y_prev = plant(u)
                u_prev = y_prev

            for i, f_nom in enumerate(freqs):
                if self._abort:
                    return
                cyc = sfra_sim.calc_cycles(float(f_nom), cyc_mode, isr)
                data_count, f_act = sim.begin_point(float(f_nom), cyc)
                settle = sim.settle_left      # inject() 会递减，先存下来

                # 单 pass 同时采 Y 与 Inj（D19 修正）
                for _ in range(settle + data_count + 4):
                    if self._abort:
                        return
                    inj = sim.inject()
                    if mode == MODE_PLANT:
                        x = inj
                        y = plant(x)
                        sim.monitor(x, y)
                    else:
                        r = ref_dc + inj
                        e = r - y_prev
                        u = comp(e)
                        y_prev = plant(u)
                        sim.monitor(u, y_prev)
                    sim._advance()      # 采样之后推进相位（与 sfra_sim.sweep 一致）

                p = sim.make_point(f_act)
                self.pointReady.emit(p)
                self.progress.emit(i + 1, n_total, f_act)

            self.finishedOk.emit()
        except Exception as e:      # 仿真崩了也要让界面恢复
            self.failed.emit(f"{type(e).__name__}: {e}")


def _make_dut(cfg: dict):
    """按界面上的"被测对象"配置造一个功率级 + 补偿器。"""
    fs = float(cfg["isr_freq"])
    dut = cfg.get("dut", "buck")
    comp_cfg = cfg.get("comp", {})

    if dut == "first":
        plant = FirstOrderLP(fc=float(cfg.get("fc", 1300.0)), fs=fs)
    else:
        plant = SecondOrderLC(f0=float(cfg.get("f0", 1300.0)),
                              Q=float(cfg.get("q", 2.0)), fs=fs)

    # 占空比实际范围 0~1（Buck），初值给工作点一半
    comp = PICompensator(kp=float(comp_cfg.get("kp", 0.5)),
                         ki=float(comp_cfg.get("ki", 1200.0)),
                         fs=fs,
                         u0=float(cfg.get("ref_dc", 0.5)),
                         u_min=0.0, u_max=1.0)

    # 开环 Plant 模式不需要补偿器，给个直通（纯函数，不引入额外相位）
    if cfg["mode"] == MODE_PLANT:
        return plant, (lambda e: e)
    return plant, comp


# ---------------------------------------------------------------- 控制器
class SweepController(QtCore.QObject):
    """界面的扫描入口。发 sigStart / sigStop，回 sigPoint / sigProgress / sigDone。

    与 `comm.controller.SweepControllerBase` **信号集完全一致**，界面可以
    在"仿真后端"与"真链路后端"之间自由切换而不改任何信号连接。

    两者语义差别只有一处，写在这里免得以后忘：
        仿真：Worker 线程自己推进到最后一个点，点到了就 emit；
        真链路：靠上位机轮询 STATUS 拉数据（见 comm/controller.py）。
    """

    sigPoint = QtCore.pyqtSignal(object)
    sigProgress = QtCore.pyqtSignal(int, int, float)
    sigDone = QtCore.pyqtSignal(bool, str)     # 正常结束?
    sigLog = QtCore.pyqtSignal(str)


    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[SimWorker] = None
        self._abort = False
        self.result: Optional[SweepResult] = None
        self.cfg: dict = {}

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def start(self, cfg: dict) -> bool:
        """cfg 见 SimBackend.run 里读取的键。"""
        if self.running:
            return False

        self.cfg = cfg
        self._abort = False
        self.result = SweepResult(
            mode=int(cfg["mode"]),
            amplitude_pu=float(cfg["amp_pu"]),
            isr_freq=float(cfg["isr_freq"]),
            timestamp=QtCore.QDateTime.currentDateTime()
                        .toString("yyyy-MM-dd HH:mm:ss"))

        w = SimWorker(cfg, self)
        w.pointReady.connect(self._on_point)
        w.progress.connect(self.sigProgress.emit)
        w.finishedOk.connect(lambda: self._finish(True, ""))
        w.failed.connect(lambda m: self._finish(False, m))
        w.finished.connect(w.deleteLater)
        self._worker = w
        self.sigLog.emit(f"开始扫频 {len(cfg['freqs'])} 点，"
                         f"{'Loop Gain' if cfg['mode'] == MODE_LOOP else 'Plant'} 模式")
        w.start()
        return True

    def stop(self) -> None:
        self._abort = True
        if self._worker is not None:
            self._worker.abort()

    def _on_point(self, p: RawPoint) -> None:
        if self.result is not None:
            self.result.add(p)
        self.sigPoint.emit(p)

    def _finish(self, ok: bool, msg: str) -> None:
        self._worker = None
        if not ok:
            self.sigLog.emit(f"扫频异常：{msg}")
        self.sigDone.emit(ok, msg)


# ---------------------------------------------------------------- 频率表
def make_freqs(f_start: float, f_stop: float, ppd: int,
               isr_freq: float | None = None) -> np.ndarray:
    """按每 decade 点数生成对数等距频率表（委托 `model.sweep_config`）。

    这里不再自己实现一遍：频率表必须和"预计 N 点 / 约 xx s"用同一个
    函数算，否则界面承诺 68 点、实际扫 47 点（f_s/5 截断），用户会
    以为工具坏了。
    """
    from ..model.sweep_config import make_freqs as _mf
    return _mf(f_start, f_stop, ppd, isr_freq)


def estimate_points(f_start: float, f_stop: float, ppd: int,
                    isr_freq: float | None = None) -> int:
    return len(make_freqs(f_start, f_stop, ppd, isr_freq))
