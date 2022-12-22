import logging
import os
import signal
import warnings
from asyncio import (all_tasks, CancelledError, coroutine, create_task,
                     current_task, get_event_loop, start_unix_server)

from .support import SUPPORTED_DEVICES

logging.basicConfig(format='[%(asctime)s | %(filename)s:%(lineno)s:%(funcName)s] %(message)s',
                    datefmt='%y%m%d_%H:%M:%S',
                    level=logging.DEBUG
                    )

logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore', category=DeprecationWarning)

class RyzenControl:
    cpu = None
    running = False
    socket = '/tmp/ryzenadj_socket'
    valid_commands = []
    def __init__(self):
        logger.info('ryzenadj-control service started')
        self.check_ryzen_installed()
        self.check_supported()
        self.task = None
        self.running = True
        self.get_valid_commands()
        self.loop = get_event_loop()

        for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            self.loop.add_signal_handler(s, lambda s=s: create_task(self.stop_loop(self.loop)))

    # Verify RyzenAdj is installed.
    def check_ryzen_installed(self):
        if not os.path.exists("/usr/bin/ryzenadj"):
            logger.error('RyzenAdj is not installed.')
            exit(1)

    # Checks the systems reported CPU against the database of supported devices
    def check_supported(self):
        command = 'lscpu | grep "Model name" | grep -v "BIOS" | cut -d : -f 2 | xargs'
        self.cpu = os.popen(command).read().strip()
        logger.debug('Found cpu: %s', self.cpu)
        if self.cpu not in SUPPORTED_DEVICES:
            logger.error('%s is not supported.', self.cpu)
            exit(1)

    # Have RyzenAdj report all valid comands from help file.
    def get_valid_commands(self):
        run = os.popen('ryzenadj -h', 'r', 1).read().splitlines()
        for raw_command in run:

            # Break up the commands from the description text
            trunc_command = raw_command.split()

            # Some commands have two methods of calling on te same line.
            for i in range(2):
                # Handle edge cases
                if i > len(trunc_command) -1 or len(trunc_command) == 0:
                    continue
                # Valid commands start with -
                if '-' in trunc_command[i][0]:
                    # Some commands have an = sign
                    if not '=' in trunc_command[i]:
                        # Append the command after formatting. Gets rid of spaces, newlines, extra commas.
                        self.valid_commands.append(trunc_command[i].strip().replace(',', ''))
                        continue
                    # Append the command after formatting. Gets rid of spaces, newlines, extra commas, =.
                    self.valid_commands.append(trunc_command[i].split('=')[0].strip().replace(',', ''))

    # Check if a given command is supported.
    def is_valid_command(self, raw_command):
        if raw_command in self.valid_commands:
            return True
        return False

    def start_server_task(self, Task, handler):
        unix_server = Task(handler, path=self.socket)

        self.loop.create_task(unix_server)

        logger.info('Unix socket opened at %s', self.socket)
        self.loop.run_forever()

    @coroutine
    async def handle_message(self, reader, writer):
        raw_data = await reader.read(4096)
        data = raw_data.decode('utf-8').strip().split()
        logger.debug("DATA: %s", data)
        if data:
            result = self.handle_command(data)
            logger.info(result)
            writer.write(result.encode('utf-8'))

    def handle_command(self, message):
        check_command = message[0]
        if '=' in message[0]:
            check_command = message[0].split('=')[0]
            arg = message[0].split('=')[1]
            if not arg.isdigit():
                return f'Error: Invalid argument {arg} for command {check_command}'
        if not self.is_valid_command(check_command):
            return f'Error: Got invalid command: {check_command}'
        if len(message) > 2:
            return f'Error: {message[0]} called with too many arguments'
        if len(message) == 1:
            return self.do_adjust(message[0])
        if len(message) == 2:
            return self.do_adjust(message[0], message[1])

    def do_adjust(self, command, *args):
        ryzenadj_command = f'ryzenadj {command}'
        if args:
            ryzenadj_command = f'ryzenadj {command} {args[0]}'
        run = os.popen(ryzenadj_command, 'r', 1).read().strip()
        return run

    async def stop_loop(self, loop):

        # Kill all tasks. They are infinite loops so we will wait forver.
        logger.info('Kill signal received. Shutting down.')
        self.running = False
        for task in [t for t in all_tasks() if t is not current_task()]:
            task.cancel()
            try:
                await task
            except CancelledError:
                pass
        loop.stop()
        logger.info('ryzenadj-control service stopped.')


def main():
    ryzen_control = RyzenControl()
    ryzen_control.start_server_task(start_unix_server, RyzenControl.handle_message)
