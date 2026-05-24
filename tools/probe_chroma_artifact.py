#!/usr/bin/env python3
"""Probe where the high-frequency chroma "noise" inside coloured SMS
sprites is coming from.

Strategy:
  1. Load the SMS CVBS capture.
  2. Re-do the v8 sync detection to locate a specific line containing
     the heart-sprite region.
  3. Walk that line through each decoder stage and dump the signal:
       raw CVBS  →  chroma BPF  →  C × cos(LO)  →  U LPF  →  U value
                                  →  C × sin(LO)  →  V LPF  →  V value
                 →  luma LPF      →  Y value
  4. Plot all stages on the same horizontal axis. Look for where the
     oscillation appears.

We compare against the hacktv synthetic colour bars line for sanity.
"""

import sys
import numpy as np
from scipy.signal import firwin, oaconvolve
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fs = 24e6
fSC = 4433618.75

# Same FIR filters as cvbs_decode
NTAP = 65
luma_lpf   = firwin(NTAP, 3.0e6 / (fs/2), window='hamming').astype(np.float32)
chroma_bpf = firwin(NTAP, [3.5e6 / (fs/2), 5.5e6 / (fs/2)],
                    pass_zero=False, window='hamming').astype(np.float32)
uv_lpf     = firwin(NTAP, 1.5e6 / (fs/2), window='hamming').astype(np.float32)
sync_lpf   = firwin(33, 1.0e6 / (fs/2), window='hamming').astype(np.float32)

HSYNC_S, BACK_PORCH_S, LINE_S = 4.7e-6, 5.7e-6, 64.0e-6
sync_skip_samp = int(round((HSYNC_S + BACK_PORCH_S) * fs))
active_samples = int(round(52.0e-6 * fs))


def find_hsync_edges(x):
    xs = oaconvolve(x, sync_lpf, mode='same').astype(np.float32)
    thr = 0.5 * (np.percentile(xs, 0.5) + np.percentile(xs, 30))
    is_sync = xs < thr
    d = np.diff(is_sync.astype(np.int8))
    starts = np.where(d == 1)[0] + 1
    stops  = np.where(d == -1)[0] + 1
    if stops[0] < starts[0]: stops = stops[1:]
    n = min(len(starts), len(stops))
    starts = starts[:n]; stops = stops[:n]
    durs = stops - starts
    H_MIN = int(round(3.5e-6 * fs))
    H_MAX = int(round(6.0e-6 * fs))
    return starts[(durs >= H_MIN) & (durs <= H_MAX)]


def analyse(src, label, target_line_idx, x_window=(0, 1300)):
    """Pull one line out of `src`, run it through decode stages, plot."""
    x = np.fromfile(src, dtype="<i2").astype(np.float32)
    x = x[int(0.1 * fs):]
    edges = find_hsync_edges(x)
    print(f"[{label}] {len(x)} samples, {len(edges)} H-sync edges")

    # Global LO (same approach as cvbs_decode)
    t = np.arange(len(x), dtype=np.float64) / fs
    phi_lo = 2 * np.pi * fSC * t
    lo_cos = np.cos(phi_lo).astype(np.float32)
    lo_sin = np.sin(phi_lo).astype(np.float32)

    # Y / C via BPF mode
    Y_full = oaconvolve(x, luma_lpf, mode='same').astype(np.float32)
    C_full = oaconvolve(x, chroma_bpf, mode='same').astype(np.float32)

    # Synchronous demod + UV LPF
    raw_U_full = oaconvolve(C_full * lo_cos, uv_lpf, mode='same').astype(np.float32)
    raw_V_full = oaconvolve(C_full * lo_sin, uv_lpf, mode='same').astype(np.float32)

    # Pull the target line
    edge = int(edges[target_line_idx])
    burst_s = edge + int(5.4e-6 * fs)
    burst_e = edge + int(7.9e-6 * fs)
    a_s = edge + sync_skip_samp
    a_e = a_s + active_samples

    # Per-line burst measurement (against global LO)
    burst_cos = float(raw_U_full[burst_s:burst_e].mean())
    burst_sin = float(raw_V_full[burst_s:burst_e].mean())
    phi_line = float(np.arctan2(burst_sin, burst_cos))
    # Assume NTSC-style for this line; ψ = 135° - φ. (Half the time this
    # will be wrong by 90° — doesn't matter for the diagnostic of HF noise.)
    psi = np.radians(135.0) - phi_line
    cos_p, sin_p = float(np.cos(psi)), float(np.sin(psi))

    raw_active = x[a_s:a_e]
    C_active = C_full[a_s:a_e]
    Y_active = Y_full[a_s:a_e]
    rU = raw_U_full[a_s:a_e]
    rV = raw_V_full[a_s:a_e]
    U = cos_p * rU - sin_p * rV
    V = sin_p * rU + cos_p * rV

    # ----- plot -----
    samp_axis = np.arange(active_samples)
    # zoom: roughly the sprite region in samples
    s0, s1 = x_window

    fig, axes = plt.subplots(7, 1, figsize=(14, 13), sharex=True)
    fig.suptitle(f"{label} — line idx {target_line_idx} "
                 f"(active region, samples {s0}..{s1})", fontsize=11)

    axes[0].plot(samp_axis[s0:s1], raw_active[s0:s1], color='black', lw=0.6)
    axes[0].set_ylabel("raw CVBS")
    axes[0].axhline(0, color='gray', lw=0.3)

    axes[1].plot(samp_axis[s0:s1], Y_active[s0:s1], color='gray', lw=0.6)
    axes[1].set_ylabel("Y (LPF 3 MHz)")

    axes[2].plot(samp_axis[s0:s1], C_active[s0:s1], color='purple', lw=0.6)
    axes[2].set_ylabel("C (BPF 3.5-5.5 MHz)")
    axes[2].axhline(0, color='gray', lw=0.3)

    # Pre-LPF demod products
    pre_U = (C_full[a_s:a_e] * lo_cos[a_s:a_e]).astype(np.float32)
    pre_V = (C_full[a_s:a_e] * lo_sin[a_s:a_e]).astype(np.float32)
    axes[3].plot(samp_axis[s0:s1], pre_U[s0:s1], color='blue', lw=0.4, alpha=0.7,
                 label='C·cos')
    axes[3].plot(samp_axis[s0:s1], pre_V[s0:s1], color='red', lw=0.4, alpha=0.7,
                 label='C·sin')
    axes[3].set_ylabel("C·LO (pre-LPF)")
    axes[3].legend(loc='upper right', fontsize=8)
    axes[3].axhline(0, color='gray', lw=0.3)

    axes[4].plot(samp_axis[s0:s1], rU[s0:s1], color='blue', lw=0.8, label='raw_U')
    axes[4].plot(samp_axis[s0:s1], rV[s0:s1], color='red', lw=0.8, label='raw_V')
    axes[4].set_ylabel("after UV LPF\n(LO-frame)")
    axes[4].legend(loc='upper right', fontsize=8)
    axes[4].axhline(0, color='gray', lw=0.3)

    axes[5].plot(samp_axis[s0:s1], U[s0:s1], color='blue', lw=0.8, label='U_tx')
    axes[5].plot(samp_axis[s0:s1], V[s0:s1], color='red', lw=0.8, label='V_tx')
    axes[5].set_ylabel("after rotation\n(true U,V)")
    axes[5].legend(loc='upper right', fontsize=8)
    axes[5].axhline(0, color='gray', lw=0.3)

    # FFT of chroma in the zoom window — what frequencies are there?
    seg = C_active[s0:s1].astype(np.float64)
    seg = seg - seg.mean()
    win = np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg * win))
    freqs = np.fft.rfftfreq(len(seg), 1.0/fs) / 1e6
    axes[6].plot(freqs, 20*np.log10(spec + 1e-9), color='purple', lw=0.8)
    axes[6].set_ylabel("|FFT(C)| dB")
    axes[6].set_xlabel("frequency (MHz)")
    axes[6].axvline(fSC/1e6, color='red', lw=0.5, linestyle='--', alpha=0.7,
                    label='fSC')
    axes[6].set_xlim(0, 8)
    axes[6].legend(loc='upper right', fontsize=8)
    axes[6].grid(alpha=0.3, linestyle=':')

    out = f"/tmp/probe_{label}.png"
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    print(f"[{label}] wrote {out}")


if __name__ == "__main__":
    # SMS Wonder Boy — try a line that has the heart sprite
    # (Row 100 in the 574-row image ≈ line 17 of the 287-active lines in field 0,
    #  but H-sync indices include VBI, so total offset is ~17 + 17 VBI = ~34.
    #  Tweak target_line_idx empirically.)
    analyse("/tmp/cvbs_colour.s16", "sms",
            target_line_idx=50, x_window=(700, 1100))
    analyse("/tmp/cvbs_colour.s16", "sms_far_left",
            target_line_idx=50, x_window=(50, 450))
    # hacktv colour bars as the reference (should be CLEAN)
    analyse("/tmp/synth_pal_bars.s16", "hacktv",
            target_line_idx=50, x_window=(50, 1100))
