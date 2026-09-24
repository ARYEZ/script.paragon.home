#!/usr/bin/env python3
"""A stand-in for mpv, speaking enough of its JSON IPC to drive a beacon.

tools/paragon_speaker.py starts mpv with --input-ipc-server=<socket> and
talks to it in JSON, one request per line. This answers the way mpv does --
including sending an event of its own in among the answers, which the
script has to step over -- and keeps the state a real player would: what is
loaded, whether it is paused, how far in, and the volume.

What it "played" is written to $FAKE_MPV_LOG, one path per line, and its
pid to $FAKE_MPV_PID, so a test can see what reached the player and whether
the player was ended with the script.
"""

import json
import os
import socket
import sys
import time

DURATION = 180.0


def main(argv):
    sock_path = None
    for arg in argv:
        if arg.startswith('--input-ipc-server='):
            sock_path = arg.split('=', 1)[1]
    if not sock_path:
        sys.exit(2)
    if os.environ.get('FAKE_MPV_PID'):
        with open(os.environ['FAKE_MPV_PID'], 'w') as handle:
            handle.write('%d\n' % os.getpid())

    state = {'path': None, 'paused': False, 'volume': 100.0,
             'started': None, 'paused_at': None}

    def position():
        if state['path'] is None:
            return None
        end = state['paused_at'] if state['paused'] else time.time()
        return end - state['started']

    def log(path):
        if os.environ.get('FAKE_MPV_LOG'):
            with open(os.environ['FAKE_MPV_LOG'], 'a') as handle:
                handle.write(path + '\n')

    def handle(command):
        name = command[0] if command else ''
        if name == 'loadfile':
            state['path'] = command[1]
            state['started'] = time.time()
            state['paused'] = False
            state['paused_at'] = None
            log(command[1])
            return 'success', None, {'event': 'file-loaded'}
        if name == 'stop':
            state['path'] = None
            log('<stop>')
            return 'success', None, {'event': 'end-file'}
        if name == 'set_property':
            prop, value = command[1], command[2]
            if prop == 'pause':
                if state['path'] is not None:
                    if value and not state['paused']:
                        state['paused_at'] = time.time()
                    elif not value and state['paused']:
                        state['started'] += time.time() - state['paused_at']
                state['paused'] = bool(value)
                return 'success', None, None
            if prop == 'volume':
                state['volume'] = float(value)
                return 'success', None, None
            return 'property not found', None, None
        if name == 'get_property':
            prop = command[1]
            if prop == 'idle-active':
                return 'success', state['path'] is None, None
            if prop == 'volume':
                return 'success', state['volume'], None
            if prop == 'pause':
                return 'success', state['paused'], None
            if state['path'] is None:
                return 'property unavailable', None, None
            if prop == 'path':
                return 'success', state['path'], None
            if prop == 'time-pos':
                return 'success', position(), None
            if prop == 'duration':
                return 'success', DURATION, None
            return 'property not found', None, None
        return 'invalid parameter', None, None

    try:
        os.unlink(sock_path)
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    while True:
        conn, _addr = server.accept()
        buffer = b''
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    try:
                        request = json.loads(raw.decode('utf-8'))
                    except ValueError:
                        continue
                    error, data, event = handle(request.get('command') or [])
                    if event:
                        # As mpv does: an event lands on the socket on its
                        # own, before the answer to what caused it.
                        conn.sendall((json.dumps(event) + '\n')
                                     .encode('utf-8'))
                    answer = {'error': error,
                              'request_id': request.get('request_id')}
                    if data is not None:
                        answer['data'] = data
                    conn.sendall((json.dumps(answer) + '\n').encode('utf-8'))
        except OSError:
            pass
        finally:
            conn.close()


if __name__ == '__main__':
    main(sys.argv[1:])
