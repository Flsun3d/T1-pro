# Exclude moves toward and inside objects


import logging
import json

OBJECT_START_DIFF = 3
MAX_OBJECTS_NUM = 64
class ExcludeObject:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.reactor = self.printer.get_reactor()
        self.gcode_move = self.printer.load_object(config, 'gcode_move')
        self.printer.register_event_handler('klippy:connect',
                                        self._handle_connect)
        self.printer.register_event_handler("virtual_sdcard:reset_file",
                                            self._reset_file)
        self.next_transform = None
        self.last_position_extruded = [0., 0., 0., 0.]
        self.last_position_excluded = [0., 0., 0., 0.]

        self._reset_state()
        self.gcode.register_command(
            'EXCLUDE_OBJECT_START', self.cmd_EXCLUDE_OBJECT_START,
            desc=self.cmd_EXCLUDE_OBJECT_START_help)
        self.gcode.register_command(
            'EXCLUDE_OBJECT_END', self.cmd_EXCLUDE_OBJECT_END,
            desc=self.cmd_EXCLUDE_OBJECT_END_help)
        self.gcode.register_command(
            'EXCLUDE_OBJECT', self.cmd_EXCLUDE_OBJECT,
            desc=self.cmd_EXCLUDE_OBJECT_help)
        self.gcode.register_command(
            'EXCLUDE_OBJECT_DEFINE', self.cmd_EXCLUDE_OBJECT_DEFINE,
            desc=self.cmd_EXCLUDE_OBJECT_DEFINE_help)
        self.gcode.register_command(
            'SET_EXCLUDE_ENABLE', self.cmd_SET_EXCLUDE_ENABLE,
            desc=self.cmd_SET_EXCLUDE_ENABLE_help)

    def _register_transform(self):
        if self.next_transform is None:
            tuning_tower = self.printer.lookup_object('tuning_tower')
            if tuning_tower.is_active():
                logging.info('The ExcludeObject move transform is not being '
                    'loaded due to Tuning tower being Active')
                return

            self.next_transform = self.gcode_move.set_move_transform(self,
                                                                     force=True)
            self.extrusion_offsets = {}
            self.max_position_extruded = 0
            self.max_position_excluded = 0
            self.extruder_adj = 0
            self.initial_extrusion_moves = 5
            self.last_position = [0., 0., 0., 0.]

            self.get_position()
            self.last_position_extruded[:] = self.last_position
            self.last_position_excluded[:] = self.last_position

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def _unregister_transform(self):
        if self.next_transform:
            tuning_tower = self.printer.lookup_object('tuning_tower')
            if tuning_tower.is_active():
                logging.error('The Exclude Object move transform was not '
                    'unregistered because it is not at the head of the '
                    'transform chain.')
                return

            self.gcode_move.set_move_transform(self.next_transform, force=True)
            self.next_transform = None
            self.gcode_move.reset_last_position()

    def save_exclude_objects(self,key,value):
        save_variables = self.printer.lookup_object('save_variables')
        if save_variables is None:
            raise self.gcode.error("save_variables not found")
        save_objects = dict()
        save_objects[key] = value
        save_variables.setVariables(save_objects,None)

    def _reset_state(self):
        self.objects = []
        self.excluded_objects = []
        self.current_object = None
        self.in_excluded_region = False
        self.object_end_count = {}
        self.exclude_enabled = True
        self.is_excluded_object = False

    def _reset_file(self):
        self._reset_state()
        self._unregister_transform()

    def _get_extrusion_offsets(self):
        offset = self.extrusion_offsets.get(
            self.toolhead.get_extruder().get_name())
        if offset is None:
            offset = [0., 0., 0., 0.]
            self.extrusion_offsets[self.toolhead.get_extruder().get_name()] = \
                offset
        return offset

    def get_position(self):
        offset = self._get_extrusion_offsets()
        pos = self.next_transform.get_position()
        for i in range(4):
            self.last_position[i] = pos[i] + offset[i]
        return list(self.last_position)

    def _normal_move(self, newpos, speed):
        offset = self._get_extrusion_offsets()

        if self.initial_extrusion_moves > 0 and \
            self.last_position[3] != newpos[3]:
            # Since the transform is not loaded until there is a request to
            # exclude an object, the transform needs to track a few extrusions
            # to get the state of the extruder
            self.initial_extrusion_moves -= 1

        self.last_position[:] = newpos
        self.last_position_extruded[:] = self.last_position
        self.max_position_extruded = max(self.max_position_extruded, newpos[3])

        # These next few conditionals handle the moves immediately after leaving
        # and excluded object.  The toolhead is at the end of the last printed
        # object and the gcode is at the end of the last excluded object.
        #
        # Ideally, there will be Z and E moves right away to adjust any offsets
        # before moving away from the last position.  Any remaining corrections
        # will be made on the firs XY move.
        if (offset[0] != 0 or offset[1] != 0) and \
            (newpos[0] != self.last_position_excluded[0] or \
            newpos[1] != self.last_position_excluded[1]):
            offset[0] = 0
            offset[1] = 0
            offset[2] = 0
            offset[3] += self.extruder_adj
            self.extruder_adj = 0

        if offset[2] != 0 and newpos[2] != self.last_position_excluded[2]:
            offset[2] = 0

        if self.extruder_adj != 0 and \
            newpos[3] != self.last_position_excluded[3]:
            offset[3] += self.extruder_adj
            self.extruder_adj = 0

        tx_pos = newpos[:]
        for i in range(4):
            tx_pos[i] = newpos[i] - offset[i]
        self.next_transform.move(tx_pos, speed)

    def _ignore_move(self, newpos, speed):
        offset = self._get_extrusion_offsets()
        for i in range(3):
            offset[i] = newpos[i] - self.last_position_extruded[i]
        offset[3] = offset[3] + newpos[3] - self.last_position[3]
        self.last_position[:] = newpos
        self.last_position_excluded[:] =self.last_position
        self.max_position_excluded = max(self.max_position_excluded, newpos[3])

    def _move_into_excluded_region(self, newpos, speed):
        self.in_excluded_region = True
        self._ignore_move(newpos, speed)

    def _move_from_excluded_region(self, newpos, speed):
        self.in_excluded_region = False

        # This adjustment value is used to compensate for any retraction
        # differences between the last object printed and excluded one.
        self.extruder_adj = self.max_position_excluded \
            - self.last_position_excluded[3] \
            - (self.max_position_extruded - self.last_position_extruded[3])
        self._normal_move(newpos, speed)

    def _test_in_excluded_region(self):
        # Inside cancelled object
        self.is_excluded_object = self.current_object in self.excluded_objects
        return  self.is_excluded_object and self.initial_extrusion_moves == 0

    def _test_in_excluded_region_for_move(self):
        # Inside cancelled object
        return self.is_excluded_object and self.initial_extrusion_moves == 0            

    def get_status(self, eventtime=None):
        status = {
            "objects": self.objects,
            "excluded_objects": self.excluded_objects,
            "current_object": self.current_object,
            "enabled": bool(self.exclude_enabled)
        }
        return status

    def move(self, newpos, speed):
        move_in_excluded_region = self._test_in_excluded_region_for_move()
        self.last_speed = speed

        if move_in_excluded_region:
            if self.in_excluded_region:
                self._ignore_move(newpos, speed)
            else:
                self._move_into_excluded_region(newpos, speed)
        else:
            if self.in_excluded_region:
                self._move_from_excluded_region(newpos, speed)
            else:
                self._normal_move(newpos, speed)

    cmd_EXCLUDE_OBJECT_START_help = "Marks the beginning the current object" \
                                    " as labeled"
    def cmd_EXCLUDE_OBJECT_START(self, gcmd):
        name = gcmd.get('NAME').upper()
        if not any(obj["name"] == name for obj in self.objects):
            self._add_object_definition({"name": name})
        self.current_object = name
        self.was_excluded_at_start = self._test_in_excluded_region()

        if self.was_excluded_at_start:
            self.reactor.pause(self.reactor.monotonic() + 0.02) #hzk123 delay 20ms

    cmd_EXCLUDE_OBJECT_END_help = "Marks the end the current object"
    def cmd_EXCLUDE_OBJECT_END(self, gcmd):
        if self.current_object == None and self.next_transform:
            gcmd.respond_info("EXCLUDE_OBJECT_END called, but no object is"
                              " currently active")
            return
        name = gcmd.get('NAME', default=None)
        if name != None and name.upper() != self.current_object:
            gcmd.respond_info("EXCLUDE_OBJECT_END NAME=%s does not match the"
                              " current object NAME=%s" %
                              (name.upper(), self.current_object))

        self.current_object = None

        #clear layer count
        move_in_excluded_region = self._test_in_excluded_region_for_move()
        if not move_in_excluded_region:
            for key in self.object_end_count:
                self.object_end_count[key] = 0
            return 

        name = name.upper()
        self.object_end_count[name] = self.object_end_count.get(name, 0) + 1

        max_end_count = max(self.object_end_count.items(), key=lambda item:item[1])
        if max_end_count[1] > OBJECT_START_DIFF: #如果已排除的对象与未排除的对象之间的层数差值大于3，则认为未被排除的对象已经结束打印了
            logging.info(f"exclude_object over max_end_countx={max_end_count[1]}")
            raise self.gcode.error("exclude_object over")

    cmd_EXCLUDE_OBJECT_help = "Cancel moves inside a specified objects"
    def cmd_EXCLUDE_OBJECT(self, gcmd):
        reset = gcmd.get('RESET', None)
        current = gcmd.get('CURRENT', None)
        name = gcmd.get('NAME', '').upper()
    
        if reset:
            if name:
                self._unexclude_object(name)

            else:
                self.excluded_objects = []

        elif name:
            if name.upper() not in self.excluded_objects:
                self._exclude_object(name.upper())

        elif current:
            if not self.current_object:
                raise self.gcode.error('There is no current object to cancel')

            else:
                self._exclude_object(self.current_object)

        else:
            self._list_excluded_objects(gcmd)

    cmd_EXCLUDE_OBJECT_DEFINE_help = "Provides a summary of an object"
    def cmd_EXCLUDE_OBJECT_DEFINE(self, gcmd):
        if not self.exclude_enabled:  # 如果不支持对象排除，函数主体将不被执行
            return
        reset = gcmd.get('RESET', None)
        name = gcmd.get('NAME', '').upper()

        if reset:
            self._reset_file()

        elif name:
            parameters = gcmd.get_command_parameters().copy()
            parameters.pop('NAME')
            center = parameters.pop('CENTER', None)
            polygon = parameters.pop('POLYGON', None)

            obj = {"name": name.upper()}
            obj.update(parameters)

            if center != None:
                obj['center'] = json.loads('[%s]' % center)
            
            if len(self.objects) >= MAX_OBJECTS_NUM:
                self.exclude_enabled = False
                self.save_exclude_objects("objects_enabled",self.exclude_enabled)
                return
            
            self._add_object_definition(obj)

        else:
            self._list_objects(gcmd)

    cmd_SET_EXCLUDE_ENABLE_help = "Enable object exclusion function for reset"
    def cmd_SET_EXCLUDE_ENABLE(self, gcmd):
        exclude_enable = gcmd.get_int('ENABLE', 1)
        if exclude_enable:
            self.exclude_enabled = True
        else:
            self.exclude_enabled = False
        self.save_exclude_objects("objects_enabled",self.exclude_enabled)
        self.gcode.respond_info("Exclude_object set success")

    def _add_object_definition(self, definition):
        self.objects = sorted(self.objects + [definition],
                              key=lambda o: o["name"])
        self.object_end_count[definition["name"]] = 0

    def _exclude_object(self, name):
        self._register_transform()
        self.gcode.respond_info('Excluding object {}'.format(name.upper()))
        if name not in self.excluded_objects:
            self.excluded_objects = sorted(self.excluded_objects + [name])
            self.save_exclude_objects("excluded_objects",self.excluded_objects)

    def _unexclude_object(self, name):
        self.gcode.respond_info('Unexcluding object {}'.format(name.upper()))
        if name in self.excluded_objects:
            excluded_objects = list(self.excluded_objects)
            excluded_objects.remove(name)
            self.excluded_objects = sorted(excluded_objects)
            self.save_exclude_objects("excluded_objects",self.excluded_objects)

    def _list_objects(self, gcmd):
        if gcmd.get('JSON', None) is not None:
            object_list = json.dumps(self.objects)
        else:
            object_list = " ".join(obj['name'] for obj in self.objects)
        gcmd.respond_info('Known objects: {}'.format(object_list))

    def _list_excluded_objects(self, gcmd):
        object_list = " ".join(self.excluded_objects)
        gcmd.respond_info('Excluded objects: {}'.format(object_list))

def load_config(config):
    return ExcludeObject(config)
