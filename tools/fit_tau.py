#!/usr/bin/env python3
"""Calibrate [virtual_bed_temp] (time_constant, loss) from one recorded heat-up.

Data: Moonraker /server/temperature_store (1 sample/s, newest last, default
1200 s history). t=0 is the last sample where the heater target rises from
<=0 to >0, or --start-ago seconds before the newest sample.

Stage 1, heater power (no extra hardware): once the pad thermistor holds its
target, the heater supplies the heat flowing into the plate plus roughly
constant losses, so the power decays as
    P(t) = P_steady + A * exp(-k t),   k = (1 + loss) / time_constant
k is fitted after the pad first reaches target plus --skip seconds of
controller settling.

Stage 2, plate readings (optional, >= 2 recommended):
  --csv FILE   IR thermometer readings, lines "seconds,temp_c", seconds since
               t=0 (start a stopwatch with M140)
  --ref NAME   a Klipper sensor on the plate logged by Moonraker
The model is simulated over the recorded pad temperature with k fixed and
loss chosen to fit the readings. A fit from the readings alone is reported as
a cross-check. Without readings: loss = 0, time_constant = 1/k.

Plate starts at equilibrium with the pad at t=0 (or at the first reading taken
at t=0); that temperature is the ambient unless --ambient is given.

Stdlib only. Exit status 1 on missing/inconsistent data.
"""
import argparse
import json
import math
import random
import sys
import urllib.request

DT = 1.  # Moonraker temperature_store sample interval, s
LOSS_GRID = [i * 0.002 for i in range(151)]  # 0 .. 0.3
TAU_RANGE = (20., 5000.)
# Power data needed, in plate time constants: Monte-Carlo (IR sigma 1 °C)
# showed no accuracy gain beyond ~1.25 (≈ 15 min for a 450 s plate)
MIN_POWER_SPANS = 1.25


def fetch_store(url):
    """Moonraker temperature store: {name: {"temperatures": [...], ...}}."""
    with urllib.request.urlopen(url.rstrip('/') + '/server/temperature_store',
                                timeout=10) as resp:
        return json.load(resp)['result']


def find_start(targets):
    """Index of the last <=0 -> >0 target transition; None if absent."""
    start = None
    for i in range(1, len(targets)):
        if targets[i - 1] <= 0. < targets[i]:
            start = i
    return start


def first_reached(temps, target, tol=0.5):
    """Index of the first sample >= target - tol; None if never."""
    for i, t in enumerate(temps):
        if t >= target - tol:
            return i
    return None


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


def exp_fit(power, tau_eff):
    """Least-squares P = a + b*exp(-t/tau_eff) over power[0..]; (a, b, rms)."""
    n = len(power)
    e = [math.exp(-i * DT / tau_eff) for i in range(n)]
    se, see = sum(e), sum(x * x for x in e)
    sp, sep = sum(power), sum(x * p for x, p in zip(e, power))
    det = n * see - se * se
    if abs(det) < 1e-12:
        return sp / n, 0., float('inf')
    b = (n * sep - se * sp) / det
    a = (sp - b * se) / n
    res = math.sqrt(sum((a + b * x - p) ** 2 for x, p in zip(e, power)) / n)
    return a, b, res


def fit_power(power):
    """Effective plate time constant 1/k (s) from the decaying power trace.
    Returns (tau_eff, p_steady, amplitude, rms)."""
    tau_eff = golden(lambda t: exp_fit(power, t)[2], *TAU_RANGE)
    a, b, res = exp_fit(power, tau_eff)
    return tau_eff, a, b, res


def fit_loss_constrained(heater, ref, t_init, ambient, tau_eff):
    """Best loss on LOSS_GRID with time_constant = (1+loss)*tau_eff.
    Returns (tau, loss, rms)."""
    best = None
    for loss in LOSS_GRID:
        tau = (1 + loss) * tau_eff
        e = rms(simulate(heater, tau, loss, t_init, ambient), ref)
        if best is None or e < best[2]:
            best = (tau, loss, e)
    return best


def fit_free(heater, ref, t_init, ambient, fit_loss=True):
    """Best (tau, loss, rms) from plate readings alone."""
    best = None
    for loss in (LOSS_GRID[::5] if fit_loss else [0.]):
        def err(tau):
            return rms(simulate(heater, tau, loss, t_init, ambient), ref)
        tau = golden(err, *TAU_RANGE)
        e = err(tau)
        if best is None or e < best[2]:
            best = (tau, loss, e)
    return best


def analyze(heater, power, target, ref, t_init, ambient, skip=120,
            fit_loss=True):
    """Two-stage calibration of one heat-up.

    heater, power: 1 Hz pad temperature / heater PWM, index 0 = target set.
    ref: plate readings [(index, temp)], may be empty.
    Returns dict: reached (s until pad hit target), power_s (s of power data
    fitted), tau_eff, p_steady, amp, p_rms, tau, loss, ref_rms (None without
    readings), free ((tau, loss, rms) from readings alone, None with < 2
    readings or fit_loss off), warnings [str].
    Raises ValueError when the record is too short to fit.
    """
    warnings = []
    reached = first_reached(heater, target)
    if reached is None:
        raise ValueError('pad has not reached its target %.1f °C yet' % target)
    power = power[reached + skip:]
    if len(power) < 120:
        raise ValueError('only %d s of power data after pad reached target'
                         ' + %d s settling' % (max(len(power), 0), skip))
    tau_eff, p_ss, amp, p_rms = fit_power(power)
    if amp <= 0:
        warnings.append('power is not decaying — wrong window?'
                        ' check --skip/--start-ago')
    if len(power) * DT < MIN_POWER_SPANS * tau_eff:
        warnings.append('power record covers < %.2g time constants'
                        ' — rate still uncertain, record longer'
                        % MIN_POWER_SPANS)
    ref = [(i, t) for i, t in ref if 0 < i < len(heater)]
    free = None
    if ref and fit_loss:
        tau, loss, ref_rms = fit_loss_constrained(heater, ref, t_init,
                                                  ambient, tau_eff)
        if loss >= LOSS_GRID[-1]:
            warnings.append('loss hit its upper bound — readings far below'
                            ' the pad; check IR emissivity / timing')
        if len(ref) >= 2:
            free = fit_free(heater, ref, t_init, ambient)
            k_free = free[0] / (1 + free[1])
            if abs(k_free - tau_eff) / tau_eff > 0.25:
                warnings.append(
                    'readings and power disagree by > 25 %% on the plate rate'
                    ' (%.0f s vs %.0f s); recheck reading times'
                    % (k_free, tau_eff))
    else:
        tau, loss = tau_eff, 0.
        ref_rms = (rms(simulate(heater, tau, 0., t_init, ambient), ref)
                   if ref else None)
    return {'reached': reached, 'power_s': len(power), 'tau_eff': tau_eff,
            'p_steady': p_ss, 'amp': amp, 'p_rms': p_rms, 'tau': tau,
            'loss': loss, 'ref_rms': ref_rms, 'free': free,
            'warnings': warnings}


def soak_table(tau, loss, ambient, margin, targets=(60., 80., 100., 110.)):
    """Lines describing plate steady state and soak time per bed target."""
    lines = ['soak from %.0f °C until plate is within %.1f °C of steady state'
             ' (ideal heater step):' % (ambient, margin)]
    for tgt in targets:
        t = time_to_reach(tgt, margin, tau, loss, ambient)
        lines.append('  bed %5.0f °C: plate steady %5.1f °C, %4.1f min'
                     % (tgt, steady_state(tgt, loss, ambient), t / 60))
    return lines


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


def load_csv(path):
    """Manual readings as [(seconds index since t=0, temp)]."""
    ref = []
    with open(path) as f:
        for line in f:
            line = line.split('#')[0].strip()
            if not line:
                continue
            sec, temp = (float(x) for x in line.split(','))
            ref.append((int(round(sec / DT)), temp))
    return ref


def synthetic(tau, loss, ambient=22., target=80., n=1500, lead=100,
              watts=750., max_power=0.6, c_plate=2500., c_pad=150.):
    """Store-shaped data from a two-node pad/plate simulation with a PI
    controller on the pad thermistor; physics independent of the fitted model.
    Plate: C dT/dt = G (T_pad - T) - h (T - T_amb), G = C/tau, h = loss*G.
    Pad thermistor reading carries 0.15 °C gaussian noise (fixed seed), which
    makes the controller's power noisy like a real PID.
    Also returns the true plate trace under 'temperature_sensor plate_ref'."""
    rng = random.Random(1)
    g = c_plate / tau
    h = loss * g
    h_pad = 0.5  # W/K pad underside loss
    t_pad = t_plate = ambient
    integ = 0.
    temps, targets, powers, plate = [], [], [], []
    for i in range(n):
        tgt = target if i >= lead else 0.
        measured = t_pad + rng.gauss(0., 0.15)
        if tgt:
            err = tgt - measured
            u = 0.08 * err + integ
            if 0. < u < max_power:
                integ += 0.004 * err * DT
            pwm = min(max(u, 0.), max_power)
        else:
            pwm = 0.
        q_pp = g * (t_pad - t_plate)
        t_pad += (pwm * watts - q_pp - h_pad * (t_pad - ambient)) * DT / c_pad
        t_plate += (q_pp - h * (t_plate - ambient)) * DT / c_plate
        temps.append(measured)
        targets.append(tgt)
        powers.append(pwm)
        plate.append(t_plate)
    return {'heater_bed': {'temperatures': temps, 'targets': targets,
                           'powers': powers},
            'temperature_sensor plate_ref': {'temperatures': plate}}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--moonraker', metavar='URL',
                     help='e.g. http://mainsailos.local:7125')
    src.add_argument('--load', metavar='FILE', help='store JSON saved with --save')
    src.add_argument('--selftest', nargs=2, type=float, metavar=('TAU', 'LOSS'),
                     help='synthetic two-node heat-up with known TAU, LOSS; '
                          'combine with --csv to test IR readings')
    ap.add_argument('--save', metavar='FILE', help='write the store to FILE')
    ap.add_argument('--heater', default='heater_bed')
    ref_src = ap.add_mutually_exclusive_group()
    ref_src.add_argument('--csv', metavar='FILE')
    ref_src.add_argument('--ref', metavar='SENSOR')
    ap.add_argument('--start-ago', type=int, metavar='S',
                    help='t=0 is S seconds before the newest sample')
    ap.add_argument('--skip', type=int, default=120, metavar='S',
                    help='controller settling after the pad reaches target, '
                         'excluded from the power fit (default 120)')
    ap.add_argument('--ambient', type=float)
    ap.add_argument('--no-loss', action='store_true', help='fix loss at 0')
    ap.add_argument('--margin', type=float, default=2.,
                    help='WAIT_VIRTUAL_BED_TEMP margin for the soak table, °C')
    args = ap.parse_args()

    if args.selftest:
        store = synthetic(*args.selftest)
        print('selftest truth: time_constant %.0f, loss %.3f, k-time %.0f s'
              % (args.selftest[0], args.selftest[1],
                 args.selftest[0] / (1 + args.selftest[1])))
    elif args.load:
        with open(args.load) as f:
            store = json.load(f)
    else:
        store = fetch_store(args.moonraker)
    if args.save:
        with open(args.save, 'w') as f:
            json.dump(store, f)
    if args.heater not in store:
        sys.exit('error: %s not in store (have: %s)' % (args.heater, ', '.join(store)))

    hs = store[args.heater]
    temps = hs['temperatures']
    if args.start_ago is not None:
        start = len(temps) - 1 - args.start_ago
    else:
        start = find_start(hs.get('targets', []))
    if start is None or not 0 <= start < len(temps):
        sys.exit('error: heat-up start not found in store — pass --start-ago')
    heater = temps[start:]
    target = hs['targets'][start] if hs.get('targets') else max(heater)

    if 'powers' not in hs:
        sys.exit('error: store has no heater powers')
    power = hs['powers'][start:]

    # Sample before the target rise is still at equilibrium
    t_init = temps[start - 1] if start > 0 else heater[0]
    ref = []
    if args.csv:
        ref = load_csv(args.csv)
        if ref and ref[0][0] == 0:
            t_init = ref[0][1]
    elif args.ref:
        if args.ref not in store:
            sys.exit('error: %s not in store (have: %s)' % (args.ref, ', '.join(store)))
        plate = store[args.ref]['temperatures'][start:]
        ref = list(enumerate(plate))[::60]  # one reading per minute
        t_init = plate[0]
    ref = [(i, t) for i, t in ref if 0 < i < len(heater)]
    ambient = t_init if args.ambient is None else args.ambient

    try:
        r = analyze(heater, power, target, ref, t_init, ambient, args.skip,
                    not args.no_loss)
    except ValueError as e:
        sys.exit('error: %s' % e)
    tau, loss = r['tau'], r['loss']
    print('heat-up:       target %.0f °C, pad reached it after %d s, '
          'power fit over %d s' % (target, r['reached'], r['power_s']))
    print('power fit:     P = %.3f + %.3f·exp(−t/%.0f s), rms %.3f'
          % (r['p_steady'], r['amp'], r['tau_eff'], r['p_rms']))
    if r['ref_rms'] is None:
        print('readings:      none — loss fixed at 0')
    else:
        print('readings:      %d, rms vs model %.2f °C' % (len(ref), r['ref_rms']))
    if r['free']:
        print('cross-check:   readings alone → time_constant %.0f, loss %.3f'
              ' (rms %.2f °C)' % r['free'])
    for w in r['warnings']:
        print('warning: ' + w)

    print('\nstart temp:    %.1f °C   ambient: %.1f °C' % (t_init, ambient))
    print('time_constant: %.0f' % tau)
    print('loss:          %.3f' % loss)
    for name in store:
        if name.split()[0] == 'virtual_bed_temp' and ref:
            cur = store[name]['temperatures'][start:]
            print('current %s rms vs readings: %.2f °C' % (name, rms(cur, ref)))
    print()
    print('\n'.join(soak_table(tau, loss, ambient, args.margin)))
    print('\n[virtual_bed_temp]\ntime_constant: %.0f\nloss: %.3f\nambient_temp: %.0f'
          % (tau, loss, ambient))


if __name__ == '__main__':
    main()
