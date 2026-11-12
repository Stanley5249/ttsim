#!/usr/bin/env python3
"""Check the unit tick wall clock of a single-chip Wormhole or Blackhole libttsim.so through its public API.

Every core takes one step per clock, so a core that reads the wall clock twice in a row sees it advance by exactly one.
Upstream steps some cores several times within one clock, and those reads return the same time. Two on-core programs
read the wall clock twice in a row and store the difference in local memory for the host to read back:

- An eth core runs while busy, which upstream steps ETH_BUSY_STEPS times per clock.
- BRISC on a Tensix tile runs while its Tensix thread executes a replayed run of DMANOPs, and upstream steps the tile
  again within one clock as long as it executes a Tensix instruction. This case also covers releases older than the eth
  busy steps.

The host loads the programs, takes the cores out of reset, and reads results through a BAR0 TLB window, as tt-umd does.
It also checks that the wall clock counts clocks.
"""

import ctypes
import sys

BAR0_BASE = 0x100000000
TLB_CFG = 0x1FC00000  # BAR0 offset of the TLB config registers
WALL_CLOCK_0 = 0xFFB121F0  # RISCV_DEBUG_REGS_WALL_CLOCK_0
SOFT_RESET_0 = 0xFFB121B0  # RISCV_DEBUG_REGS_SOFT_RESET_0
BRISC_ONLY = 0x47000  # SOFT_RESET_0 with BRISC out of reset and the TRISCs and NCRISC in it
BH_IERISC_RESET_PC = 0xFFB14000  # ETH_RISC_CTRL_IERISC_RESET_PC
TENSIX_INST = 0xFFE40000  # TENSIX_INST_BASE: a BRISC store here pushes a Tensix instruction to thread 0
DMANOP = 0x60000000  # Tensix DMANOP, which unlike NOP enters the instruction FIFO
REPLAY_LEN = 16  # within the replay buffer of every release, 32 entries on Wormhole and 64 on Blackhole
ETH_RESULT = 0x2000  # eth sram past the program
TENSIX_RESULT = 0x200  # Tensix L1 past the program
SENTINEL = 0xFFFFFFFF  # a result no program stores
RUN_CLOCKS = 1000  # clocks given to a program; each needs fewer than 100 steps
CLOCKS = 100000  # clocks over which the host checks that the wall clock counts clocks
WORMHOLE_ID = 0x401E
BLACKHOLE_ID = 0xB140

# RV32I registers by ABI name
T0, T1, T2, T3, A0, A1 = 5, 6, 7, 28, 10, 11
JUMP_TO_SELF = 0x0000006F  # jal x0, 0


# RV32I encoders, one per instruction the programs use, following the base formats of the RISC-V unprivileged spec
def lui(rd: int, imm: int) -> int:
    """Encode lui rd, imm, which sets rd to imm with its low 12 bits cleared (U-type)."""
    return (imm & 0xFFFFF000) | (rd << 7) | 0x37


def addi(rd: int, rs1: int, imm: int) -> int:
    """Encode addi rd, rs1, imm for a signed 12-bit imm (I-type)."""
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13


def sub(rd: int, rs1: int, rs2: int) -> int:
    """Encode sub rd, rs1, rs2 (R-type)."""
    return (0x20 << 25) | (rs2 << 20) | (rs1 << 15) | (rd << 7) | 0x33


def lw(rd: int, rs1: int, imm: int) -> int:
    """Encode lw rd, imm(rs1) for a signed 12-bit imm (I-type)."""
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (0x2 << 12) | (rd << 7) | 0x03


def sw(rs2: int, rs1: int, imm: int) -> int:
    """Encode sw rs2, imm(rs1) for a signed 12-bit imm (S-type)."""
    return ((imm & 0xFE0) << 20) | (rs2 << 20) | (rs1 << 15) | (0x2 << 12) | ((imm & 0x1F) << 7) | 0x23


def li(rd: int, value: int) -> list[int]:
    """Encode lui and addi that set rd to the 32-bit value; addi sign-extends, so lui takes the rounded upper part."""
    return [lui(rd, value + 0x800), addi(rd, rd, ((value & 0xFFF) ^ 0x800) - 0x800)]


def replay(start: int, length: int, *, load: bool) -> int:
    """Encode the Tensix REPLAY that records the next length instructions at start, or plays them back."""
    return (0x04 << 24) | (start << 14) | (length << 4) | int(load)


def clock_delta(result: int) -> list[int]:
    """Return code that reads the wall clock twice in a row, stores the difference at result, and stops."""
    return [
        lui(T3, WALL_CLOCK_0),
        lw(A0, T3, WALL_CLOCK_0 & 0xFFF),
        lw(A1, T3, WALL_CLOCK_0 & 0xFFF),
        sub(A0, A1, A0),
        *li(T2, result),
        sw(A0, T2, 0),
        JUMP_TO_SELF,
    ]


def tensix_busy() -> list[int]:
    """Return BRISC code that queues REPLAY_LEN DMANOPs at once, so they execute over the next steps.

    One push enters the FIFO and executes in the same step, so pushes alone never leave work for the next step. The
    replay buffer instead records the DMANOPs without executing them, and one REPLAY fills the FIFO with all of them.
    """
    return [
        lui(T0, TENSIX_INST),
        *li(T1, replay(0, REPLAY_LEN, load=True)),
        sw(T1, T0, 0),
        lui(T1, DMANOP),
        *[sw(T1, T0, 0)] * REPLAY_LEN,
        *li(T1, replay(0, REPLAY_LEN, load=False)),
        sw(T1, T0, 0),
    ]


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
        # NOC0 coordinates (x | y << 6) of an eth tile and a Tensix tile; single-chip builds leave their cores in reset
        self.eth = 1 | ((0 if self.wormhole else 1) << 6)
        self.tensix = 1 | ((1 if self.wormhole else 2) << 6)

    def map(self, coord: int, addr: int) -> int:
        """Point TLB 0 at the window holding addr in the tile at coord and return the BAR0 address of addr."""
        if self.wormhole:  # 1 MiB windows, one 64-bit config each
            self.write(BAR0_BASE + TLB_CFG, (addr >> 20) | (coord << 16), 8)
            return BAR0_BASE + (addr & 0xFFFFF)
        # 2 MiB windows, three 32-bit configs each
        self.write(BAR0_BASE + TLB_CFG, (addr >> 21) & 0xFFFFFFFF, 4)
        self.write(BAR0_BASE + TLB_CFG + 4, ((addr >> 53) & 0x7FF) | (coord << 11), 4)
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

    def tile_write32(self, coord: int, addr: int, value: int) -> None:
        """Write 32 bits at addr in the tile at coord."""
        self.write(self.map(coord, addr), value, 4)

    def tile_read32(self, coord: int, addr: int) -> int:
        """Read 32 bits at addr in the tile at coord."""
        return self.read32(self.map(coord, addr))

    def load(self, coord: int, addr: int, program: list[int]) -> None:
        """Write program to the tile at coord, starting at addr."""
        for i, inst in enumerate(program):
            self.tile_write32(coord, addr + 4 * i, inst)

    def ticks(self, clocks: int) -> int:
        """Return the wall clock ticks, as the host reads them, taken by one libttsim_clock call of clocks clocks."""
        start = self.tile_read32(self.eth, WALL_CLOCK_0)
        self.lib.libttsim_clock(clocks)
        return (self.tile_read32(self.eth, WALL_CLOCK_0) - start) & 0xFFFFFFFF

    def eth_delta(self) -> tuple[int, int]:
        """Run clock_delta on the eth core while it is busy; return the difference it stored and the ticks taken."""
        # Blackhole starts only from a programmed nonzero reset PC
        program = 0 if self.wormhole else 0x1000
        self.tile_write32(self.eth, ETH_RESULT, SENTINEL)
        # A host write to eth sram makes the core busy, and the program runs within the next few clocks
        self.load(self.eth, program, clock_delta(ETH_RESULT))
        if not self.wormhole:
            self.tile_write32(self.eth, BH_IERISC_RESET_PC, program)
        self.tile_write32(self.eth, SOFT_RESET_0, 0)
        ticks = self.ticks(RUN_CLOCKS)
        return self.tile_read32(self.eth, ETH_RESULT), ticks

    def tensix_delta(self) -> tuple[int, int]:
        """Run tensix_busy, then clock_delta, on BRISC from 0; return the difference and the ticks taken."""
        self.tile_write32(self.tensix, TENSIX_RESULT, SENTINEL)
        self.load(self.tensix, 0, [*tensix_busy(), *clock_delta(TENSIX_RESULT)])
        self.tile_write32(self.tensix, SOFT_RESET_0, BRISC_ONLY)
        ticks = self.ticks(RUN_CLOCKS)
        return self.tile_read32(self.tensix, TENSIX_RESULT), ticks


def main() -> None:
    """Run the checks on the library named on the command line."""
    chip = Chip(sys.argv[1])
    eth, eth_ticks = chip.eth_delta()
    tensix, tensix_ticks = chip.tensix_delta()
    clocks = chip.ticks(CLOCKS)
    print(
        f"{sys.argv[1]}: back-to-back wall clock reads differ by {eth} on a busy eth core "
        f"and by {tensix} on BRISC while Tensix executes; {RUN_CLOCKS} busy clocks take {eth_ticks} ticks "
        f"on eth and {tensix_ticks} on Tensix; {CLOCKS} idle clocks take {clocks} ticks"
    )
    if SENTINEL in (eth, tensix):
        sys.exit("a program did not store its result")
    if eth != 1:
        sys.exit("a busy eth core steps once per clock, so its back-to-back reads must differ by one")
    if tensix != 1:
        sys.exit("a Tensix tile steps once per clock while it executes Tensix instructions, so BRISC's reads must too")
    if eth_ticks != RUN_CLOCKS or tensix_ticks != RUN_CLOCKS:
        sys.exit("busy cores step once per clock, so the wall clock must advance by one per clock while they run")
    if clocks != CLOCKS:
        sys.exit("the wall clock counts clocks, so it must advance by the number of clocks issued")


if __name__ == "__main__":
    main()
