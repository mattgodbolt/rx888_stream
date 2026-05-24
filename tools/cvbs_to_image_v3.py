#!/usr/bin/env python3
"""v3 PAL visualiser. Compared to cvbs_to_image_simple.py:

- Coalesces multiple half-line-edge runs that belong to the same v-sync
  (each PAL v-sync interval has ~5 such runs from broad/equalising
  pulses). This prevents extract_field from landing mid-field.
- Validates v-sync candidates are spaced ~312 lines apart before
  accepting them as real field starts.
- Renders TWO consecutive fields properly interlaced (field 1 on even
  output rows, field 2 on odd output rows) instead of doubling one
  field with np.repeat.
- Back-porch DC reference per line (not per-line median).
- Sample rate defaults to 16 MHz for SDR Console captures.

Input:  real int16 LE baseband CVBS, sync = LOW (negative).
Output: PGM image, one PAL FRAME (two interlaced fields).
"""
import sys, numpy as np

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 16e6
which_frame = int(sys.argv[4]) if len(sys.argv) > 4 else 0   # which frame to render
# Optional override: nominal line rate. SMS modulator runs ~0.44% slow.
nominal_line_hz = float(sys.argv[5]) if len(sys.argv) > 5 else 15625.0 / 1.00444

# Derived timings
LINE_S = 1.0 / nominal_line_hz                 # ~64.28 µs (vs 64 µs nominal)
samples_per_line = LINE_S * fs
HSYNC_S = 4.7e-6
BACK_PORCH_S = 5.7e-6
ACTIVE_S = 52.0e-6
sync_skip = int(round((HSYNC_S + BACK_PORCH_S) * fs))
active_samples = int(round(ACTIVE_S * fs))
back_porch_samples = max(4, int(round(2.0e-6 * fs)))
LINES_PER_FIELD = 312
ACTIVE_LINES = 287

print(f"fs={fs/1e6:.3f} Msps   line={LINE_S*1e6:.3f} µs   "
      f"samples/line={samples_per_line:.2f}   active={active_samples}",
      flush=True)

with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"read {len(x)} samples = {len(x)/fs:.2f}s", flush=True)

# H-sync edges from derivative crossings
ma_n = max(1, int(0.2e-6 * fs))
x_sm = np.convolve(x, np.ones(ma_n, dtype=np.float32) / ma_n, mode='same')
dx = np.diff(x_sm).astype(np.float32)
deriv_thr = np.percentile(dx, 0.3)
sync_mask = dx < deriv_thr
hits = np.where(np.diff(sync_mask.astype(np.int8)) == 1)[0]
coalesce = int(0.5 * samples_per_line)   # within half a line
edges = []
for h in hits:
    if not edges or h - edges[-1] > coalesce:
        edges.append(int(h))
edges = np.array(edges)

# Sub-sample refinement
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
print(f"{len(edges_precise)} sync edges", flush=True)

# V-sync detection — robust version:
# Real PAL v-sync = ~10 half-line-spaced edges. Require run of >= 8.
# Reject candidates whose gap from the previous accepted v-sync is far
# from the expected ~312 lines.
deltas = np.diff(edges_precise)
is_half = (deltas > 0.4 * samples_per_line) & (deltas < 0.65 * samples_per_line)
raw_runs = []
in_run = False; rs = 0
for i, h in enumerate(is_half):
    if h and not in_run:
        in_run = True; rs = i
    elif not h and in_run:
        in_run = False
        if i - rs >= 5:                                  # PAL v-sync has ~5-10 half-line edges
            raw_runs.append(i + 1)
print(f"{len(raw_runs)} robust half-line runs", flush=True)

# Drop spurious runs that are too close (< 200 lines) to the prior accepted one.
field_starts_idx = []
for s in raw_runs:
    if not field_starts_idx:
        field_starts_idx.append(s)
        continue
    gap_lines = (edges_precise[s] - edges_precise[field_starts_idx[-1]]) / samples_per_line
    if 200 < gap_lines < 400:    # ~312 expected
        field_starts_idx.append(s)
    elif gap_lines >= 400:
        # We may have missed an intermediate field; still accept.
        field_starts_idx.append(s)
    # else (gap < 200) → ignore (false positive)

print(f"{len(field_starts_idx)} coalesced v-sync events", flush=True)

# Diagnostic: print the gaps between consecutive v-syncs in line units.
gaps_lines = [(edges_precise[field_starts_idx[i+1]] - edges_precise[field_starts_idx[i]]) / samples_per_line
              for i in range(min(10, len(field_starts_idx) - 1))]
print(f"first 10 v-sync gaps (lines): {[f'{g:.1f}' for g in gaps_lines]}", flush=True)

# Mark H-sync edges (line-spaced)
hsync_mask = np.zeros(len(edges_precise), dtype=bool)
for i in range(len(edges_precise) - 1):
    gap = edges_precise[i+1] - edges_precise[i]
    if 0.9 * samples_per_line < gap < 1.1 * samples_per_line:
        hsync_mask[i] = True

VBI_LEADING_LINES = 22   # PAL VBI lines (lines 1-22 of each field are vertical blanking)

def extract_field(start_idx):
    rows = []
    i = start_idx
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
        # Back-porch reference for black level
        black = float(line_raw[:back_porch_samples].mean())
        rows.append(line_raw - black)
        i += 1
    return np.array(rows)

if len(field_starts_idx) < 2 * which_frame + 2:
    sys.exit(f"only {len(field_starts_idx)} fields, can't render frame {which_frame}")

# Render frame: two consecutive fields, interleaved
f1 = extract_field(field_starts_idx[2 * which_frame])
f2 = extract_field(field_starts_idx[2 * which_frame + 1])
print(f"f1={f1.shape}   f2={f2.shape}", flush=True)

H = min(f1.shape[0], f2.shape[0])
W = min(f1.shape[1], f2.shape[1])
frame = np.empty((2 * H, W), dtype=np.float32)
frame[0::2] = f1[:H, :W]
frame[1::2] = f2[:H, :W]

# Global black/white scaling
black_level = float(np.percentile(frame,  5))
white_level = float(np.percentile(frame, 99))
print(f"black={black_level:.0f}  white={white_level:.0f}", flush=True)
img = (frame - black_level) / max(white_level - black_level, 1.0) * 255
img = np.clip(img, 0, 255).astype(np.uint8)

# Horizontal downsample to 720
target_w = 720
if img.shape[1] > target_w:
    factor = img.shape[1] // target_w
    img = img[:, :factor*target_w].reshape(img.shape[0], target_w, factor).mean(axis=2).astype(np.uint8)

with open(dst, "wb") as f:
    f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
    f.write(img.tobytes())
print(f"wrote {dst} ({img.shape[1]}×{img.shape[0]})", flush=True)
