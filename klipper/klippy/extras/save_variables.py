# Save arbitrary variables so that values can be kept across restarts.
#
# Copyright (C) 2020 Dushyant Ahuja <dusht.ahuja@gmail.com>
# Copyright (C) 2016-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging, ast, configparser
import queue, threading

class SaveVariables:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.filename = os.path.expanduser(config.get('filename'))
        self.allVariables = {}
        try:
            if not os.path.exists(self.filename):
                open(self.filename, "w").close()
            self.loadVariables()
        except self.printer.command_error as e:
            raise config.error(str(e))
        self.vars_queue = queue.Queue(maxsize=100)
        self.thread_stop = False
        self.thread = threading.Thread(target=self._save_vars_thread)
        self.thread.start()
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('SAVE_VARIABLE', self.cmd_SAVE_VARIABLE,
                               desc=self.cmd_SAVE_VARIABLE_help)
        self.printer.register_event_handler("klippy:disconnect",
                                       self._handle_disconnect)
    def _handle_disconnect(self):
        self.thread_stop = True
        self.thread.join(timeout=0.2)

    def safe_write(self, filename, varfile):
        temp_filename = filename + ".tmp"
        try:
            # 写入临时文件
            with open(temp_filename, "w") as f:
                varfile.write(f)
                f.flush()  # 确保数据写入缓冲区
                os.fsync(f.fileno())  # 确保数据写入磁盘

            # 原子替换
            os.replace(temp_filename, filename)
            os.sync()
        except Exception as e:
            # 出错时清理临时文件
            if os.path.exists(temp_filename):
                os.remove(temp_filename)
            raise e

    def _save_vars_thread(self):
        logging.info("save variables thread start")
        while self.thread_stop is False:
            try:
                vars = self.vars_queue.get(block=True,timeout=0.1)
                if vars is None:
                    break
                varfile = configparser.ConfigParser()
                varfile.add_section('Variables')
                for name, val in sorted(vars.items()):
                    varfile.set('Variables', name, repr(val).replace('%', "%%"))
                try:
                    self.safe_write(self.filename, varfile)
                except:
                    msg = "Unable to save variable"
                    logging.warning(msg)
            except queue.Empty:
                pass
            except Exception as e:
                logging.warning(str(e))
        logging.info("save variables thread exit")
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
        logging.info("SaveVariables allVariables: %s", self.allVariables)
    cmd_SAVE_VARIABLE_help = "Save arbitrary variables to disk"
    def cmd_SAVE_VARIABLE(self, gcmd):
        varname = gcmd.get('VARIABLE')
        value = gcmd.get('VALUE')
        try:
            value = ast.literal_eval(value)
        except ValueError as e:
            raise gcmd.error("Unable to parse '%s' as a literal" % (value,))
        newvars = dict(self.allVariables)
        newvars[varname] = value
        # Write file
        try:
            self.vars_queue.put_nowait(newvars)
        except queue.Full:
            logging.warning("Variable Queue Full")
        except Exception as e:
            logging.error("Unable to save variables: %s" % (e,))
            raise gcmd.error("Unable to save variables: %s" % (e,))

        gcmd.respond_info("Variable Saved")
        self.allVariables = newvars
        #self.loadVariables()
    def get_status(self, eventtime):
        return {'variables': self.allVariables}
    def setVariables(self, variables, gcmd, dispaly=False):
        logging.debug(f"loss_power variables: {variables}")
        newvars = dict(self.allVariables)
        
        for name, val in sorted(variables.items()):
            newvars[name] = val

        logging.debug("loss_power newvars: %s", newvars)
        # Write file
        try:
            self.vars_queue.put_nowait(newvars)
        except queue.Full:
            logging.warning("Variable Queue Full")
        except Exception as e:
            logging.error("Unable to save variables: %s" % (e,))
            raise gcmd.error("Unable to save variables: %s" % (e,))
        if dispaly:
            gcmd.respond_info("Variable Saved")
        self.allVariables = newvars
def load_config(config):
    return SaveVariables(config)
