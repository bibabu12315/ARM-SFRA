/**
  ******************************************************************************
  * @file    sfra_dut.h
  * @brief   S6a 被测对象：软件二阶 Sallen-Key 低通
  ******************************************************************************
  * 为什么用软件对象而不是真功率级（docs/06 §6.0）：
  *   S6a 验的是**算法与链路**，不是功率级。拿一个有闭式解的模拟电路模型
  *   当被测对象，任何一处算错都会立刻在曲线上显形，而不会被接线问题、
  *   ADC 噪声、功率级非线性混在一起。
  *
  * 为什么是二阶而不是文档示例的一阶：
  *   一阶低通只有 −20 dB/dec 和 0→−90° 两件事，鉴错能力弱。二阶 Sallen-Key
  *   多出三样一阶没有的东西：
  *     1. **谐振峰**——Q > 0.707 时幅频曲线上凸，检验幅值算得对不对；
  *     2. **180° 相位跨度**——0° → −180°，相位方向反了会立刻变成 +180°，
  *        比一阶的 90° 好分辨；而且累加器正负号、θ 递推错误都会放大；
  *     3. **两个极点**——高频段 −40 dB/dec，斜率算错一倍立刻暴露。
  *
  * 电路与传递函数（单位增益 Sallen-Key，R1 = R2 = R，C1 = C2 = C）：
  *
  *           R               R
  *     o----/\/\/----+----/\/\/----+----o
  *                   |             |
  *                  === C1         |   +---\
  *                   |             +---|-  \____o
  *                  GND            |   |+  /
  *                                 +---/   /
  *                                 |
  *                                === C2
  *                                 |
  *                                GND
  *
  *     H(s) = 1 / (1 + 3·s·RC + (s·RC)²)
  *
  *     ω0 = 1/RC，Q = 1/3，DC 增益 = 1
  *
  * 注意 Q = 1/3 < 0.707，是**过阻尼**，没有谐振峰。这是单位增益 Sallen-Key
  * 的标准结果（那个"3"来自 C1 的反馈通路）。想要峰值就把 R1/R2 取不等值，
  * 但那样闭式解的系数要重推，容易算错——本阶段宁可少一个特征点，也不引入
  * 一个可能推错的公式。谐振峰由 SFRA_DUT_PLANT_LC 和 SFRA_DUT_PLANT_LP
  * 两种对象补上（见 sfra_dut_kind_t）。
  *
  * 离散化用 **Tustin（双线性）**，与 docs/06 §6.4 判据 A 的基准一致：
  * 固件自己也是离散的，拿连续域当基准会在 f_s/5 处差 −1.26 dB，那是数学
  * 不是 bug。PC 端 tools/selftest_s6a.py 两套基准都会算，判据分开。
  ******************************************************************************
  */
#ifndef __SFRA_DUT_H
#define __SFRA_DUT_H

#include "bsp.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 可选被测对象。切换靠 sfra_dut_select()，S6a 默认用 SALLEN_KEY。
 *
 * 三个对象都给了 Tustin 离散实现，PC 端换着扫可以交叉验证不同形状的曲线：
 *   SALLEN_KEY  二阶过阻尼低通，0 → −180°，硬特征是多一个极点
 *   PLANT_LC    二阶欠阻尼 LC，Q=0.8，有 +2 dB 左右的谐振峰
 *   PLANT_LP    一阶低通，与 docs/06 示例完全对齐，用来对文档的期望表 */
typedef enum
{
    SFRA_DUT_SALLEN_KEY = 0,    /* 默认：二阶 Sallen-Key，Q = 1/3 */
    SFRA_DUT_PLANT_LC,          /* 二阶 LC，Q = 0.8，有峰值 */
    SFRA_DUT_PLANT_LP,          /* 一阶低通，对照文档示例 */
    SFRA_DUT_KIND_MAX
} sfra_dut_kind_t;

/* 初始化被测对象。fc_hz 是特征频率（Sallen-Key 的 ω0/2π、LC 的 f0、
 * 一阶的 −3 dB 点），fs_hz 是采样率（必须等于 SFRA_ISR_FREQ_HZ）。 */
void sfra_dut_init(sfra_dut_kind_t kind, float fc_hz, float fs_hz);

/* 切成另一种对象，随后清状态。扫频进行中调用会毁掉数据一致性，
 * 调用方需先 sfra_app_stop()。 */
void sfra_dut_select(sfra_dut_kind_t kind);

/* 清内部状态（延时单元归零）。扫频开始前调用，避免上一轮残留污染第一点。 */
void sfra_dut_reset(void);

/* 喂一拍，返回下一拍输出。
 *
 * **这个函数在控制 ISR 里每拍调用一次**，必须短：
 * 一次 2 阶直接II型转置 = 2 次 mul + 2 次 mac + 状态搬移，M0+ 约 40 周期。
 * 不要在这里加 printf 或任何分支复杂的逻辑。 */
float sfra_dut_step(float x);

/* 喂一拍（Q15 定点版），返回下一拍输出。
 *
 * 与 sfra_dut_step() 数值等价，但全程整数，不调用任何 __aeabi_f* 例程。
 * M0/M0+ 这类无 FPU 的片子**必须用这个**——实测浮点版让整个 SFRA ISR
 * 占到周期预算的 96%（峰值 113%），换成定点后降到 30% 以下。
 *
 * 输入输出都是 Q15（[-1,1) ↔ [-32768,32767]），与 sfra_signal_t 在
 * SFRA_USE_FIXED_POINT=1 时的类型一致。 */
int16_t sfra_dut_step_q15(int16_t x_q15);

/* 被测对象的直流增益。Sallen-Key 与一阶低通恒为 1，LC 也是 1（分子分母
 * 同阶）。真正用到的是 PLANT 模式下的"去直流"——见 sfra_app.c。 */
float sfra_dut_dc_gain(void);

/* 当前对象名字，供 CONDITIONS / shell 显示与 PC 端对数。 */
const char *sfra_dut_name(void);

/* 特征频率，供 shell 显示。 */
float sfra_dut_fc_hz(void);

#ifdef __cplusplus
}
#endif

#endif /* __SFRA_DUT_H */
