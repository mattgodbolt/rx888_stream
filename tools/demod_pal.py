#!/usr/bin/env python3
"""AM-envelope demodulate the RX888 PAL IF capture into baseband CVBS.

Input:  real int16 LE samples at fs (e.g. /tmp/sms_ch36.iq @ 50 Msps)
Output: real int16 LE samples at fs (baseband composite video)
"""
import sys
import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi

src = sys.argv[1]
dst = sys.argv[2]
fs = float(sys.argv[3])       # input sample rate, e.g. 50e6
fc = float(sys.argv[4])       # vision-carrier IF freq, e.g. 10.799e6
skip_s = float(sys.argv[5]) if len(sys.argv) > 5 else 0.2

# Wider than the 5.5 MHz sideband to keep chroma at 4.43 MHz intact, but
# narrow enough to reject the FM sound carrier (5.5–6 MHz away in the IF).
lpf_cutoff = 5.5e6
# Long FIR — narrow transition between 5.5 MHz video and 6 MHz sound carrier.
ntaps = 257
lpf = firwin(ntaps, lpf_cutoff / (fs / 2), window="hamming").astype(np.float32)

# Pass 1: streaming envelope demod to a temp float32 file, gather stats.
chunk = 1 << 22   # 4 Mi samples per chunk
skip_samp = int(skip_s * fs)

zi_re = lfilter_zi(lpf, 1).astype(np.float32) * 0
zi_im = lfilter_zi(lpf, 1).astype(np.float32) * 0
n_off = 0  # global sample index, to keep the LO phase continuous

# Histogram for sync/white level estimation.
hist = np.zeros(65536, dtype=np.int64)

tmp = dst + ".env.f32"
with open(src, "rb") as fi, open(tmp, "wb") as ft:
    fi.seek(skip_samp * 2)
    total = 0
    while True:
        raw = fi.read(chunk * 2)
        if not raw:
            break
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        n = len(x)
        idx = np.arange(n_off, n_off + n, dtype=np.float64)
        n_off += n
        # Complex baseband shift: y = x * exp(-j*2π*fc/fs*n)
        phase = (-2.0 * np.pi * fc / fs) * idx
        yr = (x * np.cos(phase)).astype(np.float32)
        yi = (x * np.sin(phase)).astype(np.float32)
        # LPF I and Q (state-carrying).
        yr, zi_re = lfilter(lpf, [1.0], yr, zi=zi_re)
        yi, zi_im = lfilter(lpf, [1.0], yi, zi=zi_im)
        env = np.sqrt(yr * yr + yi * yi).astype(np.float32)
        ft.write(env.tobytes())
        # Histogram for level detection (clip to ±16k then offset to 0..32k).
        # Envelope is non-negative; bin into 0..65535 using quantile-friendly map.
        # We'll just bin raw values into 16-bit buckets after rough scaling later.
        # Track min/max/sum here.
        if total == 0:
            mn, mx, sm, ct = env.min(), env.max(), float(env.sum()), n
        else:
            mn = min(mn, env.min()); mx = max(mx, env.max())
            sm += float(env.sum()); ct += n
        total += n

mean = sm / ct
print(f"envelope: n={total}  min={mn:.1f}  max={mx:.1f}  mean={mean:.1f}", flush=True)

# Pass 2: read the envelope back, build a histogram to find sync tip & white.
# Sync tip = highest carrier (envelope max-region). White = lowest carrier.
# Estimate via percentiles to be robust.
print("scanning percentiles…", flush=True)
sample_step = max(1, total // 2_000_000)   # subsample for speed
samples = []
with open(tmp, "rb") as ft:
    while True:
        buf = ft.read(chunk * 4)
        if not buf:
            break
        a = np.frombuffer(buf, dtype=np.float32)
        samples.append(a[::sample_step])
samples = np.concatenate(samples)
p_sync = np.percentile(samples, 99.5)   # sync tip (peak carrier)
p_white = np.percentile(samples, 2.0)   # white (low carrier)
print(f"sync_tip≈{p_sync:.1f}  white≈{p_white:.1f}  contrast={p_sync-p_white:.1f}", flush=True)

# Map envelope to int16 CVBS:
#   PAL CVBS: sync tip = -0.3 V (-43 IRE),  white = +0.7 V (+100 IRE),
#   blanking = 0 V (0 IRE).  Use signed 16-bit, full-scale ±28000 ish.
#   ld-decode lookups expect: sync ≈ low values, white ≈ high values.
# Negative modulation: high carrier (sync) -> high envelope -> map to LOW int16.
# So invert: out = -env, then scale.
#
# Set sync tip (high env) -> -10000, white (low env) -> +24000.
SYNC_OUT = -16000.0
WHITE_OUT = +24000.0
scale = (WHITE_OUT - SYNC_OUT) / (p_white - p_sync)     # p_white < p_sync? no: p_white is LOW env, p_sync is HIGH env. So denom is negative.
offset = SYNC_OUT - scale * p_sync
print(f"scale={scale:.4f}  offset={offset:.1f}", flush=True)

print("writing scaled output…", flush=True)
with open(tmp, "rb") as ft, open(dst, "wb") as fo:
    while True:
        buf = ft.read(chunk * 4)
        if not buf:
            break
        a = np.frombuffer(buf, dtype=np.float32)
        out = a * scale + offset
        np.clip(out, -32767, 32767, out=out)
        fo.write(out.astype("<i2").tobytes())

import os
os.unlink(tmp)
print(f"done. wrote {dst} ({total} samples @ {fs/1e6:.3f} Msps)")
