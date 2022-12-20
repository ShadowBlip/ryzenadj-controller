#!/usr/sbin/python3
import logging
import os
import signal
import socket
import sys
import warnings
from asyncio import all_tasks, CancelledError, coroutine, create_task, current_task, ensure_future, get_event_loop, sleep, start_unix_server

from support import supported_devices

logging.basicConfig(format='[%(asctime)s | %(filename)s:%(lineno)s:%(funcName)s] %(message)s',
                    datefmt='%y%m%d_%H:%M:%S',
                    level=logging.DEBUG
                    )

logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore', category=DeprecationWarning)
RYZENADJ_DELAY = 0.5

class RyzenControl:
    cpu = None
    performance_selected = '--power-saving'
    performance_set = None
    running = False
    set_tctl = 95
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
        logger.debug(f'found {self.cpu}')
        if self.cpu not in supported_devices:
            logger.error('{self.cpu} is not supported.')
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

        logger.info(f'Unix socket opened at {self.socket}')
        self.loop.run_forever()

    @coroutine
    async def handle_message(self, reader, writer):
        raw_data = await reader.read(4096)
        data = raw_data.decode('utf-8').strip().split()
        logger.debug(f'{data}')
        if data:
            result = self.handle_command(data)
            logger.info(result)
            writer.write(bytes(result, 'utf-8'))

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

    async def check_tctl_set(self):

        # Check if this model is one that will not set tctl properly
        if self.cpu in [
                'AMD Ryzen 5 5560U with Radeon Graphics',
                ]:
            logger.info(f'{self.cpu} does not support tctl setting. Skipping automatic tctl management.')
            return

        while self.running:
            # Ensure safe temp ctl settings. Ensures OXP devices dont fry themselves.
            run = self.do_adjust('-i').splitlines()
            tctl = [i for i in run if 'THM LIMIT CORE' in i][0].split()[5]
            if tctl != f'{self.set_tctl}.000':
                logger.info(f'found tctl set to {tctl}')
                logger.info(self.do_adjust(f'-f {self.set_tctl}').strip())

            await sleep(RYZENADJ_DELAY)

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

if __name__ == '__main__':

    RyzenControl = RyzenControl()

    server = RyzenControl.start_server_task(start_unix_server, RyzenControl.handle_message)
