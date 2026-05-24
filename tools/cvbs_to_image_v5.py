#!/usr/bin/env python3
"""v5: render only one field (not interlaced), doubled vertically.
Removes the field-to-field horizontal-offset doubling artifact.

Usage:
    cvbs_to_image_v5.py SRC.s16 DST.pgm [fs [field [line_rate_hz]]]

Args:
    fs:           sample rate (Hz). Default 16e6.
    field:        which field to render (0-based). Default 0. Try a few — V-sync
                  detection sometimes lands mid-screen, producing a wrapped image.
    line_rate_hz: explicit line rate (Hz) to use as the line period. Default 0
                  meaning "infer from median H-sync gap" (good for stock 15.625 kHz
                  PAL; for slightly-off sources like consumer modulators it's
                  better to measure once from the envelope-modulation spectrum
                  and pass that value here).
"""
import sys, numpy as np

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

ma_n = max(1, int(0.2e-6 * fs))
x_sm = np.convolve(x, np.ones(ma_n, dtype=np.float32) / ma_n, mode='same')
dx = np.diff(x_sm).astype(np.float32)
deriv_thr = np.percentile(dx, 0.3)
sync_mask = dx < deriv_thr
hits = np.where(np.diff(sync_mask.astype(np.int8)) == 1)[0]
coalesce = int(0.5 * samples_per_line_nominal)
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
line_deltas = deltas[(deltas > 0.9 * samples_per_line_nominal) &
                    (deltas < 1.1 * samples_per_line_nominal)]
measured = float(np.median(line_deltas)) if len(line_deltas) else samples_per_line_nominal
if line_rate_hz_override > 0:
    samples_per_line = fs / line_rate_hz_override
    print(f"line period: measured median = {measured:.3f} samples; "
          f"OVERRIDING to {samples_per_line:.3f} samples = "
          f"{samples_per_line/fs*1e6:.4f} µs ({line_rate_hz_override:g} Hz)",
          flush=True)
else:
    samples_per_line = measured
    print(f"line period = {samples_per_line:.3f} samples = "
          f"{samples_per_line/fs*1e6:.4f} µs (median of H-sync gaps)",
          flush=True)

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
print(f"{len(field_starts_idx)} fields", flush=True)

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
    # Lock to anchor at this edge, step by line period.
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

with open(dst, "wb") as f:
    f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
    f.write(img.tobytes())
print(f"wrote {dst} ({img.shape[1]}×{img.shape[0]})", flush=True)
