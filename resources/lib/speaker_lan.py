# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The wire to a Paragon speaker: a Raspberry Pi with a speaker on it, running
tools/paragon_speaker.py, that plays a named audio file when asked.

Our own protocol, so it is as small as it can be made, and deliberately in the
shape of the ones already here:

    UDP  8765   discovery -- a hello goes out by broadcast, and each speaker
                answers with who it is and which clips it holds
    HTTP 8766   commands  -- GET /clips says what it can play,
                             POST /play {"clip": name} plays one

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

    def clips(self, ip, port=COMMAND_PORT):
        """What the speaker can play right now, asked directly."""
        answer = self._call(ip, port, '/clips')
        if not isinstance(answer, dict):
            raise SpeakerError('%s sent a reply with no clips in it' % ip)
        return clean_clip_names(answer.get('clips'))

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
