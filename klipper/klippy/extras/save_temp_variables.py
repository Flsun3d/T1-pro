# Copyright (c) 2024,郑州潮阔电子科技有限公司
# All rights reserved.
# 
# 文件名称：save_temp_variables.py
# 摘    要：添加临时变量保存接口，保存的变量在断电时清空，未断电时正常保存，同save_variables类
# 
# 当前版本：1.0
# 作    者：郭夫华
# 完成日期：2024年10月22日
#
# 修订记录：

import os, logging, ast, configparser, shlex

class SaveTempVariables:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.filename = os.path.expanduser(config.get('filename'))
        self.allVariables = {}
        self.initdir = False
        self._ensure_directory_exists()
        try:
            self.loadVariables()
        except self.printer.command_error as e:
            raise config.error(str(e))
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('F104', self.cmd_F104,
                               desc=self.cmd_F104_help)

    def _ensure_directory_exists(self):
        directory = os.path.dirname(self.filename)
        if not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
                logging.info(f"Created directory: {directory}")
                self.initdir = True
            except OSError as e:
                logging.error(f"Failed to create directory {directory}: {e}")

    def loadVariables(self):
        allvars = {}
        varfile = configparser.ConfigParser()
        try:
            varfile.read(self.filename)
            if varfile.has_section('Variables'):
                for name, val in varfile.items('Variables'):
                    allvars[name] = ast.literal_eval(val)
        except:
            msg = "Unable to parse existing variable file"
            logging.exception(msg)
            raise self.printer.command_error(msg)
        self.allVariables = allvars
        logging.info("SaveTempVariables allVariables: %s", self.allVariables)

    def parse_command_params(self, cmd):
        eparams = [earg.split('=', 1) for earg in shlex.split(cmd)[1:]]
        return eparams

    def validate_params(self, gcmd, eparams):
        param_count = len(eparams)
        if param_count % 2 != 0:
            raise gcmd.error(f"parameter count {param_count}, not in pairs")

    def update_variables(self, gcmd, eparams, newvars):
        last_key = None
        for k, v in eparams:
            if k.upper() == 'K':
                last_key = v
            elif k.upper() == 'V' and last_key is not None:
                try:
                    v = ast.literal_eval(v)
                except ValueError as e:
                    pass
                newvars[last_key] = v
                last_key = None
            else:
                raise gcmd.error(f"Error on {gcmd.get_commandline()}: missing K or V")

    def write_variables_to_file(self, newvars, filename):
        varfile = configparser.ConfigParser()
        varfile.add_section('Variables')
        for name, val in sorted(newvars.items()):
            varfile.set('Variables', name, repr(val).replace('%', "%%"))
        
        if self.initdir == False:
            self._ensure_directory_exists()
        with open(filename, "w") as f:
            varfile.write(f)
        
    cmd_F104_help = "Save arbitrary variables to tmpfs"
    def cmd_F104(self, gcmd):
        cmd = gcmd.get_commandline()
        newvars = dict(self.allVariables)
        try:
            eparams = self.parse_command_params(cmd)
            self.validate_params(gcmd, eparams)
            self.update_variables(gcmd, eparams, newvars)
        except Exception as e:
            raise gcmd.error(f"Error in cmd_F104: {e}")

        # Write file
        try:
            self.write_variables_to_file(newvars, self.filename)
        except PermissionError as e:
            raise gcmd.error(f"F104 Permission error saving file")
        except FileNotFoundError as e:
            self.initdir = False
            raise gcmd.error(f"F104 File not found")
        except Exception as e:
            raise gcmd.error(f"F104 Unable to save variable")
        
        gcmd.respond_info("F104 OK")
        self.allVariables = newvars
    def get_status(self, eventtime):
        return {'variables': self.allVariables}

def load_config(config):
    return SaveTempVariables(config)