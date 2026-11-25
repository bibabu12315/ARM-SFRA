/**
  ******************************************************************************
  * @file    sfra_pend.h
  * @brief   SFRA 控制节拍（TIM7）与 ISR 开销测量
  ******************************************************************************
  * 为什么用 TIM7 而不是 DWT：
  *   Cortex-M0+ 没有 DWT（Data Watchpoint and Trace）里的 CYCCNT 周期计数器
  *   ——那东西是 M3/M4/M7 才有。所以 docs/06 §6.5 的"方法 A"在 PY32F071 上
  *   用不了，只能用"方法 B：GPIO + 示波器"。
  *
  *   但示波器只能看到时间，换不成周期数（还要知道主频、还要人工算）。
  *   折中办法：TIM7 既产生控制节拍，也当计时器用——在 ISR 里读 SysTick
  *   的计数值（SysTick 由 HAL_Init 配成 1 ms 周期，72 MHz 下每毫秒 72000
  *   个时钟），用"毫秒数 + 计数值"拼出高精度时间戳。两次读的差值就是真实
  *   经过时间，乘主频得到周期数。
  *
  *   这条路径的可信度介于 DWT 和纯示波器之间：
  *     - 分辨率 1 个 AHB 时钟（SysTick 计数）；
  *     - 不含中断进入/退出开销（与 DWT 法同样漏掉这部分）；
  *     - 但**包含访存与流水线**，比静态指令数真实。
  *   所以 GPIO 翻转钩子保留：示波器给"绝对时间"，SysTick 给"周期数"，
  *   两者能互相校验（时间 × 主频 应该约等于周期数）。
  *
  * TIM7 的独占性：bsp_timer_measure_* 也用 TIM7。两者不能同时用，
  * 由 SFRA_USE_TIM7_ISR 编译开关保证——打开 SFRA 节拍时，
  * 不要在别处再调 bsp_timer_measure_init()。
  ******************************************************************************
  */
#ifndef __SFRA_PEND_H
#define __SFRA_PEND_H

#include "bsp.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 启动/停止控制节拍。频率由 SFRA_ISR_FREQ_HZ 决定，PSC/ARR 在
 * sfra_pend_init() 里按实际 PCLK1 现算，所以改主频不用改这里。
 *
 * 注意：本工程 APB 不分频，PCLK1 = HCLK = 72 MHz。
 * 若将来 PCLK1 与 HCLK 不同频，TIM7 的时钟是 PCLK1 的两倍（APB 预分频
 * ≠ 1 时定时器时钟自动 ×2），那时这里必须跟着改。定时器初始化时会读
 * HAL_RCC_GetPCLK1Freq() 并判断，不满足条件直接返回 BSP_ERROR。 */
bsp_status_t sfra_pend_init(void);
bsp_status_t sfra_pend_start(void);
void         sfra_pend_stop(void);

/* 由 TIM7_IRQHandler 调用。不要从别处调用。 */
void sfra_pend_irq_handler(void);

/* 节拍是否在跑。 */
bool sfra_pend_is_running(void);

/* 实际配置出来的 ISR 频率（重装值算出来的真值，不是标称值）。
 *
 * 为什么要报真值：TIM7 的 ARR 是整数，f = clk/((PSC+1)(ARR+1)) 一般
 * 除不尽。比如 10 kHz 在 72 MHz 下正好整除，但 8 kHz 就差 0.03%。
 * SFRA 的 f_act 是用 isr_freq 反算的，两者不一致会让整条曲线横向偏移
 * 这同一个比例——低频看不出来，高频会被放大误判成"相位不对"。
 * 所以 sfra_app_init() 用 sfra_pend_actual_freq() 的返回值去填 cfg.isr_freq。 */
float sfra_pend_actual_freq(void);

/* 节拍溢出次数（ISR 内处理不及）。非 0 说明 ISR 占比过高或主循环
 * 长时间关中断，扫出来的数据不可信。 */
uint32_t sfra_pend_overrun_count(void);

#ifdef __cplusplus
}
#endif

#endif /* __SFRA_PEND_H */
