# Copyright (c) 2024,郑州潮阔电子科技有限公司
# All rights reserved.
#
# 文件名称：locate_printing_gcode.py
# 摘    要：T1添加断料恢复时，回退到合适的位置打印，避免缝隙大。
#          禅道bug 3738 【1.0.1.0】报断料检测后进料，继续打印的模型出现明显层纹
#
# 当前版本：1.0
# 作    者：郭夫华
# 完成日期：2024年12月04日
#
# 修订记录：

from collections import deque
import logging


class RollbackCheckZChange():
    def __init__(self):
        # 是否锁定Z高度
        self.lock = False
        # 打印高度
        self.printing_height = 0.0
        self.nLoop = 0

    def is_break(self, item, triggerPrintLen):
        self.nLoop += 1
        # 读取gcode文件的时间
        start_pos = item['gcode_move_before']['gcode_position']
        end_pos = item['gcode_move_after']['gcode_position']
        # 判断是否有Z移动
        threshold = .000000001
        delta_Z = end_pos.z - start_pos.z
        move_d = abs(delta_Z)
        # 判断是否有挤出
        delta_E = end_pos.e - start_pos.e
        move_e = abs(end_pos.e - start_pos.e)
        # 没有Z变化
        if move_d < threshold:
            # 挤出机正向变化,锁定Z高度
            if (delta_E > threshold) and (not self.lock):
                self.lock = True
                self.printing_height = end_pos.z
            # Z没有变化,继续回滚
            return False
        # 有Z变化
        else:
            # 下降
            if delta_Z < 0:
                # 是否锁定
                if self.lock:
                    # 目标位置低于打印高度,避免撞模型,退出
                    if end_pos.z < self.printing_height:
                        logging.info(f"[RollbackCheckZChange] avoid collision with other objects : {start_pos} -> {end_pos}, trigger print:{triggerPrintLen}")
                        return True
            else:
                # 存在换层,避免碰撞模型,退出
                if start_pos.z < self.printing_height:
                    logging.info(f"[RollbackCheckZChange] change level Z: {self.printing_height} -> {start_pos.z} avoid collision with other objects, trigger print:{triggerPrintLen}")
                    return True

            # 有抬升或者下降在范围内,判断是否有挤出
            if move_e > threshold:
                # Gcode的Z和E不应该同时动
                logging.info(f"[RollbackCheckZChange] abnormal move : {start_pos} -> {end_pos}, trigger print:{triggerPrintLen}")
                return True
            else:
                return False

    def debug_str(self):
        return (
            f"[RollbackCheckZChange] nLoop:{self.nLoop}, "
            f"lock:{self.lock}, "
            f"printing_height:{self.printing_height}"
        )


class LocatePrintingGcode():
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode_move = self._lookup_required_module('gcode_move')
        self.virtual_sd = self._lookup_required_module('virtual_sdcard')
        self.gcode = self._lookup_required_module('gcode')
        # 队列缓存的大小
        self.max_size = config.getint('queue_size', 500, minval=10)
        # 挤出机离断料开关的长度
        self.e_offset = config.getfloat('e_offset', 13.5, minval=0.)
        # 抵消不缺料情况下,因暂停时重力流失的料,设置补偿目标值,正常情况回退gcode不超过该值
        self.gravity_target = config.getfloat('gravity_target', 0.0, minval=0.)
        # 抵消不缺料情况下,因暂停时重力流失的料,设置补偿最小值,优先级高于目标值
        self.gravity_min = config.getfloat('gravity_min', 0.0, minval=0.)
        self.queue = deque(maxlen=self.max_size)
        self.parseinfo = dict()
        self.saved_states = {}
        # 是否已经回退了
        self.is_roolback = False
        logging.info(f"[LocatePrintingGcode]__init__:status:{self.get_status()}")
        self.gcode.register_command(
            "F109", self.cmd_F109, desc=self.cmd_F109_help)

    cmd_F109_help = "Set locate_printing_gcode module config"

    def cmd_F109(self, gcmd):
        params = gcmd.get_command_parameters()
        try:
            if 'S' in params:
                new_max_size = gcmd.get_int('S')
                cur_size = len(self.queue)
                if new_max_size == self.max_size:
                    pass
                elif new_max_size < cur_size:
                    if new_max_size < 0:
                        raise gcmd.error(
                            f"Invalid queue size in '{gcmd.get_commandline()}'")
                    # 创建一个新的 deque，设置新的最大长度
                    new_queue = deque(maxlen=new_max_size)
                    # 从旧 deque 的末尾开始，将最近的元素复制到新的 deque 中
                    new_queue.extend(list(self.queue)[-new_max_size:])
                    # 替换旧的 deque
                    self.queue = new_queue
                    # 更新 max_size 属性
                    self.max_size = new_max_size
                    logging.info(f"[LocatePrintingGcode]shrink_queue_capacity: new capacity {new_max_size}")
                else:
                    # 创建一个新的 deque，设置新的最大长度
                    new_queue = deque(maxlen=new_max_size)
                    # 将旧 deque 中的内容复制到新的 deque 中
                    new_queue.extend(self.queue)
                    # 替换旧的 deque
                    self.queue = new_queue
                    # 更新 max_size 属性
                    self.max_size = new_max_size
                    logging.info(f"[LocatePrintingGcode]change queue max_size: {new_max_size}")

            if 'E' in params:
                new_e_offset = gcmd.get_float('E')
                self.e_offset = new_e_offset
                logging.info(f"[LocatePrintingGcode]change e_offset: {new_e_offset}")

            if 'N' in params:
                new_min = gcmd.get_float('N')
                self.gravity_min = new_min
                logging.info(f"[LocatePrintingGcode]change gravity_min: {new_min}")

            if 'T' in params:
                new_target = gcmd.get_float('T')
                self.gravity_target = new_target
                logging.info(f"[LocatePrintingGcode]change gravity_target: {new_target}")

        except ValueError as e:
            raise gcmd.error("Unable to parse move '%s'"
                             % (gcmd.get_commandline(),))

    # 保存et时间的实时位置
    def save_runing_info(self, state_name, et):
        runing_info = {}
        runing_info['eventtime'] = et
        motion_report = self.printer.lookup_object('motion_report')
        runing_info['motion_report'] = motion_report.get_status(et)
        self.saved_states[state_name] = runing_info

    # 恢复到时间发生时的状态
    def restore_runing_info(self, state_name):
        # 从状态表中取出状态，取出后自动清空
        runing_info = self.saved_states.pop(state_name, None)
        # 设置恢复信息
        if runing_info is not None:
            self.set_resume_pos(runing_info)

    def _lookup_required_module(self, module_name):
        module = self.printer.lookup_object(module_name, None)
        if module is None:
            raise self.gcode.error(f"not found {module_name} module_name)")
        return module

    # 记录每行gcode对应的位置信息
    def record_gcode_begin(self, line, file_position):
        self.parseinfo = {}
        self.parseinfo['gcmd'] = line
        self.parseinfo['time'] = self.reactor.monotonic()
        self.parseinfo['offset'] = file_position
        self.parseinfo['gcode_move_before'] = self.gcode_move.get_status()
        self.parseinfo['gcode_state'] = self.gcode_move.get_gcode_state()

    # 记录挤出机位置信息
    def record_gcode_end(self):
        if not self.parseinfo:
            return
        self.parseinfo['gcode_move_after'] = self.gcode_move.get_status()
        self.queue.append(self.parseinfo)
        self.parseinfo = {}

    # 不缺料的情况下,判断是否继续补偿, e_offset为当前gcode补偿的料长
    def check_rollback(self, e_offset):
        # 不缺料的情况下,避免料因重力自动流失造成的缺料,补偿最少为min,接近target的长度,
        # 如target为6, min为3,每行gcode使用耗材为1.1的话,则回退到5.5的
        # 行,接近6,但不超过6,假如每行gcode使用耗材为4.4,回退一次到2,再退
        # 一次到6.4,则从6.4处开始打印
        if e_offset < self.gravity_min:
            if e_offset < self.gravity_target:
                # 小于目标值,且小于最小值,则未达到要求,继续
                return 1
            else:
                # 超过目标值,小于最小值,无效情况,退出
                pass
        else:
            if e_offset < self.gravity_target:
                # 已超过最小值,保存最接近目标值的信息
                return 1
            else:
                # 已超过最小值,且超过目标值,则退出,不再使用
                return 0
        return -1

    # 恢复到断料时的位置，尽量恢复，会进行一系列安全判断
    def set_resume_pos(self, runout_info):
        logging.info(f"[LocatePrintingGcode]set_resume_pos:runout_info{runout_info}")
        et = runout_info.get('eventtime')
        motion_report = runout_info.get('motion_report')
        if (et is None or motion_report is None):
            return

        live_pos = motion_report['live_position']
        if live_pos is None:
            return

        # 检查队列是否为空
        if not self.queue:
            logging.info("[LocatePrintingGcode] Queue is empty")
            return

        # 根据挤出机流量定位gcode
        extrude_locate = {}
        # 获取挤出机位置
        posE = live_pos[3]
        # 挤出机缺料的位置
        posELack = posE + self.e_offset

        checkZ = RollbackCheckZChange()

        # 是否已经回退了
        self.is_roolback = False
        compensate_gravity_value = 0.
        nLoop = 0
        # 循环是否继续
        loop_continue = True
        for item in reversed(self.queue):
            nLoop += 1
            # 读取gcode文件的时间
            start_pos = item['gcode_move_before']['position']
            end_pos = item['gcode_move_after']['position']
            remainLen = posELack - end_pos[3]
            deltaE = end_pos[3] - start_pos[3]
            triggerPrintLen = end_pos[3] - posE

            # 如果检测到Z变动，则循环就可以退出
            if checkZ.is_break(item, triggerPrintLen):
                loop_continue = False

            # 行尾不缺料
            if remainLen > 0:
                # 不缺料时才退出，如果缺料，就继续回退
                if not loop_continue:
                    logging.info(f"check Z false, roolback {nLoop} stop: filament {compensate_gravity_value} mm, trigger print:{triggerPrintLen}")
                    break

                if self.gravity_min > 0.0:
                    # 不缺料的情况下,避免料因重力自动流失造成的缺料,补偿最少为min,接近target的长度
                    compensate_gravity_value += deltaE
                    check_result = self.check_rollback(
                        compensate_gravity_value)
                    if check_result < 0:
                        logging.info(f"compensate gravity roolback {nLoop} break: filament {compensate_gravity_value} mm, trigger print:{triggerPrintLen}")
                        break
                    elif check_result == 0:
                        # 一次未保存过, 且需要回退,则回退第一行
                        if not self.is_roolback:
                            self.is_roolback = True
                            extrude_locate = item
                            logging.info(f"compensate gravity roolback {nLoop} first: filament {compensate_gravity_value} mm, trigger print:{triggerPrintLen}")
                            break
                else:
                    # 不考虑重力流失补偿
                    logging.info(f"gravity_min:{self.gravity_min}, not config")
                    break
            else:
                # 行尾缺料
                # 行开始时缺料
                if posELack < start_pos[3]:
                    # 行结束位置已经超过事件起始位置,该行gcode是正常打印,不能再回滚了!
                    if end_pos[3] < posE:
                        # 该行未发生任何异常，从此行退出，不再回看
                        logging.info(f"roolback {nLoop} lines, something unexpected happened !! trigger print:{triggerPrintLen}, loss {remainLen}")
                        break
                else:
                    # 行开始时不缺料
                    # 该行缺料
                    lastLinePrint = 0
                    if deltaE > 0 and remainLen < 0:
                        lastLinePrint = 100 * \
                            (posELack - start_pos[3]) / deltaE
                    extrude_locate = item
                    logging.info(f"roolback {nLoop} lines locate loss {remainLen} filament gcode, progress {lastLinePrint}, trigger print:{triggerPrintLen}")
                    break
            # 继续回滚判断E位置变化
            if deltaE > 0:
                self.is_roolback = True
                extrude_locate = item

        logging.info(f"set_resume_pos roolback {nLoop} lines, locate:{extrude_locate}")

        if len(extrude_locate) > 0:
            self.virtual_sd.set_resume_file_position(extrude_locate['offset'])
            self.gcode_move.set_SAVE_GCODE_STATE(
                'PAUSE_STATE', extrude_locate['gcode_state'])

    def get_status(self, eventtime=None):
        return {
            'max_size': self.max_size,
            'gravity_target': self.gravity_target,
            'gravity_min': self.gravity_min,
            'queue_size': len(self.queue),
            'e_offset': self.e_offset
        }


def load_config(config):
    logging.info(f"locate_printing_gcode load_config")
    return LocatePrintingGcode(config)
