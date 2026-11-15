# klipper-virtual-bed-temp

A Klipper extension that estimates the **real build-plate temperature** of a heated bed whose
thermistor sits in the heater pad.

On many printers (e.g. Voron) the bed thermistor is glued into the silicone heater. The pad
reaches its target in a minute or two, while a thick aluminium plate takes 10–20 minutes to
follow. `M190` returns as soon as the pad is hot, so leveling, meshing and the first layer happen
on a plate that is still cold and still expanding.

This module adds a virtual temperature sensor, `virtual_bed_temp`, that models the plate's lag
behind the pad. It shows up in Mainsail/Fluidd next to the other sensors and gives you a
`WAIT_VIRTUAL_BED_TEMP` command for your start macro. No extra hardware is needed: calibration uses
the bed heater's own power log plus two readings from a handheld IR thermometer.

## Model

A first-order thermal lag, updated once per second:

```
dT_plate/dt = ((T_pad − T_plate) − loss · (T_plate − T_ambient)) / time_constant
```

| Parameter       | Meaning                                                                           |
|-----------------|-----------------------------------------------------------------------------------|
| `time_constant` | Seconds for the plate to cover ~63 % of a temperature step. Typically 200–600 s.  |
| `loss`          | Optional: how far the surface settles below the pad at equilibrium. 0 = none.     |
| `ambient_temp`  | Temperature the plate loses heat to (only used when `loss > 0`).                  |

At equilibrium the plate settles at `(T_pad + loss · T_ambient) / (1 + loss)`.

The estimate starts at the pad temperature when Klipper starts, which assumes pad and plate are
at the same temperature at that point. If Klipper restarts while the bed is still warming up or
cooling down, use `SET_VIRTUAL_BED_TEMP TEMP=` to correct it.

## Installation

```bash
cd ~
git clone https://github.com/<github-user>/klipper-virtual-bed-temp.git
~/klipper-virtual-bed-temp/install.sh
```

`install.sh` symlinks `virtual_bed_temp.py` into `~/klipper/klippy/extras/`, hides the link from
Klipper's git status (so Moonraker doesn't report Klipper as dirty), and restarts Klipper.
Environment overrides: `KLIPPER_PATH`, `KLIPPER_SERVICE`, `NO_RESTART=1`.
Remove it with `install.sh --uninstall`, after deleting the config section.

### Updates via Mainsail / Fluidd

Add to `moonraker.conf` (see `examples/moonraker.conf`):

```ini
[update_manager klipper-virtual-bed-temp]
type: git_repo
path: ~/klipper-virtual-bed-temp
origin: https://github.com/<github-user>/klipper-virtual-bed-temp.git
primary_branch: main
managed_services: klipper
install_script: install.sh
```

## Configuration

```ini
[virtual_bed_temp]
heater: heater_bed        # heater whose temperature drives the model
time_constant: 300        # s, > 10
#loss: 0                  # 0..1
#ambient_temp: 22         # °C
```

For more than one bed or zone, use named sections such as `[virtual_bed_temp plate2]` and add
`SENSOR=plate2` to the commands.

### Start macro

```
M190 S{bed}                  ; pad at target
WAIT_VIRTUAL_BED_TEMP MARGIN=2  ; plate within 2 °C of its steady state
```

`WAIT_VIRTUAL_BED_TEMP` waits until the plate is within `MARGIN` of its *steady state* for the
current heater target, not the target itself. This matters because with `loss > 0` the plate never
reaches the target, and waiting for it would never finish. If the heater is off, the command
returns immediately. Like `M190`, it blocks the G-code queue while waiting.

`TEMPERATURE_WAIT SENSOR=virtual_bed_temp MINIMUM=…` also works.

## G-code commands

| Command | Description |
|---|---|
| `SET_VIRTUAL_BED_TEMP [SENSOR=] [TIME_CONSTANT=] [LOSS=] [TEMP=]` | Change parameters at runtime. `TEMP=` resets the estimate. Always prints the current values. Changes are lost on restart. |
| `WAIT_VIRTUAL_BED_TEMP [SENSOR=] [MARGIN=2]` | Wait for the plate to soak (see above). |

Status fields (`printer.virtual_bed_temp`): `temperature`, `time_constant`, `loss`,
`ambient_temp`, `steady_state` (plate equilibrium for the current heater target, 0 when the heater is off).

## Calibration

One heat-up of about 30 minutes, no extra sensor needed.

**How it works.** Once the pad reaches its target, the heater only has to replace the heat flowing
into the plate, plus some losses that stay roughly constant. That flow shrinks as the plate catches
up, so the heater power fades at exactly the plate's rate:
`P(t) ≈ P_steady + A·e^(−t/τ)`. Moonraker logs the power once per second, so `tools/fit_tau.py`
can get τ from that log. No heater wattage or plate mass is needed. Two IR readings then pin down
`loss` and serve as a cross-check.

### Interactive (recommended): `tools/calibrate.py`

Run it on the Klipper host, i.e. the Pi, over SSH:

```bash
python3 ~/klipper-virtual-bed-temp/tools/calibrate.py
```

What it does:
- Checks Klipper: it must be ready, not printing, with the bed heater off. It also checks that the bed
  temperature is stable.
- Asks for a first IR reading as the starting value, then starts heating with `M140 S80`.
- Records bed temperature and heater power itself, once per second. No Moonraker history settings
  are needed.
- Beeps every 5 minutes and asks for an IR reading of the plate. Type the number and press Enter.
  The reading is dated 10 s before you pressed Enter, to allow for walking back to the keyboard.
- After each reading, shows how far the currently configured model is off and a preliminary fit.
- Finishes once there is enough data (at least 25 min, and enough power data to cover the plate's
  heat-up). Then it prints the recommended values and the change from your current config.
  Optionally it applies them to the running Klipper with `SET_VIRTUAL_BED_TEMP` for testing.
- Saves the session as `~/calib-<date>.json/.csv`, which `fit_tau.py --load … --csv …` can re-fit.

While it's waiting, press Enter for a menu: extra reading, finish now, or abort. Ctrl-C or a dropped
SSH connection turns the bed heater off and saves the data so far.
Options: `--target`, `--interval`, `--entry-delay`, `--min-duration`, `--moonraker`
(default `http://localhost:7125`). Try it without a printer:
`calibrate.py --simulate 500 0.03 --sim-auto --speed 120`.

### Manual: record, then `tools/fit_tau.py`

#### 1. Prepare (once)

The heat-up has to fit in Moonraker's temperature history, which is 20 minutes by default.
Raise it in `moonraker.conf` and restart Moonraker:

```ini
[data_store]
temperature_store_size: 3600   # seconds of history
```

#### 2. Record

1. **Start at equilibrium.** Leave the bed off for at least 1 h so pad and plate are at room temperature.
2. Send `M140 S80` and start a stopwatch at the same moment. A large temperature step gives
   the best resolution.
3. Take two IR thermometer readings of the plate surface, near the centre:
   - one at about **7 min**, while the plate is still clearly rising;
   - one at about **25 min**, when the plate is nearly settled.

   Measure on the PEI sheet (emissivity ≈ 0.9). Bare aluminium reflects too much for IR to read
   correctly. Hold the thermometer the same way both times.
4. Wait until at least 25 minutes have passed, with nothing else heating or moving.

Write the readings to `ir.csv` as `seconds_since_M140,temp`:

```
420,51.5
1500,73.0
```

#### 3. Fit

On any machine that can reach Moonraker:

```bash
python3 tools/fit_tau.py --moonraker http://<printer>:7125 --csv ir.csv --save run1.json
```

The output includes:
- the power fit (`P = … · exp(−t/τ)`);
- how far the model is off from your readings (RMS);
- a **cross-check** fitted from the readings alone, with a warning if it disagrees with the power
  fit by more than 25 %;
- a ready-to-paste `[virtual_bed_temp]` section and the expected soak times.

Without `--csv`, the tool fits from power alone and uses `loss: 0`. Use `--load run1.json` to
re-run the fit on saved data. `--skip` (default 120 s) leaves out the controller's settling
period after the pad first reaches its target.

#### 4. Check

Put the values in your config and restart Klipper. On a later cold heat-up to a different
target (e.g. 60 °C), compare an IR reading with the `virtual_bed_temp` value in Mainsail.

**Accuracy.** The plate's rate comes from about 20 minutes of power data, so IR timing and reading
errors hardly affect it. In simulations with a noisy controller and IR readings off by ±0.6 °C,
the tool recovered `time_constant` within 1 % and `loss` within 0.003
(`tools/fit_tau.py --selftest TAU LOSS [--csv …]`). The weakest link is the IR thermometer's
absolute accuracy, which mainly affects `loss`.

**Other reference sensors.** A thermistor taped to the plate and declared as
`[temperature_sensor …]` also works: use `--ref "temperature_sensor plate_ref"` instead of `--csv`.

**Why not use the nozzle as the reference?** A cold nozzle resting on the bed touches it over a
tiny area. The heater block is a large mass that also loses heat to the air, the heat break and
the fans, so it reads many degrees low and adds its own lag. The resulting `time_constant` would
be wrong.

## License

GPLv3, the same as Klipper. See `LICENSE`.
