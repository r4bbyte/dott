# vim: set tabstop=4 expandtab :
###############################################################################
#   Copyright (c) 2019-2021 ams AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
###############################################################################

# Authors:
# - Thomas Winkler, ams AG, thomas.winkler@ams.com

import logging
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Dict, Union
from typing import List

import dottmi.target_mem
from dottmi.breakpointhandler import BreakpointHandler
from dottmi.dottexceptions import DottException
from dottmi.gdb import GdbClient, GdbServer
from dottmi.gdb_mi import NotifySubscriber
from dottmi.symbols import BinarySymbols
from dottmi.utils import cast_str, log

logging.basicConfig(level=logging.DEBUG)


class Target(NotifySubscriber):

    def __init__(self, gdb_server: GdbServer, gdb_client: GdbClient, auto_connect: bool = True) -> None:
        """
        Creates a target which represents a target device. It requires both a GDB server (either started by DOTT
        or started externally) and a GDB client instance used to connect to the GDB server.
        If auto_connect is True (the default) the connected from GDB client to GDB server is automatically established.
        """
        NotifySubscriber.__init__(self)
        self._load_elf_file_name = None
        self._symbol_elf_file_name = None

        self._gdb_client: GdbClient = gdb_client
        self._gdb_server: GdbServer = gdb_server

        # condition variable and status flag used to implement helpers
        # allowing callers to wait until target is stopped or running
        self._cv_target_running: threading.Condition = threading.Condition()
        self._is_target_running: bool = False

        # instantiate delegates
        self._symbols: BinarySymbols = BinarySymbols(self)
        self._mem: dottmi.target_mem.TargetMem = dottmi.target_mem.TargetMemNoAlloc(self)

        # start breakpoint handler
        self._bp_handler: BreakpointHandler = BreakpointHandler()
        self._gdb_client.gdb_mi.response_handler.notify_subscribe(self._bp_handler, 'stopped', 'breakpoint-hit')
        self._bp_handler.start()

        # register to get notified if the target state changes
        self._gdb_client.gdb_mi.response_handler.notify_subscribe(self, 'stopped', None)
        self._gdb_client.gdb_mi.response_handler.notify_subscribe(self, 'running', None)

        # delay after device startup / continue
        self._startup_delay: float = 0.0

        # flag which indicates if gdb client is attached to target
        self._gdb_client_is_connected = False

        if auto_connect:
            self.gdb_client_connect()

    def gdb_client_connect(self) -> None:
        """
        Connects the GDB client instance to the GDB server.
        """
        if self._gdb_server is None:
            raise DottException('No GDB server instance set. If you disconnected the GDB client and now try to connect '
                                'it again you may need to create and set a new GDB server instance (because DOTT '
                                'auto-launches JLINK GDB server in singlerun mode.')

        try:
            self.exec('-gdb-set mi-async on', timeout=5)
            self.exec(f'-target-select remote {self._gdb_server.addr}:{self._gdb_server.port}', timeout=5)
            self.cli_exec('set mem inaccessible-by-default off', timeout=1)
        except Exception as ex:
            raise ex

        # source script with custom GDB commands (custom Python commands executed in GDB context)
        my_dir = Path(__file__).absolute().parent
        gdb_script_file = my_dir.joinpath('./gdb_cmds.py')

        # note: GDB expects paths to be POSIX-formatted.
        gdb_script_file = str(PurePosixPath(gdb_script_file))
        self.cli_exec(f'source {gdb_script_file}')

        self._gdb_client_is_connected = True

    def gdb_client_disconnect(self) -> None:
        """
        Disconnects the GDB client from the GDB server. The target is not resumed.
        Since DOTT auto-launches JLINK GDB server in 'singlerun' mode, the GDB server process terminates when the
        the GDB client disconnects. The internal state is updated accordingly by calling gdb_server_stop().
        This means that the user is required to create and set a new GDB server instance before connecting the
        the target's gdb client again.
        Important: For reasons of consistency, this is also done that way if the GDB server was not started by
                   DOTT. Also in this case, a new GDB server object needs to be creates and set.
        """
        if self._gdb_client_is_connected:
            self.cli_exec('disconnect')
            self._gdb_client_is_connected = False
            self.gdb_server_stop()

    def gdb_server_stop(self) -> None:
        """
        Stops the GDB server. The GDB client must have been disconnected before.
        """
        if self._gdb_client_is_connected:
            raise DottException("Can not terminate GDB server while client is connected. Disconnect client first!")
        if self._gdb_server is not None:
            self._gdb_server.shutdown()
            self._gdb_server = None

    def gdb_server_set(self, gdb_server: GdbServer) -> None:
        """
        Allows to set a gdb server instance for the target. Can only be called if there is no gdb server instance
        set so far or the current gdb server instance has been stopped.
        """
        if self._gdb_server is not None:
            raise DottException('Can not set GDB server is there currently is an instance in place. Stop the current'
                                'GDB server before setting a new one!')

        self._gdb_server = gdb_server

    def disconnect(self) -> None:
        """
        Disconnect first closes the GDB client connection and then terminates the GDB server. The target is not resumed.
        After calling disconnect, the target instance can no longer be used (i.e., there is not reconnect).
        """
        if self._gdb_client is not None:
            self.exec_noblock('-gdb-exit')
            self._gdb_client.gdb_mi.shutdown()
            self._bp_handler.stop()
            self._gdb_client = None
            self._gdb_client_is_connected = False
        if self._gdb_server is not None:
            self._gdb_server.shutdown()
            self._gdb_server = None


    ###############################################################################################
    # Properties

    @property
    def gdb_client(self):
        return self._gdb_client

    @property
    def symbols(self) -> BinarySymbols:
        return self._symbols

    @property
    def mem(self) -> 'TargetMem':
        if self._mem is None:
            raise DottException('No on-target memory access model set at this point!')
        return self._mem

    @mem.setter
    def mem(self, target_mem: 'TargetMem'):
        if not isinstance(target_mem, dottmi.target_mem.TargetMem):
            raise DottException('mem has to be an instance of TargetMem')
        self._mem = target_mem

    @property
    def bp_handler(self) -> BreakpointHandler:
        return self._bp_handler

    @property
    def byte_order(self) -> str:
        import dottmi.dott
        return dottmi.dott.DottConf.get('device_endianess')

    @property
    def startup_delay(self) -> float:
        return self._startup_delay

    @startup_delay.setter
    def startup_delay(self, delay: float):
        self._startup_delay = delay

    ###############################################################################################
    # General-purpose wrappers for on target command execution/evaluation

    def eval(self, expr: str) -> Union[int, float, bool, str, None]:
        """
        This method takes an expression to be evaluated. It is assumed that the target is halted when calling eval.
        An expression is every valid expression in the current program context such as registers, local or global
        variables or functions.
        For example:
          t.eval('$sp')  # returns content of stack pointer register
          t.eval('my_var')  # returns content of local variable my_var
          t.eval('*my_ptr_var')  # dereferences a local pointer variable
          t.eval('glob_var += 1')   # increments a global variable
          t.eval('my_func(99)')  # calls function my_fund with argument 99 and returns its result
        The eval function attempts to convert the result of the evaluation into a suitable python data type.

        Args:
            expr: The expression to be evaluation in the current context of the target.

        Returns:
            The evaluation result converted to a suitable Python data type.
        """
        res = self.exec(f'-data-evaluate-expression "{expr}"')
        if res is None:
            log.warn(f'Eval of {expr} did not succeed (return value is None)!')
            return None

        res = res['payload']['value']
        ret_val = cast_str(res)

        if '<optimized out>' in str(ret_val):
            log.warn(f'Accessed entity {expr} is optimized out in the target binary.')

        return ret_val

    def exec(self, cmd: str, timeout: float = None) -> Dict:
        return self._gdb_client.gdb_mi.write_blocking(cmd, timeout=timeout)

    def exec_noblock(self, cmd: str) -> int:
        return self._gdb_client.gdb_mi.write_non_blocking(cmd)

    def cli_exec(self, cmd: str, timeout: float = None) -> Dict:
        return self._gdb_client.gdb_mi.write_blocking(f'-interpreter-exec console "{cmd}"', timeout=timeout)

    ###############################################################################################
    # Execution-related target commands

    def load(self, load_elf_file_name: str, symbol_elf_file_name: str = None, enable_flash: bool = False) -> None:
        self._load_elf_file_name = load_elf_file_name
        self._symbol_elf_file_name = symbol_elf_file_name

        if load_elf_file_name is not None:
            self.exec(f'-file-exec-file {self._load_elf_file_name}')
        if symbol_elf_file_name is not None:
            self.exec(f'-file-symbol-file')  # note: -file-symbol-file without arguments clears GDB's symbol table
            self.exec(f'-file-symbol-file {self._symbol_elf_file_name}')

        self.cli_exec(f'monitor flash device {self._gdb_server.device_id}')

        if enable_flash:
            self.cli_exec('monitor flash download=1')

        if load_elf_file_name is not None:
            self.exec('-target-download')

    def reset(self, flush_reg_cache: bool = True) -> None:
        self.cli_exec('monitor reset')
        if flush_reg_cache:
            self.reg_flush_cache()

    def cont(self) -> None:
        # wait until we get a notification that the target actually is running
        num_tries = 40
        with self._cv_target_running:
            while not self.is_running() and num_tries > 0:
                self.exec_noblock('-exec-continue')
                self._cv_target_running.wait(timeout=0.1)
                num_tries -= 1
        if num_tries <= 0:
            raise Exception('Target execution could not be continued!')
        else:
            time.sleep(self._startup_delay)

    def ret(self, ret_val: Union[int, str] = None) -> None:
        if ret_val is None:
            self.cli_exec('--exec-return')
        else:
            # note: we are relying on the cli here since the MI command '-exec-return' does not support return values
            self.cli_exec(f'return {ret_val}')

    def halt(self) -> None:
        # wait until we get a notification that the target actually is stopped
        num_tries = 20
        with self._cv_target_running:
            while self.is_running() and num_tries > 0:
                self.exec_noblock('-exec-interrupt --all')
                self._cv_target_running.wait(timeout=0.1)
                num_tries -= 1
        if num_tries <= 0:
            raise Exception('Target execution could not be halted!')

    def step(self):
        self.exec('-exec-next')
        while self.is_running():
            pass

    def step_inst(self):
        self.exec('-exec-next-instruction')
        while self.is_running():
            pass

    ###############################################################################################
    # Status-related target commands

    # This callback function is called from from gdbmi response handler when a new notification
    # with at target status change notification is received.
    def _notify_callback(self):
        notify_msg = self.wait_for_notification()['message']

        with self._cv_target_running:
            if 'stopped' in notify_msg:
                self._is_target_running = False
                self._cv_target_running.notify_all()
            elif 'running' in notify_msg:
                self._is_target_running = True
                self._cv_target_running.notify_all()

    def is_running(self):
        with self._cv_target_running:
            return self._is_target_running

    ###############################################################################################
    # Breakpoint-related target commands

    def bp_clear_all(self) -> None:
        self.cli_exec('dott-bp-nostop-delete')
        self.exec('-break-delete')
        self.cli_exec('monitor clrbp')

    def bp_get_count(self) -> int:
        res = self.exec('-break-list')
        cnt = int(res['payload']['BreakpointTable']['nr_rows'])
        return cnt

    def _bp_get_list(self) -> []:
        res = self.exec('-break-list')
        bp_list = res['payload']['BreakpointTable']['body']
        return bp_list

    ###############################################################################################
    # Register-related target commands

    def reg_get_content(self, fmt: str = 'x', regs: List = None) -> Dict:
        if regs is None:
            regs = []
        res = self.exec('-data-list-register-values --skip-unavailable %s %s' % (fmt, ' '.join(str(r) for r in regs)))
        return res['payload']['register-values']

    def reg_get_names(self, regs: List = None) -> Dict:
        if regs is None:
            regs = []
        res = self.exec('-data-list-register-names %s' % ' '.join(str(r) for r in regs))
        return res['payload']['register-names']

    def reg_get_changed(self) -> Dict:
        res = self.exec('-data-list-changed-registers')
        return res['payload']['changed-registers']

    def reg_flush_cache(self) -> None:
        """
        Flush GDB's internal register cache. This command is useful if the target's state was changed in a way that
        is outside the control/awareness of GDB.
        """
        self.cli_exec('flushregs')
