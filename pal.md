# Capturing PAL TV with the RX888 mk2

How to capture analog PAL through the RX888 mk2's VHF/UHF port on
Linux, and decode it end-to-end to a still video frame. Tested against
a UK PAL Sega Master System II RF modulator on UHF Channel 36 (vision
carrier 591.2 MHz, line rate 15.556 kHz), but the recipe should work
for any PAL B/G/I source.

## TL;DR

- **Hardware:** RX888 mk2 → USB; PAL RF source → coax-to-SMA → VHF/UHF
  SMA on the RX888.
- **Software:** `rx888_stream` *with the fix from
  [PR #16](https://github.com/rhgndf/rx888_stream/pull/16) — if running
  `rhgndf/rx888_stream` main without that PR, VHF capture will appear
  to work (bytes flow, sample rate matches) but RF won't actually
  reach decodably*. The bug is a one-line fix; if you've built this
  branch you're good.
- **Firmware:** `SDDC_FX3_v22.img` built from
  `github.com/fventuri/SDDC_FX3`, or the existing `SDDC_FX3.img` in
  this tree — both work identically for capture.
- **Capture:** `rx888_stream vhf -f SDDC_FX3_v22.img -s 24000000
  --frequency 591200000 --vhf-lna 14 --vhf-vga 8 --gain 1
  -o cap.bin`
- **Decode:** `tools/demod_real.py cap.bin cvbs.s16 --fs 24e6` then
  `tools/cvbs_to_image_v5.py cvbs.s16 frame.pgm 24e6 22 15556`
- **Quality:** ~80 dB vision-carrier SNR, decodable monochrome
  picture. Same as Windows + SDR Console on the same hardware.

## Full recipe (Linux, from a fresh machine)

### 1. System packages

```bash
sudo apt update
sudo apt install -y libusb-1.0-0-dev pkg-config gcc-arm-none-eabi \
                    python3-pyqtgraph python3-numpy python3-scipy \
                    python3-pil
```

`gcc-arm-none-eabi` is only needed if you want to build the firmware
from source; if you already have an `SDDC_FX3.img` you trust, skip it.

### 2. udev rules — sudoless USB access

Create `/etc/udev/rules.d/52-rx888.rules`:

```
# RX888 / Cypress FX3 - DFU bootloader mode
SUBSYSTEM=="usb", ATTRS{idVendor}=="04b4", ATTRS{idProduct}=="00f3", MODE="0666", GROUP="plugdev"
# RX888 / Cypress FX3 - post-firmware
SUBSYSTEM=="usb", ATTRS{idVendor}=="04b4", ATTRS{idProduct}=="00f1", MODE="0666", GROUP="plugdev"
```

Reload and confirm your user is in `plugdev`:

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
groups | tr ' ' '\n' | grep plugdev || (sudo usermod -aG plugdev $USER; echo "log out / back in for group to apply")
```

### 3. Build `rx888_stream` (with the fix)

```bash
git clone https://github.com/rhgndf/rx888_stream
cd rx888_stream

# Apply PR #16 if not yet merged into rhgndf/rx888_stream main — VHF capture is broken
# without it. Either check out the branch or cherry-pick:
git fetch https://github.com/mattgodbolt/rx888_stream fix/tunerinit-r828d-reference-clock
git cherry-pick FETCH_HEAD

cargo build --release
```

### 4. Build the firmware (optional)

The `SDDC_FX3.img` in this tree works. If you'd rather build the v2.2
firmware that matches what most Windows users run, this is the recipe:

```bash
git clone https://github.com/ik1xpv/ExtIO_sddc /tmp/ExtIO_sddc
git clone https://github.com/fventuri/SDDC_FX3   /tmp/SDDC_FX3
ln -sf /tmp/ExtIO_sddc/SDK /tmp/SDDC_FX3/SDK
cd /tmp/SDDC_FX3
FX3FWROOT=/tmp/ExtIO_sddc/SDK ARMGCC_INSTALL_PATH=/usr CYFXBUILD=gcc make
cp SDDC_FX3.img ~/dev/rx888_stream/SDDC_FX3_v22.img
```

The Cypress FX3 SDK is included in the ik1xpv repo (`SDK/`), so
gcc-arm-none-eabi is the only extra cross-compile dependency needed.

### 5. Hardware setup

- Plug the RX888 mk2 into a USB port. USB 2.0 high-speed or USB 3.0
  SuperSpeed both work; the firmware streams cleanly at either link
  speed. Check enumeration with `lsusb -t`:

  - `5000M` or `10000M` for the RX888 = SuperSpeed (best).
  - `480M` = USB 2.0 high-speed (works fine for capture).
  - `12M` = USB 1.1 — would be too slow; investigate the cable/port
    if you see this.

- PAL source coax → coax-to-SMA adapter → **VHF/UHF SMA** on the
  RX888 (not the HF one).

- Power the RF source from its proper PSU. (A previous all-day debug
  was a 6 V PSU on a 9 V modulator producing clean CW with no
  modulation — saved as a memory.)

### 6. Capture

```bash
./target/release/rx888_stream vhf \
    -f ./SDDC_FX3_v22.img \
    -s 24000000 \
    --frequency 591200000 \
    --vhf-lna 14 --vhf-vga 8 --gain 1 \
    -o /tmp/cap.bin
```

Run it for a few seconds, then Ctrl-C. `cap.bin` is raw int16 LE,
~48 MB/s. 5 seconds is plenty for several full PAL frames.

Notes on the parameters:

- `--frequency 591200000` — UK PAL Ch 36 vision carrier nominal is
  591.25 MHz, but consumer modulators run slightly slow; the SMS
  observed at 591.2 MHz. Tune to wherever your vision carrier actually
  is.
- `--vhf-lna 14 --vhf-vga 8 --gain 1` — gain combo that delivers good
  signal level without ADC clipping. With `--vhf-lna 28 --gain 8` the
  ADC clips at ~94% on a strong source; backing off avoids that.
- `-s 24000000` — 24 MSps gives 12 MHz Nyquist, comfortably above the
  R828D's 4.57 MHz IF + 4.43 MHz chroma + 6 MHz audio sub. 50 MSps
  works too if your USB link can sustain it.

If you've got the GUI scope, you can verify the signal live before
committing to a file:

```bash
./target/release/rx888_stream vhf -f ./SDDC_FX3_v22.img \
    -s 24000000 --frequency 591200000 \
    --vhf-lna 14 --vhf-vga 8 --gain 1 -o - \
  | python3 tools/live_scope.py --source stdin --fs 24e6
```

You should see a tall narrow spike around 4.5 MHz (the vision
carrier in IF), with a comb of line-rate sidebands at ±15.556 kHz and
multiples thereof. `live_scope.py` needs a graphical session — no GUI
over plain SSH.

### 7. Quick sanity check on the capture

```bash
python3 tools/analyze_rx888.py /tmp/cap.bin 24e6 | head -10
```

Expected: a narrow peak around 4.5 MHz, 50+ dB above the local floor.
If the top peak is somewhere else (say 2-3 MHz) and < 30 dB excess,
the R828D PLL is uncalibrated — you're running `rx888_stream` from
`rhgndf/rx888_stream` main without PR #16 applied.

### 8. Demodulate to baseband CVBS

```bash
python3 tools/demod_real.py /tmp/cap.bin /tmp/cvbs.s16 --fs 24e6
```

This:

- finds the vision-carrier IF peak (default search 3–6 MHz),
- downconverts to bring it to 0 Hz,
- LPFs at 3 MHz (luma only — drops PAL chroma at +4.43 MHz which
  would otherwise show as herringbone interference in the picture),
- envelope-detects and inverts for PAL B/G/I negative modulation,
- writes int16 LE baseband CVBS at the same 24 MSps.

If you want to keep the chroma (e.g. for colour decoding), pass
`--lpf 5.5e6`. If you know the carrier exactly, `--carrier 4.57e6`
skips the auto-detect.

### 9. Render a frame

```bash
python3 tools/cvbs_to_image_v5.py /tmp/cvbs.s16 /tmp/frame.pgm 24e6 22 15556
```

Arguments: source CVBS, dest PGM, sample rate, **field number**, and
optional **line rate Hz**.

- The **field number** picks which detected V-sync to anchor on.
  V-sync detection sometimes lands mid-screen, producing a wrapped
  image. If your first attempt looks wrapped (e.g. status bar at the
  middle, picture continuing at the top), try a few different field
  numbers — there's usually a clean one within the first 30 or so.
- The **line rate Hz** override is useful for consumer modulators that
  run slightly off-nominal. Without it the script uses the median
  detected H-sync gap, which works for stock 15.625 kHz PAL but
  introduces a few-samples-per-line drift on the SMS (15.556 kHz
  actual). Pass the measured value from `demod_real.py`'s
  envelope-modulation analysis, or 15625 for nominal PAL.

To pick the actual line rate from your capture programmatically:

```bash
python3 -c "
import numpy as np
fs = 24e6
x = np.fromfile('/tmp/cvbs.s16', dtype='<i2').astype(np.float32)
N = min(int(2*fs), len(x))
e = x[:N] - x[:N].mean()
E = np.abs(np.fft.rfft(e))
f = np.fft.rfftfreq(N, 1.0/fs)
m = (f > 14e3) & (f < 17e3)
ipk = np.argmax(E[m])
print(f'measured line rate: {f[m][ipk]:.1f} Hz')
"
```

Convert PGM to PNG for easier viewing:

```bash
python3 -c "from PIL import Image; Image.open('/tmp/frame.pgm').save('/tmp/frame.png')"
```

A successful render is a recognisable monochrome video frame
(~720×574). Expect some imperfections: the v5 renderer is
single-field doubled vertically, so resolution is half what a full
interlaced render would give, and field-start detection occasionally
mislands. Both are documented limitations of the v5 decoder — see
**Open follow-ups**.

## SMS modulator clock drift

The SMS modulator's clocks run slightly slow vs nominal PAL:

- Vision carrier: 591.200 MHz observed vs 591.250 MHz nominal — about
  −50 kHz, or −0.0085% offset.
- Line rate: 15.556 kHz observed vs 15.625 kHz nominal — about
  −0.44% offset.

These two offsets are *different*, so the SMS uses (at least) two
different reference oscillators. The line-rate offset is significant
enough that the v5 decoder's median-of-H-sync auto-detection is off
by a few samples per line, causing a noticeable horizontal shear if
not overridden. Hence the `15556` line-rate-Hz argument in step 9
above.

## What was broken (history)

A precis of the dead ends, so future-us doesn't re-walk them.

### Day 1 (laptop): "the firmware doesn't pass RF to the mixer"

Earlier sessions concluded — based on captures showing a flat ~+3.5 dB
"noise pedestal" across the IF passband and no detectable line-rate
modulation — that the SDDC firmware was somehow commanding the R828D
to power up its IF chain but not actually passing RF through to the
mixer. A previous build with `TUNER_ANALOG_TV` mode and host-side
`R82XX_I2C_WRITE` register pokes (in `SDDC_FX3_analog.img`) produced
near-identical results, leading to "the RX888 mk2's analog noise
floor is too high to recover the picture".

Wrong conclusion. The "noise pedestal" *was* the IF chain running on
receiver-internal noise, but the reason no real signal sat above it
wasn't the noise floor — it was that the R828D's LO was free-running
because `rx888_stream` was initialising the tuner without a
reference-clock parameter.

### Day 2 morning: "it was USB 1.1 stale buffers"

On the previous Linux laptop the RX888 had enumerated at USB 1.1
(12 Mbps) due to a flaky USB port. The streamer sent commands fine,
but bulk-IN data at 12 Mbps couldn't keep up with the 800 Mbps real
sample rate, so the endpoint served stale FX3 internal-buffer
contents — which looked plausibly noise-like. The Day-1-to-Day-2
handoff hypothesised that the stale-buffer effect was the entire
reason for the previous-day "no signal" conclusion.

Also wrong. On the new desktop the device enumerated at USB 2.0
high-speed (later 3.0 SuperSpeed), bulk transfers were healthy, and
we *still* couldn't see the signal.

### Day 2 afternoon: "the firmware version must matter"

Spent considerable time trying four firmware variants — the existing
`SDDC_FX3.img` in this tree (sha256 `824c575f…`), a fresh build from
`ik1xpv/ExtIO_sddc` master (post-PR #225 Intel-hub LPM fix), the
official v1.3.0RC1 release blob (sha256 `c1adb233…`, which the
streamer can't drive at all — `Transfer failed: PollTimeout`), and
the v2.2 build from `fventuri/SDDC_FX3` matching what the Windows
side reports. **All four functionally-working firmwares produced
identical reception spectra**, with the same std within 1.5%. The
firmware was not the variable.

A consulting LLM (asked in parallel by Matt) suggested the difference
might be AGC handling, IF filter shape, or an off-by-one LNA index.
None of those held up either.

### Day 2 evening: the actual bug

Reading the Windows host-side C++ in `ik1xpv/ExtIO_sddc`,
`ExtIO_sddc/Core/radio/RX888R2Radio.cpp`, the VHF init sequence is:

```cpp
// Core/radio/RX888R2Radio.cpp:UpdatemodeRF(VHFMODE)
UpdateattRF(0);                       // mute HF chain
FX3SetGPIO(VHF_EN);                   // switch antenna mux to VHF
Fx3->SetArgument(AD8340_VGA, 0x83);   // pre-set VGA
uint32_t ref = R828D_FREQ;            // 16 MHz
Fx3->Control(TUNERINIT, ref);         // init tuner WITH ref clock
```

vs `rx888_stream/src/main.rs`:

```rust
rx888_send_command(&handle, FX3Command::TUNERINIT, 0)
```

The firmware's `TUNERINIT` handler reads its parameter and passes it
straight to `r820_initialize(freq)` as the R828D's reference clock.
With `0`, the R828D's PLL initialised without a stable reference and
produced unpredictable LO frequencies — we observed the carrier
landing at 2.07, 2.82, 3.00, 3.64, 6.25 MHz on different captures of
the same RF source. With `16_000_000` it locks to the R828D's designed
IF center of 4.57 MHz, reproducibly, every time.

Reproduction signature: take 3 back-to-back captures with the same
`--frequency` and search for the strongest narrow peak in the 1–11
MHz IF range. Pre-fix: peak lands at a different frequency each run.
Post-fix: same frequency every run, within a fraction of a Hz.

Filed as [issue #15](https://github.com/rhgndf/rx888_stream/issues/15)
and [PR #16](https://github.com/rhgndf/rx888_stream/pull/16).

### Discipline note: false-positive signal claims

During the debugging, I (the helping LLM) claimed "we have signal" at
least three times based on spectral features that turned out to be
random receiver-internal structures or RFI coupling. The lesson saved
to memory: never declare an SDR signal received from one spectrum
alone — always run three controls.

1. **Source-off**: with the RF source physically off, does the
   candidate peak disappear?
2. **Tune-off**: when `--frequency` changes by ΔF, does the IF peak
   move by ΔF? If it sits at a fixed IF regardless of tune, it's
   spurious.
3. **Reproduction**: capture again. Real RF gives the same peak
   position (within calibration drift); noise produces a different
   "winner" each run.

The final PAL result passes all three with margin: 221× std ratio on
SMS-off, line-rate comb that disappears entirely without the source,
identical carrier position across reproductions.

## What does and doesn't work

| Path | Status |
|------|--------|
| Windows + ExtIO_sddc + VHF + SMS at 591.2 MHz | ✅ ~70 dB SNR, full PAL visible |
| Linux + `rx888_stream` *with [PR #16](https://github.com/rhgndf/rx888_stream/pull/16)* + SDDC_FX3_v22.img + SMS at 591.2 MHz | ✅ ~80 dB SNR, full PAL + line-rate comb, decodes |
| Linux + `rx888_stream` *`rhgndf` main, no PR #16* + any firmware + SMS at 591.2 MHz | ❌ wandering weak carrier, no decodable picture |
| Linux + `rx888_stream` + HF + AM broadcast | ✅ works with a wire antenna |
| Linux + `rx888_stream` + official v1.3.0RC1 firmware blob | ❌ `Transfer failed: PollTimeout` — pre-#225 firmware speaks a different USB protocol |

## File map

- `tools/analyze_rx888.py` — quick Welch PSD on a raw real-int16
  capture; useful to spot the vision carrier and check SNR.
- `tools/demod_real.py` — raw int16 → CVBS for `rx888_stream` output
  (this script).
- `tools/demod_iq_wav.py` — same idea but for an SDR Console IQ WAV
  input (carrier already centered at 0 Hz).
- `tools/cvbs_to_image_v5.py` — baseband CVBS → PGM still. Single
  field, doubled vertically, back-porch DC reference. Optional
  line-rate-Hz override (5th positional arg).
- `tools/cvbs_to_image{.py,_simple.py,_v3.py,_v4.py}` — older
  variants kept for reference.
- `tools/live_scope.py` — live multi-pane scope (RF spectrum +
  waterfall + envelope mod). GUI only.
- `tools/inspect_demod.py` / `tools/demod_pal.py` / `tools/synth_pal.py`
  — ad-hoc experiments from earlier sessions; safe to ignore unless
  you're debugging the demod chain.
- `SDDC_FX3.img` — original firmware in this tree, provenance unknown
  but functionally equivalent for our purposes.
- `SDDC_FX3_analog.img` — laptop's `TUNER_ANALOG_TV` build
  (functionally identical to stock).
- `SDDC_FX3_rebuild.img` — fresh build from ik1xpv master + the
  laptop patches (R82XX_I2C_WRITE, TUNER_ANALOG_TV, GAIN_STEPS bounds
  check). Functionally identical to stock for our purposes.
- `SDDC_FX3_v22.img` — built from `fventuri/SDDC_FX3`. This is the
  firmware Windows uses. Recommended as the default.

## What we already tried that didn't help

(To save next-Matt / next-Claude time.)

- Multiple firmware builds — all functionally identical for reception.
- LNA / VGA / gain sweeps — only matter for ADC headroom once the LO
  is correct; don't help without the TUNERINIT fix.
- TUNER_ANALOG_TV vs TUNER_DIGITAL_TV switch — near-no-op because AGC
  is force-disabled when LNA gain is set explicitly.
- R828D register pokes via `--r82xx-write` (PRE_DECT, LNA TOP,
  bw=6 MHz) — no setting unlocks reception without the LO fix.
- Moving the LO so the vision carrier lands at IF center via a
  `--frequency` offset — irrelevant once the LO is properly
  referenced; the firmware does the IF offset internally.
- 10 dB SMA attenuator pad — irrelevant.
- Different physical USB ports — irrelevant; USB 2 high-speed and
  USB 3 SuperSpeed both work fine.

## Open follow-ups

- **[Issue #15](https://github.com/rhgndf/rx888_stream/issues/15) /
  [PR #16](https://github.com/rhgndf/rx888_stream/pull/16)** — the
  TUNERINIT reference-clock fix, filed against `rhgndf/rx888_stream`.
- **[Issue #17](https://github.com/rhgndf/rx888_stream/issues/17) /
  [PR #18](https://github.com/rhgndf/rx888_stream/pull/18)** —
  separate off-by-one in `--vhf-lna` clap range (0..=29 → 0..=28).
- **v5 renderer vertical-wrap**: V-sync detection occasionally lands
  mid-screen, requiring the user to try a different field number.
  Could be fixed by detecting the field-start more robustly (e.g.
  looking for the equalising-pulse pattern rather than just runs of
  half-line gaps).
- **Interlaced rendering**: v5 is single-field doubled vertically.
  Earlier variants (`_v3.py`, `_v4.py`) tried full interlace but
  produced doubled-text artifacts from per-field horizontal jitter.
  Worth another go with the now-stable LO.
- **Post-capture FX3 stuck state**: occasionally after a SIGTERM
  exit, the next invocation hits `STARTFX3: Timeout` and
  `RESETFX3: Io`. Recoverable via sysfs (`echo 0 | sudo tee
  /sys/bus/usb/devices/2-1/authorized; sleep 1; echo 1 | sudo tee
  /sys/bus/usb/devices/2-1/authorized`) or physical replug. Probably
  a missing `STOPFX3` on signal-driven exit — worth tracking down.
