#!/usr/bin/env python3
"""PAL colour decoder — single canonical tool.

Pipeline (1980s analog TV architecture, digital implementation):

  1. Sync separator with chroma rejection LPF before threshold slicing.
     Broad-pulse classification finds the V-sync gap; H-sync edges are
     duration-classified pulses between V-syncs.

  2. Y/C separation. Two modes:
       --yc-mode bpf  (frequency split): Y = LPF(x, 3 MHz),
                                         C = BPF(x, 3.5..5.5 MHz).
                                         Cheap, simple, but the BPF
                                         impulse response RINGS after
                                         sharp coloured edges → visible
                                         "echoes" trailing bright sprites.
       --yc-mode comb (2H comb, default): Y = (x + x_2H) / 2,
                                          C = (x - x_2H) / 2.
                                          fSC × 64 µs = ~283.75 cycles
                                          per line, so over 2 lines the
                                          subcarrier advances ≈ 540° ≈
                                          180°. Subtracting cancels
                                          chroma → Y; subtracting the
                                          luma residue leaves C. No
                                          BPF ringing; cost is loss of
                                          chroma vertical resolution on
                                          fine vertical stripe patterns
                                          (canonical PAL trade-off).

  3. Exact fSC LO built once across the entire capture (no per-line
     phase reset). Synchronous demod of C against the LO gives
     (raw_U, raw_V).

  4. Per-line burst measurement in the back porch → per-line
     LO-vs-subcarrier phase φ.

  5. Class-aware per-line rotation by +ψ:
        NTSC-style line (burst at -U+V): ψ = 135° - φ
        PAL-style line  (burst at -U-V): ψ = -135° - φ
     Recovery: U     = cos ψ · raw_U - sin ψ · raw_V
               V_mod = sin ψ · raw_U + cos ψ · raw_V

  6. V flip on PAL-style lines to recover transmitted V from modulated V.

  7. Which parity is NTSC-vs-PAL is auto-disambiguated by picking the
     hypothesis that makes |ψ_α - ψ_β| ≈ 0° rather than 180°.

  8. 1H comb filter on U, V.

  9. YUV → RGB (BT.601 matrix; PAL is covered by BT.601 alongside NTSC).

See pal.md "Gotchas for a C++ port" for the full list of details and
the reasoning behind each design choice. See also "Chroma 'echoes' /
trailing artifacts" for why --yc-mode comb usually beats bpf on real
content.
"""

import argparse
import sys
import numpy as np
from scipy.signal import firwin, minimum_phase, oaconvolve
from PIL import Image


# ----- command line -----
ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                             formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("src", help="baseband CVBS, int16 LE")
ap.add_argument("dst", help="output image (.png/.jpg via PIL)")
ap.add_argument("--fs", type=float, default=24e6,
                help="sample rate (default %(default).0f)")
ap.add_argument("--field", type=int, default=0,
                help="which detected V-sync to anchor on (default 0)")
ap.add_argument("--fsc", type=float, default=4433618.75,
                help="subcarrier frequency, Hz (default textbook 4.43361875 MHz)")
ap.add_argument("--yc-mode", choices=("bpf", "comb"), default="bpf",
                help="Y/C separation: 'bpf' for frequency split (default, "
                     "robust to source clock drift), 'comb' for 2H delay-line "
                     "comb (cleaner on textbook-compliant sources like "
                     "hacktv/broadcast, degrades on cheap modulators because "
                     "the fSC × line_period relationship goes off-target)")
ap.add_argument("--chroma-phase", choices=("linear", "minimum"), default="minimum",
                help="Phase response of the chroma BPF (only used in --yc-mode "
                     "bpf). 'linear' uses a symmetric FIR — same delay at all "
                     "frequencies, but step responses ring symmetrically into "
                     "and out of an edge, so a sharp red sprite ends up with a "
                     "blue 'pre-echo' before AND blue 'post-echo' after. "
                     "'minimum' (default) gives the same magnitude response "
                     "with all energy concentrated as early as possible: no "
                     "pre-echo, post-echo more concentrated. Closer to what "
                     "an analog 1980s LC chroma trap actually did, which was "
                     "minimum-phase by physics.")
ap.add_argument("--mono", action="store_true",
                help="skip chroma decoding entirely (debug)")
ap.add_argument("--debug", action="store_true",
                help="print burst-after-rotation sanity check")
args = ap.parse_args()

src = args.src
dst = args.dst
fs = args.fs
which_field = args.field
fSC = args.fsc

# ----- PAL timing constants -----
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


# ----- 1. load CVBS -----
with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"loaded {len(x)} samples = {len(x)/fs:.2f} s", flush=True)
print(f"fSC = {fSC/1e6:.6f} MHz | Y/C mode: {args.yc_mode}"
      f"{' | MONO' if args.mono else ''}", flush=True)


# FIR tap counts are specified at the 24 MHz reference rate and scaled with
# fs so every filter keeps the same Hz-domain transition width regardless of
# sample rate. Without this, a higher fs makes each fixed-length FIR span a
# narrower fraction of Nyquist — the chroma BPF in particular becomes
# unrealisable and Y/C separation collapses into noise (seen at 64 MSps).
def taps_for(ref_taps_at_24m):
    n = int(round(ref_taps_at_24m * fs / 24e6))
    n = max(n, ref_taps_at_24m)        # never fewer than the reference
    return n if n % 2 else n + 1       # force odd (type-I linear phase)


# ----- 2. sync separator -----
sync_filter_lpf = firwin(taps_for(33), 1.0e6 / (fs/2), window='hamming').astype(np.float32)
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

# Measure actual line period from H-sync gaps. Source modulators don't
# always hit textbook 64.000 µs exactly — e.g. SMS RF modulator runs at
# ~64.28 µs (about 6.7 samples per line longer than nominal at 24 MSps).
# The fixed-nominal 2H delay then misaligns luma between paired lines by
# ~14 samples → visible horizontal blur in comb mode. So measure.
gap_starts = [int(starts[pi]) for pi in hsync_active]
line_gaps = np.diff(gap_starts)
nominal_line_samp = int(round(LINE_S * fs))
valid_gaps = line_gaps[(line_gaps > 0.9 * nominal_line_samp) &
                       (line_gaps < 1.1 * nominal_line_samp)]
line_samp_measured = int(round(np.median(valid_gaps))) if len(valid_gaps) else nominal_line_samp
print(f"measured line period: {line_samp_measured} samples = "
      f"{line_samp_measured/fs*1e6:.3f} µs "
      f"(nominal {nominal_line_samp} = {LINE_S*1e6:.3f} µs)", flush=True)


# ----- 3. Y/C separation -----
NTAP = taps_for(65)
uv_lpf   = firwin(NTAP, 1.5e6 / (fs/2), window='hamming').astype(np.float32)
luma_lpf = firwin(NTAP, 3.0e6 / (fs/2), window='hamming').astype(np.float32)

if args.yc_mode == "bpf":
    # Upper cutoff at 5.0 MHz (NOT 5.5 MHz). PAL B/G FM sound subcarrier
    # sits at vision+5.5 MHz, which leaks through a BPF with 5.5 MHz upper
    # cutoff into the chroma demod, beats down to 1.07 MHz against the
    # 4.43 MHz LO, and lands inside the UV LPF passband as ~13-col-spaced
    # ripples in uniform-coloured regions. (Confirmed by running hacktv
    # with --noaudio: the ripples vanish entirely.)
    chroma_bpf_linear = firwin(NTAP, [3.5e6 / (fs/2), 5.0e6 / (fs/2)],
                               pass_zero=False, window='hamming').astype(np.float32)
    if args.chroma_phase == "minimum":
        # Same magnitude, but all impulse-response energy shifted to t=0:
        # eliminates pre-cursor ringing entirely (the blue tint that
        # appears *before* a red sprite's leading edge in the linear-
        # phase version). Length becomes (NTAP+1)//2.
        chroma_bpf = minimum_phase(chroma_bpf_linear.astype(np.float64),
                                   method='homomorphic').astype(np.float32)
        print(f"Y/C: frequency split — Y LPF 3 MHz, "
              f"C BPF 3.5..5.5 MHz, minimum-phase ({len(chroma_bpf)} taps)...",
              flush=True)
    else:
        chroma_bpf = chroma_bpf_linear
        print(f"Y/C: frequency split — Y LPF 3 MHz, "
              f"C BPF 3.5..5.5 MHz, linear-phase ({len(chroma_bpf)} taps)...",
              flush=True)
    Y_full = oaconvolve(x, luma_lpf, mode='same').astype(np.float32)
    C_full = oaconvolve(x, chroma_bpf, mode='same').astype(np.float32)
else:
    # 2H comb. Two cancellation properties matter:
    #   - SAMPLE-count delay determines chroma cancellation. The subcarrier
    #     advances fSC × delay/fs cycles; we want that fractional ≈ 0.5
    #     (180° apart → (x + x_d)/2 kills chroma, (x - x_d)/2 keeps it).
    #     At 24 MSps and textbook fSC: 3072 samples = 567.5 cycles ✓.
    #   - LINE-period delay determines luma alignment between paired lines.
    #     Use the *measured* line period × 2.
    # When the source has clock drift (line ≠ 64.000 µs exact), these two
    # requirements pull in different directions. We use the measured
    # 2 × line period for luma alignment, and lean on Y LPF to clean up
    # any residual chroma carrier in Y.
    line_2h_samp = 2 * line_samp_measured
    # Diagnostic: how well does this delay cancel chroma?
    cycles_2h = fSC * line_2h_samp / fs
    cancel = abs(float(np.cos(np.pi * cycles_2h)))
    print(f"Y/C: 2H comb (delay = {line_2h_samp} samples = "
          f"{line_2h_samp/fs*1e6:.2f} µs; "
          f"{cycles_2h:.3f} fSC cycles; "
          f"chroma residue in Y ≈ {cancel*100:.1f}% — cleaned by Y LPF)",
          flush=True)
    x_delayed = np.empty_like(x)
    x_delayed[:line_2h_samp] = 0.0
    x_delayed[line_2h_samp:] = x[:-line_2h_samp]
    Y_raw = (x + x_delayed) * 0.5
    C_full = ((x - x_delayed) * 0.5).astype(np.float32)
    del x_delayed
    # Y LPF: removes the chroma residue (at fSC = 4.43 MHz) that doesn't
    # cancel when 2 × measured_line_period isn't exactly integer + 0.5
    # cycles. With clean line period (textbook 64 µs) the LPF is a no-op
    # on the chroma; on real captures with clock drift it's essential.
    Y_full = oaconvolve(Y_raw, luma_lpf, mode='same').astype(np.float32)
    del Y_raw


# ----- 4. continuous LO + synchronous demod -----
print("building continuous LO...", flush=True)
t = np.arange(len(x), dtype=np.float64) / fs
phi_lo = 2 * np.pi * fSC * t
lo_cos = np.cos(phi_lo).astype(np.float32)
lo_sin = np.sin(phi_lo).astype(np.float32)
del phi_lo, t

if args.mono:
    raw_U_full = np.zeros_like(x)
    raw_V_full = np.zeros_like(x)
else:
    print("synchronous demod + LPF...", flush=True)
    raw_U_full = oaconvolve(C_full * lo_cos, uv_lpf, mode='same').astype(np.float32)
    raw_V_full = oaconvolve(C_full * lo_sin, uv_lpf, mode='same').astype(np.float32)
del lo_cos, lo_sin


# ----- 5. per-line burst measurement -----
n_lines = len(hsync_active)
burst_cos = np.zeros(n_lines)
burst_sin = np.zeros(n_lines)
if not args.mono:
    print("measuring per-line burst...", flush=True)
    for li, pi in enumerate(hsync_active):
        edge = int(starts[pi])
        b_s = edge + burst_start_samp
        b_e = edge + burst_end_samp
        if b_e > len(x): break
        burst_cos[li] = float(raw_U_full[b_s:b_e].mean())
        burst_sin[li] = float(raw_V_full[b_s:b_e].mean())

phi_per_line = np.arctan2(burst_sin, burst_cos)


# ----- 6. class assignment (which parity is V-inverted?) -----
R135 = np.radians(135.0)

if args.mono:
    alpha_is_ntsc = True  # doesn't matter
else:
    alpha_lines = np.arange(0, n_lines, 2)
    beta_lines  = np.arange(1, n_lines, 2)
    phi_alpha = float(np.arctan2(burst_sin[alpha_lines].mean(),
                                 burst_cos[alpha_lines].mean()))
    phi_beta  = float(np.arctan2(burst_sin[beta_lines].mean(),
                                 burst_cos[beta_lines].mean()))
    print(f"  α (even-index) burst phase: {np.degrees(phi_alpha):7.2f}°", flush=True)
    print(f"  β (odd-index)  burst phase: {np.degrees(phi_beta):7.2f}°", flush=True)

    diff_H1 = float(np.angle(np.exp(1j * ((R135 - phi_alpha) - (-R135 - phi_beta)))))
    diff_H2 = float(np.angle(np.exp(1j * ((-R135 - phi_alpha) - (R135 - phi_beta)))))
    print(f"  hyp 1 (α=NTSC, β=PAL): |ψ_α - ψ_β| = {abs(np.degrees(diff_H1)):6.2f}°", flush=True)
    print(f"  hyp 2 (α=PAL, β=NTSC): |ψ_α - ψ_β| = {abs(np.degrees(diff_H2)):6.2f}°", flush=True)

    if abs(diff_H1) < abs(diff_H2):
        alpha_is_ntsc = True
        print("  → α is NTSC-style (V not inverted)", flush=True)
    else:
        alpha_is_ntsc = False
        print("  → α is PAL-style (V inverted)", flush=True)


# Debug: after rotation, burst should land at (-1, +1) × burst_mag.
if args.debug and not args.mono:
    bU_after = np.zeros(n_lines)
    bV_after = np.zeros(n_lines)
    for li in range(n_lines):
        is_ntsc = ((li % 2 == 0) == alpha_is_ntsc)
        psi = (R135 - phi_per_line[li]) if is_ntsc else (-R135 - phi_per_line[li])
        cp, sp = float(np.cos(psi)), float(np.sin(psi))
        bU = cp * burst_cos[li] - sp * burst_sin[li]
        bV_mod = sp * burst_cos[li] + cp * burst_sin[li]
        bU_after[li] = bU
        bV_after[li] = bV_mod if is_ntsc else -bV_mod
    mag = float(np.median(np.sqrt(burst_cos**2 + burst_sin**2)))
    print(f"  recovered burst median (U, V_tx)/burst_mag = "
          f"({float(np.median(bU_after))/mag:+.3f}, "
          f"{float(np.median(bV_after))/mag:+.3f})  expected (-1, +1)", flush=True)


# ----- 7. per-line rotation + V flip on PAL-style lines -----
rows_Y = []
rows_U = []
rows_V = []
for li, pi in enumerate(hsync_active):
    edge = int(starts[pi])
    a_s = edge + sync_skip_samp
    a_e = a_s + active_samples
    if a_e > len(x): break

    Y = Y_full[a_s:a_e]

    if args.mono:
        U = np.zeros_like(Y)
        V = np.zeros_like(Y)
    else:
        phi_line = phi_per_line[li]
        is_alpha = (li % 2 == 0)
        is_ntsc  = (is_alpha == alpha_is_ntsc)
        psi = (R135 - phi_line) if is_ntsc else (-R135 - phi_line)

        cos_p = float(np.cos(psi))
        sin_p = float(np.sin(psi))
        rU = raw_U_full[a_s:a_e]
        rV = raw_V_full[a_s:a_e]
        U     = cos_p * rU - sin_p * rV
        V_mod = sin_p * rU + cos_p * rV
        V = V_mod if is_ntsc else -V_mod

    rows_Y.append(Y)
    rows_U.append(U)
    rows_V.append(V)

Y_mat = np.array(rows_Y)
U_mat = np.array(rows_U)
V_mat = np.array(rows_V)


# ----- 8. 1H comb on U, V -----
if not args.mono:
    U_comb = U_mat.copy()
    V_comb = V_mat.copy()
    U_comb[1:] = 0.5 * (U_mat[1:] + U_mat[:-1])
    V_comb[1:] = 0.5 * (V_mat[1:] + V_mat[:-1])
else:
    U_comb = U_mat
    V_comb = V_mat


# ----- 9. YUV → RGB -----
y_floor = float(np.percentile(Y_mat, 5))
y_peak  = float(np.percentile(Y_mat, 99))
Y_norm = np.clip((Y_mat - y_floor) / max(y_peak - y_floor, 1.0), 0.0, 1.0)

if args.mono:
    R_img = G_img = B_img = Y_norm
else:
    burst_mag = float(np.median(np.sqrt(burst_cos**2 + burst_sin**2)))
    uv_scale = 0.3 / max(burst_mag, 1.0)
    print(f"  burst_mag={burst_mag:.4f}  uv_scale={uv_scale:.4f}", flush=True)
    U_norm = U_comb * uv_scale
    V_norm = V_comb * uv_scale

    R_img = Y_norm + 1.13983 * V_norm
    G_img = Y_norm - 0.39465 * U_norm - 0.58060 * V_norm
    B_img = Y_norm + 2.03211 * U_norm

R8 = np.clip(R_img * 255, 0, 255).astype(np.uint8)
G8 = np.clip(G_img * 255, 0, 255).astype(np.uint8)
B8 = np.clip(B_img * 255, 0, 255).astype(np.uint8)
rgb = np.stack([R8, G8, B8], axis=-1)
rgb = np.repeat(rgb, 2, axis=0)

img = Image.fromarray(rgb).resize((720, rgb.shape[0]), Image.LANCZOS)
img.save(dst)
print(f"wrote {dst} ({img.size[0]}×{img.size[1]})", flush=True)
