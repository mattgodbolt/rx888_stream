#!/usr/bin/env python3
"""Render a single PAL field from baseband CVBS to a still image.

Single-field rendering (not interlaced), doubled vertically — avoids the
field-to-field horizontal-offset doubling artifact that the v3/v4 variants
exhibited.

Usage:
    cvbs_to_image_v5.py SRC.s16 DST.{png,pgm} [fs [field [line_rate_hz]]]

Args:
    SRC.s16:      baseband CVBS, int16 LE, sync at the negative end of the
                  range (i.e. demod_real.py output).
    DST:          output image. Extension determines format:
                    .png / .jpg / .bmp / .tif / etc → uses PIL (Pillow)
                    .pgm → raw PGM, no dependencies
                  PIL is preferred and required for non-PGM extensions.
    fs:           sample rate (Hz). Default 16e6.
    field:        which field to render (0-based). Default 0. V-sync
                  detection occasionally lands mid-screen, producing a
                  wrapped image — try a few field numbers.
    line_rate_hz: explicit line rate (Hz). Default 0 = auto-detect from the
                  envelope-modulation spectrum (the FFT picks up the strong
                  AC component at the line rate even when individual H-sync
                  edges are noisy). Pass an explicit value to override.
"""
import sys
import numpy as np

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 16e6
which_field = int(sys.argv[4]) if len(sys.argv) > 4 else 0
line_rate_hz_override = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

LINE_S = 1.0 / 15625.0
samples_per_line_nominal = LINE_S * fs
HSYNC_S = 4.7e-6
BACK_PORCH_S = 5.7e-6
ACTIVE_S = 52.0e-6
sync_skip = int(round((HSYNC_S + BACK_PORCH_S) * fs))
active_samples = int(round(ACTIVE_S * fs))
back_porch_samples = max(4, int(round(2.0e-6 * fs)))
ACTIVE_LINES = 287
VBI_LEADING_LINES = 22

with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]


def find_line_rate_from_fft(signal, fs):
    """Pick the strongest spectral peak in the 14-17 kHz range, with
    quadratic interpolation around the peak bin for sub-bin precision.
    Returns line rate in Hz, or None if no clean peak found.
    """
    # Use ~1 s of signal; sync pulses dominate the AC content so the FFT
    # picks up the line-rate fundamental strongly.
    N = min(int(1.0 * fs), len(signal))
    if N < int(0.2 * fs):
        return None
    s = signal[:N] - signal[:N].mean()
    # Hann to reduce spectral leakage
    w = np.hanning(N).astype(np.float32)
    spec = np.abs(np.fft.rfft(s * w))
    freqs = np.fft.rfftfreq(N, 1.0 / fs)
    band = (freqs > 14e3) & (freqs < 17e3)
    if not band.any():
        return None
    sub = spec[band]
    sub_f = freqs[band]
    ipk = int(np.argmax(sub))
    # Confidence: need the peak to clear the local median by a margin
    local_med = float(np.median(sub))
    if sub[ipk] < 5.0 * local_med:
        return None
    # Quadratic interpolation around the peak bin for sub-bin precision
    if 0 < ipk < len(sub) - 1:
        y_m1, y_0, y_p1 = sub[ipk-1], sub[ipk], sub[ipk+1]
        denom = (y_m1 - 2 * y_0 + y_p1)
        if denom != 0:
            shift = 0.5 * (y_m1 - y_p1) / denom
            df = freqs[1] - freqs[0]
            return float(sub_f[ipk] + shift * df)
    return float(sub_f[ipk])


# --- decide the line period ---
if line_rate_hz_override > 0:
    samples_per_line = fs / line_rate_hz_override
    print(f"line rate (override): {line_rate_hz_override:.1f} Hz "
          f"→ {samples_per_line:.3f} samples ({samples_per_line/fs*1e6:.4f} µs)",
          flush=True)
else:
    fft_rate = find_line_rate_from_fft(x, fs)
    if fft_rate is not None:
        samples_per_line = fs / fft_rate
        print(f"line rate (envelope FFT): {fft_rate:.2f} Hz "
              f"→ {samples_per_line:.3f} samples ({samples_per_line/fs*1e6:.4f} µs)",
              flush=True)
    else:
        # Fall back to median-of-H-sync as before (works for clean signals)
        ma_n_pre = max(1, int(0.2e-6 * fs))
        xs_pre = np.convolve(x, np.ones(ma_n_pre, dtype=np.float32) / ma_n_pre, mode='same')
        dx_pre = np.diff(xs_pre).astype(np.float32)
        thr_pre = np.percentile(dx_pre, 0.3)
        mask_pre = dx_pre < thr_pre
        hits_pre = np.where(np.diff(mask_pre.astype(np.int8)) == 1)[0]
        coalesce_pre = int(0.5 * samples_per_line_nominal)
        e_pre = []
        for h in hits_pre:
            if not e_pre or h - e_pre[-1] > coalesce_pre:
                e_pre.append(int(h))
        if len(e_pre) > 10:
            d_pre = np.diff(np.array(e_pre, dtype=np.float64))
            d_pre = d_pre[(d_pre > 0.9 * samples_per_line_nominal) &
                          (d_pre < 1.1 * samples_per_line_nominal)]
            samples_per_line = float(np.median(d_pre)) if len(d_pre) else samples_per_line_nominal
        else:
            samples_per_line = samples_per_line_nominal
        print(f"line rate (fallback, H-sync median): "
              f"{fs/samples_per_line:.2f} Hz → {samples_per_line:.3f} samples",
              flush=True)


# --- H-sync edge detection ---
ma_n = max(1, int(0.2e-6 * fs))
x_sm = np.convolve(x, np.ones(ma_n, dtype=np.float32) / ma_n, mode='same')
dx = np.diff(x_sm).astype(np.float32)
deriv_thr = np.percentile(dx, 0.3)
sync_mask = dx < deriv_thr
hits = np.where(np.diff(sync_mask.astype(np.int8)) == 1)[0]
coalesce = int(0.5 * samples_per_line)
edges = []
for h in hits:
    if not edges or h - edges[-1] > coalesce:
        edges.append(int(h))
edges = np.array(edges)

edges_precise = []
for e in edges:
    lo, hi = max(0, e-5), min(len(dx), e+10)
    steepest = lo + int(dx[lo:hi].argmin())
    if 0 < steepest < len(dx) - 1:
        ym1, y0, yp1 = dx[steepest-1], dx[steepest], dx[steepest+1]
        denom = ym1 - 2*y0 + yp1
        sub = 0.5 * (ym1 - yp1) / denom if denom != 0 else 0.0
        edges_precise.append(steepest + sub)
    else:
        edges_precise.append(float(steepest))
edges_precise = np.array(edges_precise)

deltas = np.diff(edges_precise)

is_half = (deltas > 0.4 * samples_per_line) & (deltas < 0.65 * samples_per_line)
raw_runs = []
in_run = False; rs = 0
for i, h in enumerate(is_half):
    if h and not in_run:
        in_run = True; rs = i
    elif not h and in_run:
        in_run = False
        if i - rs >= 5:
            raw_runs.append(i + 1)
field_starts_idx = []
for s in raw_runs:
    if not field_starts_idx:
        field_starts_idx.append(s)
        continue
    gap_lines = (edges_precise[s] - edges_precise[field_starts_idx[-1]]) / samples_per_line
    if gap_lines > 200:
        field_starts_idx.append(s)
print(f"{len(field_starts_idx)} fields detected", flush=True)
if not field_starts_idx:
    sys.exit("no V-sync runs found — capture may not contain a clean signal")
if which_field >= len(field_starts_idx):
    sys.exit(f"requested field {which_field} but only {len(field_starts_idx)} fields detected")


def extract_field(start_idx):
    i = start_idx
    skipped = 0
    while i < len(edges_precise) and skipped < VBI_LEADING_LINES:
        gap = edges_precise[i+1] - edges_precise[i] if i+1 < len(edges_precise) else 0
        if 0.9 * samples_per_line < gap < 1.1 * samples_per_line:
            skipped += 1
        i += 1
    if i >= len(edges_precise):
        return np.zeros((0, active_samples), dtype=np.float32)
    anchor = edges_precise[i]
    rows = []
    for ln in range(ACTIVE_LINES):
        s_precise = anchor + ln * samples_per_line + sync_skip
        s = int(s_precise)
        if s + active_samples + 1 > len(x):
            break
        frac = s_precise - s
        a = x[s:s + active_samples]
        b = x[s+1:s + active_samples + 1]
        line_raw = a * (1 - frac) + b * frac
        black = float(line_raw[:back_porch_samples].mean())
        rows.append(line_raw - black)
    return np.array(rows)


field = extract_field(field_starts_idx[which_field])
print(f"extracted {field.shape[0]} lines", flush=True)

# Double vertically (rather than interleave another field)
frame = np.repeat(field, 2, axis=0)

black_level = float(np.percentile(frame, 5))
white_level = float(np.percentile(frame, 99))
img = (frame - black_level) / max(white_level - black_level, 1.0) * 255
img = np.clip(img, 0, 255).astype(np.uint8)

target_w = 720
if img.shape[1] > target_w:
    factor = img.shape[1] // target_w
    img = img[:, :factor*target_w].reshape(img.shape[0], target_w, factor).mean(axis=2).astype(np.uint8)

# --- write output (format from extension) ---
ext = dst.rsplit(".", 1)[-1].lower() if "." in dst else "pgm"
if ext == "pgm":
    with open(dst, "wb") as f:
        f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
        f.write(img.tobytes())
else:
    try:
        from PIL import Image
    except ImportError:
        sys.exit(f"PIL/Pillow not available — install python3-pil or use a .pgm extension")
    Image.fromarray(img).save(dst)
print(f"wrote {dst} ({img.shape[1]}×{img.shape[0]})", flush=True)
