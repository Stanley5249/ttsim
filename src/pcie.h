// SPDX-FileCopyrightText: (c) 2025-2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0

// Host-visible PCIe endpoint (config space and BARs), used by the libttsim.so ABI in libttsim.cpp.
#pragma once
#include <stdint.h>

// Multi-MMIO host enumeration: each host-visible chip is a distinct PCI device with its own BAR
// windows. Device i's BARs come up at the device-0 bases offset by i * PER_DEVICE_PADDR_STRIDE; the
// stride exceeds every BAR's top (BH BAR4 ends at 64GiB), so default device ranges never overlap.
// The host may relocate any BAR through config space, so accesses route by the programmed BARs.
#define PER_DEVICE_PADDR_STRIDE 0x1000000000ull // 64GiB

void pcie_init();
uint32_t pcie_config_rd32(uint32_t device, uint32_t offset);
void pcie_config_wr32(uint32_t device, uint32_t offset, uint32_t data);
uint32_t pcie_device_from_paddr(uint64_t paddr, uint32_t size);
void pcie_mem_rd_bytes(uint64_t paddr, void *p, uint32_t size);
void pcie_mem_wr_bytes(uint64_t paddr, const void *p, uint32_t size);
