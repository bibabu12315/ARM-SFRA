/* ---------------------------------------------------------------------------
 * sfra_sim_mcu.c - S3 阶段的"假 MCU"
 *
 * 目的：**在没有真板子的情况下把整条链路跑通**——PC 上位机发命令，
 * 这里解析、跑真实扫频算法（sfra_core.c）、回数据，PC 端解包出图。
 *
 * 它不是玩具：用的是同一份 sfra_core.c 和 sfra_proto.c，
 * 协议实现、状态机、扫频算法都是真固件将要用的代码。
 * 差别只有两点：
 *   1. 字节流走 stdin/stdout，不走 UART；
 *   2. 被测对象用软件模拟（LC / 一阶低通 / 数字 PI 闭环），不接功率级。
 *
 * 用法：
 *     sfra_sim_mcu.exe [--plant lc|lp|loop] [--isr 100000] [--timeout 0]
 *
 *     stdin  : 来自 PC 的原始协议字节
 *     stdout : 发给 PC 的原始协议字节（**必须二进制模式**，Windows 上
 *              不加 _setmode 会把 0x0A 变成 0x0D 0x0A，帧全毁）
 *     stderr : 日志（人类可读，不参与协议）
 *
 * 仿真时钟：每个 ISR 周期按真实 ISR 频率推进虚拟时间，但**不真的 sleep**，
 * 所以一次完整扫频在 PC 上 1 秒内就能跑完。这让 S3 的回归测试可行
 * （等真实时间的话一个用例要好几分钟）。
 * ------------------------------------------------------------------------- */
#include "sfra_core.h"
#include "sfra_proto.h"
#include "sfra_port.h"          /* 四个必需函数的声明（实现见 sfra_port_pc.c） */

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <conio.h>      /* _kbhit()：非阻塞探测管道/控制台有没有可读字节 */
#endif

/* sfra_port_pc.c 提供的初始化：把 stdin/stdout 切成二进制模式。
 *
 * 之所以让 port 层负责这件事而不是 main 直接 _setmode：
 * 二进制模式是"stdout 当 UART 用"这个实现细节的一部分，归 port 层管。
 * 移植到 MCU 时这个函数不存在（HAL 没有文本模式问题），main 里那一行
 * 也就跟着删掉——移植面更小。 */
void sfra_port_init_stdio(void);

/* PC 端口层额外的两个入口（真板上没有）。
 *
 * _kbhit() 只能告诉我们"管道里有字节"，分不出"读完了"和"对端关了"。
 * EOF 单独记一个标志，主循环据此决定是"继续跑扫频"还是"收尾退出"。 */
void sfra_port_mark_stdin_closed(void);
int  sfra_port_stdin_closed(void);

/* =========================================================================
 * 虚拟时钟
 *
 * 扫频是"每 ISR 一拍"推进的，我们把每拍折算成 1000/isr_freq 微秒
 * 累加到虚拟毫秒时钟上。这样 STATUS 里的时间戳、协议超时判断都能
 * 按真实时间尺度工作，但墙钟时间是 0。
 * ========================================================================= */
static uint64_t g_us = 0;                 /* 累计虚拟微秒 */

static uint32_t now_ms(void)
{
    return (uint32_t)(g_us / 1000u);
}

static void tick_us(uint32_t us)
{
    g_us += us;
}

/* 墙钟微秒（单调）。只用来给"这一轮该补多少拍"定标，
 * 不参与任何协议字段——协议里的时间戳仍然走虚拟时钟 g_us。 */
#ifdef _WIN32
#include <windows.h>
static uint32_t wall_us(void)
{
    static LARGE_INTEGER freq;
    LARGE_INTEGER t;
    if (freq.QuadPart == 0) {
        QueryPerformanceFrequency(&freq);
    }
    QueryPerformanceCounter(&t);
    return (uint32_t)((uint64_t)t.QuadPart * 1000000u /
                      (uint64_t)freq.QuadPart);
}
#else
#include <time.h>
static uint32_t wall_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)((uint32_t)ts.tv_sec * 1000000u +
                      (uint32_t)(ts.tv_nsec / 1000));
}
#endif

/* =========================================================================
 * 被测对象（与 Python 端 plants.py 同构，方便交叉核对）
 * ========================================================================= */

/* 直流偏置注入开关（`--dcbias X`）
 *
 * 存在的理由：PLANT 分支已经把 g_dc 减掉（DFT 只要交流通路），LOOP 分支
 * 没减。两条路"减不减直流"到底影不影响 GH，之前没有任何测试守着，
 * e2e 全绿也不代表物理是对的。
 *
 * 把参考点抬高 `dc_bias` 后，a_c/a_s 与 b_c/b_s 各自会多出 N·dc·Σcosθ
 * 的残差；若 LOOP 的数据通路真的对直流免疫，GH 就不该变。这条断言
 * 由 `test_e2e_loop.py::test_loop_gh_immune_to_dc_bias` 守着。
 * 默认值 0.0 让正常使用与旧行为逐位相同。 */
static float g_dc_bias = 0.0f;

/* 被测对象的直流增益。原来是 main 的局部变量，`advance_sweep()` 拆出去
 * 之后必须提升为文件作用域——PLANT 模式下每拍都要减掉它。 */
static float g_dc = 0.0f;
static int   g_cfg_printed = 0;

#define PLANT_LC   0
#define PLANT_LP   1

typedef struct {
    int   kind;
    /* 二阶 LC：直接型 II 转置 */
    float b0, b1, b2, a1, a2;
    /* 一阶低通 */
    float alpha;
    /* 状态 */
    float x1, x2, y1, y2;
    float lp_y;
} plant_t;

static void plant_lc(plant_t *p, float f0, float q, float fs)
{
    double k   = 2.0 * (double)fs;
    double w0  = 2.0 * 3.14159265358979323846 * (double)f0;
    double den = w0 * w0 + k * w0 / (double)q + k * k;

    p->kind = PLANT_LC;
    p->b0 = (float)(w0 * w0 / den);
    p->b1 = (float)(2.0 * w0 * w0 / den);
    p->b2 = p->b0;
    p->a1 = (float)(2.0 * (w0 * w0 - k * k) / den);
    p->a2 = (float)((w0 * w0 - k * w0 / (double)q + k * k) / den);
    p->x1 = p->x2 = p->y1 = p->y2 = 0.0f;
}

static void plant_lp(plant_t *p, float fc, float fs)
{
    double a = 2.0 * 3.14159265358979323846 * (double)fc / (double)fs;
    p->kind  = PLANT_LP;
    p->alpha = (float)(a / (1.0 + a));      /* 后向欧拉 */
    p->lp_y  = 0.0f;
}

static float plant_step(plant_t *p, float x)
{
    if (p->kind == PLANT_LP) {
        p->lp_y += p->alpha * (x - p->lp_y);
        return p->lp_y;
    }
    {
        float y = p->b0 * x + p->b1 * p->x1 + p->b2 * p->x2
                - p->a1 * p->y1 - p->a2 * p->y2;
        p->x2 = p->x1; p->x1 = x;
        p->y2 = p->y1; p->y1 = y;
        return y;
    }
}

/* 求对象的直流增益：把 x=1 灌进去跑到稳态，稳态输出就是 DC 增益。
 *
 * 为什么需要它：PLANT 模式扫的是 x = 0.5 + inj，这个 0.5 V/0.5 pu 的
 * 直流分量在整周期窗里理论上贡献 0，但有限精度累加会留下
 * N·DC·Σcosθ 的残差——直流越大残差越大。所以喂给 SFRA 的 y 要减掉
 * 它的稳态值，只留交流通路。真实硬件里这一步由 01 文档"去直流"
 * 小节的高通/均值扣除完成，这里是它的 PC 等价物。
 *
 * 注意必须在扫频开始**之前**调用一次，之后 plant 状态被清零，
 * 避免 DC 标定污染扫频的第一个频点。 */
static float plant_dc_gain(plant_t *p)
{
    int   i;
    float y = 0.0f;

    for (i = 0; i < 20000; i++) {
        y = plant_step(p, 1.0f);
    }
    /* 清状态，让扫频从零初值开始 */
    p->x1 = p->x2 = p->y1 = p->y2 = 0.0f;
    p->lp_y = 0.0f;
    return y;
}

/* =========================================================================
 * 最小 FIFO（stdin → 协议层）
 *
 * 为什么要 FIFO 而不是直接 read→sfra_proto_rx：
 *   一次 read 可能只来 1 字节，而 stdio 的 read 会把整个管道内容一次性
 *   吃掉。用 FIFO 解耦"每次 read 拿到多少"与"协议层每次吃多少"，
 *   代码更简单，也更接近真实 UART 的 DMA + ring buffer 模型。
 * ========================================================================= */
#define FIFO_SIZE 4096u
static uint8_t  g_fifo[FIFO_SIZE];
static uint16_t g_fifo_head = 0, g_fifo_tail = 0;

static int fifo_empty(void) { return g_fifo_head == g_fifo_tail; }

static void fifo_push(const uint8_t *d, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++) {
        uint16_t next = (uint16_t)((g_fifo_head + 1u) % FIFO_SIZE);
        if (next == g_fifo_tail) {
            return;                    /* 满了，丢弃多余（真硬件会溢出） */
        }
        g_fifo[g_fifo_head] = d[i];
        g_fifo_head = next;
    }
}

static void fifo_pop(sfra_proto_t *p, uint16_t maxn)
{
    uint16_t n = 0;
    uint8_t  tmp[64];

    while (!fifo_empty() && n < maxn) {
        tmp[n % 64u] = g_fifo[g_fifo_tail];
        g_fifo_tail = (uint16_t)((g_fifo_tail + 1u) % FIFO_SIZE);
        n++;
        if (n % 64u == 0u) {
            sfra_proto_rx(p, tmp, 64u);
            memset(tmp, 0, sizeof(tmp));
        }
    }
    if (n % 64u != 0u) {
        sfra_proto_rx(p, tmp, (uint16_t)(n % 64u));
    }
}

/* 发一帧：**走 port 层**，不直接碰 stdout。
 *
 * 这是本文件最重要的一处"给自己上枷锁"：假 MCU 必须和真固件一样
 * 只通过 `sfra_port_uart_write()` 输出，否则 port 层的声明/语义可以
 * 随便改而不被发现，移植到 MCU 时才第一次真正调用它。
 */
static void write_all(const uint8_t *d, uint16_t n)
{
    sfra_port_uart_write(d, n);
}

/* =========================================================================
 * 补偿器系数（SET_COEFF / GET_COEFF）
 *
 * 假 MCU 也要真存一份，否则 GET_COEFF 只能回显上一次的入参，
 * 那样"读回校验"就退化成了自欺欺人。
 * ========================================================================= */
static float g_coeff[4] = {0.0f, 0.0f, 0.0f, 0.0f};
static uint8_t g_coeff_order = 0u;

int sfra_proto_set_coeff(uint8_t order, const float *b)
{
    uint8_t k;
    if (order < 1u || order > 4u) {
        return -1;
    }
    for (k = 0; k < order; k++) {
        g_coeff[k] = b[k];
    }
    for (k = order; k < 4u; k++) {
        g_coeff[k] = 0.0f;
    }
    g_coeff_order = order;
    fprintf(stderr, "[sim-mcu] SET_COEFF order=%u\n", (unsigned)order);
    return 0;
}

int sfra_proto_get_coeff(float out[4])
{
    uint8_t k;
    if (g_coeff_order == 0u) {
        /* 从没下发过：如实报告"没有"，让 PC 显示"未设置"，
         * 而不是回一串 0 让用户误以为设成了 PI(0,0)。 */
        return -1;
    }
    for (k = 0; k < g_coeff_order; k++) {
        out[k] = g_coeff[k];
    }
    return (int)g_coeff_order;
}

/* =========================================================================
 * 主程序
 * ========================================================================= */

/* -------------------------------------------------------------------------
 * 扫频推进：把"一批 ISR 拍"跑完
 *
 * 真板上这是 TIM7 中断自己的事，主循环根本看不见它。管道上只能由主循环
 * 显式补跑，所以封装成一个函数，主循环每轮调一次，语义等价于"这段时间
 * 里定时器又进了 max_ticks 次中断"。
 *
 * 三行调用的**顺序不能换**（docs/02）：
 *      inj = sfra_inject(&s)          取这一拍的注入值
 *      dut_step(inj)                  送进被测对象
 *      sfra_monitor(&s, x, y)         按当前相位累加 DFT
 *      sfra_advance(&s)              相位往前走
 *      sfra_background_task(&s,&res)  推进状态机（NEXT→下一频点）
 *
 * 退出条件用 `point_idx` 变化而不是 state：SETTLE→COLLECT 发生在同一
 * 频点内，只有 idx 变了才代表"这一频点扫完了，该让协议层喘口气"。
 * 只看 IDLE/DONE 的话，整批 1000 拍会一口气跑完，几十个频点在 PC 第一
 * 次 GET_STATUS 之前就扫光了，后半程数据 PC 永远拿不到。
 * ------------------------------------------------------------------------- */
static void advance_sweep(sfra_t *s, const sfra_result_t *res,
                          plant_t *plant, uint32_t us_per_tick,
                          uint32_t max_ticks)
{
    uint32_t k;
    uint16_t pt0;

    if (max_ticks == 0u) {
        /* 探针模式：只跑一笔，不推进也不推进状态机（调试用）。 */
        return;
    }
    if (s->state == SFRA_ST_IDLE || s->state == SFRA_ST_DONE) {
        return;
    }

    /* 单批之内的"次数"判据：
     *
     * 只看 `point_idx` 变化是不够的——**末点**跑完时 NEXT 直接把状态推到
     * DONE，`point_idx` 停在 vec_length-1 不变，而用
     * `(point_idx+1) >= vec_length` 判"是否已存点"会把最后这一点误判成
     * "还没存"。上位机于是按 point_idx 当可读上界，永远少拿一点
     * （实测 21 点配置只收到 20 点，日志里"DONE at idx=20 vec=21"）。
     *
     * 所以这里用"**DONE 是终态、不会再回退**"这个事实做判据：
     * 只要本批之内要么点索引变了、要么状态已经进了 DONE，就结束本批，
     * 把控制权还给主循环——主循环接着装 STATUS/DATA，上位机才看得到
     * 最后一点。 */
    pt0 = s->point_idx;
    for (k = 0; k < max_ticks; k++) {
        float inj = (float)sfra_inject(s);
        float x, y;

        if (s->cfg.mode == SFRA_MODE_PLANT) {
            /* 开环：注入叠在占空比上，对象真实响应。
             * 减 g_dc 是去掉直流工作点——DFT 只需要交流路径，
             * 直流会在整周期窗口里留下 N·DC·Σcosθ 的残差。 */
            x = 0.5f + g_dc_bias + inj;
            y = plant_step(plant, x) - g_dc;
        } else {
            /* 闭环：把 plant 当成"参考→反馈"的整条通路。LOOP 测的是
             * H·G 的合成响应，对象增益直接体现在 GH 上。 */
            x = inj;
            y = plant_step(plant, x + g_dc_bias);
        }
        sfra_monitor(s, x, y);
        sfra_advance(s);
        sfra_background_task(s, res);
        tick_us(us_per_tick);

        if (s->point_idx != pt0 ||
            s->state == SFRA_ST_IDLE || s->state == SFRA_ST_DONE) {
            break;
        }
    }
}

int main(int argc, char **argv)
{
    static sfra_t        s;
    static sfra_proto_t  proto;
    static float         freq_vect[256], a_c[256], a_s[256], b_c[256], b_s[256];
    static sfra_result_t res;

    sfra_setup_t def;
    plant_t      plant;
    int          plant_kind = PLANT_LC;
    float        isr_freq = 100000.0f;
    /* 被测对象参数。**必须可配**：
     *
     * 这里原来写死 f0=2000 / Q=0.8，于是上位机"被测对象"面板里填的
     * 1300 / 2 完全不生效——界面拿 1300/2 的解析解去比一条 2000/0.8
     * 的曲线，怎么对都对不上。用户看到的现象是"高频平白高了 7 dB、
     * 相位差 60°"，第一反应会怀疑算法或相位约定，实际只是参数没传下去。
     *
     * 现在由上位机启动子进程时按界面值传参，理论与实测才在同一个对象上比。 */
    float        plant_f0 = 1300.0f;
    float        plant_q  = 2.0f;
    float        plant_fc = 1000.0f;
    uint32_t     timeout_ms = SFRA_PROTO_TIMEOUT_MS;
    int          i;
    uint32_t     us_per_tick;
    uint32_t     t_last_loop;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--plant") && i + 1 < argc) {
            const char *v = argv[++i];
            if (!strcmp(v, "lp"))        plant_kind = PLANT_LP;
            else if (!strcmp(v, "lc"))   plant_kind = PLANT_LC;
            else {
                fprintf(stderr, "unknown --plant %s\n", v);
                return 2;
            }
        } else if (!strcmp(argv[i], "--isr") && i + 1 < argc) {
            isr_freq = (float)atof(argv[++i]);
        } else if (!strcmp(argv[i], "--f0") && i + 1 < argc) {
            plant_f0 = (float)atof(argv[++i]);
        } else if (!strcmp(argv[i], "--q") && i + 1 < argc) {
            plant_q = (float)atof(argv[++i]);
        } else if (!strcmp(argv[i], "--fc") && i + 1 < argc) {
            plant_fc = (float)atof(argv[++i]);
        } else if (!strcmp(argv[i], "--timeout") && i + 1 < argc) {
            timeout_ms = (uint32_t)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--dcbias") && i + 1 < argc) {
            g_dc_bias = (float)atof(argv[++i]);
        } else {
            fprintf(stderr,
                    "usage: %s [--plant lc|lp] [--isr HZ] [--f0 HZ] [--q Q]"
                    " [--fc HZ] [--timeout MS] [--dcbias X]\n", argv[0]);
            return 2;
        }
    }

    /* Windows 上 stdout 默认文本模式，0x0A 会被翻译成 0x0D 0x0A，二进制帧
     * 就毁了（协议里 float 低位出现 0x0A 的概率很高，不设大约每 3 帧错 1 帧）。
     * 这件事归 port 层管——移植到 MCU 时这行连同函数一起消失。 */
    sfra_port_init_stdio();

    /* ---- 初始化 ---- */
    sfra_init_all();

    memset(&def, 0, sizeof(def));
    def.isr_freq    = isr_freq;
    def.freq_start  = 100.0f;
    def.freq_step   = 1.1220184543019633f;      /* 20 pts/dec */
    def.amplitude   = 0.01f;
    def.vec_length  = 68u;
    def.cycles_mode = SFRA_CYC_NORMAL;
    def.mode        = SFRA_MODE_PLANT;

    res.freq_vect = freq_vect;
    res.a_c = a_c;  res.a_s = a_s;
    res.b_c = b_c;  res.b_s = b_s;

    sfra_init(&s, &def, &res);
    sfra_proto_init(&proto, &def);
    proto.cfg = def;

    if (plant_kind == PLANT_LP) {
        plant_lp(&plant, plant_fc, isr_freq);
    } else {
        plant_lc(&plant, plant_f0, plant_q, isr_freq);
    }
    /* DC 增益在扫频前后必须一致，且 plant 状态要清零。
     * 这一步在"START"之后做更准（因为扫频起点随配置变），
     * 所以 demo 里在收到 START 时重算——见下面的 g_need_dc 逻辑。 */
    g_dc = plant_dc_gain(&plant);

    us_per_tick = (uint32_t)(1000000.0 / (double)isr_freq + 0.5);
    if (us_per_tick == 0u) {
        us_per_tick = 1u;
    }

    fprintf(stderr,
            "[sim-mcu] ready plant=%s isr=%.0fHz timeout=%ums "
            "fixed_point=%d sin_bits=%d\n",
            plant_kind == PLANT_LP ? "lp" : "lc",
            (double)isr_freq, timeout_ms,
            SFRA_USE_FIXED_POINT, SFRA_SIN_TAB_BITS);

    /* ---- 主循环 ----
     *
     * 结构与真板主循环**刻意保持一致**：
     *
     *     while (1) {
     *         sfra_background_task();              // 状态机推进
     *         收字节 → 分流 → sfra_app_poll();     // 协议
     *     }
     *
     * 唯一的不同是"中断"由谁驱动。真板上 TIM7 自己按 isr_freq 进中断，
     * 主循环在干什么都不影响；管道上只能由主循环代跑，所以每轮循环按
     * **这一轮实际耗掉的墙钟时间**折算成 ISR 拍数补跑（见下面 t_loop）。
     * 这样虚拟时间与墙钟对齐，上位机 200 ms 问一次 STATUS，固件也就
     * 只走 200 ms 的电平，两侧时间轴一致。
     * --------------------------------------------------------------- */
    t_last_loop = wall_us();
    for (;;) {
        uint8_t buf[1];
        size_t  n;
        int     guard;
        int     ch;
        uint32_t t_loop;

        /* 1) **先**补跑这段时间的中断，再去等字节。
         *
         * 拍数 = 本轮实际耗掉的墙钟时间 / 一个 ISR 周期。定死批大小是
         * 不行的：管道路径一轮循环只花几十微秒，固定 1000 拍等于虚拟
         * 时间比墙钟快几百倍，整轮扫频在上位机第一次取数之前就跑完了
         * （实测 10 kHz 档固件冲到第 16 点被 PC 打断，此后不再推进）。
         * 按墙钟折算之后，"固件扫多久"与"PC 问了多久"是同一根时间轴，
         * 与真板 TIM7 自走、PC 定期来问的形态一致。
         * `advance_sweep` 内部还有"点索引变了就 break"，所以每个频点
         * 边界都会回到主循环，不会一次吞掉好几个点。 */
        t_loop = wall_us();
        {
            uint32_t el = (uint32_t)(t_loop - t_last_loop);
            uint32_t ticks_due;
            if (el < 100u) { el = 100u; }          /* 起步/抖动兜底 */
            ticks_due = el / (us_per_tick ? us_per_tick : 1u);
            if (ticks_due > 20000u) { ticks_due = 20000u; }
            advance_sweep(&s, &res, &plant, us_per_tick, ticks_due);
        }
        t_last_loop = t_loop;
        if (getenv("SFRA_SIM_CFG") && !g_cfg_printed && s.state != SFRA_ST_IDLE) {
            g_cfg_printed = 1;
            fprintf(stderr, "[cfg] vec=%u isr=%.0f\n",
                    (unsigned)s.cfg.vec_length, (double)s.cfg.isr_freq);
            for (unsigned q = 0; q < s.cfg.vec_length; q++) {
                fprintf(stderr, "   f[%u]=%.3f\n", q,
                        (double)sfra_freq_at(&s.cfg, (uint16_t)q));
            }
        }
        if (getenv("SFRA_SIM_VERBOSE")) {
            fprintf(stderr, "[sim] after advance: idx=%u st=%u vec=%u\n",
                    (unsigned)s.point_idx, (unsigned)s.state,
                    (unsigned)s.cfg.vec_length);
        }
        /* 2) 收字节（阻塞，一次一个）。
         *    必须单字节：管道的 fread(256) 会阻塞到凑满 256 字节，
         *    上位机发完 6 字节 PING 就在等应答，直接死锁。
         *    只在**扫频已经跑完**时才允许一直阻塞：那时没有需要推进的
         *    中断，等多久都无所谓；扫频中则用带超时的读，见 port 层。 */
        ch = sfra_port_uart_read_byte();
        if (ch < 0) {
            continue;                    /* 没有字节：回到第 1 步继续补拍 */
        }
        buf[0] = (uint8_t)ch;
        n = 1u;
        fifo_push(buf, n);

        /* 3) 协议：每轮最多吃 512 字节、最多装一帧应答。
         *    **装到一帧就 break**：协议层一次只装一帧（tx_len != 0 就
         *    return），继续循环只会空转。 */
        for (guard = 0; guard < 8; guard++) {
            uint16_t had = proto.tx_len;
            fifo_pop(&proto, 512u);
            if (had != 0u) {
                write_all(proto.tx_buf, had);
                sfra_proto_tx_done(&proto);
            }
            sfra_proto_poll(&proto, &s, &res, now_ms());
            if (proto.tx_len != 0u) {
                write_all(proto.tx_buf, proto.tx_len);
                sfra_proto_tx_done(&proto);
                break;
            }
            if (fifo_empty()) {
                break;
            }
        }
    }

    return 0;
}
