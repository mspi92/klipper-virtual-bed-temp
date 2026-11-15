"""fit_tau: calibration accuracy against the simulated bed."""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
sys.path.insert(0, HERE)
import fit_tau  # noqa: E402
from sim import synthetic  # noqa: E402

LEAD = 100


def heatup(tau, loss, minutes):
    """(pad, power, true plate) from t=0 (target set) for `minutes`."""
    d = synthetic(tau, loss, n=LEAD + minutes * 60, lead=LEAD)
    return (d['heater_bed']['temperatures'][LEAD:],
            d['heater_bed']['powers'][LEAD:],
            d['temperature_sensor plate_ref']['temperatures'][LEAD:])


class PowerFit(unittest.TestCase):
    def test_rate_from_power_alone(self):
        for tau, loss in ((250., 0.03), (450., 0.), (800., 0.02)):
            pad, pw, _ = heatup(tau, loss, 25)
            r = fit_tau.analyze(pad, pw, 80., [], 22., 22.)
            self.assertAlmostEqual(r['tau_eff'], tau / (1 + loss),
                                   delta=0.03 * tau, msg='tau %s' % tau)
            self.assertEqual(r['loss'], 0.)

    def test_too_short_raises(self):
        pad, pw, _ = heatup(450., 0., 4)
        with self.assertRaises(ValueError):
            fit_tau.analyze(pad, pw, 80., [], 22., 22.)


class WithReadings(unittest.TestCase):
    def test_15_min_with_noisy_ir(self):
        """15 min, 3 readings with 1 °C noise: model within 2 °C of the true
        plate over a separate 40 min heat-up, for every seed."""
        tau, loss = 450., 0.02
        pad, pw, true = heatup(tau, loss, 15)
        vpad, _, vtrue = heatup(tau, loss, 40)
        for seed in range(10):
            rng = random.Random(seed)
            ref = [(t, true[t] + rng.gauss(0., 1.)) for t in (300, 600, 870)]
            r = fit_tau.analyze(pad, pw, 80., ref, 22., 22.)
            self.assertAlmostEqual(r['tau'], tau, delta=0.06 * tau)
            model = fit_tau.simulate(vpad, r['tau'], r['loss'], 22., 22.)
            err = max(abs(m - t) for m, t in zip(model, vtrue))
            self.assertLess(err, 2., 'seed %d' % seed)

    def test_high_loss_open_printer(self):
        """Plate settling ~15 °C below an 80 °C pad (loss 0.36, as measured
        on an open Voron): loss recovered, no upper-bound warning."""
        tau, loss = 254., 0.356
        pad, pw, true = heatup(tau, loss, 15)
        ref = [(t, true[t]) for t in (300, 600, 880)]
        r = fit_tau.analyze(pad, pw, 80., ref, 22., 22.)
        self.assertAlmostEqual(r['loss'], loss, delta=0.03)
        self.assertAlmostEqual(r['tau'], tau, delta=0.05 * tau)
        self.assertFalse(any('upper bound' in w for w in r['warnings']))

    def test_cross_check_warns_on_bad_timing(self):
        pad, pw, true = heatup(450., 0.02, 20)
        # readings attributed to twice their real time: plate looks much slower
        ref = [(2 * t, true[t]) for t in (150, 300, 450, 600)]
        r = fit_tau.analyze(pad, pw, 80., ref, 22., 22.)
        self.assertTrue(any('disagree' in w for w in r['warnings']), r['warnings'])


class Helpers(unittest.TestCase):
    def test_steady_state_and_soak(self):
        self.assertAlmostEqual(fit_tau.steady_state(80., 0., 22.), 80.)
        self.assertAlmostEqual(fit_tau.steady_state(80., 1., 20.), 50.)
        # within 2 °C of a 58 °C rise: ln(58/2) time constants
        t = fit_tau.time_to_reach(80., 2., 300., 0., 22.)
        self.assertAlmostEqual(t, 300. * 3.3673, delta=1.)
        self.assertEqual(fit_tau.time_to_reach(23., 2., 300., 0., 22.), 0.)

    def test_find_start(self):
        self.assertEqual(fit_tau.find_start([0, 0, 80, 80, 0, 60]), 5)
        self.assertIsNone(fit_tau.find_start([0, 0]))


class Cli(unittest.TestCase):
    def test_load_and_csv(self):
        d = synthetic(450., 0.02, n=LEAD + 20 * 60, lead=LEAD)
        true = d['temperature_sensor plate_ref']['temperatures']
        with tempfile.TemporaryDirectory() as tmp:
            store, csv = os.path.join(tmp, 's.json'), os.path.join(tmp, 'ir.csv')
            with open(store, 'w') as f:
                json.dump(d, f)
            with open(csv, 'w') as f:
                f.write('# s,temp\n420,%.1f\n1100,%.1f\n'
                        % (true[LEAD + 420], true[LEAD + 1100]))
            out = subprocess.run(
                [sys.executable, '-B', os.path.join(HERE, '..', 'tools', 'fit_tau.py'),
                 '--load', store, '--csv', csv],
                capture_output=True, text=True, check=True).stdout
        tau = float(out.split('time_constant:')[1].split()[0])
        self.assertAlmostEqual(tau, 450., delta=25.)
        self.assertIn('[virtual_bed_temp]', out)


if __name__ == '__main__':
    unittest.main()
