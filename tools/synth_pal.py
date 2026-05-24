#!/usr/bin/env python3
"""Generate a synthetic PAL-like AM-modulated IF capture for pipeline testing.

Produces real int16 LE samples at 50 Msps with a vision carrier at ~10.8 MHz
amplitude-modulated by a synthesised 15.625 kHz horizontal-sync waveform —
i.e. a (very crude) stand-in for what a real PAL signal *should* look like
after the R820T2 has downconverted UHF Ch 36 to its low IF.

Run analyze_rx888.py / demod_pal.py / the 15.625 kHz autocorr & FFT detectors
on the output. If they don't find the line structure here, the bug is in the
analysis code, not in the SMS/RX888 chain.
"""
import sys, numpy as np

fs = 50e6
fc = 10.799e6      # IF carrier
dur_s = 1.0
n = int(fs * dur_s)

# 15.625 kHz line rate, 64 µs period.
line_hz = 15625.0
line_period_s = 1.0 / line_hz
sync_us = 4.7
back_porch_us = 5.7
active_us = 51.9
front_porch_us = 1.65

# Build one line as a CVBS-shape envelope:
#   0 .. sync_us:                sync tip (high carrier amplitude)
#   sync_us .. back_porch:       blanking
#   back_porch .. active:        ramp from black to white (test pattern)
#   active .. end:               front porch (blanking)
samples_per_line = int(round(line_period_s * fs))
t_line = np.arange(samples_per_line) / fs * 1e6  # µs into line

# Map: cvbs[0..1], where 0 = sync tip, 0.3 = blanking, 1 = white
cvbs = np.full(samples_per_line, 0.3, dtype=np.float32)   # blanking baseline
sync_mask = t_line < sync_us
cvbs[sync_mask] = 0.0
active_start = sync_us + back_porch_us
active_end = active_start + active_us
am = (t_line >= active_start) & (t_line < active_end)
cvbs[am] = np.linspace(0.3, 1.0, am.sum(), dtype=np.float32)
fp = t_line >= active_end
cvbs[fp] = 0.3

# Tile to full duration.
nlines = n // samples_per_line + 1
env = np.tile(cvbs, nlines)[:n]

# PAL uses NEGATIVE modulation: carrier amplitude = high at sync, low at white.
# So carrier_amp = 1 - 0.85 * cvbs  (i.e. depth ~85%).
carrier_amp = 1.0 - 0.85 * env

# Modulate.
t = np.arange(n) / fs
signal = carrier_amp * np.cos(2 * np.pi * fc * t)

# Add some noise (signal is fairly strong, low SNR isn't the point here).
rng = np.random.default_rng(42)
noise = rng.standard_normal(n).astype(np.float32) * 0.02
signal = (signal.astype(np.float32) + noise)

# Scale to int16. Put signal at ~ ±10000 amplitude (clear of saturation,
# similar magnitude to a real strong capture).
out = (signal * 10000.0).astype(np.int16)
out_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/synth_pal.iq"
out.tofile(out_path)
print(f"wrote {out_path}: {len(out)} samples = {len(out)/fs:.3f}s "
      f"at {fs/1e6:.1f} Msps, carrier {fc/1e6:.3f} MHz, line {line_hz/1e3} kHz, "
      f"int16 range {out.min()}..{out.max()}")
