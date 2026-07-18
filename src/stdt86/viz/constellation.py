from __future__ import annotations

from pathlib import Path

import numpy as np

from stdt86.dsp.qam import LEVELS


def plot_constellation(
    symbols: np.ndarray,
    out_path: str | Path,
    title: str = "16QAM constellation",
    evm: float | None = None,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.scatter(symbols.real, symbols.imag, s=4, alpha=0.35, color="tab:blue")
    grid_i, grid_q = np.meshgrid(LEVELS, LEVELS)
    ax.scatter(grid_i.ravel(), grid_q.ravel(), marker="x", color="tab:red", s=60)

    lim = 1.6
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal")
    ax.axhline(0, color="gray", lw=0.5)
    ax.axvline(0, color="gray", lw=0.5)
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("In-phase")
    ax.set_ylabel("Quadrature")
    subtitle = title if evm is None else f"{title}  (EVM={evm:.2f}%)"
    ax.set_title(subtitle)

    fig.tight_layout()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
