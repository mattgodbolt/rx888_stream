#!/usr/bin/env python3
"""PAL colour decoder, 1980s TV architecture.

Pipeline (per line, matching what an analog colour TV did):

  1. Sync separator + H-sync edge detection           (from v6)
  2. V-sync via broad-pulse classification            (from v6)
  3. Per-line:
       a. Measure colour burst phase in the back porch (synchronous
          detection against fSC sin/cos)
       b. Y/C separation:
            Y = LPF(CVBS) below 3 MHz
            C = BPF(CVBS) around fSC ± 1 MHz
       c. Demodulate C against the burst-locked LO:
            U =  LPF( C × cos(2π fSC t + burst_phase) )
            V =  LPF( C × sin(2π fSC t + burst_phase) ) × pal_sign
       d. (Optional) Comb-filter U/V vertically — the "Phase
          Alternating Line" trick: average two consecutive lines'
          V to cancel chroma phase errors.
  4. YUV → RGB matrix; save PNG.

Usage:
    cvbs_to_image_v7.py SRC.s16 DST.png [fs [field [fsc_hz]]]

Args:
    SRC.s16:  baseband CVBS, int16 LE. MUST have been demodulated with
              an LPF ≥5.5 MHz to preserve the chroma subcarrier.
    DST:      output image (.png/.jpg via PIL; .ppm raw).
    fs:       sample rate. Default 24e6.
    field:    which detected V-sync to anchor on (try a few). Default 0.
    fsc_hz:   chroma subcarrier frequency. Default 0 = auto-detect from
              spectrum peak in 4-5 MHz.
"""
import sys
import numpy as np
from scipy.signal import firwin, oaconvolve

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 24e6
which_field = int(sys.argv[4]) if len(sys.argv) > 4 else 0
fsc_override = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

# PAL constants
HSYNC_S       = 4.7e-6
BACK_PORCH_S  = 5.7e-6
ACTIVE_S      = 52.0e-6
LINE_S        = 64.0e-6
ACTIVE_LINES        = 287
VBI_LINES_PAL       = 17

# Colour burst is between roughly 5.6 µs and 7.9 µs after the H-sync
# leading edge (≈10 cycles of fSC starting after the back-porch blanking).
BURST_START_S = 5.4e-6
BURST_END_S   = 7.9e-6

sync_skip_samp     = int(round((HSYNC_S + BACK_PORCH_S) * fs))
active_samples     = int(round(ACTIVE_S * fs))
burst_start_samp   = int(round(BURST_START_S * fs))
burst_end_samp     = int(round(BURST_END_S * fs))
burst_n            = burst_end_samp - burst_start_samp

# ---------------------------------------------------------------- load
with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"loaded {len(x)} samples = {len(x)/fs:.2f} s at fs={fs/1e6:g} MHz",
      flush=True)

# ------------------------------------------------- determine chroma fSC
if fsc_override > 0:
    fSC = fsc_override
    print(f"chroma subcarrier (override): {fSC/1e6:.5f} MHz", flush=True)
else:
    # FFT a chunk of the signal, find strongest peak in 4-5 MHz
    N = min(int(1.0 * fs), len(x))
    spec = np.abs(np.fft.rfft((x[:N] - x[:N].mean()) * np.hanning(N).astype(np.float32)))
    fb = np.fft.rfftfreq(N, 1.0 / fs)
    mask = (fb > 4.0e6) & (fb < 5.0e6)
    ipk = int(np.argmax(spec[mask]))
    # Quadratic interpolation for sub-bin precision
    band_idx = np.where(mask)[0]
    abs_idx = band_idx[ipk]
    if 0 < abs_idx < len(spec) - 1:
        y0, ym1, yp1 = spec[abs_idx], spec[abs_idx-1], spec[abs_idx+1]
        denom = (ym1 - 2*y0 + yp1)
        shift = 0.5 * (ym1 - yp1) / denom if denom != 0 else 0.0
        fSC = float(fb[abs_idx] + shift * (fb[1] - fb[0]))
    else:
        fSC = float(fb[abs_idx])
    print(f"chroma subcarrier (auto): {fSC/1e6:.5f} MHz "
          f"(textbook PAL fSC = 4.43362 MHz; this source is slightly slow)",
          flush=True)

# ----------------------------------------------------- sync separator
sync_tip = float(np.percentile(x, 0.5))
black    = float(np.percentile(x, 30.0))
sync_thr = 0.5 * (sync_tip + black)
print(f"sync separator: sync_tip≈{sync_tip:.0f} black≈{black:.0f} "
      f"thr={sync_thr:.0f}", flush=True)
is_sync = x < sync_thr

# Pulse runs
diff = np.diff(is_sync.astype(np.int8))
starts = np.where(diff == 1)[0] + 1
stops  = np.where(diff == -1)[0] + 1
if stops[0] < starts[0]:
    stops = stops[1:]
n = min(len(starts), len(stops))
starts = starts[:n]; stops = stops[:n]
durations = stops - starts

H_MIN = int(round(3.5e-6 * fs))
H_MAX = int(round(6.0e-6 * fs))
B_MIN = int(round(15.0e-6 * fs))
is_hsync = (durations >= H_MIN) & (durations <= H_MAX)
is_broad = durations >= B_MIN
print(f"{n} sync pulses; H={is_hsync.sum()} broad={is_broad.sum()}", flush=True)

# V-sync = run of >=3 consecutive broad pulses
vsync_anchors = []
i = 0
line_samp = int(LINE_S * fs)
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
print(f"V-sync runs detected: {len(vsync_anchors)}", flush=True)
if which_field >= len(vsync_anchors):
    sys.exit(f"requested field {which_field} but only {len(vsync_anchors)} V-syncs")

# Find end of selected V-sync run, then collect ACTIVE_LINES H-syncs
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

if len(hsync_indices) < VBI_LINES_PAL + 50:
    sys.exit("not enough H-syncs after V-sync")
hsync_active = hsync_indices[VBI_LINES_PAL: VBI_LINES_PAL + ACTIVE_LINES]
print(f"using {len(hsync_active)} active-video lines", flush=True)

# ---------------------------------------------- precompute FIR filters
# Luma LPF: cut at 3 MHz (well below chroma's 3.5 MHz lower edge)
NTAP = 65    # short FIR — runs per-line on small slices
luma_lpf  = firwin(NTAP, 3.0e6 / (fs/2), window='hamming').astype(np.float32)
# Chroma BPF: 3.5–5.5 MHz pass band, around fSC
chroma_bpf = firwin(NTAP, [3.5e6 / (fs/2), 5.5e6 / (fs/2)],
                   pass_zero=False, window='hamming').astype(np.float32)
# Post-mix LPF (after multiplying C by LO; recovers baseband U/V)
uv_lpf = firwin(NTAP, 1.3e6 / (fs/2), window='hamming').astype(np.float32)


def filt(line_signal, h):
    """Apply FIR `h` to `line_signal`, return same-length output."""
    return oaconvolve(line_signal, h, mode='same').astype(np.float32)


# ---------------------------------------------- per-line decode
rows_Y = []
rows_U = []
rows_V = []
burst_phases = []

for line_num, pulse_idx in enumerate(hsync_active):
    edge = int(starts[pulse_idx])
    # The "line span" we work on: from H-sync edge to start of next line.
    # We use a slightly oversized slice to give the FIRs room.
    line_end = edge + active_samples + sync_skip_samp
    if line_end + NTAP > len(x):
        break
    line = x[edge : line_end + NTAP]

    # --- 1. Burst phase ---
    burst = line[burst_start_samp : burst_end_samp]
    bn = len(burst)
    # Reference LO: cos(2π fSC t), sin(2π fSC t) at sample indices
    # within the burst slice. t origin = edge of H-sync.
    t_burst = (np.arange(bn) + burst_start_samp) / fs
    ref_cos = np.cos(2 * np.pi * fSC * t_burst, dtype=np.float64)
    ref_sin = np.sin(2 * np.pi * fSC * t_burst, dtype=np.float64)
    Ic = float((burst.astype(np.float64) * ref_cos).sum())
    Qc = float((burst.astype(np.float64) * ref_sin).sum())
    burst_phase = np.arctan2(Qc, Ic)
    burst_amp = np.sqrt(Ic*Ic + Qc*Qc) / max(bn, 1)
    burst_phases.append(burst_phase)

    # --- 2. Active video Y/C separation ---
    active = line[sync_skip_samp : sync_skip_samp + active_samples]
    Y_full = filt(active, luma_lpf)
    C      = filt(active, chroma_bpf)

    # --- 3. Demodulate chroma against the burst-locked LO ---
    # Build LO at sample indices relative to H-sync edge so it stays
    # phase-coherent with the burst.
    t_active = (np.arange(active_samples) + sync_skip_samp) / fs
    lo_cos = np.cos(2 * np.pi * fSC * t_active + burst_phase).astype(np.float32)
    lo_sin = np.sin(2 * np.pi * fSC * t_active + burst_phase).astype(np.float32)

    U_raw = C * lo_cos
    V_raw = C * lo_sin
    U = filt(U_raw, uv_lpf)
    V = filt(V_raw, uv_lpf)

    rows_Y.append(Y_full)
    rows_U.append(U)
    rows_V.append(V)

Y_mat = np.array(rows_Y)
U_mat = np.array(rows_U)
V_mat = np.array(rows_V)
print(f"decoded {Y_mat.shape[0]} lines × {Y_mat.shape[1]} samples each",
      flush=True)
print(f"burst phase range: {min(burst_phases):.3f} .. {max(burst_phases):.3f} rad",
      flush=True)

# ---------------------------------------------- PAL alternation
# In PAL the burst phase alternates ±135° from line to line.
# Determine each line's PAL sign from the burst phase (+135° = +V,
# -135° = -V), then average pairs of consecutive lines to cancel
# phase errors. This is the "Phase Alternating Line" trick.
#
# Most direct: classify each burst by which half-plane its phase
# falls in (sin(phase) > 0 vs < 0).
pal_sign = np.sign(np.sin(burst_phases))   # ±1 per line
pal_sign[pal_sign == 0] = 1
# Apply alternation correction to V
V_mat *= pal_sign[:, None]

# Vertical comb on V: average each line with previous to cancel
# residual chroma phase error (Hanover bars)
V_comb = V_mat.copy()
V_comb[1:] = (V_mat[1:] + V_mat[:-1]) * 0.5
U_comb = U_mat.copy()
U_comb[1:] = (U_mat[1:] + U_mat[:-1]) * 0.5

# ---------------------------------------------- YUV -> RGB
# Normalize Y to roughly [0..1] using back-porch as 0 and sync-clipped
# peak as 1. Use percentiles for robustness.
y_floor = float(np.percentile(Y_mat, 5))
y_peak  = float(np.percentile(Y_mat, 99))
Y_norm = np.clip((Y_mat - y_floor) / max(y_peak - y_floor, 1.0), 0.0, 1.0)

# U,V scaling: divide by twice the average burst amplitude — burst
# corresponds to U=B-Y at ~75% of full chroma amplitude.
# Find a reasonable scale empirically; the burst amplitude IS the
# reference for chroma amplitude in PAL.
chroma_scale = 2.0 / max(float(np.median(np.abs(np.array(burst_phases) * 0) +
                              np.sqrt((np.array([
                                  (np.cos(p)*1, np.sin(p)*1) for p in burst_phases
                              ])**2).sum(axis=1)))), 1.0)
# Simpler: scale U/V to roughly span ±0.5
u_span = max(float(np.percentile(np.abs(U_comb), 99)), 1.0)
v_span = max(float(np.percentile(np.abs(V_comb), 99)), 1.0)
U_norm = U_comb / u_span * 0.436     # 0.436 = max |U| in YUV
V_norm = V_comb / v_span * 0.615     # 0.615 = max |V| in YUV

# Standard PAL YUV -> RGB
R = Y_norm + 1.13983 * V_norm
G = Y_norm - 0.39465 * U_norm - 0.58060 * V_norm
B = Y_norm + 2.03211 * U_norm

R = np.clip(R * 255, 0, 255).astype(np.uint8)
G = np.clip(G * 255, 0, 255).astype(np.uint8)
B = np.clip(B * 255, 0, 255).astype(np.uint8)
rgb = np.stack([R, G, B], axis=-1)

# Double vertically (single field render)
rgb = np.repeat(rgb, 2, axis=0)

# Resize horizontally to 720
target_w = 720
from PIL import Image
img = Image.fromarray(rgb).resize((target_w, rgb.shape[0]), Image.LANCZOS)
img.save(dst)
print(f"wrote {dst} ({img.size[0]}×{img.size[1]})", flush=True)
