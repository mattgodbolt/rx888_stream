#!/usr/bin/env python3
"""Demod just a 1 ms window and look at the raw envelope, no scaling."""
import sys, numpy as np
from scipy.signal import firwin, lfilter

fs = 50e6
fc = 10.799e6
src = "/tmp/sms_ch36.iq"
start_s = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
dur_s   = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0005   # 500 µs

with open(src, "rb") as f:
    f.seek(int(start_s * fs) * 2)
    raw = f.read(int(dur_s * fs) * 2)
x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
n = len(x)
print(f"loaded {n} samples = {n/fs*1e6:.1f} µs from t={start_s}s")
print(f"raw stats: mean={x.mean():.1f} std={x.std():.1f} range={x.min():.0f}..{x.max():.0f}")

idx = np.arange(n, dtype=np.float64)
phase = -2*np.pi * fc / fs * idx
yc = x * np.exp(1j * phase).astype(np.complex64)
lpf = firwin(257, 5.5e6 / (fs/2), window="hamming").astype(np.float32)
yc = lfilter(lpf, [1.0], yc.real).astype(np.float32) + 1j*lfilter(lpf, [1.0], yc.imag).astype(np.float32)
env = np.abs(yc).astype(np.float32)
print(f"env stats: mean={env.mean():.1f} std={env.std():.1f} range={env.min():.1f}..{env.max():.1f}")

# Bin to 80 buckets (~6.25 µs each for 500 µs) and ASCII-plot.
bins = 80
chunk = n // bins
y = env[:bins*chunk].reshape(bins, chunk)
ymin = y.min(axis=1); ymax = y.max(axis=1); ymean = y.mean(axis=1)
lo, hi = env.min(), env.max()
W = 70
for i in range(bins):
    pos = int((ymean[i]-lo)/(hi-lo)*W)
    lo_pos = int((ymin[i]-lo)/(hi-lo)*W)
    hi_pos = int((ymax[i]-lo)/(hi-lo)*W)
    bar = [' '] * (W+1)
    for p in range(lo_pos, hi_pos+1):
        bar[p] = '-'
    bar[pos] = '*'
    t = (i*chunk + chunk/2) / fs * 1e6
    print(f'{t:6.1f}µs |{"".join(bar)}| {ymin[i]:.0f}/{ymean[i]:.0f}/{ymax[i]:.0f}')
