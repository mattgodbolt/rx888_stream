#!/usr/bin/env python3
"""Live multi-pane scope for RX888 / CXADC / any real-int16 or int8 stream.

Four panes:
  1. Raw scope (time domain, decimated)
  2. RF spectrum + waterfall (FFT of recent chunk)
  3. AM-envelope at chosen IF centre (click the spectrum to set it)
  4. Envelope-modulation spectrum (1 Hz–250 kHz) — the "is this video?" pane

Sources:
  --source file:/path/to.bin
  --source stdin                (e.g.  rx888_stream ... -o - | live_scope.py --source stdin)
  --source /dev/cxadc0

Sample format:
  --dtype  i2  (default, RX888 real int16 LE)
  --dtype  u1  (CXADC unsigned 8-bit)
  --dtype  i1  (signed 8-bit)
"""
import sys, os, signal, argparse, threading, time
import numpy as np
from collections import deque

import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

# ---- args ----------------------------------------------------------------
ap = argparse.ArgumentParser()
ap.add_argument("--source", required=True,
                help="file:/path, stdin, or a device path like /dev/cxadc0")
ap.add_argument("--fs", type=float, default=50e6, help="sample rate (Hz)")
ap.add_argument("--dtype", default="i2", choices=["i2", "u1", "i1"])
ap.add_argument("--chunk", type=int, default=1 << 16,
                help="samples per GUI frame (FFT size)")
ap.add_argument("--fps", type=float, default=20.0)
ap.add_argument("--waterfall-rows", type=int, default=200)
ap.add_argument("--scope-decim", type=int, default=64,
                help="how many samples per displayed scope pixel")
args = ap.parse_args()

DTYPE_MAP = {
    "i2": (np.int16,  2, lambda x: x.astype(np.float32)),
    "u1": (np.uint8,  1, lambda x: x.astype(np.float32) - 128.0),
    "i1": (np.int8,   1, lambda x: x.astype(np.float32)),
}
np_dtype, bytes_per_sample, to_f32 = DTYPE_MAP[args.dtype]

# ---- source reader (background thread, ring buffer) ----------------------
RING_SIZE = args.chunk * 16
ring = np.zeros(RING_SIZE, dtype=np.float32)
ring_lock = threading.Lock()
ring_write_idx = 0
samples_seen = 0
stop_flag = threading.Event()

def open_source():
    if args.source == "stdin":
        return sys.stdin.buffer
    if args.source.startswith("file:"):
        return open(args.source[5:], "rb")
    return open(args.source, "rb")

def reader_thread():
    global ring_write_idx, samples_seen
    fh = open_source()
    read_bytes = args.chunk * bytes_per_sample
    while not stop_flag.is_set():
        buf = fh.read(read_bytes)
        if not buf:
            # File EOF — loop the file for steady visualization.
            if args.source.startswith("file:"):
                fh.seek(0)
                continue
            time.sleep(0.01)
            continue
        n = len(buf) // bytes_per_sample
        x = np.frombuffer(buf, dtype=np_dtype, count=n)
        f32 = to_f32(x)
        with ring_lock:
            end = ring_write_idx + n
            if end <= RING_SIZE:
                ring[ring_write_idx:end] = f32
            else:
                first = RING_SIZE - ring_write_idx
                ring[ring_write_idx:] = f32[:first]
                ring[:n - first] = f32[first:]
            ring_write_idx = end % RING_SIZE
            samples_seen += n

th = threading.Thread(target=reader_thread, daemon=True)
th.start()

def latest_chunk(n):
    """Return the most recent n samples from the ring (copy)."""
    with ring_lock:
        end = ring_write_idx
        start = (end - n) % RING_SIZE
        if start < end:
            return ring[start:end].copy()
        return np.concatenate([ring[start:], ring[:end]])

# ---- GUI -----------------------------------------------------------------
pg.setConfigOptions(antialias=False, useOpenGL=False)
app = QtWidgets.QApplication(sys.argv)
win = pg.GraphicsLayoutWidget(show=True,
                              title=f"live_scope — {args.source} @ {args.fs/1e6} Msps")
win.resize(1400, 900)

# Pane 1: time domain
p_scope = win.addPlot(row=0, col=0, title="Raw scope (time domain)")
p_scope.setLabel("bottom", "Sample (decimated)")
p_scope.setLabel("left", "Amplitude")
curve_scope = p_scope.plot(pen="y")
clip_marker = p_scope.addLine(y=0, pen=None)  # placeholder

# Pane 2: RF spectrum + waterfall
p_spec = win.addPlot(row=0, col=1, title="RF spectrum (FFT)")
p_spec.setLabel("bottom", "Frequency (MHz)")
p_spec.setLabel("left", "Power (dB)")
curve_spec = p_spec.plot(pen="c")
demod_line = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("r", width=2))
p_spec.addItem(demod_line)
demod_freq_hz = 2.5e6  # initial IF guess
demod_line.setPos(demod_freq_hz / 1e6)

def on_demod_moved():
    global demod_freq_hz
    demod_freq_hz = float(demod_line.value()) * 1e6
demod_line.sigPositionChanged.connect(on_demod_moved)

p_wf = win.addPlot(row=1, col=1, title="Waterfall")
p_wf.setLabel("bottom", "Frequency (MHz)")
p_wf.setLabel("left", "Time (rows, newest at bottom)")
img_wf = pg.ImageItem()
p_wf.addItem(img_wf)
wf_buffer = None  # initialised on first FFT (depends on chunk size)

# Pane 3: envelope time series
p_env = win.addPlot(row=1, col=0, title="AM envelope (click red line to set IF centre)")
p_env.setLabel("bottom", "Time (µs)")
p_env.setLabel("left", "Envelope")
curve_env = p_env.plot(pen="g")

# Pane 4: envelope-modulation spectrum
p_envspec = win.addPlot(row=2, col=0, colspan=2,
                        title="Envelope-modulation spectrum (PAL line rate = 15.625 kHz)")
p_envspec.setLabel("bottom", "Modulation freq (kHz)")
p_envspec.setLabel("left", "Power (dB)")
p_envspec.setLogMode(x=False, y=False)
curve_envspec = p_envspec.plot(pen="m")
# Vertical markers at PAL/NTSC line rates
for f_khz, name in [(15.625, "PAL H"), (15.734, "NTSC H"), (31.25, "PAL 2H")]:
    ln = pg.InfiniteLine(pos=f_khz, angle=90,
                         pen=pg.mkPen((180, 180, 180), style=QtCore.Qt.PenStyle.DashLine),
                         label=name, labelOpts={"position": 0.9, "color": "w"})
    p_envspec.addItem(ln)

# Status label
status = QtWidgets.QLabel("starting…")
proxy = QtWidgets.QGraphicsProxyWidget()
proxy.setWidget(status)
win.addItem(proxy, row=3, col=0, colspan=2)

# ---- update loop ---------------------------------------------------------
last_t = time.time()
last_seen = 0
spec_window = np.hanning(args.chunk).astype(np.float32)

def update():
    global wf_buffer, last_t, last_seen
    chunk = latest_chunk(args.chunk)
    if chunk.size < args.chunk:
        return

    # Scope (decimated): min/max of bins to preserve sync edges visibly
    decim = args.scope_decim
    nbins = args.chunk // decim
    reshaped = chunk[:nbins * decim].reshape(nbins, decim)
    # Show mean as the trace, but the user gets to see clipping via the y-range
    trace = reshaped.mean(axis=1)
    curve_scope.setData(trace)

    # RF spectrum
    fft = np.fft.rfft(chunk * spec_window)
    psd = 20 * np.log10(np.abs(fft) + 1e-9)
    freqs_mhz = np.fft.rfftfreq(args.chunk, 1.0 / args.fs) / 1e6
    curve_spec.setData(freqs_mhz, psd)

    # Waterfall: seed with the FIRST frame's PSD so autoscale has a sensible
    # range immediately. Using psd.min() seeded the whole buffer with one
    # value, which collapsed the percentile autoscale to a single level and
    # painted the entire image white until the buffer rolled in real data.
    if wf_buffer is None or wf_buffer.shape[1] != psd.size:
        wf_buffer = np.tile(psd.astype(np.float32),
                            (args.waterfall_rows, 1))
    wf_buffer = np.roll(wf_buffer, -1, axis=0)
    wf_buffer[-1] = psd
    # Drive levels from the LATEST PSD's percentiles, not the whole buffer's.
    # The buffer is mostly stale identical rows on startup; using its
    # percentiles collapses to a tiny range and the image clips to white.
    img_wf.setImage(wf_buffer.T, autoLevels=False,
                    levels=(np.percentile(psd, 5),
                            np.percentile(psd, 99.5)))
    img_wf.setRect(pg.QtCore.QRectF(0, 0,
                                    freqs_mhz[-1], args.waterfall_rows))

    # AM envelope at demod_freq_hz: shift to BB, LPF, |.|
    n = chunk.size
    t = np.arange(n, dtype=np.float64)
    lo = np.exp(-2j * np.pi * demod_freq_hz / args.fs * t).astype(np.complex64)
    bb = chunk.astype(np.complex64) * lo
    # FFT-based LPF to 6 MHz
    B = np.fft.fft(bb)
    fb = np.fft.fftfreq(n, 1.0 / args.fs)
    B[np.abs(fb) > 6e6] = 0
    bb_lpf = np.fft.ifft(B)
    env = np.abs(bb_lpf).astype(np.float32)
    env_dc = env - env.mean()

    # Plot the first ~640 µs of envelope (10 PAL lines) for visual line-rate sanity
    show_n = min(env.size, int(640e-6 * args.fs))
    t_us = np.arange(show_n) / args.fs * 1e6
    curve_env.setData(t_us, env[:show_n])

    # Envelope-modulation spectrum (1 Hz .. 250 kHz)
    E = np.abs(np.fft.rfft(env_dc * spec_window))
    ef = np.fft.rfftfreq(n, 1.0 / args.fs)
    mask = (ef >= 100) & (ef <= 250e3)
    env_db = 20 * np.log10(E[mask] + 1e-9)
    curve_envspec.setData(ef[mask] / 1e3, env_db - env_db.max())

    # Status
    now = time.time()
    with ring_lock:
        rate = (samples_seen - last_seen) / (now - last_t) if now > last_t else 0
        last_seen = samples_seen
    last_t = now
    # Probe specific line-rate amplitude
    if mask.any():
        target = 15.625e3
        i = np.argmin(np.abs(ef[mask] - target))
        line_db = env_db[i] - env_db.max()
    else:
        line_db = float('nan')
    status.setText(
        f"sample rate (in): {rate/1e6:7.2f} Msps   "
        f"chunk: {args.chunk}   "
        f"raw std: {chunk.std():7.1f}   raw range: [{chunk.min():.0f}, {chunk.max():.0f}]   "
        f"demod IF: {demod_freq_hz/1e6:.3f} MHz   "
        f"15.625 kHz: {line_db:+.1f} dB"
    )

timer = QtCore.QTimer()
timer.timeout.connect(update)
timer.start(int(1000 / args.fps))

# Click-on-spectrum to retune demod line
def on_spec_click(evt):
    pos = evt.scenePos()
    if p_spec.sceneBoundingRect().contains(pos):
        mp = p_spec.vb.mapSceneToView(pos)
        demod_line.setPos(mp.x())
p_spec.scene().sigMouseClicked.connect(on_spec_click)

# Ctrl-C handling: when the GUI is running, Qt's C event loop swallows
# SIGINT and the reader thread is stuck in a blocking read on stdin, so
# the process hangs (only ^] = SIGQUIT killed it). Fix:
#   1. Restore default SIGINT so Python actually interprets ^C
#   2. Install a Python handler that quits the Qt app cleanly
#   3. Run a no-op timer so Python gets CPU time to deliver the signal
def _handle_sigint(*_):
    stop_flag.set()
    app.quit()
signal.signal(signal.SIGINT, _handle_sigint)
sigtimer = QtCore.QTimer()
sigtimer.timeout.connect(lambda: None)
sigtimer.start(200)

app.exec()
stop_flag.set()
# Reader thread is daemon=True so it dies with the process; no join needed.
