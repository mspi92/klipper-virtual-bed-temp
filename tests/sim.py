"""Simulated heated bed for tests: two-node pad/plate physics with a PI
controller on the (noisy) pad thermistor.

Plate: C_plate dT/dt = G (T_pad - T) - h (T - T_amb), G = C_plate/tau,
h = loss*G — the plate equation virtual_bed_temp models. The pad node (own
heat capacity, underside loss) and the controller are not part of the model,
so fits against this data exercise the method rather than echo the model.
"""
import random
import threading
import time

C_PLATE, C_PAD = 2500., 150.  # J/K
WATTS, MAX_POWER = 750., 0.6
H_PAD = 0.5                   # W/K pad underside loss
KP, KI = 0.08, 0.004          # PI gains, PWM per °C (per °C·s)
SENSOR_NOISE = 0.15           # °C, gaussian


class BedPhysics:
    """One-second steps of pad/plate temperatures under a PI loop."""

    def __init__(self, tau, loss, ambient=22., seed=1):
        self.g = C_PLATE / tau
        self.h = loss * self.g
        self.ambient = ambient
        self.t_pad = self.t_plate = ambient
        self.target = self.pwm = self.integ = 0.
        self.measured = ambient
        self.rng = random.Random(seed)

    def step(self):
        self.measured = self.t_pad + self.rng.gauss(0., SENSOR_NOISE)
        if self.target:
            err = self.target - self.measured
            u = KP * err + self.integ
            if 0. < u < MAX_POWER:
                self.integ += KI * err
            self.pwm = min(max(u, 0.), MAX_POWER)
        else:
            self.pwm = 0.
        q = self.g * (self.t_pad - self.t_plate)
        self.t_pad += (self.pwm * WATTS - q
                       - H_PAD * (self.t_pad - self.ambient)) / C_PAD
        self.t_plate += (q - self.h * (self.t_plate - self.ambient)) / C_PLATE


def synthetic(tau, loss, ambient=22., target=80., n=1500, lead=100):
    """Moonraker-temperature-store-shaped heat-up: target set at sample
    `lead`; true plate trace under 'temperature_sensor plate_ref'."""
    bed = BedPhysics(tau, loss, ambient)
    temps, targets, powers, plate = [], [], [], []
    for i in range(n):
        bed.target = target if i >= lead else 0.
        bed.step()
        temps.append(bed.measured)
        targets.append(bed.target)
        powers.append(bed.pwm)
        plate.append(bed.t_plate)
    return {'heater_bed': {'temperatures': temps, 'targets': targets,
                           'powers': powers},
            'temperature_sensor plate_ref': {'temperatures': plate}}


class SimPrinter:
    """BedPhysics behind calibrate.py's backend interface, running `speed`
    times faster than real time. A virtual_bed_temp model (time_constant 300,
    loss 0, settable via SET_VIRTUAL_BED_TEMP) runs alongside. Emulates
    [idle_timeout]: heater off after idle_timeout s without a G-code command;
    SET_IDLE_TIMEOUT supported. ignore: G-code commands silently dropped."""

    def __init__(self, tau, loss, speed=200., ambient=22., ignore=()):
        self.bed = BedPhysics(tau, loss, ambient, seed=2)
        self.speed, self.ambient, self.ignore = speed, ambient, set(ignore)
        self.model = ambient
        self.model_tau, self.model_loss = 300., 0.
        self.idle_timeout, self.last_gcode = 300., 0.
        self.gcodes = []
        self.t0_real = time.monotonic()
        self.sim_t = 0.
        self.lock = threading.Lock()

    def _advance(self):
        goal = (time.monotonic() - self.t0_real) * self.speed
        while self.sim_t + 1. <= goal:
            self.sim_t += 1.
            if self.bed.target and self.sim_t - self.last_gcode > self.idle_timeout:
                self.bed.target = 0.
            self.bed.step()
            self.model += ((self.bed.measured - self.model)
                           - self.model_loss * (self.model - self.ambient)) / self.model_tau

    def info(self):
        return {'state': 'ready'}

    def objects(self):
        return ['heater_bed', 'virtual_bed_temp', 'print_stats', 'idle_timeout']

    def query(self, objects):
        with self.lock:
            self._advance()
            return {'heater_bed': {'temperature': self.bed.measured,
                                   'target': self.bed.target, 'power': self.bed.pwm},
                    'virtual_bed_temp': {'temperature': self.model,
                                         'time_constant': self.model_tau,
                                         'loss': self.model_loss},
                    'print_stats': {'state': 'standby'},
                    'idle_timeout': {'idle_timeout': self.idle_timeout}}

    def gcode(self, script):
        with self.lock:
            self._advance()
            words = script.split()
            if words[0] in self.ignore:
                return
            self.gcodes.append(script)
            self.last_gcode = self.sim_t
            if words[0] == 'M140':
                self.bed.target = float(words[1][1:])
            elif words[0] == 'SET_IDLE_TIMEOUT':
                self.idle_timeout = float(words[1].split('=')[1])
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
            return self.bed.t_plate

    def now(self):
        return (time.monotonic() - self.t0_real) * self.speed

    def sleep(self, s):
        time.sleep(s / self.speed)
