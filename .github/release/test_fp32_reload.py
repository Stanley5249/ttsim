"""Exercise LLK's TEN-3868 workaround with a multi-block fp32 partial-sum reload.

Run in a tt-metal/ttnn environment with TT_METAL_SIMULATOR pointing at the
candidate library and its SOC descriptor. No hardware or model weights are used.
PROBE_FLAGS unset enables both flags; fp32 and packer isolate each flag.
"""

import os

import torch  # pyrefly: ignore[missing-import]
import ttnn  # pyrefly: ignore[missing-import]

assert os.environ.get("TT_METAL_SIMULATOR")
mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))
mode = os.environ.get("PROBE_FLAGS", "both")
cfg = ttnn.WormholeComputeKernelConfig(
    math_fidelity=ttnn.MathFidelity.HiFi2, fp32_dest_acc_en=mode != "packer", packer_l1_acc=mode != "fp32"
)
a = ttnn.from_torch(torch.randn(1, 1, 32, 128), dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=mesh)
b = ttnn.from_torch(torch.randn(1, 1, 128, 64), dtype=ttnn.bfloat8_b, layout=ttnn.TILE_LAYOUT, device=mesh)
pc = ttnn.MatmulMultiCoreReuseMultiCastProgramConfig(
    compute_with_storage_grid_size=(1, 1),
    in0_block_w=1,
    out_subblock_h=1,
    out_subblock_w=1,
    per_core_M=1,
    per_core_N=2,
    transpose_mcast=False,
    fused_activation=None,
    fuse_batch=True,
)
y = ttnn.linear(a, b, dtype=ttnn.bfloat16, compute_kernel_config=cfg, program_config=pc)
result = ttnn.to_torch(y, mesh_composer=ttnn.ConcatMeshToTensor(mesh, dim=0))
assert result.shape == (1, 1, 32, 64)
assert torch.isfinite(result).all()
ttnn.close_mesh_device(mesh)
