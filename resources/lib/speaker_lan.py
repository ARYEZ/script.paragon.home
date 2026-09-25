# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The wire to a beacon: a Raspberry Pi with a speaker on it, running
tools/paragon_speaker.py, that plays a named audio file when asked, and can
be paused, resumed, turned up and down, and asked what it is doing.

Our own protocol, so it is as small as it can be made, and deliberately in the
shape of the ones already here:

    UDP  8765   discovery -- a hello goes out by broadcast, and each speaker
                answers with who it is and which clips it holds
    HTTP 8766   commands  -- GET /clips says what it can play,
                             GET /status says what it is doing,
                             POST /play {"clip": name} plays one,
                             POST /stop ends whatever is playing,
                             POST /pause, /resume, /volume {"volume": n}

One thing plays at a time on a speaker: starting a clip stops the last one,
and the speaker says which. So an announcement lands when it was sent, not
after whatever album was on, and there is a way to end an hour of music
that was started by mistake.

A clip is a file in a folder on the Pi, named by its filename with the
extension dropped: drop "hardboiled complete.wav" in the folder and the next
device search offers "hardboiled complete". Nothing is learned and nothing is
uploaded, which is the point -- the Pi that plays the message is the one place
the message lives.

The clip list rides along in the discovery reply rather than being fetched
afterwards, so one broadcast is one complete answer: a refresh is what picks
up a file added to the folder. A datagram has room for a few dozen names,
which is more clips than a room wants.
"""

import json
import socket
import time

from compat import HTTPError, Request, URLError, to_native, to_text, urlopen
from govee_lan import local_addresses

DISCOVERY_PORT = 8765
COMMAND_PORT = 8766
BROADCAST_ADDRESS = '255.255.255.255'

HELLO = b'PARAGON_SPEAKER?'

# What a reply must say to be one of ours. Anything else on the port -- and a
# home network has plenty on it -- is not a speaker, however well-formed.
KIND = 'speaker'

# How many times the hello goes out and how long the whole search waits. UDP
# has no retransmission, so one broadcast is one chance; three is enough that
# a Pi busy playing something still gets asked.
BROADCAST_ROUNDS = 3


class SpeakerError(Exception):
    """The speaker could not be reached, or refused the request."""


def parse_hello(data, ip):
    """A speaker's answer to the hello, as a dict, or None if it is not one.

    Strict about the shape because the port is a broadcast port and the reply
    is plain JSON: anything that happens to answer with JSON must not turn
    into a device with no clips and no name.
    """
    try:
        body = json.loads(to_text(data))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or body.get('paragon') != KIND:
        return None
    device_id = (body.get('id') or '').strip().upper()
    if not device_id:
        return None
    try:
        port = int(body.get('port') or COMMAND_PORT)
    except (TypeError, ValueError):
        port = COMMAND_PORT
    return {
        'id': device_id,
        'name': (body.get('name') or '').strip(),
        'ip': ip,
        'port': port,
        'clips': clean_clip_names(body.get('clips')),
        # Which of those are songs. Empty from a Pi on the older script,
        # which has no songs folder -- its clips are all there is.
        'songs': clean_clip_names(body.get('songs')),
    }


def clean_clip_names(raw):
    """The usable names in a clip list, in order, without repeats or blanks."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen, kept = set(), []
    for item in raw:
        try:
            name = to_text(item).strip()
        except Exception:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        kept.append(name)
    return kept


def clean_status(raw):
    """A beacon's /status answer, in the shape the rest of the add-on reads.

    Strict about types because this is shown on a phone as numbers: an
    elapsed time that is a string is a progress bar that does nothing.

    The seconds into the clip are 'elapsed', not 'position': a position is
    how far open a blind is, and one word for both would have a beacon read
    as 62% open.
    """
    if not isinstance(raw, dict):
        return None

    def number(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    playing = raw.get('playing')
    playing = to_text(playing).strip() if playing else None
    volume = number(raw.get('volume'))
    return {
        'playing': playing or None,
        'paused': bool(raw.get('paused')) if playing else False,
        'elapsed': number(raw.get('elapsed')) if playing else None,
        'duration': number(raw.get('duration')) if playing else None,
        'volume': int(round(volume)) if volume is not None else None,
        # Whether pause and volume will work on this one: mpv or not.
        'controls': bool(raw.get('controls')),
        # Bytes free for clips on the Pi. None from a Pi running a script
        # older than the question, which is every Pi until it is copied over.
        'free': _bytes(raw.get('free')),
    }


def _bytes(value):
    """A whole, non-negative number of bytes, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class SpeakerTransport(object):
    """Discovery by broadcast, commands by HTTP."""

    def __init__(self, bind_address='', timeout=5.0, log_func=None):
        self.bind_address = bind_address or ''
        self.timeout = float(timeout or 5.0)
        self._log = log_func or (lambda message: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0, targets=None):
        """Send the hello, collect the answers. Returns a list of dicts.

        `targets` is for a test that wants to reach a speaker on loopback
        rather than the whole subnet. Left alone, the hello goes out by
        broadcast from every local address, for the reason the other searches
        do: a Kodi box with a VPN or a container bridge has a default route
        that is often not the one the speakers are on.
        """
        if targets is None:
            targets = [(BROADCAST_ADDRESS, DISCOVERY_PORT)]
        addresses = [self.bind_address] if self.bind_address \
            else list(local_addresses())
        if not addresses:
            addresses = ['']

        found = {}
        deadline = time.time() + max(0.5, float(timeout))
        sockets = []
        try:
            for address in addresses:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.bind((address, 0))
                    sock.setblocking(False)
                    sockets.append(sock)
                except socket.error as exc:
                    sock.close()
                    self._log('Speaker search: could not open a socket on %s: '
                              '%s' % (address or 'default route', exc))
            if not sockets:
                raise SpeakerError('No network interface to search from')

            # The hello is sent again at intervals through the window rather
            # than once at the start, and the sockets stay open the whole
            # time: a reply comes back to the port the hello left from.
            gap = max(0.2, float(timeout) / BROADCAST_ROUNDS)
            next_send = 0.0
            while True:
                now = time.time()
                if now >= next_send:
                    for sock in sockets:
                        for target in targets:
                            try:
                                sock.sendto(HELLO, target)
                            except socket.error as exc:
                                self._log('Speaker search: send to %s:%s '
                                          'failed: %s' % (target[0],
                                                          target[1], exc))
                    next_send = now + gap
                for sock in sockets:
                    while True:
                        try:
                            data, sender = sock.recvfrom(8192)
                        except socket.error:
                            break
                        entry = parse_hello(data, sender[0])
                        if entry:
                            found[entry['id']] = entry
                if now >= deadline:
                    break
                time.sleep(0.05)
        finally:
            for sock in sockets:
                sock.close()

        self._log('Speaker search found %d speaker(s)' % len(found))
        return list(found.values())

    # -- commands ----------------------------------------------------------

    def _call(self, ip, port, path, payload=None):
        """One HTTP exchange, returning the JSON it answered with."""
        url = 'http://%s:%d%s' % (ip, int(port or COMMAND_PORT), path)
        body = None
        headers = {'Accept': 'application/json'}
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf8'
        request = Request(to_native(url), data=body,
                          headers=dict((k, to_native(v))
                                       for k, v in headers.items()))
        try:
            handle = urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            detail = ''
            try:
                detail = to_text(exc.read())[:200]
            except Exception:
                pass
            try:
                # The listener says why in JSON; use that when it did.
                detail = json.loads(detail).get('error') or detail
            except (ValueError, AttributeError):
                pass
            raise SpeakerError('%s answered HTTP %d%s'
                               % (ip, exc.code, ': %s' % detail if detail
                                  else ''))
        except URLError as exc:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, socket.timeout) or \
                    'timed out' in str(reason).lower():
                raise SpeakerError('%s did not answer within %.0fs'
                                   % (ip, self.timeout))
            raise SpeakerError('Could not reach %s: %s' % (ip, reason))
        except socket.timeout:
            raise SpeakerError('%s did not answer within %.0fs'
                               % (ip, self.timeout))
        try:
            raw = handle.read()
        finally:
            try:
                handle.close()
            except Exception:
                pass
        try:
            return json.loads(to_text(raw))
        except ValueError:
            raise SpeakerError('%s sent something that is not JSON' % ip)

    def listing(self, ip, port=COMMAND_PORT):
        """What the speaker can play right now, asked directly: every
        name it will play, and which of those are songs."""
        answer = self._call(ip, port, '/clips')
        if not isinstance(answer, dict):
            raise SpeakerError('%s sent a reply with no clips in it' % ip)
        return {'clips': clean_clip_names(answer.get('clips')),
                'songs': clean_clip_names(answer.get('songs'))}

    def clips(self, ip, port=COMMAND_PORT):
        return self.listing(ip, port)['clips']

    def shuffle(self, ip, port=COMMAND_PORT):
        """Play a song the beacon picks at random. Returns its name.

        The Pi picks, from what is in its songs folder now, so a song copied
        on this morning is in the draw without a search.
        """
        try:
            answer = self._call(ip, port, '/play', {'shuffle': True})
        except SpeakerError as exc:
            if 'HTTP 400' in str(exc):
                # The older script reads this as a play with no clip named.
                raise SpeakerError(
                    '%s does not know about songs yet: copy the new '
                    'paragon_speaker.py onto it' % ip)
            raise
        name = answer.get('playing') if isinstance(answer, dict) else None
        if not isinstance(answer, dict) or not answer.get('ok') or not name:
            raise SpeakerError('%s did not play a song' % ip)
        return to_text(name)

    def play(self, ip, port, name):
        """Play one clip. Returns as soon as the speaker has started it.

        Not waiting for the clip to finish is deliberate. A step that plays a
        ten-second message and then switches a plug off should not sit out
        the ten seconds unless it was told to -- and if it was, that is what
        a pause on the step is for, and it works the same as everywhere else.
        """
        answer = self._call(ip, port, '/play', {'clip': name})
        if not isinstance(answer, dict) or not answer.get('ok'):
            raise SpeakerError('%s did not play "%s"' % (ip, name))
        return True

    def status(self, ip, port=COMMAND_PORT):
        """What the beacon is doing now. See clean_status for the shape."""
        answer = clean_status(self._call(ip, port, '/status'))
        if answer is None:
            raise SpeakerError('%s sent a reply with no status in it' % ip)
        return answer

    def _simple(self, ip, port, path, payload, verb):
        answer = self._call(ip, port, path, payload)
        if not isinstance(answer, dict) or not answer.get('ok'):
            raise SpeakerError('%s did not %s' % (ip, verb))
        return answer

    def pause(self, ip, port=COMMAND_PORT):
        """Pause what is playing. A beacon with nothing playing, or one
        without mpv, answers 409 with why, and that comes back as the
        error."""
        self._simple(ip, port, '/pause', {}, 'pause')
        return True

    def resume(self, ip, port=COMMAND_PORT):
        self._simple(ip, port, '/resume', {}, 'resume')
        return True

    def set_volume(self, ip, port, volume):
        """0 to 100. Returns the volume the beacon settled on."""
        answer = self._simple(ip, port, '/volume',
                              {'volume': int(volume)}, 'take the volume')
        try:
            return int(answer.get('volume'))
        except (TypeError, ValueError):
            return int(volume)

    def stop(self, ip, port=COMMAND_PORT):
        """End whatever the speaker is playing. Returns its name, or None.

        None is not a failure: a stop when nothing was playing is a stop
        that has nothing to do, and a sequence that ends with one should not
        report a fault for arriving to silence.
        """
        # An empty body rather than none: urllib sends a request with no
        # data as a GET, and /stop is a POST. A stop that arrived as a GET
        # would be answered 404 and read here as a dead speaker.
        answer = self._call(ip, port, '/stop', {})
        if not isinstance(answer, dict) or not answer.get('ok'):
            raise SpeakerError('%s did not stop' % ip)
        stopped = answer.get('stopped')
        return to_text(stopped) if stopped else None
