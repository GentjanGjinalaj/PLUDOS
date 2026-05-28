"""
1D-CNN autoencoder anomaly labelling for PLUDOS MOVING telemetry (T3.3).

Replaces the LSTM autoencoder. Architecture (~6 K params vs LSTM ~18 K) is
faster on Jetson CPU and matches the inductive bias of vibration diagnostics:
bearing faults are local frequency content, not long-range temporal sequences
(Zhang et al. 2017 WDCNN, Wen et al. 2018).

T3.5: 6 raw axes only (accel xyz + gyro xyz). accel_mag excluded — the conv
kernel learns any cross-channel combination it needs.
T3.1: Welford running stats persisted across FL rounds (cnn_feature_stats.npz).
T3.2: IDLE-baseline threshold = mean(idle_loss) + ANOMALY_K * std(idle_loss).
"""

import json
import logging
import os
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# T3.5: 6 raw channels — no accel_mag.
CNN_FEATURES = ["accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z"]

# Config — mirrors LSTM env-var naming for parity.
CNN_WINDOW_SIZE           = int(os.getenv("CNN_WINDOW_SIZE",           "50"))
CNN_EPOCHS                = int(os.getenv("CNN_EPOCHS",                "20"))
CNN_BATCH_SIZE            = int(os.getenv("CNN_BATCH_SIZE",            "32"))
CNN_LR                    = float(os.getenv("CNN_LR",                  "1e-3"))
CNN_MIN_MOVING_SAMPLES    = int(os.getenv("CNN_MIN_MOVING_SAMPLES",    "200"))
CNN_FEATURE_STATS_BURN_IN = int(os.getenv("CNN_FEATURE_STATS_BURN_IN", "200"))
CNN_FEATURE_STATS_FREEZE  = int(os.getenv("CNN_FEATURE_STATS_FREEZE",  "10000"))
CNN_MIN_IDLE_WINDOWS      = int(os.getenv("CNN_MIN_IDLE_WINDOWS",      "5"))
ANOMALY_K                 = float(os.getenv("ANOMALY_K",               "3.0"))


# ---------------------------------------------------------------------------
# T3.1: Welford running-stats helpers (persisted as cnn_feature_stats.npz)
# ---------------------------------------------------------------------------

def _load_feature_stats(state_dir: Path, n_features: int) -> tuple[int, np.ndarray, np.ndarray]:
    """Load Welford (count, mean, M2) from disk; returns zeros-initialised tuple if missing."""
    path = state_dir / "cnn_feature_stats.npz"
    if path.exists():
        try:
            d = np.load(path)
            return int(d["count"]), d["mean"].astype(np.float64), d["M2"].astype(np.float64)
        except Exception as exc:
            logger.warning("[CNN] feature stats load failed (%s) — starting fresh", exc)
    return 0, np.zeros(n_features, np.float64), np.zeros(n_features, np.float64)


def _update_feature_stats(
    count: int, run_mean: np.ndarray, run_M2: np.ndarray, X: np.ndarray
) -> tuple[int, np.ndarray, np.ndarray]:
    """Chan's parallel Welford update — merges (count, run_mean, run_M2) with batch X (N, D)."""
    X    = X.astype(np.float64)
    n_b  = len(X)
    m_b  = X.mean(axis=0)
    ss_b = ((X - m_b) ** 2).sum(axis=0)
    if count == 0:
        return n_b, m_b, ss_b
    delta = m_b - run_mean
    n_ab  = count + n_b
    return (
        n_ab,
        run_mean + delta * (n_b / n_ab),
        run_M2 + ss_b + delta**2 * (count * n_b / n_ab),
    )


def _save_feature_stats(state_dir: Path, count: int, mean: np.ndarray, M2: np.ndarray) -> None:
    """Persist Welford state; best-effort — failures are logged and never fatal."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        np.savez(state_dir / "cnn_feature_stats.npz", count=count, mean=mean, M2=M2)
    except Exception as exc:
        logger.warning("[CNN] feature stats save failed: %s", exc)


# ---------------------------------------------------------------------------
# T3.2: IDLE-baseline threshold helpers
# ---------------------------------------------------------------------------

def _load_anomaly_threshold(state_dir: Path) -> float | None:
    """Load persisted IDLE-baseline threshold; returns None if missing or unreadable."""
    path = state_dir / "cnn_anomaly_thresholds.json"
    if path.exists():
        try:
            val = json.loads(path.read_text()).get("cnn_global")
            return float(val) if val is not None else None
        except Exception:
            pass
    return None


def _save_anomaly_threshold(state_dir: Path, threshold: float) -> None:
    """Persist IDLE-baseline threshold; best-effort."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "cnn_anomaly_thresholds.json").write_text(
            json.dumps({"cnn_global": threshold})
        )
    except Exception as exc:
        logger.warning("[CNN] threshold save failed: %s", exc)


# ---------------------------------------------------------------------------
# Main labeller
# ---------------------------------------------------------------------------

def make_anomaly_labels_cnn(
    df_clean: pd.DataFrame,
    state_dir: Path,
    if_fallback_fn: Callable[[pd.DataFrame], tuple[np.ndarray, str]],
    if_contamination: float,
) -> tuple[np.ndarray, str]:
    """
    Train a 1D-CNN autoencoder on sliding windows of MOVING packets and label
    high-reconstruction-error windows as anomalous.

    Falls back to IsolationForest when MOVING data < CNN_MIN_MOVING_SAMPLES.
    Returns (labels, labeller_name).
    """
    try:
        import torch
        import torch.nn as nn
    except ImportError:
        logger.error("[CNN] torch not installed — falling back to IsolationForest")
        return if_fallback_fn(df_clean)

    moving_mask = df_clean["state"].astype(int) == 1
    n_moving    = int(moving_mask.sum())

    if n_moving < CNN_MIN_MOVING_SAMPLES:
        logger.warning(
            "[CNN] only %d MOVING samples (need %d) — falling back to IsolationForest",
            n_moving, CNN_MIN_MOVING_SAMPLES,
        )
        return if_fallback_fn(df_clean)

    available = [c for c in CNN_FEATURES if c in df_clean.columns]
    X_moving  = df_clean.loc[moving_mask, available].values.astype(np.float32)  # (N, D)

    stride = max(1, CNN_WINDOW_SIZE // 2)
    starts = list(range(0, n_moving - CNN_WINDOW_SIZE + 1, stride))
    if not starts:
        logger.warning("[CNN] not enough packets for even one window — falling back")
        return if_fallback_fn(df_clean)

    windows = np.stack([X_moving[s:s + CNN_WINDOW_SIZE] for s in starts])  # (W, T, D)

    # T3.1: global Welford normalization — load, update unless frozen, persist.
    n_features         = len(available)
    count, run_mean, run_M2 = _load_feature_stats(state_dir, n_features)
    if count < CNN_FEATURE_STATS_FREEZE:
        count, run_mean, run_M2 = _update_feature_stats(
            count, run_mean, run_M2, windows.reshape(-1, n_features)
        )
        _save_feature_stats(state_dir, count, run_mean, run_M2)
    if count >= CNN_FEATURE_STATS_BURN_IN:
        norm_std = np.sqrt(run_M2 / count) + 1e-8   # (D,)
        X_norm   = (windows - run_mean) / norm_std
    else:
        logger.info("[CNN] stats burn-in %d/%d — using batch normalization",
                    count, CNN_FEATURE_STATS_BURN_IN)
        run_mean = windows.mean(axis=(0, 1))
        norm_std = windows.std(axis=(0, 1)) + 1e-8
        X_norm   = (windows - run_mean) / norm_std

    # Conv1d expects (B, C, T) — permute from (W, T, D).
    tensor = torch.tensor(X_norm).permute(0, 2, 1)

    class _CNNAutoencoder(nn.Module):
        """Encoder halves T twice (stride=2); decoder restores it. ~6 K params total."""

        def __init__(self, n_feat: int) -> None:
            super().__init__()
            self.encoder = nn.Sequential(
                nn.Conv1d(n_feat, 16, kernel_size=5, stride=1, padding=2), nn.ReLU(),
                nn.Conv1d(16,     32, kernel_size=5, stride=2, padding=2), nn.ReLU(),
                nn.Conv1d(32,     16, kernel_size=3, stride=2, padding=1),
            )
            # output_padding=1 on the second transpose restores T=50 exactly.
            self.decoder = nn.Sequential(
                nn.ConvTranspose1d(16, 32, kernel_size=3, stride=2, padding=1),
                nn.ReLU(),
                nn.ConvTranspose1d(32, 16, kernel_size=5, stride=2, padding=2, output_padding=1),
                nn.ReLU(),
                nn.ConvTranspose1d(16, n_feat, kernel_size=5, stride=1, padding=2),
            )

        def forward(self, x: "torch.Tensor") -> "torch.Tensor":
            return self.decoder(self.encoder(x))

    model     = _CNNAutoencoder(n_features)
    optimizer = torch.optim.Adam(model.parameters(), lr=CNN_LR)
    criterion = nn.MSELoss()

    dataset = torch.utils.data.TensorDataset(tensor)
    loader  = torch.utils.data.DataLoader(
        dataset, batch_size=CNN_BATCH_SIZE, shuffle=True, drop_last=False,
    )

    logger.info(
        "[CNN] training autoencoder — %d windows, T=%d, D=%d, epochs=%d",
        len(starts), CNN_WINDOW_SIZE, n_features, CNN_EPOCHS,
    )
    model.train()
    for epoch in range(CNN_EPOCHS):
        epoch_loss = 0.0
        for (batch,) in loader:
            optimizer.zero_grad()
            loss = criterion(model(batch), batch)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
        if (epoch + 1) % 5 == 0:
            logger.info("[CNN] epoch %02d/%d loss=%.4f",
                        epoch + 1, CNN_EPOCHS, epoch_loss / len(loader))

    # Per-window MSE — average over channels and time.
    model.eval()
    with torch.no_grad():
        recon  = model(tensor)
        errors = ((tensor - recon) ** 2).mean(dim=(1, 2)).numpy()  # (W,)

    # Map window scores back to per-packet by averaging over all overlapping windows.
    packet_scores = np.zeros(n_moving, dtype=np.float32)
    packet_counts = np.zeros(n_moving, dtype=np.float32)
    for i, s in enumerate(starts):
        packet_scores[s:s + CNN_WINDOW_SIZE] += errors[i]
        packet_counts[s:s + CNN_WINDOW_SIZE] += 1.0
    covered    = packet_counts > 0
    avg_scores = np.where(covered, packet_scores / np.maximum(packet_counts, 1), 0.0)

    # T3.2: IDLE-baseline threshold.
    idle_mask = df_clean["state"].astype(int) == 0
    n_idle    = int(idle_mask.sum())
    threshold = None

    if n_idle >= CNN_WINDOW_SIZE * CNN_MIN_IDLE_WINDOWS:
        X_idle      = df_clean.loc[idle_mask, available].values.astype(np.float32)
        idle_starts = list(range(0, len(X_idle) - CNN_WINDOW_SIZE + 1, stride))
        if len(idle_starts) >= CNN_MIN_IDLE_WINDOWS:
            idle_win  = np.stack([X_idle[s:s + CNN_WINDOW_SIZE] for s in idle_starts])
            idle_norm = (idle_win - run_mean) / norm_std
            t_idle    = torch.tensor(idle_norm).permute(0, 2, 1)
            with torch.no_grad():
                idle_err = ((t_idle - model(t_idle)) ** 2).mean(dim=(1, 2)).numpy()
            threshold = float(idle_err.mean() + ANOMALY_K * idle_err.std()) + 1e-8
            _save_anomaly_threshold(state_dir, threshold)
            logger.info(
                "[CNN] IDLE-baseline threshold=%.6f (n=%d idle windows, K=%.1f)",
                threshold, len(idle_starts), ANOMALY_K,
            )

    if threshold is None:
        saved = _load_anomaly_threshold(state_dir)
        if saved is not None:
            threshold = saved
            logger.info("[CNN] using persisted threshold=%.6f (no IDLE data this round)", threshold)
        else:
            threshold = float(
                np.percentile(avg_scores[covered], 100.0 * (1.0 - if_contamination))
            )
            logger.warning("[CNN] no IDLE data and no persisted threshold — percentile fallback")

    moving_labels = (avg_scores >= threshold).astype(int)
    n_anomalous   = int(moving_labels.sum())
    logger.info(
        "[CNN] %d/%d MOVING packets flagged anomalous (%.1f%%), threshold=%.6f",
        n_anomalous, n_moving, 100.0 * n_anomalous / n_moving, threshold,
    )

    # moving_mask.values maps moving_labels[i] to the i-th True position in the full df.
    y = np.zeros(len(df_clean), dtype=int)
    y[moving_mask.values] = moving_labels
    return y, "cnn_autoencoder"
