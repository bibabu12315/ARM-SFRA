"""SFRA Studio - 视觉规范（唯一定义处）。

所有颜色、字号、圆角都从这里取，QSS 与 pyqtgraph 都读这里的常量。
设计基准：design/sfra-ui-mockup.html（iOS / macOS 风格）。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 颜色
BG = "#f5f5f7"          # 页面底色
CARD = "#ffffff"        # 卡片
PANEL = "#fcfcfd"       # 侧栏
TOOLBAR = "#fbfbfd"     # 顶栏
GROUP = "#f2f2f5"       # 分组填充（分段控件底）
INSET = "#fafafb"       # 内嵌说明块

T1 = "#1d1d1f"          # 主文字
T2 = "#6e6e73"          # 次要文字
T3 = "#a1a1a6"          # 提示文字
LINE = "#e6e6eb"        # 边框
LINE2 = "#f1f1f4"       # 分隔线

BLUE = "#0071e3"        # 苹果蓝（主曲线 / 主按钮）
BLUE_BG = "#e9f2fd"     # 蓝底
GREEN = "#34c759"       # 正常
GREEN_T = "#248a3d"     # 正常文字
AMBER = "#ff9500"       # 警告
AMBER_T = "#b36b00"     # 警告文字
RED = "#ff3b30"
PURPLE = "#7d5fe8"      # 预测曲线
GREY_CURVE = "#c8c8cf"  # 参考线

# ---------------------------------------------------------------- 尺寸
RADIUS_CARD = 12
RADIUS_WIN = 22
SIDEBAR_L = 300
SIDEBAR_R = 268
ROW_H = 36

# ---------------------------------------------------------------- 字体
FONT_UI = ('-apple-system, "SF Pro Text", "Segoe UI", "PingFang SC", '
           '"Microsoft YaHei", sans-serif')
FONT_MONO = '"SF Mono", "Cascadia Mono", Consolas, monospace'

#: 等宽数字（读数不能跳动）
FONT_NUM = ('"SF Mono", "Cascadia Mono", Consolas, monospace')


# ---------------------------------------------------------------- 绘图配色
PLOT = {
    "bg": "#ffffff",
    "grid_minor": "#f8f8fa",
    "grid_major": "#ededf1",
    "axis_text": "#a1a1a6",
    "ref_line": "#c8c8cf",
    "warn_line": "#e8b96f",
    "main": BLUE,
    "main_fill": (0, 113, 227, 16),      # RGBA，面积渐变
    "pred": PURPLE,
    "comp": AMBER,
    "marker": "#ffffff",
}


# ---------------------------------------------------------------- QSS
def qss() -> str:
    """全局样式表。控件靠 objectName / 动态属性选择，避免到处写内联样式。"""
    return f"""
/* ============ 基础 ============ */
* {{
    font-family: {FONT_UI};
    font-size: 12.5px;
    color: {T1};
    outline: none;
}}
QWidget#Root {{
    background: {BG};
}}
QMainWindow {{
    background: {BG};
}}

/* ============ 顶栏 ============ */
QWidget#TitleBar {{
    background: {TOOLBAR};
    border-bottom: 1px solid {LINE2};
}}
QLabel#AppName {{
    font-size: 13px;
    font-weight: 600;
    color: {T1};
}}
QLabel#Pill {{
    background: {GROUP};
    color: {T2};
    border-radius: 11px;
    padding: 3px 10px;
    font-size: 11px;
    font-family: {FONT_MONO};
}}
QLabel#PillOn {{
    background: rgba(52,199,89,.13);
    color: {GREEN_T};
    border-radius: 11px;
    padding: 3px 10px;
    font-size: 11px;
    font-family: {FONT_MONO};
}}

/* ============ 分页（顶栏中格的分段控件） ============ */
/* 注意：原本还有一个独立的 QWidget#TabStrip（占满一整行、46px 高），
   v4.7 把分页合进顶栏后就删掉了 —— 那一行除中间 220px 外全是空白，
   白扔 46px 高度。现在 Tab 是顶栏 QGridLayout 的中格。 */
QWidget#SegTrack, QWidget#SegTrackPS {{
    background: {GROUP};
    border-radius: 9px;
}}

/* ============ 侧栏 ============ */
QWidget#ColLeft, QWidget#ColRight {{
    background: {PANEL};
}}
QWidget#ColLeft {{
    border-right: 1px solid {LINE2};
}}
QWidget#ColRight {{
    border-left: 1px solid {LINE2};
}}
QWidget#ColMid {{
    background: {BG};
}}

QLabel#H {{
    font-size: 10.5px;
    font-weight: 600;
    color: {T3};
    letter-spacing: 0.6px;
    padding-left: 2px;
}}

/* ============ 分组卡片 ============ */
QFrame#Group {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#GroupRow {{
    border-bottom: 1px solid {LINE2};
    background: transparent;
}}
QFrame#GroupRowLast {{
    background: transparent;
}}
QFrame#Card {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: {RADIUS_CARD}px;
}}
QFrame#CardHeader {{
    background: transparent;
    border-bottom: 1px solid {LINE2};
}}
QLabel#CardTitle {{
    font-size: 12px;
    font-weight: 600;
}}
QLabel#CardTitle small {{
    color: {T3};
}}

/* ============ 键值行 ============ */
QLabel#Key {{
    font-size: 12px;
    color: {T2};
}}
QLabel#Val {{
    font-size: 12px;
    color: {T1};
}}
QLabel#ValSm {{
    font-size: 11.5px;
    color: {T1};
    font-family: {FONT_NUM};
}}
QLabel#Unit {{
    font-size: 11px;
    color: {T3};
}}

/* ============ 输入框 ============ */
QLineEdit {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 12px;
    font-family: {FONT_NUM};
    selection-background-color: {BLUE_BG};
    selection-color: {T1};
}}
QLineEdit:focus {{
    border: 1px solid {BLUE};
    background: {CARD};
}}
QLineEdit:disabled {{
    background: {GROUP};
    color: {T3};
}}

QDoubleSpinBox, QSpinBox {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 6px;
    padding: 4px 6px;
    font-size: 12px;
    font-family: {FONT_NUM};
}}
QDoubleSpinBox:focus, QSpinBox:focus {{
    border: 1px solid {BLUE};
}}
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    width: 0;
    border: 0;
}}

QComboBox {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
    min-height: 18px;
}}
QComboBox:focus {{ border: 1px solid {BLUE}; }}
QComboBox::drop-down {{ width: 16px; border: 0; }}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {BLUE_BG};
    selection-color: {T1};
    outline: none;
}}

/* ============ 按钮 ============ */
QPushButton {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 12px;
    color: {T1};
}}
QPushButton:hover {{ background: {INSET}; }}
QPushButton:pressed {{ background: {GROUP}; }}
QPushButton:disabled {{ color: {T3}; background: {INSET}; border-color: {LINE2}; }}

QPushButton#Primary {{
    background: {BLUE};
    border: 1px solid {BLUE};
    color: #ffffff;
    font-weight: 600;
    padding: 9px 12px;
    border-radius: 9px;
}}
QPushButton#Primary:hover {{ background: #0077ed; }}
QPushButton#Primary:pressed {{ background: #0062c4; }}
QPushButton#Primary:disabled {{ background: #b9d7f4; border-color: #b9d7f4; color: #ffffff; }}

QPushButton#Danger {{
    color: {RED};
}}

QPushButton#Small {{
    padding: 3px 9px;
    font-size: 11px;
    border-radius: 6px;
}}

/* ============ 分段控件内的按钮 ============ */
/* 分段控件的每一项。
   **内边距不能用控件级 setStyleSheet 设**：那会整体覆盖本规则，
   把下面的":checked 边框"规则一起干掉，导致文字被压掉一半
   （这正是「Plant / Precise 选中只显示一半」的根因）。
   内边距改为由 `SegmentedControl` 用 `setProperty("segPadX"/"segPadY")`
   传进来，QSS 用属性选择器读。
   `:checked` 态必须保持**相同的左右内边距**，否则切换时文字会左右跳动。
   高度用 min-height 锁住，避免字体放大（用户改系统 DPI）时被压扁。 */
QPushButton#SegItem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 4px 10px;
    font-size: 12px;
    color: {T2};
    min-height: 14px;
}}
QPushButton#SegItem[segPadX="6"] {{ padding: 4px 6px; }}
QPushButton#SegItem[segPadX="8"] {{ padding: 4px 8px; }}
QPushButton#SegItem[segPadX="10"] {{ padding: 4px 10px; }}
QPushButton#SegItem[segPadX="12"] {{ padding: 4px 12px; }}
QPushButton#SegItem[segPadX="14"] {{ padding: 4px 14px; }}
QPushButton#SegItem[segPadX="16"] {{ padding: 4px 16px; }}
QPushButton#SegItem:hover {{ color: {T1}; }}
QPushButton#SegItem:checked {{
    background: {CARD};
    color: {T1};
    font-weight: 600;
    /* 与未选中态**同宽**的边框：只换颜色不换宽度，文字位置就不会跳 */
    border: 1px solid rgba(0,0,0,0.04);
}}

/* ============ 进度条 ============ */
QProgressBar {{
    background: {LINE};
    border: 0;
    border-radius: 3px;
    height: 5px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {BLUE};
    border-radius: 3px;
}}

/* ============ 提示块 ============ */
QFrame#Note {{
    background: {INSET};
    border-radius: 9px;
}}
QFrame#NoteWarn {{
    background: rgba(255,149,0,.09);
    border-radius: 9px;
}}
QLabel#NoteText {{
    font-size: 11px;
    color: {T2};
    line-height: 145%;
}}
QLabel#NoteTextWarn {{
    font-size: 11px;
    color: #8a5300;
}}
QLabel#NoteDot {{
    color: {T3};
    font-size: 11px;
}}
QLabel#NoteDotWarn {{
    color: {AMBER};
    font-size: 11px;
}}

/* ============ 判据读数 ============ */
QLabel#BigVal {{
    font-size: 17px;
    font-weight: 600;
    font-family: {FONT_NUM};
}}
QLabel#BigUnit {{
    font-size: 10px;
    color: {T3};
    font-weight: 400;
}}
QLabel#ChipOk {{
    background: rgba(52,199,89,.13);
    color: {GREEN_T};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 600;
}}
QLabel#ChipWarn {{
    background: rgba(255,149,0,.14);
    color: {AMBER_T};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 600;
}}
QLabel#ChipMute {{
    background: {GROUP};
    color: {T2};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 600;
}}

/* ============ 图例 ============ */
QLabel#Legend {{
    font-size: 10.5px;
    color: {T2};
}}

/* ============ 串口日志面板（design/sfra-console-mockup.html） ============ */
QFrame#Console {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: {RADIUS_CARD}px;
}}
QWidget#ConsoleHead {{
    background: transparent;
    border-bottom: 1px solid {LINE2};
}}
QWidget#ConsoleHead:hover {{
    background: {PANEL};
}}
QLabel#ConsoleCaret {{
    color: {T3};
    font-size: 9px;
}}
QLabel#ConsoleTitle {{
    font-size: 12px;
    font-weight: 600;
    color: {T1};
}}
/* 状态点：未连接灰、已连接绿（`on` 属性由 set_port 切换） */
QLabel#ConsoleDot {{
    color: {T3};
    font-size: 8px;
}}
QLabel#ConsoleDot[on="true"] {{
    color: {GREEN};
}}
QLabel#ConsolePort {{
    font-size: 10px;
    color: {T3};
    font-family: {FONT_NUM};
}}
QLabel#ConsoleCount {{
    font-size: 10px;
    color: {T3};
    font-family: {FONT_NUM};
}}
QLabel#ConsoleCount[hasErr="true"] {{
    color: {RED};
    font-weight: 600;
}}
/* 图标按钮（暂停 / 清空）。
   **必须用 QSS 写死 width/height**，不能只靠 `setFixedSize()`：
   QToolButton 的 `minimumSizeHint()` 会把 padding 算进去（36px），
   而布局是照 `minimumSizeHint` 分空间的——只设 FixedSize 时，
   布局仍按 36px 预留，标题栏就被撑宽 12px，最右侧图标被裁掉。 */
QToolButton#IconBtn {{
    background: transparent;
    border: 0;
    border-radius: 6px;
    color: {T3};
    font-size: 11px;
    padding: 0;
    margin: 0;
    width: 20px;
    height: 20px;
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
}}
QToolButton#IconBtn:hover {{
    background: {GROUP};
    color: {T2};
}}
QToolButton#IconBtn:checked {{
    background: {BLUE_BG};
    color: {BLUE};
}}
QWidget#ConsoleFilt {{
    background: {PANEL};
    border-bottom: 1px solid {LINE2};
}}
QPushButton#FChip {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 9px;
    padding: 1px 7px;
    font-size: 10px;
    font-family: {FONT_NUM};
    color: {T3};
}}
QPushButton#FChip:hover {{
    color: {T2};
}}
QPushButton#FChip:checked {{
    background: {BLUE_BG};
    border-color: transparent;
    color: {BLUE};
    font-weight: 600;
}}
QPushButton#QBtn {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 6px;
    padding: 1px 8px;
    font-size: 10px;
    font-family: {FONT_NUM};
    color: {T2};
}}
QPushButton#QBtn:hover {{
    background: {BLUE_BG};
    border-color: transparent;
    color: {BLUE};
}}
QPlainTextEdit#ConsoleLog {{
    background: {CARD};
    border: 0;
    font-family: {FONT_NUM};
    font-size: 10.5px;
    color: {T1};
    selection-background-color: {BLUE_BG};
    selection-color: {T1};
}}
QWidget#ConsoleIn {{
    background: {PANEL};
    border-top: 1px solid {LINE2};
}}
QLabel#ConsolePrompt {{
    color: {BLUE};
    font-family: {FONT_NUM};
    font-size: 12px;
    font-weight: 600;
}}
QLineEdit#ConsoleInput {{
    background: transparent;
    border: 0;
    padding: 2px 0;
    font-family: {FONT_NUM};
    font-size: 11px;
    color: {T1};
}}
QLineEdit#ConsoleInput:disabled {{
    color: {T3};
}}
QPushButton#SendBtn {{
    background: {BLUE};
    color: #fff;
    border: 0;
    border-radius: 7px;
    padding: 3px 11px;
    font-size: 11px;
    font-weight: 500;
}}
QPushButton#SendBtn:hover {{
    background: #0077ed;
}}
QPushButton#SendBtn:disabled {{
    background: {GROUP};
    color: {T3};
}}

/* ============ 滚动区 ============ */
QScrollArea {{
    background: transparent;
    border: 0;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(0,0,0,.16);
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(0,0,0,.28);
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ============ 菜单 ============ */
QMenu {{
    background: {CARD};
    border: 1px solid {LINE};
    border-radius: 12px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 22px 7px 10px;
    border-radius: 8px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background: {BLUE_BG};
    color: {BLUE};
}}
QMenu::separator {{
    height: 1px;
    background: {LINE2};
    margin: 4px 8px;
}}

QToolTip {{
    background: rgba(29,29,31,.92);
    color: #ffffff;
    border: 0;
    border-radius: 7px;
    padding: 5px 8px;
    font-size: 10px;
    font-family: {FONT_MONO};
}}
"""
