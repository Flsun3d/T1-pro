# G-Code G1 movement commands (and associated coordinate manipulation)
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, chelper
import subprocess #flsun add, for AI detect,Add a child thread
import copy

class GCodeMove:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        #flsun add , add x_size_offset and y_size_offset to modify size precision
        p_config = config.getsection('printer')
        self.x_size_offset = p_config.getfloat('x_size_offset', 0, above=-0.035, below=0.035) 
        self.y_size_offset = p_config.getfloat('y_size_offset', 0, above=-0.035, below=0.035) 
        
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        printer.register_event_handler("toolhead:set_position",
                                       self.reset_last_position)
        printer.register_event_handler("toolhead:manual_move",
                                       self.reset_last_position)
        printer.register_event_handler("gcode:command_error",
                                       self.reset_last_position)
        printer.register_event_handler("extruder:activate_extruder",
                                       self._handle_activate_extruder)
        printer.register_event_handler("homing:home_rails_end",
                                       self._handle_home_rails_end)
        self.is_printer_ready = False
        # Register g-code commands
        gcode = printer.lookup_object('gcode')
        handlers = [
            'G1', 'G20', 'G21',
            'M82', 'M83', 'G90', 'G91', 'G92', 'M220', 'M221',
            'SET_GCODE_OFFSET', 'SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE',
        ]
        for cmd in handlers:
            func = getattr(self, 'cmd_' + cmd)
            desc = getattr(self, 'cmd_' + cmd + '_help', None)
            gcode.register_command(cmd, func, False, desc)
        gcode.register_command('G0', self.cmd_G1)
        gcode.register_command('M114', self.cmd_M114, True)
        gcode.register_command('GET_POSITION', self.cmd_GET_POSITION, True,
                               desc=self.cmd_GET_POSITION_help)
        gcode.register_command('SIZE_ANALYZE', self.cmd_SIZE_ANALYZE,
                                    desc=self.cmd_SIZE_ANALYZE_help)
        self.ffi_main, self.ffi_lib = chelper.get_ffi()
        self.Coord = gcode.Coord
        # G-Code coordinate manipulation
        self.absolute_coord = self.absolute_extrude = True
        self.base_position = [0.0, 0.0, 0.0, 0.0]
        self.last_position = [0.0, 0.0, 0.0, 0.0]
        self.homing_position = [0.0, 0.0, 0.0, 0.0]
        self.speed = 25.
        self.speed_factor = 1. / 60.
        self.extrude_factor = 1.
        self.e_pos = 0 #flsun add, Record extruder coordinates
        self.z_pos = 0 #flsun add, Record Z coordinates
        self.last_AI_z = 0 #flsun add,
        self.max_z = 330 #flsun add
        self.reactor = self.printer.get_reactor() #flsun add 
        # G-Code state
        self.saved_states = {}
        self.move_transform = self.move_with_transform = None
        self.position_with_transform = (lambda: [0., 0., 0., 0.])
        self.size_data = {'X': [], 'Y': []}
        self.size_flag = False 
        self.size_cali_mode = p_config.get('size_cali_mode', 'Refined')
        if str(self.size_cali_mode).strip() == "integrated".strip():
            self.size_flag = False 
        elif str(self.size_cali_mode).strip() == "Refined".strip():
            self.size_flag = True
        self.x_offset = list(p_config.getfloatlist('x_offset', (0.0,0.0,0.0,0.0,0.0,0.0), count=6))
        self.y_offset = list(p_config.getfloatlist('y_offset', (0.0,0.0,0.0,0.0,0.0,0.0), count=6))
        self.x_size = [70, 70, 70, 70, 70, 70]
        self.y_size = [70, 70, 70, 70, 70, 70]
        self.max_offset = 0.035
        for i in range(6):
            self.x_offset[i] = max(min(self.x_offset[i], self.max_offset), -self.max_offset)
            self.y_offset[i] = max(min(self.y_offset[i], self.max_offset), -self.max_offset)
            self.x_size[i] = 70.0 - 70.0*self.x_offset[i]
            self.y_size[i] = 70.0 - 70.0*self.y_offset[i]

    cmd_SIZE_ANALYZE_help = "size calibration"
    def cmd_SIZE_ANALYZE(self, gcmd):
        args = {'X': 6, 'Y': 6}
        input_x = False
        input_y = False
        for name, count in args.items():
            data = gcmd.get(name, None)
            if data is None:
                continue
            try:
                parts = list(map(float, data.split(',')))
            except:
                raise gcmd.error("Unable to parse parameter '%s'" % (name,))
            if len(parts) != count:
                raise gcmd.error("Parameter '%s' must have %d values"
                                 % (name, count))
            self.size_data[name] = parts
            if 'X' in name:
                input_x = True
            if 'Y' in name:
                input_y = True
            logging.info("SIZE_ANALYZE %s = %s", name, parts)
        #The blocks are arranged according to this pattern
        # 1   2   3     SIZE_ANALYZE X=L_1_2,L_2_3,L_4_5,L_5_6,L_7_8,L_8_9
        # 4   5   6     SIZE_ANALYZE Y=L_1_4,L_4_7,L_2_5,L_5_8,L_3_6,L_6_9
        # 7   8   9
        # or
        #     1
        # 2       3
        #     4
        configfile = self.printer.lookup_object('configfile')
        self.new_x_offset = []
        self.new_y_offset = []
        if len(self.size_data['X']) == 6 and input_x:
            self.size_data['X'][3] -= 0.1
            for i in range(len(self.size_data['X'])):
                self.new_x_offset.append((((70.0 - self.size_data['X'][i])/70) + 1) * (1 + self.x_offset[i]) - 1)
            configfile.set('printer', 'x_offset', "%.5f,%.5f,%.5f,%.5f,%.5f,%.5f" % tuple(self.new_x_offset))
        if len(self.size_data['Y']) == 6 and input_y:
            for i in range(len(self.size_data['Y'])):
                self.new_y_offset.append((((70.0 - self.size_data['Y'][i])/70) + 1) * (1 + self.y_offset[i]) - 1)
            configfile.set('printer', 'y_offset', "%.5f,%.5f,%.5f,%.5f,%.5f,%.5f" % tuple(self.new_y_offset))

    def _handle_ready(self):
        self.is_printer_ready = True
        if self.move_transform is None:
            toolhead = self.printer.lookup_object('toolhead')
            self.move_with_transform = toolhead.move
            self.position_with_transform = toolhead.get_position
        self.reset_last_position()
        self.printer_workmode = self.printer.lookup_object('printer_workmode')
        self.pre_speed = -1.0
    def _handle_shutdown(self):
        if not self.is_printer_ready:
            return
        self.is_printer_ready = False
        logging.info("gcode state: absolute_coord=%s absolute_extrude=%s"
                     " base_position=%s last_position=%s homing_position=%s"
                     " speed_factor=%s extrude_factor=%s speed=%s",
                     self.absolute_coord, self.absolute_extrude,
                     self.base_position, self.last_position,
                     self.homing_position, self.speed_factor,
                     self.extrude_factor, self.speed)
    def _handle_activate_extruder(self):
        self.reset_last_position()
        self.extrude_factor = 1.
        self.base_position[3] = self.last_position[3]
    def _handle_home_rails_end(self, homing_state, rails):
        self.reset_last_position()
        self.max_z = self.last_position[2] #flsun add
        for axis in homing_state.get_axes():
            self.base_position[axis] = self.homing_position[axis]
    def set_move_transform(self, transform, force=False):
        if self.move_transform is not None and not force:
            raise self.printer.config_error(
                "G-Code move transform already specified")
        old_transform = self.move_transform
        if old_transform is None:
            old_transform = self.printer.lookup_object('toolhead', None)
        self.move_transform = transform
        self.move_with_transform = transform.move
        self.position_with_transform = transform.get_position
        return old_transform
    def _get_gcode_position(self):
        p = [lp - bp for lp, bp in zip(self.last_position, self.base_position)]
        p[3] /= self.extrude_factor
        return p
    def _get_gcode_speed(self):
        return self.speed / self.speed_factor
    def _get_gcode_speed_override(self):
        return self.speed_factor * 60.
    def get_status(self, eventtime=None):
        move_position = self._get_gcode_position()
        return {
            'speed_factor': self._get_gcode_speed_override(),
            'speed': self._get_gcode_speed(),
            'extrude_factor': self.extrude_factor,
            'absolute_coordinates': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'homing_origin': self.Coord(*self.homing_position),
            'position': self.Coord(*self.last_position),
            'gcode_position': self.Coord(*move_position),
        }
    
    def get_gcode_state(self):
        return {
            'absolute_coord': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'base_position': list(self.base_position),
            'last_position': list(self.last_position),
            'homing_position': list(self.homing_position),
            'speed': self.speed, 'speed_factor': self.speed_factor,
            'extrude_factor': self.extrude_factor,
        }
    
    def set_SAVE_GCODE_STATE(self, state_name, state):
        self.saved_states[state_name] = state
    
    def reset_last_position(self):
        if self.is_printer_ready:
            self.last_position = self.position_with_transform()
    # G-Code movement commands
    def get_xy_size_offset(self):
        return self.x_size_offset, self.y_size_offset
    def set_first_layer_detect(self): #flsun add
        self.last_AI_z = 0
        self.first_layer_detect = True
    def cmd_G1(self, gcmd):
        # Move
        params = gcmd.get_command_parameters()
        try:
            for pos, axis in enumerate('XYZ'):
                if axis in params:
                    v = float(params[axis])
                    if not self.absolute_coord:
                        # value relative to position of last move
                        self.last_position[pos] += v
                    else:
                        # value relative to base coordinate position
                        self.last_position[pos] = v + self.base_position[pos]
            if 'Z' in params: #flsun add ,to save z value while printing for power loss
                if self.last_position[2] >= self.z_pos + 0.09:
                    eventtime = self.reactor.monotonic()
                    idle_timeout = self.printer.lookup_object("idle_timeout")
                    is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"
                    is_paused = self.printer.lookup_object("pause_resume").is_paused #wzy add
                    if is_printing and not is_paused: #wzy modify
                        gcode = self.printer.lookup_object('gcode')
                        gcode.run_script_from_command("F103")
                self.z_pos = self.last_position[2]

            if 'E' in params:
                v = float(params['E']) * self.extrude_factor
                if not self.absolute_coord or not self.absolute_extrude:
                    # value relative to position of last move
                    self.last_position[3] += v
                else:
                    # value relative to base coordinate position
                    self.last_position[3] = v + self.base_position[3]
            
            if self.last_position[3] < 10.0: #flsun add,set self.e_pos = 0 when a print start
                self.e_pos = self.last_position[3]
            if self.last_position[3] - self.e_pos > 70 and self.last_position[3] > 100 and self.last_position[2] - self.last_AI_z >= 0.10: #flsun add ,Perform AI detection when these conditions are met  
                eventtime = self.reactor.monotonic()
                idle_timeout = self.printer.lookup_object("idle_timeout")
                is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"          
                if is_printing: 
                    if self.last_AI_z >= 0.20 and self.first_layer_detect:
                        gcode = self.printer.lookup_object('gcode')
                        gcode.run_script_from_command("RESTORE_E_CURRENT")
                        self.first_layer_detect = False
                    self.e_pos = self.last_position[3]
                    self.last_AI_z = self.last_position[2]
                    subprocess.Popen(["bash", "/home/pi/flsun_func/time_lapse/time_lapse.sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) #flsun add
            #flsun add, modify x and y direction,
            self.cali_position = self.last_position[:] #flsun add
            real_x_size_offset = real_y_size_offset = 0
            if self.last_position[2] > (self.max_z - 2.5):
                real_x_size_offset = 0
                real_y_size_offset = 0
            else:
                if self.size_flag:
                    self.ffi_lib.get_size_offset(self.x_size, self.y_size, self.max_offset, self.last_position[0], self.last_position[1])
                    real_x_size_offset = self.ffi_lib.get_x_size_offset()
                    real_y_size_offset = self.ffi_lib.get_y_size_offset()
                else:
                    real_x_size_offset = self.x_size_offset
                    real_y_size_offset = self.y_size_offset
            self.cali_position[0] = self.last_position[0] * (1 + real_x_size_offset) 
            self.cali_position[1] = self.last_position[1] * (1 + real_y_size_offset) 
            
            if 'F' in params:
                gcode_speed = float(params['F'])
                if gcode_speed <= 0.:
                    raise gcmd.error("Invalid speed in '%s'"
                                     % (gcmd.get_commandline(),))
                self.speed = gcode_speed * self.speed_factor
                self.pre_speed = -1.0

            if 'E' not in params and (self.printer_workmode.get_workmode() == 3):
                if self.speed > self.printer_workmode.pace_speed:
                    self.pre_speed = self.speed
                    self.speed = self.printer_workmode.pace_speed
            elif self.pre_speed > 0:
                self.speed = self.pre_speed
                self.pre_speed = -1.0
                
        except ValueError as e:
            raise gcmd.error("Unable to parse move '%s'"
                             % (gcmd.get_commandline(),))
        self.move_with_transform(self.cali_position, self.speed) #flsun modfiy
    # G-Code coordinate manipulation
    def cmd_G20(self, gcmd):
        # Set units to inches
        raise gcmd.error('Machine does not support G20 (inches) command')
    def cmd_G21(self, gcmd):
        # Set units to millimeters
        pass
    def cmd_M82(self, gcmd):
        # Use absolute distances for extrusion
        self.absolute_extrude = True
    def cmd_M83(self, gcmd):
        # Use relative distances for extrusion
        self.absolute_extrude = False
    def cmd_G90(self, gcmd):
        # Use absolute coordinates
        self.absolute_coord = True
    def cmd_G91(self, gcmd):
        # Use relative coordinates
        self.absolute_coord = False
    def cmd_G92(self, gcmd):
        # Set position
        offsets = [ gcmd.get_float(a, None) for a in 'XYZE' ]
        for i, offset in enumerate(offsets):
            if offset is not None:
                if i == 3:
                    offset *= self.extrude_factor
                self.base_position[i] = self.last_position[i] - offset
        if offsets == [None, None, None, None]:
            self.base_position = list(self.last_position)
    def cmd_M114(self, gcmd):
        # Get Current Position
        p = self._get_gcode_position()
        gcmd.respond_raw("X:%.3f Y:%.3f Z:%.3f E:%.3f" % tuple(p))
        size_copy = gcmd.get_float('S', None, above=-1.)
        if size_copy == 1.0:
            subprocess.Popen(["bash", "/home/pi/flsun_func/change_printer_cfg_size_offset.sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) #flsun add
    def cmd_M220(self, gcmd):
        # Set speed factor override percentage
        value = gcmd.get_float('S', 100., above=0.) / (60. * 100.)
        self.speed = self._get_gcode_speed() * value
        self.speed_factor = value
    def cmd_M221(self, gcmd):
        # Set extrude factor override percentage
        new_extrude_factor = gcmd.get_float('S', 100., above=0.) / 100.
        last_e_pos = self.last_position[3]
        e_value = (last_e_pos - self.base_position[3]) / self.extrude_factor
        self.base_position[3] = last_e_pos - e_value * new_extrude_factor
        self.extrude_factor = new_extrude_factor
    cmd_SET_GCODE_OFFSET_help = "Set a virtual offset to g-code positions"
    def cmd_SET_GCODE_OFFSET(self, gcmd):
        move_delta = [0., 0., 0., 0.]
        for pos, axis in enumerate('XYZE'):
            offset = gcmd.get_float(axis, None)
            if offset is None:
                offset = gcmd.get_float(axis + '_ADJUST', None)
                if offset is None:
                    continue
                offset += self.homing_position[pos]
            delta = offset - self.homing_position[pos]
            move_delta[pos] = delta
            self.base_position[pos] += delta
            self.homing_position[pos] = offset
        # Move the toolhead the given offset if requested
        if gcmd.get_int('MOVE', 0):
            speed = gcmd.get_float('MOVE_SPEED', self.speed, above=0.)
            for pos, delta in enumerate(move_delta):
                self.last_position[pos] += delta
            self.move_with_transform(self.last_position, speed)
    cmd_SAVE_GCODE_STATE_help = "Save G-Code coordinate state"
    def cmd_SAVE_GCODE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        self.saved_states[state_name] = {
            'absolute_coord': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'base_position': list(self.base_position),
            'last_position': list(self.last_position),
            'homing_position': list(self.homing_position),
            'speed': self.speed, 'speed_factor': self.speed_factor,
            'extrude_factor': self.extrude_factor,
        }
    cmd_RESTORE_GCODE_STATE_help = "Restore a previously saved G-Code state"
    def cmd_RESTORE_GCODE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        state = self.saved_states.get(state_name)
        if state is None:
            raise gcmd.error("Unknown g-code state: %s" % (state_name,))
        # Restore state
        self.absolute_coord = state['absolute_coord']
        self.absolute_extrude = state['absolute_extrude']
        self.base_position = list(state['base_position'])
        self.homing_position = list(state['homing_position'])
        self.speed = state['speed']
        # self.speed_factor = state['speed_factor'] #dingzeqi
        self.extrude_factor = state['extrude_factor']
        # Restore the relative E position
        e_diff = self.last_position[3] - state['last_position'][3]
        self.base_position[3] += e_diff
        # Move the toolhead back if requested
        if gcmd.get_int('MOVE', 0):
            speed = gcmd.get_float('MOVE_SPEED', self.speed, above=0.)
            # 将终点位置深拷贝到 position_xy，确保对 position_xy 的修改不会影响终点位置。
            position_xy = copy.deepcopy(state['last_position'])
            # 先移动XY,后移动Z,E
            position_xy[2] = self.last_position[2]
            position_xy[3] = self.last_position[3]

            self.last_position[:3] = state['last_position'][:3]
            toolhead = self.printer.lookup_object('toolhead', None)
            # If the recorded position is already higher than max_z, 
            # then it can only be restored to the origin
            if toolhead is not None:
                kin = toolhead.get_kinematics()
                max_z = kin.axes_max[2]
                if self.last_position[2] >= max_z:
                    logging.info("move out of range, change z pos:%s --> %s",
                                self.last_position[2], max_z,)
                    self.last_position[2] = max_z
                    self.last_position[1] = 0
                    self.last_position[0] = 0
            
            # 修改恢复的时候先移动XY,后移动Z,E
            axis_maximum_z = toolhead.get_kinematics().axes_max[2]
            homing_origin_z = self.homing_position[2]
            if self.last_position[2] < (axis_maximum_z - homing_origin_z) - 60:
                self.move_with_transform(position_xy, speed)
            if self.last_position[2] < (axis_maximum_z - homing_origin_z) - 1:
                self.move_with_transform(self.last_position, speed)
            else:
                logging.info("pre pause position is too high and not resume")
    cmd_GET_POSITION_help = (
        "Return information on the current location of the toolhead")
    def cmd_GET_POSITION(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            raise gcmd.error("Printer not ready")
        kin = toolhead.get_kinematics()
        steppers = kin.get_steppers()
        mcu_pos = " ".join(["%s:%d" % (s.get_name(), s.get_mcu_position())
                            for s in steppers])
        cinfo = [(s.get_name(), s.get_commanded_position()) for s in steppers]
        stepper_pos = " ".join(["%s:%.6f" % (a, v) for a, v in cinfo])
        kinfo = zip("XYZ", kin.calc_position(dict(cinfo)))
        kin_pos = " ".join(["%s:%.6f" % (a, v) for a, v in kinfo])
        toolhead_pos = " ".join(["%s:%.6f" % (a, v) for a, v in zip(
            "XYZE", toolhead.get_position())])
        gcode_pos = " ".join(["%s:%.6f"  % (a, v)
                              for a, v in zip("XYZE", self.last_position)])
        base_pos = " ".join(["%s:%.6f"  % (a, v)
                             for a, v in zip("XYZE", self.base_position)])
        homing_pos = " ".join(["%s:%.6f"  % (a, v)
                               for a, v in zip("XYZ", self.homing_position)])
        gcmd.respond_info("mcu: %s\n"
                          "stepper: %s\n"
                          "kinematic: %s\n"
                          "toolhead: %s\n"
                          "gcode: %s\n"
                          "gcode base: %s\n"
                          "gcode homing: %s"
                          % (mcu_pos, stepper_pos, kin_pos, toolhead_pos,
                             gcode_pos, base_pos, homing_pos))

def load_config(config):
    return GCodeMove(config)
