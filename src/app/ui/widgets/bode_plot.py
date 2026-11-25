"""SFRA Studio - Bode 图控件（pyqtgraph）。

两块图上下叠放、X 轴联动（复刻 TI SFRA GUI 与设计稿布局）：
    上：幅值 dB
    下：相位 deg（自动 unwrap）

对外接口只吃 numpy 数组，不认识 RawPoint / SweepResult，
这样 Tab1（实测）和 Tab2（预测）都能复用同一个控件。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ...model.analysis import analyze, unwrap_phase
from ... import theme


# ---------------------------------------------------------------- 渲染配置
# 【2026-11-24 抗锯齿 / HiDPI】
# 背景：4K 屏 + DPR=2 时，用户滚轮缩放后看到"蓝色横竖条纹"，但离屏
# grab() 复现不出来。逐像素分析离屏截图（幅值区 1396x712）结果是
# 干净的：没有满宽行、没有满高列，填充只占 10.8%，蓝线 2774 px
# 分布合理。也就是说绘制逻辑没问题，问题在**屏幕合成**这一步。
#
# 根因是 pyqtgraph 默认 antialias=False：
#   * 关闭抗锯齿后 QPainter 把线对齐到整数设备像素；
#   * DPR=2 时 2px 宽的线落在半像素边界上，缩放后同一段线在不同
#     位置被栅格化成 1px / 3px 交替 —— 视觉上就是一条断续的"条纹"；
#   * 面积填充的边界同样会被锯齿化，边缘出现明暗相间的短横线。
# 打开抗锯齿后线宽变为连续覆盖，条纹消失。
#
# 代价：抗锯齿让每次重绘多做一次混合，但 49 点、2 条曲线的量级完全
# 无感（实测缩放手势仍跟手）。
#
# 注意 setConfigOption 必须在创建任何 PlotWidget 之前调用，
# 所以放在模块导入期执行，而不是 BodePlot.__init__ 里。
pg.setConfigOptions(antialias=True, useOpenGL=False)


# ---------------------------------------------------------------- 数据容器
@dataclass
class Curve:
    """一条要画的曲线。"""
    freq: np.ndarray
    mag: np.ndarray
    phase: np.ndarray          # 原始相位（-180~180），画图前自动 unwrap
    name: str = ""
    color: str = theme.BLUE
    width: float = 2.0
    dashed: bool = False
    fill: bool = True          # 是否画面积渐变
    visible: bool = True


def _mk_pen(color: str, width: float, dashed: bool = False) -> QtGui.QPen:
    """建一支线笔。

    【2026-11-24 线宽按 DPR 补偿】
    实测：4K 屏 DPR=2，Curve.width 默认 2.0，但截图里量到的实际线宽
    只有 1~2 个物理像素（mean=1.15, std=0.36, min=1, max=2）。
    也就是说这条线一直卡在栅格化的临界点上，同一段曲线在不同位置
    被画成 1px 或 2px —— 滚轮缩放后屏幕上就是明暗交替的斜纹/横竖条纹。
    离屏 grab() 之所以看着"干净"，是因为它不经过屏幕合成，临界抖动
    被掩盖了。

    QPen 的宽度单位是**逻辑像素**，但 pyqtgraph 的 QGraphicsView 视口
    在部分平台上并不会替我们按 DPR 放大笔宽。这里显式乘上设备像素比，
    保证 2.0 在任何 DPR 下都真的画出 2 个逻辑像素宽（4K 上 = 4 物理
    像素），线宽稳定，条纹消失。
    """
    dpr = 1.0
    try:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            scr = app.primaryScreen()
            if scr is not None:
                dpr = max(1.0, float(scr.devicePixelRatio()))
    except Exception:                          # noqa: BLE001
        dpr = 1.0

    pen = pg.mkPen(color=color, width=width * dpr)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    if dashed:
        pen.setStyle(QtCore.Qt.DashLine)
        pen.setDashPattern([4.0, 3.0])
    return pen


def _dash_pen(color: str, on: float, off: float) -> QtGui.QPen:
    """自定义虚线节拍（参考线用，pyqtgraph 的 DashLine 节拍太密）。"""
    pen = pg.mkPen(color=color, width=1)
    pen.setStyle(QtCore.Qt.CustomDashLine)
    pen.setDashPattern([on, off])
    return pen


# ---------------------------------------------------------------- 对数频率轴
class LogFreqAxis(pg.AxisItem):
    """X 轴：内部坐标是 log10(f)，刻度显示成 100、1k、10k。

    不用 pyqtgraph 的 setLogMode，因为它会对已经取过 log 的数据再取一次，
    曲线会脱离视野。自己格式化刻度更可控，也能做 1-2-5 细刻度。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setStyle(tickFont=QtGui.QFont("Consolas", 8), tickLength=-4)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            f = 10 ** v
            if f >= 1000:
                s = f"{f/1000:g}k"
            elif f >= 1:
                s = f"{f:g}"
            else:
                s = f"{f:.3g}"
            out.append(s)
        return out

    def tickValues(self, minVal, maxVal, size):
        """1-2-5 十进制细刻度：主刻度带文字，次刻度只画短线。"""
        if maxVal <= minVal:
            return []
        lo = int(np.floor(minVal))
        hi = int(np.ceil(maxVal))
        if hi - lo > 8:                     # 跨度太大就别画细刻度了
            return [(1.0, list(np.linspace(minVal, maxVal, 6)))]
        major, minor = [], []
        for d in range(lo, hi + 1):
            for m in (1, 2, 5):
                v = d + np.log10(m)
                if minVal - 1e-9 <= v <= maxVal + 1e-9:
                    (major if m == 1 else minor).append(v)
        return [(1.0, major), (0.35, minor)]


# ---------------------------------------------------------------- 主控件
class _CursorOverlay(QtWidgets.QWidget):
    """盖在 GraphicsLayoutWidget 上的一块透明画布，只负责画游标竖线。

    为什么不用 QWidget.paintEvent 直接在 BodePlot 上画：
    BodePlot 自己的内容全在子控件 glw 里，Qt 不会给父控件发 paintEvent，
    画了也看不到。所以必须用一个**盖在 glw 之上**的独立 widget。

    为什么不用 pg.InfiniteLine：见 BodePlot.__init__ 里的长注释。
    """

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        # 不吃鼠标：glw 要照常收到 hover
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, False)
        self._x: Optional[float] = None
        self._spans: list[tuple[float, float]] = []

    def clear(self) -> None:
        if self._x is not None:
            self._x = None
            self._spans = []
            self.update()

    def set_cursor(self, x: float, plots, glw) -> None:
        """x 是 glw 坐标系下的横坐标；plots 是两个 PlotItem。

        坐标系：覆盖层与 glw 都由 QVBoxLayout 铺满父控件且边距为 0，
        所以 glw 坐标 == 覆盖层坐标，不需要 mapTo（对兄弟控件调用
        QWidget.mapTo 会段错误，踩过）。
        """
        owner = self.parentWidget()
        if owner is None:
            return
        self._x = float(x)
        spans = []
        for p in plots:
            r = p.vb.sceneBoundingRect()
            tl = glw.mapFromScene(r.topLeft())
            br = glw.mapFromScene(r.bottomRight())
            y0, y1 = float(tl.y()), float(br.y())
            if y1 > y0:
                spans.append((y0, y1))
        self._spans = spans

    def paintEvent(self, e) -> None:      # noqa: N802
        if self._x is None or not self._spans:
            return
        p = QtGui.QPainter(self)
        try:
            c = QtGui.QColor(theme.BLUE)
            c.setAlpha(90)
            pen = QtGui.QPen(c, 1)
            pen.setStyle(QtCore.Qt.DashLine)
            p.setPen(pen)
            for y0, y1 in self._spans:
                p.drawLine(QtCore.QPointF(self._x, y0),
                           QtCore.QPointF(self._x, y1))
        finally:
            p.end()


class BodePlot(QtWidgets.QWidget):
    """上下两块的 Bode 图。"""

    #: 游标移动时发出 (freq, mag_db, phase_deg)
    cursorMoved = QtCore.pyqtSignal(float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._curves: list[Curve] = []
        self._items: list[dict] = []
        self._stability = None
        self._show_markers = True
        self._hover: Optional[int] = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(theme.PLOT["bg"])
        self.glw.ci.setContentsMargins(0, 0, 0, 0)
        self.glw.ci.setSpacing(6)

        # 高 DPI：让 QGraphicsView 按设备像素比渲染。
        # 不设的话（默认 QGraphicsView 走 raster 视口），DPR=2 的屏幕上
        # 场景内容先按 1x 画好再整体放大 2 倍，线宽和填充边界都会糊；
        # 配合下面的 antialias=True，缩放时才会得到干净的细线。
        self.glw.setRenderHints(
            QtGui.QPainter.Antialiasing
            | QtGui.QPainter.TextAntialiasing
            | QtGui.QPainter.SmoothPixmapTransform)

        lay.addWidget(self.glw)

        # ---- 幅值图 ----
        self.pm = self.glw.addPlot(row=0, col=0)
        # ---- 相位图 ----
        self.pp = self.glw.addPlot(row=1, col=0,
                                   axisItems={"bottom": LogFreqAxis("bottom")})
        self.pp.setXLink(self.pm)

        for p in (self.pm, self.pp):
            p.showGrid(x=True, y=True, alpha=0.0)   # 网格自己画，要控制深浅
            # **不用 setLogMode**：它会把数据再取一次 log，而我们已经
            # 把频率手工转成 log10 了，开了它曲线会飞到图外（踩过这个坑）。
            # 代价是刻度文字要自己格式化，见上面的 LogFreqAxis。
            p.getAxis("left").setWidth(46)
            p.getAxis("left").setPen(pg.mkPen(theme.PLOT["grid_major"]))
            p.getAxis("bottom").setPen(pg.mkPen(theme.PLOT["grid_major"]))
            p.getAxis("left").setTextPen(pg.mkPen(theme.T3))
            p.getAxis("bottom").setTextPen(pg.mkPen(theme.T3))
            ax_font = QtGui.QFont("Consolas", 8)
            p.getAxis("left").setStyle(tickFont=ax_font, tickLength=-4)
            p.getAxis("bottom").setStyle(tickFont=ax_font, tickLength=-4)
            p.setMenuEnabled(False)
            p.setMouseEnabled(x=True, y=True)
            p.hideButtons()
            p.getViewBox().setMouseMode(pg.ViewBox.PanMode)

        # 幅值图的 X 轴刻度隐藏（与相位图共用）
        self.pm.getAxis("bottom").setStyle(showValues=False)
        self.pm.getAxis("bottom").setHeight(4)

        # 只让幅值图管 Y 轴自动缩放，相位图独立
        self.pm.setMouseEnabled(x=True, y=True)
        self.pp.setMouseEnabled(x=True, y=True)

        # 参考线
        self.zero_line = pg.InfiniteLine(angle=0, movable=False,
                                         pen=_dash_pen(theme.PLOT["ref_line"], 4, 4))
        self.pm.addItem(self.zero_line)

        self.p180_line = pg.InfiniteLine(angle=0, movable=False, pos=-180,
                                         pen=_dash_pen(theme.PLOT["warn_line"], 5, 4))
        self.pp.addItem(self.p180_line)

        # 游标（竖线 + 两块图各一个点）
        # 【2026-11-24 竖线改成自绘，不再用 InfiniteLine】
        # 原写法是两条 pg.InfiniteLine(angle=90) 加进两个 ViewBox。
        # 实测（真实屏幕 GDI BitBlt，非离屏 grab）：
        #   * 只要这两条线存在，每调一次 set_curves()，画布上就残留一片
        #     alpha-90 蓝矩形；刷 4 次从 10 万涨到 170 万像素；
        #   * 隐藏它们，同样的操作立刻干净（稳定 1,4xx）；
        #   * 几何完全自洽（boundingRect=(-40,-0.002,60,0.004)、
        #     viewRect=(-40,-3.27,60,1.30)、_endPoints=(-40,20)、
        #     pixelVectors=(0.1429,0.0014) —— 数值都对得上）；
        #   * 裸 PlotWidget 里同样的 InfiniteLine 是干净的；
        #   * 换笔（alpha=0 / alpha=1 / 不透明实线）、改 span、
        #     关 markers、换 viewportUpdateMode 全部无效。
        # 即：InfiniteLine 的 boundingRect 只有 3px 高（极薄 item），
        # QGraphicsView 在 MinimalViewportUpdate 下把这个薄条的脏区
        # 算错，把旧帧内容错位回填 —— 这是 PyQt5+Qt5 的渲染缺陷，
        # 改项目逻辑治不了。所以绕开 QGraphicsScene：
        # 竖线改由盖在 glw 上的 _CursorOverlay 自绘（见 _draw_cursor）。
        self._cursor_x: Optional[float] = None      # 游标 x（glw 坐标）
        self._cursor_visible = False

        self.dot_m = pg.ScatterPlotItem(size=7, pen=pg.mkPen(theme.BLUE, width=2),
                                        brush=pg.mkBrush("#ffffff"))
        self.dot_p = pg.ScatterPlotItem(size=7, pen=pg.mkPen(theme.BLUE, width=2),
                                        brush=pg.mkBrush("#ffffff"))
        self.pm.addItem(self.dot_m)
        self.pp.addItem(self.dot_p)

        # 穿越点标记
        self.fc_marker = pg.ScatterPlotItem(size=9, pen=pg.mkPen(theme.BLUE, width=2),
                                            brush=pg.mkBrush("#ffffff"), symbol="o")
        self.pm.addItem(self.fc_marker)
        self.p180_marker = pg.ScatterPlotItem(size=9, pen=pg.mkPen(theme.AMBER, width=2),
                                              brush=pg.mkBrush("#ffffff"), symbol="s")
        self.pp.addItem(self.p180_marker)

        # 悬停提示
        self.tip = QtWidgets.QLabel(self)
        self.tip.setStyleSheet(
            f"background: rgba(29,29,31,.90); color:#fff; border-radius:7px;"
            f"padding:4px 8px; font-size:10px; font-family:{theme.FONT_MONO};")
        self.tip.hide()
        self.tip.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)

        self.setMouseTracking(True)
        self.glw.scene().sigMouseMoved.connect(self._on_move)

        # 游标覆盖层：必须在 glw 之后创建，才能盖在它上面
        self._overlay = _CursorOverlay(self)
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()

        self.setMinimumHeight(320)
        self.clear()

    def _drop_items(self) -> None:
        """把上一批曲线从两个 ViewBox 里摘干净。

        每项的 key 都是 ('mag'|'phase', 'line'|'fill'|'fill_base') 二元组，
        显式带上所属的 plot，避免靠 if-else 猜归属。
        """
        for it in self._items:
            for (which, kind), item in it.items():
                plot = self.pm if which == "mag" else self.pp
                try:
                    plot.removeItem(item)
                except Exception:
                    pass
        self._items.clear()

    def clear(self) -> None:
        self._drop_items()
        self._curves.clear()
        self._stability = None
        self.fc_marker.setData([], [])
        self.p180_marker.setData([], [])
        self._reset_ranges()

    def set_curves(self, curves: list[Curve]) -> None:
        """整体替换曲线集合（最省事、不会残留旧线）。

        **内部 X 坐标一律是 log10(f)**：对外接口仍收真实 Hz，
        在这里统一转换。忘了转会画出脱离视野的线（实测踩过）。

        【2026-11-24 换掉 FillBetweenItem】
        面积填充原来用 pg.FillBetweenItem(line, base_curve)。那个类的
        updatePath() 是把两条曲线各自 toSubpathPolygons() 后
        `zip(ps1, ps2)` 逐对拼成多边形（OddEvenFill）。这个实现有两个
        先天弱点：
          ① 曲线一旦出现子路径断裂（connect="finite" 遇 NaN、x 有空洞），
             ps1 与 ps2 的配对就会错位，拼出跨越全图的多边形；
          ② 多边形顶点 = 线上点 + 基线点交错连接，视窗缩放后
             填充区被栅格化成贯穿全图的条纹（实测：真机 Loop Gain
             扫频后滚轮缩放，幅值/相位两图同时出现竖/横条纹）。
        改用 PlotDataItem 自带的 fillLevel=0 单路径填充：一条路径、
        WindingFill、不依赖第二条曲线的子路径配对，上面两类病态
        都不存在。旧的 base_curve/FillBetweenItem 全部删掉。
        """
        self._drop_items()
        self._curves = list(curves)

        for c in self._curves:
            if c.freq.size < 2:
                continue
            lx = np.log10(np.maximum(c.freq, 1e-6))
            ph = unwrap_phase(c.phase)
            store: dict = {}

            for which, plot, y in (("mag", self.pm, np.asarray(c.mag, dtype=float)),
                                   ("phase", self.pp, ph)):
                # NaN 不替换成 0：换 0 会在曲线上画出假的"俯冲到 0 dB"，
                # connect="finite" 会把 NaN 处断开成空隙（正是它的用途）。
                y = np.where(np.isfinite(y), y, np.nan)
                pen = _mk_pen(c.color, c.width, c.dashed)
                line = pg.PlotDataItem(lx, y, pen=pen, connect="finite")

                if which == "mag" and c.fill and not c.dashed:
                    line.setFillLevel(0.0)
                    line.setFillBrush(pg.mkBrush(*theme.PLOT["main_fill"]))

                plot.addItem(line)
                store[(which, "line")] = line

            self._items.append(store)

        self.autoscale()

    def autoscale(self) -> None:
        if not self._curves:
            self._reset_ranges()
            return
        fmin = min(float(np.nanmin(c.freq)) for c in self._curves if c.freq.size)
        fmax = max(float(np.nanmax(c.freq)) for c in self._curves if c.freq.size)
        if fmin <= 0 or fmax <= fmin:
            self._reset_ranges()
            return

        self.pm.setXRange(np.log10(fmin), np.log10(fmax), padding=0.02)

        # 幅值范围：覆盖全部曲线 + 0dB，留 8dB 余量，按 20dB 取整
        mags = np.concatenate([c.mag[np.isfinite(c.mag)] for c in self._curves
                               if c.mag.size])
        if mags.size:
            lo = min(float(mags.min()), -3.0)
            hi = max(float(mags.max()), 3.0)
            lo = np.floor((lo - 8) / 20) * 20
            hi = np.ceil((hi + 8) / 20) * 20
            self.pm.setYRange(lo, hi, padding=0)

        # 相位范围：按 90° 对齐，但限制在合理跨度内。
        # 实测数据在高频信噪比差时会剧烈跳变，unwrap 出一条 -1000° 的曲线
        # 会把整个相位图压扁，所以用中位数 + 稳健跨度来定范围。
        phs = np.concatenate([unwrap_phase(c.phase) for c in self._curves])
        phs = phs[np.isfinite(phs)]
        if phs.size:
            lo = np.floor((np.percentile(phs, 1) - 15) / 90) * 90
            hi = np.ceil((np.percentile(phs, 99) + 15) / 90) * 90
            lo = min(lo, -180.0)
            hi = max(hi, 0.0)
            # 最多显示 810°（9 个 90° 格），再多就失去可读性
            if hi - lo > 810:
                mid = (hi + lo) / 2
                lo, hi = mid - 405, mid + 405
            self.pp.setYRange(lo, hi, padding=0)

    def _reset_ranges(self) -> None:
        self.pm.setXRange(2.0, 4.3, padding=0)      # 100Hz ~ 20kHz
        self.pm.setYRange(-60, 60, padding=0)
        self.pp.setYRange(-360, 0, padding=0)

    # ------------------------------------------------------------ 稳定性标记
    def set_stability(self, res) -> None:
        self._stability = res
        if res is None:
            self.fc_marker.setData([], [])
            self.p180_marker.setData([], [])
            return
        if res.fc is not None:
            self.fc_marker.setData([np.log10(res.fc)], [0.0])
        else:
            self.fc_marker.setData([], [])
        if res.f180 is not None:
            self.p180_marker.setData([np.log10(res.f180)], [-180.0])
        else:
            self.p180_marker.setData([], [])

    # ------------------------------------------------------------ 游标
    def _draw_cursor(self) -> None:
        """让覆盖层重画一条游标竖线（幅值区、相位区各一段）。

        为什么自己画：见 __init__ 里关于 InfiniteLine 的说明。
        覆盖层是个独立的 QWidget，它的绘制由 Qt 的普通 widget 走
        paintEvent，不经过 QGraphicsScene 的脏区计算，
        绕开"极薄 item 局部重绘错位"那个缺陷。
        """
        ov = self._overlay
        if not self._cursor_visible or self._cursor_x is None:
            ov.clear()
            return
        ov.set_cursor(self._cursor_x, [self.pm, self.pp], self.glw)
        ov.update()

    def paintEvent(self, e) -> None:      # noqa: N802 (Qt 约定)
        # 父控件本身不画东西：内容全在 glw 与覆盖层里。
        # 保留这个方法只是为了不让默认实现填底色。
        pass

    def resizeEvent(self, e) -> None:     # noqa: N802
        super().resizeEvent(e)
        self._overlay.setGeometry(self.rect())

    def _on_move(self, pos) -> None:
        if not self._curves:
            return
        vb = None
        if self.pm.sceneBoundingRect().contains(pos):
            vb = self.pm.vb
        elif self.pp.sceneBoundingRect().contains(pos):
            vb = self.pp.vb
        if vb is None:
            self._hide_cursor()
            return

        mp = vb.mapSceneToView(pos)
        f = 10 ** mp.x()
        idx = self._nearest(f)
        if idx is None:
            self._hide_cursor()
            return

        c = self._curves[idx]
        if c.freq.size == 0:
            self._hide_cursor()
            return
        j = int(np.argmin(np.abs(np.log10(np.maximum(c.freq, 1e-9)) - np.log10(f))))
        if not (0 <= j < c.freq.size):
            self._hide_cursor()
            return
        fx = float(c.freq[j])
        my = float(c.mag[j])
        py = float(unwrap_phase(c.phase)[j])

        # 游标竖线：算出 glw 坐标系里的 x，交给覆盖层画
        sp = self.glw.mapFromScene(vb.mapViewToScene(
            QtCore.QPointF(np.log10(fx), 0)))
        self._cursor_x = float(sp.x())
        self._cursor_visible = True
        self._draw_cursor()

        self.dot_m.setData([np.log10(fx)], [my])
        self.dot_p.setData([np.log10(fx)], [py])

        unit_f = f"{fx/1000:.2f} kHz" if fx >= 1000 else f"{fx:.1f} Hz"
        mode = "mag" if self._is_in_mag(pos) else "phase"
        txt = f"{unit_f}\n{my:+.1f} dB · {py:.1f}°"
        self.tip.setText(txt)
        self.tip.adjustSize()

        # 定位到鼠标附近（在幅值图内就贴幅值点）
        pt = (self.pm.vb.mapViewToScene(QtCore.QPointF(np.log10(fx), my))
              if mode == "mag" else
              self.pp.vb.mapViewToScene(QtCore.QPointF(np.log10(fx), py)))
        sp = self.glw.mapFromScene(pt)
        x = sp.x() - self.tip.width() // 2
        y = sp.y() - self.tip.height() - 12
        x = max(4, min(x, self.width() - self.tip.width() - 4))
        y = max(4, min(y, self.height() - self.tip.height() - 4))
        self.tip.move(x, y)
        self.tip.show()
        self.tip.raise_()

        self.cursorMoved.emit(fx, my, py)

    def _is_in_mag(self, pos) -> bool:
        return self.pm.sceneBoundingRect().contains(pos)

    def _nearest(self, f: float) -> Optional[int]:
        """返回频率最接近且可见的那条曲线下标。"""
        best, dmin = None, 1e18
        for i, c in enumerate(self._curves):
            if not c.visible or c.freq.size == 0:
                continue
            d = np.min(np.abs(np.log10(np.maximum(c.freq, 1e-9)) - np.log10(max(f, 1e-9))))
            if d < dmin:
                dmin, best = float(d), i
        return best

    def _hide_cursor(self) -> None:
        if self._cursor_visible:
            self._cursor_visible = False
            self._cursor_x = None
            self._draw_cursor()
        self.dot_m.setData([], [])
        self.dot_p.setData([], [])
        self.tip.hide()

    def leaveEvent(self, e) -> None:
        self._hide_cursor()
        super().leaveEvent(e)

    # ------------------------------------------------------------ 导出
    def snapshot(self, path: str, width: int = 1240, height: int = 820) -> bool:
        """导出 PNG。临时放大再截图，得到 2x 清晰度。"""
        old_min = self.minimumHeight()
        self.setMinimumHeight(height)
        self.resize(width, height)
        QtWidgets.QApplication.processEvents()
        try:
            pix = self.glw.grab()
            ok = pix.save(path)
            return bool(ok)
        finally:
            self.setMinimumHeight(old_min)


# ---------------------------------------------------------------- 便捷函数
def curve_from_result(res, color=theme.BLUE, name="", dashed=False) -> Curve:
    """SweepResult → Curve。"""
    return Curve(freq=res.freq, mag=res.mag, phase=res.phase,
                 name=name or res.mode_name, color=color, dashed=dashed,
                 fill=not dashed)
