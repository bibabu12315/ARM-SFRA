/* ---------------------------------------------------------------------------
 * sfra_proto.c - 串口协议实现（基线：docs/03_通信协议.md）
 *
 * 三条实现约束，都是踩过坑的：
 *
 * 1. **CRC 只算 LEN + CMD + payload，不含 AA 55**。
 *    含不含帧头在某些数据下都能碰巧对上，出问题时表现为"偶发误判错帧"，
 *    极难定位。测试用 `crc16(b"123456789")==0x4B37` 锁死算法。
 *
 * 2. **payload 逐字段拼，不 memcpy 结构体**（D20）。
 *    gcc 的 `struct { uint8_t a; uint16_t b; }` 会占 4 字节（b 前插 1 字节
 *    填充），而协议表是紧凑排布。一旦 memcpy，PC 解出来的字段全是垃圾。
 *
 * 3. **poll 一次最多处理一帧**。扫频期间主循环还要跑状态机和 UART 发送，
 *    如果一次把 ring buffer 里的帧全处理完，高频 GET_STATUS 会饿死别的任务。
 * ------------------------------------------------------------------------- */
#include "sfra_proto.h"

#include <string.h>

/* =========================================================================
 * CRC16-Modbus
 *
 * 逐位算而不是查表：省 512 B flash，代价是每字节 8 次循环。
 * 一帧最长 260 字节 × 8 = 2080 次迭代，在 168MHz 上约 60 µs——
 * 主循环里处理一帧可接受。（若嫌慢，把表改成 const 放 flash。）
 * ========================================================================= */
static uint16_t crc16_modbus(const uint8_t *d, uint16_t n)
{
    uint16_t c = 0xFFFFu;
    uint16_t i;
    uint8_t  b;

    for (i = 0; i < n; i++) {
        c ^= (uint16_t)d[i];
        for (b = 0; b < 8u; b++) {
            c = (c & 1u) ? (uint16_t)((c >> 1) ^ 0xA001u) : (uint16_t)(c >> 1);
        }
    }
    return c;
}

/* =========================================================================
 * 小端序列化（不依赖主机字节序，也不依赖对齐）
 *
 * 为什么不用 `*(uint32_t *)p = v`：
 *   M0+ 不允许非对齐访问（HardFault），而 payload 里的 f32 可能落在奇数地址。
 * 为什么不用 union：
 *   strict-aliasing 下行为未定义，-O2 可能被优化掉。
 * ========================================================================= */
static void put_u8(uint8_t *p, uint8_t v)   { p[0] = v; }
static void put_u16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put_f32(uint8_t *p, float v)
{
    uint32_t u;
    memcpy(&u, &v, 4);            /* 拷贝 float 的位模式是合法的 */
    p[0] = (uint8_t)u;         p[1] = (uint8_t)(u >> 8);
    p[2] = (uint8_t)(u >> 16); p[3] = (uint8_t)(u >> 24);
}

static uint8_t  get_u8(const uint8_t *p)  { return p[0]; }
static uint16_t get_u16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static float    get_f32(const uint8_t *p)
{
    uint32_t u = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                 ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    float v;
    memcpy(&v, &u, 4);
    return v;
}

/* =========================================================================
 * 组帧
 * ========================================================================= */

/* 开始一帧，返回 **payload** 的写入起点。
 *
 * 帧布局固定为：
 *     [0]=AA [1]=55 [2]=LEN [3]=CMD [4..]=payload [..]=CRC_lo CRC_hi
 * 所以 CMD 在 tx_buf[3]，payload 起点是 tx_buf[4]。
 * `SFRA_PROTO_HDR`=3 表示"帧头 3 字节（SOF+LEN）"，CMD 属于 body——
 * 返回 4 是因为要跳过 CMD 写 payload，两者不是一回事，别混。 */
static uint8_t *frame_begin(sfra_proto_t *p, uint8_t cmd)
{
    p->tx_buf[0] = SFRA_SOF0;
    p->tx_buf[1] = SFRA_SOF1;
    p->tx_buf[2] = 0u;                       /* LEN，frame_end 里填 */
    p->tx_buf[3] = cmd;
    return &p->tx_buf[SFRA_PROTO_HDR + 1u];  /* 4：CMD 之后 */
}

/* 收尾：payload_len 是已写入的字节数 */
static void frame_end(sfra_proto_t *p, uint16_t payload_len)
{
    uint16_t len = (uint16_t)(1u + payload_len);   /* CMD + payload */
    uint16_t crc;

    p->tx_buf[2] = (uint8_t)len;
    /* CRC 覆盖 LEN..payload 末，即 **1 + len** 字节：
     * LEN 自己占 1 字节，再加上 len 字节的 CMD+payload。
     * 这里曾经只传 len 而漏掉 LEN 字节，导致 PC 端算不出同样的值
     * ——当场表现是"所有应答都被当 CRC 错丢掉"。 */
    crc = crc16_modbus(&p->tx_buf[2], (uint16_t)(1u + len));
    /* CRC 写在帧尾：偏移 = 1(LEN) + 1(CMD) + payload_len + 2(SOF)
     *                  = SFRA_PROTO_HDR + len */
    p->tx_buf[SFRA_PROTO_HDR + len]      = (uint8_t)crc;
    p->tx_buf[SFRA_PROTO_HDR + len + 1u] = (uint8_t)(crc >> 8);
    p->tx_len = (uint16_t)(SFRA_PROTO_HDR + len + 2u);
}

/* ACK: cmd(u8) err(u8) [detail(u8)，仅 err!=0 时] */
static void send_ack(sfra_proto_t *p, uint8_t cmd, uint8_t err, uint8_t detail)
{
    uint8_t *q = frame_begin(p, SFRA_CMD_ACK);
    put_u8(q + 0, cmd);
    put_u8(q + 1, err);
    if (err != SFRA_ERR_OK) {
        put_u8(q + 2, detail);
        frame_end(p, 3u);
    } else {
        frame_end(p, 2u);
    }
}

/* =========================================================================
 * 弱符号：用户不实现也能编译
 *
 * **原来是 `#if defined(__GNUC__) ... #else #define SFRA_WEAK (空) #endif`，
 * 在 Keil/ARMCC 下等于"什么都不做"**——三个回调于是在 `sfra_proto.o`
 * 里成了**强定义**，与用户在 `sfra_port.c` 里的实现撞车，链接直接报
 *
 *     L6200E: Symbol sfra_proto_set_coeff multiply defined
 *            (by sfra_port.o and sfra_proto.o)
 *
 * 三种编译器都必须覆盖到：
 *   · GCC / Clang（PC 假 MCU、arm-none-eabi） → __attribute__((weak))
 *   · Keil ARMCC V5（__CC_ARM）              → `__weak` 关键字
 *   · ARMCLANG（V6，定义了 __GNUC__）         → 走第一条即可
 * ========================================================================= */
#if defined(__CC_ARM)
/* ARMCC V5：用关键字。实测 `__attribute__((weak))` 在这个版本上也能过，
 * 但官方写法是关键字，且更少踩优化器的坑。 */
#define SFRA_WEAK __weak
#elif defined(__GNUC__) || defined(__clang__)
#define SFRA_WEAK __attribute__((weak))
#else
#define SFRA_WEAK
#endif

SFRA_WEAK int sfra_proto_get_conditions(float out[8])
{
    uint8_t i;
    for (i = 0; i < 8u; i++) {
        out[i] = 0.0f;
    }
    return 0;
}

SFRA_WEAK int sfra_proto_set_coeff(uint8_t order, const float *b)
{
    (void)order; (void)b;
    return -1;                        /* 默认不支持 */
}

SFRA_WEAK int sfra_proto_get_coeff(float out[4])
{
    (void)out;
    return -1;                        /* 默认不支持 */
}

/* =========================================================================
 * 生命周期
 * ========================================================================= */

void sfra_proto_init(sfra_proto_t *p, const sfra_setup_t *default_cfg)
{
    if (!p) {
        return;
    }
    memset(p, 0, sizeof(*p));
    if (default_cfg) {
        p->cfg = *default_cfg;
    }
}

void sfra_proto_rx(sfra_proto_t *p, const uint8_t *data, uint16_t n)
{
    uint16_t room;

    if (!p || !data || n == 0u) {
        return;
    }
    room = (uint16_t)(sizeof(p->rx_buf) - p->rx_len);
    if (n > room) {
        /* 缓冲满：丢最旧的，保住新数据。
         * 丢旧的而不是新的——协议有 CRC 与帧同步，丢旧数据最多丢一帧，
         * 而丢新数据会让"刚发的命令"永远等不到应答。 */
        uint16_t drop = (uint16_t)(p->rx_len + n - sizeof(p->rx_buf));
        if (drop >= p->rx_len) {
            p->rx_len = 0u;
        } else {
            memmove(p->rx_buf, p->rx_buf + drop,
                    (size_t)(p->rx_len - drop));
            p->rx_len = (uint16_t)(p->rx_len - drop);
        }
        room = (uint16_t)(sizeof(p->rx_buf) - p->rx_len);
        if (n > room) {
            n = room;
        }
    }
    memcpy(p->rx_buf + p->rx_len, data, n);
    p->rx_len = (uint16_t)(p->rx_len + n);
}

void sfra_proto_tx_done(sfra_proto_t *p)
{
    if (p) {
        p->tx_len = 0u;
    }
}

/* =========================================================================
 * 命令处理
 * ========================================================================= */

static void do_ping(sfra_proto_t *p)
{
    /* PING_ACK: ver(1) proto(1) cap(u16) */
    uint8_t *q = frame_begin(p, SFRA_CMD_PING_ACK);
    uint16_t caps = 0u;

#if SFRA_USE_FIXED_POINT
    caps |= SFRA_CAP_FIXED;
#else
    caps |= SFRA_CAP_FLOAT;
#endif
    caps |= SFRA_CAP_READBACK;

    put_u8 (q + 0, (uint8_t)SFRA_VERSION_MAJOR);
    put_u8 (q + 1, (uint8_t)SFRA_PROTO_VERSION);
    put_u16(q + 2, caps);
    frame_end(p, 4u);
}

static void do_get_caps(sfra_proto_t *p)
{
    /* CAPS: isr_freq(f32) max_pts_per_dec(u8) reserved(u8)
     *       max_vec_len(u16) default_amp(f32) fw_ver(4B)  = 16 B */
    uint8_t *q = frame_begin(p, SFRA_CMD_CAPS);
    put_f32(q + 0,  p->cfg.isr_freq);
    put_u8 (q + 4,  20u);                 /* max_pts_per_dec */
    put_u8 (q + 5,  0u);                  /* reserved */
    put_u16(q + 6,  256u);                /* max_vec_len */
    put_f32(q + 8,  0.01f);               /* default_amp (pu) */
    put_u8 (q + 12, (uint8_t)SFRA_VERSION_MAJOR);
    put_u8 (q + 13, (uint8_t)SFRA_VERSION_MINOR);
    put_u8 (q + 14, (uint8_t)SFRA_PROTO_VERSION);
    put_u8 (q + 15, 0u);
    frame_end(p, 16u);
}

static void do_set_config(sfra_proto_t *p, sfra_t *s,
                          const uint8_t *pl, uint16_t n)
{
    float    f_start, f_step, amp;
    uint16_t nvec;
    uint8_t  cyc, mode;

    if (n < 17u) {
        send_ack(p, SFRA_CMD_SET_CONFIG, SFRA_ERR_BAD_PARAM, 0u);
        return;
    }
    f_start = get_f32(pl + 0);
    f_step  = get_f32(pl + 4);
    amp     = get_f32(pl + 8);
    nvec    = get_u16(pl + 12);
    cyc     = get_u8 (pl + 14);
    mode    = get_u8 (pl + 15);

    /* 参数校验。上限 f_s/5 是 01 文档 1.10 的可测边界，
     * 在这里挡掉比让用户扫出错误数据再解释要好。 */
    if (!(f_start > 0.0f) || !(f_step > 1.0f) || nvec == 0u ||
        nvec > 256u || cyc > SFRA_CYC_PRECISE || mode > SFRA_MODE_LOOP) {
        send_ack(p, SFRA_CMD_SET_CONFIG, SFRA_ERR_BAD_PARAM, 1u);
        return;
    }
    if (!(amp > 0.0f) || amp > 0.25f) {
        send_ack(p, SFRA_CMD_SET_CONFIG, SFRA_ERR_BAD_PARAM, 2u);
        return;
    }
    if (sfra_is_running(s)) {
        /* 扫频中改配置会毁掉数据一致性（D8） */
        send_ack(p, SFRA_CMD_SET_CONFIG, SFRA_ERR_STATE, 0u);
        return;
    }

    /* isr_freq 是硬件事实，由初始化给定，PC 不能改 */
    p->cfg.isr_freq    = s->cfg.isr_freq;
    p->cfg.freq_start  = f_start;
    p->cfg.freq_step   = f_step;
    p->cfg.amplitude   = amp;
    p->cfg.vec_length  = nvec;
    p->cfg.cycles_mode = cyc;
    p->cfg.mode        = mode;
    p->configured      = 1u;

    send_ack(p, SFRA_CMD_SET_CONFIG, SFRA_ERR_OK, 0u);
}

static void do_start(sfra_proto_t *p, sfra_t *s)
{
    int r;

    if (!p->configured) {
        send_ack(p, SFRA_CMD_START, SFRA_ERR_NOT_CFG, 0u);
        return;
    }
    if (sfra_is_running(s)) {
        send_ack(p, SFRA_CMD_START, SFRA_ERR_BUSY, 0u);
        return;
    }

    /* **配置在这里生效**：SET_CONFIG 只写 p->cfg（协议上下文），
     * sfra_t 里那份 s->cfg 是硬件初始化时给的三百六十度默认值。
     * 漏了这一句的表现是"SET_CONFIG 明明 ACK OK，扫出来还是默认
     * 100Hz 起、68 点"——e2e 第一版就是这么错的。
     * 放在 START 而不是 SET_CONFIG 里是为了让"配置"和"启动"两件事
     * 各自幂等：连续发两次 SET_CONFIG 不会留下中间态。 */
    s->cfg = p->cfg;
    r = sfra_start(s);
    if (r != 0) {
        send_ack(p, SFRA_CMD_START, SFRA_ERR_BAD_PARAM, (uint8_t)(-r));
        return;
    }
    send_ack(p, SFRA_CMD_START, SFRA_ERR_OK, 0u);
}

static void do_get_status(sfra_proto_t *p, sfra_t *s)
{
    /* STATUS: state(u8) flags(u8) freq_index(u16) pass(u8) reserved(u8)
     *         cur_f_act(f32)                                  = 10 B */
    uint8_t  *q  = frame_begin(p, SFRA_CMD_STATUS);
    uint8_t   st = sfra_state(s);
    uint8_t   stx;
    uint8_t   flags = 0u;
    float     f_act = 0.0f;

    switch (st) {
    case SFRA_ST_IDLE:
        stx = SFRA_ST_IDLE_G;    flags = 0x04u; break;          /* 注入已关 */
    case SFRA_ST_SETTLE:
        stx = SFRA_ST_SETTLE_G;  break;
    case SFRA_ST_COLLECT:
        stx = SFRA_ST_COLLECT_G; break;
    case SFRA_ST_NEXT:
        stx = SFRA_ST_NEXT_G;    break;
    default:                                                     /* ST_DONE */
        stx = SFRA_ST_DONE_G;    flags = 0x04u | 0x08u; break;  /* inj off + done */
    }

    /* 当前频点的实际频率（D7：由固件算好上报，PC 不反推）。
     * IDLE 时没有有效频点，报 0。 */
    if (st != SFRA_ST_IDLE && s->point_idx < s->cfg.vec_length) {
        uint32_t cyc = sfra_calc_cycles(sfra_freq_at(&s->cfg, s->point_idx),
                                        s->cfg.cycles_mode, s->cfg.isr_freq);
        f_act = sfra_actual_freq(s->cfg.isr_freq, s->data_count, cyc);
    }

    put_u8 (q + 0, stx);
    put_u8 (q + 1, flags);
    /* freq_index 的语义：**已完成并写进结果数组的点数**。
     *
     * 非 DONE 时它就是"正在采集的索引"，与"已完成数"恰好相等（索引 i
     * 正在采，说明 0..i-1 已经存好）。
     * 但 DONE 时必须报 `vec_length`：末点跑完时 NEXT 直接把状态推到
     * DONE，`point_idx` 停在 `vec_length-1` 不动，若照实上报，上位机
     * 会把它当成"只完成了 vec_length-1 点"，最后一个点永远取不到
     * （实测 21 点配置只回 20 点，日志 "DONE at idx=20 vec=21"）。
     * 这一步与 get_data 的 `avail` 口径**逐字一致**，两侧不会再错位。 */
    put_u16(q + 2, (uint16_t)((st == SFRA_ST_DONE) ? s->cfg.vec_length
                                                   : s->point_idx));
    put_u8 (q + 4, 0u);        /* pass：D19 单 pass 后恒 0，字段保留 */
    put_u8 (q + 5, 0u);        /* reserved */
    put_f32(q + 6, f_act);
    frame_end(p, 10u);
}

static void do_get_data(sfra_proto_t *p, sfra_t *s, const sfra_result_t *res,
                        const uint8_t *pl, uint16_t n)
{
    /* 请求: start_idx(u16) count(u8) */
    /* 应答: start_idx(u16) count(u8) then count×{f,a_c,a_s,b_c,b_s} */
    uint16_t start;
    uint8_t  cnt, k, avail;
    uint8_t *q;

    if (n < 3u || !res || !res->freq_vect) {
        send_ack(p, SFRA_CMD_GET_DATA, SFRA_ERR_BAD_PARAM, 0u);
        return;
    }
    /* payload 布局：start_idx(u16) + count(u8) —— count 在偏移 2。
     * 写 3 会读到 payload 之外（下一个字节是 CRC），cnt 变成随机大数，
     * 下面的 cnt>7 裁剪虽然能挡住越界，但截图永远是"第一个 chunk 就错"。 */
    start = get_u16(pl + 0);
    cnt   = get_u8 (pl + 2);

    /* 可取的"已完成"点数：
     *   已写进 res 的点数是 point_idx（NEXT 时才 store），
     *   所以 idx < point_idx 才是有效数据。DONE 时 point_idx 停在末点，
     *   末点还差一次 NEXT 才 store——用 state 判断更稳。 */
    avail = (uint8_t)((sfra_state(s) == SFRA_ST_DONE)
                      ? s->cfg.vec_length
                      : s->point_idx);

    if (start >= avail) {
        send_ack(p, SFRA_CMD_GET_DATA, SFRA_ERR_INDEX, 0u);
        return;
    }
    if (cnt == 0u) {
        cnt = 1u;
    }
    if ((uint16_t)(start + cnt) > avail) {
        cnt = (uint8_t)(avail - start);
    }
    if (cnt > 7u) {
        cnt = 7u;                  /* 一帧最多 7 点（143 B） */
    }

    q = frame_begin(p, SFRA_CMD_DATA);
    put_u16(q + 0, start);
    put_u8 (q + 2, cnt);
    /* 首点从 payload 偏移 3 开始（start_idx 2B + count 1B）。
     * 这里写 4 会整体右移 1 字节，最后一字节溢出 tx_buf，
     * 并且 PC 端按 "<HB"+N*20 解就会把 f_act 的低字节吃掉。 */
    for (k = 0; k < cnt; k++) {
        uint8_t *e = q + 3u + (uint16_t)k * 20u;
        put_f32(e + 0,  res->freq_vect[start + k]);
        put_f32(e + 4,  res->a_c[start + k]);
        put_f32(e + 8,  res->a_s[start + k]);
        put_f32(e + 12, res->b_c[start + k]);
        put_f32(e + 16, res->b_s[start + k]);
    }
    frame_end(p, (uint16_t)(3u + (uint16_t)cnt * 20u));
}

static void do_set_coeff(sfra_proto_t *p, const uint8_t *pl, uint16_t n)
{
    uint8_t order;
    float   b[4];
    uint8_t k;

    if (n < 1u) {
        send_ack(p, SFRA_CMD_SET_COEFF, SFRA_ERR_BAD_PARAM, 0u);
        return;
    }
    order = get_u8(pl + 0);
    if (order < 1u || order > 4u || n < (uint16_t)(1u + order * 4u)) {
        send_ack(p, SFRA_CMD_SET_COEFF, SFRA_ERR_BAD_PARAM, 0u);
        return;
    }
    for (k = 0; k < order; k++) {
        b[k] = get_f32(pl + 1u + k * 4u);
    }
    if (sfra_proto_set_coeff(order, b) != 0) {
        send_ack(p, SFRA_CMD_SET_COEFF, SFRA_ERR_BAD_PARAM, 1u);
        return;
    }
    /* 回读校验：COEFF 帧原样带回，让 PC 确认写入的是什么 */
    {
        uint8_t *q = frame_begin(p, SFRA_CMD_COEFF);
        put_u8(q + 0, order);
        for (k = 0; k < order; k++) {
            put_f32(q + 1u + k * 4u, b[k]);
        }
        frame_end(p, (uint16_t)(1u + order * 4u));
    }
}

static void do_get_coeff(sfra_proto_t *p)
{
    float   b[4];
    int     order;
    uint8_t k;
    uint8_t *q;

    /* 读的是"当前生效"的系数，不是上一次 SET_COEFF 的入参 */
    order = sfra_proto_get_coeff(b);
    if (order <= 0) {
        send_ack(p, SFRA_CMD_GET_COEFF, SFRA_ERR_BAD_PARAM, 1u);
        return;
    }
    if (order > 4) {
        order = 4;
    }
    q = frame_begin(p, SFRA_CMD_COEFF);
    put_u8(q + 0, (uint8_t)order);
    for (k = 0; k < (uint8_t)order; k++) {
        put_f32(q + 1u + (uint16_t)k * 4u, b[k]);
    }
    frame_end(p, (uint16_t)(1u + (uint16_t)order * 4u));
}

static void do_get_conditions(sfra_proto_t *p)
{
    float c[8];
    uint8_t *q;
    uint8_t k;

    (void)sfra_proto_get_conditions(c);
    q = frame_begin(p, SFRA_CMD_CONDITIONS);
    for (k = 0; k < 8u; k++) {
        put_f32(q + (uint16_t)k * 4u, c[k]);
    }
    frame_end(p, 32u);
}

/* =========================================================================
 * 收帧：找帧头 → 校验长 → 校验 CRC
 * ========================================================================= */

/* 返回值：
 *   >0  : 处理了一帧，返回消耗的字节数
 *    0  : 数据不够，等下次
 *   -1  : 丢了一字节（假帧头 / CRC 错），调用方继续试
 */
static int take_frame(sfra_proto_t *p, sfra_t *s, const sfra_result_t *res)
{
    uint16_t len, total, crc_rx, crc_calc;
    uint8_t  cmd;
    const uint8_t *body;

    /* 帧头定位。用逐字节比较而不是 memcmp——M0+ 上没有字节块比较指令，
     * 但这段最多扫 260 字节，开销可忽略，换来的是不用引入 string.h 的
     * memmem（C99 里也没有）。 */
    while (p->rx_len >= 2u) {
        if (p->rx_buf[0] == SFRA_SOF0 && p->rx_buf[1] == SFRA_SOF1) {
            break;
        }
        /* 只前进一字节：AA AA 55 这种序列里，第二个 AA 才是真头 */
        memmove(p->rx_buf, p->rx_buf + 1, (size_t)(p->rx_len - 1u));
        p->rx_len--;
    }
    if (p->rx_len < SFRA_PROTO_HDR) {
        return 0;
    }

    len = p->rx_buf[2];
    if (len < 1u) {
        memmove(p->rx_buf, p->rx_buf + 1, (size_t)(p->rx_len - 1u));
        p->rx_len--;
        return -1;
    }
    /* 整帧 = SOF(2) + LEN(1) + len + CRC(2) = HDR + len + 2 */
    total = (uint16_t)(SFRA_PROTO_HDR + len + 2u);
    if (p->rx_len < total) {
        return 0;                       /* 半帧，等后续字节 */
    }

    body     = &p->rx_buf[2];           /* 从 LEN 开始参与 CRC */
    crc_calc = crc16_modbus(body, (uint16_t)(1u + len));   /* LEN + CMD + payload */
    /* CRC 在帧尾：CRC 起点 = SOF(2) + LEN(1) + len = 3 + len
     *                = SFRA_PROTO_HDR + 1 + len（HDR 只算 SOF+LEN） */
    /* CRC 起点 = 1(SOF 后) + 2(SOF) ... 直接写清楚：
     *   偏移 0,1 = AA 55
     *   偏移 2   = LEN
     *   偏移 3   = CMD
     *   偏移 4..(3+len) = payload
     *   偏移 3+len, 4+len = CRC_lo, CRC_hi
     * 注意是 **3 + len** 而不是 HDR + len：HDR(=3) 只覆盖 SOF+LEN，
     * 而 len 已经含 CMD，两者相加会正好落在 CRC 上——但下面对
     * `body` 用的是 &rx_buf[2]（LEN 起点），长度 1+len。 */
    crc_rx   = (uint16_t)(p->rx_buf[3u + len] |
                          ((uint16_t)p->rx_buf[4u + len] << 8));
    if (crc_calc != crc_rx) {
        p->crc_errors++;
        memmove(p->rx_buf, p->rx_buf + 1, (size_t)(p->rx_len - 1u));
        p->rx_len--;
        return -1;
    }

    /* ---- CRC 通过，执行命令 ---- */
    cmd = p->rx_buf[3];
    p->frame_count++;

    switch (cmd) {
    case SFRA_CMD_PING:
        do_ping(p);
        break;
    case SFRA_CMD_GET_CAPS:
        do_get_caps(p);
        break;
    case SFRA_CMD_SET_CONFIG:
        do_set_config(p, s, &p->rx_buf[SFRA_PROTO_HDR + 1u], (uint16_t)(len - 1u));
        break;
    case SFRA_CMD_START:
        do_start(p, s);
        break;
    case SFRA_CMD_ABORT:
        sfra_abort(s);
        send_ack(p, SFRA_CMD_ABORT, SFRA_ERR_OK, 0u);
        /* 注：abort 与 scan 的收尾由调用方感知 state==IDLE 完成 */
        break;
    case SFRA_CMD_GET_STATUS:
        do_get_status(p, s);
        break;
    case SFRA_CMD_GET_DATA:
        do_get_data(p, s, res, &p->rx_buf[SFRA_PROTO_HDR + 1u], (uint16_t)(len - 1u));
        break;
    case SFRA_CMD_SET_COEFF:
        do_set_coeff(p, &p->rx_buf[SFRA_PROTO_HDR + 1u], (uint16_t)(len - 1u));
        break;
    case SFRA_CMD_GET_COEFF:
        do_get_coeff(p);
        break;
    case SFRA_CMD_GET_CONDITIONS:
        do_get_conditions(p);
        break;
    default:
        /* 未知命令：明确回 NACK，不要静默丢弃。
         * 静默丢弃会让 PC 一直重试到超时，用户看到的是"连接不稳"。 */
        send_ack(p, cmd, SFRA_ERR_BAD_PARAM, 0xFFu);
        break;
    }

    memmove(p->rx_buf, p->rx_buf + total, (size_t)(p->rx_len - total));
    p->rx_len = (uint16_t)(p->rx_len - total);
    return (int)total;
}

/* =========================================================================
 * 主循环入口
 * ========================================================================= */

void sfra_proto_poll(sfra_proto_t *p, sfra_t *s, const sfra_result_t *res,
                     uint32_t now_ms)
{
    int r;

    if (!p || !s) {
        return;
    }

    /* 发送缓冲还没被取走：等。不覆盖，否则会发出半帧。 */
    if (p->tx_len != 0u) {
        return;
    }

    /* 一次只处理一帧，然后返回，让主循环轮到别的任务 */
    r = take_frame(p, s, res);
    if (r > 0 && now_ms != 0u) {
        p->last_rx_ms = now_ms;
    }

    /* 通信超时保护：超过阈值没有有效帧就自动 abort。
     * 这是安全护栏（03 文档 §3.4），不能依赖上位机主动发 ABORT——
     * 上位机崩了、线断了，注入必须自己停。 */
    if (now_ms != 0u && p->last_tick_ms != 0u &&
        sfra_is_running(s) &&
        (uint32_t)(now_ms - p->last_rx_ms) > SFRA_PROTO_TIMEOUT_MS) {
        sfra_abort(s);
    }
    p->last_tick_ms = now_ms;
}
