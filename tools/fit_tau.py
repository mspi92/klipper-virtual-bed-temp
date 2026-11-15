#!/usr/bin/env python3
"""Fit time_constant and loss of [virtual_bed_temp] to a recorded heat-up.

Data source: Moonraker /server/temperature_store (1 sample/s, newest last,
default 1200 s history). The heat-up start (t=0) is the first sample where
the heater target rises from <=0 to >0, or --start-ago seconds before the
newest sample.

Plate reference, one of:
  --ref NAME   a Klipper sensor logged by Moonraker, e.g.
               "temperature_sensor plate_ref"
  --csv FILE   manual readings, lines "seconds,temp_c", seconds since t=0

The model is simulated over the recorded heater trace, starting from the
plate's equilibrium temperature (first reference sample, else heater temp at
t=0), which is also used as ambient unless --ambient is given.

Stdlib only. Exit status 1 on missing/inconsistent data.
"""
import argparse
import json
import math
import sys
import urllib.request

DT = 1.  # Moonraker temperature_store sample interval, s


def fetch_store(url):
    """Return Moonraker's temperature store: {name: {"temperatures": [...], ...}}."""
    with urllib.request.urlopen(url.rstrip('/') + '/server/temperature_store',
                                timeout=10) as resp:
        return json.load(resp)['result']


def find_start(targets):
    """Index of the last <=0 → >0 target transition; None if absent."""
    start = None
    for i in range(1, len(targets)):
        if targets[i - 1] <= 0. < targets[i]:
            start = i
    return start


def simulate(heater, tau, loss, t_init, ambient):
    """Model trajectory, one value per heater sample (value before each step)."""
    temp = t_init
    out = []
    for h in heater:
        out.append(temp)
        temp += ((h - temp) - loss * (temp - ambient)) * DT / tau
    return out


def rms(sim, ref):
    """RMS error over ref = [(index, temp)]; indices beyond sim are ignored."""
    errs = [(sim[i] - t) ** 2 for i, t in ref if i < len(sim)]
    return math.sqrt(sum(errs) / len(errs))


def golden(f, lo, hi, iters=60):
    """Minimiser of unimodal f on [lo, hi]."""
    g = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return (a + b) / 2


def fit(heater, ref, t_init, ambient, fit_loss, tau_range=(20., 5000.)):
    """Best (tau, loss, rms). loss grid 0..0.3 step 0.005 when fit_loss, else 0."""
    best = None
    losses = [i * 0.005 for i in range(61)] if fit_loss else [0.]
    for loss in losses:
        def err(tau):
            return rms(simulate(heater, tau, loss, t_init, ambient), ref)
        tau = golden(err, *tau_range)
        e = err(tau)
        if best is None or e < best[2]:
            best = (tau, loss, e)
    return best


def steady_state(target, loss, ambient):
    """Plate equilibrium temperature for a heater held at target."""
    return (target + loss * ambient) / (1 + loss)


def time_to_reach(target, margin, tau, loss, ambient):
    """Seconds from equilibrium at ambient until the plate is within margin of
    its steady state, heater treated as an ideal step to target; 0 if the
    margin already covers the whole rise."""
    steady = steady_state(target, loss, ambient)
    if steady - margin <= ambient:
        return 0.
    k = (1 + loss) / tau
    return math.log((steady - ambient) / margin) / k


def load_csv(path, start):
    """Manual readings as [(store index, temp)]; seconds rounded to samples."""
    ref = []
    with open(path) as f:
        for line in f:
            line = line.split('#')[0].strip()
            if not line:
                continue
            sec, temp = (float(x) for x in line.split(','))
            ref.append((start + int(round(sec / DT)), temp))
    return ref


def synthetic(tau, loss, ambient=22., target=80., n=1500):
    """Store-shaped data: heater ramps to target in ~90 s, plate follows the model."""
    heater, targets = [], []
    h = ambient
    for i in range(n):
        tgt = target if i >= 100 else 0.
        if tgt:
            h += (tgt - h) * DT / 20.
        heater.append(h)
        targets.append(tgt)
    plate = simulate(heater, tau, loss, ambient, ambient)
    return {'heater_bed': {'temperatures': heater, 'targets': targets},
            'temperature_sensor plate_ref': {'temperatures': plate}}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--moonraker', metavar='URL',
                     help='e.g. http://mainsailos.local:7125')
    src.add_argument('--load', metavar='FILE', help='store JSON saved with --save')
    src.add_argument('--selftest', nargs=2, type=float, metavar=('TAU', 'LOSS'),
                     help='fit synthetic data generated with known TAU, LOSS')
    ap.add_argument('--save', metavar='FILE', help='write the fetched store to FILE')
    ap.add_argument('--heater', default='heater_bed')
    ref_src = ap.add_mutually_exclusive_group()
    ref_src.add_argument('--ref', metavar='SENSOR')
    ref_src.add_argument('--csv', metavar='FILE')
    ap.add_argument('--start-ago', type=int, metavar='S',
                    help='t=0 is S seconds before the newest sample')
    ap.add_argument('--ambient', type=float)
    ap.add_argument('--no-loss', action='store_true', help='fix loss at 0')
    ap.add_argument('--margin', type=float, default=2.,
                    help='TEMPERATURE_WAIT margin for the timing table, °C')
    args = ap.parse_args()

    if args.selftest:
        store = synthetic(*args.selftest)
        args.ref = 'temperature_sensor plate_ref'
    elif args.load:
        with open(args.load) as f:
            store = json.load(f)
    else:
        store = fetch_store(args.moonraker)
    if args.save:
        with open(args.save, 'w') as f:
            json.dump(store, f)
    if not (args.ref or args.csv):
        sys.exit('error: need --ref or --csv')
    if args.heater not in store:
        sys.exit('error: %s not in store (have: %s)' % (args.heater, ', '.join(store)))

    temps = store[args.heater]['temperatures']
    if args.start_ago is not None:
        start = len(temps) - 1 - args.start_ago
    else:
        start = find_start(store[args.heater].get('targets', []))
    if start is None or not 0 <= start < len(temps):
        sys.exit('error: heat-up start not found in store — pass --start-ago')
    heater = temps[start:]

    if args.csv:
        ref = [(i - start, t) for i, t in load_csv(args.csv, start)]
        t_init = ref[0][1] if ref and ref[0][0] == 0 else heater[0]
    else:
        if args.ref not in store:
            sys.exit('error: %s not in store (have: %s)' % (args.ref, ', '.join(store)))
        plate = store[args.ref]['temperatures'][start:]
        ref = list(enumerate(plate))
        t_init = plate[0]
    ref = [(i, t) for i, t in ref if 0 <= i < len(heater)]
    if len(ref) < 3:
        sys.exit('error: fewer than 3 reference points inside the recorded window')
    ambient = t_init if args.ambient is None else args.ambient

    tau, loss, err = fit(heater, ref, t_init, ambient, not args.no_loss)
    print('samples:       %d s of heater data, %d reference points' % (len(heater), len(ref)))
    print('start temp:    %.1f °C   ambient: %.1f °C' % (t_init, ambient))
    print('time_constant: %.0f' % tau)
    print('loss:          %.3f' % loss)
    print('rms error:     %.2f °C' % err)
    if heater[-1] - heater[0] > 0 and len(heater) * DT < 3 * tau:
        print('warning: record covers < 3·tau — fit is extrapolating, record longer')
    for name in store:
        if name.split()[0] == 'virtual_bed_temp':
            cur = store[name]['temperatures'][start:]
            print('current %s rms vs reference: %.2f °C' % (name, rms(cur, ref)))
    print('\nsoak from %.0f °C until plate is within %.1f °C of steady state'
          ' (ideal heater step):' % (ambient, args.margin))
    for target in (60., 80., 100., 110.):
        t = time_to_reach(target, args.margin, tau, loss, ambient)
        print('  bed %5.0f °C: plate steady %5.1f °C, %4.1f min'
              % (target, steady_state(target, loss, ambient), t / 60))
    print('\n[virtual_bed_temp]\ntime_constant: %.0f\nloss: %.3f\nambient_temp: %.0f'
          % (tau, loss, ambient))


if __name__ == '__main__':
    main()
