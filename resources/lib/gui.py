# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The interactive control panel.

Everything is built from the stock Kodi dialogs rather than a custom window.
On Krypton a custom skin file has to be maintained per resolution and per skin,
and none of that buys anything for what is essentially a list of lights and a
handful of values -- dialogs work identically under Estuary, skin.paragon and
whatever else the user has installed.
"""

import copy
import time

import xbmcgui

import addon_utils as utils
import palette as palette_lib
import reracks as rerack_lib
import sequences as sequence_lib
import scenes as scene_lib
import speeddial as dial_lib
import voice as voice_lib
from devices import (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP,
                     CAP_COMMANDS, CAP_LOCK, CAP_POSITION, CAP_POWER,
                     CAP_STATE, CAP_UNLOCK,
                     ControlError, DEFAULT_DRIVER, TRANSPORT_CLOUD,
                     describe_state)

# Presets offered before the user has to type anything.
BRIGHTNESS_STEPS = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

TEMP_PRESETS = [
    ('Candle - 2000K', 2000),
    ('Warm - 2700K', 2700),
    ('Soft - 3000K', 3000),
    ('Neutral - 4000K', 4000),
    ('Cool - 5000K', 5000),
    ('Daylight - 6500K', 6500),
]

BACK = -1
# Backed out of a question inside a question, where None is a real answer.
GAVE_UP = object()

# What a light is set to while the naming walkthrough is asking about it.
# Full brightness magenta reads clearly against normal room lighting and
# against the warm whites these bulbs usually sit at.
# The order the kinds of device are offered in, and what to call one
# when its driver is not loaded. A driver not listed here still
# appears, after these, under its own name.
DRIVER_ORDER = ('govee', 'broadlink', 'tuya', 'kasa')
from devices import DRIVER_LABELS  # noqa: E402

HIGHLIGHT_COLOR = (255, 0, 255)
HIGHLIGHT_BRIGHTNESS = 100

# How many times "Identify" blinks a light, and the gap between each change.
# Ten blinks is about eight seconds -- long enough to walk into the next room
# and still be looking when it is flashing.
IDENTIFY_FLASHES = 10
IDENTIFY_GAP = 0.4

# How long to wait for a remote button press when learning a code. Thirty
# seconds is long enough to find the right button on an unfamiliar remote.
# Offered before a sweep, because a shade or blind remote usually has its
# frequency printed on the back and these two cover nearly all of them.
RF_PRESETS = (('433.92', 433.92), ('315', 315.0))
TYPE_FREQUENCY = object()

# What an RM4 will accept at all. Wider than the two presets on purpose: the
# point of the guard is to catch a kilohertz or a hertz typed in by mistake,
# not to argue with an unusual remote.
RF_MIN_MHZ = 280.0
RF_MAX_MHZ = 960.0

LEARN_ATTEMPTS = 30
LEARN_POLL = 1.0

# Offered when setting how often a mix scene moves the colours along.
CYCLE_STEPS = [
    ('Off - hold the arrangement', 0),
    ('Every 15 seconds', 15),
    ('Every 30 seconds', 30),
    ('Every minute', 60),
    ('Every 2 minutes', 120),
    ('Every 5 minutes', 300),
    ('Every 15 minutes', 900),
]

# Cycling drives every light on every step. Over the cloud that is metered:
# Govee allows about 10,000 calls a day, so this is the point at which a
# cycle would eat the lot.
CLOUD_DAILY_CALLS = 10000


def _dialog():
    return xbmcgui.Dialog()


def _select(heading, options):
    """Wrapper around Dialog().select that returns BACK when cancelled."""
    if not options:
        return BACK
    return _dialog().select(heading, options)


def _duration(seconds):
    """'90 seconds' -> 'minute and a half'-ish, in plain words."""
    seconds = int(seconds)
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return '%d hour%s' % (hours, '' if hours == 1 else 's')
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return 'minute' if minutes == 1 else '%d minutes' % minutes
    return '%d seconds' % seconds


def _report(result, success_message):
    """Turn a (applied, errors) result pair into one user-facing message."""
    done, errors = result
    if done and not errors:
        utils.notify(success_message)
    elif done and errors:
        utils.notify('%s (%d light(s) failed)' % (success_message, len(errors)))
    elif errors:
        utils.force_notify(errors[0])
    else:
        utils.force_notify('No lights to control. Run a device refresh.')


class ControlPanel(object):
    """Drives the nested dialog menus."""

    def __init__(self, app):
        self.app = app

    # -- entry point -------------------------------------------------------

    def run(self):
        if not self.app.devices:
            if self._first_run():
                return
        while True:
            # Another interpreter may have rewritten any of the shared files
            # since the last pass. The web remote and the schedule run in the
            # service, the menus are their own script, and a satellite syncs
            # on a timer -- so a search started from a phone moves a blaster's
            # address in devices.json and these menus never hear about it.
            # Four stat calls a pass, against a blaster being dialled at an
            # address it left until Kodi is restarted.
            self.app.reload_changed()
            if self.main_menu() == BACK:
                return

    def _current(self, device):
        """The device as devices.json has it now, not as this screen opened it.

        What matters to a command is the address it will actually be sent to,
        and that is a fact about the file rather than about the menu, so it is
        looked up again each time round rather than remembered. Falls back to
        the device it was handed, which is what a forgotten device or a
        satellite mid-sync leaves behind.
        """
        self.app.reload_changed()
        return self.app.device_by_id(device.device_id) or device

    def _first_run(self):
        """Offer a discovery pass when the cache is empty. True = give up."""
        prompt = ('No devices are known yet.\n\n'
                  'Search the network for them now?')
        if not _dialog().yesno(utils.ADDON_NAME, prompt):
            return True
        self.refresh_devices()
        return not self.app.devices

    # -- main menu ---------------------------------------------------------

    def main_menu(self):
        """Top-level menu, built as (label, handler) rows.

        One row per kind of device rather than one row per device. With three
        drivers and forty devices the flat list ran off the screen, and a
        Broadlink blaster sat among the bulbs offering a brightness it does
        not have. Sorting by driver first means every menu below this point
        can offer only what that kind of device actually does.

        The version is in the heading on purpose: the add-on is normally
        installed as a git clone and updated with `git pull`, and this is the
        quickest way to confirm which version is actually running.
        """
        devices = self.app.enabled_devices
        rows = []

        cycle = self.app.read_cycle()
        if cycle:
            rows.append(('Stop cycling (%s)' % cycle.get('scene'),
                         self.stop_cycling))

        for driver_id in self._driver_ids(devices):
            owned = self._devices_for(driver_id, devices)
            rows.append(('%s (%d)' % (self._driver_label(driver_id),
                                      len(owned)),
                         lambda d=driver_id: self.driver_menu(d)))

        rows.extend([
            ('Scenes...', self.scene_menu),
            ('Sequences...', self.sequence_menu),
            ('Speed dial: %s' % dial_lib.describe(self.app.dial),
             self.dial_menu),
            ('Phrases: %s' % voice_lib.describe(self.app.phrases),
             self.phrase_menu),
        ])
        # A satellite has no reracks of its own: the master keeps the day's
        # shape for the whole house, and a satellite following its own copy
        # would run every phase a second time.
        if self.app.satellite_mode:
            rows.append(('Satellite of %s' % self.app.describe_satellite(),
                         self.satellite_menu))
        else:
            rows.append(('Reracks...', self.rerack_menu))
        rows.extend([
            # A satellite does not search: the master decides which devices
            # the house has, and this is the row that gets them.
            ('Refresh devices', self.refresh_devices)
            if not self.app.satellite_mode
            else ('Copy from the master now', self._sync_now),
            # Stays at the top level rather than inside a driver: the time you
            # need it is when a driver found nothing and so has no menu.
            ('Diagnose device search...', self.diagnose_menu),
            ('Settings', utils.open_settings),
        ])

        choice = _select('%s %s' % (utils.ADDON_NAME, utils.ADDON_VERSION),
                         [label for label, _handler in rows])
        if choice == BACK:
            return BACK
        rows[choice][1]()
        return None

    # -- drivers -----------------------------------------------------------

    @staticmethod
    def _driver_of(device):
        return getattr(device, 'driver', None) or DEFAULT_DRIVER

    @classmethod
    def _devices_for(cls, driver_id, devices):
        return [d for d in devices if cls._driver_of(d) == driver_id]

    @classmethod
    def _driver_ids(cls, devices):
        """Which drivers own something, in a stable, familiar order.

        Taken from the devices rather than from the installed drivers so a
        driver with nothing found does not sit on the menu as a dead row.
        """
        seen = []
        for device in devices:
            driver_id = cls._driver_of(device)
            if driver_id not in seen:
                seen.append(driver_id)
        known = [d for d in DRIVER_ORDER if d in seen]
        return known + sorted(d for d in seen if d not in DRIVER_ORDER)

    def _driver_label(self, driver_id):
        lookup = getattr(self.app.controller, 'driver', None)
        driver = lookup(driver_id) if lookup is not None else None
        return (getattr(driver, 'DRIVER_LABEL', None)
                or DRIVER_LABELS.get(driver_id)
                or driver_id.title())

    def driver_menu(self, driver_id):
        """Everything belonging to one kind of device, and nothing else."""
        while True:
            label = self._driver_label(driver_id)
            devices = self._devices_for(driver_id, self.app.enabled_devices)
            if not devices:
                utils.force_notify('No %s devices are enabled' % label)
                return

            rows = []
            if CAP_POWER in self._capabilities(devices):
                rows.append(('All %s (%d)' % (label, len(devices)),
                             lambda: self.control_menu(devices,
                                                       'All %s' % label)))

            for device in devices:
                # Bind the device to this row rather than closing over the
                # loop variable, which would leave every row pointing at the
                # last one.
                rows.append((self._device_label(device),
                             lambda d=device: self.device_menu(d)))

            rows.append(('Manage %s devices...' % label,
                         lambda: self.manage_devices(driver_id)))

            choice = _select(label, [row_label for row_label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _device_label(self, device):
        """A device row: its name, and how it is reached when that varies.

        Only Govee can be reached more than one way. Tagging a Tuya plug
        [LAN] states the only possibility it has, which is noise on a menu
        whose whole point is that every row earns its place.
        """
        transports = device.transports()
        if not transports or not self._has_transport_choice(device):
            return device.name
        return '%s  [%s]' % (device.name,
                             '+'.join(t.upper() for t in transports))

    def _has_transport_choice(self, device):
        lookup = getattr(self.app.controller, 'driver_for', None)
        driver = lookup(device) if lookup is not None else None
        return bool(getattr(driver, 'HAS_TRANSPORTS', False))

    def device_menu(self, device):
        """Open the menu that fits what this device is.

        A blaster has codes, not brightness; a plug has power, not colour.
        Routing here is what stops one menu having to apologise for rows that
        do not apply.
        """
        capabilities = self.app.controller.capabilities(device)
        # Before the commands check, not after: a blind reports both, and the
        # position is the thing you actually came here to change.
        if CAP_POSITION in capabilities:
            return self.cover_menu(device)
        if CAP_COMMANDS in capabilities:
            return self.command_menu(device)
        return self.control_menu([device], device.name)

    def _capabilities(self, targets):
        """The union of what the targets can do."""
        devices = (targets if targets is not None
                   else self.app.enabled_devices)
        found = set()
        for device in devices:
            found |= set(self.app.controller.capabilities(device) or [])
        return found

    def stop_cycling(self):
        name = self.app.stop_cycle()
        if name:
            utils.notify('Stopped cycling %s' % name)

    def diagnose_menu(self):
        """Which search to look into. Each driver fails its own way."""
        rows = [('Govee lights', self.diagnose),
                ('Tuya plugs', self.diagnose_tuya),
                ('Kasa plugs', self.diagnose_kasa),
                ('Broadlink blasters', self.diagnose_broadlink)]
        choice = _select('Diagnose device search',
                         [label for label, _handler in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def diagnose_kasa(self):
        """Broadcast for Kasa devices and explain what came back."""
        import diagnostics

        self._diagnose('Kasa', 'Searching for Kasa devices...',
                       lambda: diagnostics.run_kasa(self.app))

    def diagnose_broadlink(self):
        """Broadcast for blasters and say whether they have moved.

        The others ask whether a device is there. This one mostly asks whether
        it is still at the address written down for it, because that is how a
        blaster fails: it reports nothing between commands, so an address it
        left is invisible until something tries to use it.
        """
        import diagnostics

        self._diagnose('Broadlink', 'Searching for Broadlink blasters...',
                       lambda: diagnostics.run_broadlink(self.app))

    def _diagnose(self, label, busy, run):
        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, busy)
        try:
            text, _report = run()
        except Exception as exc:
            utils.log('%s diagnostics failed: %s' % (label, exc))
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - %s search' % (utils.ADDON_NAME, label), text)

    def diagnose_tuya(self):
        """Listen for Tuya announcements and explain what was heard."""
        import diagnostics

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Listening for Tuya devices...')
        try:
            text, _report = diagnostics.run_tuya(self.app)
        except Exception as exc:
            utils.log('Tuya diagnostics failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - Tuya search' % utils.ADDON_NAME, text)

    def diagnose(self):
        """Probe the LAN and explain the result on screen and in the log."""
        import diagnostics

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Probing the network...')
        try:
            text, _report = diagnostics.run(self.app)
        except Exception as exc:
            utils.log('Diagnostics failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - LAN diagnostics' % utils.ADDON_NAME, text)

    def verify_status(self, device):
        """Drive one bulb to a known colour and see if it reports it back."""
        import diagnostics

        if not _dialog().yesno(
                utils.ADDON_NAME,
                'This will briefly set %s to a test colour and then put it '
                'back, to check whether it reports its state honestly.\n\n'
                'Continue?' % device.name):
            return

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Testing %s...' % device.name)
        try:
            report = diagnostics.verify_status(self.app, device)
        except Exception as exc:
            utils.log('Status round-trip failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Test failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - status check' % utils.ADDON_NAME,
                     diagnostics.verify_summary(report))

    # -- control ------------------------------------------------------------

    def control_menu(self, targets, heading):
        """Power/brightness/colour menu for one device or the whole group.

        Rows are (label, handler) pairs rather than a list indexed against a
        chain of elifs, so inserting a row cannot silently rewire the ones
        below it.
        """
        while True:
            capabilities = self._capabilities(targets)
            rows = []

            if CAP_POWER in capabilities:
                rows.extend([
                    ('Toggle',
                     lambda: _report(self.app.toggle_all(targets),
                                     'Toggled %s' % heading)),
                    ('On',
                     lambda: _report(self.app.power_all(True, targets),
                                     '%s on' % heading)),
                    ('Off',
                     lambda: _report(self.app.power_all(False, targets),
                                     '%s off' % heading)),
                ])
            if CAP_LOCK in capabilities:
                rows.append(('Lock',
                             lambda: self._lock_them(targets, heading)))
            # Only where the box is allowed to. Absent rather than present and
            # refusing: a row that says no every time is a row that teaches
            # people to press it and wait for the notification.
            if (CAP_UNLOCK in capabilities
                    and getattr(self.app.controller, 'allow_unlock', False)):
                rows.append(('Unlock',
                             lambda: self._unlock_them(targets, heading)))
            if CAP_BRIGHTNESS in capabilities:
                rows.append(('Brightness...',
                             lambda: self.brightness_menu(targets, heading)))
            if CAP_COLOR in capabilities:
                rows.append(('Colour...',
                             lambda: self.color_menu(targets, heading)))
            if CAP_COLOR_TEMP in capabilities:
                rows.append(('Colour temperature...',
                             lambda: self.temp_menu(targets, heading)))

            if targets and len(targets) == 1:
                if CAP_STATE in capabilities:
                    rows.append(('Show status',
                                 lambda: self.show_status(targets[0])))
                if CAP_COLOR in capabilities:
                    # The round trip drives the device to a known colour and
                    # reads it back, so it has nothing to say about a plug.
                    rows.append(('Check status reporting...',
                                 lambda: self.verify_status(targets[0])))

            choice = _select(heading, [label for label, _handler in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _explain_no_status(self, device):
        """Say why a state read failed, in the terms of the device that failed.

        A read returns None whatever went wrong, so this asks the driver
        again through its own connection test, which does report a reason.
        Without it every driver inherited Govee's explanation -- a Kasa plug
        that could not be reached was blamed on a UDP port Govee uses and
        Kasa has never heard of.
        """
        message = ''
        ok = False
        try:
            ok, message = self.app.test_device(device)
        except ControlError:
            message = ''

        if ok and message:
            # It answered on the second attempt, so the reading is the answer.
            _dialog().ok(utils.ADDON_NAME, message)
            return
        if message:
            _dialog().ok(utils.ADDON_NAME,
                         'Could not read the state of %s.\n\n%s'
                         % (device.name, message))
            return

        # Only Govee gets here: it is the one driver with no connection test,
        # and the one whose status read contends for a well-known port.
        _dialog().ok(utils.ADDON_NAME,
                     'Could not read the state of %s.\n\n'
                     'LAN status needs UDP port 4002, which another '
                     'program may be holding.' % device.name)

    def brightness_menu(self, targets, heading):
        options = ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('%s - brightness' % heading, options)
        if choice == BACK:
            return

        if choice == len(BRIGHTNESS_STEPS):
            value = self._ask_number('Brightness (1-100)', '50')
            if value is None:
                return
            percent = max(1, min(100, value))
        else:
            percent = BRIGHTNESS_STEPS[choice]

        _report(self.app.brightness_all(percent, targets),
                '%s at %d%%' % (heading, percent))

    def _palette_rows(self):
        """Menu labels for the saved colours, e.g. 'Deep Red  #FF0000'."""
        return ['%s  %s' % (entry['name'], palette_lib.to_hex(entry['color']))
                for entry in self.app.palette]

    def color_menu(self, targets, heading):
        while True:
            entries = self.app.palette
            options = self._palette_rows()
            options.append('Custom hex...')
            # A custom hex is used once and not saved, so it stays. Managing
            # the palette writes a file the master owns.
            if self.app.owns_data:
                options.append('Manage colours...')

            choice = _select('%s - colour' % heading, options)
            if choice == BACK:
                return

            if choice == len(entries) + 1:
                self.manage_colors()
                continue

            if choice == len(entries):
                entered = self._ask_hex()
                if entered is None:
                    return
                rgb, label = entered
            else:
                entry = entries[choice]
                rgb, label = entry['color'], entry['name']

            _report(self.app.color_all(rgb, targets),
                    '%s set to %s' % (heading, label))
            return

    # -- colour palette -----------------------------------------------------

    def manage_colors(self):
        """Add, edit, reorder and delete the saved colours."""
        while True:
            entries = self.app.palette
            options = self._palette_rows()
            options.append('Add a colour...')
            options.append('Reset to the built-in colours')

            choice = _select('Manage colours', options)
            if choice == BACK:
                return
            if choice == len(entries):
                self._add_color()
            elif choice == len(entries) + 1:
                if _dialog().yesno(
                        utils.ADDON_NAME,
                        'Replace the colour list with the built-in set?\n\n'
                        'Any colours you added are removed.'):
                    self.app.reset_palette()
                    utils.notify('Colours reset')
            else:
                self._edit_color(choice)

    def _add_color(self):
        entered = self._ask_hex()
        if entered is None:
            return
        rgb, _label = entered

        name = _dialog().input('Name for this colour', '')
        if not name or not name.strip():
            return
        name = name.strip()

        if self.app.color_by_name(name) is not None and not _dialog().yesno(
                utils.ADDON_NAME,
                'A colour called "%s" already exists.\n\nReplace it?' % name):
            return

        if self.app.save_color(name, rgb) is None:
            utils.force_notify('That colour could not be saved')
            return
        utils.notify('Added %s  %s' % (name, palette_lib.to_hex(rgb)))

    def _edit_color(self, index):
        entries = self.app.palette
        if index < 0 or index >= len(entries):
            return
        entry = entries[index]

        rows = [
            ('Rename (currently "%s")' % entry['name'], 'rename'),
            ('Change colour (currently %s)'
             % palette_lib.to_hex(entry['color']), 'recolor'),
            ('Move up', 'up'),
            ('Move down', 'down'),
            ('Delete', 'delete'),
        ]
        choice = _select(entry['name'], [label for label, _key in rows])
        if choice == BACK:
            return
        action = rows[choice][1]

        if action == 'rename':
            name = _dialog().input('Colour name', entry['name'])
            if name and name.strip() and name.strip() != entry['name']:
                # Remove first so the rename does not collide with itself.
                self.app.remove_color(entry)
                self.app.save_color(name.strip(), entry['color'], index=index)
                utils.notify('Renamed to %s' % name.strip())
        elif action == 'recolor':
            entered = self._ask_hex()
            if entered is None:
                return
            self.app.save_color(entry['name'], entered[0])
            utils.notify('%s is now %s'
                         % (entry['name'], palette_lib.to_hex(entered[0])))
        elif action == 'up':
            self.app.move_color(index, -1)
        elif action == 'down':
            self.app.move_color(index, 1)
        elif action == 'delete':
            if _dialog().yesno(utils.ADDON_NAME,
                               'Delete the colour "%s"?' % entry['name']):
                self.app.remove_color(entry)
                utils.notify('Deleted %s' % entry['name'])

    def temp_menu(self, targets, heading):
        options = [name for name, _k in TEMP_PRESETS]
        options.append('Custom...')
        choice = _select('%s - colour temperature' % heading, options)
        if choice == BACK:
            return

        if choice == len(TEMP_PRESETS):
            value = self._ask_number('Colour temperature in Kelvin', '2700')
            if value is None:
                return
            kelvin = max(1500, min(12000, value))
        else:
            kelvin = TEMP_PRESETS[choice][1]

        _report(self.app.color_temp_all(kelvin, targets),
                '%s at %dK' % (heading, kelvin))

    def show_status(self, device):
        state = self.app.controller.get_state(device)
        if not state:
            self._explain_no_status(device)
            return

        lines = describe_state(state)
        if not lines:
            # It answered, but with nothing this add-on knows how to read.
            # Said plainly, with the keys, rather than as "Power: unknown" --
            # which is what a lock and a blind both used to be told.
            lines = ['It answered, but said nothing recognisable.',
                     '',
                     'What it sent: %s' % ', '.join(sorted(state.keys()))]
        if device.ip:
            lines.append('Address: %s' % device.ip)

        _dialog().ok(device.name, '\n'.join(lines))

    # -- scenes -------------------------------------------------------------

    def scene_menu(self):
        while True:
            scenes = self.app.scenes
            options = ['%s  -  %s' % (s['name'], scene_lib.describe(s))
                       for s in scenes]
            # Same rule as the sequences: the master owns the scene list, so
            # a satellite offers no way to add to it. Capture especially --
            # setting a room up by hand and losing the result a quarter of an
            # hour later is the worst version of this.
            editable = self.app.owns_data
            if editable:
                options.append('Capture lights as a new scene...')
                options.append('Manage scenes...')

            choice = _select('Scenes', options)
            if choice == BACK:
                return
            if editable and choice == len(scenes):
                self.capture_scene()
                continue
            if editable and choice == len(scenes) + 1:
                self.manage_scenes()
                continue
            self.app.apply_scene(scenes[choice])

    def capture_scene(self):
        """Snapshot the lights' current state into a scene.

        This is the route for Govee Tap-to-Run scenes: run one in the Govee
        app, then capture the result here. The saved copy replays over the
        LAN, so it needs no Govee account credentials and no cloud call.
        """
        if not self.app.enabled_devices:
            utils.force_notify('No lights to capture. Run a device refresh.')
            return

        name = _dialog().input('Name for the captured scene', '')
        if not name or not name.strip():
            return
        name = name.strip()

        existing = self.app.scene_by_name(name)
        if existing is not None and not _dialog().yesno(
                utils.ADDON_NAME,
                'A scene called "%s" already exists.\n\nReplace it with what '
                'the lights are doing now?' % name):
            return

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Reading the lights...')
        try:
            scene, captured, skipped = self.app.capture_scene(name)
        except Exception as exc:
            utils.log('Capture failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Capture failed:\n\n%s' % exc)
            return
        progress.close()

        if not captured:
            _dialog().ok(utils.ADDON_NAME,
                         'None of the lights reported their state.\n\n'
                         'Status replies need UDP port %d -- close the Govee '
                         'Desktop app and try again.' % 4002)
            return

        if self.app.save_scene(scene) is None:
            utils.force_notify('That scene could not be saved')
            return

        message = 'Captured "%s" from %d light(s).' % (name, captured)
        summary = self.app.summarise_capture(scene)
        if summary:
            message += '\n\n' + summary
        if skipped:
            message += ('\n\n%d did not answer and were left out:\n%s'
                        % (len(skipped), ', '.join(skipped[:6])))
        # The LAN protocol has no scene concept, so a bulb driven into a
        # Govee app scene keeps reporting whatever was last set locally.
        # Saying so here is the difference between the user trusting a wrong
        # capture and knowing to set the look from Kodi first.
        message += ('\n\nIf that does not match what you see, the bulbs '
                    'reported a stale state: Govee app scenes are invisible '
                    'over the LAN. Set the look from Kodi first, then '
                    'capture.')
        # Always shown rather than a toast: this is the moment to notice that
        # every bulb read back as white, or every brightness as 100%.
        _dialog().ok(utils.ADDON_NAME, message)

    def manage_scenes(self):
        while True:
            scenes = self.app.scenes
            options = [s['name'] for s in scenes]
            options.append('Add a new scene...')

            choice = _select('Manage scenes', options)
            if choice == BACK:
                return
            if choice == len(scenes):
                self.edit_scene(None)
            else:
                self.edit_scene(choice)

    def edit_scene(self, index):
        """Edit an existing scene by index, or create one when index is None."""
        scenes = self.app.scenes
        if index is None:
            scene = scene_lib.make_scene('New scene')
        else:
            scene = dict(scenes[index])

        while True:
            brightness = ('leave alone' if scene['brightness'] is None
                          else '%d%%' % scene['brightness'])
            bar_brightness = ('same as above'
                              if scene.get('bar_brightness') is None
                              else '%d%%' % scene['bar_brightness'])
            backlight_brightness = (
                'same as above'
                if scene.get('backlight_brightness') is None
                else '%d%%' % scene['backlight_brightness'])
            strip_brightness = (
                'same as above'
                if scene.get('strip_brightness') is None
                else '%d%%' % scene['strip_brightness'])
            if scene['mode'] == scene_lib.MODE_COLOR:
                appearance = 'RGB %d, %d, %d' % tuple(scene['color'][:3])
            elif scene['mode'] == scene_lib.MODE_MIX:
                names = [e['name'] for e in scene.get('colors') or []]
                appearance = 'mix: %s' % (', '.join(names[:3])
                                          + (', ...' if len(names) > 3 else ''))
            elif scene['mode'] == scene_lib.MODE_TEMP:
                appearance = '%dK' % scene['kelvin']
            else:
                appearance = 'leave alone'
            targets = scene['targets']
            # Says which "all" this is. A scene with no targets applies to
            # what it can describe, so a colour scene means the colour
            # devices and not every plug in the house.
            if targets:
                target_label = '%d selected' % len(targets)
            elif scene_lib.scene_expresses(scene):
                target_label = 'all colour lights'
            else:
                target_label = 'all lights'
            cycle = int(scene.get('cycle') or 0)
            cycle_label = ('off' if not cycle
                           else 'every %s' % _duration(cycle))

            commands = scene.get('actions') or []
            command_label = ('none' if not commands
                             else '%d' % len(commands))

            # (label, handler) rows rather than a list read back by index.
            # This editor has grown a row four times, and every time the
            # numbering under it had to be re-counted by hand.
            rows = [
                ('Name: %s' % scene['name'],
                 lambda: self._edit_scene_name(scene)),
                ('Power: %s' % scene['power'],
                 lambda: self._edit_power(scene)),
                ('Brightness: %s' % brightness,
                 lambda: self._edit_brightness(scene)),
                ('Lightbar brightness: %s' % bar_brightness,
                 lambda: self._edit_bar_brightness(scene)),
                ('Backlight brightness: %s' % backlight_brightness,
                 lambda: self._edit_backlight_brightness(scene)),
                ('Light strip brightness: %s' % strip_brightness,
                 lambda: self._edit_strip_brightness(scene)),
                ('Appearance: %s' % appearance,
                 lambda: self._edit_appearance(scene)),
                ('Lights: %s' % target_label,
                 lambda: self._edit_targets(scene)),
                ('What it does to each light...',
                 lambda: self._per_light_menu(scene)),
                ('Commands to send: %s' % command_label,
                 lambda: self._edit_actions(scene)),
                ('Cycle colours: %s' % cycle_label,
                 lambda: self._edit_cycle(scene)),
                ('Test this scene', lambda: self.app.apply_scene(scene)),
                ('Duplicate...',
                 lambda: self._duplicate_scene(scene, index)),
                ('Save', lambda: self._save_and_close(scene, index)),
            ]
            if index is not None:
                rows.append(('Delete',
                             lambda: self._delete_scene(scene, index)))

            choice = _select('Edit scene', [label for label, _h in rows])
            if choice == BACK:
                return
            if rows[choice][1]() is False:
                return

    def _edit_scene_name(self, scene):
        name = _dialog().input('Scene name', scene['name'])
        if name and name.strip():
            scene['name'] = name.strip()

    def _edit_power(self, scene):
        pick = _select('Power', ['Turn on', 'Turn off', 'Leave as it is'])
        if pick != BACK:
            scene['power'] = [scene_lib.POWER_ON, scene_lib.POWER_OFF,
                              scene_lib.POWER_KEEP][pick]

    @staticmethod
    def _copy_name(name, taken):
        """"Dawn" -> "Dawn 2", or the next number `taken` says is free."""
        base = (name or 'Copy').strip()
        for number in range(2, 100):
            candidate = '%s %d' % (base, number)
            if not taken(candidate):
                return candidate
        return base

    def _duplicate_scene(self, scene, index):
        """Copy this scene under a new name and open the copy.

        Copies what is on screen rather than what is saved, so a change made
        just before pressing this is in the copy -- which is what anyone
        pressing "duplicate" halfway through an edit meant.

        The copy is opened straight away because that is invariably the point:
        a duplicate exists to be changed, not to sit there identical.
        """
        suggestion = self._copy_name(
            scene['name'],
            lambda n: scene_lib.find(self.app.scenes, n) is not None)
        name = _dialog().input('Name for the copy', suggestion)
        if name is None or not name.strip():
            return
        name = name.strip()
        if scene_lib.find(self.app.scenes, name) is not None:
            _dialog().ok(utils.ADDON_NAME,
                         'There is already a scene called "%s".' % name)
            return

        # A deep copy, not dict(): a scene holds lists and a per-device map,
        # and a shallow one would leave the copy editing the original's.
        made = copy.deepcopy(scene)
        made['name'] = name
        cleaned = scene_lib.normalise(made)
        if cleaned is None:
            utils.force_notify('That scene could not be copied')
            return

        self.app.scenes.append(cleaned)
        self.app.save_scenes()
        utils.notify('Copied to "%s"' % name)
        self.edit_scene(len(self.app.scenes) - 1)
        return False

    def _save_and_close(self, scene, index):
        self._save_scene(scene, index)
        return False

    def _delete_scene(self, scene, index):
        if _dialog().yesno(utils.ADDON_NAME,
                           'Delete the scene "%s"?' % scene['name']):
            del self.app.scenes[index]
            self.app.save_scenes()
            utils.notify('Scene deleted')
        return False

    def _edit_actions(self, scene):
        """The commands a scene fires alongside its lighting.

        A scene sets state; a command has none to set. An infrared blaster has
        no colour, it has "AVR Power" -- so one "Movie Night" can dim the
        lights and switch the amplifier on in the same breath.
        """
        from devices import CAP_COMMANDS

        while True:
            actions = scene.get('actions') or []
            rows = []
            for action in actions:
                device = self.app.device_by_id(action['device'])
                name = device.name if device else action['device']
                rows.append(('%s: %s' % (name, action['command']),
                             lambda a=action: self._remove_action(scene, a)))

            emitters = [d for d in self.app.enabled_devices
                        if CAP_COMMANDS in self.app.controller.capabilities(d)]
            if emitters:
                rows.append(('Add a command...',
                             lambda: self._add_action(scene, emitters)))
            elif not rows:
                utils.force_notify('No device here sends commands')
                return

            choice = _select('%s - commands' % scene['name'],
                             [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _add_action(self, scene, emitters):
        """Pick a blaster, then one of the codes it knows."""
        pick = _select('Which device', [d.name for d in emitters])
        if pick == BACK:
            return
        device = emitters[pick]

        names = self.app.controller.commands(device)
        if not names:
            _dialog().ok(utils.ADDON_NAME,
                         '%s has not learned any commands yet.\n\n'
                         'Teach it one under its own menu first.' % device.name)
            return

        chosen = _select('Which command', list(names))
        if chosen == BACK:
            return

        actions = list(scene.get('actions') or [])
        actions.append({'device': device.device_id,
                        'command': names[chosen]})
        scene['actions'] = actions

    def _remove_action(self, scene, action):
        if not _dialog().yesno(
                utils.ADDON_NAME,
                'Stop this scene sending "%s"?' % action['command']):
            return
        scene['actions'] = [a for a in scene.get('actions') or []
                            if a is not action]

    def _save_scene(self, scene, index):
        cleaned = scene_lib.normalise(scene)
        if cleaned is None:
            utils.force_notify('That scene could not be saved')
            return
        scenes = self.app.scenes
        clash = scene_lib.find(scenes, cleaned['name'])
        if clash is not None and (index is None or scenes[index] is not clash):
            if not _dialog().yesno(
                    utils.ADDON_NAME,
                    'A scene called "%s" already exists.\n\nReplace it?'
                    % cleaned['name']):
                return
            scenes.remove(clash)
            if index is not None and index >= len(scenes):
                index = None

        if index is None:
            scenes.append(cleaned)
        else:
            scenes[index] = cleaned
        self.app.save_scenes()
        utils.notify('Scene "%s" saved' % cleaned['name'])

    def _edit_brightness(self, scene):
        options = ['Leave brightness alone']
        options += ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('Scene brightness', options)
        if choice == BACK:
            return
        if choice == 0:
            scene['brightness'] = None
        elif choice == len(options) - 1:
            value = self._ask_number('Brightness (1-100)', '50')
            if value is not None:
                scene['brightness'] = max(1, min(100, value))
        else:
            scene['brightness'] = BRIGHTNESS_STEPS[choice - 1]

    def _edit_bar_brightness(self, scene):
        """A separate brightness for lightbars, overriding the scene's own.

        A lightbar at 50% is far brighter than a bulb at 50%, so a scene that
        looks right on the bulbs often blows out the bars.
        """
        options = ['Same as the scene brightness']
        options += ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('Lightbar brightness', options)
        if choice == BACK:
            return
        if choice == 0:
            scene['bar_brightness'] = None
        elif choice == len(options) - 1:
            value = self._ask_number('Lightbar brightness (1-100)', '25')
            if value is not None:
                scene['bar_brightness'] = max(1, min(100, value))
        else:
            scene['bar_brightness'] = BRIGHTNESS_STEPS[choice - 1]

    def _edit_strip_brightness(self, scene):
        """A separate brightness for light strips, overriding the scene's own.

        The third of the same problem the lightbars and backlights have: a
        strip along a counter is a line of light rather than something
        filling a room, and the percentage that suits the bulbs overhead is
        rarely the one that suits it.
        """
        options = ['Same as the scene brightness']
        options += ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('Light strip brightness', options)
        if choice == BACK:
            return
        if choice == 0:
            scene['strip_brightness'] = None
        elif choice == len(options) - 1:
            value = self._ask_number('Light strip brightness (1-100)', '40')
            if value is not None:
                scene['strip_brightness'] = max(1, min(100, value))
        else:
            scene['strip_brightness'] = BRIGHTNESS_STEPS[choice - 1]

    def _edit_backlight_brightness(self, scene):
        """A separate brightness for backlights, overriding the scene's own.

        The same problem as the lightbars, from the other end: a strip washing
        a wall behind a screen at the level that suits the room's bulbs is
        either glare or nothing at all.
        """
        options = ['Same as the scene brightness']
        options += ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('Backlight brightness', options)
        if choice == BACK:
            return
        if choice == 0:
            scene['backlight_brightness'] = None
        elif choice == len(options) - 1:
            value = self._ask_number('Backlight brightness (1-100)', '25')
            if value is not None:
                scene['backlight_brightness'] = max(1, min(100, value))
        else:
            scene['backlight_brightness'] = BRIGHTNESS_STEPS[choice - 1]

    def _edit_appearance(self, scene):
        choice = _select('Scene appearance',
                         ['Colour temperature', 'Colour',
                          'Mix of colours (spread over the lights)...',
                          'Leave alone'])
        if choice == BACK:
            return
        if choice == 3:
            scene['mode'] = scene_lib.MODE_NONE
        elif choice == 2:
            self._edit_mix(scene)
        elif choice == 0:
            options = [name for name, _k in TEMP_PRESETS] + ['Custom...']
            pick = _select('Colour temperature', options)
            if pick == BACK:
                return
            if pick == len(TEMP_PRESETS):
                value = self._ask_number('Kelvin', str(scene['kelvin']))
                if value is None:
                    return
                scene['kelvin'] = max(1500, min(12000, value))
            else:
                scene['kelvin'] = TEMP_PRESETS[pick][1]
            scene['mode'] = scene_lib.MODE_TEMP
        else:
            entries = self.app.palette
            options = self._palette_rows() + ['Custom hex...']
            pick = _select('Colour', options)
            if pick == BACK:
                return
            if pick == len(entries):
                entered = self._ask_hex()
                if entered is None:
                    return
                scene['color'] = list(entered[0])
            else:
                scene['color'] = list(entries[pick]['color'])
            scene['mode'] = scene_lib.MODE_COLOR

    def _edit_cycle(self, scene):
        """How often a mix moves the colours along, or off."""
        if scene.get('mode') != scene_lib.MODE_MIX:
            _dialog().ok(utils.ADDON_NAME,
                         'Cycling moves the lights through a mix of '
                         'colours.\n\nSet Appearance to "Mix of colours" '
                         'first.')
            return

        choice = _select('Cycle colours',
                         [label for label, _seconds in CYCLE_STEPS])
        if choice == BACK:
            return

        seconds = CYCLE_STEPS[choice][1]
        if seconds and not self._cycle_cost_ok(scene, seconds):
            return
        scene['cycle'] = seconds

    def _cycle_cost_ok(self, scene, seconds):
        """Warn before a cycle that would burn the Govee cloud quota.

        Every step drives every light. Over the LAN that is free; over the
        cloud it is metered, and 25 lights on a one-minute cycle is 36,000
        calls a day against a limit of about 10,000.
        """
        targets = scene_lib.scene_targets(scene, self.app.devices,
                                          self.app.controller)
        cloud = [d for d in targets
                 if self.app.controller.pick_transport(d) == TRANSPORT_CLOUD]
        if not cloud:
            return True

        per_day = len(cloud) * (86400.0 / seconds)
        if per_day <= CLOUD_DAILY_CALLS:
            return True

        return _dialog().yesno(
            utils.ADDON_NAME,
            '%d of these lights are driven over the Govee cloud, which is '
            'rate limited.\n\nCycling every %s would use about %d cloud '
            'calls a day against a limit near %d, so the lights would stop '
            'responding partway through.\n\nUse it anyway?'
            % (len(cloud), _duration(seconds), int(per_day),
               CLOUD_DAILY_CALLS))

    def _edit_mix(self, scene):
        """Tick which saved colours go into the mix.

        Colours are copied into the scene rather than referenced by name, so
        editing or deleting a palette entry later cannot silently change or
        empty a scene that was already built and tested.
        """
        entries = self.app.palette
        if not entries:
            utils.force_notify('No colours defined yet')
            return

        chosen = [dict(e) for e in (scene.get('colors') or [])]
        chosen_rgb = [tuple(e['color']) for e in chosen]

        while True:
            rows = []
            for entry in entries:
                mark = '[x]' if tuple(entry['color']) in chosen_rgb else '[ ]'
                rows.append('%s %s  %s'
                            % (mark, entry['name'],
                               palette_lib.to_hex(entry['color'])))
            rows.append('Done (%d in the mix)' % len(chosen))

            choice = _select('Colours to spread over the lights', rows)
            if choice == BACK:
                return
            if choice == len(entries):
                if not chosen:
                    utils.force_notify('Pick at least one colour')
                    continue
                scene['colors'] = chosen
                scene['mode'] = scene_lib.MODE_MIX
                return

            entry = entries[choice]
            rgb = tuple(entry['color'])
            if rgb in chosen_rgb:
                position = chosen_rgb.index(rgb)
                del chosen[position]
                del chosen_rgb[position]
            else:
                chosen.append(dict(entry))
                chosen_rgb.append(rgb)

    def _scene_lights(self, scene):
        """The lights a scene touches, in the order it would reach them.

        The named ones where it names any, and every light it could express to
        where it does not -- the same reading apply_scene takes, so this list
        is what really happens rather than a guess at it.
        """
        lights = [d for d in self.app.devices
                  if scene_lib.is_a_light(d, self.app.controller)]
        named = [str(t).upper() for t in (scene.get('targets') or [])]
        if not named:
            return lights
        return [d for d in lights if d.device_id.upper() in named]

    def _per_light_menu(self, scene):
        """What this scene does to each light, and where to copy it from.

        Built because a captured scene held a colour and a brightness for every
        light and nothing ever showed them back: Capture wrote them and only
        the bulbs ever saw them again.
        """
        while True:
            lights = self._scene_lights(scene)
            if not lights:
                utils.force_notify('%s reaches no lights' % scene['name'])
                return
            own = scene.get('devices') or {}
            rows = []
            for device in lights:
                settings = scene_lib.effective_settings(scene, device)
                # Marked, because "its own" and "the scene's" look identical
                # once they are both a colour and a percentage, and only one of
                # them changes when the scene's own values are edited.
                apart = ' [set apart]' \
                    if device.device_id.upper() in own else ''
                rows.append(('%s  -  %s%s'
                             % (device.name,
                                scene_lib.describe_settings(settings), apart),
                             device))

            choice = _select('%s: each light' % scene['name'],
                             [label for label, _d in rows])
            if choice == BACK:
                return
            self._one_light_menu(scene, rows[choice][1])

    def _one_light_menu(self, scene, device):
        """What can be done with one light's settings inside a scene."""
        settings = scene_lib.effective_settings(scene, device)
        own = (scene.get('devices') or {})
        is_own = device.device_id.upper() in own

        rows = [('Copy this into another scene...',
                 lambda: self._copy_settings_to_scene(scene, device, settings))]
        if is_own:
            rows.append(('Stop setting this one apart',
                         lambda: self._forget_one_light(scene, device)))

        choice = _select('%s in %s' % (device.name, scene['name']),
                         [label for label, _h in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def _copy_settings_to_scene(self, scene, device, settings):
        """Write one light's settings from this scene into another one."""
        others = [other for other in self.app.scenes
                  if other['name'] != scene['name']]
        if not others:
            utils.force_notify('There is no other scene to copy into')
            return
        choice = _select('Copy %s into which scene' % device.name,
                         ['%s  -  %s' % (other['name'],
                                         scene_lib.describe(other))
                          for other in others])
        if choice == BACK:
            return
        target = dict(others[choice])

        existing = (target.get('devices') or {}).get(device.device_id.upper())
        if existing is not None and not _dialog().yesno(
                utils.ADDON_NAME,
                '%s already has its own settings in "%s":\n\n%s\n\n'
                'Replace them with %s?'
                % (device.name, target['name'],
                   scene_lib.describe_settings(existing),
                   scene_lib.describe_settings(settings))):
            return

        scene_lib.set_device_settings(target, device.device_id, settings)
        self.app.save_scene(target)
        utils.notify('%s in %s: %s'
                     % (device.name, target['name'],
                        scene_lib.describe_settings(settings)))

    def _forget_one_light(self, scene, device):
        """Put a light back on the scene's own values."""
        if not _dialog().yesno(
                utils.ADDON_NAME,
                'Put %s back on the scene\'s own colour and brightness?'
                % device.name):
            return
        if scene_lib.forget_device_settings(scene, device.device_id):
            utils.notify('%s now follows %s' % (device.name, scene['name']))

    def _edit_targets(self, scene):
        """Pick which lights a scene touches, one at a time.

        Krypton's Dialog().multiselect exists but silently differs across skins
        on some builds, so this uses a plain checklist the user toggles.
        """
        # Lights only. A plug and a blaster are perfectly good sequence
        # steps, but a scene describes how a room looks and neither has a
        # look -- offering them here is a choice that would do nothing.
        devices = [d for d in self.app.devices
                   if scene_lib.is_a_light(d, self.app.controller)]
        if not devices:
            utils.force_notify('No lights known yet')
            return

        chosen = set(scene['targets'])
        while True:
            options = ['Apply to all lights' + (' [x]' if not chosen else '')]
            for device in devices:
                mark = '[x]' if device.device_id in chosen else '[ ]'
                options.append('%s %s' % (mark, device.name))
            options.append('Done')

            choice = _select('Lights in this scene', options)
            if choice == BACK or choice == len(options) - 1:
                scene['targets'] = sorted(chosen)
                return
            if choice == 0:
                chosen = set()
                continue
            device = devices[choice - 1]
            if device.device_id in chosen:
                chosen.discard(device.device_id)
            else:
                chosen.add(device.device_id)

    def satellite_menu(self):
        """What this box follows, and a way to copy from it now."""
        while True:
            rows = [
                ('Copy from the master now', self._sync_now),
                ('Master address: %s' % (self.app.master_ip or 'not set'),
                 utils.open_settings),
                ('Last copied: %s' % (self.app.last_sync or 'never'),
                 self._sync_now),
                ('Stop following a master', utils.open_settings),
            ]
            choice = _select('Satellite', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()
            if choice in (1, 3):
                # Settings were open; what they say may have changed.
                return

    def _sync_now(self):
        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Copying from the master...')
        try:
            copied, problems = self.app.sync_from_master()
        except Exception as exc:
            utils.log('Satellite sync raised: %s' % exc)
            copied, problems = [], [str(exc)]
        finally:
            progress.close()

        if copied and not problems:
            utils.notify('Copied %d file(s) from the master' % len(copied))
        elif copied:
            utils.force_notify('Copied %d, %d problem(s)'
                               % (len(copied), len(problems)))
        elif problems:
            _dialog().ok('Satellite', 'Nothing was copied.', problems[0])
        else:
            utils.notify('Nothing to copy')

    # -- devices ------------------------------------------------------------

    def refresh_devices(self):
        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Searching for lights...')
        try:
            devices, warnings = self.app.refresh_devices()
        except Exception as exc:  # a failed refresh must not kill the panel
            utils.log('Device refresh raised: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Device search failed:\n\n%s' % exc)
            return
        progress.close()

        if not devices:
            message = 'No lights were found.\n\n' \
                      'Check that LAN Control is switched on for each light ' \
                      'in the Govee Home app, or set a Govee API key in ' \
                      'Settings to use the cloud.'
            if warnings:
                message += '\n\n' + '\n'.join(warnings[:2])
            _dialog().ok(utils.ADDON_NAME, message)
            return

        lan_count = len([d for d in devices if d.lan])
        summary = 'Found %d light(s), %d on the LAN.' % (len(devices),
                                                         lan_count)
        missing = getattr(self.app, 'last_refresh_missing', 0)
        if missing:
            summary += ('\n\n%d known light(s) did not answer and were kept '
                        'as they were. Their names are safe; they will pick '
                        'up again on the next search.' % missing)
        if warnings:
            _dialog().ok(utils.ADDON_NAME,
                         summary + '\n\n' + '\n'.join(warnings[:2]))
        else:
            utils.notify(summary)

    def manage_devices(self, driver_id=None):
        """Rename, enable, identify and forget. Scoped to one kind of device
        when it is reached from that kind's menu."""
        while True:
            self.app.reload_changed()
            devices = self.app.devices
            if driver_id is not None:
                devices = self._devices_for(driver_id, devices)
            if not devices:
                utils.force_notify('No devices known yet')
                return

            heading = 'Manage devices'
            rows = []
            if driver_id is not None:
                heading = 'Manage %s devices' % self._driver_label(driver_id)
            # The walkthrough lights each device in turn and asks what it is,
            # which needs something that can show a colour.
            if CAP_COLOR in self._capabilities(devices):
                rows.append(('Name lights one by one...',
                             lambda: self.name_lights(driver_id)))
            for device in devices:
                mark = '[x]' if device.enabled else '[ ]'
                label = '%s %s  (%s)' % (mark, device.name,
                                         '+'.join(device.transports())
                                         or 'offline')
                rows.append((label,
                             lambda d=device: self._edit_device(d)))

            choice = _select(heading, [l for l, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def name_lights(self, driver_id=None):
        """Walk the lights one at a time, lighting each while asking its name.

        Renaming through the per-device menu means going and looking at the
        room, coming back, and navigating two menus again -- 25 times. Here
        the light stays lit while the keyboard is up, so the answer is on the
        wall in front of you as you type it.
        """
        devices = self.app.enabled_devices
        if driver_id is not None:
            devices = self._devices_for(driver_id, devices)
        if not devices:
            utils.force_notify('No lights to name. Run a device refresh.')
            return

        unnamed = [d for d in devices if self._is_placeholder_name(d)]
        if unnamed and len(unnamed) != len(devices):
            choice = _select(
                'Which lights?',
                ['Only the %d still unnamed' % len(unnamed),
                 'All %d lights' % len(devices)])
            if choice == BACK:
                return
            devices = unnamed if choice == 0 else devices

        if not _dialog().yesno(
                utils.ADDON_NAME,
                'Each light will come on bright magenta in turn. Type the '
                'name of whichever one lights up.\n\n'
                'Cancel the keyboard to stop; names entered so far are '
                'kept.\n\nStart?'):
            return

        # One bulk read up front rather than per light: the lights get put
        # back from this snapshot as the walk moves on.
        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Reading the lights...')
        try:
            states = self.app.controller.get_states(devices)
        except Exception as exc:
            utils.log('Could not read states before naming: %s' % exc)
            states = {}
        progress.close()

        named = self._walk_and_name(devices, states)

        self.app.save_devices()
        if named:
            _dialog().ok(utils.ADDON_NAME, 'Named %d light(s).' % named)
        else:
            utils.notify('No lights were renamed')

    @staticmethod
    def _is_placeholder_name(device):
        """True when the name is still the model + id one discovery invents."""
        return device.name.startswith(device.model + ' (')

    def _walk_and_name(self, devices, states):
        """Light each device in turn and prompt. Returns how many were named."""
        highlight = {'power': scene_lib.POWER_ON,
                     'brightness': HIGHLIGHT_BRIGHTNESS,
                     'mode': scene_lib.MODE_COLOR,
                     'color': list(HIGHLIGHT_COLOR),
                     'kelvin': 2700}
        named = 0

        for index, device in enumerate(devices):
            try:
                scene_lib.apply_settings(self.app.controller, device,
                                         highlight)
            except ControlError as exc:
                utils.log('Could not light %s for naming: %s'
                          % (device.name, exc))

            answer = _dialog().input(
                'Light %d of %d - which one is lit up?'
                % (index + 1, len(devices)), device.name)

            # Put it back before doing anything else, so a stop leaves the
            # room as it was rather than with one bulb stuck on magenta.
            self._restore(device, states.get(device.device_id))

            if not answer:
                # Kodi returns an empty string when the keyboard is cancelled,
                # which is the only signal available for "stop here".
                break
            answer = answer.strip()
            if answer and answer != device.name:
                device.name = answer
                named += 1

        return named

    def _restore(self, device, state):
        settings = scene_lib.state_to_settings(state)
        if not settings:
            return
        try:
            scene_lib.apply_settings(self.app.controller, device, settings)
        except ControlError as exc:
            utils.log('Could not restore %s after naming: %s'
                      % (device.name, exc))

    def _edit_device(self, device):
        """The menu for one device, built from what that device actually is.

        Rows are (label, handler) pairs rather than a list read back by
        index: which rows exist depends on the driver, so every new device
        type used to shift the numbering under the rows below it.
        """
        from devices import CAP_COMMANDS, CAP_POWER

        device = self._current(device)
        capabilities = self.app.controller.capabilities(device)
        driver = self.app.controller.driver_for(device)
        emitter = CAP_COMMANDS in capabilities
        keyed = hasattr(driver, 'set_local_key')
        testable = hasattr(driver, 'test_connection')

        # The device list travels down from the master too -- names, the
        # enabled flag, all of it -- so a satellite shows what a device is and
        # what it can do, and none of the ways to change it.
        owns = self.app.owns_data
        rows = []
        if owns:
            rows.extend([
                ('Rename (currently "%s")' % device.name,
                 lambda: self._rename_device(device)),
                ('Disable' if device.enabled else 'Enable',
                 lambda: self._toggle_enabled(device)),
            ])
            if CAP_POWER in capabilities or device.power_only:
                # Only offered for something that switches, and shown for a
                # device already set this way so it can be set back -- the
                # narrowing hides the very capabilities that put the row here.
                rows.append(
                    ('Let scenes control this light' if device.power_only
                     else 'Switch only (no colour or brightness)',
                     lambda: self._toggle_power_only(device)))

        if emitter:
            # An IR blaster has no light to flash and nothing to identify by,
            # so its menu is about the codes it knows instead. A beacon's
            # are clips it was given, not commands it learned, and the row
            # says which so nobody goes looking for a Learn that is not there.
            count = len(self.app.controller.commands(device))
            rows.append((('Commands (%d learned)...' % count
                          if hasattr(driver, 'start_learning')
                          else 'Clips (%d)...' % count),
                         lambda: self.command_menu(device)))
        else:
            # The keys travel down from the master with everything else, so
            # one typed in here would be overwritten by the master's copy --
            # or by the master not having one.
            if keyed and owns:
                rows.append(('Set local key%s'
                             % (' (needed)' if self.app.needs_local_key(device)
                                else ''),
                             lambda: self.set_local_key(device)))
            if CAP_COLOR in capabilities:
                # Only something that can show a colour has anything to flash.
                rows.append(('Identify (flash this light)',
                             lambda: self._identify(device)))
            if testable and not (keyed
                                 and self.app.needs_local_key(device)):
                rows.append(('Test connection',
                             lambda: self.test_device(device)))
            if hasattr(driver, 'set_power_memory') and not (
                    keyed and self.app.needs_local_key(device)):
                rows.append(('After a power cut...',
                             lambda: self.power_memory_menu(device)))

        # Anything that switches, not only the drivers that need a key. This
        # was written when Tuya was the only plug driver, and quietly denied
        # a Kasa plug the two rows it most wants.
        if CAP_COMMANDS not in capabilities and CAP_POWER in capabilities:
            rows.append(('Switch on', lambda: self._switch(device, True)))
            rows.append(('Switch off', lambda: self._switch(device, False)))

        if owns:
            rows.append(('Forget this device',
                         lambda: self._forget_device(device)))

        choice = _select(device.name, [label for label, _handler in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def _toggle_power_only(self, device):
        wanted = not device.power_only
        if not self.app.set_device_power_only(device, wanted):
            return
        if wanted:
            utils.notify('%s is switched only; scenes will pass over it'
                         % device.name)
        else:
            utils.notify('%s is a light again' % device.name)

    def _rename_device(self, device):
        name = _dialog().input('Device name', device.name)
        if name and self.app.rename_device(device, name):
            utils.notify('Renamed to %s' % device.name)

    def _toggle_enabled(self, device):
        if self.app.set_device_enabled(device, not device.enabled):
            utils.notify('%s %s' % (device.name,
                                    'enabled' if device.enabled else
                                    'disabled'))

    def _switch(self, device, on):
        """Power one device from its own menu.

        The place a plug most wants testing is right after its key is typed
        in, which is exactly where this sits.
        """
        try:
            self.app.controller.turn(device, on)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        utils.notify('%s %s' % (device.name, 'on' if on else 'off'))

    def _forget_device(self, device):
        if _dialog().yesno(
                utils.ADDON_NAME,
                'Forget "%s"?\n\nIts name and settings are removed. A '
                'later search will find it again as an unnamed device.'
                % device.name):
            self.app.forget_device(device)
            utils.notify('Forgot %s' % device.name)

    def power_memory_menu(self, device):
        """What the plug should do when mains power comes back.

        Read from the plug rather than remembered here: it is the plug's own
        setting and survives the add-on entirely, so showing a value we merely
        last wrote would be a guess that looks like a fact.
        """
        utils.notify('Reading %s...' % device.name)
        try:
            current, options = self.app.power_memory(device)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return

        if not options:
            _dialog().ok(utils.ADDON_NAME,
                         '%s did not report a power-cut setting.\n\n'
                         'Not every plug has one.' % device.name)
            return

        labels = []
        for label, value in options:
            labels.append('%s%s' % (label, '  (now)' if value == current
                                    else ''))
        choice = _select('%s after a power cut' % device.name, labels)
        if choice == BACK:
            return

        value = options[choice][1]
        if value == current:
            return
        try:
            self.app.set_power_memory(device, value)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        # Said plainly because the entry may be one outlet of several: there
        # is one relay memory in the box, however many sockets it has.
        _dialog().ok(utils.ADDON_NAME,
                     'After a power cut, %s will %s.\n\nThis is a setting '
                     'on the plug itself, so it covers every outlet on it and '
                     'holds whether or not Kodi is running.'
                     % (device.name, options[choice][0].lower()))

    def set_local_key(self, device):
        """Paste in the local key a Tuya device needs before it can be used."""
        value = _dialog().input(
            'Local key for %s (16 characters)' % device.name,
            self.app.controller.driver_for(device).local_key(device))
        if value is None:
            return
        if self.app.set_local_key(device, value):
            utils.notify('Key %s for %s'
                         % ('cleared' if not value.strip() else 'saved',
                            device.name))
        else:
            _dialog().ok(utils.ADDON_NAME,
                         'A Tuya local key is exactly 16 characters.\n\n'
                         'You gave %d.' % len(value.strip()))

    # -- the speed dial ------------------------------------------------------

    def dial_menu(self):
        """Ten slots, each one thing worth a single press from a phone."""
        while True:
            dial = self.app.dial
            rows = [(label, number + 1) for number, label
                    in enumerate(self._dial_labels(dial))]

            # Nothing to reorder until two of them are set, and a row that
            # says no when pressed is worse than no row.
            reorderable = len(dial_lib.filled(dial)) > 1
            if reorderable:
                rows.append(('Reorder the dial...', None))

            choice = _select('Speed dial', [row for row, _n in rows])
            if choice == BACK:
                return
            if reorderable and choice == len(rows) - 1:
                self.reorder_dial()
                continue
            self.edit_dial_slot(rows[choice][1])

    def reorder_dial(self):
        """Move slots about, staying on the screen between moves.

        The sequence editor's reordering, on the dial. A mode of its own rather
        than a "move up" on every slot: dragging one from tenth to first a row
        at a time is nine trips through the menu and the ninth is where the
        mistake gets made.
        """
        while True:
            dial = self.app.dial
            choice = _select('Pick a slot to move', self._dial_labels(dial))
            if choice == BACK:
                return
            if not dial_lib.is_filled(dial[choice]):
                utils.force_notify('Slot %d is empty' % (choice + 1))
                continue
            self._move_dial_slot(choice)

    def _move_dial_slot(self, index):
        """Ask where a slot should go, and put it there.

        The destination list is the dial as it stands, with the slot being
        moved marked in it. Naming what is in each position is what makes this
        answerable: "above the bedtime one" is the question being asked, and
        "position 6" is not.
        """
        dial = self.app.dial
        labels = self._dial_labels(dial)
        labels[index] += '   <- moving this'

        choice = _select('Move slot %d to' % (index + 1), labels)
        if choice == BACK or choice == index:
            return
        self.app.save_dial(dial_lib.move(dial, index, choice))
        utils.notify('Slot %d is now slot %d' % (index + 1, choice + 1))

    def _dial_labels(self, dial):
        """One numbered row per slot, empty ones included.

        Empty slots are listed because they are where a slot can be moved to --
        a dial of three with a gap at the top is reordered by moving one into
        that gap.
        """
        return ['%d.  %s' % (number,
                             self.app.dial_label(dial[number - 1])
                             if dial_lib.is_filled(dial[number - 1])
                             else 'Empty')
                for number in range(1, dial_lib.SLOT_COUNT + 1)]

    def edit_dial_slot(self, number):
        """What one slot does, and what it is called on the button."""
        while True:
            dial = self.app.dial
            slot = dial[number - 1]
            filled = dial_lib.is_filled(slot)

            rows = [('Does: %s' % (sequence_lib.describe_step(
                slot['step'], self._target_name(slot['step']))
                if filled else 'nothing yet'),
                lambda: self._set_dial_step(number))]
            if filled:
                # Only once there is something to name. A label on an empty
                # slot is a button that says a word and does nothing.
                rows.append(('Called: %s'
                             % (slot['label'] or 'whatever it does'),
                             lambda: self._name_dial_slot(number)))
                rows.append(('Try it now',
                             lambda: self.app.run_dial_slot(number)))
                rows.append(('Clear this slot',
                             lambda: self._clear_dial_slot(number)))

            choice = _select('Speed dial %d' % number,
                             [label for label, _h in rows])
            if choice == BACK:
                return
            if rows[choice][1]() is False:
                return

    def _set_dial_step(self, number):
        """Pick what a slot does, from everything a sequence step can be."""
        kinds = [('Scene', self._step_scene)]
        if self.app.sequences:
            kinds.append(('Sequence', lambda: self._step_sequence(None)))
        for driver_id in self._driver_ids(self.app.enabled_devices):
            kinds.append((self._driver_label(driver_id),
                          lambda d=driver_id: self._step_device(d)))

        choice = _select('Speed dial %d does what' % number,
                         [label for label, _h in kinds])
        if choice == BACK:
            return

        step = kinds[choice][1]()
        if step is None:
            return
        dial = self.app.dial
        dial[number - 1] = dial_lib.normalise_slot(
            {'label': dial[number - 1].get('label'), 'step': step})
        self.app.save_dial(dial)

    def _name_dial_slot(self, number):
        """A short name for the button, or nothing to use what it does."""
        dial = self.app.dial
        slot = dial[number - 1]
        name = _dialog().input('Name for speed dial %d' % number,
                               slot.get('label') or '')
        if name is None:
            return
        dial[number - 1] = dial_lib.normalise_slot(
            {'label': name, 'step': slot['step']})
        self.app.save_dial(dial)

    def _clear_dial_slot(self, number):
        dial = self.app.dial
        dial[number - 1] = dial_lib.empty_slot()
        self.app.save_dial(dial)
        utils.notify('Speed dial %d cleared' % number)
        return False

    # -- the phrase book -----------------------------------------------------

    def phrase_menu(self):
        """What the house has been taught to hear.

        Nothing here listens. A microphone is another machine's job and what
        reaches this is text, so the same screens build the book, try it and
        turn a miss into another way of asking -- all of which work today,
        with a keyboard, before anything has a microphone at all.
        """
        while True:
            book = self.app.phrases
            rows = [(voice_lib.describe_entry(
                entry, self.app.phrase_target(entry)),
                lambda e=entry: self.edit_phrase(e)) for entry in book]

            if self.app.owns_data:
                rows.append(('Teach it something...', self.new_phrase))
            # Only when there is one. An empty row saying nothing was missed
            # is a row that has to be pressed to find that out.
            misses = self.app.unheard
            if misses and self.app.owns_data:
                rows.append(('Heard but not known (%d)...' % len(misses),
                             self.unheard_menu))
            rows.append(('Try saying something...', self.try_phrase))

            choice = _select('Phrases', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def try_phrase(self):
        """Type what you would say, and watch what the house does about it.

        The whole point of the phrase book being text: it can be exercised
        from here, with the real matcher and the real actions, months before
        anything in the house has ears.
        """
        said = _dialog().input('Say something')
        if not said or not said.strip():
            return
        ran, message = self.app.run_phrase(said, announce=False)
        _dialog().ok('%s - %s' % (utils.ADDON_NAME,
                                  'did it' if ran else 'did nothing'),
                     message)

    def new_phrase(self):
        """A phrase and what it does, in that order.

        The phrase first because it is the part being invented. What it does
        is picked from what already exists, and asking for that first means
        backing out of a keyboard loses the choice.
        """
        said = _dialog().input('What will you say')
        if not said or not said.strip():
            return
        phrase = voice_lib.normalise(said)
        if not phrase:
            utils.force_notify('That is not something to say')
            return
        _index, existing = voice_lib.find_phrase(self.app.phrases, phrase)
        if existing is not None:
            _dialog().ok(utils.ADDON_NAME,
                         '"%s" already does this:\n\n%s'
                         % (phrase, sequence_lib.describe_step(
                             existing['step'],
                             self.app.phrase_target(existing))))
            return

        step = self._pick_phrase_step('"%s" does what' % phrase)
        if step is None:
            return
        book = self.app.phrases
        book.append({'phrases': [phrase], 'step': step})
        self.app.save_phrases(book)
        self.app.forget_unheard(phrase)
        utils.notify('"%s" is set' % phrase)

    def _pick_phrase_step(self, heading):
        """What a phrase does, from everything a step can be bar one.

        Unlocking is not offered. A phrase is not a PIN -- anyone within
        earshot is authenticated, including through an open window -- and the
        door is the one device where that matters enough to take the choice
        away rather than leave it to be got right.
        """
        kinds = [('Scene', self._step_scene)]
        if self.app.sequences:
            kinds.append(('Sequence', lambda: self._step_sequence(None)))
        for driver_id in self._driver_ids(self.app.enabled_devices):
            kinds.append((self._driver_label(driver_id),
                          lambda d=driver_id: self._step_device(d)))

        choice = _select(heading, [label for label, _h in kinds])
        if choice == BACK:
            return None
        step = kinds[choice][1]()
        if step is None:
            return None
        if (step or {}).get('kind') == sequence_lib.KIND_UNLOCK:
            _dialog().ok(utils.ADDON_NAME,
                         'A phrase cannot unlock a door.\n\n'
                         'Anyone who can be heard from the room can say it, '
                         'and that includes through an open window. Locking '
                         'is offered; unlocking is not.')
            return None
        return step

    def edit_phrase(self, entry):
        """One action, the ways of asking for it, and the way to be rid of it."""
        while True:
            index, current = voice_lib.find_phrase(self.app.phrases,
                                                   entry['phrases'][0])
            if current is None:
                return
            rows = [('Does: %s' % sequence_lib.describe_step(
                current['step'], self.app.phrase_target(current)), None)]
            for phrase in current['phrases']:
                rows.append(('"%s"' % phrase, phrase))

            labels = [label for label, _p in rows]
            extras = ['Another way of saying it...', 'Try it now',
                      'Forget this phrase']
            choice = _select('Phrase', labels + extras)
            if choice == BACK:
                return
            if choice < len(rows):
                if rows[choice][1] is None:
                    continue
                self._drop_phrase(index, rows[choice][1])
                continue
            extra = extras[choice - len(rows)]
            if extra.startswith('Another'):
                self._add_phrase(index)
            elif extra.startswith('Try'):
                ran, message = self.app.run_phrase(current['phrases'][0],
                                                   announce=False)
                _dialog().ok('%s - %s' % (utils.ADDON_NAME,
                                          'did it' if ran else 'did nothing'),
                             message)
            else:
                if _dialog().yesno(utils.ADDON_NAME,
                                   'Forget every way of asking for this?'):
                    book = self.app.phrases
                    del book[index]
                    self.app.save_phrases(book)
                    return

    def _add_phrase(self, index, said=None):
        """Another way of asking for what entry `index` already does."""
        if said is None:
            said = _dialog().input('Another way of saying it')
        if not said or not said.strip():
            return
        phrase = voice_lib.normalise(said)
        if not phrase:
            return
        book = self.app.phrases
        where, existing = voice_lib.find_phrase(book, phrase)
        if existing is not None:
            if where != index:
                utils.force_notify('"%s" already does something else' % phrase)
            return
        book[index]['phrases'].append(phrase)
        self.app.save_phrases(book)
        self.app.forget_unheard(phrase)
        utils.notify('"%s" added' % phrase)

    def _drop_phrase(self, index, phrase):
        """Forget one way of asking, keeping the rest and what they do."""
        book = self.app.phrases
        if len(book[index]['phrases']) == 1:
            utils.force_notify('That is the only way to ask for this. '
                               'Forget the phrase instead.')
            return
        if not _dialog().yesno(utils.ADDON_NAME,
                               'Stop answering to "%s"?' % phrase):
            return
        book[index]['phrases'] = [p for p in book[index]['phrases']
                                  if p != phrase]
        self.app.save_phrases(book)

    def unheard_menu(self):
        """What was said that meant nothing, and what to do about it.

        This is the screen that makes the book converge. Speech recognition
        will hear "make me a coffee" as "make me coffee", and the fix is
        another way of asking rather than a cleverer matcher -- so a miss is
        one press from becoming one.
        """
        while True:
            misses = self.app.unheard
            if not misses:
                return
            rows = [(voice_lib.describe_miss(entry), entry)
                    for entry in misses]
            choice = _select('Heard but not known',
                             [label for label, _e in rows])
            if choice == BACK:
                return
            self._use_miss(rows[choice][1]['text'])

    def _use_miss(self, said):
        """Turn one miss into a phrase, or be rid of it."""
        book = self.app.phrases
        rows = [('Make it a new phrase', None)]
        for index, entry in enumerate(book):
            rows.append(('Another way to say: %s'
                         % sequence_lib.describe_step(
                             entry['step'], self.app.phrase_target(entry)),
                         index))
        rows.append(('Forget it was ever said', -1))

        choice = _select('"%s"' % said, [label for label, _i in rows])
        if choice == BACK:
            return
        where = rows[choice][1]
        if where is None:
            step = self._pick_phrase_step('"%s" does what' % said)
            if step is None:
                return
            book.append({'phrases': [said], 'step': step})
            self.app.save_phrases(book)
            self.app.forget_unheard(said)
            utils.notify('"%s" is set' % said)
        elif where == -1:
            self.app.forget_unheard(said)
        else:
            self._add_phrase(where, said)

    # -- sequences -----------------------------------------------------------

    def sequence_menu(self):
        """The saved sequences. Picking one runs it."""
        while True:
            sequences = self.app.sequences
            rows = []
            for sequence in sequences:
                summary = sequence_lib.describe(sequence)
                # A failure is said in the list, not only inside the editor.
                # "Did the 07:00 one work" should be answerable by looking.
                failed = (self.app.last_run(sequence['name']) or {}).get(
                    sequence_lib.FAILED) or 0
                if failed:
                    summary = '[%d FAILED]  %s' % (failed, summary)
                if sequence_lib.scheduled(sequence):
                    summary = '%s  -  %s' % (
                        sequence_lib.describe_schedule(sequence), summary)
                # One part way through a long pause says so here rather than
                # only when it is opened: what the list is for is knowing what
                # the house is doing without pressing anything.
                waiting = self.app.pending_for(sequence['name'])
                if waiting is not None:
                    summary = 'Waiting %s  -  %s' % (
                        sequence_lib.describe_wait(
                            max(0, waiting['at'] - time.time())),
                        summary)
                rows.append(('%s  -  %s' % (sequence['name'], summary),
                             lambda r=sequence: self.run_sequence(r)))
            # A satellite copies its sequences from the master and cannot
            # save its own, so it is not offered the chance to write one that
            # would be gone at the next sync. Running one by hand is
            # untouched -- that is the whole point of a satellite.
            if self.app.owns_data:
                rows.append(('New sequence...', self.new_sequence))
                if sequences:
                    rows.append(('Manage sequences...', self.manage_sequences))

            choice = _select('Sequences', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def run_sequence(self, sequence):
        """Run a sequence, showing progress and letting a long one be stopped.

        Short pauses are waited out here, behind a cancellable progress dialog.
        A long one is not: the sequence hands it back, the dialog closes, and
        the service carries the rest on when the wait is over. Sitting on a
        long pause here would freeze the menu -- the dialog cannot even be
        cancelled during a sleep, because iscanceled is only asked between
        steps -- and would hold the box against everything else meanwhile.
        """
        name = sequence['name']
        steps = sequence_lib.filled_steps(sequence)
        if not steps:
            utils.force_notify('%s has no steps yet' % name)
            return

        waiting = self.app.pending_for(name)
        if waiting is not None:
            # Already part way through. Offer to drop the rest rather than
            # starting it again, which would repeat its opening steps.
            left = sequence_lib.describe_wait(
                max(0, waiting['at'] - time.time()))
            if _dialog().yesno(
                    utils.ADDON_NAME,
                    '%s is part way through. It carries on in %s.\n\n'
                    'Stop it there?' % (name, left)):
                self.app.cancel_pending(name)
                utils.force_notify('%s stopped' % name)
            return

        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME, 'Running %s...' % name)
        # Counted on what will really run, not on the fifteen slots: a sequence
        # that runs others is longer than its own list, and the bar was filling
        # up and then carrying on past the end.
        total = len(sequence_lib.expand(sequence, self.app.sequences))

        def announce(index, step):
            if progress.iscanceled():
                return False
            progress.update(int(100.0 * index / max(1, total)),
                            'Running %s...' % name,
                            sequence_lib.describe_step(
                                step, self._target_name(step)))
            return True

        try:
            self.app.run_sequence(sequence, on_step=announce, defer=True)
        finally:
            progress.close()

    def _target_name(self, step):
        """The friendly name of a step's target, for showing it back."""
        if step.get('kind') == sequence_lib.KIND_SCENE:
            return step.get('target')
        if step.get('target') == sequence_lib.TARGET_ALL:
            return None
        found = sequence_lib.resolve_targets(step, self.app.devices)
        return found[0].name if found else None

    def new_sequence(self):
        name = _dialog().input('Name for the new sequence', '')
        if not name or not name.strip():
            return
        if self.app.sequence_by_name(name):
            _dialog().ok(utils.ADDON_NAME,
                         'There is already a sequence called "%s".'
                         % name.strip())
            return
        sequence = self.app.save_sequence(sequence_lib.make_sequence(name.strip()))
        if sequence:
            self.edit_sequence(sequence)

    def manage_sequences(self):
        while True:
            sequences = self.app.sequences
            if not sequences:
                return
            rows = [(r['name'], lambda r=r: self.edit_sequence(r))
                    for r in sequences]
            choice = _select('Manage sequences', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def edit_sequence(self, sequence):
        """Every slot, always all of them, numbered as they run."""
        while True:
            rows = []
            for index, label in enumerate(self._slot_labels(sequence)):
                rows.append((label,
                             lambda i=index: self.edit_step(sequence, i)))
            # Nothing to reorder until there are two things to put in an
            # order, and an extra row on a sequence with one step is just a
            # row that says no when you press it.
            if len(sequence_lib.filled_steps(sequence)) > 1:
                rows.append(('Reorder steps...',
                             lambda: self.reorder_steps(sequence)))
            used = self.app.sequence_used_by(sequence['name'])
            if used:
                rows.append(('Used by: %s' % ', '.join(used),
                             lambda: self._show_used_by(sequence)))
            rows.append(('Runs: %s' % sequence_lib.describe_schedule(sequence),
                         lambda: self.schedule_sequence(sequence)))
            last = self.app.last_run(sequence['name'])
            if last:
                rows.append(('Last run: %s' % sequence_lib.describe_run(last),
                             lambda: self.show_last_run(sequence['name'])))
            rows.append(('Skip what is already done: %s'
                         % ('yes' if sequence.get('skip_done') else 'no'),
                         lambda: self._toggle_skip_done(sequence)))
            rows.append(('Run it now', lambda: self.run_sequence(sequence)))
            rows.append(('Rename', lambda: self._rename_sequence(sequence)))
            rows.append(('Duplicate...',
                         lambda: self._duplicate_sequence(sequence)))
            rows.append(('Delete this sequence',
                         lambda: self._delete_sequence(sequence)))

            choice = _select(sequence['name'], [label for label, _h in rows])
            if choice == BACK:
                return
            if rows[choice][1]() is False:
                return

    def _slot_labels(self, sequence):
        """Every slot numbered and described, as the editor lists them.

        One place rather than three, so the reorder screens read as the same
        list the sequence editor showed rather than a second opinion of it.
        """
        return ['%2d. %s' % (index + 1,
                             sequence_lib.describe_step(
                                 step, self._target_name(step)))
                for index, step in enumerate(sequence['steps'])]

    def reorder_steps(self, sequence):
        """Move steps about, staying on the screen between moves.

        A mode of its own rather than a "move up" on every slot: nudging a
        step from the bottom of fifteen to the top one row at a time is
        fourteen trips through the menu, and the fourteenth is where the
        mistake gets made. Pick a step, say where it goes, done.

        The loop is the point -- reordering is rarely one move -- so it stays
        open until you back out of it.
        """
        while True:
            choice = _select('%s - pick a step to move' % sequence['name'],
                             self._slot_labels(sequence))
            if choice == BACK:
                return
            if sequence['steps'][choice].get('kind') == sequence_lib.KIND_NONE:
                utils.force_notify('Slot %d is empty' % (choice + 1))
                continue
            self._move_step(sequence, choice)

    def _move_step(self, sequence, index):
        """Ask where a step should go, and put it there.

        The destination list is the slots as they stand now, with the step
        being moved marked in it. Naming what is already in each slot is what
        makes this answerable: "before the scene" is the question actually
        being asked, and "position 6" is not.
        """
        if sequence['steps'][index].get('kind') == sequence_lib.KIND_NONE:
            utils.force_notify('Slot %d is empty' % (index + 1))
            return
        labels = self._slot_labels(sequence)
        labels[index] += '   <- moving this'

        choice = _select('Move step %d to' % (index + 1), labels)
        if choice == BACK or choice == index:
            return
        sequence['steps'] = sequence_lib.move_step(sequence['steps'],
                                                   index, choice)
        self.app.save_sequence(sequence)
        utils.notify('Step %d is now step %d' % (index + 1, choice + 1))

    def _show_used_by(self, sequence):
        """Where a sequence is used, so a change is not a surprise elsewhere."""
        used = self.app.sequence_used_by(sequence['name'])
        _dialog().ok(utils.ADDON_NAME,
                     '"%s" runs at:\n\n%s\n\nEditing it changes all of '
                     'them.' % (sequence['name'], '\n'.join(used)))

    def _duplicate_sequence(self, sequence):
        """Copy a sequence under a new name and open the copy.

        The copy is deliberately not scheduled, however the original was. Two
        sequences firing at the same minute on the same days is not what
        anyone means by "make me a variant of this", and it is the sort of
        thing that would only be noticed the following morning.
        """
        suggestion = self._copy_name(
            sequence['name'],
            lambda n: self.app.sequence_by_name(n) is not None)
        name = _dialog().input('Name for the copy', suggestion)
        if name is None or not name.strip():
            return
        name = name.strip()
        if self.app.sequence_by_name(name) is not None:
            _dialog().ok(utils.ADDON_NAME,
                         'There is already a sequence called "%s".' % name)
            return

        # Deep, as for a scene: the steps are a list of dicts and a shallow
        # copy would leave the two editing the same steps.
        made = copy.deepcopy(sequence)
        made['name'] = name
        made['time'] = ''
        made['days'] = []
        made['phase'] = 0

        copied = self.app.save_sequence(made)
        if copied is None:
            utils.force_notify('That sequence could not be copied')
            return
        if sequence_lib.scheduled(sequence):
            _dialog().ok(utils.ADDON_NAME,
                         '"%s" is a copy of "%s" with no schedule of its own.'
                         '\n\nTwo of them running at the same moment is '
                         'rarely what a copy is for, so set its own under '
                         '"Runs:".' % (name, sequence['name']))
        else:
            utils.notify('Copied to "%s"' % name)
        self.edit_sequence(copied)
        return False

    def show_last_run(self, name):
        """Every step of the last run and what became of it.

        The counts answer "did it work"; this answers "which bit did not", and
        that is the one that takes you to the blind with the flat battery.
        """
        record = self.app.last_run(name)
        if not record:
            utils.force_notify('%s has not run yet' % name)
            return
        lines = ['%s  -  %s' % (name, sequence_lib.describe_run(record)), '']
        for entry in record.get('steps') or []:
            line = '%d. %s  --  %s' % (
                entry.get('n', 0), entry.get('what', ''),
                sequence_lib.describe_outcome(entry.get('outcome')))
            why = (entry.get('why') or '').strip()
            if why:
                # The reason is the whole point of keeping this. "FAILED" on
                # its own is what the log already said.
                line += ': %s' % why
            lines.append(line)
        _dialog().ok(utils.ADDON_NAME, '\n'.join(lines))

    def _toggle_skip_done(self, sequence):
        """Ask the devices where they are, and leave alone what is already there.

        Off by default, and said plainly when it is turned on, because it rests
        on devices telling the truth about themselves. Some do not -- that is
        what Check status reporting exists to find out -- and a blind left open
        because a plug lied is worse than a motor running for two seconds.
        """
        turning_on = not sequence.get('skip_done')
        if turning_on and not _dialog().yesno(
                utils.ADDON_NAME,
                'Before each step, ask the device where it is, and skip the '
                'step if it is already there.\n\n'
                'This asks over the network, so the sequence starts a moment '
                'later, and it trusts what each device reports.\n\n'
                'Turn it on for "%s"?' % sequence['name']):
            return
        sequence['skip_done'] = turning_on
        self.app.save_sequence(sequence)
        utils.notify('%s: %s already-done steps'
                     % (sequence['name'],
                        'skipping' if turning_on else 'no longer skipping'))

    def _rename_sequence(self, sequence):
        name = _dialog().input('Sequence name', sequence['name'])
        if not name or not name.strip() or name.strip() == sequence['name']:
            return
        if self.app.sequence_by_name(name):
            _dialog().ok(utils.ADDON_NAME,
                         'There is already a sequence called "%s".'
                         % name.strip())
            return
        _renamed, moved = self.app.rename_sequence(sequence, name)
        if moved:
            utils.notify('Renamed, and %d place(s) now point at it' % moved)

    def _delete_sequence(self, sequence):
        name = sequence['name']
        used = self.app.sequence_used_by(name)
        question = 'Delete the sequence "%s"?' % name
        if used:
            # Said before it happens rather than found out at six in the
            # morning when the shutdown gives up at the step that named it.
            question += ('\n\nIt is used by: %s.\nThose will stop working.'
                         % ', '.join(used))
        if not _dialog().yesno(utils.ADDON_NAME, question):
            return
        self.app.delete_sequence(sequence)
        utils.notify('Deleted %s' % name)
        return False

    # -- reracks: a day in nine phases -------------------------------------

    def rerack_menu(self):
        """The nine presets, and the week that says which day gets which."""
        while True:
            rows = [('Which rerack runs on which day...', self.week_menu)]
            for rerack in self.app.reracks:
                days = self._days_using(rerack['name'])
                label = '%s  -  %s' % (rerack['name'],
                                       rerack_lib.describe(rerack))
                if days:
                    label += '  [%s]' % days
                rows.append((label,
                             lambda r=rerack: self.edit_rerack(r)))

            choice = _select('Reracks', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _days_using(self, name):
        return ', '.join(rerack_lib.DAYS[day][:3]
                         for day, assigned
                         in enumerate(self.app.effective_week())
                         if assigned == name)

    def week_menu(self):
        """Which rerack each day gets, laid out as a week."""
        import paragon_tv

        while True:
            following = self.app.week_follows_tv
            week = self.app.effective_week()
            rows = []

            if paragon_tv.installed():
                rows.append(('Days: %s'
                             % ('matched to Paragon TV' if following
                                else 'my own'),
                             self._toggle_week_follows_tv))

            for day, name in enumerate(week):
                label = '%-10s %s' % (rerack_lib.DAYS[day], name or 'nothing')
                rows.append((label, lambda d=day: self._pick_day_rerack(d)))

            if paragon_tv.installed() and not following:
                rows.append(('Copy Paragon TV\'s days once',
                             self._copy_week_from_tv))

            choice = _select('Which rerack runs on which day',
                             [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _show_tv_report(self, rerack):
        """Exactly what Paragon Home can read, and why it might be nothing."""
        import paragon_tv

        _dialog().ok('%s - Paragon TV' % utils.ADDON_NAME,
                     paragon_tv.report(rerack['name'], sequence_lib.now()))

    def _toggle_week_follows_tv(self):
        """Match the week to Paragon TV's, and keep matching it.

        Not a copy: the table is read from Paragon TV every time it is
        needed, so changing a day there changes it here with nothing to
        press. Our own table is kept and comes back if this is switched off.
        """
        import paragon_tv

        if not self.app.week_follows_tv:
            if not paragon_tv.enabled():
                _dialog().ok(utils.ADDON_NAME,
                             'Paragon TV is installed but its Rerack system '
                             'is switched off, so it has no days to match.')
                return
            self.app.set_week_follows_tv(True)
            _dialog().ok(
                utils.ADDON_NAME,
                'The week now matches Paragon TV, and keeps matching it.\n\n'
                'Change a day there and it changes here too. Your own days '
                'are kept and come back if you switch this off.')
            return

        self.app.set_week_follows_tv(False)
        utils.notify('The week is your own again')

    def _copy_week_from_tv(self):
        """Take Paragon TV's days once, as a starting point to edit."""
        import paragon_tv

        if not paragon_tv.enabled():
            _dialog().ok(utils.ADDON_NAME,
                         'Paragon TV is installed but its Rerack system is '
                         'switched off, so it has no days to copy.')
            return
        if not _dialog().yesno(
                utils.ADDON_NAME,
                'Replace the days here with Paragon TV\'s?\n\nThis is a '
                'copy you can then change. To keep them matched instead, use '
                '"Days: my own" above.'):
            return
        taken = self.app.copy_week_from_tv()
        utils.notify('Copied %d day(s) from Paragon TV' % taken)

    def _pick_day_rerack(self, weekday):
        if self.app.week_follows_tv:
            _dialog().ok(utils.ADDON_NAME,
                         'These days come from Paragon TV.\n\nChange them '
                         'there, or switch "Days" above back to your own.')
            return
        options = ['Nothing'] + [r['name'] for r in self.app.reracks]
        choice = _select(rerack_lib.DAYS[weekday], options)
        if choice == BACK:
            return
        self.app.set_day(weekday, '' if choice == 0 else options[choice])

    def edit_rerack(self, rerack):
        """The nine phases, always all nine, and where their times come from."""
        import paragon_tv

        while True:
            tv_times = self.app.tv_phase_times(rerack, sequence_lib.now())
            rows = []
            for number in range(1, rerack_lib.PHASE_COUNT + 1):
                phase = rerack['phases'][number - 1]
                rows.append((rerack_lib.describe_phase_row(
                    rerack, number, phase, tv_times.get(number)),
                    lambda n=number: self.edit_phase(rerack, n)))

            if paragon_tv.installed() or rerack_lib.needs_tv(rerack):
                rows.append(('What Paragon TV says about %s'
                             % rerack['name'],
                             lambda: self._show_tv_report(rerack)))
            rows.append(('Run this rerack now',
                         lambda: self._run_rerack_now(rerack)))

            choice = _select('%s  -  %s' % (rerack['name'],
                                            self._days_using(rerack['name'])
                                            or 'no day yet'),
                             [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def edit_phase(self, rerack, number):
        """What a phase does, and when -- unless Paragon TV says when."""
        phase = rerack['phases'][number - 1]
        rows = [('Sequence: %s' % (phase['sequence'] or 'none'),
                 lambda: self._pick_phase_sequence(rerack, number))]
        rows.append(('Time: %s' % (phase['time'] or 'with Paragon TV'),
                     lambda: self._pick_phase_time(rerack, number)))
        if phase['sequence']:
            rows.append(('Clear this phase',
                         lambda: self._clear_phase(rerack, number)))

        choice = _select('%s - %s' % (rerack['name'],
                                      sequence_lib.describe_phase(number)),
                         [label for label, _h in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def learn_rf_command(self, device, sleep_func=None):
        """Learn a radio code.

        Two passes by default, because the blaster does not know which
        frequency to listen on: it sweeps for one while the button is held
        down, and only then listens for the code. That is why this asks for
        two presses where infrared asks for one, and why the first is a hold
        rather than a tap -- a sweep needs something to find.

        Given the frequency, the first pass is skipped and one press does it.
        Blind and shade remotes print theirs on the back, and 433.92 MHz is
        most of them.
        """
        import time

        sleep = sleep_func or time.sleep
        frequency = self._ask_rf_frequency()
        if frequency is BACK:
            return

        if frequency is not None:
            try:
                taken = self.app.start_rf_capture(device, frequency)
            except ControlError as exc:
                utils.force_notify(str(exc))
                return
            if taken:
                self._rf_capture_code(device, sleep)
                return
            # An older Pro will not take one. Sweeping still works, so say so
            # once and carry on rather than making the user start again.
            utils.force_notify('%s cannot be told the frequency. Sweeping '
                               'for it instead.' % device.name)

        if not self._rf_sweep(device, sleep):
            return
        try:
            self.app.start_rf_capture(device)
        except ControlError as exc:
            self.app.cancel_rf_sweep(device)
            utils.force_notify(str(exc))
            return
        self._rf_capture_code(device, sleep)

    def _ask_rf_frequency(self):
        """The frequency to listen on: a number in MHz, None to sweep, or BACK.

        Offered before anything is sent, because knowing the frequency changes
        what the user is about to be asked to do -- one press rather than a
        hold and a press.
        """
        rows = [('Sweep for it (hold the button down)', None)]
        rows.extend((('%s MHz' % text, value) for text, value in RF_PRESETS))
        rows.append(('Type the frequency...', TYPE_FREQUENCY))

        choice = _select('How should the frequency be found?',
                         [label for label, _value in rows])
        if choice == BACK:
            return BACK
        picked = rows[choice][1]
        if picked is not TYPE_FREQUENCY:
            return picked

        typed = _dialog().input('Frequency in MHz, e.g. 433.92')
        if not typed:
            return BACK
        try:
            value = float(typed.strip())
        except (TypeError, ValueError):
            utils.force_notify('%s is not a frequency in MHz' % typed)
            return BACK
        if not RF_MIN_MHZ <= value <= RF_MAX_MHZ:
            utils.force_notify('%s MHz is outside what these blasters can '
                               'reach (%g to %g)'
                               % (typed, RF_MIN_MHZ, RF_MAX_MHZ))
            return BACK
        return value

    def _rf_sweep(self, device, sleep):
        """Hunt for the frequency while the button is held. True if found."""
        try:
            self.app.start_rf_sweep(device)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return False

        found = False
        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME,
                        'Hold the button down on your remote, close to %s.'
                        % device.name,
                        'Finding the frequency. Keep holding.')
        try:
            for step in range(LEARN_ATTEMPTS):
                progress.update(int(step * 100.0 / LEARN_ATTEMPTS))
                if progress.iscanceled():
                    break
                if self.app.rf_frequency_found(device):
                    found = True
                    break
                sleep(LEARN_POLL)
        finally:
            progress.close()

        if not found:
            # Leaving it sweeping would make the next ordinary command look
            # broken, so it is stopped whether this was a cancel or a failure.
            self.app.cancel_rf_sweep(device)
            _dialog().ok(utils.ADDON_NAME,
                         'No frequency was found.\n\nHold the button down '
                         'rather than tapping it, and keep the remote within '
                         'a foot of %s.\n\nIf this blaster is a Mini it has '
                         'no radio at all and can learn infrared only.'
                         % device.name)
        return found

    def _rf_capture_code(self, device, sleep):
        """Listen for the code and save it under a name the user gives."""
        code = None
        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME,
                        'Press the button on your remote.',
                        'A single press, close to %s.' % device.name)
        try:
            for step in range(LEARN_ATTEMPTS):
                progress.update(int(step * 100.0 / LEARN_ATTEMPTS))
                if progress.iscanceled():
                    break
                code = self.app.collect_learned(device)
                if code:
                    break
                sleep(LEARN_POLL)
        finally:
            progress.close()

        if not code:
            self.app.cancel_rf_sweep(device)
            _dialog().ok(utils.ADDON_NAME,
                         'No code came through.\n\nTry again and press the '
                         'button firmly while the dialog is open.\n\nSome '
                         'remotes change their code on every press for '
                         'security. Those cannot be replayed by anything, '
                         'and this will never capture one.')
            return

        name = _dialog().input('Name for this RF command', '')
        if not name or not name.strip():
            self.app.cancel_rf_sweep(device)
            return
        if self.app.save_command(device, name.strip(), code):
            utils.notify('Learned %s' % name.strip())

    def _pick_phase_sequence(self, rerack, number):
        names = [s['name'] for s in self.app.sequences]
        if not names:
            utils.force_notify('No sequences to choose from yet')
            return
        choice = _select('Which sequence', names)
        if choice == BACK:
            return
        rerack['phases'][number - 1]['sequence'] = names[choice]
        self.app.save_reracks()

    def _pick_phase_time(self, rerack, number):
        """Its own time, or Paragon TV's for the same phase.

        Two rows rather than a blank meaning something: "leave it empty and it
        follows the television" is true but not the sort of thing a menu
        should expect anyone to work out.
        """
        phase = rerack['phases'][number - 1]
        choice = _select(sequence_lib.describe_phase(number).capitalize(),
                         ['Run with Paragon TV%s'
                          % ('  (now)' if not phase['time'] else ''),
                          'Set a time of my own%s'
                          % ('  (now %s)' % phase['time'] if phase['time']
                             else '')])
        if choice == BACK:
            return
        if choice == 0:
            phase['time'] = ''
            self.app.save_reracks()
            return

        value = _dialog().input('Time of day (18:00, or 6pm)',
                                phase['time'] or '')
        if value is None:
            return
        parsed = sequence_lib.parse_time(value)
        if not parsed:
            _dialog().ok(utils.ADDON_NAME,
                         'Could not read "%s" as a time.\n\n'
                         'Try 18:00, 6pm or 1800.' % value.strip())
            return
        phase['time'] = parsed
        self.app.save_reracks()

    def _clear_phase(self, rerack, number):
        rerack['phases'][number - 1] = rerack_lib.empty_phase()
        self.app.save_reracks()

    def _run_rerack_now(self, rerack):
        """Run every filled phase in order, ignoring the clock."""
        filled = rerack_lib.filled_phases(rerack)
        if not filled:
            utils.force_notify('%s has no phases yet' % rerack['name'])
            return
        for number, phase in filled:
            sequence = self.app.sequence_by_name(phase['sequence'])
            if sequence is None:
                utils.force_notify('No sequence called "%s"'
                                   % phase['sequence'])
                continue
            self.run_sequence(sequence)

    # -- when it runs ------------------------------------------------------

    def schedule_sequence(self, sequence):
        """Its own clock, or one of Paragon TV's phases. Not both."""
        import paragon_tv

        while True:
            following = sequence_lib.follows_tv(sequence)
            rows = []
            if not following:
                rows.append(('Time: %s' % (sequence['time'] or 'not set'),
                             lambda: self._edit_sequence_time(sequence)))
                rows.append(('Days: %s' % (self._days_label(sequence) or 'none'),
                             lambda: self._edit_sequence_days(sequence)))

            if paragon_tv.installed():
                rows.append(('Follow Paragon TV: %s'
                             % (sequence_lib.describe_phase(sequence['phase'])
                                if following else 'no'),
                             lambda: self._edit_sequence_phase(sequence)))
                if following:
                    rows.append(("What Paragon TV says about today",
                                 lambda: self._show_tv_status()))

            if sequence_lib.scheduled(sequence):
                rows.append(('Stop running it on a schedule',
                             lambda: self._clear_schedule(sequence)))

            choice = _select('%s - when it runs' % sequence['name'],
                             [label for label, _h in rows])
            if choice == BACK:
                self.app.save_sequence(sequence)
                return
            if rows[choice][1]() is False:
                self.app.save_sequence(sequence)
                return

    @staticmethod
    def _days_label(sequence):
        return ', '.join(sequence_lib.DAYS[day][:3]
                         for day in sequence.get('days') or [])

    def _edit_sequence_time(self, sequence):
        value = _dialog().input(
            'Time of day (18:00, or 6pm)', sequence['time'] or '')
        if value is None:
            return
        if not value.strip():
            sequence['time'] = ''
            return
        parsed = sequence_lib.parse_time(value)
        if not parsed:
            _dialog().ok(utils.ADDON_NAME,
                         'Could not read "%s" as a time.\n\n'
                         'Try 18:00, 6pm or 1800.' % value.strip())
            return
        sequence['time'] = parsed

    def _edit_sequence_days(self, sequence):
        """A toggled checklist, as the scene target picker is and for the
        same reason: Krypton's multiselect differs across skins."""
        chosen = set(sequence.get('days') or [])
        while True:
            options = ['Every day', 'Weekdays', 'Weekends']
            for index, name in enumerate(sequence_lib.DAYS):
                options.append('%s %s' % ('[x]' if index in chosen else '[ ]',
                                          name))
            options.append('Done')

            choice = _select('Which days', options)
            if choice == BACK or choice == len(options) - 1:
                sequence['days'] = sorted(chosen)
                return
            if choice == 0:
                chosen = set(range(7))
            elif choice == 1:
                chosen = set(sequence_lib.WEEKDAYS)
            elif choice == 2:
                chosen = set(sequence_lib.WEEKEND)
            else:
                day = choice - 3
                chosen.symmetric_difference_update([day])

    def _edit_sequence_phase(self, sequence):
        """Hang this sequence off one of Paragon TV's nine phases.

        Following Paragon TV replaces its own time and days rather than
        adding to them: two schedules on one sequence would be two answers to
        one question.
        """
        import paragon_tv

        options = ['Keep its own time instead']
        for phase in range(1, paragon_tv.PHASE_COUNT + 1):
            options.append(paragon_tv.describe_phase(phase).capitalize())

        choice = _select('Follow which Paragon TV phase', options)
        if choice == BACK:
            return
        if choice == 0:
            sequence['phase'] = 0
            return

        sequence['phase'] = choice
        sequence['time'] = ''
        sequence['days'] = []

    def _show_tv_status(self):
        """What Paragon TV's own schedule says, so a phase can be checked."""
        import paragon_tv

        _dialog().ok('%s - Paragon TV' % utils.ADDON_NAME,
                     paragon_tv.status(sequence_lib.now()))

    def _clear_schedule(self, sequence):
        sequence['time'] = ''
        sequence['days'] = []
        sequence['phase'] = 0
        utils.notify('%s runs only when you run it' % sequence['name'])
        return False

    # -- one step ----------------------------------------------------------

    def edit_step(self, sequence, index):
        """Pick a step in the order you would say it: kind, target, action."""
        kinds = [('Scene', self._step_scene)]
        # Offered only when there is another sequence to reach for. On a house
        # with one sequence the row would always say no when pressed.
        if self._nestable(sequence):
            kinds.append(('Sequence',
                          lambda: self._step_sequence(sequence)))
        for driver_id in self._driver_ids(self.app.enabled_devices):
            kinds.append((self._driver_label(driver_id),
                          lambda d=driver_id: self._step_device(d)))
        kinds.append(('Pause after this step', self._step_pause))
        # Offered only on a slot that holds something, since moving an empty
        # slot somewhere else achieves nothing but renumbering.
        if sequence['steps'][index].get('kind') != sequence_lib.KIND_NONE:
            kinds.append(('Move this step...', self._step_move))
        kinds.append(('Clear this step', lambda: sequence_lib.empty_step()))

        choice = _select('Step %d' % (index + 1),
                         [label for label, _h in kinds])
        if choice == BACK:
            return

        step = kinds[choice][1]()
        if step is None:
            return
        if step == 'pause':
            self._ask_pause(sequence, index)
            return
        if step == 'move':
            self._move_step(sequence, index)
            return

        # A step's pause belongs to the slot rather than to what is in it, so
        # replacing the action does not silently drop the gap after it.
        step['pause'] = sequence['steps'][index].get('pause', 0)
        sequence['steps'][index] = sequence_lib.normalise_step(step)
        self.app.save_sequence(sequence)

    def _step_scene(self):
        scenes = self.app.scenes
        if not scenes:
            utils.force_notify('No scenes to choose from yet')
            return None
        choice = _select('Which scene', [s['name'] for s in scenes])
        if choice == BACK:
            return None
        return {'kind': sequence_lib.KIND_SCENE,
                'target': scenes[choice]['name']}

    def _nestable(self, sequence=None):
        """The sequences this one may run: not itself, and nothing that loops.

        A host of None means there is no host -- a speed dial slot, which sits
        outside every sequence and so cannot be inside one. Everything is
        offered there.
        """
        if sequence is None:
            return list(self.app.sequences)
        return [other for other in self.app.sequences
                if not sequence_lib.would_loop(self.app.sequences,
                                               sequence.get('name'),
                                               other.get('name'))]

    def _lock_them(self, targets, heading):
        """Throw the bolts. Asked about first, because a door is not a lamp."""
        if not _dialog().yesno(utils.ADDON_NAME, 'Lock %s?' % heading):
            return
        locked, problems = 0, []
        for device in targets:
            try:
                self.app.controller.lock(device)
                locked += 1
            except Exception as exc:
                problems.append('%s: %s' % (device.name, exc))
        if problems:
            # Forced, and named: a bolt that did not throw is the one thing
            # here worth interrupting somebody for.
            utils.force_notify(problems[0])
        elif locked:
            utils.notify('%s locked' % heading)

    def _unlock_them(self, targets, heading):
        """Withdraw the bolts.

        Asks first only where the box has been told to. The permission to
        unlock at all is already a deliberate one, so a prompt on every press
        is a keystroke rather than a decision -- but it is one setting away for
        anyone who wants it, and it is worded plainly when it does appear:
        "Unlock?" beside "Lock?" on a remote, half-read from a sofa, is two
        words a letter apart.
        """
        if getattr(self.app.controller, 'confirm_unlock', False):
            if not _dialog().yesno(
                    utils.ADDON_NAME,
                    'Open %s?\n\nThis withdraws the bolt. Anyone at the door '
                    'can walk in.' % heading):
                return
        opened, problems = 0, []
        for device in targets:
            try:
                self.app.controller.unlock(device)
                opened += 1
            except Exception as exc:
                problems.append('%s: %s' % (device.name, exc))
        if problems:
            utils.force_notify(problems[0])
        elif opened:
            # Forced, not the quiet kind. An unlocked door is worth a
            # notification that does not fade behind whatever is playing.
            utils.force_notify('%s UNLOCKED' % heading)

    def _step_sequence(self, sequence=None):
        """Run another sequence's steps here, in place of retyping them."""
        choices = self._nestable(sequence)
        if not choices:
            # Every other sequence already reaches this one, so any of them
            # here would close a ring.
            utils.force_notify('No sequence can go here without looping')
            return None
        choice = _select('Which sequence',
                         ['%s  -  %s' % (other['name'],
                                         sequence_lib.describe(other))
                          for other in choices])
        if choice == BACK:
            return None
        return {'kind': sequence_lib.KIND_SEQUENCE,
                'target': choices[choice]['name']}

    def _step_device(self, driver_id):
        """Which device of this driver, then what to do to it."""
        from devices import CAP_COMMANDS, CAP_PLAYBACK, CAP_POWER
        from speaker_driver import STOP

        label = self._driver_label(driver_id)
        devices = self._devices_for(driver_id, self.app.enabled_devices)
        if not devices:
            utils.force_notify('No %s devices' % label)
            return None

        rows = [('All %s' % label, None)]
        rows.extend((device.name, device) for device in devices)
        choice = _select('Which %s device' % label,
                         [row_label for row_label, _d in rows])
        if choice == BACK:
            return None

        device = rows[choice][1]
        target = device.device_id if device is not None \
            else sequence_lib.TARGET_ALL
        sample = device if device is not None else devices[0]
        capabilities = self.app.controller.capabilities(sample)

        # (label, kind, value). The kind travels with the row rather than
        # being worked back out from the value afterwards -- a learned code
        # named "On" used to be saved as a power step, because that is what
        # reading the kind off the value gets you.
        actions = []
        if CAP_POWER in capabilities:
            actions.extend([
                ('On', sequence_lib.KIND_POWER, sequence_lib.ACTION_ON),
                ('Off', sequence_lib.KIND_POWER, sequence_lib.ACTION_OFF),
                ('Toggle', sequence_lib.KIND_POWER,
                 sequence_lib.ACTION_TOGGLE)])
        if device is not None and CAP_COMMANDS in capabilities:
            actions.extend((name, sequence_lib.KIND_COMMAND, name)
                           for name in self.app.controller.commands(device))
        if CAP_LOCK in capabilities:
            actions.append(('Lock', sequence_lib.KIND_LOCK, ''))
        if (CAP_UNLOCK in capabilities
                and getattr(self.app.controller, 'allow_unlock', False)):
            actions.append(('Unlock', sequence_lib.KIND_UNLOCK, ''))
        if device is not None and CAP_POSITION in capabilities:
            actions.extend(('Position %d%%' % step,
                            sequence_lib.KIND_POSITION, str(step))
                           for step in (0, 25, 50, 75, 100))
            actions.append(('Position...', sequence_lib.KIND_POSITION, None))

        if not actions:
            utils.force_notify('%s has nothing to switch or send' % label)
            return None

        pick = _select('What should it do',
                       [action_label for action_label, _k, _v in actions])
        if pick == BACK:
            return None

        _picked, kind, chosen = actions[pick]
        if kind == sequence_lib.KIND_POSITION and chosen is None:
            value = self._ask_number('Position (0-100)', '50')
            if value is None:
                return None
            chosen = str(max(0, min(100, value)))
        step = {'kind': kind, 'driver': driver_id, 'target': target,
                'action': chosen}
        # A beacon's clip, then how loud. Not for Stop, which has nothing to
        # be loud about.
        if (kind == sequence_lib.KIND_COMMAND
                and CAP_PLAYBACK in capabilities and chosen != STOP):
            volume = self._ask_step_volume()
            if volume is GAVE_UP:
                return None
            if volume is not None:
                step['volume'] = volume
            when = _select('When', ['Straight away - stops what is playing',
                                    'After what is playing'])
            if when == BACK:
                return None
            if when == 1:
                step['after'] = True
        return step

    def _ask_step_volume(self):
        """How loud a beacon plays this clip: a volume, None to leave the
        beacon as it is, or GAVE_UP to give up on the step."""
        rows = [('Leave the volume as it is', None)]
        rows.extend(('%d%%' % level, level) for level in (25, 50, 75, 100))
        rows.append(('Volume...', 'ask'))
        choice = _select('How loud', [label for label, _v in rows])
        if choice == BACK:
            return GAVE_UP
        volume = rows[choice][1]
        if volume == 'ask':
            value = self._ask_number('Volume (0-100)', '50')
            if value is None:
                return GAVE_UP
            volume = max(0, min(100, value))
        return volume

    def _step_pause(self):
        return 'pause'

    def _step_move(self):
        return 'move'

    def _ask_pause(self, sequence, index):
        current = str(sequence['steps'][index].get('pause') or 0)
        value = self._ask_number(
            'Seconds to wait after this step (0-%d)' % sequence_lib.MAX_PAUSE,
            current)
        if value is None:
            return
        sequence['steps'][index]['pause'] = max(0, min(sequence_lib.MAX_PAUSE,
                                                     value))
        self.app.save_sequence(sequence)

    # -- learned commands ---------------------------------------------------

    def cover_menu(self, device):
        """Open, close, or put a blind somewhere in between.

        The named rows come from the driver rather than from here, because
        what "open" means is the hardware's business: a tilt closes in two
        directions and a curtain in one. Asking the driver keeps this menu
        from having an opinion it cannot back up.

        The percentages are offered without a reading attached to them -- a
        tilt is shut at both ends of its range and open in the middle, so
        labelling 50 as "half open" would be wrong on the one device this was
        written for. They are numbers; the slats show you what they mean.
        """
        rows = [(name, lambda n=name: self._send_cover_command(device, n))
                for name in self.app.controller.commands(device)]
        rows.extend(
            ('Position %d%%' % step,
             lambda p=step: self._set_position(device, p))
            for step in (25, 50, 75))
        rows.append(('Set position...',
                     lambda: self._ask_position(device)))

        choice = _select(device.name, [label for label, _handler in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def _send_cover_command(self, device, name):
        from devices import ControlError

        try:
            self.app.controller.send_command(device, name)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        utils.notify('%s: %s' % (device.name, name))

    def _set_position(self, device, percent):
        from devices import ControlError

        try:
            self.app.controller.set_position(device, percent)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        utils.notify('%s to %d%%' % (device.name, percent))

    def _ask_position(self, device):
        value = self._ask_number('Position (0-100)', '50')
        if value is None:
            return
        self._set_position(device, max(0, min(100, value)))

    def command_menu(self, device):
        """The codes a blaster knows: fire one, learn another, delete one."""
        while True:
            device = self._current(device)
            names = self.app.controller.commands(device)
            rows = list(names)
            # A satellite copies the codes down from the master along with
            # everything else, so it can fire them and nothing more.
            owns = self.app.owns_data
            # Only a driver that can learn is offered a Learn row. A beacon
            # emits what it was given -- its clips are files on the Pi --
            # and a row that led to a learning mode it does not have would be
            # a row that says no when pressed.
            learns = hasattr(self.app.controller.driver_for(device),
                             'start_learning')
            # Named rather than counted. What follows the codes has grown
            # from one row to three, and every version of this that worked
            # out which by subtracting broke the first time it did.
            extras = []
            if owns and learns:
                extras.append(('Learn a new command...',
                               lambda: self.learn_command(device)))
                extras.append(('Learn an RF command...',
                               lambda: self.learn_rf_command(device)))
            extras.append(('Test connection',
                           lambda: self.test_device(device)))
            rows.extend(label for label, _handler in extras)

            choice = _select('%s - commands' % device.name, rows)
            if choice == BACK:
                return
            if choice >= len(names):
                extras[choice - len(names)][1]()
                continue

            name = names[choice]
            action = _select(name, ['Send it now', 'Delete'] if owns
                             else ['Send it now'])
            if action == 0:
                try:
                    self.app.controller.send_command(device, name)
                    utils.notify('Sent %s' % name)
                except ControlError as exc:
                    utils.force_notify(str(exc))
            elif action == 1 and _dialog().yesno(
                    utils.ADDON_NAME, 'Delete the command "%s"?' % name):
                self.app.forget_command(device, name)
                utils.notify('Deleted %s' % name)

    def test_device(self, device):
        """Authenticate with an emitter and report the result plainly."""
        try:
            ok, message = self.app.test_device(device)
        except ControlError as exc:
            ok, message = False, str(exc)
        _dialog().ok('%s - %s' % (utils.ADDON_NAME,
                                  'connected' if ok else 'not connected'),
                     message)

    def learn_command(self, device, sleep_func=None):
        """Put the blaster into learning mode and wait for a remote press.

        The device has to be polled: it does not push the captured code, and
        it answers an error while it is still waiting, which is why a failed
        check means "keep waiting" rather than "give up".
        """
        import time

        sleep = sleep_func or time.sleep

        try:
            self.app.start_learning(device)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return

        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME,
                        'Point your remote at %s and press the button.'
                        % device.name,
                        'Cancel to give up.')
        code = None
        try:
            for step in range(LEARN_ATTEMPTS):
                progress.update(int(step * 100.0 / LEARN_ATTEMPTS))
                if progress.iscanceled():
                    break
                code = self.app.collect_learned(device)
                if code:
                    break
                sleep(LEARN_POLL)
        finally:
            progress.close()

        if not code:
            _dialog().ok(utils.ADDON_NAME,
                         'No code was captured.\n\nHold the remote close to '
                         '%s and press the button firmly while the dialog is '
                         'open.' % device.name)
            return

        name = _dialog().input('Name for this command', '')
        if not name or not name.strip():
            return
        if self.app.save_command(device, name.strip(), code):
            utils.notify('Learned %s' % name.strip())
        else:
            utils.force_notify('That command could not be saved')

    def _identify(self, device, sleep_func=None):
        """Blink a light so the user can tell which physical unit it is.

        Long enough to walk into the next room, and cancellable so it can be
        stopped the moment the light is spotted rather than standing there
        waiting for it to finish. The bulb is put back to the state it was in,
        which matters when it started out switched off -- otherwise
        identifying a light silently turns it on and leaves it on.
        """
        import time

        sleep = sleep_func or time.sleep
        before = self.app.controller.get_state(device)

        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME, 'Flashing %s' % device.name,
                        'Cancel once you have spotted it.')
        try:
            try:
                for index in range(IDENTIFY_FLASHES):
                    progress.update(int(index * 100.0 / IDENTIFY_FLASHES))
                    if progress.iscanceled():
                        break
                    self.app.controller.turn(device, False)
                    sleep(IDENTIFY_GAP)
                    self.app.controller.turn(device, True)
                    sleep(IDENTIFY_GAP)
            finally:
                progress.close()
        except ControlError as exc:
            utils.force_notify(str(exc))
            return

        self._restore(device, before)
        utils.notify('Flashed %s' % device.name)

    # -- input helpers ------------------------------------------------------

    @staticmethod
    def _ask_number(heading, default):
        value = _dialog().input(heading, str(default),
                                type=xbmcgui.INPUT_NUMERIC)
        if value is None or value == '':
            return None
        try:
            return int(value)
        except ValueError:
            utils.force_notify('That is not a number')
            return None

    @staticmethod
    def _ask_hex():
        """Prompt for a hex colour, returning ((r, g, b), label) or None.

        The Govee app hands out 8-digit codes, so pasting one straight in has
        to work. The returned label carries how it was read, because dropping
        the alpha off the wrong end silently produces a different colour.
        """
        value = _dialog().input('Colour as hex - 6 or 8 digits, e.g. FF8800 '
                                'or FFFF8800', '')
        if not value:
            return None

        rgb, note = scene_lib.parse_hex_color(value)
        if rgb is None:
            utils.force_notify(note)
            return None

        label = '#%02X%02X%02X' % rgb
        if note:
            label = '%s (%s)' % (label, note)
        return rgb, label


def pick_scene_for_setting(app, setting_id):
    """Settings helper: choose a scene and write its name into `setting_id`."""
    scenes = app.scenes
    if not scenes:
        utils.force_notify('No scenes are defined yet')
        return
    options = ['(none)'] + ['%s  -  %s' % (s['name'], scene_lib.describe(s))
                            for s in scenes]
    choice = _select('Choose a scene', options)
    if choice == BACK:
        return
    name = '' if choice == 0 else scenes[choice - 1]['name']
    utils.set_setting(setting_id, name)
    utils.notify('Scene set to %s' % (name or 'none'))
