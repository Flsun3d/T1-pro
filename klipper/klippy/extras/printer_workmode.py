# Copyright (c) 2024,郑州潮阔电子科技有限公司
# All rights reserved.
# 
# 文件名称：printer_workmode.py
# 摘    要：添加打印机工作模式模块
# 
# 当前版本：1.0
# 作    者：hzk
# 完成日期：2025年7月14日
#
# 修订记录：

from . import bus, tmc
import logging
import stepper

TRINAMIC_DRIVERS = ["tmc2130", "tmc2208", "tmc2209", "tmc2240", "tmc2660",
    "tmc5160"]

TMC_FREQUENCY=12000000.
FAN_MIN_TIME = 0.100
PIN_MIN_TIME = 0.100
MAX_CURRENT = 4.000

class PrinterWorkMode:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        
        self.work_mode = 0 #正常模式

        # Register commands
        self.gcode = self.printer.lookup_object('gcode')

        # Checking for dependent modules 
        self.extruder = None
        self.v_sd = self._lookup_required_module('virtual_sdcard')
        self.save_variables = self._lookup_required_module('save_variables')
        self.heater_bed = self._lookup_required_module('heater_bed')
        self.print_stats = self._lookup_required_module('print_stats')
        self.gcode_move = self._lookup_required_module('gcode_move')
        self.fan = self._lookup_required_module('fan')
        self.exclude_object = self._lookup_required_module('exclude_object')

        #get config value
        self.silent_max_velocity = config.getfloat('silent_max_velocity', 300)
        self.silent_max_accel = config.getfloat('silent_max_accel', 6000)
        self.silent_max_accel_to_decel = config.getfloat('silent_max_accel_to_decel', 2000)
        self.silent_square_corner_velocity = config.getfloat('silent_square_corner_velocity', 5)

        self.silent_stealthchop = config.getfloat('silent_stealthchop', 500)
        self.silent_extruder_run_current = config.getfloat('silent_extruder_run_current', 0.8)
        self.silent_step_abc_run_current = config.getfloat('silent_step_abc_run_current', 2.0)
        self.silent_fan_max_power = config.getfloat('silent_fan_max_power', 0.3)
        self.silent_heater_fan_heat_sink_fan_fan_speed = config.getfloat('silent_heater_fan_heat_sink_fan_fan_speed', 0.5)

        #空驶速度
        self.pace_speed = config.getfloat('pace_speed', 100)
        
        # register commands
        self.gcode.register_command("PRINTER_WORKMODE", self.cmd_PRINTER_WORKMODE, desc=self.cmd_PRINTER_WORKMODE_help)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        
    def _lookup_required_module(self, module_name):
        module = self.printer.lookup_object(module_name, None)
        if module is None:
            raise self.gcode.error(f"not found {module_name} module_name)")
        return module

    def get_config(self, setion_name):
        for section_config in config.get_prefix_sections(''):
            self.load_object(config, section_config.get_name(), None)

    def get_workmode(self):
        return self.work_mode

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.extruder = self.printer.lookup_object('extruder', None)
        if self.extruder is None:
            raise self.gcode.error("not found extruder module")
        self.kin = kin = self.toolhead.get_kinematics()
        for s in kin.get_steppers():
            if s._name == 'stepper_a':
                self.stepper_a = s
            elif s._name == 'stepper_b':
                self.stepper_b = s
            elif s._name == 'stepper_c':
                self.stepper_c = s
            else:
                logging.info(f"hzk123 unknown steppers [{s._name}]")
        

        self.tmc5160_stepper_a = self.printer.lookup_object('tmc5160 stepper_a')
        if self.tmc5160_stepper_a is None:
            raise self.gcode.error("not found stepper_a module")
        self.tmc5160_stepper_b = self.printer.lookup_object('tmc5160 stepper_b')
        if self.tmc5160_stepper_b is None:
            raise self.gcode.error("not found stepper_b module")
        self.tmc5160_stepper_c = self.printer.lookup_object('tmc5160 stepper_a')
        if self.tmc5160_stepper_c is None:
            raise self.gcode.error("not found stepper_c module")
        self.tmc5160_stepper_extruder = self.printer.lookup_object('tmc5160 extruder')
        if self.tmc5160_stepper_extruder is None:
            raise self.gcode.error("not found stepper_extruder module")

        printer_fan = self.printer.lookup_object('fan')
        self.fan = printer_fan.fan      #涡轮风扇
        self.heater_fan_heat_sink_fan = self.printer.lookup_object('heater_fan heat_sink_fan') #效应器风扇

        self.toolhead = self.printer.lookup_object('toolhead')

        #get normal value
        pconfig = self.printer.lookup_object('configfile')
        gconfig = pconfig.read_main_config()
        config = gconfig.getsection('tmc5160 stepper_a')
        self.normal_stepper_a_stealthchop_threshold = config.getfloat('stealthchop_threshold', 0., minval=0.)
        self.normal_stepper_a_run_current = config.getfloat('run_current',above=0., maxval=MAX_CURRENT)
        config = gconfig.getsection('tmc5160 stepper_b')
        self.normal_stepper_b_stealthchop_threshold = config.getfloat('stealthchop_threshold', 0., minval=0.)
        self.normal_stepper_b_run_current = config.getfloat('run_current',above=0., maxval=MAX_CURRENT)
        config = gconfig.getsection('tmc5160 stepper_c')
        self.normal_stepper_c_stealthchop_threshold = config.getfloat('stealthchop_threshold', 0., minval=0.)
        self.normal_stepper_c_run_current = config.getfloat('run_current',above=0., maxval=MAX_CURRENT)

        config = gconfig.getsection('tmc5160 extruder')
        self.normal_extruder_run_current = config.getfloat('run_current', minval=0.1,maxval=2.4)
        
        #涡轮风扇
        config = gconfig.getsection('fan')
        self.normal_fan_max_power = config.getfloat('max_power', 1., above=0., maxval=1.)

        #效应器风扇
        config = gconfig.getsection('heater_fan heat_sink_fan')
        self.normal_heat_sink_fan_max_power = config.getfloat("fan_speed", 1., minval=0., maxval=1.)

        config = gconfig.getsection('printer')
        self.normal_max_velocity = config.getfloat('max_velocity', above=0.)
        self.normal_max_accel = config.getfloat('max_accel', above=0.)
        self.normal_max_accel_to_decel = config.getfloat('max_accel_to_decel', self.normal_max_accel * 0.5, above=0.)
        self.normal_square_corner_velocity = config.getfloat('square_corner_velocity', 5., minval=0.)
        self.normal_max_z_velocity = config.getfloat('max_z_velocity', above=0.)


    #set "stealthchop" mode
    def set_stealthchop(self, stepperx, mcu_tmc, tmc_freq, stealthchop):
        fields = mcu_tmc.get_fields()
        en_pwm_mode = False
        velocity = stealthchop
        print_time = self.toolhead.get_last_move_time()
        if velocity:
            rotation_dist = stepperx._rotation_dist
            steps_per_rotation = stepperx._steps_per_rotation
            step_dist = rotation_dist / steps_per_rotation
            step_dist_256 = step_dist / (1 << fields.get_field("mres"))
            threshold = int(tmc_freq * step_dist_256 / velocity + .5)

            reg_val = fields.set_field("tpwmthrs", max(0, min(0xfffff, threshold)))
            reg = fields.lookup_register("tpwmthrs", None)
            mcu_tmc.set_register(reg, reg_val, print_time)
            
            en_pwm_mode = True
        reg = fields.lookup_register("en_pwm_mode", None)
        if reg is not None:
            reg_val = fields.set_field("en_pwm_mode", en_pwm_mode)
            mcu_tmc.set_register(reg, reg_val, print_time)
        else:
            # TMC2208 uses en_spreadCycle
            reg_val = fields.set_field("en_spreadcycle", not en_pwm_mode)
        
            reg = fields.lookup_register("en_spreadcycle", None)
            mcu_tmc.set_register(reg, reg_val, print_time)

    def switch_to_normal(self, pre_work_mode):
        logging.info("hzk123 switch_to_normal")
        #1 set "stealthchop" mode
        self.set_stealthchop(self.stepper_a, self.tmc5160_stepper_a.mcu_tmc, TMC_FREQUENCY, self.normal_stepper_a_stealthchop_threshold)
        self.set_stealthchop(self.stepper_b, self.tmc5160_stepper_b.mcu_tmc, TMC_FREQUENCY, self.normal_stepper_b_stealthchop_threshold)
        self.set_stealthchop(self.stepper_c, self.tmc5160_stepper_c.mcu_tmc, TMC_FREQUENCY, self.normal_stepper_c_stealthchop_threshold)

        #2 set extruder run_current 挤出机运行电流
        current_str = "{0:.1f}".format(self.normal_extruder_run_current)
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=extruder CURRENT={self.normal_extruder_run_current:.1f}")
        #a,b,c电机电流
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=stepper_a CURRENT={self.normal_stepper_a_run_current:.1f}")
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=stepper_b CURRENT={self.normal_stepper_b_run_current:.1f}")
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=stepper_c CURRENT={self.normal_stepper_c_run_current:.1f}")

        #3 set fan max_power 涡轮风扇
        self.fan.max_power = self.normal_fan_max_power
        curtime = self.printer.get_reactor().monotonic()
        print_time = self.fan.get_mcu().estimated_print_time(curtime)
        print_time = print_time + PIN_MIN_TIME
        self.fan.set_speed(print_time, self.fan.cur_set_speed)

        #4 效应器风扇
        self.heater_fan_heat_sink_fan.fan_speed = self.normal_heat_sink_fan_max_power
        curtime = self.printer.get_reactor().monotonic()
        self.heater_fan_heat_sink_fan.callback(curtime)

        #5 速度加速度
        #max_velocity , max_accel, max_accel_to_decel, square_corner_velocity
        self.toolhead.set_velocity_limitx(99999999.0, 99999999.0, 99999999.0, 99999999.0)
        self.kin.max_z_velocity = self.normal_max_z_velocity

        #调节速度
        self.gcode.run_script_from_command("M220 S100")
        

    def switch_to_violent(self, pre_work_mode):
        logging.info("hzk123 switch_to_violent")
        pass

    def switch_to_sports(self, pre_work_mode):
        logging.info("hzk123 switch_to_sports")
        pass

    def switch_to_silent(self, pre_work_mode):
        logging.info("hzk123 switch_to_silent")
        self.reactor.pause(self.reactor.monotonic() + 1.0)

        #调节速度
        self.gcode.run_script_from_command("M220 S66")

        #5 速度加速度
        #max_velocity , max_accel, max_accel_to_decel, square_corner_velocity
        self.toolhead.set_velocity_limitx(self.silent_max_velocity, self.silent_max_accel, self.silent_max_accel_to_decel, self.silent_square_corner_velocity)
        self.kin.max_z_velocity = 100

        #1 set "stealthchop" mode
        self.set_stealthchop(self.stepper_a, self.tmc5160_stepper_a.mcu_tmc, TMC_FREQUENCY, self.silent_stealthchop)
        self.set_stealthchop(self.stepper_b, self.tmc5160_stepper_b.mcu_tmc, TMC_FREQUENCY, self.silent_stealthchop)
        self.set_stealthchop(self.stepper_c, self.tmc5160_stepper_c.mcu_tmc, TMC_FREQUENCY, self.silent_stealthchop)

        #2 set extruder run_current 挤出机运行电流
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=extruder CURRENT={self.silent_extruder_run_current}")
        #a,b,c电机电流
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=stepper_a CURRENT={self.silent_step_abc_run_current}")
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=stepper_b CURRENT={self.silent_step_abc_run_current}")
        self.gcode.run_script_from_command(f"SET_TMC_CURRENT STEPPER=stepper_c CURRENT={self.silent_step_abc_run_current}")

        #3 set fan max_power 涡轮风扇
        self.fan.max_power = self.silent_fan_max_power
        curtime = self.printer.get_reactor().monotonic()
        print_time = self.fan.get_mcu().estimated_print_time(curtime)
        print_time = print_time + PIN_MIN_TIME
        self.fan.set_speed(print_time, self.fan.cur_set_speed)

        #4 效应器风扇
        self.heater_fan_heat_sink_fan.fan_speed = self.silent_heater_fan_heat_sink_fan_fan_speed
        curtime = self.printer.get_reactor().monotonic()
        self.heater_fan_heat_sink_fan.callback(curtime)




    cmd_PRINTER_WORKMODE_help = "Switch the 3D printer working mode: violent mode, sports mode, standard mode, silent mode"
    def cmd_PRINTER_WORKMODE(self, gcmd):
        work_mode = gcmd.get_int('M', 0)
        if self.work_mode != work_mode:
            if 0 == work_mode: #正常模式
                self.switch_to_normal(self.work_mode)
                self.work_mode = work_mode
            elif 1 == work_mode: #狂暴模式
                self.switch_to_violent(self.work_mode)
                self.work_mode = work_mode
            elif 2 == work_mode: #运动模式
                self.switch_to_sports(self.work_mode)
                self.work_mode = work_mode
            elif 3 == work_mode: #静音模式
                self.switch_to_silent(self.work_mode)
                self.work_mode = work_mode
            else:
                pass

    def get_status(self, eventtime):
        return {
            'mode': self.work_mode
        }


def load_config(config):
    logging.info(f"printer_workmode load_config")
    return PrinterWorkMode(config)
