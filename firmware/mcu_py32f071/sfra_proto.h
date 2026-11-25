/* ---------------------------------------------------------------------------
 * sfra_proto.h - SFRA 串口协议（**唯一基线：docs/03_通信协议.md**）
 *
 * 设计原则：
 *   1. 零动态内存、零阻塞。`sfra_proto_poll()` 每次处理有限字节就返回，
 *      可以直接在 main 循环里调。
 *   2. **不 memcpy 结构体**（D20）。payload 逐字段按小端拼，字节数有测试守。
 *   3. 不依赖任何 HAL：接收靠用户把字节塞进 `sfra_proto_rx()`，
 *      发送靠用户在 `sfra_proto_poll()` 里把 `tx_buf` 取走。
 *
 * 用法（main 循环里）：
 *
 *     while (1) {
 *         // UART RX 中断里调 sfra_proto_rx(p, n) 塞字节
 *         sfra_proto_poll(&proto, &sfra, &res);
 *         while (proto.tx_len) {            // 把待发数据倒出去
 *             uart_write(proto.tx_buf, proto.tx_len);
 *             sfra_proto_tx_done(&proto);
 *         }
 *     }
 *
 * 帧格式：
 *
 *     AA 55 | LEN(1B) | CMD | payload | CRC16_lo CRC16_hi
 *            └──────── CRC 覆盖范围 ────┘
 *     LEN = 1 + payload_len
 * ------------------------------------------------------------------------- */
#ifndef SFRA_PROTO_H
#define SFRA_PROTO_H

#include <stdint.h>

#include "sfra_core.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------- 帧常量 */
#define SFRA_SOF0        0xAAu
#define SFRA_SOF1        0x55u
#define SFRA_PROTO_HDR   3u          /* SOF(2) + LEN(1)，CMD 属于 body */
#define SFRA_PROTO_MAXPAYLOAD 254u   /* LEN 是 1 字节，减掉 CMD 1 字节 */
#define SFRA_PROTO_MAXFRAME  (3u + 255u + 2u)

#define SFRA_PROTO_VERSION 1u        /* 协议版本，PING_ACK 里回报 */

/* ---------------------------------------------------------------- 命令字 */
#define SFRA_CMD_PING            0x01u
#define SFRA_CMD_GET_CAPS        0x02u
#define SFRA_CMD_SET_CONFIG      0x03u
#define SFRA_CMD_START           0x04u
#define SFRA_CMD_ABORT           0x05u
#define SFRA_CMD_GET_STATUS      0x06u
#define SFRA_CMD_GET_DATA        0x07u
#define SFRA_CMD_SET_COEFF       0x08u
#define SFRA_CMD_GET_COEFF       0x09u
#define SFRA_CMD_GET_CONDITIONS  0x0Au

#define SFRA_CMD_ACK             0x80u
#define SFRA_CMD_PING_ACK        0x81u
#define SFRA_CMD_CAPS            0x82u
#define SFRA_CMD_STATUS          0x86u
#define SFRA_CMD_DATA            0x87u
#define SFRA_CMD_COEFF           0x88u
#define SFRA_CMD_CONDITIONS      0x8Au

/* ---------------------------------------------------------------- 错误码 */
#define SFRA_ERR_OK          0x00u
#define SFRA_ERR_BAD_CRC     0x01u
#define SFRA_ERR_BAD_PARAM   0x02u
#define SFRA_ERR_BUSY        0x03u
#define SFRA_ERR_NOT_CFG     0x04u
#define SFRA_ERR_INDEX       0x05u
#define SFRA_ERR_STATE       0x06u

/* ---------------------------------------------------------------- 状态字 */
#define SFRA_ST_IDLE_G   0u
#define SFRA_ST_SETTLE_G 1u
#define SFRA_ST_COLLECT_G 2u
#define SFRA_ST_NEXT_G   3u
#define SFRA_ST_DONE_G   4u
#define SFRA_ST_ABORT_G  5u

/* ---------------------------------------------------------------- 能力位 */
#define SFRA_CAP_FLOAT   0x0001u
#define SFRA_CAP_FIXED   0x0002u
#define SFRA_CAP_READBACK 0x0004u

/* ---------------------------------------------------------------- 未应答超时
 *
 * MCU 端"多久收不到有效帧就自动 abort"，安全护栏，不依赖上位机。
 * 单位：毫秒，由调用方在 poll 里传入当前毫秒数。
 * 0 表示关闭（自测时可以关掉，真机必须开）。
 */
#ifndef SFRA_PROTO_TIMEOUT_MS
#define SFRA_PROTO_TIMEOUT_MS 500u
#endif

/* ---------------------------------------------------------------- 上下文 */
typedef struct {
    /* --- 接收状态机 --- */
    uint8_t  rx_buf[SFRA_PROTO_MAXFRAME];
    uint16_t rx_len;
    uint32_t crc_errors;
    uint32_t frame_count;

    /* --- 发送缓冲（单帧，发完再装下一帧） --- */
    uint8_t  tx_buf[SFRA_PROTO_MAXFRAME];
    uint16_t tx_len;

    /* --- 配置与能力 --- */
    sfra_setup_t cfg;
    uint8_t  configured;       /* 收到过 SET_CONFIG */
    uint8_t  pending_nack;     /* 需要在 poll 里回一个 NACK */
    uint8_t  nack_cmd;
    uint8_t  nack_code;

    /* --- 反压/超时 --- */
    uint32_t last_rx_ms;       /* 最近一帧有效帧的时间戳 */
    uint32_t last_tick_ms;
} sfra_proto_t;

/* ---------------------------------------------------------------- 生命周期 */

/* 初始化。caps 由调用方填（isr_freq / 能力位 / 版本）。 */
void sfra_proto_init(sfra_proto_t *p, const sfra_setup_t *default_cfg);

/* UART RX 中断里调用：把收到的字节塞进来。
 *
 * 只做"入缓冲 + 找帧头"，**不做 CRC 与解析**——ISR 里不能跑重活。
 * 缓冲满时丢弃最旧数据并计数，不阻塞。 */
void sfra_proto_rx(sfra_proto_t *p, const uint8_t *data, uint16_t n);

/* 主循环里调用：解析已到齐的帧、执行命令、装好待发帧。
 *
 * now_ms 是当前毫秒时间戳（用户用 SysTick 提供）。传 0 表示不检查超时。
 * **一次调用最多处理一帧**，保证不会长时间占用主循环。 */
void sfra_proto_poll(sfra_proto_t *p, sfra_t *s, const sfra_result_t *res,
                     uint32_t now_ms);

/* 用户把 tx_buf 发出去之后调用，清空发送缓冲。 */
void sfra_proto_tx_done(sfra_proto_t *p);

/* 用户提供：报告此刻被测系统的条件（Vin/Vout/Iout/…）。
 * 返回 0 表示成功；非 0 则回 NACK。可以不实现，弱符号给默认全 0。 */
int sfra_proto_get_conditions(float out[8]);

/* 用户提供：收到 SET_COEFF 时的回调。返回 0 成功。
 * 弱符号默认返回"不支持"（NACK BAD_PARAM）。 */
int sfra_proto_set_coeff(uint8_t order, const float *b);

/* 用户提供：收到 GET_COEFF 时的回调，**读当前生效的系数**。
 * out 至少能放 4 个 float；返回实际阶数 1~4，返回 <=0 表示不支持。
 *
 * 必须读"当前生效"的那一组，不要回显上一次 SET_COEFF 的入参——
 * 否则 GET_COEFF 变成了自己回显自己，读回校验就失去意义。 */
int sfra_proto_get_coeff(float out[4]);

#ifdef __cplusplus
}
#endif

#endif /* SFRA_PROTO_H */
