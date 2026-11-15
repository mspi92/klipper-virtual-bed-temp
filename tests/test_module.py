"""virtual_bed_temp.py driven by minimal stand-ins for Klipper's printer,
reactor, heater and G-code objects (no Klipper checkout needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import virtual_bed_temp as vbt  # noqa: E402


class ConfigError(Exception):
    pass


class Heater:
    """Pad approaching its target with a 20 s first-order response."""

    def __init__(self):
        self.temp, self.target = 22., 0.

    def get_temp(self, eventtime):
        return self.temp, self.target

    def step(self):
        self.temp += ((self.target or 22.) - self.temp) / 20.


class Reactor:
    NOW = 0.

    def __init__(self, heater):
        self.t, self.timers, self.heater = 0., [], heater

    def register_timer(self, cb, when):
        self.timers.append([cb, when])

    def monotonic(self):
        return self.t

    def pause(self, until):
        while self.t < until:
            self.t += 1.
            self.heater.step()
            for tm in self.timers:
                if tm[1] <= self.t:
                    tm[1] = tm[0](self.t)
        return self.t


class GCode:
    def __init__(self):
        self.mux = {}

    def register_mux_command(self, cmd, key, value, func, desc=None):
        self.mux[(cmd, value)] = func


class Heaters:
    def __init__(self, heater):
        self.heater, self.sensors = heater, []

    def register_sensor(self, config, sensor):
        self.sensors.append(config.get_name())

    def lookup_heater(self, name):
        if name != 'heater_bed':
            raise ConfigError("Unknown heater '%s'" % name)
        return self.heater


class Toolhead:
    def get_last_move_time(self):
        return 0.


class Printer:
    config_error = ConfigError

    def __init__(self):
        self.heater = Heater()
        self.reactor = Reactor(self.heater)
        self.gcode = GCode()
        self.heaters = Heaters(self.heater)
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def load_object(self, config, name):
        return self.heaters

    def lookup_object(self, name):
        return {'gcode': self.gcode, 'heaters': self.heaters,
                'toolhead': Toolhead()}[name]

    def register_event_handler(self, event, cb):
        self.handlers[event] = cb

    def get_start_args(self):
        return {}

    def is_shutdown(self):
        return False


class Config:
    def __init__(self, printer, opts, name='virtual_bed_temp'):
        self.printer, self.opts, self.name = printer, opts, name

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def get(self, key, default):
        return self.opts.get(key, default)

    def getfloat(self, key, default, **kw):
        return float(self.opts.get(key, default))


class GCmd:
    def __init__(self, **params):
        self.params, self.messages = params, []

    def get_float(self, key, default, **kw):
        return float(self.params[key]) if key in self.params else default

    def respond_info(self, msg):
        self.messages.append(msg)


def make(opts=None, name='virtual_bed_temp'):
    p = Printer()
    s = vbt.VirtualBedTemp(Config(p, opts or {}, name))
    p.handlers['klippy:connect']()
    p.handlers['klippy:ready']()
    p.reactor.pause(3)
    return p, s


class Module(unittest.TestCase):
    def test_registers_and_initialises_to_heater(self):
        p, s = make()
        self.assertEqual(p.heaters.sensors, ['virtual_bed_temp'])
        self.assertAlmostEqual(s.get_temp(0)[0], 22.)
        self.assertEqual(s.get_status(0)['steady_state'], 0.)

    def test_lags_heater_by_time_constant(self):
        p, s = make({'time_constant': 300})
        p.heater.temp = p.heater.target = 80.   # ideal step
        p.heater.step = lambda: None
        p.reactor.pause(p.reactor.t + 300)
        # first-order response: 1 - 1/e of the step after one time constant
        self.assertAlmostEqual(s.temp, 22. + 58. * (1 - 0.3679), delta=0.5)

    def test_wait_reaches_steady_state_minus_margin(self):
        p, s = make({'time_constant': 300, 'loss': 0.05})
        off = GCmd()
        p.gcode.mux[('WAIT_VIRTUAL_BED_TEMP', None)](off)
        self.assertIn('off, not waiting', off.messages[0])
        p.heater.target = 80.
        steady = s.get_status(0)['steady_state']
        self.assertAlmostEqual(steady, (80. + 0.05 * 22.) / 1.05, places=2)
        p.gcode.mux[('WAIT_VIRTUAL_BED_TEMP', 'virtual_bed_temp')](GCmd(MARGIN=2))
        self.assertGreaterEqual(s.temp, steady - 2.)
        self.assertLess(s.temp, steady - 1.5)

    def test_wait_stops_when_heater_turned_off(self):
        p, s = make({'time_constant': 300})
        p.heater.target = 80.
        p.reactor.register_timer(
            lambda t: setattr(p.heater, 'target', 0.) or 1e9, 60.)
        g = GCmd()
        p.gcode.mux[('WAIT_VIRTUAL_BED_TEMP', None)](g)
        self.assertIn('turned off', g.messages[-1])
        self.assertLess(p.reactor.t, 70.)

    def test_set_command(self):
        p, s = make()
        g = GCmd(TIME_CONSTANT=500, LOSS=0.03, TEMP=40)
        p.gcode.mux[('SET_VIRTUAL_BED_TEMP', None)](g)
        self.assertEqual((s.time_constant, s.loss, s.temp), (500., 0.03, 40.))
        self.assertIn('time_constant=500', g.messages[0])

    def test_named_instance(self):
        p, s = make(name='virtual_bed_temp zone2')
        self.assertIn(('SET_VIRTUAL_BED_TEMP', 'zone2'), p.gcode.mux)
        self.assertNotIn(('SET_VIRTUAL_BED_TEMP', None), p.gcode.mux)

    def test_unknown_heater(self):
        p = Printer()
        vbt.VirtualBedTemp(Config(p, {'heater': 'nope'}))
        with self.assertRaises(ConfigError):
            p.handlers['klippy:connect']()


if __name__ == '__main__':
    unittest.main()
