#!/usr/bin/env python3
"""Quick PSD of RX888 raw real-int16 capture to spot PAL vision carrier."""
import sys
import numpy as np

path = sys.argv[1]
fs = float(sys.argv[2]) if len(sys.argv) > 2 else 50e6  # ADC sample rate
# We sample real samples — Nyquist is fs/2.

# Skip ~0.2s of warm-up, then take ~4M samples for the FFT.
skip = int(0.2 * fs)
n = 1 << 22   # 4_194_304 samples => bin width ~12 Hz at 50 Msps
with open(path, "rb") as f:
    f.seek(skip * 2)
    raw = f.read(n * 2)
x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
print(f"loaded {len(x)} samples; mean={x.mean():.1f} std={x.std():.1f} "
      f"min={x.min():.0f} max={x.max():.0f}")

# Welch-style PSD via averaged periodograms (cheap, no scipy needed).
seg = 1 << 16        # 65536-bin FFT  -> ~763 Hz bins @ 50 Msps
nsegs = len(x) // seg
x = x[: nsegs * seg].reshape(nsegs, seg)
win = np.hanning(seg).astype(np.float32)
x = (x - x.mean(axis=1, keepdims=True)) * win
spec = np.fft.rfft(x, axis=1)
psd = (np.abs(spec) ** 2).mean(axis=0)
psd_db = 10 * np.log10(psd + 1e-12)
freqs = np.fft.rfftfreq(seg, d=1.0 / fs)

# Print top peaks (above the median by >= 15 dB).
order = np.argsort(psd_db)[::-1]
floor = np.median(psd_db)
print(f"\nNoise floor (median PSD): {floor:.1f} dB")
print(f"Peak PSD: {psd_db.max():.1f} dB at {freqs[psd_db.argmax()]/1e6:.3f} MHz "
      f"(SNR over floor: {psd_db.max()-floor:.1f} dB)")
print("\nTop 12 peaks (>= 15 dB over floor), greedy-deduped by 200 kHz:")
shown, taken = 0, []
for idx in order:
    f_mhz = freqs[idx] / 1e6
    if psd_db[idx] - floor < 15:
        break
    if any(abs(f_mhz - t) < 0.2 for t in taken):
        continue
    taken.append(f_mhz)
    print(f"  {f_mhz:8.3f} MHz   {psd_db[idx]:6.1f} dB   "
          f"(+{psd_db[idx]-floor:5.1f} dB)")
    shown += 1
    if shown >= 12:
        break

# Crude ASCII spectrum: downsample to ~100 bins for the printout.
print("\nCoarse spectrum (linear bins across 0..fs/2):")
bins = 100
chunk = len(psd_db) // bins
coarse = psd_db[: bins * chunk].reshape(bins, chunk).max(axis=1)
lo, hi = coarse.min(), coarse.max()
width = 60
for i, v in enumerate(coarse):
    f_mhz = i * (fs / 2 / 1e6) / bins
    bar = "#" * int((v - lo) / max(hi - lo, 1) * width)
    print(f"{f_mhz:6.2f} MHz | {bar}")
