#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

paragon_speaker.py -- runs on a Raspberry Pi with a speaker plugged in: a
beacon. It plays a named audio file when Paragon Home asks, and can be
paused, resumed, turned up and down, and asked what it is doing.

    python3 paragon_speaker.py --name "Kitchen" --folder ~/aurora

Drop audio files in the folder. Each one becomes a clip named by its filename
with the extension dropped -- "hardboiled complete.wav" is the clip
"hardboiled complete" -- and shows up in Paragon Home's step picker after the
next Refresh devices. There is nothing to learn and nothing to upload: the Pi
that plays the message is the one place the message lives.

Songs go in a folder of their own, "songs" inside the clip folder unless
--songs says otherwise, so the phrases stay a short list and one
`scp -r aurora` still copies both. A song is a clip like any other and can be
played by name; a step can also ask for one at random, which this Pi picks
from what is in the songs folder at that moment -- never the one it picked
last time, when there is another to choose.

Two things listen:

    UDP  8765   discovery. Paragon Home broadcasts PARAGON_SPEAKER? and this
                answers with who it is and what it can play.
    HTTP 8766   GET  /clips              -> {"clips": [...],
                                             "songs": [...]}
                GET  /status             -> {"playing": name or null,
                                             "paused", "elapsed",
                                             "duration", "volume",
                                             "controls"}
                POST /play {"clip": n}   -> {"ok": true, "stopped": prev},
                                            having started it
                POST /play {"shuffle": true}
                                         -> the same, and "playing": the
                                            song it picked
                POST /stop               -> {"ok": true, "stopped": name}
                POST /pause              -> {"ok": true, "paused": true}
                POST /resume             -> {"ok": true, "paused": false}
                POST /volume {"volume"}  -> {"ok": true, "volume": n}

One thing plays at a time. Starting a clip stops whatever was playing, and
says which, so "the eggs are done" lands when it was sent rather than after
an hour of whatever album was on. Playback starts and the request returns;
it does not wait for the clip to end. A pause on the step in Paragon Home is
how two clips are kept apart, the same way a pause spaces anything else.

Stopping the listener stops the player with it: a service restart must not
leave an hour of music running with nothing left that can end it.

Playback is mpv, kept running for the life of this script and driven over
its control socket: that is what makes pause, volume and "how far in" a
question this can answer. Install it with `sudo apt install mpv`. Without
it this falls back to whatever player is present -- aplay for WAV, mpg123
for MP3 -- and can play and stop, and nothing more; /status says so in
"controls", and Paragon Home offers only the buttons that will work.
Everything else is the standard library.

The volume is kept in a dotfile in the clip folder, so the kitchen set to
40 is still 40 after a reboot.

The clip name in a request is never joined onto a path. The folder is listed
and the name looked up in what was found, so a request for "../../etc/passwd"
is a request for a clip that does not exist, and nothing more.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import random
import tempfile
import threading
import time
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

    def free_bytes(self):
        """Room left for clips: the free space on the drive the folder is
        on, which on a Pi is the SD card. None if it cannot be read -- a
        folder that is not there yet, say."""
        try:
            return shutil.disk_usage(self.folder).free
        except OSError:
            return None


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


class ShellPlayer(object):
    """One process per clip: whatever player is installed, started and ended.

    What this script did before mpv. Kept as the fallback for a Pi that has
    not had mpv installed yet: it plays and it stops, and it cannot pause,
    cannot be turned down, and cannot say how far in it is.
    """

    controls = False
    # Starting a clip does not end the last one by itself: two processes
    # would play over each other. The speaker stops first.
    replaces = False

    def __init__(self, override=None, log=print):
        self.override = override
        self.log = log
        self._lock = threading.Lock()
        self._proc = None

    def start(self):
        return True

    def can_play(self, path):
        return player_for(path, self.override) is not None

    def play(self, path):
        """Start it. Returns None, or a reason it could not."""
        command = player_for(path, self.override)
        if command is None:
            return 'nothing installed can play %s' % os.path.basename(path)
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as exc:
            return 'could not start %s: %s' % (command[0], exc)
        with self._lock:
            self._proc = proc
        return None

    def playing(self):
        """Whether the player is still going. A finished one is let go."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
            return self._proc is not None

    def stop(self):
        """Terminate, then kill: a player that ignores the polite request
        for half a second gets the other one. Nothing here waits longer,
        because the request that asked is still open."""
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.5)
        except OSError:
            pass
        return True

    def pause(self):
        return 'this beacon cannot pause: install mpv'

    resume = pause

    def set_volume(self, volume):
        return 'this beacon has no volume control: install mpv'

    def status(self):
        return {'paused': False, 'elapsed': None, 'duration': None,
                'volume': None}

    def close(self):
        self.stop()


class MpvPlayer(object):
    """One mpv for the life of the script, driven over its control socket.

    mpv is started idle with no file and told what to do in JSON, one
    request per line, on a unix socket it creates: load this file, pause,
    volume 40, where are you. Answers come back on the same socket, with
    the request id they answer, in among the events mpv sends on its own.

    The volume is a property of the player, so it holds across clips; it is
    written to a dotfile as well so it holds across a restart of this
    script too.
    """

    controls = True
    # "loadfile ... replace" ends whatever was playing itself, with no gap
    # of silence and no second command on the wire.
    replaces = True

    def __init__(self, command, folder, log=print):
        self.command = list(command)
        self.folder = folder
        self.log = log
        self.socket_path = os.path.join(
            tempfile.gettempdir(), 'paragon-beacon-%d.sock' % os.getpid())
        self.volume_file = os.path.join(folder, '.paragon-volume')
        self._proc = None
        self._sock = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._buffer = b''

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Start mpv and connect. False, with a log line, if it cannot."""
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        argv = self.command + ['--idle=yes', '--no-video', '--no-terminal',
                               '--really-quiet', '--keep-open=no',
                               '--input-ipc-server=%s' % self.socket_path]
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.log('Could not start %s: %s' % (self.command[0], exc))
            return False
        deadline = time.time() + 10
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self.log('%s exited at once (code %s)'
                         % (self.command[0], self._proc.returncode))
                return False
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
            except OSError:
                sock.close()
                time.sleep(0.1)
                continue
            sock.settimeout(2.0)
            self._sock = sock
            self.set_volume(self._saved_volume())
            return True
        self.log('%s never opened its control socket' % self.command[0])
        self.close()
        return False

    def close(self):
        with self._lock:
            sock, self._sock = self._sock, None
            proc, self._proc = self._proc, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    # -- the wire ----------------------------------------------------------

    def _ask(self, *command):
        """Send one command and return its answer's data, or raise."""
        with self._lock:
            if self._sock is None:
                raise OSError('mpv is not running')
            request_id = self._next_id
            self._next_id += 1
            line = json.dumps({'command': list(command),
                               'request_id': request_id}) + '\n'
            self._sock.sendall(line.encode('utf-8'))
            deadline = time.time() + 2.0
            while time.time() < deadline:
                while b'\n' in self._buffer:
                    raw, self._buffer = self._buffer.split(b'\n', 1)
                    try:
                        answer = json.loads(raw.decode('utf-8'))
                    except ValueError:
                        continue
                    if answer.get('request_id') != request_id:
                        continue  # an event, or an answer to something else
                    if answer.get('error') != 'success':
                        raise MpvError(answer.get('error') or 'error')
                    return answer.get('data')
                chunk = self._sock.recv(65536)
                if not chunk:
                    raise OSError('mpv closed the socket')
                self._buffer += chunk
            raise OSError('mpv did not answer')

    def _property(self, name, default=None):
        try:
            return self._ask('get_property', name)
        except MpvError:
            # "property unavailable" is mpv for "no file loaded".
            return default

    # -- what a beacon does --------------------------------------------------

    def can_play(self, path):
        return True

    def play(self, path):
        try:
            self._ask('loadfile', path, 'replace')
            self._ask('set_property', 'pause', False)
        except (OSError, MpvError) as exc:
            return 'mpv would not play it: %s' % exc
        return None

    def playing(self):
        return not self._property('idle-active', True)

    def stop(self):
        if not self.playing():
            return False
        try:
            self._ask('stop')
        except (OSError, MpvError):
            return False
        return True

    # Whether anything is playing is the speaker's question, asked once,
    # there -- see Speaker.pause. A second copy here would be two guards
    # covering each other, and neither one tested.

    def pause(self):
        try:
            self._ask('set_property', 'pause', True)
        except (OSError, MpvError) as exc:
            return 'mpv would not pause: %s' % exc
        return None

    def resume(self):
        try:
            self._ask('set_property', 'pause', False)
        except (OSError, MpvError) as exc:
            return 'mpv would not resume: %s' % exc
        return None

    def set_volume(self, volume):
        try:
            self._ask('set_property', 'volume', volume)
        except (OSError, MpvError) as exc:
            return 'mpv would not take the volume: %s' % exc
        try:
            with open(self.volume_file, 'w') as handle:
                handle.write('%d\n' % volume)
        except OSError:
            pass
        return None

    def _saved_volume(self):
        try:
            with open(self.volume_file) as handle:
                return clamp_volume(handle.read().strip())
        except (OSError, ValueError):
            return 100

    def status(self):
        return {
            'paused': bool(self._property('pause', False)),
            'elapsed': self._property('time-pos'),
            'duration': self._property('duration'),
            'volume': self._property('volume'),
        }


class MpvError(Exception):
    """mpv answered, and the answer was no."""


def clamp_volume(value):
    """0 to 100, whole numbers. Raises ValueError for anything else."""
    number = int(round(float(value)))
    return max(0, min(100, number))


def build_player(mpv_command, folder, override=None, log=print):
    """mpv if it is there, the shell players if not. Says which."""
    if mpv_command:
        player = MpvPlayer(mpv_command, folder, log=log)
        if player.start():
            log('Playing through %s: pause and volume available'
                % mpv_command[0])
            return player
    log('mpv is not installed: play and stop only. '
        'sudo apt install mpv for pause and volume.')
    return ShellPlayer(override, log=log)


class Speaker(object):
    """The state both listeners share."""

    def __init__(self, name, folder, port, device_id=None, log=print,
                 player=None, songs=None):
        self.name = name
        self.folder = ClipFolder(folder)
        # The songs, in a folder of their own: inside the clip folder unless
        # told otherwise, so one copy of the clip folder brings both.
        self.songs = ClipFolder(songs or os.path.join(self.folder.folder,
                                                      'songs'))
        # The song a shuffle picked last, so the next one is a different
        # one whenever there is another to pick.
        self._shuffled = None
        self.port = port
        self.device_id = device_id or stable_id()
        self.log = log
        # Whatever plays: an MpvPlayer or a ShellPlayer. Set by main once
        # the folder exists; a test hands one in.
        self.player = player
        # The clip that was last started, so status and stop can name it.
        # Guarded, because the HTTP handler and the shutdown path both
        # touch it.
        self._lock = threading.Lock()
        self._playing = None

    def now_playing(self):
        """The clip playing right now, or None. A finished one is let go."""
        with self._lock:
            if self._playing is not None and not self.player.playing():
                self._playing = None
            return self._playing

    def stop(self):
        """End whatever is playing. Returns the clip name, or None."""
        with self._lock:
            name, self._playing = self._playing, None
        stopped = self.player.stop()
        if not stopped or name is None:
            return None
        self.log('Stopped "%s"' % name)
        return name

    def pause(self):
        """Returns None, or the reason it did not."""
        if self.now_playing() is None:
            return 'nothing is playing'
        return self.player.pause()

    def resume(self):
        if self.now_playing() is None:
            return 'nothing is playing'
        return self.player.resume()

    def set_volume(self, volume):
        return self.player.set_volume(volume)

    def status(self):
        """What this beacon is doing, for /status."""
        playing = self.now_playing()
        report = {'playing': playing, 'controls': self.player.controls,
                  'paused': False, 'elapsed': None, 'duration': None,
                  'volume': None}
        report.update(self.player.status())
        report['free'] = self.folder.free_bytes()
        if playing is None:
            report['paused'] = False
            report['elapsed'] = None
            report['duration'] = None
        return report

    def song_names(self):
        """The songs, less any a phrase of the same name hides."""
        phrases = set(self.folder.names())
        return [name for name in self.songs.names() if name not in phrases]

    def all_names(self):
        """Everything that can be played by name: the phrases, then the
        songs. One list, so a Paragon Home that has never heard of songs
        can still play every one of them."""
        return self.folder.names() + self.song_names()

    def path_for(self, name):
        """The file behind a name, phrases first. Looked up in what the
        two folders hold, never joined onto either."""
        return self.folder.path_for(name) or self.songs.path_for(name)

    def pick_song(self):
        """A song at random, or None when the folder has none. Not the one
        picked last time, when there is another -- "random" that plays the
        same song twice running reads as broken."""
        names = self.song_names()
        if len(names) > 1 and self._shuffled in names:
            names.remove(self._shuffled)
        if not names:
            return None
        self._shuffled = random.choice(names)
        return self._shuffled

    def hello(self):
        return json.dumps({
            'paragon': 'speaker',
            'id': self.device_id,
            'name': self.name,
            'port': self.port,
            'clips': self.all_names(),
            'songs': self.song_names(),
        }).encode('utf-8')

    def play(self, name):
        """Start a clip, stopping whatever was playing.

        Returns (HTTP status, message, what was stopped or None).

        404 is "no such clip" and nothing else. A clip that is there but
        cannot be played is a fault on this Pi, not a name Paragon Home got
        wrong, and the two need different responses -- one is fixed by a
        search, the other by apt-get. Neither of those stops what is playing:
        a request that could not be honoured should not cost the music.
        """
        path = self.path_for(name)
        if path is None:
            return 404, 'no clip called "%s"' % name, None
        if not self.player.can_play(path):
            return 503, 'nothing installed can play %s' % \
                os.path.basename(path), None
        if self.player.replaces:
            stopped = self.now_playing()
            with self._lock:
                self._playing = None
            if stopped:
                self.log('Stopped "%s"' % stopped)
        else:
            stopped = self.stop()
        problem = self.player.play(path)
        if problem:
            return 503, problem, stopped
        with self._lock:
            self._playing = name
        self.log('Playing "%s"' % name)
        return 200, 'playing', stopped


class Handler(BaseHTTPRequestHandler):
    """GET /clips and /status; POST /play, /stop, /pause, /resume and
    /volume. Everything else is 404."""

    def _send(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        speaker = self.server.speaker
        if path == '/clips':
            self._send(200, {'clips': speaker.all_names(),
                             'songs': speaker.song_names()})
        elif path == '/status':
            self._send(200, speaker.status())
        else:
            self._send(404, {'error': 'no such path'})

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length).decode('utf-8') or '{}')
        except (ValueError, TypeError):
            return None
        return body if isinstance(body, dict) else None

    def do_POST(self):
        path = self.path.split('?')[0]
        speaker = self.server.speaker
        if path == '/stop':
            stopped = speaker.stop()
            self._send(200, {'ok': True, 'stopped': stopped})
            return
        if path in ('/pause', '/resume'):
            problem = speaker.pause() if path == '/pause' \
                else speaker.resume()
            if problem:
                # 409: the request was fine and the beacon is not in a
                # state to do it. Not a 404, which would read as a dead
                # beacon, and not a 503, which would read as a broken one.
                self._send(409, {'ok': False, 'error': problem})
                return
            self._send(200, {'ok': True, 'paused': path == '/pause'})
            return
        if path == '/volume':
            body = self._body()
            try:
                volume = clamp_volume((body or {}).get('volume'))
            except (ValueError, TypeError):
                self._send(400, {'ok': False,
                                 'error': 'send JSON with a "volume" 0-100'})
                return
            problem = speaker.set_volume(volume)
            if problem:
                self._send(409, {'ok': False, 'error': problem})
                return
            self._send(200, {'ok': True, 'volume': volume})
            return
        if path != '/play':
            self._send(404, {'error': 'no such path'})
            return
        body = self._body()
        if body is None:
            self._send(400, {'error': 'send JSON with a "clip" in it'})
            return
        if body.get('shuffle') is True:
            name = speaker.pick_song()
            if name is None:
                # 404, like a clip that is not there: the fix is the same --
                # put some files in the folder.
                self._send(404, {'ok': False, 'error': 'no songs in %s'
                                 % speaker.songs.folder, 'stopped': None})
                return
        else:
            name = str(body.get('clip') or '').strip()
        if not name:
            self._send(400, {'error': 'no clip named'})
            return
        status, message, stopped = speaker.play(name)
        ok = status == 200
        self._send(status, {'ok': ok, 'error': '' if ok else message,
                            'stopped': stopped, 'playing': name if ok
                            else None})

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
    parser.add_argument('--songs', default=None,
                        help='where the songs are (default: a "songs" '
                             'folder inside --folder)')
    parser.add_argument('--port', type=int, default=COMMAND_PORT,
                        help='HTTP port to take commands on (default: %d)'
                             % COMMAND_PORT)
    parser.add_argument('--discovery-port', type=int, default=DISCOVERY_PORT,
                        help='UDP port to answer searches on (default: %d)'
                             % DISCOVERY_PORT)
    parser.add_argument('--id', default=None,
                        help='override the device id (default: this '
                             'machine\'s MAC address)')
    parser.add_argument('--mpv', default='mpv',
                        help='the mpv to play through (default: mpv on the '
                             'PATH). Given something that is not there, '
                             'this falls back to play-and-stop.')
    parser.add_argument('--player', default=None,
                        help='without mpv: play every clip with this '
                             'command instead of choosing by file type')
    args = parser.parse_args(argv)

    speaker = Speaker(args.name, args.folder, args.port, args.id,
                      songs=args.songs)
    os.makedirs(speaker.folder.folder, exist_ok=True)
    os.makedirs(speaker.songs.folder, exist_ok=True)
    speaker.player = build_player(args.mpv.split() if args.mpv else None,
                                  speaker.folder.folder,
                                  override=args.player.split()
                                  if args.player else None,
                                  log=speaker.log)
    names = speaker.folder.names()
    speaker.log('%s: %d clip(s) in %s' % (speaker.name, len(names),
                                          speaker.folder.folder))
    for name in names:
        speaker.log('  %s' % name)
    if not names:
        speaker.log('  (drop .wav or .mp3 files in that folder)')
    songs = speaker.song_names()
    speaker.log('%d song(s) in %s' % (len(songs), speaker.songs.folder))

    stop = threading.Event()
    discovery = threading.Thread(target=serve_discovery,
                                 args=(speaker, args.discovery_port, stop))
    discovery.daemon = True
    discovery.start()

    server = HTTPServer(('', args.port), Handler)
    server.speaker = speaker
    speaker.log('Taking commands on HTTP %d' % args.port)

    # systemctl stop sends SIGTERM, and Python's answer to SIGTERM is to exit
    # on the spot without running a finally -- which would leave the player
    # running with nothing left that can stop it. Turned into the same unwind
    # Ctrl+C gets, so a service restart ends the music the way a keyboard
    # would.
    def _terminated(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _terminated)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        # A restart of this service must not orphan an hour of music with
        # nothing left running that can stop it.
        speaker.stop()
        speaker.player.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
