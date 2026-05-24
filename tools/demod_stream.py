#!/usr/bin/env python3
"""Streaming chunked demod, architecturally cleaner.

Pipeline (per chunk, identical to how a C++ DSP loop would look):

  raw int16 in (24 MSps)
    -> mix down at fc via cos/sin LO  (real I, real Q at 24 MSps)
    -> FIR LPF + decimate by 3        (real I, real Q at 8 MSps)
    -> envelope = sqrt(I^2 + Q^2)
    -> invert + scale to int16
  cvbs int16 out (8 MSps)

Streaming state held between chunks:
  - sample_index_total       (int64, for phase continuity)
  - lpf_state_i, lpf_state_q (last NTAP-1 samples of each baseband
                              channel, for overlap-save FIR)

This is what the C++ port would do: ring buffer in, NCO step per
sample, polyphase decimating FIR, envelope. The only Python-specific
shortcut is scipy.signal.oaconvolve doing overlap-add internally per
chunk; in C++ you'd write the overlap-save explicitly.
"""
import sys, time
import numpy as np
from scipy.signal import firwin, oaconvolve

FS_IN  = 24_000_000
DECIM  = 3
FS_OUT = FS_IN // DECIM
LPF_HZ = 3_000_000
NTAP   = 257
CHUNK_S = 0.25
CHUNK   = int(FS_IN * CHUNK_S)

print(f"[stream] fs_in={FS_IN/1e6}MSps, decim={DECIM}, fs_out={FS_OUT/1e6}MSps, "
      f"chunk={CHUNK_S}s ({CHUNK} samples in)", file=sys.stderr)

# --- carrier auto-detect: read first 1 s ---
DETECT_S = 1.0
detect_n = int(FS_IN * DETECT_S)
buf0 = sys.stdin.buffer.read(detect_n * 2)
if len(buf0) < (detect_n//2) * 2:
    sys.exit("not enough input for carrier detect")
detect_samples = np.frombuffer(buf0, dtype='<i2').astype(np.float32)
ds = detect_samples - detect_samples.mean()
seg = 1 << 20
nsegs = max(1, len(ds) // seg)
xs = ds[:nsegs*seg].reshape(nsegs, seg) * np.hanning(seg).astype(np.float32)
psd = (np.abs(np.fft.rfft(xs, axis=1))**2).mean(axis=0)
f = np.fft.rfftfreq(seg, 1.0/FS_IN)
mask = (f > 3e6) & (f < 6e6)
fc = float(f[mask][np.argmax(psd[mask])])
print(f"[stream] carrier = {fc/1e6:.4f} MHz", file=sys.stderr)

# Precomputed LPF
lpf = firwin(NTAP, LPF_HZ / (FS_IN/2), window='hamming').astype(np.float32)
phase_step = -2.0 * np.pi * fc / FS_IN

# Streaming state
sample_index_total = 0           # int64 in C++
# Overlap-save: between chunks we keep the last NTAP-1 samples of the
# baseband signal so the FIR can produce a continuous output stream.
# (oaconvolve on a chunk alone has edge transients; overlap-save fixes that.)
overlap_i = np.zeros(NTAP - 1, dtype=np.float32)
overlap_q = np.zeros(NTAP - 1, dtype=np.float32)
# To stay aligned with DECIM, we'll feed (NTAP-1 + chunk) samples
# through the filter and discard the first NTAP-1 outputs of the convolution.

# Inversion: maintain a long-window peak/floor estimator so AM scaling is
# stable across chunks. Update once per chunk from local stats blended
# with running estimates.
peak_est  = None
floor_est = None
PEAK_FLOOR_ALPHA = 0.1

def process_chunk(samples_f32: np.ndarray) -> bytes:
    """Returns int16 LE bytes of one chunk of CVBS output."""
    global sample_index_total, overlap_i, overlap_q, peak_est, floor_est

    n = len(samples_f32)

    # 1. NCO mix down
    idx = np.arange(sample_index_total, sample_index_total + n, dtype=np.float64)
    phase = phase_step * idx
    lo_r = np.cos(phase).astype(np.float32)
    lo_i = np.sin(phase).astype(np.float32)
    bb_r = samples_f32 * lo_r
    bb_i = samples_f32 * lo_i
    sample_index_total += n

    # 2. Overlap-save LPF + decimate
    # Prepend last NTAP-1 samples from previous chunk so the convolution
    # has no edge transient.
    bb_r_padded = np.concatenate([overlap_i, bb_r])
    bb_q_padded = np.concatenate([overlap_q, bb_i])
    I_full = oaconvolve(bb_r_padded, lpf, mode='valid').astype(np.float32)
    Q_full = oaconvolve(bb_q_padded, lpf, mode='valid').astype(np.float32)
    # Update overlap state to the tail of the unfiltered baseband
    overlap_i = bb_r[-(NTAP - 1):].copy()
    overlap_q = bb_i[-(NTAP - 1):].copy()

    # 3. Decimate
    I = I_full[::DECIM]
    Q = Q_full[::DECIM]

    # 4. Envelope detect
    env = np.sqrt(I*I + Q*Q)

    # 5. Track peak/floor (long-window EMA)
    local_peak  = float(np.percentile(env, 99.5))
    local_floor = float(np.percentile(env, 0.5))
    if peak_est is None:
        peak_est, floor_est = local_peak, local_floor
    else:
        peak_est  += PEAK_FLOOR_ALPHA * (local_peak  - peak_est)
        floor_est += PEAK_FLOOR_ALPHA * (local_floor - floor_est)
    span = max(peak_est - floor_est, 1.0)

    # 6. Invert (PAL negative modulation) and quantise to int16
    out = np.clip((peak_est - env) / span * 48000.0 - 24000.0,
                  -32760, 32760).astype(np.int16)
    return out.tobytes()

# Process the carrier-detect buffer first
t0 = time.time()
sys.stdout.buffer.write(process_chunk(detect_samples))

chunk_count = 1
while True:
    buf = sys.stdin.buffer.read(CHUNK * 2)
    if not buf:
        break
    samples = np.frombuffer(buf, dtype='<i2').astype(np.float32)
    sys.stdout.buffer.write(process_chunk(samples))
    chunk_count += 1

elapsed = time.time() - t0
in_seconds = sample_index_total / FS_IN
print(f"[stream] processed {sample_index_total} samples = {in_seconds:.2f}s "
      f"in {elapsed:.2f}s ({in_seconds/elapsed:.2f}× realtime, {chunk_count} chunks)",
      file=sys.stderr)
