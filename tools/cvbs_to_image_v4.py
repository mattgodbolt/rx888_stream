#!/usr/bin/env python3
"""v4: instead of detecting H-sync per line (which jitters), lock to
field-start once via v-sync, then step by an empirically-estimated
line period derived from many lines in the same field. This forces
all lines in one field to align horizontally.

Also: estimate the true line period from the median of detected H-sync
edge spacings inside the field.
"""
import sys, numpy as np

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 16e6
which_frame = int(sys.argv[4]) if len(sys.argv) > 4 else 0

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

print(f"fs={fs/1e6:.3f} Msps   nominal samples/line={samples_per_line_nominal:.2f}",
      flush=True)

with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"read {len(x)} samples = {len(x)/fs:.2f}s", flush=True)

# H-sync detection
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
print(f"{len(edges)} sync edges", flush=True)

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

# Estimate ACTUAL line period from median of normal H-sync intervals.
deltas = np.diff(edges_precise)
line_deltas = deltas[(deltas > 0.9 * samples_per_line_nominal) &
                    (deltas < 1.1 * samples_per_line_nominal)]
samples_per_line = float(np.median(line_deltas))
print(f"measured line period: {samples_per_line:.3f} samples = "
      f"{samples_per_line/fs*1e6:.4f} µs ({fs/samples_per_line:.2f} Hz)", flush=True)

# V-sync: runs of >=5 consecutive half-line gaps, coalesced by 200-line spacing
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
print(f"{len(field_starts_idx)} v-sync events", flush=True)
if len(field_starts_idx) < 2 * which_frame + 2:
    sys.exit(f"only {len(field_starts_idx)} fields, can't render frame {which_frame}")

def extract_field_locked(start_idx):
    """Find the first NORMAL H-sync after VBI, then step by exact line period
    *measured from this specific field's edges*."""
    i = start_idx
    skipped = 0
    while i < len(edges_precise) and skipped < VBI_LEADING_LINES:
        gap = edges_precise[i+1] - edges_precise[i] if i+1 < len(edges_precise) else 0
        if 0.9 * samples_per_line < gap < 1.1 * samples_per_line:
            skipped += 1
        i += 1
    if i >= len(edges_precise):
        return np.zeros((0, active_samples), dtype=np.float32)

    # Collect H-sync edges in this field (next ACTIVE_LINES line-spaced ones).
    field_edges = [edges_precise[i]]
    j = i + 1
    while j < len(edges_precise) and len(field_edges) < ACTIVE_LINES:
        gap = edges_precise[j] - field_edges[-1]
        if 0.9 * samples_per_line < gap < 1.1 * samples_per_line:
            field_edges.append(edges_precise[j])
        j += 1
    field_edges = np.array(field_edges)
    # Linear regression edge_position = anchor + line_idx * period.
    line_idx = np.arange(len(field_edges))
    A = np.vstack([line_idx, np.ones_like(line_idx)]).T.astype(np.float64)
    period, anchor = np.linalg.lstsq(A, field_edges, rcond=None)[0]
    residuals = field_edges - (anchor + line_idx * period)
    print(f"  field-local period={period:.4f} (vs global {samples_per_line:.4f})  "
          f"residual std={residuals.std():.2f}", flush=True)

    rows = []
    for ln in range(ACTIVE_LINES):
        s_precise = anchor + ln * period + sync_skip
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

f1 = extract_field_locked(field_starts_idx[2 * which_frame])
f2 = extract_field_locked(field_starts_idx[2 * which_frame + 1])
print(f"f1={f1.shape}   f2={f2.shape}", flush=True)

H = min(f1.shape[0], f2.shape[0])
W = min(f1.shape[1], f2.shape[1])
frame = np.empty((2 * H, W), dtype=np.float32)
frame[0::2] = f1[:H, :W]
frame[1::2] = f2[:H, :W]

black_level = float(np.percentile(frame,  5))
white_level = float(np.percentile(frame, 99))
print(f"black={black_level:.0f}  white={white_level:.0f}", flush=True)
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
