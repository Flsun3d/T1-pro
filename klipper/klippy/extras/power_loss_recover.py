# Copyright (c) 2024,郑州潮阔电子科技有限公司
# All rights reserved.
# 
# 文件名称：power_loss_recover.py
# 摘    要：添加断电续打功能处理模块
# 
# 当前版本：1.0
# 作    者：郭夫华
# 完成日期：2024年10月18日
#
# 修订记录：

import logging

class PowerLossRecover:
    def __init__(self, config):
        self.printer = config.get_printer()

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
        
        # register commands
        self.gcode.register_command("RESUME_INTERRUPTED", self.cmd_RESUME_INTERRUPTED, 
            desc=self.cmd_RESUME_INTERRUPTED_help)
        self.gcode.register_command("F102", self.cmd_F102, desc=self.cmd_F102_help)
        self.gcode.register_command("F103", self.cmd_F103, desc=self.cmd_F103_help)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        
    def _lookup_required_module(self, module_name):
        module = self.printer.lookup_object(module_name, None)
        if module is None:
            raise self.gcode.error(f"not found {module_name} module_name)")
        return module

    def _handle_ready(self):
        self.extruder = self.printer.lookup_object('extruder', None)
        if self.extruder is None:
            raise self.gcode.error("not found extruder module")

    cmd_RESUME_INTERRUPTED_help = "Recover print after power loss and power on"
    def cmd_RESUME_INTERRUPTED(self, gcmd):
        reactor = self.printer.get_reactor()
        print_stats = self.print_stats.get_status(reactor.monotonic())
        if print_stats['state'] == "printing" :
            self.gcode.respond_info("printing, can't resume interrupted")
            return
        variables = self.save_variables.get_status(None)['variables']
        logging.info(f"RESUME_INTERRUPTED variables = {variables}")
        # read continue print parameters and recover print
        last_file = variables.get('sd_filename')
        file_position = int(variables.get('file_position'))
        print_duration = variables.get('print_duration')
        self.v_sd.recover_print(gcmd, last_file, file_position, print_duration)

    cmd_F102_help = "Recover pre-print prearation actions"
    def cmd_F102(self, gcmd):
        reactor = self.printer.get_reactor()

        variables = self.save_variables.get_status(reactor.monotonic())['variables']
        # read continue print parameters and recover print
        e_pos = float(variables.get('e_pos', 0.))
        x_pos = float(variables.get('x_pos', 0.))
        y_pos = float(variables.get('y_pos', 0.))
        z_pos = float(variables.get('z_pos', 0.))
        fan_speed = variables.get('fan_speed')
        # max_power_factor = variables.get('max_power_factor', 0.)
        nozzle_temp = variables.get('nozzle_temp')
        bed_temp = variables.get('bed_temp')
        absolute_extrude = variables.get('absolute_extrude', True)
        absolute_coordinates = variables.get('absolute_coordinates', True)
        filament_used = float(variables.get('filament_used', 0.))
        excluded_objects = variables.get('excluded_objects')
        # objects = variables.get('objects')
        objects_enabled = variables.get('objects_enabled')
        if not objects_enabled:
            self.gcode.run_script_from_command("SET_EXCLUDE_ENABLE ENABLE=0")
        
        # check variables
        if (e_pos is None or x_pos is None or y_pos is None or z_pos is None 
                or fan_speed is None or nozzle_temp is None or bed_temp is None):
            raise gcmd.error(f"save_variables e_pos x_pos y_pos z_pos incomplete")
        
        # if len(objects):
        #     for obj in objects:
        #         obj_def = "EXCLUDE_OBJECT_DEFINE " + \
        #                   "NAME=" + str(obj["name"]).replace(" ", "") + \
        #                   " CENTER=" + str(obj["center"]).replace(" ", "")
        #         self.gcode.run_script_from_command(obj_def)
        
        # 判断是否存在零件跳过
        if len(excluded_objects):
            # 如果存在，优先跳过被取消的零件
            for excluded_object in excluded_objects:
                excluded_object_def = "EXCLUDE_OBJECT NAME="+str(excluded_object)
                logging.info(excluded_object_def)
                self.gcode.run_script_from_command(excluded_object_def)
        cur_nozzle_temp = self.extruder.get_status(reactor.monotonic())['temperature']
        cur_bed_temp = self.heater_bed.get_status(reactor.monotonic())['temperature']

        need_preheating_nozzle = cur_nozzle_temp < 140
        need_preheating_bed = cur_bed_temp < 50
        # 先设置目标温度，进行加热
        if need_preheating_nozzle:
            if need_preheating_bed:
                self.gcode.run_script_from_command("M104 S140")
            else:
                self.gcode.run_script_from_command("M140 S" + str(bed_temp))
        else:
            self.gcode.run_script_from_command("M104 S" + str(nozzle_temp))
            if not need_preheating_bed:
                self.gcode.run_script_from_command("M140 S" + str(bed_temp))
        # 等待温度加热到目标值
        if need_preheating_bed:
            self.gcode.run_script_from_command("M190 S50")
        if need_preheating_nozzle:
            # 热床加热后，如果喷头温度已经达到，就不再等待，未达到再等待
            cur_nozzle_temp = self.extruder.get_status(reactor.monotonic())['temperature']
            if cur_nozzle_temp < 140:
                self.gcode.run_script_from_command("M109 S140")
        # 恢复打印前温度
        if need_preheating_bed:
            self.gcode.run_script_from_command("M140 S" + str(bed_temp))
        if need_preheating_nozzle:
            self.gcode.run_script_from_command("M104 S" + str(nozzle_temp))
        # 恢复打印前的挤出模式及运动模式
        if absolute_extrude:
            self.gcode.run_script_from_command("M82")
        else:
            self.gcode.run_script_from_command("M83")
        # 回家
        self.gcode.run_script_from_command("G28")
        self.gcode.run_script_from_command("SAVE_VARIABLE VARIABLE=plr_flag VALUE=True")
        
        # 如果温度已经达到打印前温度，不再等待
        cur_bed_temp = self.heater_bed.get_status(reactor.monotonic()).get('temperature', 0)
        if cur_bed_temp < bed_temp - 3:
            self.gcode.run_script_from_command("M190 S" + str(bed_temp))
        cur_nozzle_temp = self.extruder.get_status(reactor.monotonic()).get('temperature', 0)
        if cur_nozzle_temp < nozzle_temp - 3:
            self.gcode.run_script_from_command("M109 S" + str(nozzle_temp))

        fan_speed = min(max(fan_speed * 255, 0), 255)
        self.gcode.run_script_from_command("M106 S" + str(fan_speed))
        # self.gcode.run_script_from_command("F105 S" + str(max_power_factor))
        self.gcode.run_script_from_command("G92 E" + str(e_pos))

        toolhead = self.printer.lookup_object('toolhead')
        max_z = float(toolhead.get_status(reactor.monotonic())['axis_maximum'].z)
        # 判断zoffset后，Z位置轴最大值，不能超过
        homed_z = self.gcode_move.get_status(reactor.monotonic())['homing_origin'].z
        # limit_z取精度小数点后4位，且不大于最大值，精度0.001不影响实际打印
        limit_z = round(max_z - homed_z, 4) - 0.001
        if not z_pos < limit_z:
            logging.info("F102 move out of range, change z pos:%s --> %s", z_pos, limit_z)
            z_pos = limit_z

        if z_pos + 0.6 < limit_z:
            self.gcode.run_script_from_command("G1 Z" + str(z_pos+0.6))

        self.gcode.run_script_from_command("G1 X%d Y%d Z%d F3000" % (x_pos, y_pos, z_pos))
        self.gcode.run_script_from_command(f"SET_FILAMENT_USED S={filament_used}")
        self.gcode.run_script_from_command("M400")
        # 相对坐标及绝对坐标模式在效应器头恢复到位置后再切换，避免效应器恢复出错
        if absolute_coordinates:
            self.gcode.run_script_from_command("G90")
        else:
            self.gcode.run_script_from_command("G91")
        self.gcode.run_script_from_command("CLEAR_PAUSE")

    cmd_F103_help = "Save power loss info"
    def cmd_F103(self, gcmd):
        reactor = self.printer.get_reactor()
        try:
            print_stats = self.print_stats.get_status(reactor.monotonic())
            if print_stats['state'] == "printing" :
                newvars = dict()
                newvars['was_interrupted'] = True
                
                v_sd_stats = self.v_sd.get_status(reactor.monotonic())
                newvars['sd_filename'] = str(v_sd_stats['file_path'])
                newvars['file_position'] = v_sd_stats['file_position']

                gcode_move = self.gcode_move.get_status(reactor.monotonic())
                gcode_position = gcode_move['gcode_position']
                newvars['e_pos'] = gcode_position.e
                newvars['x_pos'] = gcode_position.x
                newvars['y_pos'] = gcode_position.y
                newvars['z_pos'] = gcode_position.z
                newvars['absolute_extrude'] = gcode_move['absolute_extrude']
                newvars['absolute_coordinates'] = gcode_move['absolute_coordinates']

                newvars['print_duration'] = print_stats['print_duration']
                newvars['filament_used'] = float(print_stats['filament_used'])
                fan_max_power = max(0.1, float(self.fan.get_status(reactor.monotonic())['max_power']))
                newvars['fan_speed'] = float(self.fan.get_status(reactor.monotonic())['speed'])/fan_max_power
                # newvars['max_power_factor'] = float(self.fan.get_status(reactor.monotonic())['max_power_factor'])

                newvars['nozzle_temp'] = float(self.extruder.get_status(reactor.monotonic())['target'])
                newvars['bed_temp'] = float(self.heater_bed.get_status(reactor.monotonic())['target'])
                #每一层要保存一下零件跳过的信息
                newvars['excluded_objects'] = self.exclude_object.get_status(reactor.monotonic())['excluded_objects']
                # newvars['objects'] = self.exclude_object.get_status(reactor.monotonic())['objects']
                newvars['objects_enabled'] = self.exclude_object.get_status(reactor.monotonic())['enabled']
                
                self.save_variables.setVariables(newvars, gcmd)
            else:
                logging.info(f"print_stats.state = {print_stats['state']}, not printing")
        except Exception as e:
            logging.error(f"Error in cmd_F103: {e}")

def load_config(config):
    logging.info(f"power_loss_recover load_config")
    return PowerLossRecover(config)
