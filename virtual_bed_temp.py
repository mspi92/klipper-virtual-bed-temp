# Virtual bed-plate temperature sensor
#
# Estimates the build-surface temperature of a heated bed whose thermistor
# sits in the heater pad, using a first-order thermal lag model.
#
# Copyright (C) 2026  Marcel Spitz
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

UPDATE_INTERVAL = 1.


class VirtualBedTemp:
    """Plate temperature estimate driven by a heater's measured temperature.

    Model, integrated every UPDATE_INTERVAL seconds (explicit Euler, stable
    for time_constant >> UPDATE_INTERVAL):
        dT/dt = ((T_heater - T) - loss * (T - T_ambient)) / time_constant
    Steady state: T = (T_heater + loss * T_ambient) / (1 + loss).

    Registered as a temperature sensor under the config section name: shows
    up in heaters.available_sensors (Mainsail, Moonraker temperature store)
    and accepts TEMPERATURE_WAIT SENSOR=<section name>.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()
        self.heater_name = config.get('heater', 'heater_bed')
        self.time_constant = config.getfloat(
            'time_constant', 300., above=UPDATE_INTERVAL * 10.)
        self.loss = config.getfloat('loss', 0., minval=0., maxval=1.)
        self.ambient_temp = config.getfloat('ambient_temp', 22.)
        self.heater = None
        self.temp = 0.
        self.initialized = False
        pheaters = self.printer.load_object(config, 'heaters')
        pheaters.register_sensor(config, self)
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:ready',
                                            self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        # Unnamed section [virtual_bed_temp] also answers without SENSOR=
        mux_keys = [self.name.split()[-1]]
        if self.name == 'virtual_bed_temp':
            mux_keys.append(None)
        for key in mux_keys:
            gcode.register_mux_command(
                'SET_VIRTUAL_BED_TEMP', 'SENSOR', key,
                self.cmd_SET_VIRTUAL_BED_TEMP,
                desc=self.cmd_SET_VIRTUAL_BED_TEMP_help)
            gcode.register_mux_command(
                'WAIT_VIRTUAL_BED_TEMP', 'SENSOR', key,
                self.cmd_WAIT_VIRTUAL_BED_TEMP,
                desc=self.cmd_WAIT_VIRTUAL_BED_TEMP_help)

    def _handle_connect(self):
        pheaters = self.printer.lookup_object('heaters')
        try:
            self.heater = pheaters.lookup_heater(self.heater_name)
        except self.printer.config_error as e:
            raise self.printer.config_error(
                "[%s]: %s" % (self.name, str(e)))

    def _handle_ready(self):
        self.reactor.register_timer(self._update, self.reactor.NOW)

    def _update(self, eventtime):
        heater_temp = self.heater.get_temp(eventtime)[0]
        if not self.initialized:
            if heater_temp <= 0.:
                # Heater has not reported yet
                return eventtime + UPDATE_INTERVAL
            self.temp = heater_temp
            self.initialized = True
            logging.info("%s: initialised to %.1f", self.name, self.temp)
            return eventtime + UPDATE_INTERVAL
        drive = (heater_temp - self.temp
                 - self.loss * (self.temp - self.ambient_temp))
        self.temp += drive * UPDATE_INTERVAL / self.time_constant
        return eventtime + UPDATE_INTERVAL

    def get_temp(self, eventtime):
        return self.temp, 0.

    def stats(self, eventtime):
        return False, '%s: temp=%.1f' % (self.name, self.temp)

    def get_status(self, eventtime):
        heater_target = 0.
        if self.heater is not None:
            heater_target = self.heater.get_temp(eventtime)[1]
        return {
            'temperature': round(self.temp, 2),
            'time_constant': self.time_constant,
            'loss': self.loss,
            'ambient_temp': self.ambient_temp,
            # Plate equilibrium for the heater's current target (0 when off);
            # wait for this minus a margin, never for the heater target itself
            # when loss > 0, or the wait never finishes.
            'steady_state': round(self.steady_state(heater_target), 2)
                            if heater_target > 0. else 0.,
        }

    def steady_state(self, heater_temp):
        """Plate equilibrium temperature for a heater held at heater_temp."""
        return (heater_temp + self.loss * self.ambient_temp) / (1. + self.loss)

    cmd_SET_VIRTUAL_BED_TEMP_help = (
        "Set virtual bed temperature model parameters or reset its state")

    def cmd_SET_VIRTUAL_BED_TEMP(self, gcmd):
        """TIME_CONSTANT= (s), LOSS= (0..1), TEMP= (reset estimate, °C).

        Changes are runtime-only; persist them in the config section.
        Always responds with the resulting parameters.
        """
        self.time_constant = gcmd.get_float(
            'TIME_CONSTANT', self.time_constant, above=UPDATE_INTERVAL * 10.)
        self.loss = gcmd.get_float('LOSS', self.loss, minval=0., maxval=1.)
        temp = gcmd.get_float('TEMP', None)
        if temp is not None:
            self.temp = temp
            self.initialized = True
        gcmd.respond_info(
            "%s: temp=%.1f time_constant=%.0f loss=%.3f ambient_temp=%.1f"
            % (self.name, self.temp, self.time_constant, self.loss,
               self.ambient_temp))

    cmd_WAIT_VIRTUAL_BED_TEMP_help = (
        "Wait until the estimated plate temperature is within MARGIN of its"
        " steady state for the heater's current target")

    def cmd_WAIT_VIRTUAL_BED_TEMP(self, gcmd):
        """MARGIN= (°C, default 2, >0).

        Goal is steady_state(heater target) - MARGIN, so it is reachable for
        any loss. Returns immediately if the heater is off. Blocks the G-code
        queue like M190 (only an emergency stop interrupts it). Reports
        progress every 60 s.
        """
        margin = gcmd.get_float('MARGIN', 2., above=0.)
        if self.printer.get_start_args().get('debugoutput') is not None:
            return
        reactor = self.reactor
        eventtime = reactor.monotonic()
        target = self.heater.get_temp(eventtime)[1]
        if target <= 0.:
            gcmd.respond_info("%s: %s is off, not waiting"
                              % (self.name, self.heater_name))
            return
        goal = self.steady_state(target) - margin
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.get_last_move_time()
        last_report = None
        while not self.printer.is_shutdown() and self.temp < goal:
            if last_report is None or eventtime - last_report >= 60.:
                gcmd.respond_info("%s: plate %.1f / %.1f"
                                  % (self.name, self.temp, goal))
                last_report = eventtime
            eventtime = reactor.pause(eventtime + 1.)
        gcmd.respond_info("%s: plate %.1f reached %.1f"
                          % (self.name, self.temp, goal))


def load_config(config):
    return VirtualBedTemp(config)


def load_config_prefix(config):
    return VirtualBedTemp(config)
