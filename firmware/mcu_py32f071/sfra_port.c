/**
  ******************************************************************************
  * @file    sfra_port.c
  * @brief   SFRA 库移植层：把零 HAL 依赖的算法/协议层接到本工程的 BSP 上
  ******************************************************************************
  * 本文件实现 sfra_port.h 声明的四个必需函数，并覆盖 sfra_proto.c 里的
  * 三个弱符号回调。sfra_core.c / sfra_proto.c 一行都不改。
  *
  * 与 docs/06 §6.2 的对应关系：
  *   ① sfra_port_uart_write()            → bsp_uart_write()
  *   ② sfra_port_uart_read_byte()        → 固定返回 -1（字节由分流器塞入）
  *   ③ sfra_port_enter/exit_critical()   → 保存/恢复 PRIMASK
  *   ④ UART RX 中断                       → 本工程不用 RX 中断，改用主循环
  *                                          分流器轮询 bsp_uart_read_byte()
  *
  * ④ 的偏离是有意的，理由见 sfra_app.c 顶部关于"文本 shell 与二进制协议
  * 共存"的说明：现有 BSP 的 RX 是纯 DMA 循环缓冲，字节要靠主循环按需
  * 取走；在 DMA 完成中断里塞协议层会让 shell 拿到重复字节。改成主循环
  * 分流后，一次 sfra_proto_poll() 的最大延迟 = 主循环一圈（实测百微秒
  * 量级），远小于协议的 500 ms 超时。
  ******************************************************************************
  */

#include "sfra_port.h"
#include "sfra_app.h"
#include "bsp_uart.h"

/*============================================================================*/
/*                              传输层                                        */
/*============================================================================*/

/* 临界区嵌套计数与保存的 PRIMASK。
 *
 * 为什么不能只是 __disable_irq() / __enable_irq()：
 *   嵌套调用时内层退出会无条件开中断，把外层的保护提前解除。
 *   本工程目前只在主循环上下文调用（协议层内部成对使用），嵌套深度
 *   最多 1，但保留计数能让"将来在别处再包一层"不至于静默失效。 */
static uint32_t s_crit_primask;
static uint8_t  s_crit_depth;

/**
  * @brief  把一帧字节交给串口发出。
  * @param  buf: 待发数据。
  * @param  len: 字节数，最大 260（SFRA_PROTO_MAXFRAME）。
  * @retval 无
  * @note   **必须整帧发全**。bsp_uart_write() 在环形缓冲剩余空间不足时
  *         返回 BSP_BUSY 而不是部分入队，所以这里可以安全地重试；
  *         若返回 BSP_ERROR（DMA 故障或参数非法），再重试也没意义，
  *         只能丢弃——但那时串口整条链路已经废了，协议层收不到应答
  *         会自己超时归零注入，属于安全方向。
  *
  *         不能在这里调用 app_log_printf()：文本会和二进制帧交织，
  *         上位机解析彻底乱掉。
  */
void sfra_port_uart_write(const uint8_t *buf, uint16_t len)
{
    uint32_t guard = UINT32_C(200000);

    if ((buf == NULL) || (len == 0U))
    {
        return;
    }

    /* 921600 下 260 字节约 2.8 ms。环形缓冲 511 字节，正常情况一两次
     * 就能进队；guard 只是防止 DMA 彻底不动时把主循环卡死在这里。 */
    while (bsp_uart_write((const char *)buf, len) == BSP_BUSY)
    {
        if (--guard == 0U)
        {
            return;                     /* DMA 疑似卡死，放弃本帧 */
        }
    }
}

/**
  * @brief  尝试从串口取一个字节。
  * @param  无
  * @retval 固定返回 -1（无数据）
  * @note   字节由 sfra_app.c 的分流器在主循环里从 bsp_uart_read_byte()
  *         取出后直接塞进 sfra_proto_rx()，协议层不需要自己读串口。
  *         这里返回 -1 是 sfra_port.h 明确允许的实现方式。
  *
  *         故意**不**在这里去读 bsp_uart_read_byte()：那样会与分流器
  *         抢字节，出现"一半字节给了 shell、一半给了协议层"的局面。
  */
int16_t sfra_port_uart_read_byte(void)
{
    return -1;
}

/**
  * @brief  进入临界区，保护 sfra_t 的 int64 累加器与状态字。
  * @param  无
  * @retval 无
  */
void sfra_port_enter_critical(void)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    if (s_crit_depth == 0U)
    {
        s_crit_primask = primask;
    }
    if (s_crit_depth < UINT8_MAX)
    {
        s_crit_depth++;
    }
}

/**
  * @brief  退出临界区，恢复进入前的中断状态。
  * @param  无
  * @retval 无
  */
void sfra_port_exit_critical(void)
{
    if (s_crit_depth == 0U)
    {
        return;                         /* 不成对调用，忽略而不是开中断 */
    }
    s_crit_depth--;
    if (s_crit_depth == 0U)
    {
        __set_PRIMASK(s_crit_primask);
    }
}

/*============================================================================*/
/*                    弱符号回调（SET/GET_COEFF、CONDITIONS）                  */
/*============================================================================*/

/* 补偿器系数镜像。
 *
 * SFRA 库不假设你的补偿器长什么样，只要求"收到的系数真的存下来了"。
 * 本工程还没有补偿器（S6a 的被测对象是软件 Sallen-Key，不是闭环控制），
 * 所以这里只做一份 RAM 镜像：SET_COEFF 写进来、GET_COEFF 读出去。
 * 等到 S6b 接真实功率级时，把 set_coeff 里的赋值换成"写进你的 PI 模块"，
 * get_coeff 换成"从 PI 模块读当前生效值"即可，协议层不用动。
 *
 * 为什么必须真存：GET_COEFF 的意义是"读回当前生效值"，如果只是回显
 * 上一次 SET_COEFF 的入参，读回校验就退化成了自欺欺人（见 sfra_proto.h）。 */
static float   s_coeff[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
static uint8_t s_coeff_order;

/**
  * @brief  收到 SET_COEFF：保存系数并回读校验。
  * @param  order: 阶数 1~4。
  * @param  b: 系数数组，长度 order。
  * @retval 0 表示接受；-1 表示参数非法。
  */
int sfra_proto_set_coeff(uint8_t order, const float *b)
{
    uint8_t k;

    if ((b == NULL) || (order < 1U) || (order > 4U))
    {
        return -1;
    }
    for (k = 0U; k < order; k++)
    {
        s_coeff[k] = b[k];
    }
    for (k = order; k < 4U; k++)
    {
        s_coeff[k] = 0.0f;
    }
    s_coeff_order = order;
    return 0;
}

/**
  * @brief  收到 GET_COEFF：报告当前生效的系数。
  * @param  out: 至少 4 个 float 的接收缓冲。
  * @retval 实际阶数 1~4；返回 <=0 表示尚无系数（上位机显示"未设置"）。
  * @note   从没下发过时返回 -1 而不是一串 0——回 0 会让用户误以为
  *         已经设成了零系数。
  */
int sfra_proto_get_coeff(float out[4])
{
    uint8_t k;

    if ((out == NULL) || (s_coeff_order == 0U))
    {
        return -1;
    }
    for (k = 0U; k < s_coeff_order; k++)
    {
        out[k] = s_coeff[k];
    }
    return (int)s_coeff_order;
}

/**
  * @brief  收到 GET_CONDITIONS：报告本次测量的测试条件。
  * @param  out: 8 个 float，顺序见 sfra_proto.h。
  * @retval 0 表示成功。
  * @note   这些值会进 meta.json，是"这条曲线在什么工况下测的"的唯一记录。
  *         S6a 的被测对象是软件模型，所以 vin/vout/iout/温度/adc 满量程/
  *         分流增益都填 0 并**如实标记**——不要让上位机看到假数据。
  */
int sfra_proto_get_conditions(float out[8])
{
    uint8_t k;

    if (out == NULL)
    {
        return -1;
    }
    for (k = 0U; k < 8U; k++)
    {
        out[k] = 0.0f;
    }
    out[3] = SFRA_ISR_FREQ_HZ;          /* f_s，硬件事实，由配置宏给定 */
    return 0;
}
