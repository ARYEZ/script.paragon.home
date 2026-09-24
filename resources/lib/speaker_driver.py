# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The beacon driver: a Raspberry Pi with a speaker on it, as a Paragon device.

A beacon is a blaster that emits sound. It has nothing to switch; what it
has is a set of named things it can emit, and a step that says "emit this
one on that device". So it claims the capability the Broadlink claims, and
its clip names are its commands. Everything that already knows what to do
with a command -- the step picker, the sequence editor, the speed dial, a
phrase, the web remote's command action, the last-run record -- works on a
beacon without learning that it is one.

It is also a player, which a blaster is not: it can say what it is playing
and how far in, be paused and resumed, and be turned up and down. That is
CAP_PLAYBACK, and the three verbs go through the Hub like every other
verb. It reports state, so a beacon that gave no reading is looked for the
way a light is.

The one difference from the blaster: a code is learned into this add-on and
kept here, whereas a clip is a file on the Pi. So the clip list is read from
the device at each search and kept in speaker_clips.json, keyed by device --
the same shape as the codes, so it travels to satellites the same way and the
menus can offer clips without a network call. Adding a message to a room is
copying a file onto that room's Pi and pressing Refresh devices.
"""

from devices import (CAP_COMMANDS, CAP_PLAYBACK, CAP_STATE, ControlError,
                     Device)
from speaker_lan import COMMAND_PORT, SpeakerError, SpeakerTransport

CLIP_FILE = 'speaker_clips.json'

# The one command every speaker has that is not a clip. Offered first in the
# list, so it is a step, a dial slot and a phrase like any clip is -- "Aurora,
# stop the music" costs nothing more than any other phrase. A clip that
# happens to be named this is hidden rather than fought over; the verb wins,
# and the README says not to name one that.
STOP = 'Stop'


class SpeakerDriver(object):
    """Finds speakers and plays their clips."""

    # The id stays 'speaker': it is in every devices.json and every step
    # that names one. The word people see is Beacon.
    DRIVER_ID = 'speaker'
    DRIVER_LABEL = 'Beacon'

    def __init__(self, transport=None, clips=None, save_clips=None,
                 log_func=None):
        self.transport = transport or SpeakerTransport(log_func=log_func)
        # {device_id: [clip names]} -- the very dict the app persists, so a
        # search that finds a new file saves it without a round trip.
        self.clips = clips if clips is not None else {}
        self._log = log_func or (lambda message: None)
        self._save_clips = save_clips or (lambda: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        devices = []
        try:
            found = self.transport.discover(timeout=timeout)
        except Exception as exc:
            return [], ['Speaker search failed: %s' % exc]

        changed = False
        for entry in found:
            device = Device(
                driver=self.DRIVER_ID,
                device_id=entry['id'],
                name=entry.get('name') or '',
                model='Paragon beacon',
                ip=entry.get('ip', ''),
                lan=True,
                driver_data={'port': entry.get('port') or COMMAND_PORT},
            )
            devices.append(device)
            if self.clips.get(device.device_id) != entry.get('clips', []):
                self.clips[device.device_id] = list(entry.get('clips') or [])
                changed = True
        if changed:
            self._save_clips()
        return devices, []

    def locate(self, devices, timeout=3.0):
        """Where the listed beacons are right now: {device_id: ip}."""
        wanted = set(device.device_id for device in devices)
        return dict((entry['id'], entry['ip'])
                    for entry in self.transport.discover(timeout=timeout)
                    if entry.get('ip') and entry['id'] in wanted)

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def capabilities(device):
        """Emits clips, plays, and says what it is playing. No colour, no
        power: nothing here is a switch."""
        return set([CAP_COMMANDS, CAP_STATE, CAP_PLAYBACK])

    def commands(self, device):
        """Stop, then the clips. Stop is always there, clips or no clips."""
        return [STOP] + [name for name in self.clips.get(device.device_id, [])
                         if name != STOP]

    # -- commands ----------------------------------------------------------

    @staticmethod
    def _port(device):
        try:
            return int((getattr(device, 'driver_data', None) or {}).get(
                'port') or COMMAND_PORT)
        except (TypeError, ValueError):
            return COMMAND_PORT

    def send_command(self, device, name):
        """Play one clip by name.

        Refused here, by name, before anything goes on the wire, when the
        clip is not one the speaker had at the last search. The Pi would
        refuse it too, but the Pi's answer is "HTTP 404" and this one says
        which clip and which speaker -- and it is what stands between a
        sequence and a message that was renamed on the Pi last week.
        """
        if name == STOP:
            try:
                stopped = self.transport.stop(device.ip, self._port(device))
            except SpeakerError as exc:
                raise ControlError('%s: %s' % (device.name, exc))
            self._log('Stopped %s on %s' % ('"%s"' % stopped if stopped
                                            else 'nothing', device.name))
            return True

        known = self.commands(device)
        if name not in known:
            raise ControlError('%s has no clip called "%s"'
                               % (device.name, name))
        try:
            self.transport.play(device.ip, self._port(device), name)
        except SpeakerError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        self._log('Played "%s" on %s' % (name, device.name))
        return True

    def test_connection(self, device):
        """Ask the speaker what it can play, over the path a command takes.

        The discovery hello proves the Pi is on the network; this proves the
        HTTP side that play goes through. It also refreshes the clip list,
        which is why the answer names the clips rather than only saying yes.
        """
        try:
            names = self.transport.clips(device.ip, self._port(device))
        except SpeakerError as exc:
            return False, str(exc)
        if self.clips.get(device.device_id) != names:
            self.clips[device.device_id] = list(names)
            self._save_clips()
        if not names:
            return True, ('%s answered, but has no clips.\n\nDrop audio files '
                          'in its clips folder and search again.'
                          % device.name)
        return True, ('%s answered with %d clip(s):\n\n%s'
                      % (device.name, len(names), '\n'.join(names)))

    # -- playback ------------------------------------------------------------

    def _playback(self, device, verb, call, *args):
        try:
            return call(device.ip, self._port(device), *args)
        except SpeakerError as exc:
            raise ControlError('%s: %s' % (device.name, exc))

    def pause(self, device):
        self._playback(device, 'pause', self.transport.pause)
        self._log('Paused %s' % device.name)
        return True

    def resume(self, device):
        self._playback(device, 'resume', self.transport.resume)
        self._log('Resumed %s' % device.name)
        return True

    def set_volume(self, device, volume):
        try:
            volume = max(0, min(100, int(round(float(volume)))))
        except (TypeError, ValueError):
            raise ControlError('%r is not a volume' % (volume,))
        settled = self._playback(device, 'volume', self.transport.set_volume,
                                 volume)
        self._log('%s volume %d' % (device.name, settled))
        return settled

    # -- state ---------------------------------------------------------------

    def get_state(self, device):
        """What it is playing, or None if it did not answer."""
        try:
            return self.transport.status(device.ip, self._port(device))
        except SpeakerError as exc:
            self._log('No status from %s: %s' % (device.name, exc))
            return None

    def get_states(self, devices, timeout=3.0):
        """One short HTTP exchange per beacon. A house has three."""
        return dict((device.device_id, self.get_state(device))
                    for device in devices)

    # -- switch verbs: a beacon is not a switch ------------------------------

    @staticmethod
    def turn(device, on):
        raise ControlError('%s is a beacon; it cannot be switched'
                           % device.name)

    set_brightness = turn
    set_color = turn
    set_color_temp = turn
