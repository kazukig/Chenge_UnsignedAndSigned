/**
 * @file    util_module.c
 * @brief   Small mixed-utility module showcasing Doxygen, globals, and static internals.
 * @details
 * - Provides a tiny ring buffer, a CRC16-CCITT calculator, a toy PRNG,
 *   simple statistics helpers, and a super-light logger.
 * - Intentionally uses many `static` and some global variables to illustrate style.
 * - Not production-ready; this is a pedagogical sample (~300 lines).
 *
 * @author  You
 * @date    2025-09-20
 */

#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>

/* ========================================================================== */
/*                               Public Types                                 */
/* ========================================================================== */

#define TEST_NUM 100U
/**
 * @brief Logger severity levels.
 */
typedef enum {
    LOG_SILENT = 0,  /**< No logs emitted. */
    LOG_ERROR  = 1,  /**< Errors only.     */
    LOG_WARN   = 2,  /**< Warnings.        */
    LOG_INFO   = 3,  /**< Info messages.   */
    LOG_DEBUG  = 4   /**< Verbose debug.   */
} log_level_t;

/**
 * @brief Minimal configuration structure.
 */
typedef struct {
    uint32_t magic;          /**< Magic number for config validity.        */
    uint32_t version;        /**< Configuration version.                   */
    uint16_t ring_capacity;  /**< Capacity of the ring buffer (bytes).     */
    log_level_t log_level;   /**< Default log level.                       */
    float     outlier_z;     /**< Z-score for outlier detection.           */
} module_config_t;

/**
 * @brief Rolling statistics accumulator for mean/variance (Welford).
 */
typedef struct {
    uint64_t n;     /**< Number of samples. */
    double   mean;  /**< Current mean.      */
    double   m2;    /**< Sum of squares of differences from the mean. */
} welford_t;

typedef int16_t TEST_TYPE1;


/* ========================================================================== */
/*                             Global (extern) Vars                           */
/* ========================================================================== */

/**
 * @brief Global configuration used across the module.
 * @warning Modified by init and set_config related functions.
 */
module_config_t g_config = {
    .magic = 0xC0FFEEu,
    .version = 1u,
    .ring_capacity = 256u,
    .log_level = LOG_INFO,
    .outlier_z = 3.0f
};

/**
 * @brief Global counter of total bytes processed through the ring.
 */
uint64_t g_total_bytes_pushed = 0;

/**
 * @brief Global counter of total bytes popped from the ring.
 */
uint64_t g_total_bytes_popped = 0;

/* ========================================================================== */
/*                              Static Internals                              */
/* ========================================================================== */

/** @brief Internal ring buffer storage (max). */
static uint8_t s_ring_storage[1024];

/** @brief Internal ring indices and capacity. */
static struct {
    uint16_t head;       /**< Next write index.        */
    uint16_t tail;       /**< Next read index.         */
    uint16_t capacity;   /**< Active capacity (<=1024).*/
} s_ring = {0, 0, 0};

/** @brief Internal logger level; mirrors g_config.log_level at init. */
static log_level_t s_log_level = LOG_INFO;

/** @brief Internal CRC16 table lazy-init flag. */
static bool s_crc_table_ready = false;

/** @brief Internal CRC16 lookup table. */
static uint16_t s_crc16_table[256];

/** @brief Internal PRNG state for xorshift32. */
static uint32_t s_prng_state = 0x12345678u;

/** @brief Internal stats accumulator. */
static welford_t s_stats = {0, 0.0, 0.0};

/* ========================================================================== */
/*                         Forward Static Declarations                        */
/* ========================================================================== */

static void        prv_crc16_build_table(void);
static void        prv_log(log_level_t lvl, const char *fmt, ...);
static inline bool prv_ring_empty(void);
static inline bool prv_ring_full(void);

/* ========================================================================== */
/*                              Public Functions                              */
/* ========================================================================== */

/**
 * @brief Initialize the module and internal subsystems.
 * @param cfg Optional configuration. If NULL, uses current g_config.
 * @return true on success, false on invalid config.
 */
bool module_init(const module_config_t *cfg)
{
    const module_config_t *use = cfg ? cfg : &g_config;

    if ((use->ring_capacity == 0u) || (use->ring_capacity > sizeof(s_ring_storage))) {
        return false;
    }

    g_config = *use; /* copy */
    s_log_level = g_config.log_level;

    s_ring.capacity = g_config.ring_capacity;
    s_ring.head = s_ring.tail = 0;



    s_crc_table_ready = false; /* rebuild lazily */
    s_prng_state = 0xCAFEBABEu;

    s_stats.n = 0;
    s_stats.mean = 0.0;
    s_stats.m2 = 0.0;
    
    int akkk;
    akkk = TEST_NUM + 30U;

    int a = 5;
    unsigned int rk = 0;
    rk = a;
    
    prv_log(LOG_INFO, "module_init: cap=%u level=%d", s_ring.capacity, (int)s_log_level);
    return true;
}

/**
 * @brief Set configuration dynamically.
 * @param cfg Non-NULL pointer to a configuration struct.
 * @return true on success, false on invalid arguments.
 */
bool module_set_config(const module_config_t *cfg)
{
    if (!cfg) return false;
    if (cfg->ring_capacity == 0u || cfg->ring_capacity > sizeof(s_ring_storage))
        return false;

    g_config = *cfg;
    s_log_level = g_config.log_level;
    s_ring.capacity = g_config.ring_capacity;
    s_ring.head = s_ring.tail = 0;

    prv_log(LOG_DEBUG, "set_config: cap=%u level=%d", s_ring.capacity, (int)s_log_level);
    return true;
}

/**
 * @brief Reset the ring buffer and counters.
 */
void module_reset(void)
{
    s_ring.head = s_ring.tail = 0;
    g_total_bytes_pushed = 0;
    g_total_bytes_popped = 0;
    prv_log(LOG_INFO, "module_reset");
}

/**
 * @brief Set the logger level.
 * @param level Desired level.
 */
void log_set_level(log_level_t level)
{
    s_log_level = level;
    prv_log(LOG_INFO, "log_set_level=%d", (int)level);
}

/**
 * @brief Push raw bytes into the ring buffer.
 * @param data Pointer to data to push.
 * @param len  Number of bytes to push.
 * @return Number of bytes actually pushed.
 */
size_t ring_push(const void *data, size_t len)
{
    const uint8_t *p = (const uint8_t *)data;
    size_t pushed = 0;

    if (!data || len == 0) return 0;

    while (pushed < len && !prv_ring_full()) {
        s_ring_storage[s_ring.head] = p[pushed];
        s_ring.head = (uint16_t)((s_ring.head + 1u) % s_ring.capacity);
        ++pushed;
    }

    g_total_bytes_pushed += pushed;
    if (pushed < len) {
        prv_log(LOG_WARN, "ring_push: truncated (wanted=%zu, pushed=%zu)", len, pushed);
    }
    return pushed;
}
/**
 * @brief Pop bytes from the ring buffer.
 * @param out Pointer to output buffer.
 * @param len Max bytes toß pop.
 * @return Number of bytes popped.
 */
size_t ring_pop(void *out, size_t len)
{
    uint8_t *p = (uint8_t *)out;
    size_t popped = 0;

    if (!out || len == 0) return 0;

    while (popped < len && !prv_ring_empty()) {
        p[popped] = s_ring_storage[s_ring.tail];
        s_ring.tail = (uint16_t)((s_ring.tail + 1u) % s_ring.capacity);
        ++popped;
    }

    g_total_bytes_popped += popped;
    return popped;
}

/**
 * @brief Compute CRC16-CCITT (poly 0x1021, init 0xFFFF) with table.
 * @param data Buffer pointer.
 * @param len  Buffer length.
 * @return CRC16 value.
 */
uint16_t crc16_ccitt(const void *data, size_t len)
{
    if (!s_crc_table_ready) {
        prv_crc16_build_table();
        s_crc_table_ready = true;
    }
    const uint8_t *p = (const uint8_t *)data;
    uint16_t crc = 0xFFFFu;

    for (size_t i = 0; i < len; ++i) {
        uint8_t idx = (uint8_t)((crc >> 8) ^ p[i]);
        crc = (uint16_t)((crc << 8) ^ s_crc16_table[idx]);
    }
    return crc;
}

/**
 * @brief Seed the internal xorshift32 PRNG.
 * @param seed Seed value; 0 will be adjusted to a non-zero value.
 */
void srand32(uint32_t seed)
{
    s_prng_state = seed ? seed : 0x1u;
    prv_log(LOG_DEBUG, "srand32: seed=0x%08X", s_prng_state);
}

/**
 * @brief Generate a pseudo-random 32-bit value (xorshift32).
 * @return Pseudo-random number.
 */
uint32_t rand32(void)
{
    uint32_t x = s_prng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    s_prng_state = x;
    return x;
}

/**
 * @brief Add a sample to rolling stats (Welford).
 * @param x Sample value.
 */
void stats_add_sample(double x)
{
    s_stats.n += 1;
    double delta = x - s_stats.mean;
    s_stats.mean += delta / (double)s_stats.n;
    double delta2 = x - s_stats.mean;
    s_stats.m2 += delta * delta2;
}

/**
 * @brief Get current mean and variance (population) from stats accumulator.
 * @param[out] mean_out Mean.
 * @param[out] var_out  Variance (population). If n<2, variance=0.
 */
void stats_get(double *mean_out, double *var_out)
{
    if (mean_out) *mean_out = s_stats.mean;
    if (var_out)  *var_out  = (s_stats.n > 1) ? (s_stats.m2 / (double)s_stats.n) : 0.0;
}

/**
 * @brief Heuristic outlier check using Z-score threshold from config.
 * @param x Value to test.
 * @return true if |Z| > g_config.outlier_z and variance > 0; false otherwise.
 */
bool stats_is_outlier(double x)
{
    double mean, var;
    stats_get(&mean, &var);
    if (var <= 0.0) return false;
    double z = (x - mean) / sqrt(var);
    return (fabs(z) > (double)g_config.outlier_z);
}

/**
 * @brief Dump internal state summary to a FILE* (e.g., stdout).
 * @param fp Stream to print to; defaults to stdout if NULL.
 */
void module_dump(FILE *fp)
{
    FILE *out = fp ? fp : stdout;
    double mean=0, var=0;
    stats_get(&mean, &var);
    fprintf(out,
        "---- MODULE DUMP ----\n"
        " config.magic     = 0x%08X\n"
        " config.version   = %u\n"
        " ring.capacity    = %u\n"
        " ring.head/tail   = %u/%u\n"
        " log.level        = %d\n"
        " totals pushed/pop= %llu/%llu\n"
        " stats: n=%llu mean=%.6f var=%.6f\n"
        " --------------------\n",
        g_config.magic,
        g_config.version,
        s_ring.capacity,
        s_ring.head, s_ring.tail,
        (int)s_log_level,
        (unsigned long long)g_total_bytes_pushed,
        (unsigned long long)g_total_bytes_popped,
        (unsigned long long)s_stats.n, mean, var
    );
}

/**
 * @brief Tiny self-test routine to exercise major paths.
 * @return true on pass, false on fail.
 */
bool module_self_test(void)
{
    const char *msg = "hello, ring";
    module_reset();

    size_t p = ring_push(msg, strlen(msg));
    uint8_t buf[32] = {0};
    size_t r = ring_pop(buf, sizeof(buf));

    uint16_t c = crc16_ccitt(buf, r);

    srand32(0xDEADBEEF);
    (void)rand32(); (void)rand32();

    for (int i = 0; i < 10; ++i) stats_add_sample((double)i);

    bool ok = (p == strlen(msg)) && (r == strlen(msg)) && (c != 0);
    prv_log(ok ? LOG_INFO : LOG_ERROR, "self_test: %s", ok ? "OK" : "FAIL");
    return ok;
}

/* ========================================================================== */
/*                             Static Implementations                         */
/* ========================================================================== */

#include <stdarg.h>

/**
 * @brief Internal logger.
 * @param lvl Severity.
 * @param fmt printf-like format.
 * @param ... Args.
 */
static void prv_log(log_level_t lvl, const char *fmt, ...)
{
    if (lvl > s_log_level || lvl == LOG_SILENT) return;

    static const char *tag[] = {"SILENT","ERROR","WARN","INFO","DEBUG"};
    const char *tt = (lvl >= LOG_SILENT && lvl <= LOG_DEBUG) ? tag[lvl] : "UNK";

    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "[%s] ", tt);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
}

/**
 * @brief Build CRC16-CCITT lookup table.
 */
static void prv_crc16_build_table(void)
{
    const uint16_t poly = 0x1021u;
    for (uint16_t i = 0; i < 256u; ++i) {
        uint16_t crc = (uint16_t)(i << 8);
        for (int b = 0; b < 8; ++b) {
            if (crc & 0x8000u) {
                crc = (uint16_t)((crc << 1) ^ poly);
            } else {
                crc <<= 1;
            }
        }
        s_crc16_table[i] = crc;
    }
}

/**
 * @brief Check if ring is empty.
 * @return true when empty.
 */
static inline bool prv_ring_empty(void)
{
    return s_ring.head == s_ring.tail;
}

/**
 * @brief Check if ring is full.
 * @return true when full.
 */
static inline bool prv_ring_full(void)
{
    return (uint16_t)((s_ring.head + 1u) % s_ring.capacity) == s_ring.tail;
}

/* ========================================================================== */
/*                                   Demo                                     */
/* ========================================================================== */
/**
 * @brief Optional demo main (define UTIL_MODULE_DEMO to compile).
 */
#ifdef UTIL_MODULE_DEMO
int main(void)
{
    module_init(NULL);

    const char payload[] = "The quick brown fox jumps over the lazy dog.";
    ring_push(payload, sizeof(payload)-1);
    uint8_t out[64];
    size_t n = ring_pop(out, sizeof(out));
    uint16_t crc = crc16_ccitt(out, n);

    for (int i = -5; i <= 5; ++i) stats_add_sample((double)i);

    module_dump(NULL);
    printf("CRC16=0x%04X, popped=%zu\n", crc, n);

    bool ok = module_self_test();
    printf("self_test=%s\n", ok ? "OK" : "FAIL");
    return ok ? 0 : 1;
}
#endif

/* ========================================================================== */
/*                               Doxygen Blurbs                               */
/* ========================================================================== */
/**
 * @defgroup PublicAPI Public API
 * @brief    Functions callable by users of the module.
 * @{
 * - module_init
 * - module_set_config
 * - module_reset
 * - log_set_level
 * - ring_push
 * - ring_pop
 * - crc16_ccitt
 * - srand32
 * - rand32
 * - stats_add_sample
 * - stats_get
 * - stats_is_outlier
 * - module_dump
 * - module_self_test
 * @}
 */
