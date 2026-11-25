/* ---------------------------------------------------------------------------
 * sfra_port.h - 移植适配层（**唯一需要你动手填的文件**）
 *
 * 02 文档 §2.2.3。整个库只有这里的函数与具体芯片/板子有关：
 *
 *      ┌──────────────────────────────────────────────────────────┐
 *      │  sfra_core.c/h   算法与状态机   ← 平台无关，C99，不用改    │
 *      │  sfra_proto.c/h  协议栈         ← 平台无关，C99，不用改    │
 *      │  sfra_port.h     就是本文件     ← 你按自己的 HAL 实现      │
 *      └──────────────────────────────────────────────────────────┘
 *
 * ===========================================================================
 * 一、必须实现（4 个）
 * ===========================================================================
 *
 *      void    sfra_port_uart_write(const uint8_t *buf, uint16_t len);
 *      int16_t sfra_port_uart_read_byte(void);
 *      void    sfra_port_enter_critical(void);
 *      void    sfra_port_exit_critical(void);
 *
 * **这四个函数不在库的调用链上。** 它们是"你要遵守的适配约定"：
 * 库本身只认 `sfra_proto_rx()`（塞字节进来）和 `tx_buf`（取帧出去），
 * 不直接调用任何 port 函数。所以：
 *
 *   - 不实现它们，`sfra_core.c` + `sfra_proto.c` **照样能编译链接通过**；
 *   - 但你的应用代码（那个把串口和协议层接起来的 3 行主循环）必须用
 *     它们，否则上位机一个字节都收不到。
 *
 * 这是**有意的设计**，不是漏洞：库保持零 HAL 依赖（02 §2.1 边界原则），
 * 才能在 PC 上用 stdin/stdout 跑同一份代码做端到端验证（S3/S4 的做法）。
 * 代价是不能靠链接器逼你实现——所以 S6a 的验收方式是"上板能 PING 通"，
 * 而不是"编译能过"（见 §五 自检清单）。
 *
 * ===========================================================================
 * 二、可选实现（2 个弱符号）
 * ===========================================================================
 *
 *      int sfra_proto_get_conditions(float out[8]);   // Vin/Vout/… 回读
 *      int sfra_proto_set_coeff(uint8_t order, const float *b);
 *
 * 注意这两个函数的**名字带 proto**，不叫 `sfra_port_*`。原因：
 * 它们的调用点是 `sfra_proto.c` 里的命令分派（GET_CONDITIONS /
 * SET_COEFF 两条命令），是"协议层需要用户回答的问题"，不是"移植层
 * 能力"。之前文档里 02 §2.2.3 写成了 `sfra_port_get_vin/vout/iout`
 * 三个函数，与这里的 `get_conditions(out[8])` 是同一件事的两种说法，
 * 已经统一到后者（一次取齐 8 个量，正好对应 CONDITIONS 帧的 32 B）。
 *
 * `sfra_proto.c` 里给了弱符号默认实现：
 *      get_conditions → 全部填 0、返回 0（上位机看到 0，不报错）
 *      set_coeff      → 返回 -1（上位机收到 NACK BAD_PARAM）
 * 所以初版可以不写这两个，先把扫频跑通，再补标定信息。
 *
 * ===========================================================================
 * 三、实现示例（STM32 HAL，可直接抄）
 * ===========================================================================
 *
 *   sfra_port.c:
 *
 *     #include "sfra_port.h"
 *     #include "main.h"                 // 你的 HAL 头
 *
 *     extern UART_HandleTypeDef huart1;
 *
 *     void sfra_port_uart_write(const uint8_t *buf, uint16_t len)
 *     {
 *         // 阻塞发送最简单；要非阻塞就改成 DMA + 完成中断置标志，
 *         // 并让 sfra_proto_tx_done() 在发完中断里调（见下面"注意 2"）。
 *         HAL_UART_Transmit(&huart1, (uint8_t *)buf, len, 50);
 *     }
 *
 *     int16_t sfra_port_uart_read_byte(void)
 *     {
 *         // 上位机是主、MCU 是从。协议层不需要主动读串口：字节由
 *         // UART RX 中断里调 sfra_proto_rx() 塞进环形缓冲。
 *         // 所以这里返回 -1（"没数据"）即可。
 *         return -1;
 *     }
 *
 *     void sfra_port_enter_critical(void) { __disable_irq(); }
 *     void sfra_port_exit_critical(void)  { __enable_irq();  }
 *
 *     // 需要上报测试条件就实现这两个（不实现走弱符号默认）
 *     int sfra_proto_get_conditions(float out[8])
 *     {
 *         out[0] = adc_to_volt(VIN_ADC);      // vin   [V]
 *         out[1] = adc_to_volt(VOUT_ADC);     // vout  [V]
 *         out[2] = adc_to_amp(IOUT_ADC);      // iout  [A]
 *         out[3] = g_isr_freq;                // fs    [Hz]
 *         out[4] = DEADTIME_NS;               // deadtime [ns]
 *         out[5] = g_temp_c;                  // temp  [°C]
 *         out[6] = ADC_VREF_V;                // adc_fs_v 满量程电压
 *         out[7] = SHUNT_GAIN;                // shunt_gain [V/A]
 *         return 0;
 *     }
 *
 *     int sfra_proto_set_coeff(uint8_t order, const float *b)
 *     {
 *         if (order > 3u) return -1;
 *         for (uint8_t i = 0; i < order; i++) g_comp.b[i] = b[i];
 *         return 0;
 *     }
 *
 * ===========================================================================
 * 四、四个必须知道的注意点
 * ===========================================================================
 *
 * 注意 1 · **不要在中断里调用协议函数**
 *     `sfra_proto_poll()` / `sfra_background_task()` 只能放在主循环。
 *     UART RX 中断里只调 `sfra_proto_rx()`（它只做入缓冲）。
 *
 * 注意 2 · `sfra_port_uart_write()` 必须是"整段写出去"或"由你保证顺序"
 *     库是按"一帧装进 tx_buf → 调一次 write → 调 tx_done()"用的。
 *     若你用 DMA 且 write() 立即返回，**不要**马上调 tx_done()——
 *     等 DMA 传输完成中断里再调，否则下一帧会覆盖正在发的半帧。
 *     发半帧的表现是"偶发 CRC 错"，极难查。
 *
 * 注意 3 · 临界区要短
 *     它只保护 `sfra_t` 的累加器与状态字（几个 32/64 位量），
 *     ISR 里 `sfra_inject/monitor/advance` 与主循环的
 *     `sfra_background_task` 之间的竞态。**不要**用它去包 ADC 或 PWM。
 *     若你的 ISR 里本来就不调 background_task（推荐），可以留空实现，
 *     但那时 `sfra_background_task` 必须更快——它每拍都要判一次。
 *
 * 注意 4 · **500 ms 通信超时要靠 now_ms**
 *     `sfra_proto_poll(..., now_ms)` 的 now_ms 必须单调递增、单位毫秒。
 *     用 SysTick 计数 × 节拍即可。传 0 = 关闭超时检查（只在自测时用）。
 *     真机必须开：串口一断，MCU 要能自己把注入归零。
 *
 * ===========================================================================
 * 五、上板前自检清单（对应 05 文档 §5.5）
 * ===========================================================================
 *
 *   [ ] 四个必需函数都实现了（编译能过 = 实现全了，声明无默认体）
 *   [ ] ISR 里 inject → monitor → advance 三行齐全，顺序对
 *   [ ] 主循环里 sfra_proto_poll 与 sfra_background_task 都被调用
 *   [ ] UART RX 中断里调了 sfra_proto_rx
 *   [ ] 波特率 921600 8N1 对得上（PING 不通先查这个）
 *   [ ] now_ms 单调，超时不是 0
 * ------------------------------------------------------------------------- */
#ifndef SFRA_PORT_H
#define SFRA_PORT_H

#include <stdint.h>

#include "sfra_core.h"
#include "sfra_proto.h"

#ifdef __cplusplus
extern "C" {
#endif

/* =========================================================================
 * 必需：传输层
 * ========================================================================= */

/* 把 len 个字节交给串口发出。
 *
 * 语义要求（违反会出现"偶发 CRC 错"，因为半帧被下一帧覆盖）：
 *   - 要么阻塞到全部发完再返回；
 *   - 要么非阻塞，但必须保证调用方在"发完"之后才调
 *     `sfra_proto_tx_done()`（见注意 2）。
 * 不允许"部分发送、剩下的丢掉"——调用方无法知道丢了多少。 */
void sfra_port_uart_write(const uint8_t *buf, uint16_t len);

/* 取一个字节。**无数据时返回 -1**（不是 0，0 是合法的字节值）。
 *
 * 大多数实现直接 `return -1;` —— 因为字节是 UART RX 中断主动
 * 塞进 `sfra_proto_rx()` 的，协议层不需要轮询串口。
 * 只有"主循环轮询式接收"的实现（比如寄存器直读）才需要真的读。 */
int16_t sfra_port_uart_read_byte(void);

/* =========================================================================
 * 必需：临界区
 * ========================================================================= */

/* 进/出临界区：保护 `sfra_t` 的累加器与状态字。
 *
 * 实现要求：
 *   - 关的是**控制 ISR**，不是全部中断。关 UART 中断会导致丢字节。
 *   - 成对使用，临界区里不要调任何会开中断的函数。
 *   - 临界区内只做几个 64 位量的读写，别放循环。
 *
 * 参考实现见 `firmware/examples/pc/sfra_port_pc.c`：
 *     PC 假 MCU 是单线程（协议层与"ISR"都在同一个循环里），所以留空。
 *     STM32 上填 `__disable_irq();` / `__enable_irq();` 即可。 */

void sfra_port_enter_critical(void);
void sfra_port_exit_critical(void);

/* =========================================================================
 * 可选：由协议层的弱符号回调（声明在 sfra_proto.h，这里只是提示）
 * =========================================================================
 *
 * int sfra_proto_get_conditions(float out[8]);
 *      顺序：vin, vout, iout, fs, deadtime, temp, adc_fs_v, shunt_gain
 *      返回 0 = 成功；非 0 = 上位机收到 NACK。
 *      不实现 → 弱符号填 0，上位机 meta.json 里这些字段全是 0。
 *
 * int sfra_proto_set_coeff(uint8_t order, const float *b);
 *      返回 0 = 接受；非 0 = NACK BAD_PARAM。
 *      不实现 → 弱符号返回 -1，"下发系数"按钮会报 BAD_PARAM。
 *
 * =========================================================================
 * 参考实现（不放这里，是为了让"没实现就编译失败"这条护栏成立）
 * =========================================================================
 * 完整的可编译样例见 `firmware/examples/pc/sfra_sim_mcu.c`：
 * 它用 stdin/stdout 当 UART、临界区留空，是把本文件四个函数换
 * 成 PC 等价物后的样子。移植到 MCU 时**照它的骨架改 HAL 调用**，
 * 不要重新设计调用顺序。
 */

#ifdef __cplusplus
}
#endif

#endif /* SFRA_PORT_H */
