/**
  ******************************************************************************
  * @file    sfra_app.h
  * @brief   SFRA 应用层：配置、初始化、主循环入口、字节分流器
  ******************************************************************************
  * 分层：
  *
  *     sfra_core.c/h   扫频算法与状态机     ← 平台无关，不改
  *     sfra_proto.c/h  串口协议栈           ← 平台无关，不改
  *     sfra_port.c     四个移植函数 + 回调   ← 接 bsp_uart
  *     sfra_app.c      本文件实现            ← 节拍、被测对象、分流
  *     sfra_dut.c      被测对象（软件模型）
  *     sfra_pend.c     TIM7 控制节拍 + ISR 开销测量
  *
  * 主循环约定（三行，一个都不能少）：
  *
  *     sfra_app_poll();     // 内部分流 + poll + 发应答 + background_task
  *
  * 对应 docs/06 §6.2「主循环」那一节。其中 background_task 必须无条件
  * 调用——写成 if (sfra_is_running()) 会让状态机卡在 NEXT，扫频假死。
  ******************************************************************************
  */
#ifndef __SFRA_APP_H
#define __SFRA_APP_H

#include "bsp.h"
#include "sfra_core.h"
#include "sfra_proto.h"

/* 本地配置覆盖（调试开关集中在这里，改它不会被 Keil 覆盖）。
 *
 * 注意用引号 + 无路径：文件就在本目录，编译器一定找得到。
 * 找不到会报 #5-D 警告并按"没配置"处理，不影响编译通过。 */
#include "sfra_local_cfg.h"

#ifdef __cplusplus
extern "C" {
#endif

/*============================ 编译期配置 ==================================*/

/* 控制 ISR 频率 = 采样率 f_s，单位 Hz。
 *
 * 这个值是**硬件事实**：由 TIM7 的重装值决定，PC 端不能改（SET_CONFIG
 * 里被显式忽略）。所以改了这里必须同步改 sfra_pend.c 的 TIM7 配置，
 * 两处都对得上才能扫出正确曲线。
 *
 * 为什么先取 10 kHz 而不是文档示例的 100 kHz：
 *   1. 本阶段被测对象在软件里，不需要真实 PWM，10 kHz 已经能跑完整条
 *      扫频带（100 Hz ~ f_s/5 = 2 kHz）；
 *   2. M0+ @72 MHz 上每拍 7200 周期，ISR 占比有个宽松余量，先把
 *      "算法对"和"开销可接受"两件事分开验证；
 *   3. 想测高频开销就把它改成 100000，TIM7 会跟着重算 PSC/ARR。
 *      注意 f_s/5 上限会跟着变成 20 kHz，扫频带整体上移。 */
#ifndef SFRA_ISR_FREQ_HZ
#define SFRA_ISR_FREQ_HZ        10000.0f
#endif

/* TIM7 控制节拍开关。与 bsp_timer_measure_* 互斥（都独占 TIM7）。
 *
 * 置 0 时 SFRA 不产生节拍，只能用来验证协议层（PING/CAPS/SET_CONFIG
 * 会通，START 之后状态永远不动）。 */
#ifndef SFRA_USE_TIM7_ISR
#define SFRA_USE_TIM7_ISR       1
#endif

/* ISR 开销测量。打开后每拍额外做一次 GPIO 翻转（示波器看宽度）
 * 与一次 SysTick 差值统计，会让周期数偏大 5~10 周期。
 * 只在 S6a §6.5 取证时打开，平时关掉。
 *
 * 改名成 SFRA_LOCAL_MEASURE_ISR_COST 是刻意的：真正的开关在
 * sfra_local_cfg.h 里，这里只是接住它。这样即使有人在 uvprojx 的
 * <Define> 里写了 SFRA_MEASURE_ISR_COST，也不会和本文件的默认值
 * 打架（Keil 会把那个宏覆盖掉，导致"改了没生效"）。 */
#if defined(SFRA_LOCAL_MEASURE_ISR_COST)
#  undef  SFRA_MEASURE_ISR_COST
#  define SFRA_MEASURE_ISR_COST   SFRA_LOCAL_MEASURE_ISR_COST
#endif

#ifndef SFRA_MEASURE_ISR_COST
#define SFRA_MEASURE_ISR_COST   0
#endif

/* 结果数组点数上限。f_s/5 截断后实际点数一般 40~70。
 *
 * 68 点 × 5 个 float = 1360 B。M0+ 只有 16 KB SRAM，不能按文档示例开 256
 * （那就是 5120 B，占掉三分之一）。协议层允许 PC 请求任意 vec_length，
 * 超过这个值会在 SET_CONFIG 里被挡成 BAD_PARAM——比"越界写穿内存"好。 */
#ifndef SFRA_MAX_POINTS
#define SFRA_MAX_POINTS         80
#endif

/*============================ 接口 ========================================*/

/* 建立正弦表、初始化算法与协议层、初始化被测对象与节拍。
 * 必须在 bsp_init() 之后调用（依赖串口）。 */
bsp_status_t sfra_app_init(void);

/* 主循环调用，非阻塞。内部顺序：
 *   ① 分流器从 bsp_uart 取字节 → 二进制帧给协议层 / 文本给 shell
 *   ② sfra_proto_poll()      解析命令、组应答
 *   ③ 把 tx_buf 倒出去
 *   ④ sfra_background_task() 推进状态机（无条件） */
void sfra_app_poll(void);

/* 分流器入口：把一个字节交给 SFRA 协议层还是文本 shell。
 * 由 sfra_app_poll() 内部调用，也允许 shell 侧在别处复用它。 */
void sfra_app_feed_byte(uint8_t byte);

/* 供 shell 的 sfra 命令查询，返回一行人类可读摘要。 */
void sfra_app_describe(char *buf, uint16_t size);

/* 读 ISR 开销统计（SFRA_MEASURE_ISR_COST=0 时全返回 0）。 */
void sfra_app_isr_cost(uint32_t *avg_cycles, uint32_t *max_cycles,
                       uint32_t *count);
/* 被丢弃的异常样本数（单次 ≥ 1 ms，说明测量被打断）。 */
uint32_t sfra_app_isr_cost_dropped(void);
/* 分阶段耗时：sfra_inject 段（查表正弦 + 幅度缩放）。
 * 与总耗时相减即得 dut_step + monitor + advance 段。 */
void sfra_app_isr_cost_split(uint32_t *avg_cycles, uint32_t *max_cycles);
void sfra_app_isr_cost_reset(void);

/* ISR 实际节拍频率（Hz），未初始化返回 0。
 * shell 用它把周期数换算成"本拍预算占用百分比"。 */
float sfra_app_isr_freq_hz(void);

/* 当前扫描状态与点数，供 shell 与自检使用。 */
const sfra_t       *sfra_app_handle(void);
const sfra_result_t *sfra_app_result(void);

/* 立刻停止扫频（安全优先：注入归零）。shell 的 sfra stop 命令用它。 */
void sfra_app_stop(void);

#ifdef __cplusplus
}
#endif

#endif /* __SFRA_APP_H */
