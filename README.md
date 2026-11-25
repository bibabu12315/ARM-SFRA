# ARM-SFRA

面向 ARM Cortex-M 的软件扫频分析仪（SFRA，Sweep Frequency Response Analysis）。
注入单频正弦扰动，用整周期 DFT 提取复振幅，扫出一条 Bode 曲线。

**当前状态：v1.0 — 阶段性暂停，待功率板到位后继续。**

---

## 一句话概括

在 **PY32F071（Cortex-M0+，72 MHz，无 FPU）** 上跑通了
「PC 下发配置 → MCU 扫频 → 回传原始数据 → PC 画 Bode 图 → 导出 CSV」
的完整链路，被测对象是 MCU 内部的**软件二阶 LC 模型**。

对账结果（PLANT 模式，Sallen-Key ω0=2π·1000、Q=1/3）：

| 指标 | 实测偏差 | 判据 |
|---|---|---|
| 幅值最大偏差 | 0.039 dB | < 1 dB ✅ |
| 相位最大偏差 | 0.701° | < 3° ✅ |
| 超限点数 | 0 / 27 | 0 个 ✅ |

ISR 占用：**avg 1312 cycle / 7200 预算 = 18%**（Q15 定点路径）。

---

## 为什么暂停

**没有真实功率级可测。**

当前被测对象是 MCU 里的软件模型，它只能证明"测量链路本身是对的"，
不能证明"这套 SFRA 能拿来测真电路"。要往下走必须先有功率板。

下一步（有板子之后）：

1. 把 `sfra_pend.c` 里 `sfra_dut_step_q15(inj)` 换成
   "注入叠到占空比/参考 → 读 ADC"
2. 删掉 `sfra_dut.c/h`
3. 确认注入幅值（真实功率级注 10% 会触发 OVP，通常 1~2%）
4. 闭环稳定后切 LOOP 模式，读穿越频率 / PM / GM

详见 [`docs/11_v1.0版本说明.md`](docs/11_v1.0版本说明.md) 第八节。

---

## 仓库结构

```
arm-SFRA/
├── firmware/
│   ├── sfra_src/         核心算法（纯 C99，零依赖）
│   │   ├── sfra_core.c   DFT 累加、正弦表、状态机
│   │   └── sfra_proto.c  串口帧协议、CRC16
│   ├── sfra_inc/         对应的头文件
│   ├── mcu_py32f071/     实际 MCU 工程里的 13 个文件（含 ISR、应用层）
│   └── examples/
│       ├── pc/           假 MCU（PC 上跑的同一份 C 代码，用于回归）
│       └── stm32/        STM32 自测桩
├── src/                  上位机（PyQt5 + pyqtgraph）
│   ├── app/comm/         协议解析、串口/仿真后端
│   ├── app/model/        扫频配置、Bode 数据、裕度分析
│   ├── app/ui/           界面
│   └── tests/            pytest 用例
├── docs/                 11 篇设计文档（见下方索引）
├── design/               设计稿
├── tools/                验证工具（交叉验证、假 MCU 构建、压力测试）
├── 运行上位机.bat
└── 运行测试.bat
```

---

## 快速开始

### 跑上位机（仿真后端，不需要硬件）

```bat
运行上位机.bat
```

首次运行会自动建 venv 并装依赖。默认用 `SFRA_BACKEND=sim`
（假 MCU，PC 上跑真实 C 代码）。

接真板：设 `SFRA_BACKEND=hw`，在界面里选串口。

### 跑回归

```bat
运行测试.bat
```

或手动：

```bash
python -m pytest -m "e2e or crosscheck" -q
# 73 passed
```

`e2e` 启动假 MCU 子进程跑完整扫频，`crosscheck` 验证 C 与 Python 的
数值等价性。两者都需要本机有 x86 gcc（第一次会自动下载工具链）。

### 编译固件

用 Keil MDK 打开 `firmware/mcu_py32f071/` 下对应工程，或命令行：

```bash
UV4.exe -r PVCharger.uvprojx -j0 -o build.log
# Program Size: Code=30732  RO-data=1276  RW-data=200  ZI-data=7256
```

---

## 关键设计

### 1. 幅相计算放在 PC，不在 MCU

MCU 只累加 4 个量（`a_c/a_s/b_c/b_s`），幅值和相位由上位机算。
好处：MCU 不需要 `log10` / `atan`，M0+ 也跑得动；算法改了不用重烧板子。

### 2. 整周期取整

每个频点的采集窗内必须恰好是整数个正弦周期，否则频谱泄漏。
`data_count = ceil(f_s · cycles / f)`，相位步进按实际 `data_count` 重算。

### 3. 相位必须 `Z = S_c − j·S_s`

虚部取负号。写反了全项目相位反号，PM 会算得"很高"但其实很危险。

### 4. ISR 三行，顺序不可换

```c
inj = sfra_inject(&s);              // ① 产本拍正弦
y   = dut_step(inj);                // ② 推进对象
sfra_monitor(&s, inj, y);           // ③ 用本拍 θ 累加
sfra_advance(&s);                   // ④ 推进相位（必须在 ③ 之后）
```

把 ④ 提到 ③ 前面，整条曲线会多出 `360/data_count` 度的相位偏移。

### 5. M0+ 必须用 Q15 定点

float 路径实测占周期预算 96%（峰值 113%），切定点后降到 18%。
`SFRA_LOCAL_USE_FIXED_POINT` 必须为 1。

---

## 已知限制

| 限制 | 原因 | 能不能解 |
|---|---|---|
| 扫频上限 2 kHz | `f_max = f_s / 5`，`f_s = 10 kHz` | 提到 10 kHz 需 `f_s=50 kHz`，ISR 预算只剩 1440 cyc，**极紧** |
| 只有 PLANT 模式可用 | S6a 阶段没有真实闭环 | 接功率级后才行 |
| `sfra_core.c` 两份不同步 | MCU 侧 16 位截断，仓库侧 22 位四舍五入 | 实测差异 ≤0.0005°，**可忽略**但建议统一 |
| 文本 shell 未删 | 开发期脚手架 | 开关 `SFRA_LOCAL_USE_TEXT_SHELL` 置 0 即可 |

---

## 文档索引

| 文档 | 一句话 |
|---|---|
| [00 总纲与决策记录](docs/00_总纲与决策记录.md) | 现状 → 缺口 → 下一步 → 决策账本 |
| [01 测量原理](docs/01_测量原理.md) | 为什么这样算是对的 |
| [02 系统架构](docs/02_系统架构.md) | 固件库 / 上位机 / SOP 的边界与接口 |
| [03 通信协议](docs/03_通信协议.md) | 帧格式、CRC、状态机 |
| [04 验证路线](docs/04_验证路线.md) | L0-L3 验证体系与判据 |
| [05 环路接入指南](docs/05_环路接入指南.md) | 测某个环，代码改哪两行 |
| [06 S6a 上板接入指南](docs/06_S6a上板接入指南.md) | 从空板到第一条 Bode 曲线 |
| [07 v1.0 验证清单](docs/07_v1.0验证清单.md) | 现场勾选式清单 |
| [08 验收报告](docs/08_验收报告.md) | 自动生成的产物快照 |
| [09 上位机操作 SOP](docs/09_上位机操作SOP.md) | 逐控件说明 |
| [10 验收路线](docs/10_验收路线.md) | 拿什么当基准、每关怎么判生死 |
| [11 v1.0 版本说明](docs/11_v1.0版本说明.md) | **暂停点快照：做了什么、没做什么** |

---

## 参考

调研过 [libsfra](https://github.com/Frank-zheng-C/libsfra)（560 行，
算法内核 + 无通信层，数据回传在 readme 里留成 `T.B.D.`），
结论是**不替换**：它的算法没有优势，且换核心会让已验证的对账作废。
详细对比见 [`docs/11_v1.0版本说明.md`](docs/11_v1.0版本说明.md) 第七节。
