#!/usr/bin/env python3
"""Interactive calibration of [virtual_bed_temp] against a live printer.

Guides one cold heat-up: checks the printer via Moonraker, starts the bed
heater, samples heater temperature/power at 1 Hz in the background and asks
for an IR reading of the plate every --interval seconds. A reading is
timestamped --entry-delay seconds before it was entered. After each reading
it shows how far the configured model is off and a preliminary fit; at the
end it recommends time_constant/loss (fit_tau.analyze: heater power decay +
readings), optionally applies them with SET_VIRTUAL_BED_TEMP, and saves the
session for fit_tau.py --load.

Meant to run on the Klipper host (default Moonraker URL localhost:7125).
While waiting, press Enter for a menu (extra reading, finish, abort).
Ctrl-C or a lost SSH session (SIGHUP/SIGTERM) aborts: bed heater off, data
saved to --save-dir (default ~, outside the plugin's git checkout).

--simulate TAU LOSS replaces the printer with a time-accelerated two-node
pad/plate simulation (fit_tau.synthetic physics); --sim-auto also answers
the reading prompts with the simulated plate temperature ± 0.5 °C.

Stdlib only, POSIX terminal (uses select on stdin).
"""
import argparse
import json
import os
import random
import select
import signal
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fit_tau  # noqa: E402

MODEL = 'virtual_bed_temp'


class Moonraker:
    """Minimal Moonraker HTTP client; times from time.monotonic()."""

    def __init__(self, url):
        self.url = url.rstrip('/')

    def _req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=data,
            headers={'Content-Type': 'application/json'} if data else {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)['result']

    def info(self):
        return self._req('/printer/info')

    def query(self, objects):
        """{object: fields-or-None} -> {object: status} (missing objects absent)."""
        return self._req('/printer/objects/query', {'objects': objects})['status']

    def objects(self):
        return self._req('/printer/objects/list')['objects']

    def gcode(self, script):
        self._req('/printer/gcode/script', {'script': script})

    def now(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)


class SimPrinter:
    """Two-node pad/plate printer behind the Moonraker interface, running
    `speed` times faster than real time. Physics as fit_tau.synthetic; a
    virtual_bed_temp model with time_constant 300, loss 0 runs alongside."""

    def __init__(self, tau, loss, speed, ambient=22.):
        self.speed, self.ambient = speed, ambient
        self.c_plate, self.c_pad, self.watts, self.max_power = 2500., 150., 750., 0.6
        self.g = self.c_plate / tau
        self.h = loss * self.g
        self.t_pad = self.t_plate = self.model = ambient
        self.model_tau, self.model_loss = 300., 0.
        self.target = self.pwm = self.integ = 0.
        self.measured = ambient
        self.rng = random.Random(2)
        self.t0_real = time.monotonic()
        self.sim_t = 0.
        self.lock = threading.Lock()

    def _advance(self):
        goal = (time.monotonic() - self.t0_real) * self.speed
        while self.sim_t + 1. <= goal:
            self.sim_t += 1.
            self.measured = self.t_pad + self.rng.gauss(0., 0.15)
            if self.target:
                err = self.target - self.measured
                u = 0.08 * err + self.integ
                if 0. < u < self.max_power:
                    self.integ += 0.004 * err
                self.pwm = min(max(u, 0.), self.max_power)
            else:
                self.pwm = 0.
            q = self.g * (self.t_pad - self.t_plate)
            self.t_pad += (self.pwm * self.watts - q
                           - 0.5 * (self.t_pad - self.ambient)) / self.c_pad
            self.t_plate += (q - self.h * (self.t_plate - self.ambient)) / self.c_plate
            self.model += ((self.measured - self.model)
                           - self.model_loss * (self.model - self.ambient)) / self.model_tau

    def info(self):
        return {'state': 'ready'}

    def objects(self):
        return ['heater_bed', MODEL, 'print_stats']

    def query(self, objects):
        with self.lock:
            self._advance()
            return {'heater_bed': {'temperature': self.measured,
                                   'target': self.target, 'power': self.pwm},
                    MODEL: {'temperature': self.model,
                            'time_constant': self.model_tau,
                            'loss': self.model_loss},
                    'print_stats': {'state': 'standby'}}

    def gcode(self, script):
        with self.lock:
            self._advance()
            words = script.split()
            if words[0] == 'M140':
                self.target = float(words[1][1:])
            elif words[0] == 'SET_VIRTUAL_BED_TEMP':
                for w in words[1:]:
                    k, v = w.split('=')
                    if k == 'TIME_CONSTANT':
                        self.model_tau = float(v)
                    elif k == 'LOSS':
                        self.model_loss = float(v)

    def plate_truth(self):
        with self.lock:
            self._advance()
            return self.t_plate

    def now(self):
        return (time.monotonic() - self.t0_real) * self.speed

    def sleep(self, s):
        time.sleep(s / self.speed)


class Recorder(threading.Thread):
    """Polls heater (and model, if present) once per printer-second.
    samples: [(t, pad_temp, target, power, model_temp_or_None)], t on the
    backend clock. Stops on stop(); connection errors are counted, not raised."""

    def __init__(self, backend, heater, has_model):
        super().__init__(daemon=True)
        self.backend, self.heater, self.has_model = backend, heater, has_model
        self.samples, self.errors = [], 0
        self.lock = threading.Lock()
        self._halt = threading.Event()  # Thread already defines _stop()

    def run(self):
        objs = {self.heater: ['temperature', 'target', 'power']}
        if self.has_model:
            objs[MODEL] = ['temperature']
        while not self._halt.is_set():
            t = self.backend.now()
            try:
                st = self.backend.query(objs)
                h = st[self.heater]
                m = st.get(MODEL, {}).get('temperature')
                with self.lock:
                    self.samples.append((t, h['temperature'], h['target'],
                                         h.get('power', 0.), m))
            except Exception:
                self.errors += 1
            self.backend.sleep(max(0., 1. - (self.backend.now() - t)))

    def stop(self):
        self._halt.set()

    def last(self):
        with self.lock:
            return self.samples[-1] if self.samples else None

    def series(self, t0):
        """1 Hz arrays from t0 (index 0 = t0) up to the newest sample, each
        second taking the latest sample at or before it.
        Returns (pad, power, model) lists; model entries may be None."""
        with self.lock:
            samples = list(self.samples)
        pad, power, model = [], [], []
        j = 0
        if not samples:
            return pad, power, model
        n = int(samples[-1][0] - t0) + 1
        for i in range(max(n, 0)):
            t = t0 + i
            while j + 1 < len(samples) and samples[j + 1][0] <= t:
                j += 1
            s = samples[j]
            pad.append(s[1])
            power.append(s[3])
            model.append(s[4])
        return pad, power, model


def slope(values):
    """Least-squares slope per sample; 0 for fewer than 3 values."""
    n = len(values)
    if n < 3:
        return 0.
    xm, ym = (n - 1) / 2., sum(values) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(values))
    return num / sum((i - xm) ** 2 for i in range(n))


def fmt_t(s):
    return '%d:%02d' % (s // 60, s % 60)


def ask(prompt, auto=None):
    """input() with optional automatic answer (echoed)."""
    if auto is not None:
        print(prompt + str(auto))
        return str(auto)
    return input(prompt)


def ask_yes(prompt, default, auto=None):
    """Yes/no prompt; Enter gives default. auto (bool) answers without input."""
    if auto is not None:
        print(prompt + ('y' if auto else 'n'))
        return auto
    ans = input(prompt).strip().lower()
    return default if not ans else ans.startswith('y')


def read_temp(prompt, auto_value=None):
    """Prompt until a float, '' (skip) or 'q' (finish). Returns float/None/'q'."""
    while True:
        ans = ask(prompt, None if auto_value is None else '%.1f' % auto_value).strip()
        if not ans:
            return None
        if ans.lower() in ('q', 'd', 'done'):
            return 'q'
        try:
            v = float(ans.replace(',', '.'))
        except ValueError:
            print('  enter a temperature in °C, Enter to skip, q to finish')
            continue
        if not -20. < v < 200.:
            print('  implausible, try again')
            continue
        return v


def enter_pressed():
    """True if a line is waiting on stdin (consumed). Never blocks."""
    if not sys.stdin.isatty():
        return False
    r, _, _ = select.select([sys.stdin], [], [], 0)
    if r:
        sys.stdin.readline()
        return True
    return False


class Session:
    """One calibration run; see module docstring."""

    def __init__(self, args, backend):
        self.a, self.b = args, backend
        self.auto = args.sim_auto
        self.readings = []  # [(seconds since t0, temp)]
        self.t0 = None
        self.t_init = None
        self.heating = False
        self.rec = None
        self.model_cfg = None

    def say(self, *lines):
        print('\n'.join(lines))

    # --- phases ---------------------------------------------------------
    def preflight(self):
        a, b = self.a, self.b
        info = b.info()
        if info.get('state') != 'ready':
            sys.exit('Klipper is %s: %s' % (info.get('state'), info.get('state_message', '')))
        objs = b.objects()
        if a.heater not in objs:
            sys.exit('%s not found in Klipper' % a.heater)
        has_model = MODEL in objs
        q = {a.heater: None, 'print_stats': ['state']}
        if has_model:
            q[MODEL] = None
        st = b.query(q)
        if st.get('print_stats', {}).get('state') in ('printing', 'paused'):
            sys.exit('printer is %s — not calibrating' % st['print_stats']['state'])
        h = st[a.heater]
        if has_model:
            self.model_cfg = st[MODEL]
            self.say('configured model: time_constant %.0f, loss %.3f'
                     % (self.model_cfg['time_constant'], self.model_cfg['loss']))
        else:
            self.say('note: [%s] not loaded — calibrating anyway, '
                     'no comparison with a running model' % MODEL)
        if h['target'] > 0:
            sys.exit('%s is already heating (target %.0f) — turn it off and let '
                     'the bed cool to room temperature first' % (a.heater, h['target']))
        self.say('%s now %.1f °C' % (a.heater, h['temperature']))
        self.rec = Recorder(b, a.heater, has_model)
        self.rec.start()

    def intro(self):
        a = self.a
        self.say('',
                 'Calibration heat-up to %.0f °C, about %d–%d min.' % (
                     a.target, a.min_duration // 60, a.max_duration // 60),
                 'You need: an IR thermometer, the bed at room temperature '
                 '(heater off ≥ 1 h).',
                 'Measure on the PEI near the bed centre, same spot and distance '
                 'each time.',
                 'Readings are taken as measured %d s before you press Enter.'
                 % a.entry_delay, '')
        if not ask_yes('Bed off for at least 1 hour? [y/N] ', False,
                       True if self.auto else None):
            sys.exit('let it cool first, then run again')
        self.b.sleep(a.baseline)
        if not self.rec.samples:
            sys.exit('no data from Moonraker during %d s baseline' % a.baseline)
        pad, _, _ = self.rec.series(self.rec.samples[0][0])
        drift = slope(pad) * 60
        if abs(drift) > 0.5:
            self.say('warning: pad temperature drifting %.1f °C/min — bed not at '
                     'equilibrium, results will be off' % drift)
        sim_truth = self.b.plate_truth() + 0.3 if self.auto else None
        v = read_temp('Measure the plate now (°C, Enter to skip): ', sim_truth)
        pad_now = self.rec.last()[1]
        if isinstance(v, float):
            self.t_init = v
            if abs(v - pad_now) > 3.:
                self.say('warning: plate %.1f vs pad %.1f °C — not at equilibrium '
                         'or IR offset; continuing' % (v, pad_now))
        else:
            self.t_init = pad_now

    def start_heating(self):
        a = self.a
        if not self.auto:
            input('\nPress Enter to start heating (sends M140 S%.0f) ' % a.target)
        self.b.gcode('M140 S%.0f' % a.target)
        self.t0 = self.b.now()
        self.heating = True
        self.say('heating started — next reading at %s (Enter for menu)'
                 % fmt_t(a.interval))

    def run(self):
        a = self.a
        next_prompt = a.interval
        last_status = -1e9
        while True:
            el = self.b.now() - self.t0
            done, why = self.finished(el)
            if done:
                self.say('\n' + why)
                v = read_temp('Final reading now (°C, Enter to skip): ',
                              self.sim_reading())
                if isinstance(v, float):
                    self.add_reading(v)
                return
            if el >= next_prompt:
                print('\a', end='')
                v = read_temp('\n[%s] Measure the plate now (°C, Enter to skip, '
                              'q to finish): ' % fmt_t(el), self.sim_reading())
                if v == 'q':
                    return
                if isinstance(v, float):
                    self.add_reading(v)
                next_prompt = (self.b.now() - self.t0) + a.interval
            elif enter_pressed():
                c = ask('\n[r] extra reading  [f] finish now  [a] abort  '
                        '[Enter] continue: ').strip().lower()
                if c == 'r':
                    v = read_temp('plate (°C): ')
                    if isinstance(v, float):
                        self.add_reading(v)
                elif c == 'f':
                    return
                elif c == 'a':
                    raise KeyboardInterrupt
            if el - last_status >= a.status_every:
                s = self.rec.last()
                if s:
                    model = '' if s[4] is None else '  model %.1f' % s[4]
                    print('  %s  pad %.1f/%.0f  power %3.0f%%%s'
                          % (fmt_t(el), s[1], s[2], s[3] * 100, model))
                last_status = el
            self.b.sleep(0.5)

    def finished(self, el):
        a = self.a
        if el >= a.max_duration:
            return True, 'maximum duration reached.'
        if el < a.min_duration:
            return False, ''
        r = self.analyze(quiet=True)
        if r and r['power_s'] >= 2.5 * r['tau_eff'] and len(self.readings) >= 2:
            return True, 'enough data recorded (%.0f s of power ≥ 2.5 × %.0f s).' % (
                r['power_s'], r['tau_eff'])
        return False, ''

    # --- data -----------------------------------------------------------
    def sim_reading(self):
        if not self.auto:
            return None
        return self.b.plate_truth() + random.uniform(-0.5, 0.5)

    def add_reading(self, v):
        t = self.b.now() - self.t0 - self.a.entry_delay
        self.readings.append((t, v))
        pad, _, model = self.rec.series(self.t0)
        i = min(int(round(t)), len(pad) - 1)
        line = '  reading %.1f °C at %s' % (v, fmt_t(t))
        if model and model[i] is not None:
            d = model[i] - v
            line += '; running model %.1f → %s by %.1f °C' % (
                model[i], 'high' if d > 0 else 'low', abs(d))
        self.say(line)
        r = self.analyze(quiet=True)
        if r:
            self.say('  preliminary: time_constant ≈ %.0f, loss ≈ %.3f '
                     '(%.0f s power data, want ≥ %.0f)'
                     % (r['tau'], r['loss'], r['power_s'], 2.5 * r['tau_eff']))

    def analyze(self, quiet=False):
        pad, power, _ = self.rec.series(self.t0)
        ref = [(int(round(t)), v) for t, v in self.readings]
        try:
            return fit_tau.analyze(pad, power, self.a.target, ref, self.t_init,
                                   self.t_init, self.a.skip)
        except ValueError as e:
            if not quiet:
                self.say('cannot fit yet: %s' % e)
            return None

    # --- results --------------------------------------------------------
    def report(self):
        r = self.analyze()
        if not r:
            return
        tau, loss = r['tau'], r['loss']
        self.say('', '=' * 60,
                 'power fit:   plate rate %.0f s from %d s of heater power'
                 % (r['tau_eff'], r['power_s']))
        if r['ref_rms'] is not None:
            self.say('readings:    %d, fitted model within %.2f °C rms'
                     % (len(self.readings), r['ref_rms']))
        if r['free']:
            self.say('cross-check: readings alone → time_constant %.0f, loss %.3f'
                     % r['free'][:2])
        for w in r['warnings']:
            self.say('warning:     ' + w)
        self.say('', 'Recommended:  time_constant %.0f   loss %.3f   ambient_temp %.0f'
                 % (tau, loss, self.t_init))
        if self.model_cfg:
            ct, cl = self.model_cfg['time_constant'], self.model_cfg['loss']
            if abs(tau - ct) / ct < 0.05 and abs(loss - cl) < 0.01:
                self.say('Current config is already within 5 % — no change needed.')
            else:
                self.say('Change:       time_constant %.0f → %.0f (%+.0f %%), '
                         'loss %.3f → %.3f' % (ct, tau, (tau - ct) / ct * 100, cl, loss))
        self.say('', *fit_tau.soak_table(tau, loss, self.t_init, 2.))
        self.say('', 'Put this into printer.cfg:', '',
                 '[%s]' % MODEL, 'heater: %s' % self.a.heater,
                 'time_constant: %.0f' % tau, 'loss: %.3f' % loss,
                 'ambient_temp: %.0f' % self.t_init, '')
        if self.model_cfg and ask_yes(
                'Apply to the running Klipper now (until restart)? [y/N] ',
                False, True if self.auto else None):
            self.b.gcode('SET_VIRTUAL_BED_TEMP TIME_CONSTANT=%.0f LOSS=%.3f'
                         % (tau, loss))
            self.say('applied.')

    def save(self):
        """Write store-shaped JSON (+ CSV of readings) for fit_tau.py --load."""
        if self.t0 is None or not self.rec:
            return
        base = os.path.join(self.a.save_dir,
                            time.strftime('calib-%Y%m%d-%H%M%S'))
        first = self.rec.samples[0][0] if self.rec.samples else self.t0
        lead = max(int(self.t0 - first), 1)
        pad, power, model = self.rec.series(self.t0 - lead)
        targets = [0.] * lead + [self.a.target] * (len(pad) - lead)
        store = {self.a.heater: {'temperatures': pad, 'targets': targets,
                                 'powers': power}}
        if any(m is not None for m in model):
            store[MODEL] = {'temperatures': [m or 0. for m in model]}
        with open(base + '.json', 'w') as f:
            json.dump(store, f)
        with open(base + '.csv', 'w') as f:
            f.write('# seconds since M140, plate °C\n')
            if self.t_init is not None:
                f.write('0,%.1f\n' % self.t_init)
            for t, v in self.readings:
                f.write('%.0f,%.1f\n' % (t, v))
        self.say('saved %s.json / .csv  (re-fit: fit_tau.py --load %s.json --csv %s.csv)'
                 % (base, base, base))

    def heater_off(self, ask_first):
        if not self.heating:
            return
        if not ask_first or ask_yes('Turn the bed heater off? [Y/n] ', True,
                                    True if self.auto else None):
            self.b.gcode('M140 S0')
            self.say('bed heater off.')
        self.heating = False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--moonraker', default='http://localhost:7125', metavar='URL')
    ap.add_argument('--heater', default='heater_bed')
    ap.add_argument('--target', type=float, default=80., help='bed target °C (default 80)')
    ap.add_argument('--interval', type=int, default=300,
                    help='seconds between reading prompts (default 300)')
    ap.add_argument('--entry-delay', type=int, default=10,
                    help='a reading is dated this many s before Enter (default 10)')
    ap.add_argument('--min-duration', type=int, default=1500, help='s (default 1500)')
    ap.add_argument('--max-duration', type=int, default=3600, help='s (default 3600)')
    ap.add_argument('--skip', type=int, default=120,
                    help='controller settling excluded from the power fit, s')
    ap.add_argument('--baseline', type=int, default=30,
                    help='s of idle sampling before heating, used for the '
                         'equilibrium check (default 30)')
    ap.add_argument('--status-every', type=int, default=30,
                    help='status line interval, s (default 30)')
    ap.add_argument('--save-dir', default=os.path.expanduser('~'))
    ap.add_argument('--simulate', nargs=2, type=float, metavar=('TAU', 'LOSS'))
    ap.add_argument('--speed', type=float, default=60.,
                    help='simulation speed-up (default 60)')
    ap.add_argument('--sim-auto', action='store_true',
                    help='with --simulate: answer all prompts automatically')
    args = ap.parse_args()
    if args.sim_auto and not args.simulate:
        ap.error('--sim-auto needs --simulate')

    def hangup(signum, frame):
        # stdout may be gone with the terminal; abort path must not fail on print
        sys.stdout = open(os.devnull, 'w')
        raise KeyboardInterrupt
    signal.signal(signal.SIGHUP, hangup)
    signal.signal(signal.SIGTERM, hangup)

    backend = (SimPrinter(*args.simulate, speed=args.speed) if args.simulate
               else Moonraker(args.moonraker))
    s = Session(args, backend)
    try:
        s.preflight()
        s.intro()
        s.start_heating()
        s.run()
        s.report()
        s.save()
        s.heater_off(ask_first=True)
    except KeyboardInterrupt:
        print('\naborted.')
        try:
            s.heater_off(ask_first=False)
        finally:
            s.save()
        sys.exit(1)
    finally:
        if s.rec:
            s.rec.stop()
        if s.rec and s.rec.errors:
            print('note: %d Moonraker polls failed during the run' % s.rec.errors)


if __name__ == '__main__':
    main()
