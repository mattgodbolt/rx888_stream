#!/usr/bin/env python3
"""Simulate the chroma-trail experiment without hardware.

hacktv `-m b` emits a complex IQ stream that is the **already-down-converted**
PAL-B AM signal — i.e. exactly what an ideal SDR + tuner would deliver at
complex baseband after mixing with the vision-carrier LO. The signal has
VSB asymmetry baked in (I has the wanted modulation, Q has the Hilbert
residue of the missing lower sideband).

We can therefore reproduce the demod chain in pure software:

   1. Read hacktv IQ (already complex baseband, no carrier).
   2. Apply our LPF (same as demod_real.py's 5.5 MHz, 257-tap Hamming).
   3. Pick the demodulator: 'envelope' = |I+jQ| (naïve, has Q² distortion)
                            'sync'     = Re(I+jQ)    (kills Q²)
                            'i-only'   = same as sync but skip phase
                                         tracking (assumes hacktv is locked)
   4. Invert (PAL negative mod) and emit int16 baseband CVBS.

Pipe the output into cvbs_decode.py and compare. Both modes get the
*same* simulated input, so any trail in 'envelope' that's absent in
'sync' is solid evidence for VSB quadrature distortion as the root.
"""

import argparse
import numpy as np
from scipy.signal import firwin, oaconvolve

ap = argparse.ArgumentParser()
ap.add_argument("input", help="hacktv complex IQ output (int16 interleaved)")
ap.add_argument("output", help="output int16 baseband CVBS")
ap.add_argument("--fs", type=float, default=24e6)
ap.add_argument("--lpf", type=float, default=5.5e6)
ap.add_argument("--ntap", type=int, default=257)
ap.add_argument("--demod", choices=("envelope", "sync"), default="envelope")
ap.add_argument("--skip-secs", type=float, default=0.5,
                help="Skip this many seconds at start (hacktv ramp-up)")
args = ap.parse_args()

raw = np.fromfile(args.input, dtype="<i2").astype(np.float32)
n_complex = len(raw) // 2
cplx = raw[0::2] + 1j * raw[1::2]
print(f"loaded {n_complex} complex samples = {n_complex/args.fs:.2f}s")

skip = int(args.skip_secs * args.fs)
cplx = cplx[skip:]

# Remove DC bias (hacktv puts the vision carrier at DC with a strong DC
# component; this is what AM looks like). For envelope detection it
# matters: env = |1 + m(t)|, and the "1" is the DC carrier.
# For sync detection of just m(t), we'd subtract the DC. We KEEP it for
# envelope so the env represents (1 + m). For sync we'll subtract during
# the demod step.

# LPF the I and Q channels separately (same as demod_real.py).
print(f"LPF to {args.lpf/1e6:g} MHz ({args.ntap} taps)...")
lpf = firwin(args.ntap, args.lpf / (args.fs / 2), window='hamming').astype(np.float32)
I = oaconvolve(cplx.real, lpf, mode='same').astype(np.float32)
Q = oaconvolve(cplx.imag, lpf, mode='same').astype(np.float32)

if args.demod == "envelope":
    # The naïve detector. For a VSB AM signal:
    #   env = sqrt(I² + Q²) = sqrt((1+m)² + m_q²) ≈ (1+m) + m_q²/(2(1+m))
    # The m_q²/(2(1+m)) term IS the chroma trail.
    demoded = np.sqrt(I*I + Q*Q).astype(np.float32)
    print(f"envelope: range [{demoded.min():.0f}..{demoded.max():.0f}], "
          f"mean={demoded.mean():.0f} std={demoded.std():.0f}")

elif args.demod == "sync":
    # Synchronous detection: just take Re. Since hacktv emits with vision
    # carrier already at DC (no residual frequency offset), no PLL needed.
    # The signal in I is exactly (1+m); Q is m_q (the VSB Hilbert residue).
    demoded = I.astype(np.float32)
    print(f"sync (Re only): range [{demoded.min():.0f}..{demoded.max():.0f}], "
          f"mean={demoded.mean():.0f} std={demoded.std():.0f}")

# Invert (PAL negative modulation).
peak  = float(np.percentile(demoded, 99.9))
floor = float(np.percentile(demoded, 0.1))
span  = max(peak - floor, 1.0)
out = np.clip((peak - demoded) / span * 48000.0 - 24000.0,
              -32760, 32760).astype(np.int16)
out.tofile(args.output)
print(f"wrote {args.output} ({len(out)} samples)")
