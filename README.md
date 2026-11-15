# klipper-virtual-bed-temp

A Klipper extension that estimates the **real build-plate temperature** of a heated bed whose
thermistor sits in the heater pad.

On many printers (e.g. Voron) the bed thermistor is glued into the silicone heater. The pad
reaches its target in a minute or two, while a thick aluminium plate takes 10–20 minutes to
follow. `M190` returns as soon as the pad is hot, so leveling, meshing and the first layer happen
on a plate that is still cold and still expanding.

This module adds a virtual temperature sensor, `virtual_bed_temp`, that models the plate's lag
behind the pad. It shows up in Mainsail/Fluidd next to the other sensors and gives you a
`WAIT_VIRTUAL_BED_TEMP` command for your start macro. No extra hardware is needed. Calibrating it
once with a reference sensor is recommended.

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

You need a reference measurement of the plate surface during one heat-up.

1. **Start at equilibrium.** Leave the bed off for at least 1 h so pad and plate are at room temperature.
2. **Reference sensor (best):** tape a spare 100k NTC thermistor to the top of the plate with
   Kapton tape and cover it with a bit of insulation. Connect it to a free thermistor input and
   add it temporarily:
   ```ini
   [temperature_sensor plate_ref]
   sensor_type: Generic 3950
   sensor_pin: <free thermistor pin>
   ```
   Moonraker logs it once per second together with the bed.
   **Fallback:** an IR thermometer on the plate surface every 60 s, written down as
   `seconds_since_M140,temp` lines in a CSV file. IR readings work on PEI (emissivity ≈ 0.9) but
   not on bare aluminium.
3. **Heat:** `M140 S80`. A large step gives the best resolution. Wait about 25 minutes without
   touching anything.
4. **Fit**, on any machine that can reach Moonraker. Do this before Moonraker's 20-minute history
   runs out, or increase `temperature_store_size` in its `[data_store]` section:
   ```bash
   python3 tools/fit_tau.py --moonraker http://<printer>:7125 --ref "temperature_sensor plate_ref" --save run1.json
   # or with manual readings:
   python3 tools/fit_tau.py --moonraker http://<printer>:7125 --csv ir.csv --save run1.json
   ```
   The tool runs the model over the actual recorded pad temperature and finds the best
   `time_constant` and `loss`. It prints the RMS error, a ready-to-paste config section, and the
   expected soak times. Pass `--no-loss` to fit `time_constant` only. Use `--load run1.json` to
   re-run the fit on saved data.
5. **Verify** with a second run at another target, e.g. 60 °C. With the new values in the config,
   the tool also reports how far the running `virtual_bed_temp` is off from the reference.
6. Remove `plate_ref`.

**Quick estimate without the tool:** the pad reaches target much faster than the plate, so you can
treat it as an instant step. One plate reading `T_plate` at time `t` then gives
`time_constant ≈ (t − t_pad) / ln((T_set − T_start) / (T_set − T_plate))`,
where `t_pad` is when the pad reached its target.

**Why not use the nozzle as the sensor?** A cold nozzle resting on the bed touches it over a
tiny area. The heater block is a large mass that also loses heat to the air, the heat break and
the fans, so it reads many degrees low and adds its own lag. The resulting `time_constant` would
be wrong.

## License

GPLv3, the same as Klipper. See `LICENSE`.
