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
  - overlap_i, overlap_q     (last NTAP-1 baseband samples for overlap-
                              save FIR continuity)
  - peak_est, floor_est      (EMA so AM-inversion scaling doesn't
                              flicker between chunks)

Working buffers are preallocated once and reused via numpy's `out=`
parameter to keep per-chunk allocation traffic low. This is also the
buffer-layout the C++ port would use: a small fixed set of ring
buffers, no per-frame malloc.
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
if len(buf0) < (detect_n // 2) * 2:
    sys.exit("not enough input for carrier detect")
detect_samples = np.frombuffer(buf0, dtype='<i2').astype(np.float32)
ds = detect_samples - detect_samples.mean()
seg = 1 << 20
nsegs = max(1, len(ds) // seg)
xs = ds[:nsegs * seg].reshape(nsegs, seg) * np.hanning(seg).astype(np.float32)
psd = (np.abs(np.fft.rfft(xs, axis=1)) ** 2).mean(axis=0)
f = np.fft.rfftfreq(seg, 1.0 / FS_IN)
mask = (f > 3e6) & (f < 6e6)
fc = float(f[mask][np.argmax(psd[mask])])
print(f"[stream] carrier = {fc/1e6:.4f} MHz", file=sys.stderr)

# Precomputed LPF
lpf = firwin(NTAP, LPF_HZ / (FS_IN / 2), window='hamming').astype(np.float32)
phase_step = -2.0 * np.pi * fc / FS_IN

# ---- Preallocated working buffers ------------------------------------
# Sized for the largest chunk we'll process (the carrier-detect buffer
# is detect_n samples; subsequent chunks are CHUNK samples).
W_MAX = max(CHUNK, detect_n)

samples_f32_buf = np.empty(W_MAX, dtype=np.float32)             # incoming samples cast to f32
phase_f64       = np.empty(W_MAX, dtype=np.float64)             # phase ramp (needs f64 precision)
cos_f64         = np.empty(W_MAX, dtype=np.float64)
sin_f64         = np.empty(W_MAX, dtype=np.float64)
lo_r_buf        = np.empty(W_MAX, dtype=np.float32)             # downcast LO real
lo_i_buf        = np.empty(W_MAX, dtype=np.float32)             # downcast LO imag
bb_r_buf        = np.empty(W_MAX, dtype=np.float32)             # mixed I (full rate)
bb_i_buf        = np.empty(W_MAX, dtype=np.float32)             # mixed Q (full rate)
# Padded buffers for overlap-save (NTAP-1 prefix + chunk)
padded_i_buf    = np.empty(NTAP - 1 + W_MAX, dtype=np.float32)
padded_q_buf    = np.empty(NTAP - 1 + W_MAX, dtype=np.float32)
# Decimated I/Q and envelope (output rate is W/DECIM)
W_OUT_MAX       = W_MAX // DECIM + 4
env_buf         = np.empty(W_OUT_MAX, dtype=np.float32)
sq_tmp_buf      = np.empty(W_OUT_MAX, dtype=np.float32)
out_buf         = np.empty(W_OUT_MAX, dtype=np.int16)

# Streaming state
sample_index_total = 0
overlap_i = np.zeros(NTAP - 1, dtype=np.float32)
overlap_q = np.zeros(NTAP - 1, dtype=np.float32)
peak_est  = None
floor_est = None
PEAK_FLOOR_ALPHA = 0.1


def process_chunk(samples_in: np.ndarray) -> memoryview:
    """Process one chunk; return a memoryview of the int16 output bytes."""
    global sample_index_total, overlap_i, overlap_q, peak_est, floor_est

    n = len(samples_in)
    # Slice views into the preallocated buffers (no allocation)
    samp_f32 = samples_f32_buf[:n]
    phase    = phase_f64[:n]
    cos_d    = cos_f64[:n]
    sin_d    = sin_f64[:n]
    lo_r     = lo_r_buf[:n]
    lo_i     = lo_i_buf[:n]
    bb_r     = bb_r_buf[:n]
    bb_i     = bb_i_buf[:n]
    pad_i    = padded_i_buf[: NTAP - 1 + n]
    pad_q    = padded_q_buf[: NTAP - 1 + n]

    # 0. Cast int16 -> float32 (in place via out=)
    np.copyto(samp_f32, samples_in, casting='unsafe')

    # 1. Phase ramp: phase[k] = phase_step * (sample_index_total + k)
    #    Compute in f64 for precision then downcast cos/sin to f32.
    #    np.arange doesn't support out=, so we copy into the preallocated buffer.
    phase[:] = np.arange(sample_index_total, sample_index_total + n, dtype=np.float64)
    np.multiply(phase, phase_step, out=phase)

    # 2. cos/sin -> downcast to f32 LO
    np.cos(phase, out=cos_d)
    np.sin(phase, out=sin_d)
    np.copyto(lo_r, cos_d, casting='unsafe')
    np.copyto(lo_i, sin_d, casting='unsafe')

    # 3. Mix: bb = samples * LO (in place)
    np.multiply(samp_f32, lo_r, out=bb_r)
    np.multiply(samp_f32, lo_i, out=bb_i)
    sample_index_total += n

    # 4. Overlap-save assemble: pad = [overlap | bb]
    pad_i[: NTAP - 1] = overlap_i
    pad_i[NTAP - 1:] = bb_r
    pad_q[: NTAP - 1] = overlap_q
    pad_q[NTAP - 1:] = bb_i
    # Save the next chunk's overlap (last NTAP-1 of THIS chunk's bb).
    overlap_i[:] = bb_r[-(NTAP - 1):]
    overlap_q[:] = bb_i[-(NTAP - 1):]

    # 5. FIR LPF (overlap-save, mode='valid' yields exactly n outputs).
    #    oaconvolve does its own allocation internally — we can't pass out=.
    I_full = oaconvolve(pad_i, lpf, mode='valid')  # length n
    Q_full = oaconvolve(pad_q, lpf, mode='valid')

    # 6. Decimate by DECIM (stride view, no copy).
    I = I_full[::DECIM]
    Q = Q_full[::DECIM]
    n_out = len(I)

    env = env_buf[:n_out]
    sq_q = sq_tmp_buf[:n_out]
    out  = out_buf[:n_out]

    # 7. Envelope = sqrt(I² + Q²), in-place via outs
    np.multiply(I, I, out=env)         # env = I²
    np.multiply(Q, Q, out=sq_q)        # sq_q = Q²
    np.add(env, sq_q, out=env)         # env = I² + Q²
    np.sqrt(env, out=env)              # env = sqrt(...)

    # 8. Track peak/floor (long-window EMA)
    local_peak  = float(np.percentile(env, 99.5))
    local_floor = float(np.percentile(env,  0.5))
    if peak_est is None:
        peak_est, floor_est = local_peak, local_floor
    else:
        peak_est  += PEAK_FLOOR_ALPHA * (local_peak  - peak_est)
        floor_est += PEAK_FLOOR_ALPHA * (local_floor - floor_est)
    span = max(peak_est - floor_est, 1.0)

    # 9. Invert (PAL negative modulation), scale to int16, in-place
    np.subtract(peak_est, env, out=env)          # env = peak - env
    np.multiply(env, 48000.0 / span, out=env)
    np.subtract(env, 24000.0, out=env)
    np.clip(env, -32760, 32760, out=env)
    np.copyto(out, env, casting='unsafe')

    return memoryview(out).cast('B')


# Process the carrier-detect buffer first
t0 = time.time()
sys.stdout.buffer.write(process_chunk(detect_samples))

chunk_count = 1
while True:
    buf = sys.stdin.buffer.read(CHUNK * 2)
    if not buf:
        break
    samples = np.frombuffer(buf, dtype='<i2')
    sys.stdout.buffer.write(process_chunk(samples))
    chunk_count += 1

elapsed = time.time() - t0
in_seconds = sample_index_total / FS_IN
print(f"[stream] processed {sample_index_total} samples = {in_seconds:.2f}s "
      f"in {elapsed:.2f}s ({in_seconds/elapsed:.2f}× realtime, {chunk_count} chunks)",
      file=sys.stderr)
