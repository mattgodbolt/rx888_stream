#!/usr/bin/env python3
"""Render a baseband-CVBS int16 LE capture to a PGM image.

Differences from cvbs_to_image.py:
- No per-line median subtraction (that assumes the median = blanking,
  which is only true for dark/mid pictures; for a bright picture like
  the Alex Kidd title screen the median is near WHITE, and subtraction
  pushes everything dark).
- Uses sync-tip percentile per line for DC reference, OR global
  field-wide percentiles for level scaling. Both options live below.

Input:  real int16 LE samples, sync = LOW (negative), white = HIGH.
Output: PGM image, one PAL field's worth of detected lines.
"""
import sys, numpy as np

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 16e6

LINE_US = 64.0
HSYNC_US = 4.7
BACK_PORCH_US = 5.7
ACTIVE_US = 52.0
samples_per_line = LINE_US * 1e-6 * fs
sync_skip = int(round((HSYNC_US + BACK_PORCH_US) * 1e-6 * fs))
active_samples = int(round(ACTIVE_US * 1e-6 * fs))
back_porch_samples = max(4, int(round(2e-6 * fs)))  # use 2 µs of back porch as black ref

print(f"fs={fs/1e6} Msps; samples/line={samples_per_line:.1f}; active={active_samples}",
      flush=True)

with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"read {len(x)} samples = {len(x)/fs:.2f}s", flush=True)

# Derivative-based H-sync detection (immune to DC drift, robust to bright lines).
ma_n = max(1, int(0.2e-6 * fs))
x_sm = np.convolve(x, np.ones(ma_n, dtype=np.float32) / ma_n, mode='same')
dx = np.diff(x_sm).astype(np.float32)
deriv_thr = np.percentile(dx, 0.3)
sync_start_mask = dx < deriv_thr
hits = np.where(np.diff(sync_start_mask.astype(np.int8)) == 1)[0]
coalesce = int(30e-6 * fs)
edges = []
for h in hits:
    if not edges or h - edges[-1] > coalesce:
        edges.append(int(h))
edges = np.array(edges)
print(f"{len(edges)} sync edges", flush=True)

# Sub-sample edge refinement
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

# Detect v-sync via runs of half-line-spaced edges
deltas = np.diff(edges_precise)
is_half = (deltas > 0.4 * samples_per_line) & (deltas < 0.65 * samples_per_line)
field_starts_idx = []
in_run = False; rs = 0
for i, h in enumerate(is_half):
    if h and not in_run:
        in_run = True; rs = i
    elif not h and in_run:
        in_run = False
        if i - rs >= 5:
            field_starts_idx.append(i + 1)
print(f"{len(field_starts_idx)} v-sync events", flush=True)
if len(field_starts_idx) < 2:
    sys.exit("not enough v-syncs")

# Mark H-syncs (line-spaced gaps).
hsync_mask = np.zeros(len(edges_precise), dtype=bool)
for i in range(len(edges_precise) - 1):
    gap = edges_precise[i+1] - edges_precise[i]
    if 0.9 * samples_per_line < gap < 1.1 * samples_per_line:
        hsync_mask[i] = True

VBI_LEADING_LINES = 14
ACTIVE_LINES = 287

def extract_field(start_idx_in_edges):
    rows = []
    i = start_idx_in_edges
    skipped = 0
    while i < len(edges_precise) and skipped < VBI_LEADING_LINES:
        if hsync_mask[i]:
            skipped += 1
        i += 1
    while i < len(edges_precise) and len(rows) < ACTIVE_LINES:
        if not hsync_mask[i]:
            i += 1
            continue
        e_precise = edges_precise[i]
        s_precise = e_precise + sync_skip
        s = int(s_precise)
        if s + active_samples + 1 > len(x):
            break
        frac = s_precise - s
        a = x[s:s + active_samples]
        b = x[s+1:s + active_samples + 1]
        line_raw = a * (1 - frac) + b * frac

        # DC reference: back porch is the first ~2 µs of the active window
        # (between end of sync pulse and start of active video). It's at the
        # black-level reference voltage by design. Use the MEAN of the
        # FIRST few samples as the line's "black".
        black = float(line_raw[:back_porch_samples].mean())
        rows.append(line_raw - black)
        i += 1
    return np.array(rows)

fi = min(4, len(field_starts_idx) - 2)
f0 = extract_field(field_starts_idx[fi])
print(f"f0={f0.shape[0]} lines × {f0.shape[1]} samples", flush=True)
frame = np.repeat(f0, 2, axis=0)

# Roll to put bright content at top
peak_per_row = frame.max(axis=1)
white_threshold = peak_per_row.max() * 0.6
bright_rows = np.where(peak_per_row > white_threshold)[0]
if len(bright_rows) > 0:
    gaps = np.diff(np.r_[bright_rows, bright_rows[0] + frame.shape[0]])
    max_gap_idx = int(np.argmax(gaps))
    top_row = int(bright_rows[(max_gap_idx + 1) % len(bright_rows)])
    if 5 < top_row < frame.shape[0] - 5:
        frame = np.roll(frame, -top_row + 8, axis=0)

# Global black/white scaling: black = 5th percentile, white = 99th
black_level = float(np.percentile(frame, 5))
white_level = float(np.percentile(frame, 99))
print(f"black={black_level:.0f}  white={white_level:.0f}  span={white_level-black_level:.0f}",
      flush=True)
img = (frame - black_level) / max(white_level - black_level, 1) * 255
img = np.clip(img, 0, 255).astype(np.uint8)

# Downsample horizontally
target_w = 720
if img.shape[1] > target_w:
    factor = img.shape[1] // target_w
    img = img[:, :factor*target_w].reshape(img.shape[0], target_w, factor).mean(axis=2).astype(np.uint8)

with open(dst, "wb") as f:
    f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
    f.write(img.tobytes())
print(f"wrote {dst} ({img.shape[1]}×{img.shape[0]})", flush=True)
