/**
  ******************************************************************************
  * @file    sfra_app.c
  * @brief   SFRA 应用层：初始化、主循环、文本/二进制字节分流
  ******************************************************************************
  * ===========================================================================
  * 一、为什么要分流
  * ===========================================================================
  *
  * USART1 上同时跑两种语言：
  *
  *     文本 shell（app_log.c）      人打的命令，`help\n`
  *     二进制 SFRA 协议（sfra_proto.c）机器发的帧，`AA 55 ...`
  *
  * 两者共用一条物理链路。不能简单地把收到的每个字节都塞给两边：
  *   给协议层塞 ASCII 会污染它的帧同步计数（虽然 CRC 会挡掉，但每次
  *   刷新页面都会多几次 crc_errors，排查时干扰判断）；
  *   给 shell 塞二进制会把行缓冲塞成乱码，然后吐一句 "Invalid character"，
  *   而那串字节本来是一条合法命令——这条应答又会被上位机当成垃圾。
  *
  * 所以按**帧头**分流：首字节是 0xAA 且次字节是 0x55，进二进制通道；
  * 其余进文本通道。
  *
  * ===========================================================================
  * 二、分流状态机（这里是全文件最容易写错的地方）
  * ===========================================================================
  *
  * 难点：判定"是不是 AA 55"需要看**两个**字节，但 bsp_uart_read_byte()
  * 一次只给一个。所以必须暂存一个字节，并且不能丢。
  *
  *     状态 IDLE
  *        字节 != 0xAA  → 直接给 shell
  *        字节 == 0xAA  → 暂存，进状态 SAW_AA
  *
  *     状态 SAW_AA
  *        下一个字节 == 0x55 → 两个都进协议层，回 IDLE
  *        下一个字节 != 0x55 → **先**把 0xAA 补给 shell，**再**处理这个字节
  *                              （它可能又是一个 0xAA，所以重新走一遍 IDLE 的判断）
  *
  *     状态 IN_FRAME
  *        一直喂给协议层，直到协议层"吃掉"整帧。
  *
  * 三个坑：
  *   1. **不要用"帧内就不看帧头"的简化**。协议层自己会找帧头，我们只管
  *      "两个字节确认后交给它"，之后来的字节一律进协议层——因为一条帧
  *      内部的 payload 完全可能是 AA 55，此时再分流就会把一条好帧劈成两半。
  *      退出 IN_FRAME 的唯一依据是"协议层已经消费了整帧"。
  *   2. **AA 回补必须在前**。写反了会把命令的第一个字符吞掉，
  *      用户输 `\xAAhelp` 就变成 `elp`，报"未知命令"，极难看出原因。
  *   3. **文本通道的字节不能攒批**。app_log_poll() 每次只吃一个字节
  *      （它的设计就是字节状态机），所以这里也只能一个一个喂。
  *
  * ===========================================================================
  * 三、帧结束怎么判定
  * ===========================================================================
  *
  * 协议层没有对外暴露"当前缓冲里有几个字节"。最省事也最可靠的做法是
  * **每次喂一个字节后马上跑一次 sfra_proto_poll()**，poll 内部会
  * 反复尝试 take_frame() 直到凑不出完整帧为止。于是：
  *
  *     喂 N 个字节 → 调用 poll N 次 → 缓冲里剩下的必然是不完整帧
  *
  * 那么"帧结束"就是"poll 之后 rx_len 归零"。sfra_proto_t 是公开结构体
  * （sfra_proto.h 里允许调用方读），所以可以直接看 p->rx_len。
  *
  * 为什么不按 LEN 字段自己算帧长：那样要和协议层各写一遍帧格式，
  * 一旦协议升级就漏改一处。读 rx_len 是"问协议层还剩多少"，单一事实来源。
  *
  * ===========================================================================
  * 四、shell 被打断怎么办
  * ===========================================================================
  *
  * 用户可能在 shell 里输了一半，上位机突然开始扫频。此时 shell 的行缓冲
  * 里有一段瞎字符，协议层收到 AA 55 后会正常处理；等扫频结束用户再敲
  * 命令，前面那半截会和后面拼在一起。
  *
  * 处理：进入二进制通道时给 shell 送一个 '\r'，让它把半截行当成一条
  * 命令执行掉（大概率报"未知命令"）并清空行缓冲。代价是上位机扫频
  * 期间 shell 会意外吐一行提示，可以接受；不做的话用户看到的乱码更难解释。
  ******************************************************************************
  */

#include "sfra_app.h"
#include "sfra_pend.h"
#include "sfra_dut.h"
#include "sfra_port.h"
#include "app_log.h"
#include "bsp_uart.h"

#include <stdio.h>
#include <string.h>

/*============================ 配置 ========================================*/

/* 默认扫频参数，与 docs/06 的期望表一致：100 Hz 起，20 点/十倍频程。
 *
 * vec_length 取 68 而不是按 f_s/5 算出的真实点数：协议层会用
 * sfra_effective_points() 把 vec_length 截到触顶前的最后一个点，
 * 所以填大一点没关系，但**不能超过 SFRA_MAX_POINTS**（数组只有那么大）。
 * 100 Hz × 1.122^75 ≈ 100 Hz × 1.1e4 ≈ 1.1 MHz，足够覆盖 2 kHz 的上限。 */
#define SFRA_DEF_FREQ_START     100.0f
#define SFRA_DEF_FREQ_STEP      1.1220184543019633f    /* 10^(1/20) */
#define SFRA_DEF_AMPLITUDE      0.01f
#define SFRA_DEF_VEC_LENGTH     68U
#define SFRA_DEF_CYCLES_MODE    SFRA_CYC_NORMAL
#define SFRA_DEF_MODE           SFRA_MODE_PLANT

/* 默认被测对象特征频率。
 *
 * 取 f_s/10 = 1 kHz（f_s = 10 kHz）：
 *   - 转折点落在扫频带中间，两侧各有一个多 decade；
 *   - f_s/5 = 2 kHz 的上限下，只能看到 −14 dB / −110°，看不到 −40 dB/dec
 *     的完整平台。这是 10 kHz 采样率的固有局限，不是 bug。
 *   想要完整高频段就把 SFRA_ISR_FREQ_HZ 提到 100 kHz 并把 fc 提到 10 kHz。
 *
 * 与 tools/selftest_s6a.py 的 --fc 必须一致，否则判据算出来的理论值对不上。 */
#define SFRA_DEF_DUT_FC_HZ      1000.0f

/*============================ 内部状态 ====================================*/

static sfra_t        s_sfra;
static sfra_proto_t  s_proto;
static sfra_result_t s_res;

static float s_freq[SFRA_MAX_POINTS];
static float s_ac[SFRA_MAX_POINTS];
static float s_as[SFRA_MAX_POINTS];
static float s_bc[SFRA_MAX_POINTS];
static float s_bs[SFRA_MAX_POINTS];

static sfra_setup_t s_def;
static bool         s_ready;

/* 文本 shell 通路开关（见 sfra_local_cfg.h 的 SFRA_LOCAL_USE_TEXT_SHELL）。
 *
 * =1：保留分流器，字节先看帧头再决定走 shell 还是协议。
 * =0：所有字节直接进协议层，shell 命令失效。上位机不受影响。 */
#ifndef SFRA_LOCAL_USE_TEXT_SHELL
#define SFRA_LOCAL_USE_TEXT_SHELL 1
#endif

/* 分流状态机 */
typedef enum
{
    MUX_IDLE = 0,       /* 等首字节 */
    MUX_SAW_AA,         /* 刚收到 0xAA，等 0x55 */
    MUX_IN_FRAME        /* 已在二进制帧内 */
} mux_state_t;

static mux_state_t  s_mux;
static uint32_t     s_mux_text_bytes;    /* 累计给 shell 的字节数，shell 诊断用 */
static uint32_t     s_mux_bin_frames;    /* 累计完整二进制帧数 */
static uint32_t     s_mux_orphan_aa;     /* AA 后面不是 55 的次数 */

/*============================ 内部分流 ====================================*/

#if SFRA_LOCAL_USE_TEXT_SHELL
/**
  * @brief  把一个字节交给文本 shell。
  * @param  byte: 待处理字节。
  * @retval 无
  * @note   只做输入处理，不在内部读串口——字节已经由分流器取走了。
  *         （app_log_poll() 是"自拉自处理"形态，分流器不能用它，
  *         否则它会把分流器取走的字节当成"没数据"，直接丢掉。）
  */
static void mux_to_shell(uint8_t byte)
{
    (void)app_log_rx_byte(byte);
    s_mux_text_bytes++;
}
#endif /* SFRA_LOCAL_USE_TEXT_SHELL */

/**
  * @brief  把一个字节交给 SFRA 协议层，并顺带跑一次 poll 判帧尾。
  * @param  byte: 待处理字节。
  * @retval 无
  */
static void mux_to_proto(uint8_t byte)
{
    sfra_proto_rx(&s_proto, &byte, 1U);

    /* 每进来一字节就尝试解析一次：一帧最多 260 字节，协议层的
     * take_frame() 在"数据不够"时立刻返回 0，代价只有几次比较。
     * 这样不必自己维护帧长，也就不会与协议层各写一份帧格式。 */
    sfra_proto_poll(&s_proto, &s_sfra, &s_res, HAL_GetTick());

    if (s_proto.rx_len == 0U)
    {
        /* 缓冲空了 = 整帧已被消费（或假数据被逐字节丢弃）。
         * 注意 CRC 错的帧也会把 rx_len 递减到某处，但因为协议层
         * 每次只丢一字节并重试，最终 rx_len 仍会归零。 */
        if (s_mux == MUX_IN_FRAME)
        {
            s_mux = MUX_IDLE;
            s_mux_bin_frames++;
        }
    }
}

/**
  * @brief  分流器：决定一个字节走文本还是二进制通道。
  * @param  byte: 收到的字节。
  * @retval 无
  */
void sfra_app_feed_byte(uint8_t byte)
{
#if !SFRA_LOCAL_USE_TEXT_SHELL
    /* 关闭文本通路：不做帧头判断，字节直接进协议层。
     * mux_to_proto() 里已经包含 sfra_proto_rx() + sfra_proto_poll()，
     * 所以这一条语句等价于原来 MUX_IN_FRAME 分支的全部工作。
     * 那几个 s_mux_* 计数器保持为 0（没有 shell 也没人看）。 */
    mux_to_proto(byte);
#else
    switch (s_mux)
    {
    case MUX_IDLE:
        if (byte == SFRA_SOF0)
        {
            s_mux = MUX_SAW_AA;
        }
        else
        {
            mux_to_shell(byte);
        }
        break;

    case MUX_SAW_AA:
        if (byte == SFRA_SOF1)
        {
            /* 确认帧头。先给 shell 一个回车，把可能存在的半截行清掉
             * （见文件头第四节）。再喂协议层两个 SOF 字节。 */
            mux_to_shell('\r');
            s_mux = MUX_IN_FRAME;
            mux_to_proto(SFRA_SOF0);
            mux_to_proto(SFRA_SOF1);
        }
        else if (byte == SFRA_SOF0)
        {
            /* AA AA ... ：前一个 AA 不是帧头，补给 shell，当前这个继续等 */
            s_mux_orphan_aa++;
            mux_to_shell(SFRA_SOF0);
        }
        else
        {
            /* AA 后面跟了别的：AA 补给 shell，当前字节重新走 IDLE 判断
             * （它可能是文本字符，也可能恰好又是 0xAA） */
            s_mux_orphan_aa++;
            mux_to_shell(SFRA_SOF0);
            s_mux = MUX_IDLE;
            sfra_app_feed_byte(byte);
        }
        break;

    case MUX_IN_FRAME:
    default:
        mux_to_proto(byte);
        break;
    }
#endif /* SFRA_LOCAL_USE_TEXT_SHELL */
}

/*============================================================================*/
/*                              初始化                                        */
/*============================================================================*/

/**
  * @brief  初始化 SFRA 应用层。
  * @param  无
  * @retval BSP_OK；参数或节拍配置失败返回 BSP_ERROR。
  */
bsp_status_t sfra_app_init(void)
{
    float fs;

    /* ① 建正弦表。必须是第一步，而且全工程只能调一次
     *    （它写的是 sfra_core.c 里的静态表，重复调用不会出错但白费时间）。 */
    sfra_init_all();

    /* ② 确定采样率。优先用 TIM7 配置出来的**真值**——TIM7 的 ARR 是整数，
     *    除不尽时实际频率与标称值有偏差，而这个偏差会按同一比例缩放整条
     *    曲线的横轴，低频看不出来、高频直接变成"相位不对"。 */
#if SFRA_USE_TIM7_ISR
    if (sfra_pend_init() != BSP_OK)
    {
        return BSP_ERROR;
    }
    fs = sfra_pend_actual_freq();
    if (fs <= 0.0f)
    {
        return BSP_ERROR;
    }
#else
    fs = SFRA_ISR_FREQ_HZ;
#endif

    /* ③ 默认配置。isr_freq 是硬件事实，PC 端改不了（SET_CONFIG 会忽略它）。 */
    memset(&s_def, 0, sizeof(s_def));
    s_def.isr_freq    = fs;
    s_def.freq_start  = SFRA_DEF_FREQ_START;
    s_def.freq_step   = SFRA_DEF_FREQ_STEP;
    s_def.amplitude   = SFRA_DEF_AMPLITUDE;
    s_def.vec_length  = SFRA_DEF_VEC_LENGTH;
    s_def.cycles_mode = SFRA_DEF_CYCLES_MODE;
    s_def.mode        = SFRA_DEF_MODE;

    s_res.freq_vect = s_freq;
    s_res.a_c = s_ac;
    s_res.a_s = s_as;
    s_res.b_c = s_bc;
    s_res.b_s = s_bs;

    sfra_init(&s_sfra, &s_def, &s_res);
    sfra_proto_init(&s_proto, &s_def);
    s_proto.cfg = s_def;            /* 让 GET_CAPS 报出的默认值与上面一致 */

    /* ④ 被测对象 */
    sfra_dut_init(SFRA_DUT_SALLEN_KEY, SFRA_DEF_DUT_FC_HZ, fs);

    /* ⑤ 分流器复位 */
    s_mux = MUX_IDLE;
    s_mux_text_bytes = 0U;
    s_mux_bin_frames = 0U;
    s_mux_orphan_aa  = 0U;

    s_ready = true;
    return BSP_OK;
}

/*============================================================================*/
/*                              主循环                                        */
/*============================================================================*/

/**
  * @brief  SFRA 主循环入口，非阻塞。
  * @param  无
  * @retval 无
  * @note   一次调用最多消费若干个字节、最多装一帧应答。刻意**不**把
  *         bsp_uart 的接收缓冲抽干：主循环里还有 app_protect_poll() 和
  *         MPPT 要跑，SFRA 不能独占。
  *
  *         但也不能一个字节都不读——921600 下每字节 10.85 µs，主循环
  *         一圈如果超过 2.78 ms 就会让 256 字节的 DMA 缓冲被追上（那时
  *         bsp_uart 报 RX_ERROR 并要求回车重同步）。所以这里给一个
  *         每轮最多 32 字节的上限：既保证吞吐（32 B × 每 ms 两轮 ≈ 64 KB/s
  *         远大于实际命令速率），又不至于单轮占太久。
  */
void sfra_app_poll(void)
{
    uint8_t  byte;
    uint16_t budget = 32U;
    bsp_uart_rx_result_t rx;

    if (!s_ready)
    {
        return;
    }

    /* ---- ① 收字节并分流 ---- */
    while (budget-- != 0U)
    {
        rx = bsp_uart_read_byte(&byte);
        if (rx == BSP_UART_RX_EMPTY)
        {
            break;
        }
        if (rx == BSP_UART_RX_ERROR)
        {
            /* 缓冲被追上或帧错误。bsp_uart 已经把 s_rx_tail 跳到最新位置，
             * 中间那段数据丢了。
             *
             * 这里**必须**告诉 shell，不能只是 continue：
             * 丢掉的可能是半行命令的中间几个字符，如果静默丢弃，
             * shell 会拿着一行"看起来完整、实际缺字"的输入去执行，
             * 表现成莫名其妙的 "Unknown command" 或 "Invalid character."。
             * 提示它清行并要求用户重敲，比让用户猜要好。 */
            s_mux = MUX_IDLE;
            app_log_rx_error();
            continue;
        }
        if (rx == BSP_UART_RX_FATAL)
        {
            s_ready = false;            /* DMA 故障，串口不可用 */
            return;
        }
        sfra_app_feed_byte(byte);
    }

    /* ---- ② 把应答倒出去 ----
     *
     * 顺序要紧：先 poll 组帧，再发送。反过来的话每帧要多等一轮主循环。
     *
     * tx_len 必须**清零**才继续：协议层 poll 的第一个判断就是
     * "tx_len != 0 直接返回"（防止覆盖未发完的半帧）。所以这里发完
     * 立刻 tx_done()。
     *
     * 一个字节都不能留在缓冲里——留着会导致之后所有应答积压，
     * 表现为"第一次能连上，之后全超时"（docs/06 的坑）。 */
    if (s_proto.tx_len != 0U)
    {
        sfra_port_uart_write(s_proto.tx_buf, s_proto.tx_len);
        sfra_proto_tx_done(&s_proto);
    }

    /* ---- ③ 解析命令（一次最多一帧） ---- */
    sfra_proto_poll(&s_proto, &s_sfra, &s_res, HAL_GetTick());

    if (s_proto.tx_len != 0U)
    {
        sfra_port_uart_write(s_proto.tx_buf, s_proto.tx_len);
        sfra_proto_tx_done(&s_proto);
    }

    /* ---- ④ 推进扫频状态机 ----
     *
     * **无条件**调用。写成 if (sfra_is_running()) 会让状态机卡在 NEXT：
     * COLLECT 采满后 state 立刻变 NEXT，此时 is_running 为假，background
     * 再没人调，扫频假死（docs/02 A 系列坑，S3 阶段踩过）。 */
    sfra_background_task(&s_sfra, &s_res);

    /* ---- ⑤ 按需启停节拍 ----
     *
     * 节拍跟着扫频状态走：START 之后 SETTLE 状态需要 ISR 跑起来产正弦，
     * DONE/IDLE 时需要停下来（注入必须是 0，不能一直挂着一个正弦）。
     * sfra_inject() 在 IDLE/DONE 时本身就返回 0，所以这里停节拍是
     * "省电 + 让示波器上看不到东西"，不是安全必需——但两者都做更明确。 */
#if SFRA_USE_TIM7_ISR
    {
        uint8_t st = sfra_state(&s_sfra);
        bool need = (st == SFRA_ST_SETTLE) || (st == SFRA_ST_COLLECT) ||
                    (st == SFRA_ST_NEXT);
        if (need && !sfra_pend_is_running())
        {
            /* v1.0 修复：100 Hz 首点报 NaN。
             *
             * sfra_dut_reset() 一直定义了却没人调用，于是新扫频的第一点
             * 从上一轮残留的 d1/d2 起步。首点 settle 只有 max(20ms, 2 个
             * 注入周期)，100 Hz 时正好 20 ms，不足以完全吞掉这份残留，
             * 导致首点的累加器异常（上位机算出 b_c=b_s=0 → Y/X = nan）。
             *
             * 放在"ISR 由停转启"这个边沿，因为那一刻必然是一次新扫频的
             * 开头；放在 do_start() 里反而要把 sfra_dut.h 拉进协议层，
             * 破坏"协议层不依赖被测对象"的分层。 */
            sfra_dut_reset();
            (void)sfra_pend_start();
        }
        else if (!need && sfra_pend_is_running())
        {
            sfra_pend_stop();
        }
    }
#endif
}

/*============================================================================*/
/*                              shell 接口                                    */
/*============================================================================*/

/**
  * @brief  生成一行状态摘要给 shell 的 sfra 命令。
  * @param  buf: 输出缓冲。
  * @param  size: 缓冲长度。
  * @retval 无
  */
void sfra_app_describe(char *buf, uint16_t size)
{
    static const char *const st_name[] =
    {
        "IDLE", "SETTLE", "COLLECT", "NEXT", "DONE"
    };
    uint8_t st;

    if ((buf == NULL) || (size == 0U))
    {
        return;
    }
    st = sfra_state(&s_sfra);

    /* txt/bin/orp 三个计数是分流器的"体感指标"，排查"shell 没反应"
     * 时先看这里：txt 不涨 = 字节根本没进来（bsp_uart / DMA 问题）；
     * txt 涨但 shell 不吐字 = 输出侧问题（bsp_uart_write 或缓冲覆盖）。 */
    (void)snprintf(buf, size,
                   "sfra %s pt=%u/%u fs=%lu crc=%lu ovr=%lu obj=%s fc=%lu"
                   " txt=%lu bin=%lu orp=%lu",
                   (st < 5U) ? st_name[st] : "?",
                   (unsigned)s_sfra.point_idx,
                   (unsigned)s_sfra.cfg.vec_length,
                   (unsigned long)(s_sfra.cfg.isr_freq + 0.5f),
                   (unsigned long)s_proto.crc_errors,
                   (unsigned long)sfra_pend_overrun_count(),
                   sfra_dut_name(),
                   (unsigned long)(sfra_dut_fc_hz() + 0.5f),
                   (unsigned long)s_mux_text_bytes,
                   (unsigned long)s_mux_bin_frames,
                   (unsigned long)s_mux_orphan_aa);
}

/**
  * @brief  取 ISR 开销统计。
  * @param  avg_cycles: 接收平均周期数。
  * @param  max_cycles: 接收最大周期数。
  * @param  count: 接收拍数。
  * @retval 无
  */
void sfra_app_isr_cost(uint32_t *avg_cycles, uint32_t *max_cycles,
                       uint32_t *count)
{
#if SFRA_MEASURE_ISR_COST
    extern void sfra_pend_cost(uint32_t *, uint32_t *, uint32_t *);
    sfra_pend_cost(avg_cycles, max_cycles, count);
#else
    if (avg_cycles) { *avg_cycles = 0U; }
    if (max_cycles) { *max_cycles = 0U; }
    if (count)      { *count = 0U; }
#endif
}

/**
  * @brief  取被丢弃的 ISR 开销样本数。
  * @param  无
  * @retval 单次测得 ≥ 1 ms 而被丢弃的样本数；未开启测量时返回 0。
  */
uint32_t sfra_app_isr_cost_dropped(void)
{
#if SFRA_MEASURE_ISR_COST
    extern uint32_t sfra_pend_cost_dropped(void);
    return sfra_pend_cost_dropped();
#else
    return 0U;
#endif
}

/**
  * @brief  取分阶段耗时：sfra_inject 段。
  * @param  avg_cycles: 接收平均周期数。
  * @param  max_cycles: 接收最大周期数。
  * @retval 无
  */
void sfra_app_isr_cost_split(uint32_t *avg_cycles, uint32_t *max_cycles)
{
#if SFRA_MEASURE_ISR_COST
    extern void sfra_pend_cost_split(uint32_t *, uint32_t *);
    sfra_pend_cost_split(avg_cycles, max_cycles);
#else
    if (avg_cycles) { *avg_cycles = 0U; }
    if (max_cycles) { *max_cycles = 0U; }
#endif
}

/**
  * @brief  取 ISR 实际节拍频率。
  * @param  无
  * @retval ISR 频率（Hz），未初始化时返回 0。
  * @note   返回的是 TIM7 配置出来的**真值**，不是标称常量。
  *         shell 用它与周期数相除，算出"本拍占了多少预算"。
  */
float sfra_app_isr_freq_hz(void)
{
    return s_ready ? s_sfra.cfg.isr_freq : 0.0f;
}

/**
  * @brief  清零 ISR 开销统计。
  * @param  无
  * @retval 无
  */
void sfra_app_isr_cost_reset(void)
{
#if SFRA_MEASURE_ISR_COST
    extern void sfra_pend_cost_reset(void);
    sfra_pend_cost_reset();
#endif
}

/**
  * @brief  取算法句柄（供 shell 与自检读取状态）。
  * @param  无
  * @retval sfra_t 指针。
  */
const sfra_t *sfra_app_handle(void)
{
    return &s_sfra;
}

/**
  * @brief  取结果数组句柄。
  * @param  无
  * @retval sfra_result_t 指针。
  */
const sfra_result_t *sfra_app_result(void)
{
    return &s_res;
}

/**
  * @brief  立刻停止扫频。
  * @param  无
  * @retval 无
  * @note   安全优先：sfra_abort() 先把注入归零再改状态。节拍下一轮
  *         主循环会被 sfra_app_poll() 停掉。
  */
void sfra_app_stop(void)
{
    sfra_abort(&s_sfra);
}
