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
- **Demod:** `tools/demod_real.py cap.bin cvbs.s16 --fs 24e6` (or
  `--lpf 5.5e6` to preserve chroma for colour decoding)
- **Decode (monochrome):** `tools/cvbs_to_image_v6.py cvbs.s16
  frame.png 24e6 0` — clean, TV-style sync separator, full-width
  output. Produces a recognisable Wonder Boy III gameplay frame.
- **Decode (colour):** `tools/cvbs_to_image_v8.py cvbs.s16 frame.png
  24e6 0` — full PAL chroma demod, 1980s-architecture. Uses a fixed
  textbook fSC, class-aware per-line burst rotation, and auto-detects
  which line parity is V-inverted. Tested against `hacktv` synthetic
  colour bars (correct primaries) and the SMS Wonder Boy III capture.
- **Streaming demod:** `cat cap.bin | python3 tools/demod_stream.py >
  cvbs.s16` — chunked architecture sketched out for a future C++
  port; runs at ~0.4× realtime in Python, output bit-equivalent.
- **Quality:** ~80 dB vision-carrier SNR, fully decodable picture
  (mono) and reasonable colour. Same SNR as Windows + SDR Console on
  the same hardware.

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

## Day 3: colour decoding

Goal: decode PAL chroma using only operations a 1980s analog colour TV
would do — sync separator, burst-locked subcarrier oscillator, Y/C
separation by frequency, synchronous demodulation, vertical comb
filter for the Phase Alternating Line trick, YUV → RGB matrix.

### Where we got to

`tools/cvbs_to_image_v7.py` exists and produces colour images. For the
SMS Wonder Boy III: The Dragon's Trap capture, it shows the right
*structure* (HUD, brick walls, character sprite, underwater background)
but the **colours are wrong globally** (hues rotated) and drift
**across each line** ("yellow bricks come out cyan on the left fading
to red on the right"). Symptom signature: a frequency error of ~3.8 kHz
between LO and chroma would explain the per-line drift, but per-burst
linear fit across the field constrains the residual to ~5 Hz. The
drift's root cause isn't yet identified.

### Architecture lessons from this attempt (1980s TV ⇒ digital)

- **Sync separator must lowpass chroma first.** v6/v7 originally
  thresholded the raw CVBS. For a solid-colour test signal that has a
  strong chroma carrier swinging around the threshold, the slicer fires
  on every chroma cycle → millions of spurious "pulses". A 1 MHz LPF
  before threshold makes sync detection robust.

- **Subcarrier "frequency" is a known constant, not a measured peak.**
  A 1980s TV has a 4.43361875 MHz crystal and a narrowband PLL that
  pulls the local oscillator's phase to track the burst. v7's initial
  approach of "find fSC from spectrum peak" worked for the SMS by
  coincidence; it failed on the BBC where I picked the wrong peak (see
  below). The right design is: nominal fSC, burst-locked PLL.

- **PAL alternation detection needs care.** Burst phase alternates
  ±135° in absolute terms per line, but the relationship to a local
  LO depends on fSC × line_period mod 1. For the SMS this came out to
  ~91° per line, so naïve `sign(sin(burst_phase))` misclassifies. The
  workable approach is strict alternation seeded from line 0, but
  even then we get Hanover-bar artefacts (alternating coloured
  stripes), suggesting the PAL switch is still being applied wrong on
  some lines.

- **Visual outputs aren't ground truth from my side.** When showing
  candidate decodes (rotation sweeps, alternation seeds) I claimed
  "this one looks right!" multiple times and was wrong every time.
  Saved as a memory: present candidates with neutral labels and ask
  rather than declaring.

### BBC Micro test source (Day 3 afternoon)

To get a controlled test signal, we connected a BBC Micro's RF output
and typed `MODE 2` + a VDU sequence to set the background to a single
colour (blue, then black, etc). This should give a uniform-colour
field that's trivially decodable. Several discoveries:

- **BBC modulator is not on a standard UK UHF channel.** It sits at
  roughly 588 MHz (between Ch 35 = 583.25 and Ch 36 = 591.25). Tuning
  the RX888 to 591.2 MHz (where the SMS is) lands the BBC vision
  carrier at 8.16 MHz IF — well past the R828D's SAW filter centre
  (4.57 MHz) and 4 MHz into the filter rolloff, with vision attenuated
  to where sync pulses don't clear the noise floor.

- **`demod_real.py`'s carrier auto-detect fails on solid-colour
  signals.** It picks the strongest narrow peak in 3–6 MHz IF range.
  For a busy programme (SMS gameplay), vision wins — broad sidebands
  concentrate energy. For a uniform-colour test pattern, **chroma is
  a CW tone with all energy in one bin, while vision is spread across
  its luma sidebands.** Auto-detect grabs chroma; demodulating
  against chroma gives garbage. Workaround: pass `--carrier`
  explicitly. Better fix: pick the peak with the strongest
  line-rate sidebands (vision-only feature).

- **Even after tuning correctly to the BBC, the captured sync
  pulse-to-black depth is ~⅓ what the SMS gives** (sync_tip –
  black ≈ 4000 vs 14000 in int16 terms). This is consistent with
  Matt's observation that the BBC's picture is wobbly and that he
  has to nudge the TV's tuning to lock it. The signal is intrinsically
  worse than the SMS; our decoder's sync detection isn't robust
  enough yet for this regime. A proper TV's sync separator uses
  hysteresis (Schmitt trigger) and a narrowband PLL on H-sync to lock
  even when individual sync pulses are weak — we don't.

- **Mono decode of the BBC produces the right *picture content*** (a
  uniform grey field matching blue's luma, with the BASIC prompt
  visible in the corner and a black border) but the sync-locked
  rendering is still flaky — V-sync detection often returns zero
  runs, requiring careful gain tuning and field selection.

### Open colour issues (where the spike stalled) — now resolved in v8

1. **Per-line hue drift on the SMS** — root cause unknown, ~5 Hz LO
   residual doesn't explain the magnitude.
2. **PAL alternation correction** — strict-alternation seed from
   line 0 still produces stripes; suggests the alternation detection
   needs to track per-line burst sign rather than assume it.
3. **`vhs-decode` / `cvbs-decode` as reference** — installed the
   AppImage but it bails immediately with 0-byte output on both s16
   and u8 versions of our CVBS, exactly matching what a previous
   session hit. Not pursued further.

### Day 3 evening: v8 — `hacktv` test source, what was wrong with v7

After hitting a wall trying to diagnose colour against unknown-state
captures (SMS, BBC), we switched to a known-good synthetic source:

```
sudo apt install hacktv
hacktv -m pal -s 24000000 -o file:/tmp/synth_pal_bars.s16 \
       -t int16 --filter test:colourbars
```

This emits baseband CVBS at 24 MSps with standard 75 % PAL colour bars
(grey/yellow/cyan/green/magenta/red/blue) plus a "HACKTV" overlay. It
feeds straight into the chroma decoder — no IF demod stage needed.

**v7 produced wildly wrong colours even on this clean signal** — a
"sea of green with a single purple stripe and blue either side" rather
than a colour-bar spectrum. That proved the bug was in the decoder
code, not in the capture chain.

A code review against the canonical PAL chroma decoder
(`simoninns/ld-decode-tools` `palcolour.cpp`, the William Andrew Steer
implementation) found **six bugs** in v7. In likelihood order:

1. **`fSC` auto-detect picked a chroma sideband, not the subcarrier.**
   v7 took the largest spectral peak in 4–5 MHz. On colour bars
   that's dominated by the wide-area chroma U,V components whose
   sidebands tens of kHz from `fSC` swamp the gated, ~10-cycle burst.
   v7 settled on 4.45706 MHz, off by ~23 kHz from textbook
   4.43361875 MHz. A 23 kHz error makes the global LO wind
   `23 000 × 64 µs × 360° ≈ 530°` per line — about 1¼ turns
   relative to the true subcarrier. Per-line θ correction only zeros
   the phase at the back-porch burst; the chroma vector at the right
   edge of each line has rotated >360° past the compensation. The 1H
   comb filter then averages this smeared mess across pairs of lines
   and lands almost everywhere near a single hue.

2. **Per-line rotation θ = φ + 135° was class-blind.** PAL burst sits
   at `-U+V` (135° in the modulated frame) on V-non-inverted lines and
   `-U-V` (-135°) on V-inverted lines. The correct LO-vs-subcarrier
   phase is `ψ = 135° - φ` for the first class and `ψ = -135° - φ`
   for the second. v7 used `θ = φ + 135°` for both, so half the lines
   were over-rotated by 270° (=–90°). Combined with the V-flip step,
   this scrambled U/V on half the lines.

3. **PAL-switch detection was a global parity guess seeded from line
   zero**, vulnerable to the slow burst-phase wander that bug (1)
   amplified. The canonical detection compares each line's burst
   vector to its same-class neighbours (a local dot-product test),
   which is robust to a wandering global reference.

4. **Two-fold "which class is V-inverted" ambiguity** wasn't resolved
   at all — v7 just assumed the parity it happened to seed.

5. Burst-phase averaging across class was computed but never used
   in the actual rotation.

6. U/V scaling was a magic `0.5 / burst_mag` with no calibration
   to the PAL spec.

### v8 fixes

`tools/cvbs_to_image_v8.py`. Concretely:

- **`fSC` is fixed at the textbook 4.43361875 MHz** (overridable on
  the CLI but no spectrum-peak heuristic). A 1980s TV uses a crystal,
  not a spectrum analyser.

- **Per-line ψ is class-aware**: `ψ = 135° - φ` on V-non-inverted
  lines, `ψ = -135° - φ` on V-inverted lines.

- **The two-fold V-inversion ambiguity is resolved automatically.**
  The TRUE class assignment makes `ψ_α` and `ψ_β` (averaged over
  even-index vs odd-index lines) come out essentially equal because
  the LO-vs-subcarrier phase is a single physical quantity per
  moment. The wrong assignment makes them 180° apart. v8 picks the
  hypothesis that minimises `|ψ_α − ψ_β|`.

- **Strict alternation** (every other line is V-inverted) is kept,
  but the "which parity is V-inverted" is now driven by data via the
  auto-disambiguation above.

- **V is flipped on V-inverted lines** to recover transmitted V from
  modulated V (the canonical "PAL switch").

- **U/V scaling** anchored to measured burst magnitude (burst nominal
  is 0.3 × white-black, so `uv_scale = 0.3 / burst_mag`).

On the `hacktv` colour-bar capture, v8 reports diagnostics like:

```
fSC = 4.433619 MHz (fixed, textbook)
α (even-index) burst phase: -89.96°
β (odd-index)  burst phase:   0.02°
hyp 1 (α=NTSC, β=PAL): |ψ_α - ψ_β| =   0.02°
hyp 2 (α=PAL, β=NTSC): |ψ_α - ψ_β| = 179.98°
→ α is NTSC-style (V not inverted)
```

i.e. the burst classes are cleanly 90° apart (as expected for PAL
with this fSC × line ratio), hypothesis 1 is consistent to
sub-degree, hypothesis 2 is the canonical 180° away. Pixel samples
from the output: white, yellow, cyan, green, magenta, red, blue
across the top — the textbook PAL colour bar set.

v8 also decodes the **Sega Master System Wonder Boy III gameplay**
capture with correct primaries (heart red, potion blue, tile yellow,
underwater background light blue). The BBC Micro capture still fails
at the sync-separator stage — that's a separate weak-signal problem,
not a chroma decode one.

## Gotchas for a C++ port

This section consolidates everything we tripped on, biased toward what
matters when re-implementing in C++. Each item is what the v8 decoder
ended up doing and why.

### Subcarrier oscillator

- **`fSC = 4.43361875 MHz` is a constant, not a measurement.** Do
  NOT find it from a spectrum peak in 4–5 MHz — on real content the
  largest peak in that band is *chroma sideband energy* (especially
  for solid-coloured regions or test patterns), not the gated 10-cycle
  burst. v7 picked 4.45706 MHz, off by ~23 kHz; consequence was the
  decoder failing catastrophically on all inputs. A real TV uses a
  4.43361875 MHz crystal; do the same.

- **The LO is continuous across the whole field.** Build `cos(2π fSC
  · t)` and `sin(2π fSC · t)` once, where `t` runs over the entire
  capture's sample timeline — *not* reset per line. Burst-locked PLL
  effects (slow drift between LO and true subcarrier) are handled at
  the per-line rotation stage, not by re-phasing the LO.

- **A 23 kHz fSC error winds the LO 530° per scanline.** Generally:
  drift × line_period × 360° per line. So even tiny `fSC` errors are
  catastrophic — a ~5 Hz residual is the most you can tolerate before
  per-line correction needs to compensate >1° wind across the line.

### Sync separator

- **Lowpass the CVBS *before* threshold slicing.** A solid-coloured
  test signal has a CW chroma component swinging through the sync
  threshold; slicing the raw CVBS gives millions of spurious "pulses"
  per second from chroma zero-crossings. Use a 1 MHz LPF (e.g. 33-tap
  FIR Hamming). v7 added this; v6 didn't, and v6 misbehaves on
  uniform-colour BBC captures.

- **Classify pulses by duration after slicing**, not by edge timing.
  H-sync ≈ 4.7 µs, broad pulse ≈ 27 µs (or appears as ≈ 59 µs after
  the LPF smooths over the half-line serrations). V-sync is detected
  as a *run* of ≥3 broad-pulse pulses within 1.5 line periods.

- **Percentile-based slicer threshold works.** `sync_tip =
  P(0.5%)`, `black = P(30%)`, `thr = (sync_tip + black)/2`.
  Assumes some sync content in the buffer — fine for a real-time
  decoder that processes ≥1 frame.

- **Weak-signal sync is fragile.** A proper TV uses a Schmitt-trigger
  slicer with hysteresis plus a narrowband PLL on H-sync to lock
  through individual missing or weak pulses. We don't — and that's
  why our BBC capture (sync depth ~3 % of full scale, vs ~15 % for
  SMS) fails at sync detection. Worth implementing properly in C++.

### Burst detection and per-line rotation

- **Burst sits in the back porch**, ~5.4–7.9 µs after the H-sync
  edge. About 10 cycles of `fSC`. Measure its (cos, sin) projections
  against the global LO (i.e. integrate `raw_U_full` and `raw_V_full`
  over the burst window).

- **Burst phase alternates by ±90° in the local LO frame**, not
  ±180°. The transmitted burst alternates between `-U+V` (135° in the
  modulated frame, "NTSC-style line") and `-U-V` (-135°,
  "PAL-style line"). After mixing with our LO at unknown phase ψ,
  the burst phases are:
    - NTSC line: `φ = 135° − ψ`
    - PAL line:  `φ = −135° − ψ`
  Difference: `Δφ = 270°` modulo 360° = ±90° depending on convention.
  This explains the diagnostic `delta: ~90°` (or `-90°`) you'll see
  between the two line classes.

- **Per-line rotation must be class-aware.** Recovery formula:
    - NTSC line: `ψ = 135° − φ`
    - PAL line:  `ψ = −135° − φ`
  Then `(U_tx, V_mod) = R(+ψ) · (raw_U, raw_V)`, i.e.
    ```
    U     =  cos ψ · raw_U − sin ψ · raw_V
    V_mod =  sin ψ · raw_U + cos ψ · raw_V
    ```
  v7 used `ψ = φ + 135°` for all lines — that's correct for one
  class but 90° off for the other.

- **V flip on PAL-style lines** to recover transmitted V from
  modulated V: `V_tx = -V_mod` on PAL-style lines.

- **The 2-fold "which parity is V-inverted" ambiguity** is intrinsic
  — from a cold start, you can't tell which line was the first
  NTSC-style burst. v8 picks the hypothesis that minimises
  `|ψ_α − ψ_β|`: the *true* hypothesis gives both classes the same
  underlying ψ (it's a single physical LO-vs-subcarrier offset); the
  *wrong* hypothesis makes them appear 180° apart.

- **Self-consistency check is also a confidence indicator.** On
  clean signals you get `|ψ_α − ψ_β|` well under 2° for the right
  hypothesis and ~180° for the wrong one. Anywhere between (say 30°
  and 150°) means burst SNR is too low to trust the decision and
  the C++ port should emit a warning or fall back to a previous
  class-assignment guess.

- **Comb filter (1H delay average) on U, V** is applied *after* the
  V-flip, so adjacent lines now agree in V sign and direct averaging
  is correct: `U_comb[n] = (U[n] + U[n-1]) / 2`.

### Y/C separation

- **Frequency-based, not comb-based, in v8.** Y is LPF below 3 MHz;
  C is BPF 3.5–5.5 MHz around `fSC`. 65-tap Hamming-window FIR is
  enough at 24 MSps. A real 1980s set used L/C resonant networks or
  glass delay-line comb filters; for our purposes the frequency-based
  split is fine and trivially portable to C++.

- **Y BPF leakage is the main residual artifact.** Chroma at 4.43 MHz
  leaks into a 3 MHz Y LPF (rolloff finite, especially with a 65-tap
  Hamming → ~30 dB stopband attenuation). Result: chroma appears as
  Y modulation, visible as "dot crawl" on high-saturation edges. A
  PAL comb filter for Y/C separation would do better but isn't yet
  implemented.

### Levels and scaling

- **Negative modulation on PAL B/G/I.** Sync tip is the *peak* RF
  carrier amplitude after AM envelope detection; the envelope must be
  inverted before sync slicing / sync detection. (Done in
  `demod_real.py`, line `out = (peak − env) / span`.)

- **Burst amplitude is 0.3 × white-to-blanking** in PAL. After the
  factor-of-½ from synchronous demod (cos·cos LPF gives ½ cos Δφ),
  `burst_mag = burst_amplitude / √2 ≈ 0.15 × luma_range`. v8 uses
  `uv_scale = 0.3 / burst_mag`, which empirically gives roughly the
  right saturation on 75 % colour bars. A theoretically-anchored
  scaling would also account for the BT.601 max-U/V scaling factors
  (0.493 for U, 0.877 for V) and the burst-vector magnitude `√2`
  factor.

- **YUV → RGB uses BT.601 coefficients** (PAL is covered by BT.601
  alongside NTSC; they share the matrix).

### Capture-chain reminders

- **TUNERINIT must pass the ADC frequency**, not 0, on the third
  `rx888_send_command(STARTADC)`. This is the fix in PR #16 — without
  it, the R828D's LO ends up wandering and reception is unusable.

- **`demod_real.py`'s carrier auto-detect** picks the strongest narrow
  peak in 3–6 MHz IF. Fine for busy programme content (vision wins via
  broad sidebands), broken for uniform-colour test signals (chroma
  wins as a CW tone). For solid-colour testing, pass `--carrier
  4.57e6` explicitly. For the C++ port, prefer picking the peak with
  the strongest *line-rate* (15.625 kHz) sideband structure — that's
  the vision-only signature.

## Chroma "echoes" / trailing artifacts

Even after v8 decodes the SMS gameplay frame with correct primaries,
there's a visible *afterimage* after sharp coloured edges (e.g. a
ghost of the heart sprite trailing to its right). Theories, in
descending plausibility:

1. **Chroma BPF impulse-response ringing.** The 3.5–5.5 MHz BPF (65
   taps at 24 MSps) has an impulse response that decays over ~30
   samples at the subcarrier centre frequency. After a sharp colour
   transition (e.g. the heart-to-background edge), the BPF output
   rings for ≈1.25 µs = ≈7–8 visible pixels of trailing colour.
   This matches the visual signature precisely. **Cure:** use a
   sharper or comb-based Y/C separator. A 1H delay-line comb
   (subtract adjacent lines to cancel C, average to keep Y) is the
   1980s standard for clean Y/C separation without BPF artifacts.

2. **Y/C cross-talk in the Y LPF.** Chroma at 4.43 MHz isn't fully
   attenuated by a 3 MHz Y LPF (Hamming-window 65-tap gives ~30 dB
   stopband, so chroma leaks at ~3 % into Y). On a coloured sprite
   edge, the Y output briefly modulates from chroma, then settles —
   another contributor to the ghost. **Cure:** same as above
   (notch out fSC ± 0.5 MHz from Y, or use comb).

3. **U/V LPF response.** The 1.5 MHz LPF on the demodulated U,V
   channels has its own group delay (~32 samples = 1.3 µs at
   24 MSps); compared to the Y LPF's similar delay this should
   *match* and not cause offset, but the impulse-response tail of
   the U/V LPF independently smears chroma transitions over ~5–7
   pixels. Likely a smaller contributor than (1) but additive.

4. **AM-to-PM conversion in the R828D tuner front end.** Real-world
   tuners with AGC or saturation effects convert AM (luma) into PM
   on the chroma carrier — a known SDR-PAL gotcha. The signature is
   chroma misregistration after sharp luma edges. We have no
   evidence one way or the other on whether the R828D does this
   noticeably; would need to compare against a baseband CVBS
   capture from a real PAL decoder (e.g. through a video capture
   card) to know.

5. **Reflections in the SMS RF modulator → coax → SDR path.**
   ~10 cm of cable means any reflection echoes are sub-nanosecond
   and wouldn't show as multi-pixel ghosts. Almost certainly not
   this.

For the C++ port, the cleanest fix is a Y/C separator built around a
1-line delay comb: `Y_clean = (Y[n] + Y[n-1])/2 + Y_high_pass_lr`,
`C_clean = (Y[n] - Y[n-1])/2` followed by demodulation. That removes
both (1) and (2) at the cost of vertical resolution loss on
high-frequency vertical chroma transitions — the canonical PAL
trade-off, which real TVs accepted.

## What does and doesn't work

| Path | Status |
|------|--------|
| Windows + ExtIO_sddc + VHF + SMS at 591.2 MHz | ✅ ~70 dB SNR, full PAL visible |
| Linux + `rx888_stream` *with [PR #16](https://github.com/rhgndf/rx888_stream/pull/16)* + SDDC_FX3_v22.img + SMS at 591.2 MHz **mono** | ✅ ~80 dB SNR, decodable Wonder Boy III frame |
| Linux + `rx888_stream` *with PR #16* + SMS at 591.2 MHz **colour** | ✅ v8 decoder; verified end-to-end against `hacktv` colour bars |
| Linux + `rx888_stream` + BBC Micro (~588 MHz tune) **mono** | ⚠️ Picture structure visible, sync detection fragile |
| Linux + `rx888_stream` + BBC Micro **colour** | ❌ Junk output |
| Linux + `rx888_stream` *`rhgndf` main, no PR #16* + any firmware + SMS at 591.2 MHz | ❌ wandering weak carrier, no decodable picture |
| Linux + `rx888_stream` + HF + AM broadcast | ✅ works with a wire antenna |
| Linux + `rx888_stream` + official v1.3.0RC1 firmware blob | ❌ `Transfer failed: PollTimeout` — pre-#225 firmware speaks a different USB protocol |
| `cvbs-decode` (AppImage) on our CVBS | ❌ 0-byte output, exits with "saving JSON" without decoding |

## File map

- `tools/analyze_rx888.py` — quick Welch PSD on a raw real-int16
  capture; useful to spot the vision carrier and check SNR.
- `tools/demod_real.py` — raw int16 → CVBS for `rx888_stream` output
  (this script).
- `tools/demod_iq_wav.py` — same idea but for an SDR Console IQ WAV
  input (carrier already centered at 0 Hz).
- `tools/cvbs_to_image_v5.py` — baseband CVBS → PGM/PNG still. Single
  field, doubled vertically, back-porch DC reference. Optional
  line-rate-Hz override (5th positional arg).
- `tools/cvbs_to_image_v6.py` — same idea but with a TV-style sync
  separator (slicer + duration-classified pulses → broad-pulse-run
  V-sync, per-line H-sync anchor). Produces full-width frames.
  Recommended over v5 for monochrome rendering of clean signals.
- `tools/cvbs_to_image_v7.py` — first colour decoder attempt. Had six
  bugs (notably: auto-detecting fSC from spectrum peak, class-blind
  per-line rotation). Kept for historical reference. **Use v8 for
  colour.**
- `tools/cvbs_to_image_v8.py` — current colour decoder. Fixed fSC at
  textbook 4.43361875 MHz, class-aware per-line burst rotation, and
  auto-resolves the 2-fold V-inversion ambiguity by picking the
  self-consistent hypothesis. Tested against `hacktv -m pal --filter
  test:colourbars` synthetic source (decoded primaries match).
- `tools/demod_stream.py` — chunked streaming demod sketched out as
  the architecture an eventual C++ port would use. Reads raw int16
  from stdin, writes baseband CVBS at 8 MSps (decimated 3×) to
  stdout. Runs at ~0.4× realtime in Python on a 5.9 s capture.
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

## Streaming demod (sketch for C++ port)

`tools/demod_stream.py` is the architecture sketch. It reads chunks
from stdin (default 0.25 s = 6 M samples at 24 MSps), processes each
chunk via a continuous-LO NCO mixer, overlap-save FIR LPF (scipy
`oaconvolve`) with built-in 3× decimation, envelope detect, and
inversion, and writes CVBS chunks to stdout. State carried between
chunks: cumulative sample index (for phase continuity), overlap tail
for the FIR, EMA-tracked peak/floor for inversion scaling.

Pure-Python throughput on a 5.93 s capture (after preallocating
buffers): **~14 s wall = 0.42× realtime**. The work breakdown is
mostly `oaconvolve` doing FFT convolution.

The structure maps directly to a C++ port: a fixed set of ring
buffers, a precomputed FIR coefficient table, an NCO step multiplier
for the LO. No per-frame malloc. C++ wins come from removing Python
per-call overhead and fusing the mix+filter+envelope into a single
loop — numpy already uses SIMD via libmvec/BLAS, so the gain isn't
SIMD-vs-no-SIMD but eliminating numpy bookkeeping and intermediate
buffers.

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
- **Colour decoder** — v8 supersedes v7 and produces correct
  primaries on the `hacktv` synthetic test source. Per-line hue
  drift seen in v7 is resolved (root cause was an off-by-23-kHz
  `fSC`). Remaining polish: confidence-thresholded auto-disambiguation,
  median-smoothing of per-line burst phase, full interlace, BBC
  capture decoding (needs sync separator hardening).
- **`demod_real.py` carrier auto-detect**: picks the strongest
  narrow peak in 3–6 MHz IF. Works for typical content (vision
  carrier wins) but fails for solid-colour test signals where
  chroma is a stronger CW tone. Should select the peak with the
  strongest line-rate sideband structure instead — that's the
  vision-only signature.
- **Sync separator robustness**: v6's slicer-on-raw-CVBS fires on
  chroma cycles when chroma is present in the demod output. v7
  added a 1 MHz LPF before slicing; v6 should get the same fix.
  Sync detection still fragile on weak captures (BBC); a proper TV
  uses hysteresis + a PLL on H-sync to lock through weak pulses,
  we don't.
