#!/usr/bin/env python3
"""Check the stepping wall clock of a single-chip Wormhole or Blackhole libttsim.so through its public API.

While an eth core is busy, one clock steps it several times and the wall clock must count each step; once it is idle,
one clock is one step. The host reads the wall clock between clocks through a BAR0 TLB window, as tt-umd does.
"""

import ctypes
import sys

BAR0_BASE = 0x100000000
TLB_CFG = 0x1FC00000  # BAR0 offset of the TLB config registers
WALL_CLOCK_0 = 0xFFB121F0  # RISCV_DEBUG_REGS_WALL_CLOCK_0
SOFT_RESET_0 = 0xFFB121B0  # RISCV_DEBUG_REGS_SOFT_RESET_0
BH_IERISC_RESET_PC = 0xFFB14000  # ETH_RISC_CTRL_IERISC_RESET_PC
JUMP_TO_SELF = 0x0000006F  # jal x0, 0
IDLE_CLOCKS = 100000  # well past the window in which an eth core counts as busy
WORMHOLE_ID = 0x401E
BLACKHOLE_ID = 0xB140


class Chip:
    """A simulated chip driven through the libttsim C API."""

    def __init__(self, path: str) -> None:
        """Load and initialize the library at path."""
        self.lib = ctypes.CDLL(path)
        self.lib.libttsim_pci_config_rd32.restype = ctypes.c_uint32
        self.lib.libttsim_init()
        device_id = self.lib.libttsim_pci_config_rd32(0, 0) >> 16
        if device_id not in (WORMHOLE_ID, BLACKHOLE_ID):
            sys.exit(f"unexpected device id 0x{device_id:x}")
        self.wormhole = device_id == WORMHOLE_ID
        # An eth tile at NOC0 (1, 0) on Wormhole and (1, 1) on Blackhole; single-chip builds leave its core in reset
        self.coord = 1 | ((0 if self.wormhole else 1) << 6)

    def map(self, addr: int) -> int:
        """Point TLB 0 at the window holding addr and return the BAR0 address of addr."""
        if self.wormhole:  # 1 MiB windows, one 64-bit config each
            self.write(BAR0_BASE + TLB_CFG, (addr >> 20) | (self.coord << 16), 8)
            return BAR0_BASE + (addr & 0xFFFFF)
        # 2 MiB windows, three 32-bit configs each
        self.write(BAR0_BASE + TLB_CFG, (addr >> 21) & 0xFFFFFFFF, 4)
        self.write(BAR0_BASE + TLB_CFG + 4, ((addr >> 53) & 0x7FF) | (self.coord << 11), 4)
        self.write(BAR0_BASE + TLB_CFG + 8, 0, 4)
        return BAR0_BASE + (addr & 0x1FFFFF)

    def write(self, paddr: int, value: int, size: int) -> None:
        """Write the low size bytes of value at BAR0 address paddr."""
        data = value.to_bytes(size, "little")
        self.lib.libttsim_pci_mem_wr_bytes(ctypes.c_uint64(paddr), data, size)

    def read32(self, paddr: int) -> int:
        """Read 32 bits at BAR0 address paddr."""
        data = ctypes.create_string_buffer(4)
        self.lib.libttsim_pci_mem_rd_bytes(ctypes.c_uint64(paddr), data, 4)
        return int.from_bytes(data.raw, "little")

    def tile_write32(self, addr: int, value: int) -> None:
        """Write 32 bits at addr in the eth tile."""
        self.write(self.map(addr), value, 4)

    def wall_clock(self) -> int:
        """Read the low 32 bits of the eth tile wall clock."""
        return self.read32(self.map(WALL_CLOCK_0))

    def clock_once(self) -> int:
        """Return the wall clock ticks taken by one clock."""
        start = self.wall_clock()
        self.lib.libttsim_clock(1)
        return (self.wall_clock() - start) & 0xFFFFFFFF


def main() -> None:
    """Run the check on the library named on the command line."""
    chip = Chip(sys.argv[1])
    # Start the eth core on a jump to itself; Blackhole starts only from a programmed nonzero reset PC
    program = 0 if chip.wormhole else 0x1000
    chip.tile_write32(program, JUMP_TO_SELF)
    if not chip.wormhole:
        chip.tile_write32(BH_IERISC_RESET_PC, program)
    chip.tile_write32(SOFT_RESET_0, 0)
    chip.lib.libttsim_clock(1)

    chip.tile_write32(0x2000, 0)  # a host write to eth sram makes the core busy
    busy = chip.clock_once()
    chip.lib.libttsim_clock(IDLE_CLOCKS)
    idle = chip.clock_once()
    print(f"{sys.argv[1]}: wall clock ticks per clock: busy {busy}, idle {idle}")
    if busy <= 1:
        sys.exit("a busy eth core steps several times per clock, so the wall clock must count each step")
    if idle != 1:
        sys.exit("an idle core steps once per clock, so the wall clock must advance by one")


if __name__ == "__main__":
    main()
