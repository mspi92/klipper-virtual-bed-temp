"""calibrate.py end to end against a time-accelerated simulated printer."""
import contextlib
import glob
import io
import os
import random
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
sys.path.insert(0, HERE)
import calibrate  # noqa: E402
import fit_tau  # noqa: E402
from sim import SimPrinter  # noqa: E402


def run(printer, tmp):
    """Unattended run; readings = true plate ± 0.5 °C. Returns (session or
    SystemExit, captured output)."""
    rng = random.Random(0)
    args = calibrate.build_parser().parse_args(['--save-dir', tmp])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            res = calibrate.run_session(
                args, printer, lambda: printer.plate_truth() + rng.uniform(-.5, .5))
        except SystemExit as e:
            res = e
    return res, out.getvalue()


class EndToEnd(unittest.TestCase):
    def test_full_run(self):
        printer = SimPrinter(450., 0.02, speed=300.)
        with tempfile.TemporaryDirectory() as tmp:
            s, out = run(printer, tmp)
            self.assertIsInstance(s, calibrate.Session, out)
            tau = float(out.split('Recommended:  time_constant')[1].split()[0])
            # analysis is pinned to the measurement end, not to later samples
            self.assertEqual(round(s.analyze()['tau']), tau)
            self.assertAlmostEqual(tau, 450., delta=25., msg=out)
            # applied to the running model, heater off, idle timeout restored
            self.assertEqual(printer.model_tau, tau)
            g = printer.gcodes
            self.assertTrue(g[0].startswith('SET_IDLE_TIMEOUT TIMEOUT=5400'), g)
            self.assertEqual(g[1], 'M140 S80')
            self.assertIn('M140 S0', g)
            self.assertEqual(g[-1], 'SET_IDLE_TIMEOUT TIMEOUT=300')
            self.assertEqual(printer.bed.target, 0.)
            # run length adapts: ~15 min for a 450 s plate
            self.assertLess(s.readings[-1][0], 20 * 60, out)
            # saved session ends at the measurement end and re-fits the same
            js = glob.glob(os.path.join(tmp, 'calib-*.json'))
            self.assertEqual(len(js), 1)
            refit = subprocess.run(
                [sys.executable, '-B', os.path.join(HERE, '..', 'tools', 'fit_tau.py'),
                 '--load', js[0], '--csv', js[0][:-5] + '.csv'],
                capture_output=True, text=True, check=True).stdout
            self.assertAlmostEqual(
                float(refit.split('time_constant:')[1].split()[0]), tau, delta=5.)

    def test_external_heater_off_aborts(self):
        """Idle timeout fires because SET_IDLE_TIMEOUT is dropped: run aborts
        with exit status 1, data saved, no further heating."""
        printer = SimPrinter(450., 0., speed=300., ignore={'SET_IDLE_TIMEOUT'})
        with tempfile.TemporaryDirectory() as tmp:
            res, out = run(printer, tmp)
            self.assertIsInstance(res, SystemExit, out)
            self.assertEqual(res.code, 1)
            self.assertIn('outside this script', out)
            self.assertEqual(printer.bed.target, 0.)
            self.assertEqual(len(glob.glob(os.path.join(tmp, 'calib-*.json'))), 1)


class Recorder(unittest.TestCase):
    def test_series_resamples_to_1hz(self):
        rec = calibrate.Recorder(None, 'heater_bed', False)
        rec.samples = [(10.0, 20., 0., 0., None), (11.4, 21., 80., .6, None),
                       (12.1, 22., 80., .5, None), (14.0, 24., 80., .4, None)]
        pad, power, _ = rec.series(10.)
        # each second takes the latest sample at or before it
        self.assertEqual(pad, [20., 20., 21., 22., 24.])
        self.assertEqual(power, [0., 0., .6, .5, .4])

    def test_slope(self):
        self.assertAlmostEqual(calibrate.slope([1., 2., 3., 4.]), 1.)
        self.assertEqual(calibrate.slope([5., 5.]), 0.)


if __name__ == '__main__':
    unittest.main()
