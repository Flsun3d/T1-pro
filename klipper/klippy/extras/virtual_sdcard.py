# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, logging, threading
import subprocess #flsun add
import sys #flsun add
import importlib #flsun add
from concurrent.futures import ThreadPoolExecutor
from functools import partial
importlib.reload(sys) #flsun add
#sys.setdefaultencoding('utf8') #flsun add ,add the three line to support Chinese,now don't need it because klipper use python3
VALID_GCODE_EXTS = ['gcode', 'g', 'gco']

DEFAULT_ExcludeObject_GCODE = """
G92 E0
M106 S0
;TYPE:Custom
; filament end gcode 
M107 T0
M104 S0
M104 S0 T1
M140 S0
G92 E0
G91
G1 Z+0.5  F6000
G28 
G90 ;absolute positioning
TIMELAPSE_RENDER
M73 P100 R0
"""

class VirtualSD:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:shutdown",
                                            self.handle_shutdown)
        # sdcard state
        sd = config.get('path')
        self.sdcard_dirname = os.path.normpath(os.path.expanduser(sd))
        self.current_file = None
        self.file_position = self.file_size = 0
        self.power_loss_restart = 0
        # Print Stat Tracking
        self.print_stats = self.printer.load_object(config, 'print_stats')
        # Work timer
        self.reactor = self.printer.get_reactor()
        self.must_pause_work = self.cmd_from_sd = False
        self.next_file_position = 0
        self.work_timer = None
        # Error handling
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.on_error_gcode = gcode_macro.load_template(
            config, 'on_error_gcode', '')
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        for cmd in ['M20', 'M21', 'M23', 'M24', 'M25', 'M26', 'M27']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))
        for cmd in ['M28', 'M29', 'M30']:
            self.gcode.register_command(cmd, self.cmd_error)
        self.gcode.register_command(
            "SDCARD_RESET_FILE", self.cmd_SDCARD_RESET_FILE,
            desc=self.cmd_SDCARD_RESET_FILE_help)
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
            desc=self.cmd_SDCARD_PRINT_FILE_help)
        self.exclude_object_gcode = gcode_macro.load_template(
            config, 'gcode', DEFAULT_ExcludeObject_GCODE)

        self.thread_pool = ThreadPoolExecutor(max_workers=1)
        self.exclude_object = self.printer.load_object(config, 'exclude_object')
    def handle_shutdown(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            try:
                readpos = max(self.file_position - 1024, 0)
                readcount = self.file_position - readpos
                self.current_file.seek(readpos)
                data = self.current_file.read(readcount + 128)
            except:
                logging.exception("virtual_sdcard shutdown read")
                return
            logging.info("Virtual sdcard (%d): %s\nUpcoming (%d): %s",
                         readpos, repr(data[:readcount]),
                         self.file_position, repr(data[readcount:]))
    def stats(self, eventtime):
        if self.work_timer is None:
            return False, ""
        return True, "sd_pos=%d" % (self.file_position,)
    def get_file_list(self, check_subdirs=False):
        if check_subdirs:
            flist = []
            for root, dirs, files in os.walk(
                    self.sdcard_dirname, followlinks=True):
                for name in files:
                    ext = name[name.rfind('.')+1:]
                    if ext not in VALID_GCODE_EXTS:
                        continue
                    full_path = os.path.join(root, name)
                    r_path = full_path[len(self.sdcard_dirname) + 1:]
                    size = os.path.getsize(full_path)
                    flist.append((r_path, size))
            return sorted(flist, key=lambda f: f[0].lower())
        else:
            dname = self.sdcard_dirname
            try:
                filenames = os.listdir(self.sdcard_dirname)
                return [(fname, os.path.getsize(os.path.join(dname, fname)))
                        for fname in sorted(filenames, key=str.lower)
                        if not fname.startswith('.')
                        and os.path.isfile((os.path.join(dname, fname)))]
            except:
                logging.exception("virtual_sdcard get_file_list")
                raise self.gcode.error("Unable to get file list")
    def get_status(self, eventtime):
        return {
            'file_path': self.file_path(),
            'progress': self.progress(),
            'is_active': self.is_active(),
            'file_position': self.file_position,
            'file_size': self.file_size,
        }
    def file_path(self):
        if self.current_file:
            return self.current_file.name
        return None
    def progress(self):
        if self.file_size:
            return float(self.file_position) / self.file_size
        else:
            return 0.
    def is_active(self):
        return self.work_timer is not None
    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
    def do_resume(self):
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def do_cancel(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self.file_position = self.file_size = 0.
    # G-Code commands
    def cmd_error(self, gcmd):
        raise gcmd.error("SD write not supported")
    def _reset_file(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
        self.file_position = self.file_size = 0.
        self.print_stats.reset()
        self.printer.send_event("virtual_sdcard:reset_file")
    cmd_SDCARD_RESET_FILE_help = "Clears a loaded SD File. Stops the print "\
        "if necessary"
    def cmd_SDCARD_RESET_FILE(self, gcmd):
        if self.cmd_from_sd:
            raise gcmd.error(
                "SDCARD_RESET_FILE cannot be run from the sdcard")
        self._reset_file()
    cmd_SDCARD_PRINT_FILE_help = "Loads a SD file and starts the print.  May "\
        "include files in subdirectories."
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get("FILENAME")
        if filename[0] == '/':
            filename = filename[1:]
        self._load_file(gcmd, filename, check_subdirs=True)
        self.do_resume()
    def cmd_M20(self, gcmd):
        # List SD card
        files = self.get_file_list()
        gcmd.respond_raw("Begin file list")
        for fname, fsize in files:
            gcmd.respond_raw("%s %d" % (fname, fsize))
        gcmd.respond_raw("End file list")
    def cmd_M21(self, gcmd):
        # Initialize SD card
        gcmd.respond_raw("SD card ok")
    def cmd_M23(self, gcmd):
        # Select SD file
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get_raw_command_parameters().strip()
        if filename.startswith('/'):
            filename = filename[1:]
        self._load_file(gcmd, filename)
    def recover_print(self, gcmd, filename, fileposition, print_duration):
        logging.info(f"Recover print {filename} (position {fileposition}, duration {print_duration})")
        fname = filename[len(self.sdcard_dirname)+1:]
        self.print_stats.modify_print_time(float(print_duration))
        self._load_file(gcmd, fname, fileposition, check_subdirs=True)
        self.do_resume()
    def _load_file(self, gcmd, filename, fileposition=0, check_subdirs=False):
        files = self.get_file_list(check_subdirs)
        flist = [f[0] for f in files]
        files_by_lower = { fname.lower(): fname for fname, fsize in files }
        fname = filename
        try:
            if fname not in flist:
                fname = files_by_lower[fname.lower()]
            fname = os.path.join(self.sdcard_dirname, fname)
            f = open(fname, 'rb')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
        except:
            logging.exception("virtual_sdcard file open")
            raise gcmd.error("Unable to open file")
        gcmd.respond_raw("File opened:%s Size:%d" % (filename, fsize))
        gcmd.respond_raw("File selected")
        subprocess.Popen(["bash", "/home/pi/flsun_func/time_lapse/time_lapse_init.sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) #flsun add
        self.current_file = f
        self.file_position = int(fileposition) #wzy modify
        self.file_size = fsize
        self.print_stats.set_current_file(filename)
        # start print，default set power loss flag to 0
        #flsun add, run START_PRINT when start a print
        if(self.file_position == 0): #wzy modify
            self.power_loss_restart = 0
            self.gcode.run_script_from_command("START_PRINT")
            exclude_object = self.printer.lookup_object('exclude_object')
            if exclude_object is not None:
                exclude_object.save_exclude_objects("excluded_objects",exclude_object.excluded_objects)
                exclude_object.save_exclude_objects("objects_enabled",exclude_object.exclude_enabled)
        else:#gfh modify
            self.power_loss_restart = 1

    def cmd_M24(self, gcmd):
        # Start/resume SD print
        self.do_resume()
    def cmd_M25(self, gcmd):
        # Pause SD print
        self.do_pause()
    def cmd_M26(self, gcmd):
        # Set SD position
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        pos = gcmd.get_int('S', minval=0)
        self.file_position = pos
    def cmd_M27(self, gcmd):
        # Report SD print status
        if self.current_file is None:
            gcmd.respond_raw("Not SD printing.")
            return
        gcmd.respond_raw("SD printing byte %d/%d"
                         % (self.file_position, self.file_size))
    def get_file_position(self):
        return self.next_file_position
    def set_file_position(self, pos):
        self.next_file_position = pos
    
    def set_resume_file_position(self, pos):
        logging.info(f"[VirtualSD]set_resume_file_position:pos{pos}")
        self.file_position = pos

    def is_cmd_from_sd(self):
        return self.cmd_from_sd
    # Background work timer
    def _thread_read_handler(self,read_data):
        try:
            if self.current_file is None:
                read_data['result'] = "Error: File closed"
                return 
            read_data['data'] = self.current_file.read(8192)
        except:
            logging.exception("virtual_sdcard read")
            read_data['result'] = "virtual_sdcard read error"
            return
    
    def _read_data(self):
        thread_read = {"data": b"","result": None}
        future = self.thread_pool.submit(
            self._thread_read_handler,
            thread_read
        )
        while True:
            try:
                result = future.result(timeout=1.0)
                break
            except TimeoutError:
                logging.info("Read thread is still working")
                self.reactor.pause(self.reactor.monotonic() + 0.01)
                continue
            else:
                break
            
        if thread_read['result']:
            raise thread_read['result']

        return thread_read['data']
		
    def _recover_file_position(self, file_position):
        end_flag = False
        end_position = file_position

        self.current_file.seek(file_position)
        datax = self.current_file.read(8192 + 25)
        lines = datax.split(b'\n')
        if lines:
            lines.reverse()
            partial_input = lines.pop()
            next_file_position = file_position + len(partial_input) + 1
        else:
            return end_flag, end_position
        while lines:
            line = lines.pop()
            line_str = line.decode('utf-8')
            if len(line_str) > 25: #len('EXCLUDE_OBJECT_START NAME'):
                if line_str[:25] == "EXCLUDE_OBJECT_START NAME":
                    end_flag = True
                    end_position = next_file_position
            next_file_position = next_file_position + len(line) + 1

        return end_flag, end_position

    def recover_file_position(self, original_file_position):
        if len(self.exclude_object.excluded_objects) > 0:
            file_position = original_file_position
            end_flag, end_position = False, file_position
            while True:
                end_flag, end_position = self._recover_file_position(file_position)
                if end_flag:
                    self.current_file.seek(end_position)
                    self.exclude_object.initial_extrusion_moves = 0
                    return end_position

                if 0 == file_position:
                    self.current_file.seek(original_file_position)
                    return original_file_position

                file_position = file_position - 8192
                if file_position < 0:
                    file_position = 0
        else:
            return original_file_position

    def work_handler(self, eventtime):
        has_pre_check = False
        logging.info("Starting SD card print (position %d)", self.file_position)
        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.work_timer = None
            self.power_loss_restart = 0
            return self.reactor.NEVER
        self.print_stats.note_start()
        if(self.power_loss_restart != 0):
            self.power_loss_restart = 0
            try:
                self.gcode.run_script("F102")
            except Exception as e:
                logging.exception("resume interrupt error:%s"%str(e))
                self.work_timer = None
                self.print_stats.note_error(str(e))
                return self.reactor.NEVER

            #recover file_position
            self.file_position = self.recover_file_position(self.file_position)
            #recover file_position end
        gcode_mutex = self.gcode.get_mutex()
        partial_input = b""
        lines = []
        error_message = None
        locate_printing_gcode = self.printer.lookup_object('locate_printing_gcode', None)
        while not self.must_pause_work:
            if not lines:
                # Read more data
                try:
                    data = self._read_data()
                except Exception as e:
                    logging.exception("virtual_sdcard read,err:%s"%str(e))
                    error_message = str(e)
                    try:
                        self.gcode.run_script(self.on_error_gcode.render())
                    except:
                        logging.exception("virtual_sdcard on_error")
                    break
                except:
                    logging.exception("virtual_sdcard read")
                    break
                if not data:
                    # End of file
                    self.current_file.close()
                    self.current_file = None
                    logging.info("Finished SD card print")
                    self.gcode.respond_raw("Done printing file")
                    break
                lines = data.split(b'\n')
                lines[0] = partial_input + lines[0]
                partial_input = lines.pop()
                lines.reverse()
                self.reactor.pause(self.reactor.NOW)
                continue
            # Pause if any other request is pending in the gcode class
            if gcode_mutex.test():
                self.reactor.pause(self.reactor.monotonic() + 0.100)
                continue
            # Dispatch command
            self.cmd_from_sd = True
            line = lines.pop()
            next_file_position = self.file_position + len(line) + 1
            self.next_file_position = next_file_position
            try:
                if not has_pre_check:
                    has_pre_check = True
                    #filament check
                    filament_sensor = self.printer.lookup_object("filament_switch_sensor filament_sensor")
                    runout_helper = filament_sensor.runout_helper
                    if runout_helper.check_to_pause():
                        break
                if locate_printing_gcode is not None:
                    # 记录gcode_缓冲
                    locate_printing_gcode.record_gcode_begin(line.decode('utf-8'), self.file_position)
                self.gcode.run_script(line.decode('utf-8'))
                if locate_printing_gcode is not None:
                    locate_printing_gcode.record_gcode_end()
            except self.gcode.error as e:
                error_message = str(e)
                if "exclude_object over" in str(e):
                    self.file_position = self.file_size
                    try:
                        self.gcode.run_script(self.exclude_object_gcode.render())
                    except:
                        logging.exception("exclude_object gcode running error") 
                    self.current_file.seek(self.file_position)
                    error_message = None
                    continue
                try:
                    self.gcode.run_script(self.on_error_gcode.render())
                except:
                    logging.exception("virtual_sdcard on_error")
                break
            except:
                logging.exception("virtual_sdcard dispatch")
                break
            self.cmd_from_sd = False
            self.file_position = self.next_file_position
            # Do we need to skip around?
            if self.next_file_position != next_file_position:
                try:
                    self.current_file.seek(self.file_position)
                except:
                    logging.exception("virtual_sdcard seek")
                    self.work_timer = None
                    return self.reactor.NEVER
                lines = []
                partial_input = ""
        logging.info("Exiting SD card print (position %d)", self.file_position)
        self.work_timer = None
        self.cmd_from_sd = False
        if error_message is not None:
            self.print_stats.note_error(error_message)
        elif self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.print_stats.note_complete()
            #flsun add, run END_PRINT when end a print
            self.gcode.run_script_from_command("END_PRINT")

        return self.reactor.NEVER

def load_config(config):
    return VirtualSD(config)
