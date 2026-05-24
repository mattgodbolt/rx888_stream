#!/usr/bin/env python3
"""Render a single PAL field from baseband CVBS to a still image.

v6: TV-style sync separator architecture.

A 1980s analog TV doesn't measure "line rate" — it does:

  1. Sync separator (a comparator/slicer): anything below the sync threshold
     is "sync active". No frequency analysis required.
  2. Pulse classifier by duration:
       short (~4.7 µs) → H-sync or equalizing pulse
       broad (~27 µs) → V-sync pulse
  3. V-sync detection: a run of broad pulses (PAL has 5; we accept 3+).
  4. Per-line H-sync anchoring: each line of active video is anchored on
     its own H-sync falling edge, so any per-line phase jitter is
     absorbed rather than allowed to accumulate.

This replaces the v5 mix of "median of H-sync gaps" and "FFT-based line
rate detection", neither of which a TV would do.

Usage:
    cvbs_to_image_v6.py SRC.s16 DST.{png,pgm} [fs [field]]

Args:
    SRC.s16:  baseband CVBS, int16 LE, sync at the NEGATIVE end of the
              range (i.e. demod_real.py output).
    DST:      .png/.jpg/etc (PIL needed) or .pgm (zero deps).
    fs:       sample rate (Hz). Default 16e6.
    field:    which field to render (0-based). Default 0.
"""
import sys
import numpy as np

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3]) if len(sys.argv) > 3 else 16e6
which_field = int(sys.argv[4]) if len(sys.argv) > 4 else 0

# PAL constants (System B/G/I horizontal timing)
HSYNC_S       = 4.7e-6
BACK_PORCH_S  = 5.7e-6
ACTIVE_S      = 52.0e-6
BROAD_PULSE_S = 27.3e-6
EQ_PULSE_S    = 2.35e-6
LINE_S        = 64.0e-6  # nominal — only used for sanity bounds

ACTIVE_LINES        = 287
VBI_LINES_PAL       = 17   # field-start to first active line, after vsync run

sync_skip_samp     = int(round((HSYNC_S + BACK_PORCH_S) * fs))
active_samples     = int(round(ACTIVE_S * fs))
back_porch_samples = max(4, int(round(2.0e-6 * fs)))

# Duration buckets for pulse classifier
# H-sync and equalizing pulses are short (~4.7 µs and ~2.35 µs).
# V-sync broad pulses are nominally ~27 µs each, but if the demod chain has a
# tight luma LPF the brief return-to-black serrations between adjacent broad
# pulses get smoothed away and pairs of broad pulses merge into ~59 µs runs.
# So we just say "anything substantially longer than an H-sync is broad".
H_PULSE_MIN_S = 3.5e-6
H_PULSE_MAX_S = 6.0e-6
B_PULSE_MIN_S = 15.0e-6  # ~3× an H-sync — comfortably broad-pulse territory

# ------------------------------------------------------------------
# Load CVBS
with open(src, "rb") as f:
    x = np.frombuffer(f.read(), dtype="<i2").astype(np.float32)
# Skip 0.1 s of warm-up
x = x[int(0.1 * fs):]
print(f"loaded {len(x)} samples = {len(x)/fs:.2f} s at fs={fs/1e6:g} MHz", flush=True)

# ------------------------------------------------------------------
# Step 1: sync separator
# Sync tip is the lowest-amplitude section of CVBS (below black). We pick
# a threshold halfway between the sync-tip percentile and the black-level
# percentile. Robust to overall signal amplitude variation.
sync_tip = float(np.percentile(x, 0.5))     # sync = bottom 0.5% of values
black    = float(np.percentile(x, 30.0))    # black ≈ 30th percentile
sync_thr = 0.5 * (sync_tip + black)
print(f"sync separator: sync_tip≈{sync_tip:.0f}, black≈{black:.0f}, "
      f"threshold={sync_thr:.0f}", flush=True)

is_sync = x < sync_thr  # bool array

# ------------------------------------------------------------------
# Step 2: find sync-pulse runs (start_idx, end_idx, duration_samples)
# A "pulse" is a contiguous run of is_sync == True.
# Use diff-of-bool trick for O(n) discovery.
edges = np.diff(is_sync.astype(np.int8))
starts = np.where(edges == 1)[0] + 1   # bool went False -> True
stops  = np.where(edges == -1)[0] + 1  # bool went True -> False
# Pair them up, handling boundary cases
if len(starts) == 0 or len(stops) == 0:
    sys.exit("no sync pulses detected — capture is not a CVBS signal?")
if stops[0] < starts[0]:
    stops = stops[1:]
n = min(len(starts), len(stops))
starts = starts[:n]; stops = stops[:n]
durations = stops - starts
print(f"{n} sync pulses detected", flush=True)

# Classify
H_MIN = int(round(H_PULSE_MIN_S * fs))
H_MAX = int(round(H_PULSE_MAX_S * fs))
B_MIN = int(round(B_PULSE_MIN_S * fs))

is_hsync = (durations >= H_MIN) & (durations <= H_MAX)
is_broad = durations >= B_MIN
print(f"  H-sync-class: {is_hsync.sum()}  "
      f"broad-pulse-class: {is_broad.sum()}  "
      f"(equalizing/other: {n - is_hsync.sum() - is_broad.sum()})", flush=True)

# ------------------------------------------------------------------
# Step 3: V-sync = run of >=3 consecutive broad pulses
# Walk through broad-pulse-flagged pulses, group those within 1 line
# period of each other, accept groups of size >= 3.
vsync_anchors = []  # list of pulse indices marking the START of each V-sync group
i = 0
line_samp = int(LINE_S * fs)
while i < n:
    if is_broad[i]:
        # Start a group
        group_start = i
        last = i
        j = i + 1
        while j < n and is_broad[j] and (starts[j] - starts[last]) < 1.5 * line_samp:
            last = j
            j += 1
        if (last - group_start + 1) >= 3:
            vsync_anchors.append(group_start)
        i = j
    else:
        i += 1
print(f"V-sync runs detected: {len(vsync_anchors)}", flush=True)
if not vsync_anchors:
    sys.exit("no V-sync found — check capture quality")
if which_field >= len(vsync_anchors):
    sys.exit(f"requested field {which_field} but only {len(vsync_anchors)} V-sync runs found")

# ------------------------------------------------------------------
# Step 4: find the H-sync edges for active video
# After a V-sync run, there are equalizing pulses, then VBI lines (full-line
# H-sync gaps), then active video begins. Walk forward from the END of the
# V-sync group, accepting H-sync-class pulses with full-line gaps.

vs_idx = vsync_anchors[which_field]
# Find end of THIS V-sync group
j = vs_idx
while j + 1 < n and is_broad[j + 1] and (starts[j+1] - starts[j]) < 1.5 * line_samp:
    j += 1
vs_end_idx = j
vs_end_samp = starts[vs_end_idx]
print(f"selected field {which_field}: V-sync ends at pulse #{vs_end_idx}, "
      f"sample {vs_end_samp} ({vs_end_samp/fs*1000:.2f} ms into capture)", flush=True)

# Walk forward looking for H-sync-class pulses that are roughly a full
# line apart. Skip the equalizing-pulse region (half-line gaps).
hsync_indices = []
prev = None
k = vs_end_idx + 1
while k < n and len(hsync_indices) < VBI_LINES_PAL + ACTIVE_LINES + 10:
    if is_hsync[k]:
        if prev is None:
            hsync_indices.append(k)
            prev = k
        else:
            gap = starts[k] - starts[prev]
            # Accept pulses ~one line apart (allow some tolerance). This
            # naturally rejects equalizing pulses which are half a line
            # apart.
            if 0.9 * line_samp < gap < 1.1 * line_samp:
                hsync_indices.append(k)
                prev = k
            elif gap > 1.5 * line_samp:
                # We missed an H-sync somewhere; resync.
                hsync_indices.append(k)
                prev = k
    k += 1

print(f"collected {len(hsync_indices)} consecutive line H-syncs after V-sync", flush=True)
if len(hsync_indices) < VBI_LINES_PAL + 50:
    sys.exit("not enough H-syncs collected to render a field")

# Active video starts at line VBI_LINES_PAL+1
active_start = VBI_LINES_PAL
end = min(len(hsync_indices), active_start + ACTIVE_LINES)
hsync_active = hsync_indices[active_start:end]
print(f"active video uses H-syncs[{active_start}..{end-1}] "
      f"= {len(hsync_active)} lines", flush=True)

# ------------------------------------------------------------------
# Step 5: extract per-line, each anchored on its own H-sync falling edge
rows = []
for pulse_idx in hsync_active:
    edge_samp = float(starts[pulse_idx])  # H-sync falling edge sample
    s_precise = edge_samp + sync_skip_samp
    s = int(s_precise)
    if s + active_samples + 1 > len(x):
        break
    frac = s_precise - s
    a = x[s:s + active_samples]
    b = x[s + 1:s + active_samples + 1]
    line = a * (1 - frac) + b * frac
    # Back porch DC reference: average a few samples just AFTER sync ends
    # but BEFORE active starts. Roughly samples [HSYNC_S..HSYNC_S+2µs] after
    # the edge — i.e. the early back-porch.
    bp_skip = int(round(HSYNC_S * fs))
    bp_end  = bp_skip + back_porch_samples
    # The "line" array starts at s = edge + sync_skip, so back-porch is
    # actually at NEGATIVE offsets within `line`. Just use the first few
    # samples of line as a DC reference (close enough — they're still in
    # the late back-porch).
    black_lvl = float(line[:back_porch_samples].mean())
    rows.append(line - black_lvl)

field = np.array(rows)
print(f"extracted {field.shape[0]} active lines", flush=True)
if field.shape[0] == 0:
    sys.exit("zero lines extracted — capture truncated?")

# ------------------------------------------------------------------
# Render
frame = np.repeat(field, 2, axis=0)  # double vertically — single field

black_level = float(np.percentile(frame, 5))
white_level = float(np.percentile(frame, 99))
img = (frame - black_level) / max(white_level - black_level, 1.0) * 255
img = np.clip(img, 0, 255).astype(np.uint8)

# Resize to 720 wide. Previous integer-factor binning was buggy: with 1248
# native samples per line and target_w=720, `1248 // 720 = 1` so it
# truncated to the first 720 samples — losing ~42% on the right.
# Use PIL's bilinear resize (or skip if it'd actually downsample by an
# integer factor cleanly, which it won't at 24 MSps active=52µs).
target_w = 720
if img.shape[1] != target_w:
    try:
        from PIL import Image
        img = np.asarray(
            Image.fromarray(img).resize((target_w, img.shape[0]), Image.LANCZOS),
            dtype=np.uint8,
        )
    except ImportError:
        # No PIL: leave at native width. PGM/PNG both handle wider just fine.
        pass

ext = dst.rsplit(".", 1)[-1].lower() if "." in dst else "pgm"
if ext == "pgm":
    with open(dst, "wb") as f:
        f.write(f"P5\n{img.shape[1]} {img.shape[0]}\n255\n".encode())
        f.write(img.tobytes())
else:
    try:
        from PIL import Image
    except ImportError:
        sys.exit("PIL/Pillow not available — install python3-pil or use a .pgm extension")
    Image.fromarray(img).save(dst)
print(f"wrote {dst} ({img.shape[1]}×{img.shape[0]})", flush=True)
