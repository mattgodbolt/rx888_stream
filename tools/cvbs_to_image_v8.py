#!/usr/bin/env python3
"""PAL colour decoder v8 — fixes from v7 review.

Bugs fixed (per subagent code review of v7):

  1. fSC is no longer auto-detected from spectrum peak. v7 picked 4.457 MHz
     (a chroma sideband from the bright colour bars), off by ~23 kHz from
     the textbook 4.43361875 MHz. That error caused the demod LO to wind
     ~one full turn per active line, so the per-line θ correction only
     zeroed phase at the back-porch burst; chroma at the right edge of
     each line was rotated >360° past the compensation. The vertical
     comb then averaged the smeared mess to a single (mostly green) hue.

  2. Per-line rotation is now class-aware. v7 used θ = phi + 135° for
     every line, assuming burst always sits at +135° in true (U, V)
     space. PAL burst alternates between -U+V (= 135° in modulated
     frame) and -U-V (= -135°), so v7's formula was correct on one
     class and 90° off on the other — combined with the V-flip step
     this gave U/V axis confusion on half the lines.

  3. The two-fold class-assignment ambiguity (which class is the V-non-
     inverted "NTSC-style" line and which is the V-inverted "PAL-style"
     line) is resolved automatically by picking the hypothesis that
     produces self-consistent ψ across the two classes. The TRUE
     hypothesis gives ψ_α ≈ ψ_β (since the LO-vs-subcarrier phase is a
     single physical quantity per moment); the wrong hypothesis makes
     them differ by 180°.

  4. Removed v7's dead code: per-class mean-burst-phase computation that
     was computed but never used in rotation.

Pipeline:
  - sync separator (LPF first to ignore chroma) → broad-pulse V-sync
    detection → per-line H-sync edge times
  - exact fSC LO (no auto-detect) → Y/C separation by frequency filters
  - synchronous demod with the LO → raw_U, raw_V everywhere
  - per-line burst measurement → per-line LO-vs-subcarrier phase ψ
  - per-line rotation by +ψ recovers (U_tx, V_modulated)
  - V flip on V-inverted lines recovers V_tx
  - 1H comb filter on U, V → final YUV → RGB → image
"""

import sys
import numpy as np
from scipy.signal import firwin, oaconvolve
from PIL import Image

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 24e6
which_field = int(sys.argv[4]) if len(sys.argv) > 4 else 0
fsc_override = float(sys.argv[5]) if len(sys.argv) > 5 else 4433618.75

HSYNC_S       = 4.7e-6
BACK_PORCH_S  = 5.7e-6
ACTIVE_S      = 52.0e-6
LINE_S        = 64.0e-6
ACTIVE_LINES  = 287
VBI_LINES_PAL = 17

BURST_START_S = 5.4e-6
BURST_END_S   = 7.9e-6

sync_skip_samp   = int(round((HSYNC_S + BACK_PORCH_S) * fs))
active_samples   = int(round(ACTIVE_S * fs))
burst_start_samp = int(round(BURST_START_S * fs))
burst_end_samp   = int(round(BURST_END_S * fs))


# 1. load CVBS
with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"loaded {len(x)} samples = {len(x)/fs:.2f} s", flush=True)


# 2. fSC — textbook value, not auto-detect
fSC = fsc_override
print(f"fSC = {fSC/1e6:.6f} MHz (fixed, textbook)", flush=True)


# 3. sync separator with chroma rejection LPF before slicing
sync_filter_lpf = firwin(33, 1.0e6 / (fs/2), window='hamming').astype(np.float32)
x_for_sync = oaconvolve(x, sync_filter_lpf, mode='same').astype(np.float32)
sync_tip = float(np.percentile(x_for_sync, 0.5))
black    = float(np.percentile(x_for_sync, 30.0))
sync_thr = 0.5 * (sync_tip + black)
is_sync = x_for_sync < sync_thr

d = np.diff(is_sync.astype(np.int8))
starts = np.where(d == 1)[0] + 1
stops  = np.where(d == -1)[0] + 1
if stops[0] < starts[0]:
    stops = stops[1:]
n = min(len(starts), len(stops))
starts = starts[:n]; stops = stops[:n]
durs = stops - starts

H_MIN = int(round(3.5e-6 * fs))
H_MAX = int(round(6.0e-6 * fs))
B_MIN = int(round(15.0e-6 * fs))
is_hsync = (durs >= H_MIN) & (durs <= H_MAX)
is_broad = durs >= B_MIN

line_samp = int(LINE_S * fs)
vsync_anchors = []
i = 0
while i < n:
    if is_broad[i]:
        gs = i; last = i; j = i + 1
        while j < n and is_broad[j] and (starts[j] - starts[last]) < 1.5 * line_samp:
            last = j; j += 1
        if (last - gs + 1) >= 3:
            vsync_anchors.append(gs)
        i = j
    else:
        i += 1
if which_field >= len(vsync_anchors):
    sys.exit(f"requested field {which_field} but only {len(vsync_anchors)} V-syncs")

vs_idx = vsync_anchors[which_field]
j = vs_idx
while j + 1 < n and is_broad[j + 1] and (starts[j+1] - starts[j]) < 1.5 * line_samp:
    j += 1
vs_end_idx = j

hsync_indices = []
prev = None
k = vs_end_idx + 1
while k < n and len(hsync_indices) < VBI_LINES_PAL + ACTIVE_LINES + 10:
    if is_hsync[k]:
        if prev is None:
            hsync_indices.append(k); prev = k
        else:
            gap = starts[k] - starts[prev]
            if 0.9 * line_samp < gap < 1.1 * line_samp:
                hsync_indices.append(k); prev = k
            elif gap > 1.5 * line_samp:
                hsync_indices.append(k); prev = k
    k += 1
hsync_active = hsync_indices[VBI_LINES_PAL: VBI_LINES_PAL + ACTIVE_LINES]
print(f"using {len(hsync_active)} active lines", flush=True)


# 4. continuous LO + Y/C separation
NTAP = 65
luma_lpf   = firwin(NTAP, 3.0e6 / (fs/2), window='hamming').astype(np.float32)
chroma_bpf = firwin(NTAP, [3.5e6 / (fs/2), 5.5e6 / (fs/2)],
                    pass_zero=False, window='hamming').astype(np.float32)
uv_lpf     = firwin(NTAP, 1.5e6 / (fs/2), window='hamming').astype(np.float32)

print("building continuous LO + Y/C separation...", flush=True)
t = np.arange(len(x), dtype=np.float64) / fs
phi_lo = 2 * np.pi * fSC * t
lo_cos = np.cos(phi_lo).astype(np.float32)
lo_sin = np.sin(phi_lo).astype(np.float32)
del phi_lo, t

Y_full = oaconvolve(x, luma_lpf, mode='same').astype(np.float32)
C_full = oaconvolve(x, chroma_bpf, mode='same').astype(np.float32)

print("synchronous demod + LPF...", flush=True)
raw_U_full = oaconvolve(C_full * lo_cos, uv_lpf, mode='same').astype(np.float32)
raw_V_full = oaconvolve(C_full * lo_sin, uv_lpf, mode='same').astype(np.float32)
del lo_cos, lo_sin


# 5. per-line burst measurement
print("measuring per-line burst...", flush=True)
n_lines = len(hsync_active)
burst_cos = np.zeros(n_lines)
burst_sin = np.zeros(n_lines)
for li, pi in enumerate(hsync_active):
    edge = int(starts[pi])
    b_s = edge + burst_start_samp
    b_e = edge + burst_end_samp
    if b_e > len(x): break
    burst_cos[li] = float(raw_U_full[b_s:b_e].mean())
    burst_sin[li] = float(raw_V_full[b_s:b_e].mean())

phi_per_line = np.arctan2(burst_sin, burst_cos)


# 6. class assignment
# PAL alternates strictly per line. Even-index ("α") and odd-index ("β")
# lines fall into the two burst classes. Which one is the V-non-inverted
# "NTSC-style" line and which is V-inverted "PAL-style" is a binary
# question; we resolve it by picking the hypothesis that gives self-
# consistent LO-vs-subcarrier phase ψ across the two classes.
alpha_lines = np.arange(0, n_lines, 2)
beta_lines  = np.arange(1, n_lines, 2)
phi_alpha = float(np.arctan2(burst_sin[alpha_lines].mean(),
                             burst_cos[alpha_lines].mean()))
phi_beta  = float(np.arctan2(burst_sin[beta_lines].mean(),
                             burst_cos[beta_lines].mean()))
print(f"  α (even-index) burst phase: {np.degrees(phi_alpha):7.2f}°", flush=True)
print(f"  β (odd-index)  burst phase: {np.degrees(phi_beta):7.2f}°", flush=True)

R135 = np.radians(135.0)
# H1: α is NTSC (burst at -U+V → 135° in modulated frame, so ψ = 135° - φ)
#     β is PAL  (burst at -U-V → -135°, so ψ = -135° - φ)
psi_H1_alpha = R135 - phi_alpha
psi_H1_beta  = -R135 - phi_beta
diff_H1 = float(np.angle(np.exp(1j*(psi_H1_alpha - psi_H1_beta))))
# H2: α is PAL, β is NTSC
psi_H2_alpha = -R135 - phi_alpha
psi_H2_beta  = R135 - phi_beta
diff_H2 = float(np.angle(np.exp(1j*(psi_H2_alpha - psi_H2_beta))))

print(f"  hyp 1 (α=NTSC, β=PAL): |ψ_α - ψ_β| = {abs(np.degrees(diff_H1)):6.2f}°", flush=True)
print(f"  hyp 2 (α=PAL, β=NTSC): |ψ_α - ψ_β| = {abs(np.degrees(diff_H2)):6.2f}°", flush=True)

if abs(diff_H1) < abs(diff_H2):
    alpha_is_ntsc = True
    print("  → α is NTSC-style (V not inverted)", flush=True)
else:
    alpha_is_ntsc = False
    print("  → α is PAL-style (V inverted)", flush=True)


_DEBUG_BURST_AFTER = True  # set false to silence

# Sanity check: after per-line rotation, the BURST itself should land at
# (U_tx, V_tx) = (-1, +1) for every line (with the V-flip already applied
# on PAL-style lines). Print median to confirm formulas are right.
if _DEBUG_BURST_AFTER:
    burst_U_after = np.zeros(n_lines)
    burst_V_after = np.zeros(n_lines)
    for li in range(n_lines):
        is_ntsc = ((li % 2 == 0) == alpha_is_ntsc)
        psi = (R135 - phi_per_line[li]) if is_ntsc else (-R135 - phi_per_line[li])
        cp, sp = float(np.cos(psi)), float(np.sin(psi))
        bU = cp * burst_cos[li] - sp * burst_sin[li]
        bV_mod = sp * burst_cos[li] + cp * burst_sin[li]
        bV = bV_mod if is_ntsc else -bV_mod
        burst_U_after[li] = bU
        burst_V_after[li] = bV
    med_bU = float(np.median(burst_U_after))
    med_bV = float(np.median(burst_V_after))
    dbg_mag = float(np.median(np.sqrt(burst_cos**2 + burst_sin**2)))
    print(f"  recovered burst (U, V_tx) median = ({med_bU/dbg_mag:+.3f}, "
          f"{med_bV/dbg_mag:+.3f}) × burst_mag", flush=True)
    print(f"    expected: (-1, +1) — i.e. burst at -U+V in true frame", flush=True)


# 7. per-line rotation
rows_Y = []
rows_U = []
rows_V = []
for li, pi in enumerate(hsync_active):
    edge = int(starts[pi])
    a_s = edge + sync_skip_samp
    a_e = a_s + active_samples
    if a_e > len(x): break

    phi_line = phi_per_line[li]
    is_alpha = (li % 2 == 0)
    is_ntsc  = (is_alpha == alpha_is_ntsc)

    if is_ntsc:
        psi = R135 - phi_line          # burst at -U+V → 135° in mod frame
    else:
        psi = -R135 - phi_line         # burst at -U-V → -135° in mod frame

    Y  = Y_full[a_s:a_e]
    rU = raw_U_full[a_s:a_e]
    rV = raw_V_full[a_s:a_e]

    cos_p = float(np.cos(psi))
    sin_p = float(np.sin(psi))
    # Rotation by +ψ: (U_tx, V_mod) = R(ψ) · (raw_U, raw_V)
    U     = cos_p * rU - sin_p * rV
    V_mod = sin_p * rU + cos_p * rV

    # PAL-style line: V_mod = -V_tx, so flip to recover V_tx
    V = V_mod if is_ntsc else -V_mod

    rows_Y.append(Y)
    rows_U.append(U)
    rows_V.append(V)

Y_mat = np.array(rows_Y)
U_mat = np.array(rows_U)
V_mat = np.array(rows_V)


# 8. 1H comb filter (the classic PAL Phase-Alternating-Line trick)
U_comb = U_mat.copy()
V_comb = V_mat.copy()
U_comb[1:] = 0.5 * (U_mat[1:] + U_mat[:-1])
V_comb[1:] = 0.5 * (V_mat[1:] + V_mat[:-1])


# 9. YUV → RGB
y_floor = float(np.percentile(Y_mat, 5))
y_peak  = float(np.percentile(Y_mat, 99))
Y_norm = np.clip((Y_mat - y_floor) / max(y_peak - y_floor, 1.0), 0.0, 1.0)

# Burst is at fixed amplitude in transmitted PAL: 0.3 × (white - blanking).
# After our demod (factor 1/2 from sin·cos product), the recovered raw burst
# has magnitude ≈ 0.15 × (raw luma range). Use the measured median burst
# magnitude as the "unit" against which U,V are scaled. BT.601 expects U,V
# in roughly ±0.5; PAL's max |U|=0.436, max |V|=0.615 for fully-saturated
# primaries. Start with the burst-relative scaling and let the user evaluate.
burst_mag = float(np.median(np.sqrt(burst_cos**2 + burst_sin**2)))
# Burst direction in true (U, V) is at length √2 (it's (-1, ±1)). After rotation,
# the recovered burst vector lies along ±V_mod with magnitude burst_mag (same
# as the raw measurement, just rotated). So one "burst unit" = burst_mag.
# Scale so that this unit maps to about 0.3 (PAL burst nominal level).
uv_scale = 0.3 / max(burst_mag, 1.0)
print(f"  burst_mag={burst_mag:.4f}  uv_scale={uv_scale:.4f}", flush=True)

U_norm = U_comb * uv_scale
V_norm = V_comb * uv_scale

R = Y_norm + 1.13983 * V_norm
G = Y_norm - 0.39465 * U_norm - 0.58060 * V_norm
B = Y_norm + 2.03211 * U_norm

R = np.clip(R * 255, 0, 255).astype(np.uint8)
G = np.clip(G * 255, 0, 255).astype(np.uint8)
B = np.clip(B * 255, 0, 255).astype(np.uint8)
rgb = np.stack([R, G, B], axis=-1)

rgb = np.repeat(rgb, 2, axis=0)
img = Image.fromarray(rgb).resize((720, rgb.shape[0]), Image.LANCZOS)
img.save(dst)
print(f"wrote {dst} ({img.size[0]}×{img.size[1]})", flush=True)
