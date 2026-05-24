#!/usr/bin/env python3
"""AM-envelope demod for a raw real-int16 capture from `rx888_stream`.

Unlike demod_iq_wav.py (which expects an SDR Console IQ WAV with the
carrier already at 0 Hz), this script takes the raw int16 LE stream that
`rx888_stream` produces with `-o file` and:

  1. Finds the strongest narrow peak in the expected IF region — the
     vision carrier translated to IF by the R828D tuner.
  2. Downconverts at that carrier frequency to bring it to 0 Hz.
  3. Low-pass filters to keep the luma (typ. 3 MHz) and drop the PAL
     colour subcarrier at +4.43 MHz which would otherwise show as a
     herringbone artefact in the rendered image.
  4. Envelope-detects (|I + jQ|), inverts (PAL B/G/I is negative
     modulation: sync tip = max carrier amplitude), and writes int16 LE
     baseband CVBS at the same sample rate.

Usage:
  python3 tools/demod_real.py CAPTURE.bin CVBS.s16 --fs 24e6

  # With explicit carrier (skip auto-detect):
  python3 tools/demod_real.py CAPTURE.bin CVBS.s16 --fs 24e6 --carrier 4.57e6

  # Wider passband (e.g. for chroma decode work):
  python3 tools/demod_real.py CAPTURE.bin CVBS.s16 --fs 24e6 --lpf 5.5e6

Carrier auto-detect range defaults to 3-6 MHz, which is where the SDDC
firmware's R828D config lands the vision carrier (`R828D_IF_CARRIER` =
4.57 MHz). Override with --carrier-search-lo/--carrier-search-hi if you
know the carrier is somewhere else.
"""
import argparse, sys
import numpy as np
from scipy.signal import firwin, oaconvolve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="raw real int16 LE capture from rx888_stream")
    ap.add_argument("output", help="baseband CVBS int16 LE output")
    ap.add_argument("--fs", type=float, default=24e6,
                    help="ADC sample rate (Hz). Default 24e6.")
    ap.add_argument("--carrier", type=float, default=None,
                    help="Vision-carrier IF frequency in Hz. If unset, auto-detect.")
    ap.add_argument("--carrier-search-lo", type=float, default=3.0e6,
                    help="Auto-detect search range low (Hz). Default 3 MHz.")
    ap.add_argument("--carrier-search-hi", type=float, default=6.0e6,
                    help="Auto-detect search range high (Hz). Default 6 MHz.")
    ap.add_argument("--lpf", type=float, default=3.0e6,
                    help="Post-mix LPF cutoff (Hz). Default 3e6 (luma only — "
                         "drops PAL chroma at +4.43 MHz). Use 5.5e6 to keep chroma.")
    ap.add_argument("--ntap", type=int, default=257,
                    help="LPF FIR tap count. Default 257.")
    ap.add_argument("--skip-secs", type=float, default=0.05,
                    help="Skip this many seconds at the start (warm-up). Default 0.05.")
    ap.add_argument("--demod", choices=("envelope", "sync", "bluestein"),
                    default="envelope",
                    help="'envelope' (default): |I+jQ|, the naïve AM detector. "
                         "Known to produce chroma trails on VSB sources (PAL "
                         "B/G is VSB). 'sync': estimate residual carrier "
                         "frequency error from the analytic-signal spectrum, "
                         "refine the LO, take Real(I+jQ) only — discards the "
                         "Q² quadrature-distortion term entirely. 'bluestein': "
                         "keep |I+jQ| but subtract the Q²/(2I) distortion term "
                         "analytically.")
    args = ap.parse_args()

    fs = args.fs
    x = np.fromfile(args.input, dtype="<i2").astype(np.float32)
    print(f"loaded {len(x)} samples = {len(x)/fs:.2f} s at fs={fs/1e6:g} MHz",
          file=sys.stderr)
    if len(x) == 0:
        sys.exit("input is empty")

    skip = int(args.skip_secs * fs)
    x = x[skip:] - x[skip:].mean()

    if args.carrier is None:
        # Welch PSD on a chunk near the start to locate the carrier.
        n = min(len(x), 1 << 24)
        seg = 1 << 20
        nsegs = max(1, n // seg)
        xs = x[:nsegs*seg].reshape(nsegs, seg) * np.hanning(seg).astype(np.float32)
        spec = np.fft.rfft(xs, axis=1)
        psd = (np.abs(spec) ** 2).mean(axis=0)
        f = np.fft.rfftfreq(seg, 1.0 / fs)
        mask = (f > args.carrier_search_lo) & (f < args.carrier_search_hi)
        if not mask.any():
            sys.exit(f"carrier search range {args.carrier_search_lo}..{args.carrier_search_hi} "
                     f"out of FFT range [0..{f[-1]:g} Hz]")
        idx = np.argmax(psd[mask])
        fc = float(f[mask][idx])
        floor = float(np.median(10 * np.log10(psd[mask] + 1e-12)))
        snr = 10 * np.log10(psd[mask][idx] + 1e-12) - floor
        print(f"auto-detected carrier at {fc/1e6:.4f} MHz "
              f"(~{snr:.1f} dB above local median)", file=sys.stderr)
    else:
        fc = float(args.carrier)
        print(f"using --carrier {fc/1e6:.4f} MHz", file=sys.stderr)

    # Downconvert to baseband: multiply by exp(-j 2pi fc n / fs).
    # Phase MUST be float64 — at ~5 MHz carrier × 100M+ samples, the
    # accumulated phase reaches ~1.7e8 radians, which exceeds float32
    # mantissa precision (only 7 decimal digits) and degenerates to
    # noise. cos/sin themselves can return float32 to save memory.
    print("downconverting...", file=sys.stderr)
    phase = (-2 * np.pi * fc / fs) * np.arange(len(x), dtype=np.float64)
    lo_r = np.cos(phase).astype(np.float32)
    lo_i = np.sin(phase).astype(np.float32)
    del phase
    bb_r = x * lo_r
    bb_i = x * lo_i
    del lo_r, lo_i

    # FIR low-pass via overlap-add FFT convolution: O(N log N) rather than
    # lfilter's O(N × taps). On 100M+ samples that's typically 10× faster.
    print(f"LPF to {args.lpf/1e6:g} MHz ({args.ntap} taps)...", file=sys.stderr)
    lpf = firwin(args.ntap, args.lpf / (fs / 2), window="hamming").astype(np.float32)
    I = oaconvolve(bb_r, lpf, mode='same').astype(np.float32)
    Q = oaconvolve(bb_i, lpf, mode='same').astype(np.float32)
    del bb_r, bb_i

    if args.demod == "envelope":
        # Naïve AM envelope detection. For VSB sources (PAL B/G), this
        # introduces quadrature distortion: the Q² self-product creates a
        # chroma-band ripple that decays slowly past sharp coloured edges
        # — the "chroma trail" / "UV pre-echo on blue" artifact (Bluestein
        # 1979; windytan 2019).
        demoded = np.sqrt(I*I + Q*Q).astype(np.float32)
        print(f"envelope: mean={demoded.mean():.0f} std={demoded.std():.0f} "
              f"range={demoded.min():.0f}..{demoded.max():.0f}", file=sys.stderr)

    elif args.demod == "sync":
        # Synchronous (coherent) demodulation. After the initial mix the
        # signal is at baseband but the LO frequency may be off by a few
        # tens of Hz (FFT carrier-detect resolution + R828D drift). We
        # measure the residual offset from the centroid of |I+jQ|'s
        # spectrum near 0 Hz, refine the LO, then take Real(I+jQ) only.
        # Real(I+jQ) IS the wanted modulating signal m(t) when phase is
        # locked; Q is the unwanted VSB quadrature residue, discarded.
        from scipy.signal import hilbert
        print("estimating residual carrier offset...", file=sys.stderr)
        # complex baseband, downsampled for speed
        cplx = (I + 1j * Q).astype(np.complex64)
        # Residual carrier line is the DC component of |cplx|² should be
        # at exactly 0 Hz if LO is right. Estimate offset from spectrum
        # peak in a narrow window around 0.
        N = min(len(cplx), 1 << 22)
        spec = np.fft.fftshift(np.fft.fft(cplx[:N] * np.hanning(N).astype(np.float32)))
        freqs = np.fft.fftshift(np.fft.fftfreq(N, 1.0 / fs))
        # Search for residual peak within ±20 kHz of DC
        mask = (freqs > -2e4) & (freqs < 2e4)
        idx = np.argmax(np.abs(spec[mask]))
        df_residual = float(freqs[mask][idx])
        print(f"  residual carrier offset: {df_residual:+.2f} Hz "
              f"(refining LO to {(fc+df_residual)/1e6:.6f} MHz)", file=sys.stderr)
        # Derotate
        phase_corr = (-2 * np.pi * df_residual / fs) * np.arange(len(cplx), dtype=np.float64)
        cplx *= np.exp(1j * phase_corr).astype(np.complex64)
        # Now estimate residual STATIC phase from sync-tip samples (where
        # the signal has the largest amplitude, so the modulating signal
        # is most determinate). Sync tip carries no chroma; phase here
        # should be 0 if LO is locked.
        mag = np.abs(cplx)
        sync_thr = np.percentile(mag, 99.5)
        sync_mask = mag > sync_thr
        static_phase = float(np.angle(cplx[sync_mask].sum()))
        print(f"  static residual phase: {np.degrees(static_phase):+.2f}°",
              file=sys.stderr)
        cplx *= np.exp(-1j * static_phase)
        # Real part only
        demoded = cplx.real.astype(np.float32)
        del cplx
        print(f"sync-demod (Re): mean={demoded.mean():.0f} std={demoded.std():.0f} "
              f"range={demoded.min():.0f}..{demoded.max():.0f}", file=sys.stderr)

    elif args.demod == "bluestein":
        # Bluestein's compensation: keep |I+jQ| but subtract Q²/(2I) which
        # is the analytical quadrature-distortion term for a VSB-modulated
        # signal. Cheaper than synchronous demod (no carrier refinement)
        # but only an approximation.
        env = np.sqrt(I*I + Q*Q).astype(np.float32)
        # Floor I to avoid division blowup on near-zero crossings
        I_floor = np.maximum(np.abs(I), 0.05 * np.percentile(np.abs(I), 95))
        correction = (Q * Q) / (2.0 * I_floor)
        demoded = (env - correction).astype(np.float32)
        print(f"bluestein-corrected: env range={env.min():.0f}..{env.max():.0f}, "
              f"correction range={correction.min():.0f}..{correction.max():.0f}",
              file=sys.stderr)

    # PAL B/G/I uses NEGATIVE modulation: sync tip = max signal amplitude.
    # cvbs_decode.py expects sync at the low end, so invert.
    peak = float(np.percentile(demoded, 99.9))
    floor = float(np.percentile(demoded, 0.1))
    span = max(peak - floor, 1.0)
    out = np.clip((peak - demoded) / span * 48000.0 - 24000.0,
                  -32760, 32760).astype(np.int16)
    out.tofile(args.output)
    print(f"wrote {args.output} ({len(out)} int16 samples = {len(out)/fs:.2f} s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
