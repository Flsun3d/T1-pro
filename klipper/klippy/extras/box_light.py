# Copyright (c) 2024,郑州潮阔电子科技有限公司
# All rights reserved.
# 
# 文件名称：box_light.py
# 摘    要：T1 箱体灯由核心板驱动，本文件通过读写驱动实现灯的控制
# 
# 当前版本：1.0
# 作    者：李旭明
# 完成日期：2024年11月1日
#
# 修订记录：
import logging

light_file_path = '/sys/class/gpio/gpio108/value'

class BoxLight():
    def __init__(self, config):
        self.printer = config.get_printer()
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('F106',
                               self.cmd_F106, desc=self.cmd_F106_help)
    cmd_F106_help = "on or off box light"
    def cmd_F106(self, gcmd):
        value = gcmd.get_int('S', minval=0, maxval=1)
        try:
            with open(light_file_path,'w') as f:
                f.write(str(value))
        except Exception as e:
            raise self.printer.command_error(str(e))
    def get_status(self, eventtime):
        try:
            with open(light_file_path) as f:
                value = int(f.read())
            return {'value': value}
        except Exception as e:
            raise self.printer.command_error(str(e))

def load_config(config):
    return BoxLight(config)

