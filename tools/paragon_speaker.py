#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

paragon_speaker.py -- runs on a Raspberry Pi with a speaker plugged in, and
plays a named audio file when Paragon Home asks.

    python3 paragon_speaker.py --name "Kitchen Speaker" --folder ~/clips

Drop audio files in the folder. Each one becomes a clip named by its filename
with the extension dropped -- "hardboiled complete.wav" is the clip
"hardboiled complete" -- and shows up in Paragon Home's step picker after the
next Refresh devices. There is nothing to learn and nothing to upload: the Pi
that plays the message is the one place the message lives.

Two things listen:

    UDP  8765   discovery. Paragon Home broadcasts PARAGON_SPEAKER? and this
                answers with who it is and what it can play.
    HTTP 8766   GET  /clips            -> {"clips": [...]}
                POST /play {"clip": n} -> {"ok": true}, having started it

Playback starts and the request returns; it does not wait for the clip to
end. A pause on the step in Paragon Home is how two clips are kept apart,
the same way a pause spaces anything else.

Standard library only, so it runs on a fresh Raspberry Pi OS with nothing
installed. Playback shells out to whichever player is present: aplay for
WAV, mpg123 for MP3, ffplay for anything else it can find.

The clip name in a request is never joined onto a path. The folder is listed
and the name looked up in what was found, so a request for "../../etc/passwd"
is a request for a clip that does not exist, and nothing more.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

DISCOVERY_PORT = 8765
COMMAND_PORT = 8766
HELLO = b'PARAGON_SPEAKER?'

# What counts as a clip, and which player each kind wants. Order matters only
# for the fallback: ffplay handles all of them, if it is installed.
PLAYERS = (
    ('.wav', ['aplay', '-q']),
    ('.mp3', ['mpg123', '-q']),
    ('.ogg', ['ogg123', '-q']),
    ('.flac', ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet']),
)
FALLBACK = ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet']


def stable_id():
    """Something that survives a reboot and tells two Pis apart: the MAC."""
    node = uuid.getnode()
    return ':'.join('%02X' % ((node >> shift) & 0xFF)
                    for shift in range(40, -1, -8))


class ClipFolder(object):
    """The clips on disk, by name, read fresh each time they are asked for."""

    def __init__(self, folder):
        self.folder = os.path.abspath(os.path.expanduser(folder))

    def scan(self):
        """{name: path} for every playable file. Listed, never guessed."""
        found = {}
        try:
            entries = sorted(os.listdir(self.folder))
        except OSError:
            return found
        for entry in entries:
            path = os.path.join(self.folder, entry)
            if not os.path.isfile(path):
                continue
            stem, ext = os.path.splitext(entry)
            if not stem or ext.lower() not in dict(PLAYERS) \
                    and ext.lower() not in ('.m4a', '.aac'):
                continue
            found.setdefault(stem, path)
        return found

    def names(self):
        return sorted(self.scan())

    def path_for(self, name):
        """The file behind a clip name, or None. The lookup is the guard."""
        return self.scan().get(name)


def player_for(path, override=None):
    """The command that plays this file, or None if nothing installed can.

    `override` is a player to use for everything, given on the command line:
    for an odd setup, or for a test that wants to see which file was handed
    over rather than hear it.
    """
    if override:
        return list(override) + [path]
    ext = os.path.splitext(path)[1].lower()
    for wanted, command in PLAYERS:
        if ext == wanted and shutil.which(command[0]):
            return command + [path]
    if shutil.which(FALLBACK[0]):
        return FALLBACK + [path]
    return None


class Speaker(object):
    """The state both listeners share."""

    def __init__(self, name, folder, port, device_id=None, log=print,
                 player=None):
        self.name = name
        self.folder = ClipFolder(folder)
        self.port = port
        self.device_id = device_id or stable_id()
        self.log = log
        self.player = player

    def hello(self):
        return json.dumps({
            'paragon': 'speaker',
            'id': self.device_id,
            'name': self.name,
            'port': self.port,
            'clips': self.folder.names(),
        }).encode('utf-8')

    def play(self, name):
        """Start a clip. Returns (HTTP status, message).

        404 is "no such clip" and nothing else. A clip that is there but
        cannot be played is a fault on this Pi, not a name Paragon Home got
        wrong, and the two need different responses -- one is fixed by a
        search, the other by apt-get.
        """
        path = self.folder.path_for(name)
        if path is None:
            return 404, 'no clip called "%s"' % name
        command = player_for(path, self.player)
        if command is None:
            return 503, 'nothing installed can play %s' % \
                os.path.basename(path)
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            return 503, 'could not start %s: %s' % (command[0], exc)
        self.log('Playing "%s"' % name)
        return 200, 'playing'


class Handler(BaseHTTPRequestHandler):
    """GET /clips and POST /play. Everything else is 404."""

    def _send(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split('?')[0] == '/clips':
            self._send(200, {'clips': self.server.speaker.folder.names()})
            return
        self._send(404, {'error': 'no such path'})

    def do_POST(self):
        if self.path.split('?')[0] != '/play':
            self._send(404, {'error': 'no such path'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
            name = str(body.get('clip') or '').strip()
        except (ValueError, TypeError):
            self._send(400, {'error': 'send JSON with a "clip" in it'})
            return
        if not name:
            self._send(400, {'error': 'no clip named'})
            return
        status, message = self.server.speaker.play(name)
        ok = status == 200
        self._send(status, {'ok': ok, 'error': '' if ok else message})

    def log_message(self, fmt, *args):
        # Quiet: one line per play from Speaker.play is plenty.
        pass


def serve_discovery(speaker, port, stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', port))
    sock.settimeout(0.5)
    speaker.log('Answering discovery on UDP %d' % port)
    while not stop.is_set():
        try:
            data, sender = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if data.strip() != HELLO:
            continue
        try:
            sock.sendto(speaker.hello(), sender)
        except OSError as exc:
            speaker.log('Could not answer %s: %s' % (sender[0], exc))
    sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Play named audio clips when Paragon Home asks.')
    parser.add_argument('--name', required=True,
                        help='what this speaker is called in Paragon Home, '
                             'e.g. "Kitchen Speaker"')
    parser.add_argument('--folder', default='~/paragon-clips',
                        help='where the audio files are (default: '
                             '~/paragon-clips)')
    parser.add_argument('--port', type=int, default=COMMAND_PORT,
                        help='HTTP port to take commands on (default: %d)'
                             % COMMAND_PORT)
    parser.add_argument('--discovery-port', type=int, default=DISCOVERY_PORT,
                        help='UDP port to answer searches on (default: %d)'
                             % DISCOVERY_PORT)
    parser.add_argument('--id', default=None,
                        help='override the device id (default: this '
                             'machine\'s MAC address)')
    parser.add_argument('--player', default=None,
                        help='play every clip with this command instead of '
                             'choosing by file type, e.g. "mpv --no-video"')
    args = parser.parse_args(argv)

    speaker = Speaker(args.name, args.folder, args.port, args.id,
                      player=args.player.split() if args.player else None)
    os.makedirs(speaker.folder.folder, exist_ok=True)
    names = speaker.folder.names()
    speaker.log('%s: %d clip(s) in %s' % (speaker.name, len(names),
                                          speaker.folder.folder))
    for name in names:
        speaker.log('  %s' % name)
    if not names:
        speaker.log('  (drop .wav or .mp3 files in that folder)')

    stop = threading.Event()
    discovery = threading.Thread(target=serve_discovery,
                                 args=(speaker, args.discovery_port, stop))
    discovery.daemon = True
    discovery.start()

    server = HTTPServer(('', args.port), Handler)
    server.speaker = speaker
    speaker.log('Taking commands on HTTP %d' % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
