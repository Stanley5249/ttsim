// SPDX-FileCopyrightText: (c) 2025-2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0

// RISC-V floating-point implementation helpers, currently for babyrisc FP32 RNE only.
#pragma once
#include <bit>
#include <stdint.h>

static inline bool fp32_is_nan(uint32_t x) {
    return (x & 0x7FFFFFFF) > 0x7F800000;
}

static inline uint32_t fp32_classify(uint32_t x) {
    bool sign = x >> 31;
    uint32_t exp = (x >> 23) & 255;
    uint32_t man = x & 0x7FFFFF;
    if (exp == 255) {
        if (!man) {
            return sign ? (1u << 0) : (1u << 7); // -inf/+inf
        }
        return (man & 0x400000) ? (1u << 9) : (1u << 8); // qnan/snan
    } else if (exp == 0) {
        if (!man) {
            return sign ? (1u << 3) : (1u << 4); // -0/+0
        }
        return sign ? (1u << 2) : (1u << 5); // -subnormal/+subnormal
    } else {
        return sign ? (1u << 1) : (1u << 6); // -normal/+normal
    }
}

// fmin.s/fmax.s: both NaN -> canonical NaN; one NaN -> the other operand; -0 compares below +0.
static inline uint32_t fp32_min_max(uint32_t a, uint32_t b, bool is_max) {
    if (fp32_is_nan(a)) {
        return fp32_is_nan(b) ? 0x7FC00000 : b;
    }
    if (fp32_is_nan(b)) {
        return a;
    }
    if (std::bit_cast<float>(a) == std::bit_cast<float>(b)) {
        // Only +0/-0 reach here with differing bits: OR the signs to pick -0 for min, AND for max.
        return is_max ? (a & b) : (a | b);
    }
    return ((std::bit_cast<float>(a) < std::bit_cast<float>(b)) != is_max) ? a : b;
}

// Adding 1.5 * 2^52 to a double below 2^51 in magnitude rounds away its fraction bits, to nearest even
static inline int64_t fp32_round_to_integer(uint32_t x) {
    return int64_t((double(std::bit_cast<float>(x)) + 0x1.8p52) - 0x1.8p52);
}

static inline uint32_t fp32_to_i32(uint32_t x) {
    float f = std::bit_cast<float>(x);
    if (fp32_is_nan(x) || (f >= 0x1p31f)) {
        return 0x7FFFFFFF;
    }
    return (f <= -0x1p31f) ? 0x80000000 : uint32_t(fp32_round_to_integer(x));
}

static inline uint32_t fp32_to_u32(uint32_t x) {
    float f = std::bit_cast<float>(x);
    if (fp32_is_nan(x) || (f >= 0x1p32f)) {
        return 0xFFFFFFFF;
    }
    return (x & 0x80000000) ? 0 : uint32_t(fp32_round_to_integer(x));
}

static inline uint32_t i32_to_fp32(uint32_t x) {
    return std::bit_cast<uint32_t>(float(int32_t(x)));
}

static inline uint32_t u32_to_fp32(uint32_t x) {
    return std::bit_cast<uint32_t>(float(x));
}
