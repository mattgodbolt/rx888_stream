#!/usr/bin/env python3
"""Reconstruct an image from a baseband-CVBS capture, the SIMPLEST way:
each detected H-sync edge = one line of the output. No PLL drift, no
predicted positions, no field walking.

Input:  real int16 LE samples (default 50 Msps).
Output: PGM image. One PAL frame's worth of detected lines.

For each H-sync edge:
  - Sample active video from sync_edge+10.4µs to sync_edge+62.4µs
  - Subtract per-line median as blanking reference (robust to AC-coupling DC
    drift that varies massively within a single field on the RX888 HF input)
  - That's one row of the output

Vertical sync detection identifies field boundaries; we render one field
(or one frame's worth of lines) starting from a chosen v-sync.
"""
import sys, numpy as np

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 50e6

LINE_US = 64.0
HSYNC_US = 4.7
BACK_PORCH_US = 5.7
ACTIVE_US = 52.0
LINES_PER_FIELD = 312
samples_per_line = LINE_US * 1e-6 * fs
sync_skip = int(round((HSYNC_US + BACK_PORCH_US) * 1e-6 * fs))
active_samples = int(round(ACTIVE_US * 1e-6 * fs))

print(f"fs={fs/1e6} Msps; sync_skip={sync_skip} samples ({sync_skip/fs*1e6:.2f}µs)",
      flush=True)

with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
x = x[int(0.1 * fs):]
print(f"read {len(x)} samples = {len(x)/fs:.2f}s", flush=True)

# Derivative-based H-sync edge detection (immune to DC drift).
ma_n = max(1, int(0.2e-6 * fs))
x_sm = np.convolve(x, np.ones(ma_n, dtype=np.float32) / ma_n, mode='same')
dx = np.diff(x_sm).astype(np.float32)
deriv_thr = np.percentile(dx, 0.3)
print(f"deriv threshold: {deriv_thr:.1f}", flush=True)

sync_start_mask = dx < deriv_thr
hits = np.where(np.diff(sync_start_mask.astype(np.int8)) == 1)[0]
coalesce = int(30e-6 * fs)
edges_raw = []
for h in hits:
    if not edges_raw or h - edges_raw[-1] > coalesce:
        edges_raw.append(int(h))
edges_raw = np.array(edges_raw)
# NOTE: previously had a "filter character edges" pass that rejected edges
# where the signal at +1µs wasn't < -1000. That dropped 18 edges including
# the top-of-character scanlines of text — visible as missing-bar "A" glyphs
# in the output (looked like "H"). Removed because the AC-coupling overshoot
# from a preceding bright line makes the sync's measured depth shallower
# than the threshold for those particular lines.
edges = edges_raw
print(f"{len(edges)} sync edges (no post-edge filter)", flush=True)

# Refine sub-sample edge positions: parabolic interp on the derivative minimum.
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

# Detect v-sync: runs of >=5 consecutive half-line-spaced edges
deltas = np.diff(edges_precise)
is_half = (deltas > 0.4 * samples_per_line) & (deltas < 0.65 * samples_per_line)
field_starts_idx = []   # index INTO edges_precise of first H-sync after each v-sync
in_run = False; rs = 0
for i, h in enumerate(is_half):
    if h and not in_run:
        in_run = True; rs = i
    elif not h and in_run:
        in_run = False
        if i - rs >= 5:
            field_starts_idx.append(i + 1)
print(f"{len(field_starts_idx)} v-sync events found", flush=True)
if len(field_starts_idx) < 2:
    sys.exit("not enough v-syncs")

# Mark which edges are "H-sync" (gap to next edge ~1 line); reject equalising/broad.
hsync_mask = np.zeros(len(edges_precise), dtype=bool)
for i in range(len(edges_precise) - 1):
    gap = edges_precise[i+1] - edges_precise[i]
    if 0.9 * samples_per_line < gap < 1.1 * samples_per_line:
        hsync_mask[i] = True

# Take TWO consecutive fields (one frame's worth) and interleave them.
# PAL is interlaced — each field scans every OTHER scanline. Top of letter A
# (a single thin scanline) lives in only ONE of the two fields. Rendering
# just one field loses half the vertical resolution and drops thin features.
VBI_LEADING_LINES = 14
def extract_field(start_idx_in_edges):
    """Extract ACTIVE_LINES rows starting VBI_LEADING_LINES H-syncs after start."""
    rows = []
    i = start_idx_in_edges
    skipped = 0
    while i < len(edges_precise) and skipped < VBI_LEADING_LINES:
        if hsync_mask[i]:
            skipped += 1
        i += 1
    while i < len(edges_precise) and len(rows) < 287:
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
        blanking = float(np.median(line_raw))
        rows.append(line_raw - blanking)
        i += 1
    return np.array(rows)

fi = min(4, len(field_starts_idx) - 2)
f0 = extract_field(field_starts_idx[fi])
print(f"f0={f0.shape[0]} lines", flush=True)
# Don't interleave: our v-sync detection lands at the same screen position
# in both fields (it fires on a character-pattern repeating once per field
# rather than actual TV v-sync), so f1 would just be a duplicate of f0
# rather than the interlaced complement. Use single field, doubled for
# proper vertical aspect.
frame = np.repeat(f0, 2, axis=0)
# Roll the frame so the FIRST bright row (top of visible content) is at row 0.
# Our v-sync detection lands at an empirically-stable position that is NOT
# the actual TV v-sync (likely fires on a half-line-spaced character pattern
# inside the BBC's mode 7 text). Rather than fight it, we use the known
# property "boot text is at the top of the screen" to find the right roll.
peak_per_row = frame.max(axis=1)
white_threshold = peak_per_row.max() * 0.6
bright_rows = np.where(peak_per_row > white_threshold)[0]
if len(bright_rows) > 0:
    # Find the largest gap between bright rows — that's "back of screen to
    # front of screen" wrap. The row JUST AFTER the largest gap is the top.
    gaps = np.diff(np.r_[bright_rows, bright_rows[0] + frame.shape[0]])
    max_gap_idx = int(np.argmax(gaps))
    top_row = int(bright_rows[(max_gap_idx + 1) % len(bright_rows)])
    print(f"first bright row in roll-order: {top_row}", flush=True)
    if 5 < top_row < frame.shape[0] - 5:
        frame = np.roll(frame, -top_row + 8, axis=0)   # +8 for a tiny top border
        print(f"rolled by {-top_row + 8} so first text row near top", flush=True)

# Render: clip below 0 to pure black; scale 99.9% to white.
white_level = float(np.percentile(frame, 99.9))
print(f"white peak = {white_level:.0f}", flush=True)
img = frame / max(1, white_level) * 255
img = np.clip(img, 0, 255).astype(np.uint8)
h, w = img.shape

# Optional: double rows vertically for 1:1 aspect.
# No vertical doubling — interleave already gave us full vertical resolution.

# Downsample horizontally to 720 px.
target_w = 720
if img.shape[1] > target_w:
    factor = img.shape[1] // target_w
    img = img[:, :factor*target_w].reshape(img.shape[0], target_w, factor).mean(axis=2).astype(np.uint8)

with open(dst, "wb") as f:
    f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
    f.write(img.tobytes())
print(f"wrote {dst} ({img.shape[1]}×{img.shape[0]} PGM)", flush=True)
