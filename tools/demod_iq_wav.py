#!/usr/bin/env python3
"""AM-envelope demod from an SDR Console IQ .wav (int16 stereo, I=ch0 Q=ch1).
Assumes carrier is at 0 Hz (i.e. SDR was tuned exactly on the vision carrier).
If not, pass --carrier-offset HZ to shift before envelope detection.

Writes int16 LE baseband CVBS to <output> at the same sample rate.
"""
import sys, wave, argparse, numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("input")
ap.add_argument("output")
ap.add_argument("--carrier-offset", type=float, default=0.0,
                help="Hz shift before envelope detection (if peak isn't at 0 Hz)")
args = ap.parse_args()

w = wave.open(args.input, "rb")
assert w.getnchannels() == 2 and w.getsampwidth() == 2, "expected int16 stereo IQ"
fs = w.getframerate()
n = w.getnframes()
print(f"input: {n} frames @ {fs} Hz = {n/fs:.2f}s")

# Read all (5.7s × 16 MHz × 2ch × 2B = ~365 MB — fits)
raw = w.readframes(n)
w.close()
iq = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, 2)
I = iq[:, 0]
Q = iq[:, 1]
print(f"I std={I.std():.1f}  Q std={Q.std():.1f}")

bb = I + 1j * Q
if args.carrier_offset != 0.0:
    t = np.arange(len(bb), dtype=np.float64)
    bb = bb * np.exp(-2j * np.pi * args.carrier_offset / fs * t)
    print(f"shifted by {args.carrier_offset/1e3:.1f} kHz")

# Envelope
env = np.abs(bb).astype(np.float32)
print(f"envelope mean={env.mean():.1f} std={env.std():.1f} min={env.min():.1f} max={env.max():.1f}")

# Quick line-rate sanity check (should be VERY visible at +20 dB+ now)
N = min(int(0.5 * fs), len(env))
e_slice = env[:N] - env[:N].mean()
E = np.abs(np.fft.rfft(e_slice))
ef = np.fft.rfftfreq(N, 1.0 / fs)
mask = (ef > 1e3) & (ef < 200e3)
top = np.argsort(E[mask])[::-1][:8]
floor = np.median(E[mask])
print("\nTop envelope-modulation peaks (1..200 kHz):")
for i in top:
    print(f"  {ef[mask][i]/1e3:8.3f} kHz  amp={E[mask][i]:.0f}  +{20*np.log10(E[mask][i]/max(floor,1e-9)):.1f} dB")
idx = np.argmin(np.abs(ef[mask] - 15625))
print(f"\nat 15.625 kHz: +{20*np.log10(E[mask][idx]/max(floor,1e-9)):.1f} dB")

# PAL/B/G/I uses NEGATIVE modulation: sync tip = MAX carrier amplitude
# (i.e. max envelope), white = min envelope. Our downstream visualiser
# and cvbs-decode expect the opposite convention (sync = LOW value).
# So invert the envelope before writing.
peak = float(np.percentile(env, 99.99))
floor = float(np.percentile(env,  0.01))
# Map [floor..peak] envelope → [+24000..-24000] int16 (inverted).
span = max(peak - floor, 1.0)
out = np.clip((peak - env) / span * 48000.0 - 24000.0,
              -32760, 32760).astype(np.int16)
with open(args.output, "wb") as f:
    f.write(out.tobytes())
print(f"\nwrote {args.output} ({len(out)} int16 samples @ {fs} Hz)")
