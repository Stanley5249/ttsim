// SPDX-FileCopyrightText: (c) 2025-2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0

// libttsim.so exposes the device's physical I/O interfaces (e.g. PCIE, Ethernet).  These
// symbols form a stable, binary-compatible API intended for long-term use; changes should
// be rare and should always correspond to physical ports/connectivity on the device.
// The API/ABI contract and compatibility policy are documented in docs/libttsim_api.md.
#include "sim.h"
#include "pcie.h"

#define API_EXPORT __attribute((visibility("default")))

// Host-float results are exact only under the default FP environment (RNE, no FTZ/DAZ, exceptions masked), which
// the host may have changed. Every entry point runs under it, and the host's environment is back in place whenever
// the host runs: on return and in the DMA callbacks.
#if defined(__x86_64__)
#include <xmmintrin.h>
using FpEnv = uint32_t; // MXCSR
static constexpr FpEnv DEFAULT_FPENV = 0x1F80;
static inline FpEnv fpenv_read() {
    return _mm_getcsr();
}
static inline void fpenv_write(FpEnv env) {
    _mm_setcsr(env);
}
#elif defined(__aarch64__)
struct FpEnv {
    uint64_t fpcr;
    uint64_t fpsr;
};
static constexpr FpEnv DEFAULT_FPENV = {0, 0};
static inline FpEnv fpenv_read() {
    FpEnv env;
    asm volatile("mrs %0, fpcr" : "=r"(env.fpcr) :: "memory");
    asm volatile("mrs %0, fpsr" : "=r"(env.fpsr) :: "memory");
    return env;
}
static inline void fpenv_write(FpEnv env) {
    asm volatile("msr fpcr, %0" :: "r"(env.fpcr) : "memory");
    asm volatile("msr fpsr, %0" :: "r"(env.fpsr) : "memory");
}
#elif defined(__riscv)
using FpEnv = uint32_t; // fcsr
static constexpr FpEnv DEFAULT_FPENV = 0;
static inline FpEnv fpenv_read() {
    FpEnv env;
    asm volatile("frcsr %0" : "=r"(env) :: "memory");
    return env;
}
static inline void fpenv_write(FpEnv env) {
    asm volatile("fscsr %0" :: "r"(env) : "memory");
}
#else
#error "unsupported host"
#endif

static FpEnv s_host_fpenv;

struct FpEnvGuard {
    FpEnvGuard() {
        s_host_fpenv = fpenv_read();
        fpenv_write(DEFAULT_FPENV);
    }
    ~FpEnvGuard() {
        fpenv_write(s_host_fpenv);
    }
};

static bool s_ttsim_running = false;
static void (*s_pfn_libttsim_pci_dma_mem_rd_bytes)(uint64_t paddr, void *p, uint32_t size);
static void (*s_pfn_libttsim_pci_dma_mem_wr_bytes)(uint64_t paddr, const void *p, uint32_t size);

extern "C" API_EXPORT void libttsim_init() {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(!s_ttsim_running, ConfigurationError, "sim is already running");
    ttsim_init();
    pcie_init();
    s_ttsim_running = true;
}

extern "C" API_EXPORT void libttsim_exit() {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(s_ttsim_running, ConfigurationError, "sim is not running");
    ttsim_exit();
    s_ttsim_running = false;
}

extern "C" API_EXPORT void libttsim_set_pci_dma_mem_callbacks(
    decltype(s_pfn_libttsim_pci_dma_mem_rd_bytes) pci_dma_mem_rd,
    decltype(s_pfn_libttsim_pci_dma_mem_wr_bytes) pci_dma_mem_wr
) {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(!s_ttsim_running, ConfigurationError, "sim is already running");
    s_pfn_libttsim_pci_dma_mem_rd_bytes = pci_dma_mem_rd;
    s_pfn_libttsim_pci_dma_mem_wr_bytes = pci_dma_mem_wr;
}

// Map a bus/device/function tuple to an MMIO chip index, or NUM_MMIO_CHIPS if the slot is empty.
// An absent-but-well-formed BDF is not an error: hosts discover endpoints by probing empty slots.
static uint32_t pci_device_from_bdf(uint32_t bus_device_function) {
    // normally only 16 bits, but validate none above these are set
    TTSIM_VERIFY(!(bus_device_function >> 16), UndefinedBehavior, "bus_device_function=0x%x", bus_device_function);
    uint32_t function = bus_device_function & 7;
    uint32_t bdf_device = (bus_device_function >> 3) & 0x1F;
    uint32_t bus = (bus_device_function >> 8) & 0xFF;
    if (function) {
        return NUM_MMIO_CHIPS; // single-function endpoints; functions 1-7 are absent
    }
    return (bus == 0 && (bdf_device < NUM_MMIO_CHIPS)) ? bdf_device : NUM_MMIO_CHIPS;
}

extern "C" API_EXPORT uint32_t libttsim_pci_config_rd32(uint32_t bus_device_function, uint32_t offset) {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(s_ttsim_running, ConfigurationError, "sim is not running");
    TTSIM_VERIFY(!(offset & 3), UndefinedBehavior, "misaligned offset=0x%x", offset);
    uint32_t device = pci_device_from_bdf(bus_device_function);
    if (device >= NUM_MMIO_CHIPS) {
        return 0xFFFFFFFFu; // non-existent device
    }
    return pcie_config_rd32(device, offset);
}

extern "C" API_EXPORT void libttsim_pci_config_wr32(uint32_t bus_device_function, uint32_t offset, uint32_t data) {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(s_ttsim_running, ConfigurationError, "sim is not running");
    TTSIM_VERIFY(!(offset & 3), UndefinedBehavior, "misaligned offset=0x%x", offset);
    uint32_t device = pci_device_from_bdf(bus_device_function);
    TTSIM_VERIFY(device < NUM_MMIO_CHIPS, UndefinedBehavior, "bus_device_function=0x%x has no device",
        bus_device_function);
    pcie_config_wr32(device, offset, data);
}

// Route a host physical access to the owning MMIO device's chip, then translate within
// that device's BAR windows. For NUM_CHIPS==1 this is a straight passthrough.
extern "C" API_EXPORT void libttsim_pci_mem_rd_bytes(uint64_t paddr, void *p, uint32_t size) {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(s_ttsim_running, ConfigurationError, "sim is not running");
    TTSIM_VERIFY(size, UndefinedBehavior, "size=%d", size);
#if NUM_CHIPS > 1
    uint32_t device = pcie_device_from_paddr(paddr, size);
    uint32_t saved_chip_id = g_current_chip_id;
    ttsim_select_chip(device);
    pcie_mem_rd_bytes(paddr, p, size);
    ttsim_select_chip(saved_chip_id);
#else
    pcie_mem_rd_bytes(paddr, p, size);
#endif
}

extern "C" API_EXPORT void libttsim_pci_mem_wr_bytes(uint64_t paddr, const void *p, uint32_t size) {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(s_ttsim_running, ConfigurationError, "sim is not running");
    TTSIM_VERIFY(size, UndefinedBehavior, "size=%d", size);
#if NUM_CHIPS > 1
    uint32_t device = pcie_device_from_paddr(paddr, size);
    uint32_t saved_chip_id = g_current_chip_id;
    ttsim_select_chip(device);
    pcie_mem_wr_bytes(paddr, p, size);
    ttsim_select_chip(saved_chip_id);
#else
    pcie_mem_wr_bytes(paddr, p, size);
#endif
}

void libttsim_pci_dma_mem_rd_bytes(uint64_t paddr, void *p, uint32_t size) {
    TTSIM_VERIFY(s_pfn_libttsim_pci_dma_mem_rd_bytes, ConfigurationError, "DMA read callback not installed");
    fpenv_write(s_host_fpenv);
    s_pfn_libttsim_pci_dma_mem_rd_bytes(paddr, p, size);
    s_host_fpenv = fpenv_read();
    fpenv_write(DEFAULT_FPENV);
}

void libttsim_pci_dma_mem_wr_bytes(uint64_t paddr, const void *p, uint32_t size) {
    TTSIM_VERIFY(s_pfn_libttsim_pci_dma_mem_wr_bytes, ConfigurationError, "DMA write callback not installed");
    fpenv_write(s_host_fpenv);
    s_pfn_libttsim_pci_dma_mem_wr_bytes(paddr, p, size);
    s_host_fpenv = fpenv_read();
    fpenv_write(DEFAULT_FPENV);
}

// transitional; to be removed once the PCIe/BAR path is in place everywhere (see docs/libttsim_api.md)
extern "C" API_EXPORT void libttsim_tile_rd_bytes(uint32_t x, uint32_t y, uint64_t addr, void *p, uint32_t size) {
    FpEnvGuard fpenv_guard;
    TTSIM_ERROR_NOFMT(UnsupportedFunctionality);
}

// transitional; to be removed once the PCIe/BAR path is in place everywhere (see docs/libttsim_api.md)
extern "C" API_EXPORT void libttsim_tile_wr_bytes(uint32_t x, uint32_t y, uint64_t addr, const void *p, uint32_t size) {
    FpEnvGuard fpenv_guard;
    TTSIM_ERROR_NOFMT(UnsupportedFunctionality);
}

static void clock_current_chip() {
    for (uint32_t tile_id = 0; tile_id < std::size(g_t_tiles); tile_id++) {
        TensixTile *p_tile = &g_t_tiles[tile_id];
        // Hack: for simulator perf, we clock a tile repeatedly as long as it successfully executes at least one Tensix instruction
        for (;;) {
            for (uint32_t rv32_mask = p_tile->rv32_cores_active; rv32_mask; rv32_mask &= rv32_mask - 1) {
                uint32_t rv32_index = __builtin_ctz(rv32_mask);
                rv32_step(&p_tile->rv32[rv32_index]);
            }
            [[maybe_unused]] bool any_tensix = false;
            for (uint32_t tensix_id = 0; tensix_id < std::size(p_tile->tensix); tensix_id++) {
                TensixState *p_tensix = &p_tile->tensix[tensix_id];
                for (uint32_t pipe_mask = p_tensix->inst_pipes_active; pipe_mask; pipe_mask &= pipe_mask - 1) {
                    uint32_t pipe = __builtin_ctz(pipe_mask);
                    uint32_t inst_rd_ptr = p_tensix->inst_rd_ptr[pipe];
                    if (inst_rd_ptr != p_tensix->inst_wr_ptr[pipe]) {
                        uint32_t inst = p_tensix->inst[pipe][inst_rd_ptr];
                        if (tensix_decode_and_execute(p_tensix, pipe, inst)) {
                            any_tensix = true;
                            inst_rd_ptr = (inst_rd_ptr + 1) % TENSIX_INST_FIFO_SIZE;
                            p_tensix->inst_rd_ptr[pipe] = inst_rd_ptr;
                            if (inst_rd_ptr == p_tensix->inst_wr_ptr[pipe]) {
                                p_tensix->inst_pipes_active &= ~(1 << pipe);
                            }
                        }
                    }
                }
            }
            if (!any_tensix) {
                break; // no Tensix instructions executed, bail out
            }
        }
    }
    for (uint64_t rv32_mask = g_rv32_cores_active; rv32_mask; rv32_mask &= rv32_mask - 1) {
        uint32_t core_index = __builtin_ctzll(rv32_mask);
        uint32_t tile_id = core_index / RV32_CORES_PER_E_TILE;
        uint32_t rv32_index = core_index % RV32_CORES_PER_E_TILE;
        auto *p_hart = &g_e_tiles[tile_id].rv32[rv32_index];
        rv32_step(p_hart);
    }
}

extern "C" API_EXPORT void libttsim_clock(uint32_t n_clocks) {
    FpEnvGuard fpenv_guard;
    TTSIM_VERIFY(s_ttsim_running, ConfigurationError, "sim is not running");
#if NUM_CHIPS > 1
    uint32_t saved_chip_id = g_current_chip_id;
#endif
    for (uint32_t i = 0; i < n_clocks; i++) {
#if NUM_CHIPS > 1
        for (uint32_t chip_id = 0; chip_id < NUM_CHIPS; chip_id++) {
            ttsim_select_chip(chip_id);
            clock_current_chip();
        }
#else
        clock_current_chip();
#endif
        g_clock++; // single global timebase, advanced once per clock across all chips
    }
#if NUM_CHIPS > 1
    ttsim_select_chip(saved_chip_id);
#endif
}
