# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The add-on session: reads Kodi settings, builds a controller, and owns the
cached device list and the scene list.

Both entry points (the interactive script and the background service) create
one of these. Nothing here draws UI, so the same object serves a dialog-driven
menu and a silent playback callback.
"""

import os
import time

import addon_utils as utils
import palette as palette_lib
import reracks as rerack_lib
import satellite as satellite_lib
import sequences as sequence_lib
import scenes as scene_lib
from devices import (DEVICE_CACHE, Device, TRANSPORT_AUTO, TRANSPORT_CLOUD,
                     TRANSPORT_LAN, build_hub)

# Order must match the `values` list on the transport_mode setting.
_TRANSPORT_MODES = [TRANSPORT_AUTO, TRANSPORT_LAN, TRANSPORT_CLOUD]


class ParagonHome(object):
    """Everything the add-on needs, assembled from the user's settings."""

    CODE_FILE = 'broadlink_codes.json'
    KEY_FILE = 'tuya_keys.json'

    def __init__(self):
        # Before anything is read: the add-on id changed in v2.19,
        # and Kodi files saved data under the id.
        utils.adopt_legacy_profile()

        # Learned IR/RF codes are loaded before the hub is built: the
        # Broadlink driver holds the same dict, so a code learned through the
        # driver is saved by this session without a round trip.
        self._codes = utils.read_json(self.CODE_FILE, default={}) or {}
        settings = self.read_settings()
        settings['broadlink_codes'] = self._codes
        settings['save_broadlink_codes'] = self.save_codes
        self._tuya_keys = utils.read_json(self.KEY_FILE, default={}) or {}
        settings['tuya_keys'] = self._tuya_keys
        settings['save_tuya_keys'] = self.save_tuya_keys
        settings['known_ips'] = self.known_ips
        settings['kasa_username'] = utils.get_setting('kasa_username')
        settings['kasa_password'] = utils.get_setting('kasa_password')
        settings['switchbot_token'] = utils.get_setting('switchbot_token')
        settings['switchbot_secret'] = utils.get_setting('switchbot_secret')
        self.controller = build_hub(settings)
        self._devices = None
        self._scenes = None
        self._sequences = None
        # What each shared file looked like when this session last read it.
        # See reload_changed.
        self._stamps = {}
        self._sequence_state = None
        self._reracks = None
        self._week = None
        self._week_follows_tv = None
        self._phase_state = None
        self._palette = None
        # How many known lights failed to answer the last refresh, so the
        # control panel can say so rather than silently showing a short list.
        self.last_refresh_missing = 0

    # -- settings ----------------------------------------------------------

    @staticmethod
    def read_settings():
        mode_index = utils.get_int('transport_mode', 0)
        if mode_index < 0 or mode_index >= len(_TRANSPORT_MODES):
            mode_index = 0
        return {
            'mode': _TRANSPORT_MODES[mode_index],
            'api_key': utils.get_setting('api_key', ''),
            'bind_address': utils.get_setting('bind_address', ''),
            'verify_ssl': utils.get_bool('verify_ssl', True),
            'cloud_timeout': utils.get_int('cloud_timeout', 10) or 10,
            'command_retries': utils.get_int('command_retries', 2) or 2,
            'log_func': utils.debug,
        }

    def save_codes(self):
        if self._master_owns('learned commands'):
            return False
        return utils.write_json(self.CODE_FILE, self._codes or {})

    def known_ips(self):
        """Addresses of every device already known, whatever its brand.

        Used to work out which subnet the house is actually on. A Govee bulb's
        address says that as well as anything, and better than this machine's
        own -- which on a box with a VPN or a container bridge can be a
        confident wrong answer.
        """
        return [d.ip for d in self.devices if d.ip]

    def save_tuya_keys(self):
        if self._master_owns('Tuya keys'):
            return False
        return utils.write_json(self.KEY_FILE, self._tuya_keys or {})

    def set_local_key(self, device, key):
        """Store a Tuya local key. Returns False if it is not 16 characters."""
        from devices import ControlError

        # Before asking which device it is: on a satellite the answer is the
        # same for all of them. The key travels down with everything else, so
        # one typed here is overwritten by the master's copy -- or by the
        # master not having one.
        if not self.owns_data:
            raise ControlError('Enter Tuya keys on the master, not here')
        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'set_local_key'):
            raise ControlError('%s does not use a local key' % device.name)
        return driver.set_local_key(device, key)

    def power_memory(self, device):
        """What a plug does when mains power returns, if it can say."""
        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'power_memory'):
            return None, []
        return driver.power_memory(device)

    def set_power_memory(self, device, value):
        from devices import ControlError

        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'set_power_memory'):
            raise ControlError('%s has no power-cut setting' % device.name)
        return driver.set_power_memory(device, value)

    def needs_local_key(self, device):
        driver = self.controller.driver_for(device)
        getter = getattr(driver, 'needs_key', None)
        return bool(getter and getter(device))

    # -- learned commands ---------------------------------------------------
    #
    # Routed through the owning driver rather than assuming Broadlink, so a
    # second kind of emitter needs no change here.

    def _emitter(self, device):
        from devices import ControlError

        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'start_learning'):
            raise ControlError('%s cannot learn commands' % device.name)
        return driver

    def test_device(self, device):
        """Prove a device answers, for drivers that can say so.

        Not routed through _emitter: an IR blaster is not the only thing worth
        testing, and a plug whose key has just been typed in by remote control
        is the clearest case of all.
        """
        from devices import ControlError

        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'test_connection'):
            raise ControlError('%s cannot be tested' % device.name)
        return driver.test_connection(device)

    def start_learning(self, device):
        if not self.owns_data:
            from devices import ControlError

            # The codes travel down from the master with everything else, so
            # one learned here is overwritten at the next sync.
            raise ControlError('Learn commands on the master, not here')
        return self._emitter(device).start_learning(device)

    def collect_learned(self, device):
        return self._emitter(device).collect_learned(device)

    # Radio, which takes two passes where infrared takes one. The satellite
    # guard is on the sweep alone: it is the first thing a learn does, so
    # refusing there stops the whole business before the blaster is touched.

    def start_rf_sweep(self, device):
        if not self.owns_data:
            from devices import ControlError

            raise ControlError('Learn commands on the master, not here')
        return self._emitter(device).start_rf_sweep(device)

    def rf_frequency_found(self, device):
        return self._emitter(device).rf_frequency_found(device)

    def start_rf_capture(self, device):
        return self._emitter(device).start_rf_capture(device)

    def cancel_rf_sweep(self, device):
        return self._emitter(device).cancel_rf_sweep(device)

    def save_command(self, device, name, hex_code):
        if self._master_owns('learned commands'):
            return False
        return self._emitter(device).save_command(device, name, hex_code)

    def forget_command(self, device, name):
        if self._master_owns('learned commands'):
            return False
        return self._emitter(device).forget_command(device, name)

    @property
    def discovery_timeout(self):
        return max(1, utils.get_int('discovery_timeout', 3))

    # -- devices -----------------------------------------------------------

    @property
    def devices(self):
        """Cached device list, loaded from disk on first access."""
        if self._devices is None:
            raw = utils.read_json(DEVICE_CACHE, default=[]) or []
            self._devices = [Device.from_dict(entry) for entry in raw
                             if isinstance(entry, dict)]
        return self._devices

    @property
    def enabled_devices(self):
        return [d for d in self.devices if d.enabled]

    # -- who owns the shared data ------------------------------------------

    @property
    def owns_data(self):
        """Whether this box owns the devices, scenes, sequences and palette.

        False on a satellite. Everything in satellite.SHARED_FILES is copied
        down from the master at start-up and every few minutes, and that copy
        is a plain overwrite -- it has no idea anything was edited here. So a
        scene built on a satellite is not merely unofficial, it is deleted
        within the sync interval, silently.

        Refusing the write is the kinder answer. Being told no is a small
        annoyance; watching an evening's work disappear a quarter of an hour
        later, with nothing anywhere saying why, is not.
        """
        return not self.satellite_mode

    def _master_owns(self, what):
        """True when this box may not save `what`, saying so in the log."""
        if self.owns_data:
            return False
        utils.log('Satellite: refused to save %s -- the master owns it'
                  % what)
        return True

    def save_devices(self):
        """Write the device cache. The one shared file a satellite may write.

        devices.json travels down from the master like the rest, but this is
        also where discovery records what it found and at which address --
        a cache rather than a choice. Refusing it would leave a satellite
        unable to reach the very lights the master told it about. The choices
        inside it -- the name, the enabled flag -- are guarded on their own,
        just above.
        """
        return utils.write_json(DEVICE_CACHE,
                                [d.to_dict() for d in self.devices])

    def rename_device(self, device, name):
        """Rename a device. Returns True if the new name was kept."""
        name = (name or '').strip()
        if not name or self._master_owns('a device name'):
            return False
        device.name = name
        self.save_devices()
        return True

    def set_device_enabled(self, device, enabled):
        """Include or exclude a device. Returns True if the change stuck."""
        if self._master_owns('which devices are enabled'):
            return False
        device.enabled = bool(enabled)
        self.save_devices()
        return True

    def set_device_power_only(self, device, power_only):
        """Switch this device only, never colour or dim it. Returns True if
        the change stuck.

        For a light this add-on should be able to turn on and off but has no
        business styling -- one driven by something else, or one set by hand
        and meant to stay as it was set. It stops being a light as far as
        scenes are concerned, exactly as a plug already is, while remaining
        part of every on and off the house does.
        """
        if self._master_owns('how a device is controlled'):
            return False
        device.power_only = bool(power_only)
        self.save_devices()
        return True

    def forget_device(self, device):
        """Remove a light from the cache for good."""
        if self._master_owns('the device list'):
            return False
        if device in self.devices:
            self._devices.remove(device)
            self.save_devices()
            utils.log('Forgot %s' % device.name)
            return True
        return False

    def device_by_id(self, device_id):
        wanted = (device_id or '').upper()
        for device in self.devices:
            if device.device_id == wanted:
                return device
        return None

    def resolve_targets(self, target):
        """Turn a target name into a device list, or None meaning "all".

        The one rule for what a named target means, so a keymap, the web
        remote and anything added later all agree about it. Matching is
        case-insensitive and accepts either the friendly name or the device
        id; an empty target, or "all", means every enabled device rather than
        none. A name that matches nothing gives an empty list, which is a
        different answer from None and is meant to be: one says "everything",
        the other says "you asked for something that is not here".
        """
        if not target or target.strip().lower() == 'all':
            return None
        wanted = target.strip().lower()
        return [device for device in self.enabled_devices
                if device.name.lower() == wanted
                or device.device_id.lower() == wanted]

    def resolve_color(self, value):
        """Read a colour as (r, g, b), from a hex code or a saved name.

        Hex first, then the speed dial, because a colour added to the palette
        is meant to be usable by name anywhere a code is -- on a remote
        button, in a sequence, or from the web remote -- without its digits
        being copied around.
        """
        import scenes as scene_lib

        rgb, _note = scene_lib.parse_hex_color(value)
        if rgb is not None:
            return rgb
        entry = self.color_by_name(value)
        if entry:
            return tuple(entry['color'])
        return None

    def refresh_devices(self):
        """Re-discover and merge into the cache. Returns (devices, warnings).

        User choices that live only on our side -- the friendly name, the
        enabled flag, whether this one is switched but never styled -- are
        carried across so a refresh never silently undoes them.

        A satellite does not do this. Discovery invents device entries, and
        the master decides which devices this house has: a satellite that
        found one for itself would be listing something the master had not
        approved, until the next sync deleted it again. It copies the list
        instead, addresses and all.
        """
        if not self.owns_data:
            utils.log('Satellite: discovery is the master\'s job')
            return [], ['This box takes its devices from the master. '
                        'Search on the master, or copy from it now.']

        found, warnings = self.controller.discover(
            timeout=self.discovery_timeout)

        previous = {}
        for device in self.devices:
            previous[device.device_id] = device

        for device in found:
            old = previous.get(device.device_id)
            if old is None:
                continue
            device.enabled = old.enabled
            device.power_only = old.power_only
            # Only keep a hand-set name; a placeholder should be replaced by
            # whatever this discovery turned up.
            if old.name and not old.name.startswith(old.model + ' ('):
                device.name = old.name

        # Keep lights that did not answer this time rather than dropping
        # them. A WiFi bulb asleep, powered off at the switch, or missed by a
        # single UDP sweep would otherwise be erased along with its name and
        # its enabled flag -- work that can represent 25 trips around the
        # house. Anything genuinely gone can be removed with "Forget this
        # light" in Manage devices.
        found_ids = set(d.device_id for d in found)
        missing = [d for d in self.devices if d.device_id not in found_ids]

        # A plug that has just been split into its outlets leaves its old
        # single entry behind. Keeping it would show a switch that no longer
        # controls anything, so an entry superseded by the outlets of the same
        # hardware is the one case where a device that did not answer is still
        # dropped.
        superseded = set()
        for device in found:
            native = (getattr(device, 'native_id', '') or '').upper()
            if native and native != device.device_id:
                superseded.add(native)
        missing = [d for d in missing if d.device_id not in superseded]
        for device in missing:
            utils.log('%s did not answer this search; keeping its entry'
                      % device.name)

        self._devices = sorted(found + missing, key=lambda d: d.name.lower())
        self.save_devices()
        utils.log('Device refresh: %d found, %d kept unseen, %d total'
                  % (len(found), len(missing), len(self._devices)))
        self.last_refresh_missing = len(missing)
        return self._devices, warnings

    # -- colour palette ----------------------------------------------------

    @property
    def palette(self):
        """Named colours for the menus, seeded on first read."""
        if self._palette is None:
            raw = utils.read_json(palette_lib.PALETTE_FILE, default=None)
            if raw is None:
                self._palette = palette_lib.default_palette()
                # Not on a satellite: the master's palette is on its way, and
                # seeding through the guard would only log a refusal on every
                # start-up until it lands.
                if self.owns_data:
                    self.save_palette()
            else:
                self._palette = palette_lib.normalise_all(raw)
        return self._palette

    def save_palette(self):
        if self._master_owns('the colour palette'):
            return False
        return utils.write_json(palette_lib.PALETTE_FILE, self._palette or [])

    def color_by_name(self, name):
        return palette_lib.find(self.palette, name)

    def save_color(self, name, rgb, index=None):
        """Add or update a colour. Returns the stored entry, or None.

        A name that already exists is replaced wherever it sits, so the menu
        order is not shuffled by an edit.
        """
        entry = palette_lib.normalise({'name': name, 'color': list(rgb)})
        if entry is None or self._master_owns('the colour palette'):
            return None

        existing = palette_lib.find(self.palette, entry['name'])
        if existing is not None:
            self._palette[self._palette.index(existing)] = entry
        elif index is not None and 0 <= index < len(self._palette):
            self._palette[index] = entry
        else:
            self._palette.append(entry)
        self.save_palette()
        return entry

    def remove_color(self, entry):
        if self._master_owns('the colour palette'):
            return False
        if entry in self.palette:
            self._palette.remove(entry)
            self.save_palette()
            return True
        return False

    def move_color(self, index, offset):
        if self._master_owns('the colour palette'):
            return index
        new_index = palette_lib.move(self.palette, index, offset)
        if new_index != index:
            self.save_palette()
        return new_index

    def reset_palette(self):
        if self._master_owns('the colour palette'):
            return False
        self._palette = palette_lib.default_palette()
        return self.save_palette()

    # -- cycling -----------------------------------------------------------
    #
    # A cycling scene is stepped by the background service, but it is normally
    # started from the control panel -- a different Kodi process. The two share
    # a small state file rather than a window property: a file survives a Kodi
    # restart, so a cycle that was running is picked up again, and the service
    # can poll it for the price of a stat call.

    CYCLE_FILE = 'cycle.json'

    def read_cycle(self):
        """The running cycle, or None."""
        state = utils.read_json(self.CYCLE_FILE, default=None)
        if not isinstance(state, dict) or not state.get('scene'):
            return None
        return state

    def start_cycle(self, scene, assignment, now=None):
        """Record that `scene` is cycling, with the arrangement now showing."""
        interval = int(scene.get('cycle') or 0)
        if interval <= 0 or scene.get('mode') != scene_lib.MODE_MIX:
            return None

        now = time.time() if now is None else now
        state = {
            'scene': scene['name'],
            'interval': interval,
            'assignment': dict(assignment or {}),
            # Absolute rather than a delay, so a service restart does not
            # reset the clock and make the lights sit still for a full
            # interval again.
            'next_at': now + interval,
        }
        utils.write_json(self.CYCLE_FILE, state)
        utils.log('Cycling "%s" every %ds over %d light(s)'
                  % (scene['name'], interval, len(state['assignment'])))
        return state

    def stop_cycle(self):
        """Stop any running cycle. Returns the name that was running, or None."""
        state = self.read_cycle()
        if state is None:
            return None
        utils.write_json(self.CYCLE_FILE, {})
        utils.log('Stopped cycling "%s"' % state.get('scene'))
        return state.get('scene')

    def cycle_due(self, now=None):
        """The running cycle if it is time to step it, else None."""
        state = self.read_cycle()
        if state is None:
            return None
        now = time.time() if now is None else now
        try:
            next_at = float(state.get('next_at') or 0)
        except (TypeError, ValueError):
            return state
        # A next_at far in the future means the clock moved backwards, or the
        # file was written by a box with a different time. Step rather than
        # hang for hours.
        if now >= next_at or next_at - now > state.get('interval', 60) * 2:
            return state
        return None

    def cycle_step(self, now=None, shuffle_func=None):
        """Advance a cycling scene by one colour. Returns True if it stepped."""
        state = self.read_cycle()
        if state is None:
            return False

        scene = self.scene_by_name(state.get('scene'))
        if scene is None or scene.get('mode') != scene_lib.MODE_MIX \
                or not scene.get('colors'):
            utils.log('Cycling scene "%s" is gone or no longer a mix; stopping'
                      % state.get('scene'))
            self.stop_cycle()
            return False

        targets = scene_lib.scene_targets(scene, self.devices,
                                          self.controller)
        assignment = state.get('assignment') or {}

        # Lights that joined since the cycle started, or a cycle resumed from
        # disk with nothing recorded, need dealing in rather than being left
        # on whatever they happen to be showing.
        missing = [d.device_id for d in targets
                   if d.device_id not in assignment]
        if missing:
            assignment = dict(assignment)
            assignment.update(scene_lib.deal_assignment(
                len(scene['colors']), missing, shuffle_func))

        assignment = scene_lib.rotate_assignment(assignment,
                                                 len(scene['colors']))
        # Colour only: power and brightness are already where the previous
        # step left them, so re-sending them is two thirds of the traffic for
        # no visible effect.
        applied, errors = scene_lib.apply_scene(
            self.controller, scene, self.devices, log_func=utils.debug,
            assignment=assignment, colors_only=True)

        now = time.time() if now is None else now
        state['assignment'] = assignment
        state['next_at'] = now + int(state.get('interval') or 60)
        utils.write_json(self.CYCLE_FILE, state)

        utils.debug('Cycle step: %d light(s), %d error(s)'
                    % (applied, len(errors)))
        return True

    # -- scenes ------------------------------------------------------------

    @property
    def scenes(self):
        """Scene list, seeded with the defaults the first time it is read."""
        if self._scenes is None:
            raw = utils.read_json(scene_lib.SCENE_FILE, default=None)
            if raw is None:
                self._scenes = scene_lib.default_scenes()
                self.save_scenes()
            else:
                self._scenes = scene_lib.normalise_all(raw)
                if not self._scenes:
                    self._scenes = scene_lib.default_scenes()
        return self._scenes

    def save_scenes(self):
        if self._master_owns('scenes'):
            return False
        return utils.write_json(scene_lib.SCENE_FILE, self._scenes or [])

    def scene_by_name(self, name):
        return scene_lib.find(self.scenes, name)

    def capture_scene(self, name, timeout=3.0):
        """Snapshot what the lights are doing now into a named scene.

        Returns (scene, captured_count, skipped_names). Nothing is saved here;
        the caller decides whether to keep it.
        """
        devices = self.enabled_devices
        states = self.controller.get_states(devices, timeout=timeout)
        scene, captured, skipped = scene_lib.capture_scene(
            name, devices, states)

        # Logged unconditionally rather than behind verbose logging: capture
        # is a deliberate one-off action, and when the result looks wrong the
        # raw reading per bulb is the only thing that says why.
        utils.log('--- capture "%s": %d of %d light(s) ---'
                  % (name, captured, len(devices)))
        for device in devices:
            utils.log('  %s [%s] raw=%s -> %s'
                      % (device.name, device.ip,
                         states.get(device.device_id),
                         (scene.get('devices') or {}).get(device.device_id)))
        utils.log('--- end capture ---')
        return scene, captured, skipped

    @staticmethod
    def summarise_capture(scene):
        """Counts by mode and the brightness range, for the capture dialog.

        Wrong-looking captures have a shape: every bulb reading as white when
        they are coloured, or every brightness pinned at 100. Showing the
        spread makes that obvious on screen instead of only in the log.
        """
        entries = list((scene.get('devices') or {}).values())
        counts = {'off': 0, scene_lib.MODE_COLOR: 0, scene_lib.MODE_TEMP: 0,
                  scene_lib.MODE_NONE: 0}
        levels = []
        for entry in entries:
            if entry.get('power') == scene_lib.POWER_OFF:
                counts['off'] += 1
                continue
            counts[entry.get('mode', scene_lib.MODE_NONE)] += 1
            if entry.get('brightness') is not None:
                levels.append(entry['brightness'])

        bits = []
        if counts[scene_lib.MODE_COLOR]:
            bits.append('%d colour' % counts[scene_lib.MODE_COLOR])
        if counts[scene_lib.MODE_TEMP]:
            bits.append('%d white' % counts[scene_lib.MODE_TEMP])
        if counts[scene_lib.MODE_NONE]:
            bits.append('%d with no colour reading'
                        % counts[scene_lib.MODE_NONE])
        if counts['off']:
            bits.append('%d off' % counts['off'])
        if levels:
            low, high = min(levels), max(levels)
            if low == high:
                bits.append('brightness %d%%' % low)
            else:
                bits.append('brightness %d-%d%%' % (low, high))
        return ', '.join(bits)

    def save_scene(self, scene):
        """Add or replace a scene by name. Returns the normalised scene."""
        cleaned = scene_lib.normalise(scene)
        if cleaned is None:
            return None
        # Before the list is touched, not after: a scene added in memory and
        # refused on disk would show in the menu and be gone next start-up.
        if self._master_owns('scenes'):
            return None
        existing = scene_lib.find(self.scenes, cleaned['name'])
        if existing is not None:
            self._scenes[self._scenes.index(existing)] = cleaned
        else:
            self._scenes.append(cleaned)
        self.save_scenes()
        return cleaned

    def apply_scene(self, scene, announce=True):
        """Apply a scene dict and report the outcome. Returns True if any
        device accepted it.

        Applying anything stops a running cycle first. Otherwise dimming for a
        film would be overwritten by the party still stepping in the
        background, which is a confusing way to discover a cycle is running.
        """
        self.stop_cycle()

        assignment = None
        if scene.get('mode') == scene_lib.MODE_MIX and scene.get('colors'):
            targets = scene_lib.scene_targets(scene, self.devices,
                                               self.controller)
            assignment = scene_lib.deal_assignment(
                len(scene['colors']), [d.device_id for d in targets])

        applied, errors = scene_lib.apply_scene(
            self.controller, scene, self.devices, log_func=utils.log,
            assignment=assignment)

        if applied and assignment and int(scene.get('cycle') or 0) > 0:
            self.start_cycle(scene, assignment)

        if applied and announce:
            utils.notify('%s applied to %d light(s)'
                         % (scene.get('name', 'Scene'), applied))
        if errors and not applied:
            utils.force_notify(errors[0])
        elif errors:
            utils.log('Scene applied with %d error(s): %s'
                      % (len(errors), '; '.join(errors[:3])))
        return applied > 0

    def apply_scene_by_name(self, name, announce=True):
        scene = self.scene_by_name(name)
        if scene is None:
            utils.log('No scene named "%s"' % name)
            if announce:
                utils.force_notify('No scene named "%s"' % name)
            return False
        return self.apply_scene(scene, announce=announce)

    # -- sequences -----------------------------------------------------------

    @property
    def sequences(self):
        """The saved sequences. Empty on a fresh install rather than seeded.

        Unlike scenes, there is no sensible starter set: a sequence refers to
        this house's own devices by name, and an invented one would be ten
        slots pointing at nothing.
        """
        if self._sequences is None:
            raw = utils.read_json(sequence_lib.SEQUENCE_FILE, default=None)
            if raw is None:
                # These were called reracks until v2.14. Read the old file
                # once and write the new one, so the rename costs nobody the
                # sequences they had already built.
                raw = utils.read_json(sequence_lib.LEGACY_FILE, default=None)
                if raw:
                    utils.log('Carrying %d sequence(s) over from %s'
                              % (len(raw), sequence_lib.LEGACY_FILE))
                    self._sequences = sequence_lib.normalise_all(raw)
                    self.save_sequences()
                    return self._sequences
            self._sequences = sequence_lib.normalise_all(raw or [])
        return self._sequences

    def save_sequences(self):
        if self._master_owns('sequences'):
            return False
        return utils.write_json(sequence_lib.SEQUENCE_FILE, self.sequences)

    def sequence_by_name(self, name):
        return sequence_lib.find(self.sequences, name)

    def save_sequence(self, sequence):
        """Add or replace a sequence by name. Returns the cleaned sequence."""
        cleaned = sequence_lib.normalise(sequence)
        if cleaned is None:
            return None
        if self._master_owns('sequences'):
            return None
        existing = sequence_lib.find(self.sequences, cleaned['name'])
        if existing is not None:
            self._sequences[self._sequences.index(existing)] = cleaned
        else:
            self._sequences.append(cleaned)
        self.save_sequences()
        return cleaned

    def delete_sequence(self, sequence):
        if self._master_owns('sequences'):
            return False
        existing = sequence_lib.find(self.sequences, sequence.get('name'))
        if existing is None:
            return False
        self._sequences.remove(existing)
        self.save_sequences()
        if self.sequence_state.pop(existing['name'], None) is not None:
            # Otherwise a new sequence given the same name would inherit a
            # "already ran today" it never earned.
            self.save_sequence_state()
        return True

    def run_sequence(self, sequence, announce=True, sleep_func=None,
                   on_step=None):
        """Run a sequence and report the outcome. Returns True if any step ran."""
        done, errors = sequence_lib.run(self, sequence, log_func=utils.log,
                                      sleep_func=sleep_func, on_step=on_step)
        if announce:
            if done and not errors:
                utils.notify('%s: %d step(s) done'
                             % (sequence.get('name'), done))
            elif done:
                utils.force_notify('%s: %d done, %d failed'
                                   % (sequence.get('name'), done, len(errors)))
            elif errors:
                utils.force_notify(errors[0])
            else:
                utils.force_notify('%s has no steps yet'
                                   % sequence.get('name'))
        return done > 0

    # -- scheduled sequences -------------------------------------------------

    @property
    def sequence_state(self):
        """When each sequence last ran, so a restart does not re-run it."""
        if self._sequence_state is None:
            raw = utils.read_json(sequence_lib.SEQUENCE_STATE_FILE,
                                  default=None)
            if raw is None:
                raw = utils.read_json(sequence_lib.LEGACY_STATE_FILE,
                                      default={})
            self._sequence_state = raw if isinstance(raw, dict) else {}
        return self._sequence_state

    def save_sequence_state(self):
        utils.write_json(sequence_lib.SEQUENCE_STATE_FILE, self.sequence_state)

    def resolved_schedule(self, sequence, now):
        """The (time, days) a sequence really runs on.

        A sequence following Paragon TV has no time or days of its own: Paragon
        TV's weekly table decides whether today has a preset at all, and that
        preset decides when the phase falls. Resolving it here rather than
        inside the sequence engine keeps that engine free of any knowledge of
        another add-on.
        """
        if not sequence_lib.follows_tv(sequence):
            return sequence_lib.own_schedule(sequence)

        import paragon_tv

        if not paragon_tv.enabled():
            return '', []
        preset = paragon_tv.todays_preset(now)
        if not preset:
            return '', []
        at_time = paragon_tv.phase_time(preset, sequence['phase'])
        if not at_time:
            return '', []
        return at_time, [now.weekday()]

    def due_sequences(self, now=None, grace=0):
        """The sequences whose time has come and that have not run yet today.

        `grace` widens the catch-up window by however long the caller was too
        busy to look -- see sequences.due.
        """
        moment = now or sequence_lib.now()
        state = self.sequence_state
        due = []
        for sequence in self.sequences:
            schedule = self.resolved_schedule(sequence, moment)
            if sequence_lib.due(sequence, moment, state.get(sequence['name'], ''),
                              schedule=schedule, grace=grace):
                due.append((sequence, schedule[0]))
        return due

    def run_due_sequences(self, now=None, sleep_func=None, on_step=None,
                          grace=0):
        """Run whatever is due. Returns the names of the sequences that ran.

        Each is marked as run *before* it runs, not after. A sequence can hold
        pauses adding up to minutes, and if something went wrong half way
        through, marking it afterwards would leave it due on the next tick and
        every tick after that -- a failing sequence retrying in a loop is worse
        than one that failed once.
        """
        if self.satellite_mode:
            # The master decides. Three boxes running the same schedule would
            # send every step three times.
            return []

        moment = now or sequence_lib.now()
        ran = []
        for sequence, at_time in self.due_sequences(moment, grace=grace):
            self.sequence_state[sequence['name']] = sequence_lib.stamp(
                sequence, moment, at_time)
            self.save_sequence_state()
            utils.log('Sequence "%s" is due (%s)'
                      % (sequence['name'], sequence_lib.describe_schedule(sequence)))
            self.run_sequence(sequence, announce=True, sleep_func=sleep_func,
                            on_step=on_step)
            ran.append(sequence['name'])
        return ran

    # -- reracks: a day in nine phases --------------------------------------

    def _load_reracks(self):
        """Fill in whichever of the three is still unread.

        Only the unread ones. They share a file and any of them can be the
        first to be asked for, so a plain assignment here let reading one
        discard another that had already been set.
        """
        raw = utils.read_json(rerack_lib.RERACK_FILE, default={}) or {}
        if not isinstance(raw, dict):
            raw = {}
        if self._reracks is None:
            self._reracks = rerack_lib.normalise_all(raw.get('presets'))
        if self._week is None:
            self._week = rerack_lib.clean_week(raw.get('week'))
        if self._week_follows_tv is None:
            self._week_follows_tv = bool(raw.get('week_follows_tv'))

    @property
    def reracks(self):
        """The nine presets. They always exist; an empty one costs nothing."""
        if self._reracks is None:
            self._load_reracks()
        return self._reracks

    @property
    def week(self):
        """Which rerack each day gets, as seven names or blanks."""
        if self._week is None:
            self._load_reracks()
        return self._week

    @property
    def week_follows_tv(self):
        """Whether the weekly table is Paragon TV's rather than our own."""
        if self._week_follows_tv is None:
            self._load_reracks()
        return self._week_follows_tv

    def set_week_follows_tv(self, following):
        self._week_follows_tv = bool(following)
        self.save_reracks()

    def tv_week(self):
        """Paragon TV's weekly table, or seven blanks if it has nothing to say."""
        import paragon_tv

        if not paragon_tv.enabled():
            return ['' for _ in range(7)]
        return rerack_lib.matching_week(paragon_tv.week())

    def effective_week(self):
        """The table actually in force, whichever it is."""
        if self.week_follows_tv:
            return self.tv_week()
        return self.week

    def copy_week_from_tv(self):
        """Take Paragon TV's days once, as a starting point to edit.

        Kept separate from following it: one is a copy that can then be
        changed, the other is a table that is never ours to change.
        """
        taken = self.tv_week()
        self._week = list(taken)
        self.save_reracks()
        return len([name for name in taken if name])

    def save_reracks(self):
        utils.write_json(rerack_lib.RERACK_FILE,
                         {'presets': self.reracks, 'week': self.week,
                          'week_follows_tv': bool(self._week_follows_tv)})

    def set_day(self, weekday, name):
        if self.week_follows_tv:
            return
        if 0 <= weekday <= 6:
            self.week[weekday] = name or ''
            self.save_reracks()

    def rerack_by_name(self, name):
        return rerack_lib.find(self.reracks, name)

    def todays_rerack(self, now=None):
        return rerack_lib.todays_rerack(self.effective_week(), self.reracks,
                                        now or sequence_lib.now())

    def sequence_used_by(self, name):
        return rerack_lib.used_by(self.reracks, name)

    @property
    def phase_state(self):
        """Which phases have already run, so a restart does not repeat one."""
        if self._phase_state is None:
            raw = utils.read_json(rerack_lib.RERACK_STATE_FILE, default=[])
            self._phase_state = set(raw if isinstance(raw, list) else [])
        return self._phase_state

    def save_phase_state(self):
        # Kept to a few days' worth: a phase key names its own date, so an old
        # one can never match again and would only grow the file forever.
        keys = sorted(self.phase_state)[-200:]
        self._phase_state = set(keys)
        utils.write_json(rerack_lib.RERACK_STATE_FILE, keys)

    def tv_phase_times(self, rerack, now):
        """Paragon TV's times for the preset of the same name, if any phase
        is waiting on them. Most reracks are not, most of the time."""
        if not rerack_lib.needs_tv(rerack):
            return {}

        import paragon_tv

        if not paragon_tv.enabled():
            return {}
        times = {}
        for number in range(1, rerack_lib.PHASE_COUNT + 1):
            at_time = paragon_tv.phase_time(rerack['name'], number)
            if at_time:
                times[number] = at_time
        return times

    def run_due_phases(self, now=None, sleep_func=None, on_step=None,
                       grace=0):
        """Run whatever today's rerack says is due. Returns what ran."""
        if self.satellite_mode:
            return []

        moment = now or sequence_lib.now()
        rerack = self.todays_rerack(moment)
        if rerack is None:
            return []

        due = rerack_lib.due_phases(rerack, moment, self.phase_state,
                                    self.tv_phase_times(rerack, moment),
                                    grace=grace)
        ran = []
        for number, name, at_time, key in due:
            # Marked before running, for the same reason a sequence is: a
            # phase that fails half way must not be due again on every tick.
            self.phase_state.add(key)
            self.save_phase_state()

            sequence = self.sequence_by_name(name)
            if sequence is None:
                utils.log('%s phase %d wanted the sequence "%s", which is '
                          'not there' % (rerack['name'], number, name))
                continue
            utils.log('%s phase %d (%s) is due: %s'
                      % (rerack['name'], number, at_time, name))
            self.run_sequence(sequence, announce=True, sleep_func=sleep_func,
                              on_step=on_step)
            ran.append('%s phase %d' % (rerack['name'], number))
        return ran

    def run_sequence_by_name(self, name, announce=True):
        sequence = self.sequence_by_name(name)
        if sequence is None:
            utils.log('No sequence named "%s"' % name)
            if announce:
                utils.force_notify('No sequence named "%s"' % name)
            return False
        return self.run_sequence(sequence, announce=announce)

    # -- satellite mode ----------------------------------------------------
    #
    # One box owns the setup and the others follow it. See satellite.py for
    # why a satellite runs no schedule of its own.

    SATELLITE_STATE = 'satellite_state.json'

    @property
    def satellite_mode(self):
        return utils.get_bool('satellite_mode', False)

    @property
    def master_ip(self):
        return (utils.get_setting('master_ip') or '').strip()

    @property
    def sync_minutes(self):
        return max(5, utils.get_int('satellite_sync_minutes', 15))

    @property
    def last_sync(self):
        """When this box last copied from the master, as a display string."""
        state = utils.read_json(self.SATELLITE_STATE, default={}) or {}
        return state.get('at', '')

    def _stamp_sync(self, at=None):
        moment = at or sequence_lib.now()
        utils.write_json(self.SATELLITE_STATE,
                         {'at': moment.strftime('%Y-%m-%d %H:%M')})

    def sync_from_master(self, run=None, at=None):
        """Copy the shared files down. Returns (copied, [problems]).

        The caches are dropped afterwards rather than reloaded: the next
        reader picks the new files up, and a satellite that syncs while a
        menu is open would otherwise be looking at the old list.
        """
        if not self.satellite_mode:
            return [], ['This box is not a satellite']

        copied, problems = satellite_lib.pull(self.master_ip, run=run)
        if copied:
            self._devices = None
            self._scenes = None
            self._sequences = None
            self._palette = None
            # Emptied and refilled rather than replaced. The drivers were
            # handed these very dicts when the hub was built, so rebinding
            # the attribute leaves each driver holding the copy from before
            # the sync -- a Tuya key that arrived in this pull would sit in
            # the session unused until Kodi was restarted.
            self._codes.clear()
            self._codes.update(
                utils.read_json(self.CODE_FILE, default={}) or {})
            self._tuya_keys.clear()
            self._tuya_keys.update(
                utils.read_json(self.KEY_FILE, default={}) or {})
            self._stamp_sync(at)
        return copied, problems

    # -- noticing what another process changed -----------------------------

    # The files each cached list is read from. Both halves of this add-on
    # keep their own session -- the service that runs the schedule and the
    # web remote, and the menus you open from the add-on itself -- because
    # Kodi gives a script its own interpreter. So the copy in this process is
    # only as fresh as the last time it read.
    SHARED_STATE = (
        ('_devices', DEVICE_CACHE),
        ('_scenes', scene_lib.SCENE_FILE),
        ('_sequences', sequence_lib.SEQUENCE_FILE),
        ('_palette', palette_lib.PALETTE_FILE),
    )

    def reload_changed(self):
        """Forget anything whose file has been rewritten since it was read.

        Cheap enough to call on every pass -- four stat calls, no reading --
        and the lists themselves are only rebuilt when one of them has
        actually moved.

        Without this, editing a sequence in the menus left the web remote
        showing the old one until Kodi was restarted: the menus wrote the
        file, and the service went on holding what it had read at startup.
        """
        changed = []
        for attribute, filename in self.SHARED_STATE:
            if not self._moved(filename):
                continue
            setattr(self, attribute, None)
            changed.append(filename)

        # The learned codes and the Tuya keys are handed to the drivers as
        # dicts and kept, so these are emptied and refilled rather than
        # replaced -- rebinding would leave each driver holding the old one.
        for filename, store in ((self.CODE_FILE, self._codes),
                                (self.KEY_FILE, self._tuya_keys)):
            if not self._moved(filename):
                continue
            store.clear()
            store.update(utils.read_json(filename, default={}) or {})
            changed.append(filename)

        if changed:
            utils.log('Re-reading %s: changed by something else'
                      % ', '.join(sorted(set(changed))))
        return changed

    def _moved(self, filename):
        """Whether `filename` has changed since this session last looked.

        False on the very first look, whatever the file is doing: a session
        that has only just started has not gone stale.

        A file that is not there yet is stamped as absent rather than skipped.
        Skipping it meant its first appearance read as a first look and was
        passed over -- so the first scene ever saved was not picked up until
        the one after it.
        """
        stamp = self._file_stamp(filename)
        seen = filename in self._stamps
        previous = self._stamps.get(filename)
        self._stamps[filename] = stamp
        return seen and previous != stamp

    @staticmethod
    def _file_stamp(filename):
        """(modified, size) for a saved file, or None when it is not there.

        Both, because a file can be rewritten inside the same second as it
        was last read -- which is exactly what happens when somebody edits
        two sequences one after the other.
        """
        try:
            full = utils.profile_file(filename)
        except Exception:
            return None
        try:
            info = os.stat(full)
        except (OSError, IOError):
            return None
        return (info.st_mtime, info.st_size)

    def describe_satellite(self):
        return satellite_lib.describe(self.master_ip, self.last_sync)

    # -- bulk actions ------------------------------------------------------

    def _each(self, action, targets=None):
        """Run `action(device)` over the targets, collecting failures.

        Every target here gets the same instruction, so a driver is allowed to
        fold several of them into one command -- a multi-outlet plug switches
        the whole box in a single packet rather than once per outlet.

        Any bulk action stops a running cycle, for the same reason applying a
        scene does: the cycle is a standing instruction about how the lights
        should look, and switching them off or dimming them by hand replaces
        it. Left running it would step again within the interval and undo what
        was just asked for -- or, with the lights off, keep talking to them all
        night and bring the cycle back the moment something switched them on.

        A cycle step does not come through here; it calls the scene engine
        directly, so this cannot stop the cycle it is driving.
        """
        from devices import ControlError

        self.stop_cycle()

        done = 0
        errors = []
        chosen = targets if targets is not None else self.enabled_devices
        for device in self._collapse(chosen):
            try:
                action(device)
                done += 1
            except ControlError as exc:
                utils.log('Command failed on %s: %s' % (device.name, exc))
                errors.append(str(exc))
        return done, errors

    def _collapse(self, devices):
        collapse = getattr(self.controller, 'collapse', None)
        if collapse is None:
            return list(devices)
        return collapse(devices)

    def power_all(self, on, targets=None):
        return self._each(lambda d: self.controller.turn(d, on), targets)

    def brightness_all(self, percent, targets=None):
        return self._each(
            lambda d: self.controller.set_brightness(d, percent), targets)

    def color_all(self, rgb, targets=None):
        return self._each(
            lambda d: self.controller.set_color(d, rgb[0], rgb[1], rgb[2]),
            targets)

    def color_temp_all(self, kelvin, targets=None):
        return self._each(
            lambda d: self.controller.set_color_temp(d, kelvin), targets)

    def send_command_all(self, name, targets):
        """Fire a learned command on each target that sends commands.

        Deliberately not built on `_each`. That one falls back to every
        enabled device when given no targets, and stops a running colour
        cycle -- both wrong here. A learned code belongs to the blaster that
        learned it, so there is no sensible "all"; and firing a television's
        power code is no reason to stop the lights cycling.
        """
        from devices import CAP_COMMANDS, ControlError

        done = 0
        errors = []
        for device in (targets or []):
            if CAP_COMMANDS not in self.controller.capabilities(device):
                continue
            try:
                self.controller.send_command(device, name)
                done += 1
            except ControlError as exc:
                utils.log('Command failed on %s: %s' % (device.name, exc))
                errors.append(str(exc))
        return done, errors

    def position_all(self, percent, targets):
        """Drive each target that is a cover to `percent`.

        Built like send_command_all rather than on `_each`: there is no
        sensible "every device in the house" for a position, and moving a
        blind is no reason to stop the lights cycling. Targets that are not
        covers are passed over rather than failed -- "close the blinds" sent
        at a room should not report an error for each lamp in it.
        """
        from devices import CAP_POSITION, ControlError

        done = 0
        errors = []
        for device in (targets or []):
            if CAP_POSITION not in self.controller.capabilities(device):
                continue
            try:
                self.controller.set_position(device, percent)
                done += 1
            except ControlError as exc:
                utils.log('Position failed on %s: %s' % (device.name, exc))
                errors.append(str(exc))
        return done, errors

    def toggle_all(self, targets=None):
        """Flip the lights as a group.

        The group is treated as one switch: if any target reports itself on,
        the whole group goes off. When no state can be read -- LAN status needs
        UDP 4002, which may be busy -- it falls back to turning on, because a
        toggle that does nothing visible reads as a broken button.
        """
        devices = self._collapse(
            targets if targets is not None else self.enabled_devices)
        any_on = None
        for device in devices:
            state = self.controller.get_state(device)
            if not state:
                continue
            power = state.get('power')
            if power in ('on', 'off'):
                any_on = bool(any_on) or power == 'on'

        target_state = False if any_on else True
        return self.power_all(target_state, devices)
