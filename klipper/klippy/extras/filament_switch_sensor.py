# Generic Filament Sensor Module
#
# Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import time
import threading

class RunoutHelper:
    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        # Read config
        self.runout_pause = config.getboolean('pause_on_runout', True)
        if self.runout_pause:
            self.printer.load_object(config, 'pause_resume')
        self.runout_gcode = self.insert_gcode = None
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        if self.runout_pause or config.get('runout_gcode', None) is not None:
            self.runout_gcode = gcode_macro.load_template(
                config, 'runout_gcode', '')
        if config.get('insert_gcode', None) is not None:
            self.insert_gcode = gcode_macro.load_template(
                config, 'insert_gcode')
        self.on_disable_gcode = None
        if config.get('on_disable_gcode', None) is not None:
            self.on_disable_gcode = gcode_macro.load_template(
                config, 'on_disable_gcode')
        self.switch_off_gcode = None
        if config.get('switch_off_gcode', None) is not None:
            self.switch_off_gcode = gcode_macro.load_template(
                config, 'switch_off_gcode')
        self.pause_delay = config.getfloat('pause_delay', .5, above=.0)
        self.event_delay = config.getfloat('event_delay', 3., above=0.)
        # Internal state
        self.min_event_systime = self.reactor.NEVER
        self.filament_present = False
        self.sensor_enabled = True
        # Register commands and event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.gcode.register_mux_command(
            "QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
            self.cmd_QUERY_FILAMENT_SENSOR,
            desc=self.cmd_QUERY_FILAMENT_SENSOR_help)
        self.gcode.register_mux_command(
            "SET_FILAMENT_SENSOR", "SENSOR", self.name,
            self.cmd_SET_FILAMENT_SENSOR,
            desc=self.cmd_SET_FILAMENT_SENSOR_help)
    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.
    def _runout_event_handler(self, eventtime):
        # Pausing from inside an event requires that the pause portion
        # of pause_resume execute immediately.
        pause_prefix = ""
        if self.runout_pause:
            pause_resume = self.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            pause_prefix = "PAUSE\n"
            self.printer.get_reactor().pause(eventtime + self.pause_delay)
        self._exec_gcode(pause_prefix, self.runout_gcode)
    def _insert_event_handler(self, eventtime):
        self._exec_gcode("", self.insert_gcode)
    def _on_disable_handler(self, eventtime):
        self._exec_gcode("", self.on_disable_gcode)
    def _switch_off_handler(self, eventtime):
        self._exec_gcode("", self.switch_off_gcode)
    def _exec_gcode(self, prefix, template):
        try:
            if template is not None:
                self.gcode.run_script(prefix + template.render() + "\n")
        except Exception:
            logging.exception("Script running error")
        self.min_event_systime = self.reactor.monotonic() + self.event_delay
    def note_filament_present(self, et, is_filament_present, lazy=True):
        if lazy and is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()
        if eventtime < self.min_event_systime:
            # do not process during the initialization time, duplicates,
            # during the event delay time, while an event is running
            return
        # when the sensor is disabled
        if not self.sensor_enabled:
            if self.on_disable_gcode is not None:
                self.min_event_systime = self.reactor.NEVER
                logging.info(
                    "Filament Sensor %s: do sensor_disable gcode, Time %.2f" %
                    (self.name, eventtime))
                self.reactor.register_callback(self._on_disable_handler)
            return
        # Determine "printing" status
        idle_timeout = self.printer.lookup_object("idle_timeout")
        is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"
        # Perform filament action associated with status change (if any)
        if is_filament_present:
            if self.insert_gcode is not None:
                # insert detected
                self.min_event_systime = self.reactor.NEVER
                logging.info(
                    "Filament Sensor %s: insert event detected, Time %.2f" %
                    (self.name, eventtime))
                self.reactor.register_callback(self._insert_event_handler)
        elif is_printing and self.runout_gcode is not None:
            # runout detected
            self.min_event_systime = self.reactor.NEVER
            logging.info(
                "Filament Sensor %s: runout event detected, Time %.2f" %
                (self.name, eventtime))
            self.reactor.register_callback(self._runout_event_handler)
    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled)}
    # Check if a pause is needed;
    def check_to_pause(self, need_pause=True):
        if self.sensor_enabled and not self.filament_present:
            if need_pause:
                self.gcode.run_script("PAUSE")
                self.gcode.respond_raw("Filament Runout Detected!")
                logging.warning("Filament Runout Detected!")
            return True
        return False
    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"
    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Filament Sensor %s: filament detected" % (self.name)
        else:
            msg = "Filament Sensor %s: filament not detected" % (self.name)
        gcmd.respond_info(msg)
    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"
    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)
        if not bool(self.sensor_enabled):
            self.reactor.register_callback(self._switch_off_handler)
        else:
            eventtime = self.reactor.monotonic()
            self.note_filament_present(eventtime,self.filament_present,lazy=False)

class RunoutDebounceHelper:
    DEBOUNCE_INTERVAL = 0.2
    DEBOUNCE_COUNT = 5

    def __init__(self, config):
        self.runout_helper = RunoutHelper(config)
        self.lock = threading.Lock()
        self.debounce_count = 0
        self.runout_state = False
        self.locate_printing_gcode = None
        self.runout_helper.gcode.register_command("F108", self.cmd_F108, desc=self.cmd_F108_help)
        self.runout_helper.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.check_to_pause = self.runout_helper.check_to_pause

    def _handle_ready(self):
        self.locate_printing_gcode = self.runout_helper.printer.lookup_object('locate_printing_gcode', None)

    cmd_F108_help = "Recalculate the starting position for resuming printing"
    def cmd_F108(self, gcmd):
        if self.runout_state:
            if self.locate_printing_gcode is not None:
                self.locate_printing_gcode.restore_runing_info('RunoutDebounceHelper')

    def _runout_check_thread(self, eventtime):
        # 启动一个线程，避免阻塞其他线程，也避免调用delayed_gcode造成的排队延迟
        t = threading.Thread(target=self.thread_debounce, args=(eventtime,))
        t.start()

    def _runout_event_handler(self, eventtime):
        self.runout_state = True
        self.runout_helper._runout_event_handler(eventtime)
        self.runout_state = False
    def _exec_gcode(self, cmd):
        try:
            self.runout_helper.gcode.run_script(cmd)
        except Exception:
            logging.exception("Script running error")
    # 消抖线程；
    def thread_debounce(self, eventtime):
        if self.lock.acquire(blocking=False):
            while True:
                # 如果又检测到料，则判断为误判，取消后续动作
                if self.runout_helper.filament_present:
                    logging.info("touch by mistake, no shortage of filament")

                    self.runout_helper.reactor.register_async_callback((lambda e, s=self, d=self.debounce_count:
                                                                        s._exec_gcode(
                                                                            f"M117 not filament runout!")
                                                                        ))
                    self.runout_helper.min_event_systime = self.runout_helper.reactor.monotonic() + self.runout_helper.event_delay
                    break
                else:
                    # 检测到缺料，则定期发送状态消息
                    logging.info("RunoutDebounceHelper trigger,debounce_count=%d" % self.debounce_count)
                    self.runout_helper.reactor.register_async_callback((lambda e, s=self, d=self.debounce_count:
                                                                        s._exec_gcode(
                                                                            f"M117 now num is {d}")
                                                                        ))
                    # 达到检测次数，则调用断料执行脚本
                    if self.debounce_count >= self.DEBOUNCE_COUNT:
                        self.runout_helper.reactor.register_async_callback(
                            self._runout_event_handler)
                        break

                    self.debounce_count += 1
                    time.sleep(self.DEBOUNCE_INTERVAL)

            self.debounce_count = 0
            logging.info("RunoutDebounceHelper debounce thread is finished")
            self.lock.release()
        else:
            logging.info("RunoutDebounceHelper debounce thread is running")

    def note_filament_present(self, et, is_filament_present, lazy=True):
        if lazy and is_filament_present == self.runout_helper.filament_present:
            logging.info(f"[RunoutDebounceHelper]note_filament_present({et}, {is_filament_present}): is_filament_present is equal")
            return
        self.runout_helper.filament_present = is_filament_present
        eventtime = self.runout_helper.reactor.monotonic()
        if eventtime < self.runout_helper.min_event_systime:
            logging.info(f"[RunoutDebounceHelper]note_filament_present:{eventtime} < {self.runout_helper.min_event_systime} or not {self.runout_helper.sensor_enabled}")
            return
        # when the sensor is disabled
        if not self.runout_helper.sensor_enabled:
            if self.runout_helper.on_disable_gcode is not None:
                self.runout_helper.min_event_systime = self.reactor.NEVER
                logging.info(
                    "Filament Sensor %s: do sensor_disable gcode, Time %.2f" %
                    (self.runout_helper.name, eventtime))
                self.runout_helper.reactor.register_callback(self.runout_helper._on_disable_handler)
            return
        # 检查是不是 "printing" 状态
        idle_timeout = self.runout_helper.printer.lookup_object("idle_timeout")
        idle_state = idle_timeout.get_status(eventtime)
        is_printing = idle_state["state"] == "Printing"
        logging.info(f"[RunoutDebounceHelper]note_filament_present({et}, {is_filament_present}), idle_state:{idle_state}")
        # 执行与断料状态变化有关的动作（如断料或者插料脚本）
        if is_filament_present:
            if self.runout_helper.insert_gcode is not None:
                # 检测到插料动作
                self.runout_helper.min_event_systime = self.runout_helper.reactor.NEVER
                logging.info("Filament Sensor %s: insert event detected, Time %.2f" % (self.runout_helper.name, eventtime))
                self.runout_helper.reactor.register_callback(self.runout_helper._insert_event_handler)
        elif is_printing and self.runout_helper.runout_gcode is not None:
            if self.locate_printing_gcode is not None:
                self.locate_printing_gcode.save_runing_info('RunoutDebounceHelper', et)
            # 检测到断料动作
            self.runout_helper.min_event_systime = self.runout_helper.reactor.NEVER
            logging.info("Filament Sensor %s: runout event detected, Time %.2f" % (self.runout_helper.name, eventtime))
            self.runout_helper.reactor.register_callback(self._runout_check_thread)

    def get_status(self, eventtime):
        res = dict(self.runout_helper.get_status(eventtime))
        res.update({'debounce_count': self.debounce_count})
        return res

class SwitchSensor:
    def __init__(self, config):
        printer = config.get_printer()
        buttons = printer.load_object(config, 'buttons')
        switch_pin = config.get('switch_pin')
        # 添加helper的配置解析
        helper = config.get('helper', "")
        buttons.register_buttons([switch_pin], self._button_handler)
        if helper == "RunoutDebounceHelper":
            self.runout_helper = RunoutDebounceHelper(config)
        else:
            self.runout_helper = RunoutHelper(config)
        self.get_status = self.runout_helper.get_status
    def _button_handler(self, eventtime, state):
        self.runout_helper.note_filament_present(eventtime, state)

def load_config_prefix(config):
    return SwitchSensor(config)

