# -*- coding: utf-8 -*-
"""
Paragon Home - test suite.

Runs off-device: Kodi is replaced by the stubs in tests/kodistubs, the Govee
LAN device is replaced by a UDP socket on loopback, and the Govee cloud is
replaced by a local HTTP server. That covers the protocol encoding, the
transport-selection logic and the scene engine without needing hardware.

    python3 tests/test_paragon_home.py
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:  # pragma: no cover - Python 2 runner
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'kodistubs'))
sys.path.insert(0, os.path.join(ROOT, 'resources', 'lib'))
sys.path.insert(0, ROOT)

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402

import devices as devices_mod  # noqa: E402
import hub as hub_mod  # noqa: E402
import govee_cloud  # noqa: E402
import govee_lan  # noqa: E402
import scenes as scene_lib  # noqa: E402
from devices import (CAP_BRIGHTNESS, CAP_COLOR,  # noqa: E402
                     CAP_COLOR_TEMP, CAP_COMMANDS, CAP_POWER, CAP_STATE,
                     ControlError, Device, GoveeController)

PROFILE = xbmcaddon._PROFILE

# High ports so the suite never needs the privileged Govee ports or a real
# multicast-capable network.
TEST_SCAN_PORT = 44001
TEST_LISTEN_PORT = 44002
TEST_COMMAND_PORT = 44003


def _free_port():
    """A port nothing is listening on, for testing an unreachable device."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def gui_highlight():
    import gui
    return tuple(gui.HIGHLIGHT_COLOR)


def palette_row(panel, name):
    """Index of the palette row for `name` in a colour menu."""
    return [e['name'] for e in panel.app.palette].index(name)


def utils_read(name):
    """What is actually on disk for `name`, or None. For asserting absence."""
    import addon_utils

    return addon_utils.read_json(name, default=None)


def menu_row(open_menu, prefix):
    """Index of a menu row by its label prefix, and the labels it sat among.

    Looked up rather than hardcoded, for the same reason scene_row is: every
    positional index into a menu has broken at least once when a row was
    added above it.
    """
    xbmcgui.SELECT_QUEUE.extend([-1])
    open_menu()
    labels = xbmcgui.SELECT_CALLS[-1][1]
    xbmcgui.reset()
    matches = [i for i, label in enumerate(labels)
               if label.startswith(prefix)]
    assert matches, 'no row starting "%s" among %r' % (prefix, labels)
    return matches[0], labels


def scene_row(panel, prefix, index=None):
    """Index of a scene-editor row by its label prefix.

    Looked up rather than hardcoded: the editor has grown rows several times,
    and every positional index in these tests broke each time it did.
    """
    xbmcgui.SELECT_QUEUE.extend([-1])
    panel.edit_scene(index)
    labels = xbmcgui.SELECT_CALLS[-1][1]
    xbmcgui.reset()
    return [i for i, label in enumerate(labels)
            if label.startswith(prefix)][0]


def clean_profile():
    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGoveeDevice(object):
    """A UDP socket that answers scan and devStatus like a real Govee light."""

    def __init__(self, device_id, sku, port, host='127.0.0.1'):
        self.device_id = device_id
        self.sku = sku
        self.host = host
        self.received = []
        self.state = {'onOff': 1, 'brightness': 42,
                      'color': {'r': 10, 'g': 20, 'b': 30},
                      'colorTemInKelvin': 0}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                payload, address = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(payload.decode('utf-8'))['msg']
            except (ValueError, KeyError, UnicodeDecodeError):
                continue

            self.received.append(message)
            cmd = message.get('cmd')
            if cmd == 'scan':
                self._reply(address, {'msg': {'cmd': 'scan', 'data': {
                    'ip': self.host, 'device': self.device_id,
                    'sku': self.sku, 'wifiVersionSoft': '1.02.03'}}})
            elif cmd == 'devStatus':
                self._reply(address, {'msg': {'cmd': 'devStatus',
                                              'data': self.state}})

    def _reply(self, address, message):
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Bind to this device's own address so the reply carries it as the
            # source. A real light answers from its own IP, and the transport
            # relies on that to tell which device a status reply belongs to.
            out.bind((self.host, 0))
            out.sendto(json.dumps(message).encode('utf-8'), address)
        finally:
            out.close()

    def commands(self, name):
        return [m for m in self.received if m.get('cmd') == name]

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


class _CloudHandler(BaseHTTPRequestHandler):
    """Stands in for developer-api.govee.com."""

    responses = {}
    calls = []

    def log_message(self, fmt, *args):
        pass

    def _respond(self):
        key = (self.command, self.path.split('?')[0])
        status, body = self.responses.get(key, (404, {'code': 404,
                                                      'message': 'no route'}))
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        self.calls.append({
            'method': self.command,
            'path': self.path,
            'api_key': self.headers.get('Govee-API-Key'),
            'body': json.loads(raw.decode('utf-8')) if raw else None,
        })
        payload = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_PUT = _respond


class FakeCloud(object):
    def __init__(self):
        self.server = HTTPServer(('127.0.0.1', 0), _CloudHandler)
        self.port = self.server.server_address[1]
        _CloudHandler.responses = {}
        _CloudHandler.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    @property
    def base(self):
        return 'http://127.0.0.1:%d' % self.port

    def route(self, method, path, status, body):
        _CloudHandler.responses[(method, path)] = (status, body)

    @property
    def calls(self):
        return _CloudHandler.calls

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RecordingController(object):
    """Stands in for the Hub so scene ordering can be asserted.

    Presents the same surface the Hub does, which is what the menus and the
    scene engine talk to.
    """

    def __init__(self, fail_on=None, caps=None):
        self.calls = []
        self.fail_on = fail_on or set()
        self.caps = caps
        # What a state read comes back with, for the callers that do one.
        self.states = {}
        # The codes each blaster has been taught, by device id.
        self.command_map = {}

    def capabilities(self, device):
        """The real implementation unless a test asks for something else.

        Deliberately not a fixed set of everything: a stub that claims every
        capability for every device cannot catch a scene sending a plug a
        brightness, which is one of the things this stub exists to watch.
        """
        if self.caps is not None:
            return hub_mod.narrow(self.caps, device)
        if (getattr(device, 'driver', None) or '') == 'broadlink':
            # What the blaster driver really answers: commands and nothing
            # else, whether or not any have been learned yet.
            return set([CAP_COMMANDS])
        # Through the Hub's own narrowing, not around it. A double that says
        # a device can be coloured where the Hub says it cannot would hide
        # exactly the kind of bug it is here to catch.
        return hub_mod.narrow(GoveeController.capabilities(device), device)

    def commands(self, device):
        return list(self.command_map.get(device.device_id, []))

    class _StandIn(object):
        """What the Hub would hand back for this device's driver."""

        def __init__(self, has_transports):
            self.HAS_TRANSPORTS = has_transports

    def driver_for(self, device):
        # Answering None made every driver look alike, which cannot catch a
        # menu tagging a plug with a transport it has no choice about.
        return self._StandIn(
            (getattr(device, 'driver', None) or 'govee') == 'govee')

    def _record(self, name, device, *args):
        if device.device_id in self.fail_on:
            raise ControlError('%s is unreachable' % device.name)
        self.calls.append((name, device.device_id) + args)

    def turn(self, device, on):
        self._record('turn', device, on)

    def set_brightness(self, device, percent):
        self._record('brightness', device, percent)

    def set_color(self, device, r, g, b):
        self._record('color', device, r, g, b)

    def set_color_temp(self, device, kelvin):
        self._record('temp', device, kelvin)

    def send_command(self, device, name):
        # The Hub has this; leaving it off meant a sequence step that fires an
        # infrared code failed against the stub for a reason the real code
        # would never have had.
        self._record('command', device, name)

    def driver(self, driver_id):
        """The Hub answers with the driver object, or None for an unknown id."""
        return None

    def get_state(self, device):
        return self.states.get(device.device_id)

    def get_states(self, devices, timeout=3.0):
        return dict((device.device_id, self.states.get(device.device_id))
                    for device in devices)


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

class TestScenes(unittest.TestCase):

    def test_normalise_clamps_out_of_range_values(self):
        scene = scene_lib.normalise({
            'name': '  Bright  ', 'power': 'nonsense', 'brightness': 5000,
            'mode': 'weird', 'color': [999, -5, 'x'], 'kelvin': 99999,
            'targets': ['ab:cd'],
        })
        self.assertEqual(scene['name'], 'Bright')
        self.assertEqual(scene['power'], scene_lib.POWER_ON)
        self.assertEqual(scene['brightness'], 100)
        self.assertEqual(scene['mode'], scene_lib.MODE_NONE)
        self.assertEqual(scene['color'], [255, 255, 255])
        self.assertEqual(scene['kelvin'], 12000)
        self.assertEqual(scene['targets'], ['AB:CD'])

    def test_normalise_rejects_unusable_entries(self):
        self.assertIsNone(scene_lib.normalise(None))
        self.assertIsNone(scene_lib.normalise({'name': '   '}))
        self.assertIsNone(scene_lib.normalise({'power': 'on'}))
        self.assertIsNone(scene_lib.normalise('not a dict'))

    def test_normalise_keeps_none_brightness(self):
        scene = scene_lib.normalise({'name': 'Keep', 'brightness': None})
        self.assertIsNone(scene['brightness'])

    def test_normalise_all_drops_duplicates_and_junk(self):
        cleaned = scene_lib.normalise_all([
            {'name': 'One'}, {'name': 'one'}, 'junk', {'name': 'Two'},
        ])
        self.assertEqual([s['name'] for s in cleaned], ['One', 'Two'])

    def test_find_is_case_insensitive(self):
        found = scene_lib.find(scene_lib.default_scenes(), '  movie NIGHT ')
        self.assertEqual(found['name'], 'Movie Night')
        self.assertIsNone(scene_lib.find(scene_lib.default_scenes(), 'nope'))

    def test_default_scenes_all_survive_normalisation(self):
        defaults = scene_lib.default_scenes()
        self.assertEqual(len(scene_lib.normalise_all(defaults)), len(defaults))

    def test_lightbar_brightness_overrides_for_bars_only(self):
        controller = RecordingController()
        bulb = Device('AA:BB', name='Bulb', model='H6008', lan=True,
                      ip='127.0.0.1')
        bar = Device('CC:DD', name='Greatroom Lightbar One', model='H610A',
                     lan=True, ip='127.0.0.2')
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     bar_brightness=5)
        applied, errors = scene_lib.apply_scene(controller, scene,
                                                [bulb, bar])

        self.assertEqual(applied, 2)
        self.assertEqual(errors, [])
        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'AA:BB': 50, 'CC:DD': 5})

    def test_lightbar_brightness_absent_leaves_bars_on_scene_value(self):
        controller = RecordingController()
        bar = Device('CC:DD', name='Bar', model='H610A', lan=True,
                     ip='127.0.0.2')
        scene = scene_lib.make_scene('Dawn', brightness=50)
        scene_lib.apply_scene(controller, scene, [bar])

        levels = [c[2] for c in controller.calls if c[0] == 'brightness']
        self.assertEqual(levels, [50])

    def test_lightbar_recognised_by_name_as_well_as_model(self):
        by_model = Device('AA:BB', name='Anything', model='h610a')
        by_name = Device('CC:DD', name='Kitchen LIGHTBAR two', model='H6008')
        plain = Device('EE:FF', name='Bedroom Left Top', model='H6008')
        self.assertTrue(scene_lib.is_lightbar(by_model))
        self.assertTrue(scene_lib.is_lightbar(by_name))
        self.assertFalse(scene_lib.is_lightbar(plain))

    def test_lightbar_override_does_not_write_back_into_the_scene(self):
        controller = RecordingController()
        bar = Device('CC:DD', name='Bar', model='H610A', lan=True,
                     ip='127.0.0.2')
        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5)
        scene_lib.apply_scene(controller, scene, [bar])
        self.assertEqual(scene['brightness'], 50)
        self.assertEqual(scene['bar_brightness'], 5)

    def test_backlight_brightness_overrides_for_the_backlight_only(self):
        """The same idea as the lightbars, for a strip behind a screen."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Bulb', model='H6008', lan=True,
                      ip='127.0.0.1')
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     backlight_brightness=20)
        applied, errors = scene_lib.apply_scene(controller, scene,
                                                [bulb, back])

        self.assertEqual(applied, 2)
        self.assertEqual(errors, [])
        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'AA:BB': 50, 'EE:11': 20})

    def test_the_three_brightnesses_land_on_three_different_lights(self):
        """The point of the whole thing: one scene, three levels."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Bulb', model='H6008', lan=True,
                      ip='127.0.0.1')
        bar = Device('CC:DD', name='Greatroom Lightbar One', model='H610A',
                     lan=True, ip='127.0.0.2')
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5,
                                     backlight_brightness=20)
        scene_lib.apply_scene(controller, scene, [bulb, bar, back])

        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'AA:BB': 50, 'CC:DD': 5, 'EE:11': 20})

    def test_a_backlight_built_from_a_lightbar_takes_the_backlight_figure(self):
        """Both claims match, and the more specific one wins."""
        controller = RecordingController()
        both = Device('EE:11', name='Screen Backlight', model='H610A',
                      lan=True, ip='127.0.0.3')
        self.assertTrue(scene_lib.is_lightbar(both))
        self.assertTrue(scene_lib.is_backlight(both))

        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5,
                                     backlight_brightness=20)
        scene_lib.apply_scene(controller, scene, [both])

        levels = [c[2] for c in controller.calls if c[0] == 'brightness']
        self.assertEqual(levels, [20])

    def test_backlight_brightness_absent_leaves_it_on_the_scene_value(self):
        controller = RecordingController()
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        scene = scene_lib.make_scene('Dawn', brightness=50)
        scene_lib.apply_scene(controller, scene, [back])

        levels = [c[2] for c in controller.calls if c[0] == 'brightness']
        self.assertEqual(levels, [50])

    def test_a_backlight_is_recognised_by_name_and_nothing_else(self):
        """No SKU list: what makes a light a backlight is where it points."""
        named = Device('EE:11', name='Hundy Backlight', model='H6008')
        lower = Device('EE:22', name='hallway backlight', model='H6008')
        bar = Device('CC:DD', name='Kitchen Lightbar two', model='H610A')
        plain = Device('AA:BB', name='Bedroom Left Top', model='H6008')

        self.assertTrue(scene_lib.is_backlight(named))
        self.assertTrue(scene_lib.is_backlight(lower))
        # "Lightbar" does not contain "Backlight" -- the two never collide
        # on a name by accident.
        self.assertFalse(scene_lib.is_backlight(bar))
        self.assertFalse(scene_lib.is_backlight(plain))

    def test_backlight_override_does_not_write_back_into_the_scene(self):
        controller = RecordingController()
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     backlight_brightness=20)
        scene_lib.apply_scene(controller, scene, [back])

        self.assertEqual(scene['brightness'], 50)
        self.assertEqual(scene['backlight_brightness'], 20)

    def test_backlight_brightness_overrides_a_captured_scene_too(self):
        """A capture records what each light was doing; the override still
        applies, exactly as the lightbar one does."""
        controller = RecordingController()
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        scene = scene_lib.make_scene(
            'Captured', backlight_brightness=20,
            devices={'EE:11': {'power': 'on', 'brightness': 90,
                               'mode': 'none', 'color': [255, 255, 255],
                               'kelvin': 2700}})
        scene_lib.apply_scene(controller, scene, [back])

        levels = [c[2] for c in controller.calls if c[0] == 'brightness']
        self.assertEqual(levels, [20])
        self.assertEqual(scene['devices']['EE:11']['brightness'], 90)

    def test_strip_brightness_overrides_for_the_strip_only(self):
        """A strip along a counter is a line of light, not a room's worth."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Bulb', model='H6008', lan=True,
                      ip='127.0.0.1')
        strip = Device('FF:99', name='Kitchen Light Strip', model='H6008',
                       lan=True, ip='127.0.0.4')
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     strip_brightness=35)
        applied, errors = scene_lib.apply_scene(controller, scene,
                                                [bulb, strip])

        self.assertEqual(applied, 2)
        self.assertEqual(errors, [])
        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'AA:BB': 50, 'FF:99': 35})

    def test_the_four_brightnesses_land_on_four_different_lights(self):
        """One scene, four levels, each on the light it was meant for."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Bulb', model='H6008', lan=True,
                      ip='127.0.0.1')
        bar = Device('CC:DD', name='Greatroom Lightbar One', model='H610A',
                     lan=True, ip='127.0.0.2')
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        strip = Device('FF:99', name='Kitchen Light Strip', model='H6008',
                       lan=True, ip='127.0.0.4')
        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5,
                                     backlight_brightness=20,
                                     strip_brightness=35)
        scene_lib.apply_scene(controller, scene, [bulb, bar, back, strip])

        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'AA:BB': 50, 'CC:DD': 5,
                                  'EE:11': 20, 'FF:99': 35})

    def test_a_backlight_strip_takes_the_backlight_figure(self):
        """Two of the three words in one name, and the order has to hold.

        A backlight is usually a strip, so this is the collision that will
        actually happen in a room rather than a contrived one.
        """
        controller = RecordingController()
        both = Device('EE:11', name='TV Backlight Strip', model='H6008',
                      lan=True, ip='127.0.0.3')
        self.assertTrue(scene_lib.is_backlight(both))
        self.assertTrue(scene_lib.is_lightstrip(both))

        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     backlight_brightness=20,
                                     strip_brightness=35)
        scene_lib.apply_scene(controller, scene, [both])

        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'EE:11': 20})

    def test_a_lightbar_strip_takes_the_bar_figure(self):
        """The other collision. Strip is last of the three, deliberately."""
        controller = RecordingController()
        both = Device('CC:DD', name='Counter Strip', model='H610A',
                      lan=True, ip='127.0.0.2')
        self.assertTrue(scene_lib.is_lightbar(both))
        self.assertTrue(scene_lib.is_lightstrip(both))

        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5,
                                     strip_brightness=35)
        scene_lib.apply_scene(controller, scene, [both])

        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'CC:DD': 5})

    def test_adding_strips_changed_nothing_for_bars_and_backlights(self):
        """A scene written before strips existed behaves exactly as it did.

        The whole reason strip is asked last: no light that already took a
        bar or backlight figure may quietly start taking a different one.
        """
        controller = RecordingController()
        bar = Device('CC:DD', name='Greatroom Lightbar One', model='H610A',
                     lan=True, ip='127.0.0.2')
        back = Device('EE:11', name='Hundy Backlight', model='H6008',
                      lan=True, ip='127.0.0.3')
        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5,
                                     backlight_brightness=20)

        scene_lib.apply_scene(controller, scene, [bar, back])

        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'CC:DD': 5, 'EE:11': 20})

    def test_a_strip_is_recognised_by_name_and_nothing_else(self):
        """Shape, not SKU -- the same reasoning as a backlight."""
        named = Device('FF:99', name='Kitchen Light Strip', model='H6008')
        lower = Device('FF:98', name='under cabinet led strip', model='H6008')
        joined = Device('FF:97', name='Lightstrip', model='H6008')
        bulb = Device('AA:BB', name='Bulb', model='H6008')
        back = Device('EE:11', name='Hundy Backlight', model='H6008')

        self.assertTrue(scene_lib.is_lightstrip(named))
        self.assertTrue(scene_lib.is_lightstrip(lower))
        self.assertTrue(scene_lib.is_lightstrip(joined))
        self.assertFalse(scene_lib.is_lightstrip(bulb))
        # "Backlight" does not contain "strip"; the words never collide by
        # accident, only when a name really carries both.
        self.assertFalse(scene_lib.is_lightstrip(back))

    def test_strip_brightness_absent_leaves_it_on_the_scene_value(self):
        controller = RecordingController()
        strip = Device('FF:99', name='Kitchen Light Strip', model='H6008',
                       lan=True, ip='127.0.0.4')
        scene = scene_lib.make_scene('Dawn', brightness=50)

        scene_lib.apply_scene(controller, scene, [strip])

        levels = dict((c[1], c[2]) for c in controller.calls
                      if c[0] == 'brightness')
        self.assertEqual(levels, {'FF:99': 50})

    def test_strip_override_does_not_write_back_into_the_scene(self):
        controller = RecordingController()
        strip = Device('FF:99', name='Kitchen Light Strip', model='H6008',
                       lan=True, ip='127.0.0.4')
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     strip_brightness=35)

        scene_lib.apply_scene(controller, scene, [strip])

        self.assertEqual(scene['brightness'], 50)
        self.assertEqual(scene['strip_brightness'], 35)

    def test_normalise_clamps_strip_brightness(self):
        scene = scene_lib.normalise({'name': 'X', 'strip_brightness': 900})
        self.assertEqual(scene['strip_brightness'], 100)
        scene = scene_lib.normalise({'name': 'X', 'strip_brightness': 'junk'})
        self.assertIsNone(scene['strip_brightness'])
        scene = scene_lib.normalise({'name': 'X'})
        self.assertIsNone(scene['strip_brightness'])

    def test_a_scene_saved_before_strips_existed_still_loads(self):
        """The field is simply absent in scenes.json on every existing box."""
        scene = scene_lib.normalise({'name': 'Warshade', 'brightness': 40,
                                     'bar_brightness': 5,
                                     'backlight_brightness': 20})

        self.assertEqual(scene['brightness'], 40)
        self.assertEqual(scene['bar_brightness'], 5)
        self.assertEqual(scene['backlight_brightness'], 20)
        self.assertIsNone(scene['strip_brightness'])

    def test_the_scene_reads_out_its_strip_brightness(self):
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     strip_brightness=35)

        self.assertIn('strip 35%', scene_lib.describe(scene))

    def test_normalise_clamps_backlight_brightness(self):
        scene = scene_lib.normalise({'name': 'X', 'backlight_brightness': 900})
        self.assertEqual(scene['backlight_brightness'], 100)
        scene = scene_lib.normalise({'name': 'X',
                                     'backlight_brightness': 'junk'})
        self.assertIsNone(scene['backlight_brightness'])
        scene = scene_lib.normalise({'name': 'X'})
        self.assertIsNone(scene['backlight_brightness'])

    def test_a_scene_saved_before_backlights_existed_still_loads(self):
        """The field is simply absent in scenes.json on every existing box."""
        scene = scene_lib.normalise({'name': 'Warshade', 'brightness': 40,
                                     'bar_brightness': 5})

        self.assertEqual(scene['brightness'], 40)
        self.assertEqual(scene['bar_brightness'], 5)
        self.assertIsNone(scene['backlight_brightness'])

    def test_normalise_clamps_lightbar_brightness(self):
        scene = scene_lib.normalise({'name': 'X', 'bar_brightness': 900})
        self.assertEqual(scene['bar_brightness'], 100)
        scene = scene_lib.normalise({'name': 'X', 'bar_brightness': 'junk'})
        self.assertIsNone(scene['bar_brightness'])
        scene = scene_lib.normalise({'name': 'X'})
        self.assertIsNone(scene['bar_brightness'])

    def test_describe_reports_backlight_brightness(self):
        scene = scene_lib.make_scene('Dawn', brightness=50,
                                     backlight_brightness=20)
        self.assertIn('backlight 20%', scene_lib.describe(scene))
        plain = scene_lib.make_scene('Dawn', brightness=50)
        self.assertNotIn('backlight', scene_lib.describe(plain))

    def test_describe_reports_lightbar_brightness(self):
        scene = scene_lib.make_scene('Dawn', brightness=50, bar_brightness=5)
        self.assertIn('bars 5%', scene_lib.describe(scene))
        plain = scene_lib.make_scene('Dawn', brightness=50)
        self.assertNotIn('bars', scene_lib.describe(plain))

    def test_apply_scene_orders_brightness_before_colour(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True, ip='127.0.0.1')
        scene = scene_lib.make_scene('Test', brightness=30,
                                     mode=scene_lib.MODE_COLOR,
                                     color=[10, 20, 30])
        applied, errors = scene_lib.apply_scene(controller, scene, [device])

        self.assertEqual(applied, 1)
        self.assertEqual(errors, [])
        self.assertEqual([c[0] for c in controller.calls],
                         ['turn', 'brightness', 'color'])
        self.assertEqual(controller.calls[2], ('color', 'AA:BB', 10, 20, 30))

    def test_apply_scene_off_skips_everything_else(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True)
        scene = scene_lib.make_scene('Off', power=scene_lib.POWER_OFF,
                                     brightness=50)
        scene_lib.apply_scene(controller, scene, [device])
        self.assertEqual(controller.calls, [('turn', 'AA:BB', False)])

    def test_apply_scene_keep_power_does_not_switch(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True)
        scene = scene_lib.make_scene('Dim', power=scene_lib.POWER_KEEP,
                                     brightness=20)
        scene_lib.apply_scene(controller, scene, [device])
        self.assertEqual([c[0] for c in controller.calls], ['brightness'])

    def test_apply_scene_continues_past_a_failing_device(self):
        controller = RecordingController(fail_on={'BAD'})
        good = Device('GOOD', name='Good', lan=True)
        bad = Device('BAD', name='Bad', lan=True)
        scene = scene_lib.make_scene('Test', brightness=50)

        applied, errors = scene_lib.apply_scene(controller, scene, [bad, good])
        self.assertEqual(applied, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn('Bad is unreachable', errors[0])

    def test_apply_scene_respects_targets(self):
        controller = RecordingController()
        one = Device('ONE', name='One', lan=True)
        two = Device('TWO', name='Two', lan=True)
        scene = scene_lib.make_scene('Only one', targets=['TWO'])
        scene_lib.apply_scene(controller, scene, [one, two])
        self.assertEqual({c[1] for c in controller.calls}, {'TWO'})

    def test_apply_scene_skips_unsupported_commands(self):
        """A device that can express part of a scene gets that part only."""
        controller = RecordingController()
        device = Device('AA:BB', name='Warm bulb', lan=False, cloud=True,
                        supports=['turn', 'colorTem'])
        scene = scene_lib.make_scene('Test', brightness=30,
                                     mode=scene_lib.MODE_TEMP, kelvin=3000)

        scene_lib.apply_scene(controller, scene, [device])

        self.assertEqual([c[0] for c in controller.calls], ['turn', 'temp'])

    def test_an_untargeted_scene_leaves_out_what_it_cannot_describe(self):
        """The bug behind a sequence switching every plug in the house.

        A scene naming no targets meant "every enabled device", which was
        right when lights were all there was. Once plugs were listed too, a
        colour scene switched all of them as a side effect of the power
        setting that came with the colour.
        """
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', driver='govee', lan=True)
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True)
        controller.capabilities = lambda d: set(
            ['power', 'state'] if d.driver == 'kasa'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        scene = scene_lib.make_scene('Warshade', mode=scene_lib.MODE_COLOR,
                                     color=[80, 0, 120])

        scene_lib.apply_scene(controller, scene, [bulb, plug])

        self.assertEqual(set(c[1] for c in controller.calls), set(['AA:BB']))

    def test_a_scene_that_only_says_off_leaves_the_plugs_alone(self):
        """"All" means the lights. Switching plugs is a sequence's job."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', driver='govee', lan=True)
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True)
        controller.capabilities = lambda d: set(
            ['power', 'state'] if d.driver == 'kasa'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        scene = scene_lib.make_scene('All Off', power=scene_lib.POWER_OFF)

        scene_lib.apply_scene(controller, scene, [bulb, plug])

        self.assertEqual(set(c[1] for c in controller.calls), set(['AA:BB']))

    def test_only_a_light_is_a_scene_target(self):
        """A plug switches and a blaster emits; neither has a look."""
        controller = RecordingController()
        cases = [
            (Device('AA:BB', name='Lamp', driver='govee'),
             ['power', 'brightness', 'color', 'color_temp', 'state'], True),
            (Device('8006ABCD', name='Kitchen Plug', driver='kasa'),
             ['power', 'state'], False),
            (Device('WP9#1', name='Office Monitor Plug', driver='tuya'),
             ['power', 'state'], False),
            (Device('EE:FF', name='Bedroom Broadlink', driver='broadlink'),
             ['commands'], False),
        ]
        for device, caps, expected in cases:
            controller.capabilities = lambda d, c=caps: set(c)
            self.assertEqual(scene_lib.is_a_light(device, controller), expected,
                             '%s judged wrongly' % device.name)

    def test_a_device_is_a_light_when_nothing_can_say_otherwise(self):
        """No controller means no capability list, so do not silently drop it."""
        self.assertTrue(scene_lib.is_a_light(Device('AA:BB', name='Lamp')))

    def test_a_plug_named_in_a_scene_is_passed_over(self):
        """One rule throughout, so the picker and the engine cannot disagree.

        A scene saved before plugs were excluded may still name one. It is
        passed over rather than switched, which is what the target picker now
        shows -- the alternative is a scene doing something it cannot be
        edited to stop doing.
        """
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', driver='govee', lan=True)
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True)
        controller.capabilities = lambda d: set(
            ['power', 'state'] if d.driver == 'kasa'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        scene = scene_lib.make_scene('Warshade', mode=scene_lib.MODE_COLOR,
                                     color=[80, 0, 120],
                                     targets=['AA:BB', '8006ABCD'])

        scene_lib.apply_scene(controller, scene, [bulb, plug])

        self.assertEqual(set(c[1] for c in controller.calls), set(['AA:BB']))

    def test_apply_scene_ignores_disabled_devices(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True, enabled=False)
        applied, errors = scene_lib.apply_scene(
            controller, scene_lib.make_scene('Test'), [device])
        self.assertEqual(applied, 0)
        self.assertEqual(controller.calls, [])
        self.assertTrue(errors)


class TestPlugsInScenes(unittest.TestCase):
    """A scene is written once and applied to whatever is enabled.

    Once that includes plugs, "what commands does this bulb list" is the wrong
    question -- a plug lists nothing and would be sent everything.
    """

    def plug(self):
        return Device('wp9abc#1', name='Office Plug', driver='tuya',
                      native_id='wp9abc', driver_data={'dp': '1'})

    def test_a_plug_in_a_scene_is_only_switched(self):
        controller = RecordingController(caps=['power', 'state'])
        settings = scene_lib.make_scene(
            'Evening', power=scene_lib.POWER_ON, brightness=40,
            mode=scene_lib.MODE_COLOR, color=[255, 0, 0])

        scene_lib.apply_settings(controller, self.plug(), settings)

        self.assertEqual([call[0] for call in controller.calls], ['turn'])

    def test_a_plug_still_turns_off_with_the_rest(self):
        controller = RecordingController(caps=['power', 'state'])
        settings = scene_lib.make_scene(
            'All off', power=scene_lib.POWER_OFF)

        scene_lib.apply_settings(controller, self.plug(), settings)

        self.assertEqual(controller.calls, [('turn', 'WP9ABC#1', False)])

    def test_a_cycle_step_passes_a_plug_by(self):
        """A colour-only step has nothing to say to something with no colour."""
        controller = RecordingController(caps=['power', 'state'])
        settings = scene_lib.make_scene(
            'Mix', power=scene_lib.POWER_ON,
            mode=scene_lib.MODE_COLOR, color=[0, 255, 0])

        scene_lib.apply_settings(controller, self.plug(), settings,
                                 colors_only=True)

        self.assertEqual(controller.calls, [])

    def test_a_bulb_is_unaffected_by_the_capability_gate(self):
        """The Govee path must behave exactly as it did before."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', supports=['turn', 'brightness'])
        settings = scene_lib.make_scene(
            'Evening', power=scene_lib.POWER_ON, brightness=40,
            mode=scene_lib.MODE_COLOR, color=[255, 0, 0])

        scene_lib.apply_settings(controller, bulb, settings)

        self.assertEqual([call[0] for call in controller.calls],
                         ['turn', 'brightness'])


class TestSequences(unittest.TestCase):
    """Ten ordered steps, run as one."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'sequences', 'gui'):
            if name in sys.modules:
                del sys.modules[name]
        import sequences

        self.sequences = sequences

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.recorder = RecordingController()
        # Per driver, as the real Hub reports it. A blaster has commands and
        # no power; giving every device the same set would have let a scene
        # send a blaster a power command and called it correct.
        by_driver = {'govee': ['power', 'brightness', 'color', 'color_temp',
                               'state'],
                     'tuya': ['power', 'state'],
                     'broadlink': ['commands']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        self.recorder.commands = lambda device: ['TV power', 'Volume up']
        app.controller = self.recorder
        app._devices = [
            Device('AA:BB', name='Back Office Left Low', driver='govee',
                   lan=True),
            Device('WP9ABC#ALL', name='Office Plug All outlets',
                   driver='tuya', lan=True, native_id='wp9abc'),
            Device('EE:FF', name='Bedroom Broadlink', driver='broadlink',
                   lan=True),
        ]
        app._scenes = [scene_lib.make_scene('Warshade',
                                            power=scene_lib.POWER_ON,
                                            mode=scene_lib.MODE_COLOR,
                                            color=[80, 0, 120],
                                            targets=['AA:BB'])]
        return app

    def example(self):
        """The sequence from the request, written the way it was described."""
        return self.sequences.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'on'},
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'TV power'},
        ])

    # -- shape -------------------------------------------------------------

    def test_a_sequence_always_has_fifteen_slots(self):
        """Named after the preset system, and fixed like it: slot 4 is slot 4."""
        sequence = self.sequences.make_sequence('Wind Down')

        self.assertEqual(len(sequence['steps']), self.sequences.STEP_COUNT)
        self.assertEqual(self.sequences.STEP_COUNT, 15)
        self.assertTrue(all(s['kind'] == 'none' for s in sequence['steps']))

    def test_three_steps_leave_twelve_empty_slots(self):
        sequence = self.example()

        self.assertEqual(len(self.sequences.filled_steps(sequence)), 3)
        self.assertEqual(len(sequence['steps']), 15)

    def test_more_than_fifteen_steps_are_trimmed(self):
        sequence = self.sequences.make_sequence(
            'Too many',
            [{'kind': 'scene', 'target': 'Warshade'}] * 20)

        self.assertEqual(len(sequence['steps']), 15)

    def test_a_sequence_saved_with_ten_slots_gains_the_five_new_ones(self):
        """The file on disk predates the fifteenth slot. It is padded, not
        rejected, and the ten it had stay where they were."""
        old = {'name': 'Bedtime',
               'steps': [{'kind': 'scene', 'target': 'Warshade'}]
                        + [{'kind': 'none'}] * 8
                        + [{'kind': 'scene', 'target': 'Off'}]}

        sequence = self.sequences.normalise(old)

        self.assertEqual(len(sequence['steps']), 15)
        self.assertEqual(sequence['steps'][0]['target'], 'Warshade')
        self.assertEqual(sequence['steps'][9]['target'], 'Off')
        self.assertTrue(all(step['kind'] == 'none'
                            for step in sequence['steps'][10:]))

    # -- reordering --------------------------------------------------------

    def test_a_step_moved_down_pulls_the_ones_it_passes_up(self):
        steps = ['a', 'b', 'c', 'd']

        self.assertEqual(self.sequences.move_step(steps, 0, 2),
                         ['b', 'c', 'a', 'd'])

    def test_a_step_moved_up_pushes_the_ones_it_passes_down(self):
        steps = ['a', 'b', 'c', 'd']

        self.assertEqual(self.sequences.move_step(steps, 3, 1),
                         ['a', 'd', 'b', 'c'])

    def test_moving_a_step_leaves_the_original_list_alone(self):
        """A caller that changes its mind still has what it started with."""
        steps = ['a', 'b', 'c']

        self.sequences.move_step(steps, 0, 2)

        self.assertEqual(steps, ['a', 'b', 'c'])

    def test_a_move_never_changes_how_many_slots_there_are(self):
        sequence = self.example()

        sequence['steps'] = self.sequences.move_step(sequence['steps'], 0, 14)

        self.assertEqual(len(sequence['steps']), 15)
        self.assertEqual(len(self.sequences.filled_steps(sequence)), 3)

    def test_a_pause_travels_with_the_step_it_belongs_to(self):
        """The gap is "wait for the television", not "wait at slot 2"."""
        sequence = self.sequences.make_sequence('Wake', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Bright', 'pause': 12}])

        moved = self.sequences.move_step(sequence['steps'], 1, 0)

        self.assertEqual(moved[0]['target'], 'Bright')
        self.assertEqual(moved[0]['pause'], 12)
        self.assertEqual(moved[1]['pause'], 0)

    def test_a_nonsense_move_returns_the_steps_unchanged(self):
        steps = ['a', 'b', 'c']

        for frm, to in ((0, 0), (-1, 1), (1, 99), (99, 1), (1, -4)):
            self.assertEqual(self.sequences.move_step(steps, frm, to),
                             ['a', 'b', 'c'],
                             'move_step(%r, %r) changed something' % (frm, to))

    def test_reordering_changes_the_order_the_steps_run_in(self):
        """The whole point, checked at the far end: what actually runs."""
        sequence = self.sequences.make_sequence('Wake', [
            {'kind': 'scene', 'target': 'First'},
            {'kind': 'scene', 'target': 'Second'},
            {'kind': 'scene', 'target': 'Third'}])
        sequence['steps'] = self.sequences.move_step(sequence['steps'], 2, 0)

        self.assertEqual(
            [step['target']
             for step in self.sequences.filled_steps(sequence)],
            ['Third', 'First', 'Second'])

    def test_a_half_filled_step_becomes_empty_rather_than_broken(self):
        """A slot that looks filled but cannot run is worse than a blank one."""
        for raw in ({'kind': 'power', 'target': ''},
                    {'kind': 'power', 'target': 'AA:BB', 'action': 'sideways'},
                    {'kind': 'command', 'target': 'EE:FF', 'action': ''},
                    {'kind': 'scene', 'target': '   '}):
            self.assertEqual(self.sequences.normalise_step(raw)['kind'], 'none',
                             'accepted %r' % (raw,))

    def test_a_sequence_with_no_name_is_not_a_sequence(self):
        self.assertIsNone(self.sequences.normalise({'name': '  '}))

    # -- running -----------------------------------------------------------

    def test_the_example_runs_top_to_bottom_in_order(self):
        app = self.app()

        done, errors = self.sequences.run(app, self.example())

        self.assertEqual((done, errors), (3, []))
        self.assertEqual(
            [call[:2] for call in self.recorder.calls],
            [('turn', 'AA:BB'),          # 1. Govee, scene, Warshade
             ('color', 'AA:BB'),         #    (the scene's colour)
             ('turn', 'WP9ABC#ALL'),     # 2. Tuya, all outlets, on
             ('command', 'EE:FF')])      # 3. Broadlink, bedroom, TV power

    def test_a_scene_step_applies_the_scene_to_whatever_the_scene_says(self):
        """A sequence step runs a scene; it does not redefine one.

        The scene here names one bulb, so the plug beside it is untouched --
        the sequence's own step 2 is what switches that.
        """
        app = self.app()
        sequence = self.sequences.make_sequence(
            'Just the scene', [{'kind': 'scene', 'target': 'Warshade'}])

        self.sequences.run(app, sequence)

        self.assertEqual(set(call[1] for call in self.recorder.calls),
                         set(['AA:BB']))

    def test_a_scene_step_beside_a_plug_step_leaves_the_other_plugs_alone(self):
        """The reported bug: one Kasa step, and every Kasa plug switched.

        The scene was doing it. A scene naming no targets meant every enabled
        device, so "Scene: Warshade" switched all four plugs as a side effect
        of the power setting that came with the colour -- and the plug step
        beside it only accounted for one of them.
        """
        app = self.app()
        for suffix in ('1', '2', '3', '4'):
            app._devices.append(
                Device('8006ABC%s' % suffix, name='Kasa %s' % suffix,
                       driver='kasa', lan=True))
        by_driver = {'govee': ['power', 'brightness', 'color', 'color_temp',
                               'state'],
                     'tuya': ['power', 'state'],
                     'broadlink': ['commands'],
                     'kasa': ['power', 'state']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        # An ordinary colour scene, saved before any plug existed and so
        # naming no targets at all.
        app._scenes = [scene_lib.make_scene('Warshade',
                                            mode=scene_lib.MODE_COLOR,
                                            color=[80, 0, 120])]

        sequence = self.sequences.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'kasa', 'target': '8006ABC2',
             'action': 'on'},
        ])
        self.sequences.run(app, sequence)

        switched = set(call[1] for call in self.recorder.calls)
        self.assertEqual(switched, set(['AA:BB', '8006ABC2']))
        for suffix in ('1', '3', '4'):
            self.assertNotIn('8006ABC%s' % suffix, switched)

    def test_an_all_off_scene_step_leaves_the_plugs_to_a_power_step(self):
        """A scene says how the room looks; cutting plugs is a power step."""
        app = self.app()
        app._devices.append(Device('8006ABC1', name='Kasa 1', driver='kasa',
                                   lan=True))
        by_driver = {
            'govee': ['power', 'brightness', 'color', 'color_temp', 'state'],
            'tuya': ['power', 'state'],
            'broadlink': ['commands'],
            'kasa': ['power', 'state']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        app._scenes = [scene_lib.make_scene('All Off',
                                            power=scene_lib.POWER_OFF)]

        self.sequences.run(app, self.sequences.make_sequence(
            'Goodnight', [{'kind': 'scene', 'target': 'All Off'}]))

        touched = set(call[1] for call in self.recorder.calls)
        self.assertNotIn('8006ABC1', touched)
        self.assertTrue(touched, 'the lights should still have been switched')

    def test_a_failing_step_does_not_stop_the_ones_after_it(self):
        """A plug that has been unplugged is no reason to leave the room dark."""
        app = self.app()
        self.recorder.fail_on = set(['WP9ABC#ALL'])

        done, errors = self.sequences.run(app, self.example())

        self.assertEqual(done, 2)
        self.assertEqual(len(errors), 1)
        self.assertIn('Step 2', errors[0])
        self.assertIn(('command', 'EE:FF'),
                      [call[:2] for call in self.recorder.calls])

    def test_a_step_pointing_at_nothing_says_so_and_carries_on(self):
        app = self.app()
        sequence = self.sequences.make_sequence('Gone', [
            {'kind': 'power', 'driver': 'tuya', 'target': 'NOT:HERE',
             'action': 'on'},
            {'kind': 'scene', 'target': 'Warshade'},
        ])

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual(done, 1)
        self.assertIn('Nothing matches', errors[0])

    def test_a_missing_scene_is_reported_rather_than_silently_skipped(self):
        app = self.app()
        sequence = self.sequences.make_sequence(
            'Ghost', [{'kind': 'scene', 'target': 'No Such Scene'}])

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual(done, 0)
        self.assertIn('No scene called', errors[0])

    def test_a_pause_waits_after_its_step_not_before(self):
        """A television told to wake and change channel at once misses one."""
        app = self.app()
        slept = []
        sequence = self.sequences.make_sequence('Slow', [
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'TV power', 'pause': 4},
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'Volume up'},
        ])

        self.sequences.run(app, sequence, sleep_func=slept.append)

        self.assertEqual(slept, [4])
        self.assertEqual(len(self.recorder.calls), 2)

    def test_empty_slots_cost_nothing_and_are_skipped(self):
        app = self.app()
        sequence = self.sequences.make_sequence('Sparse')
        sequence['steps'][7] = {'kind': 'scene', 'target': 'Warshade',
                              'pause': 0}
        slept = []

        done, errors = self.sequences.run(app, sequence, sleep_func=slept.append)

        self.assertEqual((done, errors), (1, []))
        self.assertEqual(slept, [])

    def test_all_of_a_driver_is_a_valid_target(self):
        app = self.app()
        app._devices.append(Device('WP9ABC#1', name='Outlet 1', driver='tuya',
                                   lan=True, native_id='wp9abc'))
        sequence = self.sequences.make_sequence('Plugs off', [
            {'kind': 'power', 'driver': 'tuya',
             'target': self.sequences.TARGET_ALL, 'action': 'off'}])

        self.sequences.run(app, sequence)

        self.assertEqual([call[:2] for call in self.recorder.calls],
                         [('turn', 'WP9ABC#ALL'), ('turn', 'WP9ABC#1')])

    def test_a_renamed_device_is_still_found_by_id(self):
        """Sequences refer to ids, so renaming a device does not break one."""
        app = self.app()
        sequence = self.example()
        app._devices[1].name = 'Something Else Entirely'

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual((done, errors), (3, []))

    def test_a_step_written_by_hand_can_name_a_device(self):
        app = self.app()
        sequence = self.sequences.make_sequence('By name', [
            {'kind': 'power', 'target': 'Office Plug All outlets',
             'action': 'on'}])

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual((done, errors), (1, []))

    def test_a_run_can_be_stopped_part_way(self):
        app = self.app()
        seen = []

        def stop_after_first(index, step):
            seen.append(index)
            return len(seen) <= 1

        done, _errors = self.sequences.run(app, self.example(),
                                         on_step=stop_after_first)

        self.assertEqual(done, 1)

    # -- when it runs ------------------------------------------------------

    SATURDAY_6PM = datetime.datetime(2026, 8, 22, 18, 0)   # a Saturday

    def ignition(self):
        """The sequence from the request: Saturday nights at six."""
        return self.sequences.make_sequence('Ignition', [
            {'kind': 'scene', 'target': 'Warshade'}], time='6pm', days=[5])

    def test_a_time_is_read_however_it_is_typed(self):
        """Entered on a remote, where "6pm" is much less work than "18:00"."""
        for text, expected in (('18:00', '18:00'), ('6pm', '18:00'),
                               ('6 PM', '18:00'), ('6:30pm', '18:30'),
                               ('1800', '18:00'), ('0:05', '00:05'),
                               ('12am', '00:00'), ('12pm', '12:00')):
            self.assertEqual(self.sequences.parse_time(text), expected,
                             'could not read %r' % text)

    def test_something_that_is_not_a_time_is_refused(self):
        for text in ('24:00', '18:60', 'banana', '', None, '99'):
            self.assertEqual(self.sequences.parse_time(text), '',
                             'accepted %r as a time' % (text,))

    def test_a_schedule_needs_both_a_time_and_days(self):
        """Either alone would be a schedule that can never come round."""
        self.assertFalse(self.sequences.scheduled(
            self.sequences.make_sequence('A', time='6pm')))
        self.assertFalse(self.sequences.scheduled(
            self.sequences.make_sequence('B', days=[5])))
        self.assertTrue(self.sequences.scheduled(self.ignition()))

    def test_it_is_due_at_its_time_on_its_day(self):
        self.assertTrue(self.sequences.due(self.ignition(), self.SATURDAY_6PM))

    def test_it_is_not_due_before_its_time(self):
        self.assertFalse(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM - datetime.timedelta(minutes=1)))

    def test_it_is_not_due_on_another_day(self):
        for offset in (1, 2, 3, 4, 5, 6):
            self.assertFalse(
                self.sequences.due(self.ignition(),
                                 self.SATURDAY_6PM
                                 + datetime.timedelta(days=offset)),
                'fired %d day(s) late' % offset)

    def test_a_few_minutes_late_still_counts(self):
        """Kodi is not always awake at the exact minute."""
        self.assertTrue(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM + datetime.timedelta(minutes=4)))

    def test_an_hour_late_does_not(self):
        """A sequence that lifts the lights at six should not do it at seven."""
        self.assertFalse(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM + datetime.timedelta(hours=1)))

    def test_grace_covers_time_the_service_could_not_look(self):
        """An hour-long pause must not silently swallow what came due in it."""
        late = self.SATURDAY_6PM + datetime.timedelta(minutes=40)
        self.assertFalse(self.sequences.due(self.ignition(), late))
        self.assertTrue(self.sequences.due(self.ignition(), late,
                                           grace=3600))

    def test_grace_does_not_resurrect_something_older_than_the_allowance(self):
        """The allowance is the time actually spent busy, not a blank cheque."""
        late = self.SATURDAY_6PM + datetime.timedelta(hours=3)
        self.assertFalse(self.sequences.due(self.ignition(), late,
                                            grace=3600))

    def test_grace_of_zero_is_the_ordinary_catch_up_window(self):
        late = self.SATURDAY_6PM + datetime.timedelta(minutes=40)
        self.assertFalse(self.sequences.due(self.ignition(), late, grace=0))
        self.assertTrue(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM + datetime.timedelta(minutes=4),
            grace=0))

    def test_a_negative_grace_cannot_narrow_the_window(self):
        self.assertTrue(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM + datetime.timedelta(minutes=4),
            grace=-9999))

    def test_an_hour_long_pause_is_allowed(self):
        step = self.sequences.normalise_step(
            {'kind': self.sequences.KIND_SCENE, 'target': 'Dawn',
             'pause': 3600})
        self.assertEqual(step['pause'], 3600)

    def test_a_pause_beyond_the_maximum_is_clamped(self):
        step = self.sequences.normalise_step(
            {'kind': self.sequences.KIND_SCENE, 'target': 'Dawn',
             'pause': 99999})
        self.assertEqual(step['pause'], self.sequences.MAX_PAUSE)

    def test_it_does_not_run_twice_in_the_same_day(self):
        sequence = self.ignition()
        already = self.sequences.stamp(sequence, self.SATURDAY_6PM)

        self.assertFalse(self.sequences.due(sequence, self.SATURDAY_6PM, already))

    def test_moving_the_time_later_the_same_day_lets_it_run_again(self):
        """The stamp holds the time as well as the date, on purpose."""
        sequence = self.ignition()
        already = self.sequences.stamp(sequence, self.SATURDAY_6PM)
        sequence['time'] = '20:00'

        self.assertTrue(self.sequences.due(
            sequence, self.SATURDAY_6PM.replace(hour=20), already))

    def test_next_week_it_is_due_again(self):
        sequence = self.ignition()
        already = self.sequences.stamp(sequence, self.SATURDAY_6PM)

        self.assertTrue(self.sequences.due(
            sequence, self.SATURDAY_6PM + datetime.timedelta(days=7), already))

    def test_the_schedule_reads_the_way_it_would_be_said(self):
        self.assertEqual(self.sequences.describe_schedule(self.ignition()),
                         'Sat at 18:00')
        self.assertEqual(self.sequences.describe_schedule(
            self.sequences.make_sequence('A', time='07:00',
                                     days=[0, 1, 2, 3, 4])),
            'weekdays at 07:00')
        self.assertEqual(self.sequences.describe_schedule(
            self.sequences.make_sequence('B', time='09:00', days=[5, 6])),
            'weekends at 09:00')
        self.assertEqual(self.sequences.describe_schedule(
            self.sequences.make_sequence('C', time='23:00', days=list(range(7)))),
            'every day at 23:00')
        self.assertEqual(
            self.sequences.describe_schedule(self.sequences.make_sequence('D')),
            'only when you run it')

    # -- firing ------------------------------------------------------------

    def test_a_due_sequence_runs_and_is_not_run_again(self):
        app = self.app()
        app._sequences = [self.ignition()]

        first = app.run_due_sequences(now=self.SATURDAY_6PM)
        second = app.run_due_sequences(
            now=self.SATURDAY_6PM + datetime.timedelta(minutes=2))

        self.assertEqual(first, ['Ignition'])
        self.assertEqual(second, [])

    def test_a_restart_does_not_re_run_what_already_ran(self):
        """The record is on disk, so it survives Kodi closing."""
        app = self.app()
        app._sequences = [self.ignition()]
        app.run_due_sequences(now=self.SATURDAY_6PM)

        from paragon_home import ParagonHome
        again = ParagonHome()
        again.controller = self.recorder
        again._sequences = [self.ignition()]

        self.assertEqual(
            again.run_due_sequences(
                now=self.SATURDAY_6PM + datetime.timedelta(minutes=1)),
            [])

    def test_it_is_marked_as_run_before_it_runs(self):
        """A sequence that fails half way must not retry on every tick."""
        app = self.app()
        sequence = self.ignition()
        sequence['steps'][1] = {'kind': 'scene', 'target': 'No Such Scene',
                              'pause': 0}
        app._sequences = [sequence]

        app.run_due_sequences(now=self.SATURDAY_6PM)

        self.assertEqual(
            app.run_due_sequences(
                now=self.SATURDAY_6PM + datetime.timedelta(minutes=1)),
            [])

    def test_an_unscheduled_sequence_never_fires_itself(self):
        app = self.app()
        app._sequences = [self.sequences.make_sequence('Manual only', [
            {'kind': 'scene', 'target': 'Warshade'}])]

        self.assertEqual(app.run_due_sequences(now=self.SATURDAY_6PM), [])

    def test_two_sequences_due_at_once_both_run(self):
        app = self.app()
        other = self.ignition()
        other['name'] = 'Also Ignition'
        app._sequences = [self.ignition(), other]

        self.assertEqual(sorted(app.run_due_sequences(now=self.SATURDAY_6PM)),
                         ['Also Ignition', 'Ignition'])

    def test_deleting_a_sequence_forgets_when_it_last_ran(self):
        """Otherwise a new one of the same name inherits a day it never had."""
        app = self.app()
        app.save_sequence(self.ignition())
        app.run_due_sequences(now=self.SATURDAY_6PM)
        app.delete_sequence({'name': 'Ignition'})

        app.save_sequence(self.ignition())
        self.assertEqual(app.run_due_sequences(now=self.SATURDAY_6PM),
                         ['Ignition'])

    def test_a_schedule_survives_a_restart(self):
        app = self.app()
        app.save_sequence(self.ignition())

        from paragon_home import ParagonHome
        saved = ParagonHome().sequence_by_name('Ignition')

        self.assertEqual(saved['time'], '18:00')
        self.assertEqual(saved['days'], [5])

    # -- duplicating -------------------------------------------------------

    def _ignition(self, **extra):
        import sequences

        return sequences.make_sequence('Ignition', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'on', 'pause': 3},
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'TV power'},
        ], **extra)

    def _duplicate(self, panel, original, name):
        rows = menu_row(lambda: panel.edit_sequence(original), 'Duplicate')[1]
        xbmcgui.INPUT_QUEUE.append(name)
        xbmcgui.SELECT_QUEUE.extend([rows.index('Duplicate...'), -1])
        panel.edit_sequence(original)

    def panel_for(self, app):
        import gui

        return gui.ControlPanel(app)

    def test_all_fifteen_slots_come_across(self):
        """Fifteen steps is what makes retyping a variant worth avoiding."""
        app = self.app()
        app._sequences = [self._ignition()]
        panel = self.panel_for(app)

        self._duplicate(panel, app.sequences[0], 'Ignition Late')

        copied = app.sequence_by_name('Ignition Late')
        self.assertIsNotNone(copied)
        self.assertEqual(len(copied['steps']), 15)
        self.assertEqual(
            [self.sequences.describe_step(s) for s in copied['steps'][:3]],
            [self.sequences.describe_step(s)
             for s in app.sequence_by_name('Ignition')['steps'][:3]])

    def test_the_copy_does_not_inherit_the_schedule(self):
        """Two of them at the same minute is not what a variant means."""
        app = self.app()
        app._sequences = [self._ignition(time='6pm', days=[5])]
        panel = self.panel_for(app)

        self._duplicate(panel, app.sequences[0], 'Ignition Late')

        copied = app.sequence_by_name('Ignition Late')
        self.assertEqual(copied['time'], '')
        self.assertEqual(copied['days'], [])
        self.assertEqual(copied['phase'], 0)
        # And the original keeps its own.
        self.assertEqual(app.sequence_by_name('Ignition')['time'], '18:00')

    def test_the_copy_does_not_inherit_a_paragon_tv_phase_either(self):
        app = self.app()
        app._sequences = [self._ignition(phase=2)]
        panel = self.panel_for(app)

        self._duplicate(panel, app.sequences[0], 'Ignition Late')

        self.assertEqual(app.sequence_by_name('Ignition Late')['phase'], 0)

    def test_editing_the_copys_steps_does_not_touch_the_original(self):
        """The shallow-copy trap again: steps are a list of dicts."""
        app = self.app()
        app._sequences = [self._ignition()]
        panel = self.panel_for(app)

        self._duplicate(panel, app.sequences[0], 'Ignition Late')
        copied = app.sequence_by_name('Ignition Late')
        copied['steps'][1]['action'] = 'off'
        copied['steps'][1]['pause'] = 99

        original = app.sequence_by_name('Ignition')
        self.assertEqual(original['steps'][1]['action'], 'on')
        self.assertEqual(original['steps'][1]['pause'], 3)

    def test_a_name_already_in_use_is_refused(self):
        app = self.app()
        app._sequences = [self._ignition(),
                          self.sequences.make_sequence('Ignition Late')]
        panel = self.panel_for(app)

        self._duplicate(panel, app.sequences[0], 'ignition late')

        self.assertEqual(len(app.sequences), 2)
        self.assertIn('already a sequence', xbmcgui.OK_DIALOGS[-1][1])

    def test_backing_out_of_the_name_copies_nothing(self):
        app = self.app()
        app._sequences = [self._ignition()]
        panel = self.panel_for(app)

        self._duplicate(panel, app.sequences[0], '')

        self.assertEqual(len(app.sequences), 1)

    # -- persistence -------------------------------------------------------

    def test_sequences_saved_as_reracks_are_carried_over(self):
        """These were called reracks until v2.14, and were already in use."""
        import addon_utils as utils
        import sequences

        utils.write_json('reracks.json', [
            sequences.make_sequence('Ignition',
                                    [{'kind': 'scene', 'target': 'Warshade'}],
                                    time='6pm', days=[5])])

        app = self.app()
        carried = app.sequence_by_name('Ignition')

        self.assertIsNotNone(carried)
        self.assertEqual(carried['time'], '18:00')
        self.assertEqual(len(carried['steps']), 15)
        # Written out under the new name, so the old file is read only once.
        self.assertIsNotNone(utils.read_json('sequences.json', default=None))

    def test_when_it_last_ran_is_carried_over_too(self):
        """Otherwise the rename would re-run everything that ran today."""
        import addon_utils as utils
        import sequences

        utils.write_json('reracks.json', [
            sequences.make_sequence('Ignition',
                                    [{'kind': 'scene', 'target': 'Warshade'}],
                                    time='6pm', days=[5])])
        utils.write_json('rerack_state.json',
                         {'Ignition': '2026-08-22 18:00'})

        app = self.app()

        self.assertEqual(
            app.run_due_sequences(now=datetime.datetime(2026, 8, 22, 18, 0)),
            [])

    def test_a_new_install_does_not_look_for_the_old_file(self):
        app = self.app()

        self.assertEqual(app.sequences, [])

    def test_a_sequence_survives_a_restart(self):
        app = self.app()
        app.save_sequence(self.example())

        from paragon_home import ParagonHome
        again = ParagonHome()

        saved = again.sequence_by_name('Wind Down')
        self.assertIsNotNone(saved)
        self.assertEqual(len(saved['steps']), 15)
        self.assertEqual(saved['steps'][0]['target'], 'Warshade')

    def test_saving_the_same_name_replaces_rather_than_duplicates(self):
        app = self.app()
        app.save_sequence(self.example())
        app.save_sequence(self.sequences.make_sequence(
            'Wind Down', [{'kind': 'scene', 'target': 'Warshade'}]))

        self.assertEqual(len(app.sequences), 1)
        self.assertEqual(len(self.sequences.filled_steps(app.sequences[0])), 1)

    def test_a_sequence_is_found_however_its_name_is_typed(self):
        app = self.app()
        app.save_sequence(self.example())

        self.assertIsNotNone(app.sequence_by_name('  wind DOWN '))

    def test_deleting_one_leaves_the_others(self):
        app = self.app()
        app.save_sequence(self.example())
        app.save_sequence(self.sequences.make_sequence('Other'))

        self.assertTrue(app.delete_sequence({'name': 'Other'}))
        self.assertEqual([r['name'] for r in app.sequences], ['Wind Down'])

    def test_a_fresh_install_has_no_sequences_rather_than_invented_ones(self):
        """A starter sequence would be fifteen slots pointing at nobody's devices."""
        self.assertEqual(self.app().sequences, [])

    def test_run_by_name_reports_a_name_that_is_not_there(self):
        app = self.app()

        self.assertFalse(app.run_sequence_by_name('Nothing', announce=False))

    # -- how it reads ------------------------------------------------------

    def test_the_summary_keeps_the_names_as_they_were_typed(self):
        self.assertEqual(self.sequences.describe(self.example()),
                         '3 steps, first: Scene: Warshade')

    def test_a_step_reads_back_in_the_order_it_was_chosen(self):
        sequence = self.example()

        self.assertEqual(
            [self.sequences.describe_step(s) for s in sequence['steps'][:3]],
            ['Scene: Warshade', 'WP9ABC#ALL: On', 'EE:FF: TV power'])

    def test_a_pause_is_shown_on_the_step_it_follows(self):
        step = self.sequences.normalise_step(
            {'kind': 'scene', 'target': 'Warshade', 'pause': 30})

        self.assertEqual(self.sequences.describe_step(step),
                         'Scene: Warshade  (+30s)')

    def test_an_all_target_reads_as_all_of_them(self):
        step = self.sequences.normalise_step(
            {'kind': 'power', 'driver': 'tuya',
             'target': self.sequences.TARGET_ALL, 'action': 'off'})

        self.assertEqual(self.sequences.describe_step(step),
                         'all tuya devices: Off')


class TestReracks(unittest.TestCase):
    """A day laid out in nine phases, each holding a sequence."""

    SATURDAY = datetime.datetime(2026, 8, 22, 7, 0)     # a Saturday
    THURSDAY = datetime.datetime(2026, 8, 20, 7, 0)

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'paragon_tv', 'reracks',
                     'sequences', 'gui'):
            if name in sys.modules:
                del sys.modules[name]
        import reracks

        self.reracks = reracks

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome
        import sequences

        app = ParagonHome()
        self.recorder = RecordingController()
        # A Govee lamp, so a light: colour and brightness, not just a switch.
        self.recorder.capabilities = lambda d: set(
            ['power', 'brightness', 'color', 'color_temp', 'state'])
        app.controller = self.recorder
        app._devices = [Device('AA:BB', name='Lamp', driver='govee',
                               lan=True)]
        app._scenes = [scene_lib.make_scene('Warshade', targets=['AA:BB'])]
        app._sequences = [
            sequences.make_sequence('Curtain Up',
                                    [{'kind': 'scene', 'target': 'Warshade'}]),
            sequences.make_sequence('Wind Down',
                                    [{'kind': 'power', 'target': 'AA:BB',
                                      'action': 'off'}]),
        ]
        return app

    def alpha(self, follow_tv=False):
        return self.reracks.make_rerack('Alpha', [
            {},
            {'sequence': 'Curtain Up', 'time': '07:00'},
            {'sequence': 'Wind Down', 'time': '23:30'},
            {},
            {'sequence': 'Curtain Up', 'time': '17:00'},
        ], follow_tv=follow_tv)

    def with_alpha(self, follow_tv=False, days=('Alpha',) * 7):
        app = self.app()
        app._reracks = self.reracks.normalise_all([self.alpha(follow_tv)])
        app._week = list(days)
        app._phase_state = set()
        return app

    # -- shape -------------------------------------------------------------

    def test_every_preset_paragon_tv_has_exists_here_too(self):
        """As Paragon TV's do. An empty one costs nothing.

        Ten of them: Paragon TV grew Zeta, and a day set to it has to find a
        rerack of the same name or the two stop lining up.
        """
        import paragon_tv

        presets = self.reracks.default_reracks()

        self.assertEqual([p['name'] for p in presets],
                         list(self.reracks.PRESET_NAMES))
        self.assertEqual(list(self.reracks.PRESET_NAMES),
                         list(paragon_tv.PRESET_NAMES))
        self.assertIn('Zeta', self.reracks.PRESET_NAMES)

    def test_a_rerack_always_has_nine_phases(self):
        rerack = self.reracks.make_rerack('Alpha')

        self.assertEqual(len(rerack['phases']), 9)
        self.assertTrue(all(not p['sequence'] for p in rerack['phases']))

    def test_a_file_missing_presets_still_opens_on_all_nine(self):
        """A hand-edited or older file should not lose the familiar order."""
        presets = self.reracks.normalise_all([self.alpha()])

        self.assertEqual([p['name'] for p in presets],
                         list(self.reracks.PRESET_NAMES))
        self.assertEqual(len(self.reracks.filled_phases(presets[0])), 3)

    def test_an_unrecognised_preset_name_is_dropped(self):
        presets = self.reracks.normalise_all(
            [self.reracks.make_rerack('Rogue')])

        self.assertNotIn('Rogue', [p['name'] for p in presets])

    def test_a_phase_with_no_sequence_does_nothing_whatever_else_it_holds(self):
        phase = self.reracks.normalise_phase({'time': '07:00',
                                              'sequence': '  '})

        self.assertEqual(phase, {'time': '', 'sequence': ''})

    # -- the reuse this exists for -----------------------------------------

    def test_one_sequence_can_sit_in_several_phases_of_a_day(self):
        """The whole point: written once, used at several points in a day."""
        rerack = self.alpha()

        self.assertEqual(self.reracks.used_by([rerack], 'Curtain Up'),
                         ['Alpha phase 2', 'Alpha phase 5'])

    def test_each_of_those_phases_runs_at_its_own_time(self):
        app = self.with_alpha()

        morning = app.run_due_phases(now=self.SATURDAY)
        evening = app.run_due_phases(now=self.SATURDAY.replace(hour=17))

        self.assertEqual(morning, ['Alpha phase 2'])
        self.assertEqual(evening, ['Alpha phase 5'])

    # -- the week ----------------------------------------------------------

    def test_a_day_with_no_rerack_runs_nothing(self):
        app = self.with_alpha(days=('', '', '', '', '', '', ''))

        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])

    def test_only_the_day_that_has_it_runs_it(self):
        app = self.with_alpha(days=('Alpha', '', '', '', '', '', ''))

        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])
        # Monday
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY + datetime.timedelta(days=2)),
            ['Alpha phase 2'])

    def test_the_week_survives_a_restart(self):
        app = self.with_alpha(days=('', '', '', '', '', 'Alpha', ''))
        app.save_reracks()

        from paragon_home import ParagonHome
        again = ParagonHome()

        self.assertEqual(again.week[5], 'Alpha')
        self.assertEqual(len(again.reracks),
                         len(self.reracks.PRESET_NAMES))
        self.assertEqual(len(self.reracks.filled_phases(again.reracks[0])), 3)

    # -- firing ------------------------------------------------------------

    def test_a_phase_runs_once_a_day(self):
        app = self.with_alpha()

        app.run_due_phases(now=self.SATURDAY)

        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY
                               + datetime.timedelta(minutes=2)), [])

    def test_a_restart_does_not_repeat_a_phase(self):
        app = self.with_alpha()
        app.save_reracks()
        app.run_due_phases(now=self.SATURDAY)

        from paragon_home import ParagonHome
        again = ParagonHome()
        again.controller = self.recorder
        again._sequences = app._sequences

        self.assertEqual(again.run_due_phases(now=self.SATURDAY), [])

    def test_a_phase_naming_a_missing_sequence_says_so_and_carries_on(self):
        app = self.with_alpha()
        app._sequences = [s for s in app._sequences
                          if s['name'] != 'Curtain Up']

        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])
        # And it does not keep retrying it every tick.
        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])

    def test_the_sequence_actually_runs(self):
        app = self.with_alpha()

        app.run_due_phases(now=self.SATURDAY)

        self.assertEqual([call[:2] for call in self.recorder.calls],
                         [('turn', 'AA:BB')])

    # -- matching the week to Paragon TV ------------------------------------

    def test_matching_the_week_reads_paragon_tv_every_time(self):
        """A copy would drift; this cannot, because it is never ours."""
        self.install_tv(SaturdayPreset='1', MondayPreset='0')
        app = self.with_alpha(days=('', '', '', '', '', '', ''))
        app.set_week_follows_tv(True)

        self.assertEqual(app.effective_week()[5], 'Alpha')
        self.assertEqual(app.run_due_phases(now=self.SATURDAY),
                         ['Alpha phase 2'])

    def test_a_day_changed_in_paragon_tv_changes_here_with_nothing_pressed(self):
        self.install_tv(SaturdayPreset='0')
        app = self.with_alpha(days=('', '', '', '', '', '', ''))
        app.set_week_follows_tv(True)
        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])

        xbmcaddon.FOREIGN['script.paragontv']['SaturdayPreset'] = '1'

        self.assertEqual(app.run_due_phases(now=self.SATURDAY),
                         ['Alpha phase 2'])

    def test_our_own_days_are_kept_and_come_back(self):
        """Matching is a mode, not a one-way change to the table."""
        self.install_tv(SaturdayPreset='0')
        app = self.with_alpha(days=('', '', '', '', '', 'Alpha', ''))
        app.set_week_follows_tv(True)

        self.assertEqual(app.effective_week()[5], '')

        app.set_week_follows_tv(False)
        self.assertEqual(app.effective_week()[5], 'Alpha')

    def test_a_day_cannot_be_changed_while_the_week_is_matched(self):
        """It is not ours to change; changing it would silently do nothing."""
        self.install_tv()
        app = self.with_alpha(days=('', '', '', '', '', '', ''))
        app.set_week_follows_tv(True)

        app.set_day(5, 'Omega')

        self.assertEqual(app.week[5], '')

    def test_copying_the_week_takes_it_once_and_leaves_it_editable(self):
        """The other half: a starting point rather than a permanent link."""
        self.install_tv(SaturdayPreset='2', MondayPreset='1')
        app = self.with_alpha(days=('', '', '', '', '', '', ''))

        taken = app.copy_week_from_tv()

        self.assertEqual(taken, 2)
        self.assertEqual(app.week[0], 'Alpha')
        self.assertEqual(app.week[5], 'Omega')

        # And it stays put when Paragon TV moves on.
        xbmcaddon.FOREIGN['script.paragontv']['SaturdayPreset'] = '0'
        self.assertEqual(app.week[5], 'Omega')

    def test_matching_with_paragon_tv_switched_off_gives_a_blank_week(self):
        self.install_tv(EnablePresetSystem='false')
        app = self.with_alpha()
        app.set_week_follows_tv(True)

        self.assertEqual(app.effective_week(), ['' for _ in range(7)])
        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])

    def test_matching_without_paragon_tv_gives_a_blank_week(self):
        app = self.with_alpha()
        app.set_week_follows_tv(True)

        self.assertEqual(app.effective_week(), ['' for _ in range(7)])

    def test_whether_the_week_is_matched_survives_a_restart(self):
        self.install_tv()
        app = self.with_alpha()
        app.set_week_follows_tv(True)

        from paragon_home import ParagonHome

        self.assertTrue(ParagonHome().week_follows_tv)

    def test_reading_one_of_the_three_does_not_discard_another(self):
        """They share a file, and any of them can be asked for first."""
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._week = ['Alpha'] * 7

        following = app.week_follows_tv      # was clobbering the week

        self.assertFalse(following)
        self.assertEqual(app.week, ['Alpha'] * 7)

    # -- where the times come from -----------------------------------------

    def install_tv(self, **settings):
        # Paragon TV computes its times from a hardcoded anchor and offsets.
        # Alpha anchors at 03:00, so its phase 2 is 03:40 and its phase 5
        # is 04:45.
        base = {'EnablePresetSystem': 'true', 'SaturdayPreset': '1'}
        base.update(settings)
        xbmcaddon.install('script.paragontv', base)

    def test_its_own_times_need_no_paragon_tv_at_all(self):
        app = self.with_alpha(follow_tv=False)

        self.assertEqual(app.run_due_phases(now=self.SATURDAY),
                         ['Alpha phase 2'])

    def test_following_paragon_tv_takes_the_matching_presets_times(self):
        """Alpha here lines up with Alpha there, which is why the names match."""
        self.install_tv()
        app = self.with_alpha(follow_tv=True)

        # Its own phase 2 was 07:00; Paragon TV's Alpha phase 2 is 03:40.
        self.assertEqual(app.run_due_phases(now=self.SATURDAY), [])
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=3, minute=40)),
            ['Alpha phase 2'])

    def test_a_phase_paragon_tv_has_no_time_for_does_not_run(self):
        """Falling back to a stale local time would fire it at an hour
        nobody set."""
        self.install_tv()
        app = self.with_alpha(follow_tv=True)

        # Phase 3 has a local time of 23:30 and no Paragon TV time.
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=23, minute=30)),
            [])

    def test_a_phase_can_hold_the_lights_back_past_the_television(self):
        """The case the per-phase switch exists for.

        Phase 5 runs the lights at 07:00 of its own accord while phases 6
        onward take the television's word -- one rerack doing both, which is
        the ordinary arrangement rather than an exotic one.
        """
        xbmcaddon.install('script.paragontv', {
            'EnablePresetSystem': 'true', 'SaturdayPreset': '1'})
        app = self.app()
        app._reracks = self.reracks.normalise_all([
            self.reracks.make_rerack('Alpha', [
                {}, {}, {}, {},
                {'sequence': 'Curtain Up', 'time': '06:00'},
                {'sequence': 'Wind Down'},
            ])])
        app._week = ['Alpha'] * 7
        app._phase_state = set()

        # Paragon TV wakes at its own Alpha phase 5 of 04:45. Not ours.
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=4, minute=45)),
            [])
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=6, minute=0)),
            ['Alpha phase 5'])
        # And phase 6 goes when the television goes, at 07:15.
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=7, minute=15)),
            ['Alpha phase 6'])

    def test_a_phase_with_its_own_time_ignores_paragon_tv_entirely(self):
        self.install_tv()          # Alpha phase 2 is 03:40 there
        app = self.with_alpha(follow_tv=False)   # and 07:00 here

        self.assertEqual(app.run_due_phases(now=self.SATURDAY),
                         ['Alpha phase 2'])
        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=9)), [])

    def test_a_rerack_that_used_to_follow_tv_now_has_blank_phase_times(self):
        """The switch moved to the phase, so following became "no time"."""
        carried = self.reracks.normalise(
            {'name': 'Alpha', 'follow_tv': True,
             'phases': [{'sequence': 'Curtain Up', 'time': '07:00'}]})

        self.assertEqual(carried['phases'][0]['time'], '')
        self.assertTrue(self.reracks.follows_tv(carried['phases'][0]))
        self.assertNotIn('follow_tv', carried)

    def test_following_paragon_tv_with_it_switched_off_runs_nothing(self):
        self.install_tv(EnablePresetSystem='false')
        app = self.with_alpha(follow_tv=True)

        self.assertEqual(
            app.run_due_phases(now=self.SATURDAY.replace(hour=9)), [])

    def test_following_paragon_tv_without_it_installed_runs_nothing(self):
        app = self.with_alpha(follow_tv=True)

        for hour in (7, 9, 17, 19):
            self.assertEqual(
                app.run_due_phases(now=self.SATURDAY.replace(hour=hour)), [])


class TestAdoptingTheOldId(unittest.TestCase):
    """Kodi files saved data under the add-on id, and the id changed.

    Without this, updating an existing installation would open to an empty
    house: no devices, no scenes, no sequences, and a Tuya key to go and
    fetch again.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home'):
            if name in sys.modules:
                del sys.modules[name]
        self.old = os.path.join(tempfile.gettempdir(), 'paragon-old-profile')
        if os.path.isdir(self.old):
            shutil.rmtree(self.old)
        os.makedirs(self.old)

    def tearDown(self):
        clean_profile()
        if os.path.isdir(self.old):
            shutil.rmtree(self.old)

    def install_old(self, files):
        for name, payload in files.items():
            handle = open(os.path.join(self.old, name), 'w')
            try:
                handle.write(payload)
            finally:
                handle.close()
        xbmcaddon.install('script.paragon.govee', {}, profile=self.old)

    def test_everything_saved_under_the_old_id_comes_across(self):
        self.install_old({
            'devices.json': '[{"device_id": "AA:BB", "name": "Hall Lamp"}]',
            'scenes.json': '[{"name": "Warshade"}]',
            'sequences.json': '[{"name": "Ignition"}]',
            'rerack_presets.json': '{"week": ["Alpha"]}',
            'tuya_keys.json': '{"wp9abc": "0123456789abcdef"}',
            'broadlink_codes.json': '{"EE:FF": {"TV power": "26"}}',
            'palette.json': '[{"name": "Paragon Purple"}]',
            'settings.xml': '<settings />',
        })
        import addon_utils

        taken = addon_utils.adopt_legacy_profile()

        self.assertEqual(len(taken), 8)
        from paragon_home import ParagonHome
        app = ParagonHome()
        self.assertEqual(app.devices[0].name, 'Hall Lamp')
        self.assertIsNotNone(app.scene_by_name('Warshade'))
        self.assertIsNotNone(app.sequence_by_name('Ignition'))

    def test_the_old_folder_is_left_exactly_as_it_was(self):
        """A copy, never a move: the older installation still works."""
        self.install_old({'devices.json': '[]', 'scenes.json': '[]'})
        import addon_utils

        addon_utils.adopt_legacy_profile()

        self.assertTrue(os.path.exists(
            os.path.join(self.old, 'devices.json')))

    def test_nothing_is_taken_when_there_is_already_something_here(self):
        """Only ever on a fresh install, so it cannot overwrite live data."""
        import addon_utils

        addon_utils.write_json('devices.json',
                               [{'device_id': 'CC:DD', 'name': 'Mine'}])
        self.install_old({'devices.json':
                          '[{"device_id": "AA:BB", "name": "Theirs"}]'})

        self.assertEqual(addon_utils.adopt_legacy_profile(), [])

        from paragon_home import ParagonHome
        self.assertEqual(ParagonHome().devices[0].name, 'Mine')

    def test_a_file_already_here_is_not_overwritten(self):
        import addon_utils

        addon_utils.write_json('scenes.json', [{'name': 'Kept'}])
        self.install_old({'devices.json': '[]',
                          'scenes.json': '[{"name": "Replaced"}]'})

        taken = addon_utils.adopt_legacy_profile()

        self.assertIn('devices.json', taken)
        self.assertNotIn('scenes.json', taken)

    def test_nothing_happens_when_the_old_add_on_is_gone(self):
        import addon_utils

        self.assertEqual(addon_utils.adopt_legacy_profile(), [])

    def test_only_our_own_kinds_of_file_are_taken(self):
        self.install_old({'devices.json': '[]', 'notes.txt': 'hello',
                          'icon.png': 'x'})
        import addon_utils

        taken = addon_utils.adopt_legacy_profile()

        self.assertEqual(taken, ['devices.json'])


class TestParagonTV(unittest.TestCase):
    """Reading Paragon TV's own Sequence schedule, exactly as it reads it."""

    SATURDAY = datetime.datetime(2026, 8, 22, 18, 0)
    THURSDAY = datetime.datetime(2026, 8, 20, 18, 0)

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home', 'paragon_tv', 'sequences'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def install_tv(self, **settings):
        """Paragon TV as Kodi would present it, with its own settings."""
        # No phase times: Paragon TV computes them from a hardcoded anchor
        # and offsets, and the settings fields are a disabled copy.
        base = {'EnablePresetSystem': 'true',
                # The index of the preset, which is what the setting holds.
                'SaturdayPreset': '1',        # Alpha
                'ThursdayPreset': '6'}        # Sigma, a satellite preset
        base.update(settings)
        xbmcaddon.install('script.paragontv', base)
        import paragon_tv
        return paragon_tv

    def test_it_notices_when_paragon_tv_is_not_installed(self):
        import paragon_tv

        self.assertFalse(paragon_tv.installed())
        self.assertFalse(paragon_tv.enabled())
        self.assertEqual(paragon_tv.todays_preset(self.SATURDAY), '')

    def test_a_day_setting_holds_an_index_and_not_a_name(self):
        """Reading it as a name would silently find nothing at all."""
        tv = self.install_tv()

        self.assertEqual(tv.todays_preset(self.SATURDAY), 'Alpha')
        self.assertEqual(tv.preset_for_day(3), 'Sigma')

    def test_a_day_set_to_none_has_no_preset(self):
        tv = self.install_tv(SaturdayPreset='0')

        self.assertEqual(tv.todays_preset(self.SATURDAY), '')

    def test_an_index_out_of_range_is_no_preset_rather_than_a_crash(self):
        for value in ('99', '-1', 'Alpha', ''):
            tv = self.install_tv(SaturdayPreset=value)
            self.assertEqual(tv.todays_preset(self.SATURDAY), '',
                             'accepted %r as a preset index' % value)

    def test_phase_times_are_the_anchor_plus_its_offsets(self):
        """Alpha anchors at 03:00 and its first offset is 40 minutes."""
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Alpha', 1), '03:00')
        self.assertEqual(tv.phase_time('Alpha', 2), '03:40')
        self.assertEqual(tv.phase_time('Alpha', 9), '17:45')

    def test_the_computed_times_match_paragon_tvs_own_log(self):
        """Delta, taken from a line Paragon TV printed on the real box."""
        tv = self.install_tv()

        self.assertEqual(
            tv.compute_phase_times('Delta'),
            {1: '05:00', 2: '05:40', 3: '06:00', 4: '06:05', 5: '06:45',
             6: '12:15', 7: '13:45', 8: '17:15', 9: '17:45'})

    def test_a_stored_phase_time_is_ignored(self):
        """Paragon TV's own settings fields are disabled copies, not sources."""
        tv = self.install_tv(AlphaPhase2Time='23:59')

        self.assertEqual(tv.phase_time('Alpha', 2), '03:40')

    def test_an_offset_crossing_midnight_wraps(self):
        """Alpha anchors at 03:00 and its last offset is 885 minutes."""
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Alpha', 9), '17:45')
        self.assertEqual(tv.compute_phase_times('Omega')[9], '20:30')

    def test_a_satellite_has_neither_a_maintenance_phase_nor_a_push(self):
        """Both belong to the master alone, so neither has a time at all.

        Maintenance is work done once for everyone; the push is by definition
        what a master does TO a satellite. A satellite anchors at phase 2 and
        its offsets step over phase 4.
        """
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Sigma', 1), '')
        self.assertEqual(tv.phase_time('Sigma', 2), '04:25')
        self.assertEqual(tv.phase_time('Sigma', 3), '04:35')
        self.assertEqual(tv.phase_time('Sigma', 4), '')
        self.assertEqual(tv.phase_time('Sigma', 5), '04:45')

    def test_no_satellite_anywhere_has_a_push_or_a_maintenance_phase(self):
        """The masters keep both, so this is a property of being a satellite
        rather than something true of Sigma by accident.

        Phases 1 and 4 are named here rather than read from
        MASTER_ONLY_PHASES. A test that asks the code which phases to check
        agrees with the code by construction and can never disagree with it.
        """
        tv = self.install_tv()

        for preset in tv.PRESET_NAMES:
            satellite = preset in tv.SATELLITE_PRESETS
            for phase in (1, 4):                 # maintenance, push
                self.assertEqual(
                    not tv.phase_time(preset, phase), satellite,
                    '%s phase %d' % (preset, phase))

    def test_dropping_the_push_left_every_other_satellite_phase_alone(self):
        """The offsets shifted along one when the push came out of them, so
        the thing to check is that nothing else moved with it."""
        tv = self.install_tv()

        self.assertEqual(
            tv.compute_phase_times('Zeta'),
            {2: '06:20', 3: '06:40', 5: '06:55', 6: '12:15', 7: '13:45',
             8: '17:15', 9: '17:55'})

    def test_omicron_keeps_omegas_clock_from_the_first_wake_on(self):
        """The second master and satellite pair, kept together the same way.

        Omicron differs from Sigma in that it already started up after its
        master had pushed, so the two part only over the maintenance window
        rather than over the startup as well.
        """
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Omicron', 1), '')     # no maintenance
        self.assertEqual(tv.phase_time('Omicron', 4), '')     # no push
        for phase in range(5, 10):
            self.assertEqual(
                tv.phase_time('Omicron', phase), tv.phase_time('Omega', phase),
                'phase %d differs between Omega and its satellite' % phase)

    def test_omegas_evening_block(self):
        """Pinned outright, being the half of the day that was reset."""
        tv = self.install_tv()

        self.assertEqual(
            [tv.phase_time('Omega', p) for p in range(6, 10)],
            ['10:15', '19:15', '19:45', '20:30'])

    def test_moving_omega_left_its_morning_and_alpha_alone(self):
        """Only phases 7 to 10 moved, and only on the Omega pair."""
        tv = self.install_tv()

        self.assertEqual(
            [tv.shutdown_time('Omega')]
            + [tv.phase_time('Omega', p) for p in range(1, 6)],
            ['01:30', '05:00', '05:40', '06:00', '06:05', '06:45'])
        self.assertEqual(
            [tv.phase_time('Alpha', p) for p in range(6, 10)],
            ['07:15', '16:15', '17:15', '17:45'])

    def test_sigma_keeps_alphas_clock_from_the_first_wake_on(self):
        """Master and satellite share the whole viewing day. They part only
        over the early phases, where the two boxes have different work."""
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Sigma', 1), '')      # no maintenance
        for phase in range(5, 10):
            self.assertEqual(
                tv.phase_time('Sigma', phase), tv.phase_time('Alpha', phase),
                'phase %d differs between Alpha and its satellite' % phase)

    def test_sigma_starts_up_after_its_master_has_pushed(self):
        """The reason the two part early. A satellite that starts before the
        push comes up on yesterday's channels and goes down again before
        today's have arrived."""
        tv = self.install_tv()

        self.assertGreater(tv.phase_time('Sigma', 2),   # its first startup
                           tv.phase_time('Alpha', 4))   # the master's push

    def test_no_two_phases_of_a_preset_share_a_time(self):
        """Paragon TV checks its phases down an elif chain, so two phases at
        the same minute means the second one never runs at all.

        Omicron, Theta and Lambda used to share 06:25 between their first
        shutdown and their push. Taking the push off satellites altogether
        took that with it, so this can be strict about every phase that has
        a time at all.
        """
        tv = self.install_tv()

        for preset in tv.PRESET_NAMES:
            times = [t for t in [tv.shutdown_time(preset)]
                     + [tv.phase_time(preset, p) for p in range(1, 10)] if t]
            self.assertEqual(sorted(times), sorted(set(times)),
                             '%s has two phases at the same minute'
                             % preset)

    def test_zeta_is_the_tenth_preset(self):
        """Paragon TV grew one, and a day set to it must find a rerack."""
        tv = self.install_tv(SaturdayPreset='10')

        self.assertEqual(tv.todays_preset(self.SATURDAY), 'Zeta')
        # A satellite, so phase 2 is the anchor itself.
        self.assertEqual(tv.phase_time('Zeta', 1), '')
        self.assertEqual(tv.phase_time('Zeta', 2), '06:20')
        self.assertEqual(tv.phase_time('Zeta', 3), '06:40')

    def test_satellite_mode_means_today_cannot_be_known_from_here(self):
        """Paragon TV asks the master over SSH; there is nothing local to read."""
        tv = self.install_tv(SatelliteMode='true')

        self.assertEqual(tv.todays_preset(self.SATURDAY), '')
        self.assertEqual(tv.week(), ['' for _ in range(7)])
        self.assertIn('Satellite Mode', tv.report('Alpha', self.SATURDAY))
        # Its phase times are still knowable, because they are not per-box.
        self.assertEqual(tv.phase_time('Alpha', 2), '03:40')

    def test_a_disagreeing_reference_time_is_reported_as_drift(self):
        """Paragon TV keeps disabled copies; when one differs, we are stale."""
        tv = self.install_tv(AlphaPhase2Time='03:40')
        self.assertNotIn('no longer agree', tv.report('Alpha', self.SATURDAY))

        tv = self.install_tv(AlphaPhase2Time='06:00')
        text = tv.report('Alpha', self.SATURDAY)
        self.assertIn('no longer agree', text)
        self.assertIn('Paragon TV says 06:00, this says 03:40', text)

    def test_a_phase_number_out_of_range_has_no_time(self):
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Alpha', 0), '')
        self.assertEqual(tv.phase_time('Alpha', 10), '')

    def test_the_status_says_what_today_holds(self):
        tv = self.install_tv()

        text = tv.status(self.SATURDAY)

        self.assertIn('Alpha', text)
        self.assertIn('03:00', text)          # its anchor
        self.assertIn('03:40', text)          # anchor + its first offset
        self.assertIn('maintenance', text)

    def test_the_status_says_when_there_is_nothing_to_say(self):
        import paragon_tv
        self.assertIn('not installed', paragon_tv.status(self.SATURDAY))

        tv = self.install_tv(EnablePresetSystem='false')
        self.assertIn('switched off', tv.status(self.SATURDAY))

        tv = self.install_tv(SaturdayPreset='0')
        self.assertIn('no preset scheduled', tv.status(self.SATURDAY))

    def test_the_report_names_which_of_the_four_things_went_wrong(self):
        """"No time for it" is true and says nothing about why."""
        import paragon_tv

        # 1. Not there at all.
        self.assertIn('not installed', paragon_tv.report('Alpha',
                                                         self.SATURDAY))

        # 2. There, but switched off.
        tv = self.install_tv(EnablePresetSystem='false')
        self.assertIn('OFF', tv.report('Alpha', self.SATURDAY))

        # 3. On, but a preset Paragon Home has no timings for -- which is
        #    what a new preset in Paragon TV would look like from here.
        tv = self.install_tv()
        text = tv.report('Kappa', self.SATURDAY)
        self.assertIn('None at all', text)

        # 4. On, with a preset it does know.
        text = tv.report('Alpha', self.SATURDAY)
        self.assertIn('03:40', text)
        self.assertNotIn('None at all', text)

    def _paragon_tv_declaring(self, settings_xml, **settings):
        """A Paragon TV whose own settings page declares what is given.

        The point is a build whose scheme differs from the published one --
        which is exactly what turned up in practice, and is invisible from
        here unless something reads the settings page itself.
        """
        root = os.path.join(tempfile.gettempdir(), 'fake-paragontv')
        resources = os.path.join(root, 'resources')
        if os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(resources)
        handle = open(os.path.join(resources, 'settings.xml'), 'w')
        try:
            handle.write(settings_xml)
        finally:
            handle.close()
        self.addCleanup(shutil.rmtree, root, True)

        base = {'EnablePresetSystem': 'true', 'SaturdayPreset': '1'}
        base.update(settings)
        xbmcaddon.install('script.paragontv', base, path=root)
        import paragon_tv
        return paragon_tv

    def test_it_reports_the_names_the_installed_build_actually_uses(self):
        """When a build stores its times some other way, say so with names."""
        tv = self._paragon_tv_declaring(
            '<settings>'
            '<setting id="EnablePresetSystem" type="bool"/>'
            '<setting id="KappaAnchorTime" type="time"/>'
            '<setting id="KappaPhase2Offset" type="number"/>'
            '</settings>')

        text = tv.report('Kappa', self.SATURDAY)

        self.assertIn('KappaAnchorTime', text)
        self.assertIn('KappaPhase2Offset', text)

    def test_it_says_when_the_build_mentions_the_preset_nowhere(self):
        tv = self._paragon_tv_declaring(
            '<settings><setting id="EnablePresetSystem" type="bool"/>'
            '</settings>')

        self.assertIn('declares no setting mentioning Kappa',
                      tv.report('Kappa', self.SATURDAY))

    def test_an_unreadable_settings_page_is_not_mistaken_for_our_own(self):
        """An empty add-on path once joined to a relative one and read ours."""
        import paragon_tv

        xbmcaddon.install('script.paragontv',
                          {'EnablePresetSystem': 'true'})

        self.assertEqual(paragon_tv.setting_ids(), [])
        self.assertIn('could not be read',
                      paragon_tv.report('Kappa', self.SATURDAY))

    def test_the_report_lists_all_nine_phases_whether_set_or_not(self):
        tv = self.install_tv()

        text = tv.report('Sigma', self.SATURDAY)

        for phase in range(1, 10):
            self.assertIn('Phase %d' % phase, text)
        # Sigma is a satellite, so phase 1 is the one with nothing.
        self.assertIn('Phase 1  (none)', text)

    def test_the_report_says_what_today_runs(self):
        tv = self.install_tv()

        self.assertIn('Today it runs: Alpha',
                      tv.report('Alpha', self.SATURDAY))

    # -- a sequence following a phase ----------------------------------------

    def app(self, tv=None):
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.recorder = RecordingController()
        # A Govee lamp, so a light: colour and brightness, not just a switch.
        self.recorder.capabilities = lambda d: set(
            ['power', 'brightness', 'color', 'color_temp', 'state'])
        app.controller = self.recorder
        app._devices = [Device('AA:BB', name='Lamp', driver='govee',
                               lan=True)]
        app._scenes = [scene_lib.make_scene('Warshade', targets=['AA:BB'])]
        return app

    def following(self, phase):
        import sequences

        return sequences.make_sequence(
            'Curtain Up', [{'kind': 'scene', 'target': 'Warshade'}],
            phase=phase)

    def test_a_sequence_can_hang_off_a_phase_instead_of_a_clock(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]

        # Saturday is Alpha, whose phase 2 is 03:00 + 40 minutes.
        at_alpha_two = self.SATURDAY.replace(hour=3, minute=40)

        self.assertEqual(app.run_due_sequences(now=at_alpha_two),
                         ['Curtain Up'])

    def test_it_does_not_run_at_another_phases_time(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=23, minute=30)),
            [])

    def test_it_follows_today_preset_rather_than_a_fixed_time(self):
        """The same sequence runs at a different hour on a different day."""
        # Omicron rather than the fixture's Sigma: Sigma keeps Alpha's clock
        # exactly now, so it can no longer show two days running at two
        # different hours. Omicron anchors at 06:15 and still can.
        self.install_tv(ThursdayPreset='7')
        app = self.app()
        app._sequences = [self.following(2)]

        # Thursday is Omicron, whose phase 2 is its anchor of 06:15 rather
        # than Alpha's 03:40.
        self.assertEqual(
            app.run_due_sequences(now=self.THURSDAY.replace(hour=3,
                                                            minute=40)), [])
        self.assertEqual(
            app.run_due_sequences(now=self.THURSDAY.replace(hour=6,
                                                            minute=15)),
            ['Curtain Up'])

    def test_it_does_not_run_on_a_day_with_no_preset(self):
        self.install_tv(SaturdayPreset='0')
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=7)), [])

    def test_it_does_not_run_when_paragon_tv_is_switched_off(self):
        """Paragon TV's own master switch governs this too."""
        self.install_tv(EnablePresetSystem='false')
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=7)), [])

    def test_it_does_nothing_at_all_without_paragon_tv(self):
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=7)), [])

    def test_a_phase_a_satellite_preset_lacks_simply_never_comes_round(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(1)]      # maintenance

        # Thursday is Sigma, a satellite, which has no phase 1 at all.
        for hour in (4, 5, 6):
            self.assertEqual(
                app.run_due_sequences(now=self.THURSDAY.replace(hour=hour)),
                [], 'ran at %d on a satellite day' % hour)
        # Saturday is Alpha, whose phase 1 is its anchor of 03:00.
        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=3)),
            ['Curtain Up'])

    def test_it_still_runs_only_once_a_day(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]
        at_seven = self.SATURDAY.replace(hour=7)

        app.run_due_sequences(now=at_seven)

        self.assertEqual(
            app.run_due_sequences(now=at_seven + datetime.timedelta(minutes=2)),
            [])

    def test_a_phase_that_moves_re_arms_the_sequence(self):
        """The record holds the time, so a phase that moved counts as new.

        Paragon TV's timings are hardcoded rather than settings now, so this
        moves the table itself -- which is what a Paragon TV update would do.
        """
        tv = self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]
        app.run_due_sequences(now=self.SATURDAY.replace(hour=3, minute=40))

        self.addCleanup(tv.ANCHOR_TIMES.__setitem__, 'Alpha',
                        tv.ANCHOR_TIMES['Alpha'])
        tv.ANCHOR_TIMES['Alpha'] = '07:20'          # phase 2 becomes 08:00

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=8)),
            ['Curtain Up'])

    def test_following_a_phase_reads_as_such(self):
        import sequences

        self.assertEqual(sequences.describe_schedule(self.following(3)),
                         'Paragon TV phase 3 (shut down)')

    def test_paragon_tv_settings_are_never_written_to(self):
        """A working television setup must not be disturbed by any of this."""
        tv = self.install_tv()
        before = dict(xbmcaddon.FOREIGN['script.paragontv'])
        app = self.app()
        app._sequences = [self.following(2)]

        app.run_due_sequences(now=self.SATURDAY.replace(hour=7))

        self.assertEqual(xbmcaddon.FOREIGN['script.paragontv'], before)


class TestHexColours(unittest.TestCase):
    """The Govee app hands out 8-digit codes, so pasting one must work."""

    def test_six_and_three_digit_forms(self):
        self.assertEqual(scene_lib.parse_hex_color('FF8800')[0], (255, 136, 0))
        self.assertEqual(scene_lib.parse_hex_color('#FF8800')[0], (255, 136, 0))
        self.assertEqual(scene_lib.parse_hex_color('f80')[0], (255, 136, 0))
        self.assertEqual(scene_lib.parse_hex_color(' ff 88 00 ')[0],
                         (255, 136, 0))

    def test_leading_ff_is_read_as_alpha_first(self):
        rgb, note = scene_lib.parse_hex_color('FFFF2896')
        self.assertEqual(rgb, (255, 40, 150))
        self.assertIn('AARRGGBB', note)

    def test_real_govee_codes(self):
        """Straight out of the Govee app.

        The RRGGBBAA reading of these would mean alphas of 7F and 3C -- bulbs
        at 50% and 23% transparency -- so AARRGGBB is the only sensible one.
        """
        rgb, note = scene_lib.parse_hex_color('FF3C447F')
        self.assertEqual(rgb, (60, 68, 127))     # deep slate blue
        self.assertIn('AARRGGBB', note)

        rgb, note = scene_lib.parse_hex_color('FF7F3C3C')
        self.assertEqual(rgb, (127, 60, 60))     # deep brick red
        self.assertIn('AARRGGBB', note)

    def test_a_govee_code_whose_last_byte_is_ff_still_reads_as_argb(self):
        """The ambiguous default has to match Govee, not just be a coin flip.

        A Govee colour with blue at full lands on FF at both ends, and the
        alpha-first default is what keeps that correct.
        """
        rgb, note = scene_lib.parse_hex_color('FF3C44FF')
        self.assertEqual(rgb, (60, 68, 255))
        self.assertIn('ambiguous', note)

    def test_trailing_ff_is_read_as_alpha_last(self):
        rgb, note = scene_lib.parse_hex_color('2896FFFF')
        self.assertEqual(rgb, (40, 150, 255))
        self.assertIn('RRGGBBAA', note)

    def test_ambiguous_codes_default_to_alpha_first_and_say_so(self):
        """FF at both ends, or neither, cannot be told apart."""
        rgb, note = scene_lib.parse_hex_color('FF00FFFF')
        self.assertEqual(rgb, (0, 255, 255))
        self.assertIn('ambiguous', note)

        rgb, note = scene_lib.parse_hex_color('8000FF80')
        self.assertEqual(rgb, (0, 255, 128))
        self.assertIn('ambiguous', note)

    def test_six_digit_codes_carry_no_note(self):
        """Nothing was inferred, so there is nothing to warn about."""
        self.assertEqual(scene_lib.parse_hex_color('FF8800')[1], '')

    def test_rubbish_is_rejected_with_a_reason(self):
        for bad in ('nope', 'FF88', '', None, 'GGGGGG', 'FF8800AABB'):
            rgb, reason = scene_lib.parse_hex_color(bad)
            self.assertIsNone(rgb, bad)
            self.assertTrue(reason)

    def test_the_error_mentions_both_accepted_lengths(self):
        _rgb, reason = scene_lib.parse_hex_color('FF88')
        self.assertIn('6 or 8', reason)


class TestColourMix(unittest.TestCase):
    """Several colours spread evenly but randomly over the lights."""

    @staticmethod
    def devices(count):
        return [Device('%02X' % i, name='Light %d' % i, lan=True)
                for i in range(count)]

    def test_even_split_over_many_lights(self):
        """25 lights and 3 colours must be 9/8/8, not whatever chance gives."""
        dealt = scene_lib.deal_colors(['R', 'G', 'B'], 25)
        counts = sorted([dealt.count(c) for c in ('R', 'G', 'B')])
        self.assertEqual(counts, [8, 8, 9])
        self.assertEqual(len(dealt), 25)

    def test_even_split_when_it_divides_exactly(self):
        dealt = scene_lib.deal_colors(['R', 'G'], 10)
        self.assertEqual(dealt.count('R'), 5)
        self.assertEqual(dealt.count('G'), 5)

    def test_more_colours_than_lights_uses_a_subset_once_each(self):
        dealt = scene_lib.deal_colors(['R', 'G', 'B', 'Y'], 2)
        self.assertEqual(len(dealt), 2)
        self.assertEqual(len(set(dealt)), 2)

    def test_the_spare_light_does_not_always_go_to_the_same_colour(self):
        """Repeat-and-truncate alone would hand it to the first colour every
        time, which is a visible bias over repeated applications."""
        winners = set()
        for _ in range(60):
            dealt = scene_lib.deal_colors(['R', 'G', 'B'], 25)
            winners.add([c for c in ('R', 'G', 'B') if dealt.count(c) == 9][0])
        self.assertEqual(winners, {'R', 'G', 'B'})

    def test_the_arrangement_changes_between_applications(self):
        first = scene_lib.deal_colors(['R', 'G', 'B'], 25)
        self.assertTrue(any(scene_lib.deal_colors(['R', 'G', 'B'], 25) != first
                            for _ in range(40)))

    def test_empty_inputs_are_handled(self):
        self.assertEqual(scene_lib.deal_colors([], 5), [])
        self.assertEqual(scene_lib.deal_colors(['R'], 0), [])

    def test_applying_a_mix_gives_every_light_one_of_the_colours(self):
        devices = self.devices(7)
        scene = scene_lib.make_scene(
            'Party', brightness=60, mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])

        controller = RecordingController()
        applied, errors = scene_lib.apply_scene(controller, scene, devices)

        self.assertEqual(applied, 7)
        self.assertEqual(errors, [])
        colors = [c[2:] for c in controller.calls if c[0] == 'color']
        self.assertEqual(len(colors), 7)
        self.assertEqual(set(colors), {(255, 0, 0), (0, 0, 255)})
        # Even: 4 and 3, in some order.
        self.assertEqual(sorted([colors.count((255, 0, 0)),
                                 colors.count((0, 0, 255))]), [3, 4])

        # Brightness still applies to all of them.
        self.assertEqual(len([c for c in controller.calls
                              if c[0] == 'brightness']), 7)

    def test_applying_a_mix_does_not_write_back_into_the_scene(self):
        """The dealt colour is per-application, not a scene edit."""
        devices = self.devices(4)
        scene = scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        before = json.dumps(scene, sort_keys=True)

        scene_lib.apply_scene(RecordingController(), scene, devices)
        self.assertEqual(json.dumps(scene, sort_keys=True), before)

    def test_a_captured_per_light_entry_wins_over_the_mix(self):
        one, two = self.devices(2)
        scene = scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]}],
            devices={one.device_id: {'power': 'on', 'brightness': 10,
                                     'mode': scene_lib.MODE_TEMP,
                                     'kelvin': 2200,
                                     'color': [255, 255, 255]}})

        controller = RecordingController()
        scene_lib.apply_scene(controller, scene, [one, two])

        self.assertIn(('temp', one.device_id, 2200), controller.calls)
        self.assertIn(('color', two.device_id, 255, 0, 0), controller.calls)

    def test_a_mix_survives_a_json_round_trip(self):
        scene = scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        restored = scene_lib.normalise(json.loads(json.dumps(scene)))

        self.assertEqual(restored['mode'], scene_lib.MODE_MIX)
        self.assertEqual([e['name'] for e in restored['colors']],
                         ['Red', 'Blue'])

    def test_a_mix_with_no_colours_degrades_to_leaving_colour_alone(self):
        scene = scene_lib.normalise({'name': 'Empty',
                                     'mode': scene_lib.MODE_MIX,
                                     'colors': []})
        self.assertEqual(scene['mode'], scene_lib.MODE_NONE)

    def test_bare_rgb_entries_are_accepted_and_named_by_hex(self):
        scene = scene_lib.normalise({'name': 'Hand edited',
                                     'mode': scene_lib.MODE_MIX,
                                     'colors': [[255, 40, 150], 'junk',
                                                {'color': [0, 255, 0]}]})
        self.assertEqual([e['name'] for e in scene['colors']],
                         ['#FF2896', '#00FF00'])

    def test_describe_mentions_the_mix(self):
        scene = scene_lib.make_scene(
            'Party', brightness=60, mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        text = scene_lib.describe(scene)
        self.assertIn('mix of 2 colours', text)
        self.assertIn('60%', text)


class TestCycling(unittest.TestCase):
    """Stepping a mix scene through its colours on a timer."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'palette', 'scenes'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def build(self, count=4, interval=60):
        import scenes as fresh_scenes
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('%02X' % i, name='Light %d' % i, lan=True,
                               ip='10.0.0.%d' % (i + 1))
                        for i in range(count)]
        recorder = RecordingController()
        app.controller = recorder

        scene = fresh_scenes.make_scene(
            'Party', mode=fresh_scenes.MODE_MIX, cycle=interval,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Green', 'color': [0, 255, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        app.save_scene(scene)
        return app, recorder, fresh_scenes

    def test_rotation_keeps_the_even_spread(self):
        import scenes as fresh_scenes

        ids = ['%02X' % i for i in range(25)]
        assignment = fresh_scenes.deal_assignment(3, ids)
        for _step in range(6):
            counts = sorted([list(assignment.values()).count(i)
                             for i in range(3)])
            self.assertEqual(counts, [8, 8, 9])
            assignment = fresh_scenes.rotate_assignment(assignment, 3)

    def test_rotation_moves_every_light_to_a_new_colour(self):
        import scenes as fresh_scenes

        before = fresh_scenes.deal_assignment(3, ['A', 'B', 'C', 'D'])
        after = fresh_scenes.rotate_assignment(before, 3)
        for key in before:
            self.assertNotEqual(before[key], after[key])

    def test_powering_off_stops_the_cycle(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        self.assertIsNotNone(app.read_cycle())

        app.power_all(False)

        self.assertIsNone(app.read_cycle())

    def test_powering_on_stops_the_cycle_too(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))

        app.power_all(True)

        self.assertIsNone(app.read_cycle())

    def test_setting_brightness_by_hand_stops_the_cycle(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))

        app.brightness_all(20)

        self.assertIsNone(app.read_cycle())

    def test_setting_a_colour_by_hand_stops_the_cycle(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))

        app.color_all([10, 20, 30])

        self.assertIsNone(app.read_cycle())

    def test_a_power_step_in_a_sequence_stops_the_cycle(self):
        """The reported case: a shutdown sequence run from Paragon TV."""
        import sequences as sequence_lib

        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        self.assertIsNotNone(app.read_cycle())

        sequence = sequence_lib.make_sequence('Shutdown', steps=[
            {'kind': sequence_lib.KIND_POWER, 'driver': 'govee',
             'target': sequence_lib.TARGET_ALL,
             'action': sequence_lib.ACTION_OFF}])
        app.run_sequence(sequence, announce=False)

        self.assertIsNone(app.read_cycle())

    def test_a_cycle_step_does_not_stop_its_own_cycle(self):
        """cycle_step goes to the scene engine directly, not through _each."""
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))

        self.assertTrue(app.cycle_step(now=1000.0))

        self.assertIsNotNone(app.read_cycle())

    def test_applying_a_cycling_scene_starts_the_cycle(self):
        app, _recorder, _lib = self.build()

        app.apply_scene(app.scene_by_name('Party'))

        state = app.read_cycle()
        self.assertIsNotNone(state)
        self.assertEqual(state['scene'], 'Party')
        self.assertEqual(state['interval'], 60)
        self.assertEqual(len(state['assignment']), 4)
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'cycle.json')))

    def test_applying_a_plain_scene_stops_a_running_cycle(self):
        """Otherwise dimming for a film gets overwritten by the party."""
        app, _recorder, lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        self.assertIsNotNone(app.read_cycle())

        app.apply_scene(lib.make_scene('Movie Night', brightness=8,
                                       mode=lib.MODE_TEMP, kelvin=2000))
        self.assertIsNone(app.read_cycle())

    def test_a_step_advances_every_light_and_reschedules(self):
        app, recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        before = dict(app.read_cycle()['assignment'])
        del recorder.calls[:]

        self.assertTrue(app.cycle_step(now=1000.0))

        after = app.read_cycle()
        for key in before:
            self.assertNotEqual(before[key], after['assignment'][key])
        self.assertEqual(after['next_at'], 1000.0 + 60)

        colors = [c for c in recorder.calls if c[0] == 'color']
        self.assertEqual(len(colors), 4)

    def test_a_step_sends_only_the_colour(self):
        """Power and brightness are already where the last step left them."""
        app, recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        del recorder.calls[:]

        app.cycle_step(now=1000.0)

        kinds = set(c[0] for c in recorder.calls)
        self.assertEqual(kinds, set(['color']))
        self.assertEqual(len(recorder.calls), 4)

    def test_the_first_apply_still_sets_power_and_brightness(self):
        app, recorder, lib = self.build()
        scene = app.scene_by_name('Party')
        scene['brightness'] = 60
        app.save_scene(scene)
        del recorder.calls[:]

        app.apply_scene(app.scene_by_name('Party'))

        kinds = set(c[0] for c in recorder.calls)
        self.assertEqual(kinds, set(['turn', 'brightness', 'color']))

    def test_sends_are_paced_across_the_lights(self):
        """A burst of datagrams to every bulb at once gets dropped."""
        import scenes as fresh_scenes

        devices = [Device('%02X' % i, name='L%d' % i, lan=True)
                   for i in range(6)]
        gaps = []
        fresh_scenes.apply_scene(
            RecordingController(),
            fresh_scenes.make_scene('Solid', mode=fresh_scenes.MODE_COLOR,
                                    color=[1, 2, 3]),
            devices, sleep_func=lambda s: gaps.append(s))

        # One pause between each pair of lights, none before the first.
        self.assertEqual(len(gaps), 5)
        self.assertTrue(all(g > 0 for g in gaps))

    def test_pacing_can_be_switched_off(self):
        import scenes as fresh_scenes

        gaps = []
        fresh_scenes.apply_scene(
            RecordingController(),
            fresh_scenes.make_scene('Solid', mode=fresh_scenes.MODE_COLOR),
            [Device('AA', name='L', lan=True), Device('BB', name='M', lan=True)],
            gap=0, sleep_func=lambda s: gaps.append(s))
        self.assertEqual(gaps, [])

    def test_colours_only_still_honours_temperature_entries(self):
        import scenes as fresh_scenes

        controller = RecordingController()
        device = Device('AA', name='L', lan=True)
        fresh_scenes.apply_settings(
            controller, device,
            {'power': 'on', 'brightness': 50, 'mode': fresh_scenes.MODE_TEMP,
             'kelvin': 2700, 'color': [255, 255, 255]},
            colors_only=True)
        self.assertEqual(controller.calls, [('temp', 'AA', 2700)])

    def test_not_due_yet_means_no_step(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        state = app.read_cycle()

        self.assertIsNone(app.cycle_due(now=state['next_at'] - 1))
        self.assertIsNotNone(app.cycle_due(now=state['next_at']))

    def test_a_clock_jump_backwards_does_not_stall_for_hours(self):
        app, _recorder, _lib = self.build(interval=60)
        app.apply_scene(app.scene_by_name('Party'))
        state = app.read_cycle()

        # next_at far in the future relative to "now" -- a wrong clock, not a
        # cycle that genuinely has hours to wait.
        self.assertIsNotNone(app.cycle_due(now=state['next_at'] - 10000))

    def test_lights_added_after_the_cycle_started_are_dealt_in(self):
        app, recorder, _lib = self.build(count=3)
        app.apply_scene(app.scene_by_name('Party'))

        app._devices.append(Device('FF', name='Newcomer', lan=True,
                                   ip='10.0.0.9'))
        del recorder.calls[:]
        app.cycle_step(now=2000.0)

        self.assertIn('FF', app.read_cycle()['assignment'])
        self.assertEqual(len([c for c in recorder.calls if c[0] == 'color']), 4)

    def test_a_deleted_scene_stops_the_cycle_rather_than_erroring(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))

        app._scenes = [s for s in app.scenes if s['name'] != 'Party']
        app.save_scenes()

        self.assertFalse(app.cycle_step(now=3000.0))
        self.assertIsNone(app.read_cycle())

    def test_stopping_leaves_the_lights_where_they_are(self):
        app, recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        del recorder.calls[:]

        self.assertEqual(app.stop_cycle(), 'Party')
        self.assertIsNone(app.read_cycle())
        self.assertEqual(recorder.calls, [])
        self.assertIsNone(app.stop_cycle())

    def test_a_cycle_survives_being_reloaded_from_disk(self):
        """The panel starts it; the service is a different process."""
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        expected = dict(app.read_cycle()['assignment'])

        from paragon_home import ParagonHome
        service_side = ParagonHome()
        service_side._devices = app._devices
        service_side.controller = RecordingController()

        state = service_side.read_cycle()
        self.assertEqual(state['scene'], 'Party')
        self.assertEqual(state['assignment'], expected)

        service_side.cycle_step(now=4000.0)
        self.assertEqual(len([c for c in service_side.controller.calls
                              if c[0] == 'color']), 4)

    def test_cycle_is_only_kept_for_a_mix(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.normalise({'name': 'Solid', 'cycle': 60,
                                        'mode': fresh_scenes.MODE_TEMP})
        self.assertEqual(scene['cycle'], 0)

        scene = fresh_scenes.normalise({
            'name': 'Party', 'cycle': 60, 'mode': fresh_scenes.MODE_MIX,
            'colors': [[255, 0, 0], [0, 0, 255]]})
        self.assertEqual(scene['cycle'], 60)

    def test_a_cycling_scene_survives_a_json_round_trip(self):
        app, _recorder, _lib = self.build()
        saved = json.load(open(os.path.join(PROFILE, 'scenes.json')))
        party = [s for s in saved if s['name'] == 'Party'][0]
        self.assertEqual(party['cycle'], 60)


class TestSceneCapture(unittest.TestCase):
    """Snapshotting the lights -- how a Govee Tap-to-Run gets into Kodi."""

    def test_state_to_settings_reads_a_temperature_bulb(self):
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 30, 'colorTem': 2200,
             'color': {'r': 0, 'g': 0, 'b': 0}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)
        self.assertEqual(settings['kelvin'], 2200)
        self.assertEqual(settings['brightness'], 30)

    def test_state_to_settings_reads_a_colour_bulb(self):
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 80, 'colorTem': 0,
             'color': {'r': 255, 'g': 40, 'b': 10}})
        self.assertEqual(settings['mode'], scene_lib.MODE_COLOR)
        self.assertEqual(settings['color'], [255, 40, 10])

    def test_a_tinted_bulb_wins_over_a_stale_temperature(self):
        """Taking kelvin first would capture a pink bulb as white."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 60, 'colorTem': 3800,
             'color': {'r': 255, 'g': 40, 'b': 150}})
        self.assertEqual(settings['mode'], scene_lib.MODE_COLOR)
        self.assertEqual(settings['color'], [255, 40, 150])

    def test_white_rgb_beside_a_temperature_is_read_as_white(self):
        """A real H610A reading: 255,255,255 next to colorTem 3800."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 35, 'colorTem': 3800,
             'color': {'r': 255, 'g': 255, 'b': 255}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)
        self.assertEqual(settings['kelvin'], 3800)

    def test_zero_rgb_beside_a_temperature_is_read_as_white(self):
        """A real H6008 reading: 0,0,0 next to colorTem 3800."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 1, 'colorTem': 3800,
             'color': {'r': 0, 'g': 0, 'b': 0}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)

    def test_colour_with_no_temperature_is_kept(self):
        """A real H6008 reading: 141,95,255 with colorTem 0."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 1, 'colorTem': 0,
             'color': {'r': 141, 'g': 95, 'b': 255}})
        self.assertEqual(settings['mode'], scene_lib.MODE_COLOR)
        self.assertEqual(settings['color'], [141, 95, 255])

    def test_near_grey_is_not_mistaken_for_a_colour(self):
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 50, 'colorTem': 4000,
             'color': {'r': 250, 'g': 255, 'b': 248}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)

    def test_state_to_settings_treats_off_as_off(self):
        settings = scene_lib.state_to_settings(
            {'power': 'off', 'brightness': 80, 'colorTem': 2700})
        self.assertEqual(settings['power'], scene_lib.POWER_OFF)

    def test_state_to_settings_rejects_unreadable_state(self):
        self.assertIsNone(scene_lib.state_to_settings(None))
        self.assertIsNone(scene_lib.state_to_settings({}))
        self.assertIsNone(scene_lib.state_to_settings({'power': 'unknown'}))

    def test_brightness_scale_is_detected_across_the_whole_capture(self):
        """One reading over 100 proves the 0-254 scale for every bulb."""
        wide = {'a': {'power': 'on', 'brightness': 203},
                'b': {'power': 'on', 'brightness': 51}}
        self.assertEqual(scene_lib.detect_brightness_scale(wide), 254)

        narrow = {'a': {'power': 'on', 'brightness': 80},
                  'b': {'power': 'on', 'brightness': 20}}
        self.assertEqual(scene_lib.detect_brightness_scale(narrow), 100)

        # Unreadable and absent entries must not confuse the detection.
        self.assertEqual(scene_lib.detect_brightness_scale(
            {'a': None, 'b': {'power': 'on'}, 'c': {'brightness': 'x'}}), 100)
        self.assertEqual(scene_lib.detect_brightness_scale({}), 100)

    def test_capture_rescales_every_bulb_once_the_scale_is_known(self):
        """The clamp symptom: two different levels both captured at 100%."""
        low = Device('AA:BB', name='Low', lan=True)
        high = Device('CC:DD', name='High', lan=True)
        states = {
            'AA:BB': {'power': 'on', 'brightness': 51, 'colorTem': 2700},
            'CC:DD': {'power': 'on', 'brightness': 203, 'colorTem': 2700},
        }
        scene, _c, _s = scene_lib.capture_scene('Wide', [low, high], states)

        self.assertEqual(scene['devices']['AA:BB']['brightness'], 20)
        self.assertEqual(scene['devices']['CC:DD']['brightness'], 80)

    def test_capture_leaves_documented_scale_readings_alone(self):
        low = Device('AA:BB', name='Low', lan=True)
        high = Device('CC:DD', name='High', lan=True)
        states = {
            'AA:BB': {'power': 'on', 'brightness': 20, 'colorTem': 2700},
            'CC:DD': {'power': 'on', 'brightness': 80, 'colorTem': 2700},
        }
        scene, _c, _s = scene_lib.capture_scene('Narrow', [low, high], states)

        self.assertEqual(scene['devices']['AA:BB']['brightness'], 20)
        self.assertEqual(scene['devices']['CC:DD']['brightness'], 80)

    def test_capture_skips_devices_that_did_not_answer(self):
        devices = [Device('AA:BB', name='One', lan=True),
                   Device('CC:DD', name='Two', lan=True)]
        states = {'AA:BB': {'power': 'on', 'brightness': 50, 'colorTem': 3000},
                  'CC:DD': None}

        scene, captured, skipped = scene_lib.capture_scene('Snap', devices,
                                                           states)
        self.assertEqual(captured, 1)
        self.assertEqual(skipped, ['Two'])
        self.assertEqual(scene['targets'], ['AA:BB'])

    def test_captured_scene_replays_each_light_differently(self):
        """The point of capture: one scene, 2 lights, 2 different states."""
        warm = Device('AA:BB', name='Warm', lan=True)
        red = Device('CC:DD', name='Red', lan=True)
        states = {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200},
            'CC:DD': {'power': 'on', 'brightness': 80, 'colorTem': 0,
                      'color': {'r': 255, 'g': 40, 'b': 10}},
        }
        scene, _captured, _skipped = scene_lib.capture_scene(
            'Twilight', [warm, red], states)

        controller = RecordingController()
        applied, errors = scene_lib.apply_scene(controller, scene, [warm, red])

        self.assertEqual(applied, 2)
        self.assertEqual(errors, [])
        self.assertIn(('brightness', 'AA:BB', 30), controller.calls)
        self.assertIn(('temp', 'AA:BB', 2200), controller.calls)
        self.assertIn(('brightness', 'CC:DD', 80), controller.calls)
        self.assertIn(('color', 'CC:DD', 255, 40, 10), controller.calls)

    def test_captured_off_lights_stay_off_on_replay(self):
        on = Device('AA:BB', name='On', lan=True)
        off = Device('CC:DD', name='Off', lan=True)
        states = {'AA:BB': {'power': 'on', 'brightness': 50, 'colorTem': 3000},
                  'CC:DD': {'power': 'off'}}
        scene, _c, _s = scene_lib.capture_scene('Mixed', [on, off], states)

        controller = RecordingController()
        scene_lib.apply_scene(controller, scene, [on, off])

        self.assertIn(('turn', 'CC:DD', False), controller.calls)
        self.assertNotIn(('brightness', 'CC:DD', 50), controller.calls)
        self.assertIn(('turn', 'AA:BB', True), controller.calls)

    def test_captured_scene_survives_a_json_round_trip(self):
        device = Device('AA:BB', name='One', lan=True)
        states = {'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}
        scene, _c, _s = scene_lib.capture_scene('Snap', [device], states)

        restored = scene_lib.normalise(json.loads(json.dumps(scene)))
        self.assertEqual(restored['devices']['AA:BB']['kelvin'], 2200)
        self.assertEqual(restored['devices']['AA:BB']['brightness'], 30)

    def test_normalise_clamps_per_device_entries_too(self):
        scene = scene_lib.normalise({
            'name': 'Hand edited',
            'devices': {'aa:bb': {'power': 'on', 'brightness': 9000,
                                  'mode': 'nonsense', 'kelvin': -5},
                        'cc:dd': 'not a dict'},
        })
        self.assertEqual(list(scene['devices'].keys()), ['AA:BB'])
        self.assertEqual(scene['devices']['AA:BB']['brightness'], 100)
        self.assertEqual(scene['devices']['AA:BB']['mode'],
                         scene_lib.MODE_NONE)
        self.assertEqual(scene['devices']['AA:BB']['kelvin'], 1500)

    def test_uniform_scenes_still_apply_to_devices_with_no_entry(self):
        """A device added after a capture falls back to the scene defaults."""
        known = Device('AA:BB', name='Known', lan=True)
        newcomer = Device('EE:FF', name='New', lan=True)
        scene = scene_lib.make_scene(
            'Mixed', brightness=55, mode=scene_lib.MODE_TEMP, kelvin=3000,
            devices={'AA:BB': {'power': 'on', 'brightness': 10,
                               'mode': scene_lib.MODE_TEMP, 'kelvin': 2000,
                               'color': [255, 255, 255]}})

        controller = RecordingController()
        scene_lib.apply_scene(controller, scene, [known, newcomer])

        self.assertIn(('brightness', 'AA:BB', 10), controller.calls)
        self.assertIn(('brightness', 'EE:FF', 55), controller.calls)
        self.assertIn(('temp', 'EE:FF', 3000), controller.calls)

    def test_describe_summarises_a_captured_scene(self):
        devices = [Device('AA:BB', name='One', lan=True),
                   Device('CC:DD', name='Two', lan=True)]
        states = {'AA:BB': {'power': 'on', 'brightness': 50, 'colorTem': 3000},
                  'CC:DD': {'power': 'off'}}
        scene, _c, _s = scene_lib.capture_scene('Snap', devices, states)
        text = scene_lib.describe(scene)
        self.assertIn('captured', text)
        self.assertIn('2 light(s)', text)
        self.assertIn('1 on', text)


class FakeRM(object):
    """A UDP socket that answers like a real Broadlink RM.

    Speaks the actual protocol -- checksums, AES, the auth key swap -- so the
    client is exercised against the wire format rather than against a mock of
    itself.
    """

    LEARNED = b'\x26\x00\x28\x00' + b'\x10' * 36

    def __init__(self, devtype=0x27c2, name='Lounge RM', new_framing=False):
        import broadlink_lan as bl

        self.bl = bl
        self.devtype = devtype
        # Which data payload layout this device accepts. Anything else is
        # refused with an error, exactly as the hardware does.
        self.new_framing = new_framing
        # Refuse every data command regardless of layout, to stand in for a
        # device that is genuinely failing rather than mismatched.
        self.refuse_all = False
        self.name = name
        self.mac = bytearray([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])
        self.session_key = bytearray(range(0x10, 0x20))
        self.device_id = bytearray([1, 2, 3, 4])
        self.sent_codes = []
        self.learning = False
        # Radio. A Pro has it, a Mini does not, and the sweep does not lock
        # on instantly -- holds_before_lock is how many "still looking"
        # answers to give before saying found, so a test can exercise the
        # waiting rather than only the happy first poll.
        self.has_radio = True
        self.sweeping = False
        self.sweeps = 0
        self.cancels = 0
        self.holds_before_lock = 0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()

    def _hello_reply(self):
        reply = bytearray(0x40 + len(self.name) + 1)
        reply[0x34] = self.devtype & 0xFF
        reply[0x35] = (self.devtype >> 8) & 0xFF
        reply[0x3A:0x40] = self.mac
        reply[0x40:0x40 + len(self.name)] = bytearray(self.name.encode())
        return bytes(reply)

    def _wrap(self, payload, key):
        from aes import AES

        payload = bytearray(payload)
        if len(payload) % 16:
            payload.extend(bytearray(16 - len(payload) % 16))
        packet = bytearray(0x38)
        packet.extend(bytearray(AES(key, self.bl.INITIAL_IV).encrypt(payload)))
        return bytes(packet)

    def _refuse(self, sender, code=0xFFFB):
        """Answer with a device error, the way a real one refuses."""
        error = bytearray(0x38)
        error[0x22] = code & 0xFF
        error[0x23] = (code >> 8) & 0xFF
        self.sock.sendto(bytes(error), sender)

    def _reply(self, data):
        """A data reply in whichever layout this device speaks."""
        import struct as _struct

        if self.new_framing:
            body = bytearray(_struct.pack('<I', 0)) + bytearray(data)
            payload = bytearray(_struct.pack('<H', len(body))) + body
        else:
            payload = bytearray(4) + bytearray(data)
        return self._wrap(payload, self.session_key)

    def _serve(self):
        from aes import AES

        while not self._stop.is_set():
            try:
                data, sender = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            raw = bytearray(data)
            if len(raw) == 0x30:                      # discovery hello
                self.sock.sendto(self._hello_reply(), sender)
                continue
            if len(raw) < 0x38:
                continue

            command = raw[0x26] | (raw[0x27] << 8)
            if command == self.bl.CMD_AUTH:
                key = self.bl.INITIAL_KEY
                payload = bytearray(0x40)
                payload[0x00:0x04] = self.device_id
                payload[0x04:0x14] = self.session_key
                self.sock.sendto(self._wrap(payload, key), sender)
                continue

            body = bytes(raw[0x38:])
            request = bytearray(
                AES(self.session_key, self.bl.INITIAL_IV).decrypt(body))

            if self.refuse_all:
                self._refuse(sender, code=0xFFF3)
                continue

            # Refuse the wrong payload layout, which is what a real device
            # does: it authenticates fine and then rejects every command.
            if self.new_framing:
                declared = request[0] | (request[1] << 8)
                if declared < 4 or declared > len(request):
                    self._refuse(sender)
                    continue
                verb, data = request[2], request[6:]
            else:
                if request[1] or request[2] or request[3]:
                    self._refuse(sender)
                    continue
                verb, data = request[0], request[4:]

            if verb == self.bl.DATA_SEND:
                self.sent_codes.append(bytes(data))
                self.sock.sendto(self._reply(bytearray(16)), sender)
            elif verb == self.bl.DATA_LEARN:
                self.learning = True
                self.sock.sendto(self._reply(bytearray(16)), sender)
            elif verb == self.bl.DATA_CHECK:
                if not self.learning:
                    self._refuse(sender, code=0xFFF9)
                else:
                    self.sock.sendto(self._reply(bytearray(self.LEARNED)),
                                     sender)
            elif verb == self.bl.DATA_LEARN_RF_SWEEP:
                if not self.has_radio:
                    # What a Mini does: authenticates, then refuses.
                    self._refuse(sender, code=0xFFF9)
                    continue
                self.sweeping = True
                self.sweeps += 1
                self.sock.sendto(self._reply(bytearray(16)), sender)
            elif verb == self.bl.DATA_CHECK_RF_FOUND:
                if not self.sweeping:
                    self._refuse(sender, code=0xFFF9)
                    continue
                # A real device answers zero until it locks on, rather than
                # erroring. `holds_before_lock` is how many of those to give.
                payload = bytearray(16)
                if self.holds_before_lock > 0:
                    self.holds_before_lock -= 1
                else:
                    payload[0] = 1
                self.sock.sendto(self._reply(payload), sender)
            elif verb == self.bl.DATA_LEARN_RF_CODE:
                self.learning = True
                self.sock.sendto(self._reply(bytearray(16)), sender)
            elif verb == self.bl.DATA_CANCEL_RF_SWEEP:
                self.sweeping = False
                self.cancels += 1
                self.sock.sendto(self._reply(bytearray(16)), sender)


class TestBroadlinkProtocol(unittest.TestCase):
    """The RM client, against a device speaking the real wire format."""

    def setUp(self):
        self.device = FakeRM()
        self.addCleanup(self.device.close)
        import broadlink_lan as bl
        self.bl = bl
        # The fake listens on an ephemeral port; the real one is always 80.
        self._saved_port = bl.DEVICE_PORT
        bl.DEVICE_PORT = self.device.port

    def tearDown(self):
        self.bl.DEVICE_PORT = self._saved_port

    def session(self):
        session = self.bl.Session('127.0.0.1', self.device.mac,
                                  self.device.devtype, timeout=2.0)
        session.authenticate()
        return session

    def test_checksum_is_seeded_at_beaf(self):
        self.assertEqual(self.bl.checksum(b''), 0xBEAF)
        self.assertEqual(self.bl.checksum(b'\x01'), 0xBEB0)
        # Wraps at 16 bits rather than growing.
        self.assertEqual(self.bl.checksum(b'\xff' * 1000),
                         (0xBEAF + 255 * 1000) & 0xFFFF)

    def test_error_codes_are_decoded_as_signed(self):
        """0xffff is -1, which is what every reference calls it."""
        self.assertIn('Authentication failed', self.bl.error_text(0xFFFF))
        self.assertIn('-1', self.bl.error_text(0xFFFF))
        self.assertIn('You have been logged out', self.bl.error_text(0xFFFE))
        self.assertIn('Control key is expired', self.bl.error_text(0xFFF7))

    def test_an_unnamed_code_still_shows_its_signed_value(self):
        text = self.bl.error_text(0xFFF9)
        self.assertIn('-7', text)
        self.assertIn('0xfff9', text)

    def test_auth_errors_are_recognised_as_such(self):
        self.assertTrue(self.bl.is_auth_error(0xFFFF))
        self.assertTrue(self.bl.is_auth_error(0xFFF7))
        self.assertFalse(self.bl.is_auth_error(0xFFF9))
        self.assertFalse(self.bl.is_auth_error(0x000B))

    def test_an_auth_error_carries_the_unlock_advice(self):
        """The number alone tells the user nothing they can act on."""
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        reply = bytearray(0x38)
        reply[0x22] = 0xFF
        reply[0x23] = 0xFF

        with self.assertRaises(self.bl.BroadlinkError) as caught:
            session.parse_response(reply)

        message = str(caught.exception)
        self.assertIn('Authentication failed', message)
        self.assertIn('Lock device', message)

    def test_a_non_auth_error_does_not_blame_the_app_lock(self):
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        reply = bytearray(0x38)
        reply[0x22] = 0x0B

        with self.assertRaises(self.bl.BroadlinkError) as caught:
            session.parse_response(reply)
        self.assertNotIn('Lock device', str(caught.exception))

    def test_hello_is_48_bytes_with_a_valid_checksum(self):
        packet = bytearray(self.bl.build_hello('192.168.1.50', 4321))
        self.assertEqual(len(packet), 0x30)

        stored = packet[0x20] | (packet[0x21] << 8)
        packet[0x20] = packet[0x21] = 0
        self.assertEqual(self.bl.checksum(packet), stored)

    def test_hello_carries_the_local_address_and_port(self):
        packet = bytearray(self.bl.build_hello('192.168.1.50', 0x1234))
        self.assertEqual(list(packet[0x18:0x1c]), [192, 168, 1, 50])
        self.assertEqual(packet[0x1c], 0x34)
        self.assertEqual(packet[0x1d], 0x12)

    def test_a_packet_carries_the_magic_header_and_both_checksums(self):
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        packet = bytearray(session.build_packet(self.bl.CMD_DATA,
                                                bytearray([2, 0, 0, 0])))

        self.assertEqual(list(packet[0:4]), [0x5A, 0xA5, 0xAA, 0x55])
        self.assertEqual(packet[0x26], self.bl.CMD_DATA & 0xFF)
        stored = packet[0x20] | (packet[0x21] << 8)
        packet[0x20] = packet[0x21] = 0
        self.assertEqual(self.bl.checksum(packet), stored)

    def test_the_counter_advances_between_packets(self):
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        first = bytearray(session.build_packet(self.bl.CMD_DATA, b'\x02'))
        second = bytearray(session.build_packet(self.bl.CMD_DATA, b'\x02'))
        self.assertNotEqual(first[0x28:0x2a], second[0x28:0x2a])

    def test_discovery_finds_the_device(self):
        transport = self.bl.BroadlinkTransport(timeout=2.0)
        # The fake is bound to loopback, which a real 255.255.255.255
        # broadcast would never reach, so the hello is aimed there instead.
        saved = (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT)
        self.bl.BROADCAST_ADDRESS = '127.0.0.1'
        self.bl.BROADCAST_PORT = self.device.port
        try:
            found = [d for d in transport.discover(
                timeout=1.5, local_addresses=['127.0.0.1'])
                if d['mac'].startswith('FF:EE')]
        finally:
            (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT) = saved

        self.assertTrue(found, 'no Broadlink device discovered')
        self.assertEqual(found[0]['devtype'], 0x27c2)
        self.assertEqual(found[0]['label'], 'RM Mini 3')
        self.assertEqual(found[0]['name'], 'Lounge RM')

    def test_authentication_swaps_in_the_session_key(self):
        session = self.session()

        self.assertTrue(session.authenticated)
        self.assertEqual(bytearray(session.key), self.device.session_key)
        self.assertEqual(bytearray(session.device_id), self.device.device_id)
        self.assertNotEqual(bytearray(session.key),
                            bytearray(self.bl.INITIAL_KEY))

    def test_sending_a_code_reaches_the_device_intact(self):
        session = self.session()
        code = b'\x26\x00\x20\x00' + b'\x33' * 28

        session.send_code(code)
        self.assertEqual(len(self.device.sent_codes), 1)
        self.assertTrue(self.device.sent_codes[0].startswith(code))

    def test_learning_returns_the_captured_code(self):
        session = self.session()

        self.assertIsNone(session.check_learned())   # nothing pressed yet
        session.enter_learning()
        learned = session.check_learned()

        self.assertIsNotNone(learned)
        self.assertTrue(learned.startswith(b'\x26\x00'))

    def test_an_rf_code_is_learned_in_two_passes(self):
        """Sweep for the frequency, then listen on it. The code that comes
        back reads through the same check as infrared."""
        session = self.session()

        self.assertTrue(session.sweep_frequency())
        self.assertTrue(session.check_frequency())
        self.assertTrue(session.find_rf_packet())
        learned = session.check_learned()

        self.assertIsNotNone(learned)
        self.assertTrue(learned.startswith(b'\x26\x00'))

    def test_the_sweep_answers_not_yet_rather_than_erroring(self):
        """Unlike the infrared check, which errors while it waits."""
        self.device.holds_before_lock = 2
        session = self.session()
        session.sweep_frequency()

        self.assertFalse(session.check_frequency())
        self.assertFalse(session.check_frequency())
        self.assertTrue(session.check_frequency())

    def test_a_blaster_with_no_radio_refuses_the_sweep(self):
        """What a Mini does: authenticates fine, then will not sweep."""
        self.device.has_radio = False
        session = self.session()

        self.assertRaises(self.bl.BroadlinkError, session.sweep_frequency)

    def test_cancelling_the_sweep_reaches_the_device(self):
        """A blaster left sweeping stays deaf to ordinary commands."""
        session = self.session()
        session.sweep_frequency()
        self.assertTrue(self.device.sweeping)

        session.cancel_sweep()

        self.assertFalse(self.device.sweeping)
        self.assertEqual(self.device.cancels, 1)

    def test_new_framing_is_length_prefixed(self):
        """<H length> + <I verb> + data, length covering verb and data."""
        import struct as _struct

        session = self.bl.Session('10.0.0.1', b'\x01' * 6, 0x5f36)
        self.assertTrue(session.new_framing)

        payload = bytearray(session.build_data_payload(self.bl.DATA_SEND,
                                                       b'\xAA\xBB'))
        self.assertEqual(_struct.unpack('<H', bytes(payload[0:2]))[0], 6)
        self.assertEqual(payload[2], self.bl.DATA_SEND)
        self.assertEqual(bytes(payload[6:]), b'\xAA\xBB')

    def test_old_framing_has_no_prefix(self):
        session = self.bl.Session('10.0.0.1', b'\x01' * 6, 0x2712)
        self.assertFalse(session.new_framing)

        payload = bytearray(session.build_data_payload(self.bl.DATA_SEND,
                                                       b'\xAA\xBB'))
        self.assertEqual(payload[0], self.bl.DATA_SEND)
        self.assertEqual(bytes(payload[4:]), b'\xAA\xBB')

    def test_the_framing_guess_comes_from_the_device_type(self):
        self.assertIn(0x5f36, self.bl.NEW_FRAMING_TYPES)
        self.assertNotIn(0x2712, self.bl.NEW_FRAMING_TYPES)


class TestBroadlinkNewFraming(unittest.TestCase):
    """A device wanting the newer payload layout, e.g. a later RM Mini 3."""

    def setUp(self):
        # devtype 0x27c2 is on the old list, but this unit wants the new
        # layout -- exactly the mismatch that hardware sold as "RM Mini 3"
        # produces, and the reason the guess has to be correctable.
        self.device = FakeRM(devtype=0x27c2, name='Bedroom RM',
                             new_framing=True)
        self.addCleanup(self.device.close)
        import broadlink_lan as bl
        self.bl = bl
        self._saved = bl.DEVICE_PORT
        bl.DEVICE_PORT = self.device.port

    def tearDown(self):
        self.bl.DEVICE_PORT = self._saved

    def session(self):
        session = self.bl.Session('127.0.0.1', self.device.mac,
                                  self.device.devtype, timeout=2.0)
        session.authenticate()
        return session

    def test_authentication_succeeds_even_with_the_wrong_framing_guess(self):
        """Auth is layout-independent, which is why only commands failed."""
        session = self.session()
        self.assertTrue(session.authenticated)
        self.assertFalse(session.new_framing)   # guessed wrong, on purpose

    def test_a_command_corrects_the_framing_and_succeeds(self):
        session = self.session()
        code = b'\x26\x00\x10\x00' + b'\x44' * 12

        session.send_code(code)

        self.assertTrue(session.new_framing, 'framing was not corrected')
        self.assertEqual(len(self.device.sent_codes), 1)
        self.assertTrue(self.device.sent_codes[0].startswith(code))

    def test_the_correction_sticks_for_later_commands(self):
        session = self.session()
        session.send_code(b'\x26\x00\x04\x00')
        session.send_code(b'\x26\x00\x04\x00')
        self.assertEqual(len(self.device.sent_codes), 2)

    def test_learning_works_once_the_framing_is_right(self):
        session = self.session()
        session.enter_learning()
        learned = session.check_learned()

        self.assertIsNotNone(learned)
        self.assertTrue(learned.startswith(b'\x26\x00'))

    def test_a_genuinely_broken_command_reports_the_original_error(self):
        """Retrying the other way must not disguise a real failure."""
        session = self.session()
        session.new_framing = True          # already correct

        # Neither layout will satisfy a device that is simply refusing.
        self.device.refuse_all = True
        with self.assertRaises(self.bl.BroadlinkError) as caught:
            session.send_code(b'\x26\x00')

        # The error reported is the first one, from the layout we believed in,
        # not whatever the speculative retry produced.
        self.assertIn('-13', str(caught.exception))
        self.assertTrue(session.new_framing, 'framing flipped on a real error')

    def test_a_device_error_is_raised_not_swallowed(self):
        session = self.session()
        error = bytearray(0x38)
        error[0x22] = 0x0B
        with self.assertRaises(self.bl.BroadlinkError):
            session.parse_response(error)

    def test_a_short_reply_is_rejected(self):
        session = self.session()
        with self.assertRaises(self.bl.BroadlinkError):
            session.parse_response(bytearray(4))

    def test_an_unreachable_device_reports_clearly(self):
        session = self.bl.Session('127.0.0.1', self.device.mac,
                                  self.device.devtype, timeout=0.3)
        saved = self.bl.DEVICE_PORT
        self.bl.DEVICE_PORT = 1        # nothing listening
        try:
            with self.assertRaises(self.bl.BroadlinkError) as caught:
                session.authenticate()
        finally:
            self.bl.DEVICE_PORT = saved
        self.assertIn('127.0.0.1', str(caught.exception))

    def test_unknown_device_types_still_get_a_label(self):
        self.assertEqual(self.bl.device_label(0x27c2), 'RM Mini 3')
        self.assertEqual(self.bl.device_label(0xABCD), 'Broadlink abcd')


class TestBroadlinkDriver(unittest.TestCase):
    """The driver, against the fake RM speaking the real protocol."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'broadlink_driver',
                     'broadlink_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]

        self.rm = FakeRM()
        self.addCleanup(self.rm.close)
        import broadlink_lan as bl
        self.bl = bl
        self._saved = bl.DEVICE_PORT
        bl.DEVICE_PORT = self.rm.port
        self.addCleanup(self._restore)

    def _restore(self):
        self.bl.DEVICE_PORT = self._saved
        clean_profile()

    def driver(self, codes=None):
        from broadlink_driver import BroadlinkDriver

        self.saved = []
        return BroadlinkDriver(
            transport=self.bl.BroadlinkTransport(timeout=2.0),
            codes=codes if codes is not None else {},
            save_codes=lambda: self.saved.append(True))

    def device(self):
        return Device('FF:EE:DD:CC:BB:AA', name='Lounge RM',
                      driver='broadlink', ip='127.0.0.1', lan=True,
                      devtype=0x27c2)

    def test_an_rm_claims_commands_and_nothing_else(self):
        from devices import CAP_COLOR, CAP_COMMANDS, CAP_POWER

        caps = self.driver().capabilities(self.device())
        self.assertEqual(caps, set([CAP_COMMANDS]))
        self.assertNotIn(CAP_POWER, caps)
        self.assertNotIn(CAP_COLOR, caps)

    def test_the_state_verbs_refuse_rather_than_pretend(self):
        driver, device = self.driver(), self.device()
        for call in (lambda: driver.turn(device, True),
                     lambda: driver.set_brightness(device, 50),
                     lambda: driver.set_color(device, 1, 2, 3),
                     lambda: driver.set_color_temp(device, 2700)):
            self.assertRaises(ControlError, call)
        self.assertIsNone(driver.get_state(device))

    def test_learning_captures_a_code_and_saves_it(self):
        driver, device = self.driver(), self.device()

        driver.start_learning(device)
        code = driver.collect_learned(device)
        self.assertIsNotNone(code)
        self.assertTrue(code.startswith('2600'))

        self.assertTrue(driver.save_command(device, 'AVR Power', code))
        self.assertEqual(driver.commands(device), ['AVR Power'])
        self.assertTrue(self.saved, 'codes were not persisted')

    def test_an_rf_code_is_learned_and_saved_like_any_other(self):
        """The point of the whole thing: what comes out is an ordinary
        command, so scenes, sequences and the remote need no change."""
        driver, device = self.driver(), self.device()

        driver.start_rf_sweep(device)
        self.assertTrue(driver.rf_frequency_found(device))
        driver.start_rf_capture(device)
        code = driver.collect_learned(device)

        self.assertIsNotNone(code)
        self.assertTrue(driver.save_command(device, 'Blinds Open', code))
        self.assertEqual(driver.commands(device), ['Blinds Open'])

    def test_a_sweep_still_looking_reads_as_not_found(self):
        driver, device = self.driver(), self.device()
        self.rm.holds_before_lock = 1

        driver.start_rf_sweep(device)
        self.assertFalse(driver.rf_frequency_found(device))
        self.assertTrue(driver.rf_frequency_found(device))

    def test_a_mini_is_told_it_has_no_radio_rather_than_erroring(self):
        """The failure people will actually hit, so it says which it is."""
        driver, device = self.driver(), self.device()
        self.rm.has_radio = False

        try:
            driver.start_rf_sweep(device)
        except ControlError as exc:
            self.assertIn('Pro', str(exc))
            self.assertIn('Mini', str(exc))
        else:
            self.fail('a blaster with no radio swept anyway')

    def test_cancelling_a_sweep_never_raises(self):
        """It runs while something has already gone wrong; a throw here
        would replace the message explaining that."""
        driver, device = self.driver(), self.device()
        driver.start_rf_sweep(device)
        self.assertTrue(driver.cancel_rf_sweep(device))

        device_gone = Device('11:22:33:44:55:66', name='Unplugged',
                             driver='broadlink', ip='127.0.0.9', lan=True,
                             devtype=0x27c2)
        self.assertFalse(driver.cancel_rf_sweep(device_gone))

    def test_nothing_learned_yet_reads_as_still_waiting(self):
        driver, device = self.driver(), self.device()
        self.assertIsNone(driver.collect_learned(device))

    def test_a_learned_code_can_be_fired_back(self):
        driver, device = self.driver(), self.device()

        driver.start_learning(device)
        driver.save_command(device, 'AVR Power',
                            driver.collect_learned(device))
        driver.send_command(device, 'AVR Power')

        self.assertEqual(len(self.rm.sent_codes), 1)
        self.assertTrue(self.rm.sent_codes[0].startswith(b'\x26\x00'))

    def test_test_connection_reports_success(self):
        driver, device = self.driver(), self.device()
        ok, message = driver.test_connection(device)
        self.assertTrue(ok)
        self.assertIn('Lounge RM', message)

    def test_test_connection_explains_a_locked_device(self):
        """The RM Mini 3 case: discovered fine, refuses the handshake."""
        from broadlink_driver import BroadlinkDriver

        class Locked(object):
            def session(self, ip, mac, devtype):
                raise self_bl.BroadlinkError(
                    'Could not authenticate with %s.\n\n%s: %s\n\n%s'
                    % (ip, ip, self_bl.error_text(0xFFFF),
                       self_bl.AUTH_ADVICE))

            def forget_session(self, ip):
                pass

        self_bl = self.bl
        driver = BroadlinkDriver(transport=Locked())
        ok, message = driver.test_connection(self.device())

        self.assertFalse(ok)
        self.assertIn('Authentication failed', message)
        self.assertIn('Lock device', message)

    def test_an_unknown_command_is_refused_clearly(self):
        driver, device = self.driver(), self.device()
        with self.assertRaises(ControlError) as caught:
            driver.send_command(device, 'Nope')
        self.assertIn('Nope', str(caught.exception))

    def test_a_corrupt_saved_code_is_reported_not_sent(self):
        driver = self.driver(codes={'FF:EE:DD:CC:BB:AA':
                                    {'Bad': 'not hex at all'}})
        with self.assertRaises(ControlError):
            driver.send_command(self.device(), 'Bad')
        self.assertEqual(self.rm.sent_codes, [])

    def test_forgetting_a_command_persists(self):
        driver, device = self.driver(codes={'FF:EE:DD:CC:BB:AA':
                                            {'Old': '2600'}}), self.device()
        self.assertTrue(driver.forget_command(device, 'Old'))
        self.assertEqual(driver.commands(device), [])
        self.assertFalse(driver.forget_command(device, 'Old'))

    def test_mac_bytes_are_rebuilt_from_the_device_id_after_a_restart(self):
        """Nothing but device_id and devtype survives to disk."""
        driver = self.driver()
        restored = Device.from_dict(self.device().to_dict())
        self.assertFalse(hasattr(restored, 'mac_bytes'))
        self.assertEqual(restored.devtype, 0x27c2)

        session = driver._session(restored)
        self.assertEqual(bytearray(session.mac), self.rm.mac)

    def test_a_scene_action_fires_a_learned_code(self):
        """End to end: a scene reaching real protocol code."""
        import hub as hub_mod
        import scenes as fresh_scenes

        driver, device = self.driver(), self.device()
        driver.start_learning(device)
        driver.save_command(device, 'AVR Power',
                            driver.collect_learned(device))

        hub = hub_mod.Hub(drivers=[driver])
        scene = fresh_scenes.make_scene(
            'Movie Night', targets=['NOBODY'],
            actions=[{'device': 'FF:EE:DD:CC:BB:AA',
                      'command': 'AVR Power'}])

        _applied, errors = fresh_scenes.apply_scene(hub, scene, [device])
        self.assertEqual(errors, [])
        self.assertEqual(len(self.rm.sent_codes), 1)

    def test_discovery_builds_devices_the_registry_can_store(self):
        driver = self.driver()
        saved = (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT)
        self.bl.BROADCAST_ADDRESS = '127.0.0.1'
        self.bl.BROADCAST_PORT = self.rm.port
        try:
            found, warnings = driver.discover(timeout=1.5)
        finally:
            (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT) = saved

        self.assertEqual(warnings, [])
        rm = [d for d in found if d.device_id.startswith('FF:EE')]
        self.assertTrue(rm, 'RM not discovered')
        self.assertEqual(rm[0].driver, 'broadlink')
        self.assertEqual(rm[0].model, 'RM Mini 3')
        self.assertEqual(rm[0].devtype, 0x27c2)

        # And it round-trips through the device cache unchanged.
        restored = Device.from_dict(json.loads(json.dumps(rm[0].to_dict())))
        self.assertEqual(restored.driver, 'broadlink')
        self.assertEqual(restored.devtype, 0x27c2)

    def test_a_failed_search_becomes_a_warning_not_an_exception(self):
        from broadlink_driver import BroadlinkDriver

        class Broken(object):
            def discover(self, timeout=3.0):
                raise RuntimeError('no network')

        found, warnings = BroadlinkDriver(transport=Broken()).discover()
        self.assertEqual(found, [])
        self.assertTrue(any('no network' in w for w in warnings))


class TestTuyaDiscovery(unittest.TestCase):
    """Finding Tuya plugs, which announce themselves rather than answer."""

    @staticmethod
    def frame(payload, return_code=False):
        import struct as _struct
        import zlib as _zlib
        import tuya_lan as t

        if return_code:
            head = (_struct.pack('>4I', t.PREFIX, 0, 0, len(payload) + 12)
                    + _struct.pack('>I', 0))
        else:
            head = _struct.pack('>4I', t.PREFIX, 0, 0, len(payload) + 8)
        body = head + payload
        return (body + _struct.pack('>I', _zlib.crc32(body) & 0xFFFFFFFF)
                + _struct.pack('>I', t.SUFFIX))

    def test_a_clear_31_broadcast_is_understood(self):
        import tuya_lan as t

        payload = json.dumps({'gwId': 'wp9abc', 'ip': '10.0.0.55',
                              'version': '3.1'}).encode()
        for return_code in (False, True):
            found = t.parse_broadcast(self.frame(payload, return_code))
            self.assertEqual(found['device_id'], 'wp9abc')
            self.assertEqual(found['ip'], '10.0.0.55')
            self.assertEqual(found['version'], '3.1')

    def test_an_encrypted_33_broadcast_is_understood(self):
        import tuya_lan as t
        from aes import AESECB

        payload = AESECB(t.DISCOVERY_KEY).encrypt(json.dumps(
            {'gwId': 'wp9xyz', 'ip': '10.0.0.71',
             'version': '3.3'}).encode())
        for return_code in (False, True):
            found = t.parse_broadcast(self.frame(payload, return_code))
            self.assertEqual(found['device_id'], 'wp9xyz')
            self.assertEqual(found['version'], '3.3')

    def test_both_payload_offsets_are_tried(self):
        """Neither offset is announced, so guessing one would find nothing."""
        import tuya_lan as t

        payload = json.dumps({'gwId': 'a', 'ip': '1.2.3.4'}).encode()
        self.assertIsNotNone(t.parse_broadcast(self.frame(payload, False)))
        self.assertIsNotNone(t.parse_broadcast(self.frame(payload, True)))

    def test_rubbish_is_ignored(self):
        import tuya_lan as t

        for junk in (b'', b'short', b'garbage' * 10,
                     self.frame(b'not json at all')):
            self.assertIsNone(t.parse_broadcast(junk))

    def test_a_broadcast_without_an_id_is_ignored(self):
        import tuya_lan as t

        payload = json.dumps({'ip': '10.0.0.9'}).encode()
        self.assertIsNone(t.parse_broadcast(self.frame(payload)))

    def test_devid_is_accepted_as_well_as_gwid(self):
        import tuya_lan as t

        payload = json.dumps({'devId': 'alt', 'ip': '10.0.0.9'}).encode()
        self.assertEqual(t.parse_broadcast(self.frame(payload))['device_id'],
                         'alt')


class TestTuyaDiagnostics(unittest.TestCase):
    """Silence has several causes and they need different answers."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'diagnostics', 'tuya_lan'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    @staticmethod
    def report(**overrides):
        base = {'ports': {6666: 'listening', 6667: 'listening'},
                'raw': [], 'devices': [], 'listened': 8.0,
                'other_traffic': 0}
        base.update(overrides)
        return base

    def test_devices_heard_are_listed_with_their_protocol(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report(devices=[
            {'device_id': 'wp9abc', 'ip': '10.0.0.55', 'version': '3.3'}]))
        self.assertIn('wp9abc', text)
        self.assertIn('10.0.0.55', text)
        self.assertIn('3.3', text)

    def test_both_ports_blocked_blames_another_program(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report(
            ports={6666: 'could not bind: in use',
                   6667: 'could not bind: in use'}))
        self.assertIn('holding them', text)

    def test_silence_lists_the_causes_in_order(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report())
        self.assertIn('firewall', text)
        self.assertIn('different network', text)
        self.assertIn('GHome app', text)

    def test_traffic_that_is_not_tuya_is_its_own_verdict(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report(other_traffic=4))
        self.assertIn('none was a Tuya', text)

    def test_the_log_carries_raw_bytes_for_anything_unrecognised(self):
        import diagnostics

        lines = '\n'.join(diagnostics.tuya_lines(self.report(
            other_traffic=1,
            raw=[{'port': 6667, 'from': '10.0.0.55', 'bytes': 40,
                  'hex': 'deadbeef', 'parsed': False}])))
        self.assertIn('deadbeef', lines)
        self.assertIn('10.0.0.55', lines)
        self.assertIn('UDP 6666', lines)

    def test_a_probe_with_no_devices_still_reports_its_ports(self):
        import tuya_lan

        report = tuya_lan.probe(timeout=1.0)
        self.assertEqual(sorted(report['ports'].keys()), [6666, 6667])
        self.assertEqual(report['devices'], [])


class FakeTuyaPlug(object):
    """A Tuya plug that speaks the real wire protocol over loopback TCP.

    It is strict on purpose. The 3.3 rule that a status query carries no
    version header while a control does is the single easiest thing to get
    wrong in this protocol, and a lenient fake would accept both and prove
    nothing -- so a request framed the wrong way is answered with the same
    error a real device gives.
    """

    def __init__(self, key=b'0123456789abcdef', version='3.3', dps=None,
                 header_on_status_reply=True, envelope_replies=False):
        import tuya_lan

        self.tuya_lan = tuya_lan
        self.key = key
        self.version = version
        self.dps = dict(dps or {'1': False})
        self.header_on_status_reply = header_on_status_reply
        self.envelope_replies = envelope_replies
        self.requests = []
        self.connections = 0
        self.refuse = None
        self.negotiations = 0
        self.session_key = None
        self.crashed = None
        self.remote_nonce = None

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(5)
        self.server.settimeout(0.3)
        self.port = self.server.getsockname()[1]

        self.running = True
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self.running = False
        self.thread.join(2.0)
        self.server.close()

    def _serve(self):
        while self.running:
            try:
                conn, _addr = self.server.accept()
            except (socket.timeout, socket.error):
                continue
            self.connections += 1
            try:
                conn.settimeout(1.0)
                self.session_key = None
                # 3.4 negotiates a key before every command, so a connection
                # is a conversation rather than a single request -- and the
                # client is free to put two of its turns in one segment, so
                # this frames by declared length rather than by read.
                buffer = b''
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buffer += data
                    while len(buffer) >= 16:
                        length = struct.unpack('>I', buffer[12:16])[0]
                        if len(buffer) < 16 + length:
                            break
                        packet, buffer = (buffer[:16 + length],
                                          buffer[16 + length:])
                        reply = self._handle(packet)
                        if reply:
                            conn.sendall(reply)
            except socket.error:
                pass
            except Exception as exc:  # surfaced by the test, not swallowed
                self.crashed = exc
            finally:
                try:
                    conn.close()
                except socket.error:
                    pass

    # -- protocol ----------------------------------------------------------

    @property
    def is_v34(self):
        return self.version.startswith('3.4')

    @property
    def tail(self):
        return 36 if self.is_v34 else 8

    def _cipher(self, key=None):
        from aes import AESECB

        return AESECB(key or self.key)

    def _handle(self, data):
        prefix, sequence, command, length = struct.unpack('>4I', data[0:16])
        assert prefix == self.tuya_lan.PREFIX
        payload = data[16:16 + length - self.tail]
        self.requests.append((command, payload))

        if self.is_v34:
            handshake = self._handshake(sequence, command, payload)
            if handshake is not None:
                return handshake

        if self.refuse is not None:
            return self._packet(sequence, command, b'device busy',
                                retcode=self.refuse)

        try:
            body = self._decode_request(command, payload)
        except ValueError:
            # A real device encrypts its error under its own key, so a client
            # whose key is wrong cannot read the reason either. Returning
            # plain text here would hand the client a legibility it does not
            # have on the wire.
            return self._packet(sequence, command,
                                self._encode_reply({'err': 'decrypt failed'}),
                                retcode=1)

        if command in (self.tuya_lan.CMD_STATUS, self.tuya_lan.CMD_STATUS_NEW):
            return self._packet(sequence, command,
                                self._encode_reply(self._reading()))

        dps = body.get('dps')
        if dps is None:
            dps = (body.get('data') or {}).get('dps') or {}
        for key, value in dps.items():
            self.dps[str(key)] = value
        return self._packet(sequence, command, b'')

    def _reading(self):
        body = {'dps': dict(self.dps)}
        if self.envelope_replies:
            return {'protocol': 4, 't': 1700000000, 'data': body}
        return body

    # -- 3.4 key negotiation ----------------------------------------------

    def _handshake(self, sequence, command, payload):
        """The three steps 3.4 requires before it will hear a command."""
        if command == self.tuya_lan.CMD_SESS_KEY_NEG_START:
            self.negotiations += 1
            self.local_nonce = self._cipher().decrypt(payload, unpad=False)[:16]
            self.remote_nonce = bytes(bytearray(range(0x40, 0x50)))
            proof = hmac.new(self.key, self.local_nonce,
                             hashlib.sha256).digest()
            # Deliberately without a return code: real 3.4 devices omit it
            # here and nothing in the header says so, which is exactly what
            # the client has to cope with.
            return self._packet(
                sequence, self.tuya_lan.CMD_SESS_KEY_NEG_RESP,
                self._cipher().encrypt(self.remote_nonce + proof),
                retcode=None)

        if command == self.tuya_lan.CMD_SESS_KEY_NEG_FINISH:
            given = self._cipher().decrypt(payload, unpad=False)[:32]
            expected = hmac.new(self.key, self.remote_nonce,
                                hashlib.sha256).digest()
            assert given == expected, 'client failed to prove it holds the key'
            mixed = bytearray(self.local_nonce)
            for index, byte in enumerate(bytearray(self.remote_nonce)):
                mixed[index] ^= byte
            self.session_key = self._cipher().encrypt(bytes(mixed), pad=False)
            return b''
        return None

    # -- payloads ----------------------------------------------------------

    def _decode_request(self, command, payload):
        if self.is_v34:
            assert self.session_key, 'command sent before the key was agreed'
            plain = self._cipher(self.session_key).decrypt(payload)
            headerless = command in self.tuya_lan.V34_HEADERLESS
            if headerless and plain[:3] == b'3.4':
                raise ValueError('a 3.4 status query carries no header')
            if not headerless:
                if plain[:3] != b'3.4':
                    raise ValueError('a 3.4 control needs the header')
                plain = plain[15:]
            return json.loads(plain.decode('utf-8'))

        if self.version.startswith('3.1'):
            if command == self.tuya_lan.CMD_STATUS:
                if payload[:3] == b'3.1':
                    raise ValueError('3.1 status queries are sent in clear')
                return json.loads(payload.decode('utf-8'))
            if payload[:3] != b'3.1':
                raise ValueError('3.1 controls need the version header')
            signature, encoded = payload[3:19], payload[19:]
            expected = hashlib.md5(
                b'data=' + encoded + b'||lpv=3.1||' + self.key
            ).hexdigest()[8:24].encode('utf-8')
            if signature != expected:
                raise ValueError('bad signature')
            return json.loads(
                self._cipher().decrypt(base64.b64decode(encoded))
                .decode('utf-8'))

        header = self.version[:3].encode('utf-8')
        if command == self.tuya_lan.CMD_STATUS:
            if payload[:3] == header:
                raise ValueError('status queries carry no version header')
        else:
            if payload[:3] != header:
                raise ValueError('controls need the version header')
            payload = payload[15:]
        return json.loads(self._cipher().decrypt(payload).decode('utf-8'))

    def _encode_reply(self, body):
        raw = json.dumps(body).encode('utf-8')
        if self.is_v34:
            return self._cipher(self.session_key).encrypt(
                b'3.4' + (b'\x00' * 12) + raw)
        if self.version.startswith('3.1'):
            encoded = base64.b64encode(self._cipher().encrypt(raw))
            signature = hashlib.md5(
                b'data=' + encoded + b'||lpv=3.1||' + self.key
            ).hexdigest()[8:24].encode('utf-8')
            return b'3.1' + signature + encoded
        encrypted = self._cipher().encrypt(raw)
        if not self.header_on_status_reply:
            return encrypted
        return self.version[:3].encode('utf-8') + (b'\x00' * 12) + encrypted

    def _packet(self, sequence, command, payload, retcode=0):
        """Frame a reply. retcode=None omits it, as 3.4 does mid-handshake."""
        head = bytearray()
        if retcode is not None:
            head.extend(struct.pack('>I', retcode))
        head.extend(payload)

        body = bytearray(struct.pack('>4I', self.tuya_lan.PREFIX, sequence,
                                     command, len(head) + self.tail))
        body.extend(head)
        if self.is_v34:
            key = self.session_key or self.key
            body.extend(hmac.new(key, bytes(body), hashlib.sha256).digest())
        else:
            body.extend(struct.pack(
                '>I', zlib.crc32(bytes(body)) & 0xFFFFFFFF))
        body.extend(struct.pack('>I', self.tuya_lan.SUFFIX))
        return bytes(body)


class TestTuyaDriver(unittest.TestCase):
    """A plug is discoverable long before it is controllable."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def driver(self, keys=None):
        from tuya_driver import TuyaDriver

        self.saved = []
        return TuyaDriver(keys=keys if keys is not None else {},
                          save_keys=lambda: self.saved.append(True))

    def device(self):
        return Device('wp9abc', name='Lamp Plug', driver='tuya',
                      ip='10.0.0.55', lan=True, native_id='wp9abc')

    def test_a_plug_claims_power_and_state_only(self):
        from devices import CAP_COLOR, CAP_COMMANDS, CAP_POWER, CAP_STATE

        caps = self.driver().capabilities(self.device())
        self.assertEqual(caps, set([CAP_POWER, CAP_STATE]))
        self.assertNotIn(CAP_COLOR, caps)
        self.assertNotIn(CAP_COMMANDS, caps)

    def test_a_device_without_a_key_says_so_rather_than_looking_broken(self):
        driver, device = self.driver(), self.device()
        self.assertTrue(driver.needs_key(device))

        with self.assertRaises(ControlError) as caught:
            driver.turn(device, True)
        self.assertIn(device.name, str(caught.exception))

    def test_a_key_must_be_sixteen_characters(self):
        driver, device = self.driver(), self.device()

        self.assertFalse(driver.set_local_key(device, 'too short'))
        self.assertTrue(driver.needs_key(device))

        self.assertTrue(driver.set_local_key(device, 'a' * 16))
        self.assertFalse(driver.needs_key(device))
        self.assertTrue(self.saved, 'key was not persisted')

    def test_the_tuya_id_keeps_its_case(self):
        """Upper-casing a Tuya id would produce one the device disowns."""
        device = self.device()
        self.assertEqual(device.device_id, 'WP9ABC')   # for matching
        self.assertEqual(device.native_id, 'wp9abc')   # for the wire

        restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))
        self.assertEqual(restored.native_id, 'wp9abc')

    def test_keys_are_stored_under_the_tuya_id(self):
        driver, device = self.driver(), self.device()
        driver.set_local_key(device, 'k' * 16)
        self.assertIn('wp9abc', driver.keys)

    def test_a_key_can_be_cleared(self):
        driver, device = self.driver(keys={'wp9abc': 'a' * 16}), self.device()
        self.assertFalse(driver.needs_key(device))
        self.assertTrue(driver.set_local_key(device, ''))
        self.assertTrue(driver.needs_key(device))

    def test_discovery_warns_about_unkeyed_devices(self):
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        heard = [{'device_id': 'wp9abc', 'ip': '10.0.0.55', 'version': '3.3'}]
        saved_discover = t.discover
        t.discover = lambda timeout=6.0, log_func=None: heard
        try:
            found, warnings = TuyaDriver(keys={}).discover()
        finally:
            t.discover = saved_discover

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].driver, 'tuya')
        self.assertEqual(found[0].device_id, 'WP9ABC')
        self.assertTrue(any('local key' in w for w in warnings))

    def test_a_keyed_device_produces_no_warning(self):
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        heard = [{'device_id': 'wp9abc', 'ip': '10.0.0.55', 'version': '3.3'}]
        saved_discover = t.discover
        t.discover = lambda timeout=6.0, log_func=None: heard
        try:
            _found, warnings = TuyaDriver(
                keys={'wp9abc': 'k' * 16}).discover()
        finally:
            t.discover = saved_discover
        self.assertEqual(warnings, [])

    def test_a_blocked_listen_port_becomes_a_warning(self):
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        saved_discover = t.discover

        def blocked(timeout=6.0, log_func=None):
            raise t.TuyaError('Could not listen on UDP 6666 or 6667.')

        t.discover = blocked
        try:
            found, warnings = TuyaDriver().discover()
        finally:
            t.discover = saved_discover

        self.assertEqual(found, [])
        self.assertTrue(any('6666' in w for w in warnings))

    def test_discovery_listens_longer_than_a_request_response_sweep(self):
        """Tuya devices announce on their own schedule; 3 seconds is not enough."""
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        seen = []
        saved_discover = t.discover
        t.discover = lambda timeout=6.0, log_func=None: seen.append(timeout) or []
        try:
            TuyaDriver().discover(timeout=3.0)
        finally:
            t.discover = saved_discover
        self.assertGreaterEqual(seen[0], 6.0)

    def test_the_session_refuses_without_a_key(self):
        import tuya_lan as t

        session = t.Session('10.0.0.55', 'wp9abc', '', version='3.3')
        self.assertFalse(session.keyed)
        self.assertRaises(t.TuyaKeyMissing, session._cipher)

    def test_packet_framing_carries_the_prefix_and_crc(self):
        import struct as _struct
        import tuya_lan as t
        import zlib as _zlib

        session = t.Session('10.0.0.55', 'wp9abc', 'k' * 16)
        packet = bytearray(session.build_packet(t.CMD_STATUS, b'{}'))

        self.assertEqual(_struct.unpack('>I', bytes(packet[0:4]))[0], t.PREFIX)
        self.assertEqual(_struct.unpack('>I', bytes(packet[-4:]))[0], t.SUFFIX)
        body = bytes(packet[:-8])
        self.assertEqual(_struct.unpack('>I', bytes(packet[-8:-4]))[0],
                         _zlib.crc32(body) & 0xFFFFFFFF)

    def test_a_device_error_reply_is_raised(self):
        import struct as _struct
        import tuya_lan as t

        session = t.Session('10.0.0.55', 'wp9abc', 'k' * 16)
        reply = bytearray(_struct.pack('>4I', t.PREFIX, 1, 10, 0))
        reply.extend(_struct.pack('>I', 1))       # non-zero return code
        reply.extend(b'json obj data unvalid')
        reply.extend(bytearray(8))

        with self.assertRaises(t.TuyaError) as caught:
            session.parse_packet(reply)
        self.assertIn('error 1', str(caught.exception))


class TestTuyaControl(unittest.TestCase):
    """Switching a plug, against a device that speaks the real protocol."""

    KEY = '0123456789abcdef'
    WP9_DPS = {
        '1': False, '2': True, '3': False,   # the three mains outlets
        '7': True,                           # the USB bank
        '9': 0, '10': 0, '11': 0, '15': 0,   # countdown timers
        '38': 'last', '40': False,           # relay memory, child lock
    }

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]
        import tuya_lan

        self.tuya_lan = tuya_lan
        self.real_port = tuya_lan.CONTROL_PORT
        self.plug = None

    def tearDown(self):
        self.tuya_lan.CONTROL_PORT = self.real_port
        if self.plug is not None:
            crashed = self.plug.crashed
            self.plug.close()
            # A fake that died in its thread answers nothing, which surfaces
            # as a timeout and reads like a client bug. Fail on the real
            # cause instead.
            self.assertIsNone(crashed, 'the fake plug crashed: %r' % crashed)
        clean_profile()

    def start(self, **kwargs):
        kwargs.setdefault('key', self.KEY.encode('utf-8'))
        self.plug = FakeTuyaPlug(**kwargs)
        self.tuya_lan.CONTROL_PORT = self.plug.port
        return self.plug

    def driver(self, keys=None):
        from tuya_driver import TuyaDriver

        return TuyaDriver(keys=keys if keys is not None else
                          {'wp9abc': self.KEY}, timeout=3.0)

    def device(self, dp=None, version='3.3'):
        data = {'version': version}
        if dp:
            data['dp'] = dp
        return Device('wp9abc%s' % ('#%s' % dp if dp else ''),
                      name='Office Plug', driver='tuya', ip='127.0.0.1',
                      lan=True, native_id='wp9abc', driver_data=data)

    # -- the wire ----------------------------------------------------------

    def test_a_33_plug_switches_on_and_off(self):
        plug = self.start(dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(), True)
        self.assertTrue(plug.dps['1'])

        driver.turn(self.device(), False)
        self.assertFalse(plug.dps['1'])

    def test_a_31_plug_switches_too(self):
        """3.1 signs its payloads and sends status in clear; 3.3 does neither."""
        plug = self.start(version='3.1', dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(version='3.1'), True)
        self.assertTrue(plug.dps['1'])
        self.assertEqual(driver.get_state(self.device(version='3.1')),
                         {'power': 'on', 'dps': {'1': True}})

    def test_a_status_query_carries_no_version_header_on_33(self):
        """The rule that catches everyone, asserted rather than assumed.

        A 3.3 control needs the version header and a 3.3 status query must not
        have it. Sending the header on a query comes back as a device error,
        which reads exactly like a wrong key and is not one.
        """
        self.start(dps={'1': True})
        driver = self.driver()

        driver.turn(self.device(), False)
        driver.get_state(self.device())

        by_command = dict((command, payload)
                          for command, payload in self.plug.requests)
        self.assertEqual(by_command[self.tuya_lan.CMD_CONTROL][:3], b'3.3')
        self.assertNotEqual(by_command[self.tuya_lan.CMD_STATUS][:3], b'3.3')

    def test_a_reply_without_the_version_header_is_read_too(self):
        """Devices differ on whether a status reply is headered. Both work."""
        self.start(dps={'1': True}, header_on_status_reply=False)

        self.assertEqual(self.driver().get_state(self.device()),
                         {'power': 'on', 'dps': {'1': True}})

    def test_a_wrong_key_is_named_as_the_likely_cause(self):
        """An unreadable reply on this protocol means one thing."""
        self.start(dps={'1': True})
        driver = self.driver(keys={'wp9abc': 'ffffffffffffffff'})

        self.assertIsNone(driver.get_state(self.device()))
        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        self.assertIn('local key', str(caught.exception).lower())

    def test_a_device_error_reaches_the_user_as_words(self):
        plug = self.start()
        plug.refuse = 1
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        message = str(caught.exception)
        self.assertIn('Office Plug', message)
        self.assertIn('device busy', message)

    def test_a_reply_split_across_reads_is_reassembled(self):
        """TCP may cut anywhere; framing is by declared length, not by read."""
        packet = self.tuya_lan.Session(
            '127.0.0.1', 'wp9abc', self.KEY).build_packet(0x0A, b'payload')
        stream = b'\x00\x00' + packet + packet[:9]

        packets, leftover = self.tuya_lan.Session.split_packets(stream)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0], packet)
        self.assertEqual(leftover, packet[:9])

    def test_an_unsupported_version_says_which_and_why(self):
        self.start()
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(version='3.5'), True)
        message = str(caught.exception)
        self.assertIn('3.5', message)
        self.assertIn('GCM', message)

    # -- protocol 3.4 ------------------------------------------------------

    def test_a_34_plug_switches_after_negotiating_a_session_key(self):
        plug = self.start(version='3.4', dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(version='3.4'), True)

        self.assertTrue(plug.dps['1'])
        self.assertEqual(plug.negotiations, 1)

    def test_a_34_status_read_comes_back(self):
        self.start(version='3.4', dps=self.WP9_DPS)

        state = self.driver().get_state(self.device(dp='2', version='3.4'))

        self.assertEqual(state['power'], 'on')

    def test_a_34_reading_wrapped_in_an_envelope_is_unwrapped(self):
        """3.4 sends {"data":{"dps":..}} where 3.3 sends {"dps":..} flat."""
        self.start(version='3.4', dps=self.WP9_DPS, envelope_replies=True)

        state = self.driver().get_state(self.device(dp='3', version='3.4'))

        self.assertEqual(state['power'], 'off')
        self.assertEqual(state['dps']['1'], False)

    def test_a_34_connection_negotiates_every_time(self):
        """The session key belongs to the connection, not to the device."""
        plug = self.start(version='3.4', dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(version='3.4'), True)
        driver.turn(self.device(version='3.4'), False)

        self.assertEqual(plug.negotiations, 2)
        self.assertFalse(plug.dps['1'])

    def test_a_34_session_key_is_not_kept_after_the_connection(self):
        self.start(version='3.4', dps={'1': False})
        session = self.tuya_lan.Session('127.0.0.1', 'wp9abc', self.KEY,
                                        version='3.4')

        session.set_switch(True)

        self.assertIsNone(session.session_key)

    def test_a_34_wrong_key_fails_at_the_handshake_not_five_steps_later(self):
        """The HMAC is a definite answer where an unreadable reply is a guess."""
        self.start(version='3.4', dps={'1': False})
        driver = self.driver(keys={'wp9abc': 'ffffffffffffffff'})

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(version='3.4'), True)
        self.assertIn('local key', str(caught.exception))

    def test_a_34_control_carries_the_header_inside_the_encryption(self):
        """The counter-intuitive one: 3.3 puts it outside, 3.4 puts it inside.

        Asserted from the client's own output rather than trusted, because
        getting this backwards produces a packet the device rejects with an
        error that reads like a wrong key.
        """
        self.start(version='3.4', dps={'1': False})
        session = self.tuya_lan.Session('127.0.0.1', 'wp9abc', self.KEY,
                                        version='3.4')
        session.session_key = self.KEY.encode('utf-8')

        control = session.build_command_payload(
            self.tuya_lan.CMD_CONTROL,
            session._body(self.tuya_lan.CMD_CONTROL, {'1': True}))
        query = session.build_command_payload(
            self.tuya_lan.CMD_STATUS, session._body(self.tuya_lan.CMD_STATUS))

        self.assertNotEqual(control[:3], b'3.4')
        from aes import AESECB
        cipher = AESECB(self.KEY.encode('utf-8'))
        self.assertEqual(cipher.decrypt(control)[:3], b'3.4')
        self.assertNotEqual(cipher.decrypt(query)[:3], b'3.4')

    def test_a_34_plug_splits_into_outlets_like_any_other(self):
        """The version changes the wire, not what the device turns out to be."""
        self.start(version='3.4', dps=self.WP9_DPS)

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.4'})

        self.assertEqual([d.driver_data['dp'] for d in found],
                         ['all', '1', '2', '3', '7'])
        self.assertEqual(found[0].model, 'Tuya 3.4')

    def test_a_34_reply_without_a_return_code_is_read_correctly(self):
        """Nothing in the header says whether one is there; block size does."""
        session = self.tuya_lan.Session('127.0.0.1', 'wp9abc', self.KEY,
                                        version='3.4')
        blocks = b'x' * 64

        with_code = (struct.pack('>4I', self.tuya_lan.PREFIX, 1, 4,
                                 len(blocks) + 4 + 36)
                     + struct.pack('>I', 0) + blocks + b'z' * 36)
        without = (struct.pack('>4I', self.tuya_lan.PREFIX, 1, 4,
                               len(blocks) + 36) + blocks + b'z' * 36)

        self.assertEqual(session._reply_payload(with_code), (0, blocks))
        self.assertEqual(session._reply_payload(without), (0, blocks))

    # -- outlets -----------------------------------------------------------

    def test_a_multi_outlet_plug_is_listed_once_per_outlet(self):
        """A WP9 is four switches in a box, not one."""
        self.start(dps=self.WP9_DPS)
        driver = self.driver()

        found = driver._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual([d.driver_data['dp'] for d in found],
                         ['all', '1', '2', '3', '7'])
        self.assertEqual([d.name for d in found],
                         ['Tuya 9ABC All outlets', 'Tuya 9ABC Outlet 1',
                          'Tuya 9ABC Outlet 2', 'Tuya 9ABC Outlet 3',
                          'Tuya 9ABC USB'])
        # Countdowns, relay memory and the child lock are not outlets.
        for device in found:
            self.assertNotIn(device.driver_data['dp'],
                             ('9', '10', '11', '15', '38', '40'))

    def test_every_outlet_keeps_the_plug_id_for_the_wire(self):
        """The suffix is ours; the device would disown it."""
        self.start(dps=self.WP9_DPS)

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(set(d.native_id for d in found), set(['wp9abc']))
        self.assertEqual(len(set(d.device_id for d in found)), 5)

    def test_each_outlet_switches_only_itself(self):
        plug = self.start(dps=self.WP9_DPS)
        driver = self.driver()

        driver.turn(self.device(dp='2'), False)

        self.assertFalse(plug.dps['2'])
        self.assertFalse(plug.dps['1'])
        self.assertTrue(plug.dps['7'])

    # -- the whole plug ----------------------------------------------------

    def master(self, members=('1', '2', '3', '7'), version='3.4'):
        return Device('wp9abc#all', name='Office Plug', driver='tuya',
                      ip='127.0.0.1', lan=True, native_id='wp9abc',
                      driver_data={'version': version, 'dp': 'all',
                                   'members': list(members)})

    def test_the_whole_plug_switches_off_in_one_command(self):
        """A WP9 has no master relay; the app's "all off" is every outlet at
        once. One packet also means the outlets go together, not in sequence."""
        plug = self.start(version='3.4', dps=self.WP9_DPS)

        self.driver().turn(self.master(), False)

        self.assertEqual([plug.dps[dp] for dp in ('1', '2', '3', '7')],
                         [False, False, False, False])
        controls = [c for c, _p in plug.requests
                    if c == self.tuya_lan.CMD_CONTROL_NEW]
        self.assertEqual(len(controls), 1)

    def test_the_whole_plug_switches_on_too(self):
        plug = self.start(version='3.4', dps=self.WP9_DPS)

        self.driver().turn(self.master(), True)

        self.assertEqual([plug.dps[dp] for dp in ('1', '2', '3', '7')],
                         [True, True, True, True])

    def test_the_whole_plug_reads_as_on_when_anything_is_drawing_power(self):
        """Requiring all of them would call a plug with one outlet live off."""
        self.start(version='3.4',
                   dps={'1': False, '2': True, '3': False, '7': False})

        state = self.driver().get_state(self.master())

        self.assertEqual(state['power'], 'on')

    def test_the_whole_plug_reads_as_off_only_when_everything_is(self):
        self.start(version='3.4',
                   dps={'1': False, '2': False, '3': False, '7': False})

        self.assertEqual(self.driver().get_state(self.master())['power'],
                         'off')

    def test_a_master_saved_before_members_were_recorded_still_works(self):
        """It falls back to every socket datapoint Tuya defines.

        A plug ignores a datapoint it does not have, so the fallback is
        harmless where switching nothing at all would not be.
        """
        plug = self.start(version='3.4', dps=self.WP9_DPS)
        old = Device('wp9abc#all', name='Office Plug', driver='tuya',
                     ip='127.0.0.1', lan=True, native_id='wp9abc',
                     driver_data={'version': '3.4', 'dp': 'all'})

        self.driver().turn(old, False)

        self.assertEqual([plug.dps[dp] for dp in ('1', '2', '3', '7')],
                         [False, False, False, False])

    def test_a_group_command_uses_the_master_instead_of_every_outlet(self):
        """Same instruction to all of them, so one packet beats four."""
        from tuya_driver import TuyaDriver

        outlets = [self.device(dp=dp) for dp in ('1', '2', '3', '7')]
        collapsed = TuyaDriver.collapse([self.master()] + outlets)

        self.assertEqual([d.device_id for d in collapsed], ['WP9ABC#ALL'])

    def test_collapsing_leaves_outlets_of_a_plug_with_no_master_alone(self):
        from tuya_driver import TuyaDriver

        outlets = [self.device(dp=dp) for dp in ('1', '2')]
        self.assertEqual(TuyaDriver.collapse(outlets), outlets)

    def test_collapsing_never_touches_another_driver(self):
        from tuya_driver import TuyaDriver

        bulb = Device('AA:BB', name='Lamp', driver='govee')
        collapsed = TuyaDriver.collapse([self.master(), bulb])

        self.assertIn(bulb, collapsed)

    def test_collapsing_only_folds_the_plug_that_has_the_master(self):
        from tuya_driver import TuyaDriver

        other = Device('otherplug#1', name='Hall', driver='tuya',
                       native_id='otherplug', driver_data={'dp': '1'})
        collapsed = TuyaDriver.collapse(
            [self.master(), self.device(dp='2'), other])

        self.assertEqual([d.device_id for d in collapsed],
                         ['WP9ABC#ALL', 'OTHERPLUG#1'])

    def test_one_key_covers_every_outlet(self):
        """The key belongs to the plug, so it is not typed in four times."""
        self.start(dps=self.WP9_DPS)
        driver = self.driver(keys={})
        outlets = [self.device(dp=dp) for dp in ('1', '2', '3')]

        self.assertTrue(driver.set_local_key(outlets[0], self.KEY))
        for outlet in outlets:
            self.assertFalse(driver.needs_key(outlet))

    def test_outlets_of_one_plug_share_a_single_round_trip(self):
        """Three outlets, one conversation -- and one consistent reading."""
        plug = self.start(dps=self.WP9_DPS)
        driver = self.driver()
        outlets = [self.device(dp=dp) for dp in ('1', '2', '3')]

        states = driver.get_states(outlets)

        self.assertEqual(plug.connections, 1)
        self.assertEqual(states['WP9ABC#1'],
                         {'power': 'off', 'dps': plug.dps})
        self.assertEqual(states['WP9ABC#2']['power'], 'on')
        self.assertEqual(states['WP9ABC#3']['power'], 'off')

    def test_a_single_outlet_plug_stays_one_device(self):
        """A one-outlet plug should not be called "Outlet 1"."""
        self.start(dps={'1': False, '9': 0})

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(len(found), 1)
        self.assertNotIn('dp', found[0].driver_data)
        self.assertEqual(found[0].device_id, 'WP9ABC')

    def test_an_unkeyed_plug_is_listed_whole_rather_than_guessed_at(self):
        """Without a key it cannot be asked, and guessing would invent outlets."""
        self.start(dps=self.WP9_DPS)

        found = self.driver(keys={})._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_id, 'WP9ABC')

    def test_a_plug_that_will_not_answer_is_still_listed(self):
        plug = self.start(dps=self.WP9_DPS)
        plug.refuse = 1

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(len(found), 1)

    def test_an_entry_from_before_outlets_still_switches(self):
        """A devices.json written by v2.2 has no datapoint recorded."""
        plug = self.start(dps={'1': False})
        old = Device('wp9abc', name='Old Entry', driver='tuya',
                     ip='127.0.0.1', lan=True, native_id='wp9abc')

        self.driver().turn(old, True)

        self.assertTrue(plug.dps['1'])

    # -- power-cut memory --------------------------------------------------

    def test_the_power_cut_setting_is_read_from_the_plug(self):
        """Datapoint 38: what the relay does when mains power returns."""
        self.start(dps=dict(self.WP9_DPS, **{'38': 'last'}))

        value, options = self.driver().power_memory(self.device())

        self.assertEqual(value, 'last')
        self.assertEqual([v for _label, v in options],
                         ['power_off', 'power_on', 'last'])

    def test_the_power_cut_setting_can_be_changed(self):
        plug = self.start(dps=dict(self.WP9_DPS, **{'38': 'last'}))

        self.driver().set_power_memory(self.device(), 'power_off')

        self.assertEqual(plug.dps['38'], 'power_off')

    def test_a_plug_without_the_datapoint_says_so_rather_than_guessing(self):
        """Not every Tuya plug has a relay memory, and none is not "off"."""
        self.start(dps={'1': True, '2': False})

        value, options = self.driver().power_memory(self.device())

        self.assertIsNone(value)
        self.assertEqual(options, [])

    def test_an_unreadable_plug_does_not_invent_a_setting(self):
        plug = self.start(dps=self.WP9_DPS)
        plug.refuse = 1

        self.assertEqual(self.driver().power_memory(self.device()), (None, []))

    def test_a_value_the_plug_would_not_understand_is_refused_here(self):
        self.start(dps=self.WP9_DPS)

        with self.assertRaises(ControlError):
            self.driver().set_power_memory(self.device(), 'sideways')

    def test_the_setting_belongs_to_the_plug_not_to_one_outlet(self):
        """One relay memory in the box, however many sockets it has."""
        plug = self.start(dps=dict(self.WP9_DPS, **{'38': 'power_on'}))
        driver = self.driver()

        driver.set_power_memory(self.device(dp='2'), 'power_off')

        self.assertEqual(driver.power_memory(self.device(dp='3'))[0],
                         'power_off')
        self.assertEqual(plug.dps['38'], 'power_off')

    # -- test connection ---------------------------------------------------

    def test_test_connection_reports_what_it_found(self):
        self.start(dps=self.WP9_DPS)

        ok, message = self.driver().test_connection(self.device(dp='2'))

        self.assertTrue(ok)
        self.assertIn('on', message)

    def test_test_connection_explains_a_refusal(self):
        plug = self.start()
        plug.refuse = 1

        ok, message = self.driver().test_connection(self.device())

        self.assertFalse(ok)
        self.assertIn('device busy', message)


class FakeKasaPlug(object):
    """An HS103 that speaks the real Kasa protocol over loopback TCP.

    Strict about the framing difference on purpose: a TCP request without the
    4-byte length prefix is the classic way to get this wrong, and a real
    device answers it with silence rather than an error.
    """

    def __init__(self, alias='Office Lamp', model='HS103(US)',
                 relay_state=0, children=None, device_id='8006ABCD1234'):
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.device_id = device_id
        self.info = {
            'sw_ver': '1.0.6 Build 210524 Rel.162228',
            'hw_ver': '2.0',
            'model': model,
            'deviceId': device_id,
            'alias': alias,
            'mac': 'AA:BB:CC:DD:EE:FF',
            'relay_state': relay_state,
        }
        if children is not None:
            self.info['children'] = children
            del self.info['relay_state']
        self.requests = []
        self.connections = 0
        self.err_code = 0
        self.crashed = None

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(5)
        self.server.settimeout(0.3)
        self.port = self.server.getsockname()[1]

        self.running = True
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.running = False
        self.thread.join(2.0)
        self.server.close()

    def _serve(self):
        while self.running:
            try:
                conn, _addr = self.server.accept()
            except (socket.timeout, socket.error):
                continue
            self.connections += 1
            try:
                conn.settimeout(1.0)
                data = conn.recv(8192)
                if data:
                    reply = self._handle(data)
                    if reply:
                        conn.sendall(reply)
            except socket.error:
                pass
            except Exception as exc:  # surfaced by the test, not swallowed
                self.crashed = exc
            finally:
                try:
                    conn.close()
                except socket.error:
                    pass

    def _handle(self, data):
        assert len(data) >= 4, 'TCP requests carry a 4-byte length prefix'
        length = struct.unpack('>I', data[:4])[0]
        assert length == len(data) - 4, 'declared length did not match'

        body = json.loads(self.kasa_lan.decrypt(data[4:]).decode('utf-8'))
        self.requests.append(body)

        system = body.get('system') or {}
        if 'get_sysinfo' in system:
            return self._reply({'system': {'get_sysinfo': dict(self.info)}})

        relay = system.get('set_relay_state')
        if relay is not None:
            if self.err_code:
                return self._reply({'system': {'set_relay_state': {
                    'err_code': self.err_code, 'err_msg': 'device busy'}}})
            self._apply(body, relay.get('state'))
            return self._reply(
                {'system': {'set_relay_state': {'err_code': 0}}})
        return self._reply({'system': {}})

    def _apply(self, body, state):
        wanted = list((body.get('context') or {}).get('child_ids') or [])
        if not wanted:
            self.info['relay_state'] = state
            return
        for child in self.info.get('children') or []:
            if self.device_id + child['id'] in wanted \
                    or child['id'] in wanted:
                child['state'] = state

    def _reply(self, body):
        payload = self.kasa_lan.encrypt(json.dumps(body))
        return struct.pack('>I', len(payload)) + payload


class TestNativeStrings(unittest.TestCase):
    """Text meeting binary in one HTTP request, which Python 2 will not do.

    Python 2's httplib joins the request headers into one string and then
    appends the body. One unicode header there makes the join unicode, and
    appending a binary body forces an implicit ascii decode of it -- which
    fails as "'ascii' codec can't decode byte 0xcc in position 0", pointing
    at the payload rather than at the header that caused it.

    A device address read back from devices.json is unicode on Python 2,
    because that is what json.load produces, so this is the ordinary case.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'compat', 'kasa_klap', 'govee_cloud'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_to_native_returns_the_interpreters_own_str(self):
        import compat

        self.assertIsInstance(compat.to_native(u'10.0.0.31'), str)
        self.assertIsInstance(compat.to_native(b'10.0.0.31'), str)

    def test_a_klap_request_url_is_never_text_beside_a_binary_body(self):
        import kasa_klap

        captured = {}

        class _Recorder(object):
            def __init__(self, url, data=None, headers=None):
                captured['url'] = url
                captured['data'] = data
                captured['headers'] = dict(headers or {})

            def add_header(self, key, value):
                captured['headers'][key] = value

        def refuse(request, timeout=None):
            raise kasa_klap.URLError('stop here')

        kasa_klap.Request = _Recorder
        kasa_klap.urlopen = refuse
        session = kasa_klap.Session(u'10.0.0.31', u'me@example.com',
                                    u'pw', port=80)

        self.assertRaises(kasa_klap.KlapError,
                          session._post, '/app/handshake1', b'\xcc' * 16)
        self.assertIsInstance(captured['url'], str)
        self.assertEqual(captured['data'], b'\xcc' * 16)
        for value in captured['headers'].values():
            self.assertIsInstance(value, str)

    def test_an_address_from_the_device_cache_is_made_native_at_the_door(self):
        import kasa_klap

        session = kasa_klap.Session(u'10.0.0.31', 'user', 'pw')

        self.assertIsInstance(session.ip, str)
        self.assertIsInstance(session.port, int)

    def test_a_govee_cloud_request_is_native_too(self):
        import govee_cloud

        captured = {}

        class _Recorder(object):
            def __init__(self, url, data=None, headers=None):
                captured['url'] = url
                captured['headers'] = dict(headers or {})

        def refuse(request, timeout=None, context=None):
            raise govee_cloud.URLError('stop here')

        govee_cloud.Request = _Recorder
        govee_cloud.urlopen = refuse
        client = govee_cloud.CloudTransport(api_key=u'a-key')

        self.assertRaises(govee_cloud.CloudError,
                          client._request, 'GET', u'https://example.test/x')
        self.assertIsInstance(captured['url'], str)
        for value in captured['headers'].values():
            self.assertIsInstance(value, str)


class TestKasaProtocol(unittest.TestCase):
    """The cipher and the two framings, which is most of this protocol."""

    def setUp(self):
        for name in ('kasa_lan', 'kasa_driver'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_lan
        self.kasa_lan = kasa_lan

    def test_the_cipher_round_trips(self):
        text = '{"system":{"set_relay_state":{"state":1}}}'
        self.assertEqual(
            self.kasa_lan.decrypt(self.kasa_lan.encrypt(text)).decode('utf-8'),
            text)

    def test_the_cipher_matches_the_published_prefix(self):
        """Every Kasa request starts {"system": and so encrypts identically.

        Pinned against the known bytes rather than only against my own
        decrypt, which would agree with itself however wrong it was.
        """
        encrypted = self.kasa_lan.encrypt('{"system":')
        self.assertEqual(''.join('%02x' % b for b in bytearray(encrypted)),
                         'd0f281f88bff9af7d5ef')

    def test_the_chain_runs_off_the_output_not_the_input(self):
        """Two identical plaintext bytes must not encrypt identically."""
        encrypted = bytearray(self.kasa_lan.encrypt('aa'))
        self.assertNotEqual(encrypted[0], encrypted[1])

    def test_tcp_framing_declares_the_length_and_udp_does_not(self):
        payload = 'ab'
        framed = self.kasa_lan.framed(payload)

        self.assertEqual(struct.unpack('>I', framed[:4])[0], len(payload))
        self.assertEqual(framed[4:], self.kasa_lan.encrypt(payload))

    def test_the_sweep_range_comes_from_devices_not_just_this_machine(self):
        """The fix for a search that found one plug and swept the wrong subnet.

        Working out which interface reaches the plugs is guesswork: a VPN, a
        container bridge or a hostname resolving somewhere unhelpful all give
        a confident wrong answer, and sweeping the wrong /24 looks exactly
        like sweeping the right one and finding nothing.
        """
        subnets = self.kasa_lan.sweep_subnets(
            local=['192.0.2.2'],            # a VPN address, reaches nothing
            found_ips=['10.0.0.25'],        # a plug the broadcast did find
            hints=['10.0.0.71'])            # a device already in the cache

        self.assertIn('10.0.0', subnets)

    def test_the_subnet_of_an_already_known_device_is_enough_on_its_own(self):
        """Even when the broadcast finds nothing and this host looks useless."""
        subnets = self.kasa_lan.sweep_subnets(
            local=[''], found_ips=[], hints=['10.0.0.71'])

        self.assertEqual(subnets, ['10.0.0'])

    def test_a_subnet_is_swept_once_however_many_devices_name_it(self):
        subnets = self.kasa_lan.sweep_subnets(
            local=['10.0.0.5'],
            found_ips=['10.0.0.25', '10.0.0.26'],
            hints=['10.0.0.71', '10.0.0.99'])

        self.assertEqual(subnets, ['10.0.0'])

    def test_loopback_and_rubbish_are_not_swept(self):
        subnets = self.kasa_lan.sweep_subnets(
            local=['127.0.0.1'], found_ips=['', 'not-an-address'],
            hints=[None])

        self.assertEqual(subnets, [])

    def test_sysinfo_without_an_id_is_not_a_device(self):
        self.assertIsNone(self.kasa_lan.parse_sysinfo({'alias': 'No id'}))

    def test_sysinfo_is_read_whether_wrapped_or_bare(self):
        bare = {'deviceId': '8006', 'alias': 'Lamp', 'relay_state': 1}
        wrapped = {'system': {'get_sysinfo': bare}}

        self.assertEqual(self.kasa_lan.parse_sysinfo(bare)['alias'], 'Lamp')
        self.assertEqual(self.kasa_lan.parse_sysinfo(wrapped)['alias'], 'Lamp')

    def test_a_child_id_is_made_whole_when_reported_relative(self):
        """Firmware differs on this and the device wants the whole one."""
        parsed = self.kasa_lan.parse_sysinfo({
            'deviceId': '8006ABCD',
            'children': [{'id': '00', 'alias': 'Left'},
                         {'id': '8006ABCD01', 'alias': 'Right'}],
        })

        self.assertEqual([c['id'] for c in parsed['children']],
                         ['8006ABCD00', '8006ABCD01'])


class FakeKasaResponder(object):
    """A Kasa plug answering discovery over UDP, on its own loopback address.

    `deaf_to_broadcast` models the failure that actually happens: an access
    point that drops broadcast traffic, so the plug never hears the search
    even though it is reachable and answers a datagram addressed to it.
    """

    def __init__(self, host, port, alias, deaf_to_broadcast=False):
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.host = host
        self.alias = alias
        self.deaf_to_broadcast = deaf_to_broadcast
        self.asked = 0
        self.crashed = None

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)

        self.running = True
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.running = False
        self.thread.join(2.0)
        self.sock.close()

    def _serve(self):
        while self.running:
            try:
                data, sender = self.sock.recvfrom(4096)
            except socket.error:
                continue
            try:
                self.asked += 1
                body = json.loads(
                    self.kasa_lan.decrypt(data).decode('utf-8'))
                if 'get_sysinfo' not in (body.get('system') or {}):
                    continue
                reply = {'system': {'get_sysinfo': {
                    'deviceId': '8006%s' % self.alias.replace(' ', ''),
                    'alias': self.alias, 'model': 'HS103(US)',
                    'sw_ver': '1.0.6', 'relay_state': 0}}}
                self.sock.sendto(self.kasa_lan.encrypt(json.dumps(reply)),
                                 sender)
            except Exception as exc:
                self.crashed = exc


class TestKasaDiscovery(unittest.TestCase):
    """Finding all of them, on a network that does not carry broadcast."""

    HOSTS = ['127.0.0.2', '127.0.0.3', '127.0.0.4']

    def setUp(self):
        for name in ('kasa_lan', 'kasa_driver'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.port = _free_port()
        kasa_lan.PORT = self.port
        self.real_addresses = kasa_lan.local_addresses
        self.real_sweep = kasa_lan.sweep_addresses
        self.real_subnets = kasa_lan.sweep_subnets
        # The search runs on loopback, which the real helpers deliberately
        # refuse to sweep -- so both the subnet choice and the host list are
        # stood in for here.
        kasa_lan.local_addresses = lambda: ['127.0.0.1']
        kasa_lan.sweep_subnets = lambda local, found, hints: ['127.0.0']
        kasa_lan.sweep_addresses = lambda address: list(self.HOSTS)
        self.plugs = []

    def tearDown(self):
        for plug in self.plugs:
            crashed = plug.crashed
            plug.close()
            self.assertIsNone(crashed, 'a fake plug crashed: %r' % crashed)
        self.kasa_lan.local_addresses = self.real_addresses
        self.kasa_lan.sweep_addresses = self.real_sweep
        self.kasa_lan.sweep_subnets = self.real_subnets

    def start(self, *specs):
        for host, alias, deaf in specs:
            self.plugs.append(
                FakeKasaResponder(host, self.port, alias,
                                  deaf_to_broadcast=deaf))
        return self.plugs

    def test_every_plug_is_found_when_broadcast_does_not_reach_them(self):
        """The one that matters: three of four silent on broadcast.

        Loopback carries no broadcast at all, so every plug here is found by
        the sweep -- which is the point. A network that drops broadcast used
        to mean those devices simply never appeared.
        """
        self.start(('127.0.0.2', 'Tree', True), ('127.0.0.3', 'Lamp', True),
                   ('127.0.0.4', 'Fan', True))

        devices, report = self.kasa_lan.search(timeout=4.0)

        self.assertEqual(sorted(d['alias'] for d in devices),
                         ['Fan', 'Lamp', 'Tree'])
        self.assertEqual(report['sweep'], 3)

    def test_a_reply_to_a_send_is_not_thrown_away(self):
        """The bug this replaced: the socket that sent was closed at once.

        A device answers to the port the request came from, so closing that
        socket discarded every reply to it -- and the search found only the
        devices that happened to answer the one send from the socket that
        stayed open.
        """
        self.start(('127.0.0.2', 'Tree', True))

        devices, _report = self.kasa_lan.search(timeout=3.0)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['ip'], '127.0.0.2')

    def test_the_same_plug_answering_twice_is_listed_once(self):
        """Broadcast repeats, so a device is asked more than once."""
        self.start(('127.0.0.2', 'Tree', True))

        devices, _report = self.kasa_lan.search(timeout=3.0)

        self.assertGreater(self.plugs[0].asked, 1)
        self.assertEqual(len(devices), 1)

    def test_a_search_with_the_sweep_off_stays_on_broadcast(self):
        self.start(('127.0.0.2', 'Tree', True))

        devices, report = self.kasa_lan.search(timeout=2.0, sweep=False)

        self.assertEqual(devices, [])
        self.assertEqual(report['sweep'], 0)

    def test_a_pass_stops_once_everything_has_gone_quiet(self):
        """A search that found everything should not sit out its window."""
        self.start(('127.0.0.2', 'Tree', True))

        started = time.time()
        self.kasa_lan.search(timeout=20.0)
        elapsed = time.time() - started

        self.assertLess(elapsed, 12.0)

    def test_the_driver_says_when_broadcast_was_the_problem(self):
        """Silently working around it would hide a real network fault."""
        from kasa_driver import KasaDriver

        self.start(('127.0.0.2', 'Tree', True))

        devices, warnings = KasaDriver().discover(timeout=3.0)

        self.assertEqual(len(devices), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn('dropping broadcast', warnings[0])


class _KlapHandler(BaseHTTPRequestHandler):
    """The device half of the KLAP handshake, written from the spec.

    Deliberately not sharing helpers with the client beyond the AES itself:
    if both sides computed their hashes with the same function, the tests
    would prove the two agree rather than that either is right.
    """

    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _body(self):
        return self.rfile.read(int(self.headers.get('Content-Length') or 0))

    def _send(self, payload, code=200, cookie=None):
        self.send_response(code)
        self.send_header('Content-Length', str(len(payload)))
        if cookie:
            self.send_header('Set-Cookie', '%s=%s;TIMEOUT=1440'
                             % (kasa_klap.SESSION_COOKIE, cookie))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        plug = self.server.plug
        path = self.path.split('?')[0]
        body = self._body()

        if path == '/app/handshake1':
            if plug.refuse_handshake:
                return self._send(b'', code=403)
            plug.local_seed = body[:16]
            digest = plug.digest()
            proof = hashlib.sha256(
                plug.local_seed + plug.remote_seed + digest).digest() \
                if plug.scheme == 'v2' else \
                hashlib.sha256(plug.local_seed + digest).digest()
            return self._send(plug.remote_seed + proof, cookie='fake-session')

        if path == '/app/handshake2':
            digest = plug.digest()
            expected = hashlib.sha256(
                plug.remote_seed + plug.local_seed + digest).digest() \
                if plug.scheme == 'v2' else \
                hashlib.sha256(plug.remote_seed + digest).digest()
            assert body == expected, 'client failed handshake2'
            plug.cookie_seen = self.headers.get('Cookie') or ''
            plug.derive(digest)
            return self._send(b'')

        if path == '/app/request':
            assert plug.key, 'a request arrived before the handshake'
            plug.cookie_seen = self.headers.get('Cookie') or ''
            # The sequence travels in the URL and is part of the IV. A device
            # answers under the same one it was asked with, so this is read
            # rather than counted independently.
            sequence = int(self.path.split('seq=')[1])
            request = json.loads(plug.decrypt(body, sequence).decode('utf-8'))
            plug.requests.append(request)
            reply = json.dumps(plug.answer(request)).encode('utf-8')
            return self._send(plug.encrypt(reply, sequence))

        return self._send(b'', code=404)


class FakeKlapPlug(object):
    """An HS103 hardware v5, answering KLAP over HTTP."""

    def __init__(self, username='me@example.com', password='hunter2',
                 scheme='v1', relay_state=0):
        import kasa_klap as klap_module

        globals()['kasa_klap'] = klap_module
        self.username = username
        self.password = password
        self.scheme = scheme
        self.relay_state = relay_state
        self.remote_seed = bytes(bytearray(range(0x10, 0x20)))
        self.local_seed = b''
        self.key = b''
        self.iv = b''
        self.signature = b''
        self.requests = []
        self.cookie_seen = ''
        self.refuse_handshake = False

        self.server = HTTPServer(('127.0.0.1', 0), _KlapHandler)
        self.server.plug = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2.0)

    def digest(self):
        if self.scheme == 'v2':
            return hashlib.sha256(
                hashlib.sha1(self.username.encode()).digest()
                + hashlib.sha1(self.password.encode()).digest()).digest()
        return hashlib.md5(
            hashlib.md5(self.username.encode()).digest()
            + hashlib.md5(self.password.encode()).digest()).digest()

    def derive(self, digest):
        """The session key, worked out here rather than borrowed from the
        client -- otherwise the tests would only prove the two agree."""
        material = self.local_seed + self.remote_seed + digest
        self.key = hashlib.sha256(b'lsk' + material).digest()[:16]
        self.iv = hashlib.sha256(b'iv' + material).digest()[:12]
        self.signature = hashlib.sha256(b'ldk' + material).digest()[:28]

    def _cipher(self, sequence):
        from aes import AES

        return AES(self.key, self.iv + struct.pack('>i', sequence))

    def decrypt(self, body, sequence):
        plain = bytearray(self._cipher(sequence).decrypt(body[32:]))
        return bytes(plain[:len(plain) - plain[-1]])

    def encrypt(self, message, sequence):
        padding = 16 - (len(message) % 16)
        padded = message + bytes(bytearray([padding] * padding))
        ciphertext = self._cipher(sequence).encrypt(padded)
        signed = hashlib.sha256(
            self.signature + struct.pack('>i', sequence) + ciphertext).digest()
        return signed + ciphertext

    def answer(self, request):
        system = request.get('system') or {}
        if 'get_sysinfo' in system:
            return {'system': {'get_sysinfo': {
                'deviceId': '8006KLAP', 'alias': 'Christmas Tree',
                'model': 'HS103(US)', 'sw_ver': '1.1.3', 'hw_ver': '5.0',
                'relay_state': self.relay_state}}}
        relay = system.get('set_relay_state')
        if relay is not None:
            self.relay_state = relay.get('state')
            return {'system': {'set_relay_state': {'err_code': 0}}}
        return {'system': {}}


class TestKasaKlap(unittest.TestCase):
    """Later Kasa hardware: same model number, different protocol."""

    USER = 'me@example.com'
    PASSWORD = 'hunter2'

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home', 'kasa_driver', 'kasa_lan',
                     'kasa_klap'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_klap

        self.klap = kasa_klap
        self.plug = None

    def tearDown(self):
        if self.plug is not None:
            self.plug.close()
        clean_profile()

    def start(self, **kwargs):
        kwargs.setdefault('username', self.USER)
        kwargs.setdefault('password', self.PASSWORD)
        self.plug = FakeKlapPlug(**kwargs)
        return self.plug

    def session(self, username=None, password=None):
        return self.klap.Session(
            '127.0.0.1',
            self.USER if username is None else username,
            self.PASSWORD if password is None else password,
            port=self.plug.port, timeout=5.0)

    # -- handshake ---------------------------------------------------------

    def test_the_handshake_agrees_a_key_and_a_command_goes_through(self):
        plug = self.start(relay_state=0)
        session = self.session()

        session.request({'system': {'set_relay_state': {'state': 1}}})

        self.assertEqual(plug.relay_state, 1)

    def test_the_hash_scheme_is_detected_rather_than_assumed(self):
        """The device never says which it uses; its handshake hash does."""
        self.start(scheme='v2')

        session = self.session()
        session.handshake()

        self.assertEqual(session.scheme, 'v2')

    def test_the_other_scheme_is_detected_too(self):
        self.start(scheme='v1')

        session = self.session()
        session.handshake()

        self.assertEqual(session.scheme, 'v1')

    def test_a_wrong_password_is_named_as_the_cause(self):
        self.start()

        with self.assertRaises(self.klap.KlapAuthError) as caught:
            self.session(password='wrong').handshake()
        message = str(caught.exception)
        self.assertIn('account it is registered to', message)

    def test_a_refused_handshake_is_an_auth_error_not_a_network_one(self):
        """HTTP 403 means the credentials, not the connection."""
        plug = self.start()
        plug.refuse_handshake = True

        with self.assertRaises(self.klap.KlapAuthError):
            self.session().handshake()

    def test_the_session_cookie_is_carried_after_the_handshake(self):
        plug = self.start()

        self.session().request({'system': {'get_sysinfo': {}}})

        self.assertIn(self.klap.SESSION_COOKIE, plug.cookie_seen)

    def test_an_unreachable_device_is_a_plain_reach_error(self):
        self.start()
        session = self.klap.Session('127.0.0.1', self.USER, self.PASSWORD,
                                    port=_free_port(), timeout=2.0)

        with self.assertRaises(self.klap.KlapError) as caught:
            session.handshake()
        self.assertIn('Could not reach', str(caught.exception))

    def test_a_plug_never_bound_to_an_account_is_handled(self):
        """Blank credentials are a real case, and the hash still settles it."""
        self.start(username='', password='')

        session = self.session(username='someone@example.com',
                               password='whatever')
        session.handshake()

        # The distinction that matters when some plugs work and others do
        # not: this one was reached without the account, so the account being
        # wrong would not have shown up here.
        self.assertFalse(session.used_account)

    def test_a_plug_reached_with_the_entered_account_says_so(self):
        """The other half of the same distinction."""
        self.start()

        session = self.session()
        session.handshake()

        self.assertTrue(session.used_account)

    def test_tp_links_own_setup_credentials_are_tried(self):
        """A device in setup state answers to those and nothing else."""
        self.start(username='kasa@tp-link.net', password='kasaSetup')

        session = self.session()
        session.handshake()

        self.assertFalse(session.used_account)

    def test_the_failure_message_has_real_line_breaks(self):
        """A quoted heredoc doubled the escape once; this pins it.

        The dialog is where a user reads this, and a literal backslash-n in
        the middle of a sentence is worse than no formatting at all.
        """
        self.start()

        with self.assertRaises(self.klap.KlapAuthError) as caught:
            self.session(password='wrong').handshake()
        message = str(caught.exception)

        self.assertIn('\n\n', message)
        self.assertNotIn('\\n', message)

    # -- through the driver ------------------------------------------------

    def driver(self, username=None, password=None):
        from kasa_driver import KasaDriver

        return KasaDriver(
            timeout=5.0,
            username=self.USER if username is None else username,
            password=self.PASSWORD if password is None else password)

    def klap_device(self):
        return Device('8006KLAP', name='Christmas Tree', driver='kasa',
                      ip='127.0.0.1', lan=True, native_id='8006KLAP',
                      driver_data={'protocol': 'klap',
                                   'http_port': self.plug.port})

    def test_a_klap_plug_switches_through_the_driver(self):
        plug = self.start(relay_state=0)

        self.driver().turn(self.klap_device(), True)

        self.assertEqual(plug.relay_state, 1)

    def test_a_klap_plug_reports_its_state(self):
        self.start(relay_state=1)

        self.assertEqual(self.driver().get_state(self.klap_device()),
                         {'power': 'on'})

    def test_without_credentials_it_says_what_is_needed_and_where(self):
        """Listed and honest, rather than hidden or looking broken."""
        self.start()

        with self.assertRaises(ControlError) as caught:
            self.driver(username='', password='').turn(self.klap_device(),
                                                       True)
        message = str(caught.exception)
        self.assertIn('TP-Link account', message)
        self.assertIn('Settings', message)
        self.assertIn('nothing is sent to TP-Link', message)

    def test_a_legacy_plug_is_untouched_by_any_of_this(self):
        """Hardware v2 must keep working with no account at all."""
        from kasa_driver import KasaDriver

        legacy = Device('8006OLD', name='Egg Maker', driver='kasa',
                        ip='10.0.0.25', lan=True, native_id='8006OLD',
                        driver_data={'protocol': 'legacy'})
        driver = KasaDriver(username='', password='')

        self.assertFalse(driver.is_klap(legacy))
        self.assertTrue(driver.is_klap(self.klap_device_stub()))

    def klap_device_stub(self):
        return Device('8006KLAP', name='Tree', driver='kasa',
                      driver_data={'protocol': 'klap'})

    def test_a_device_cached_before_klap_existed_is_treated_as_legacy(self):
        """A devices.json written by v2.7 records no protocol at all."""
        from kasa_driver import KasaDriver

        old = Device('8006OLD', name='Egg Maker', driver='kasa',
                     ip='10.0.0.25', lan=True, native_id='8006OLD')

        self.assertFalse(KasaDriver().is_klap(old))

    def test_discovery_warns_once_about_devices_needing_an_account(self):
        from kasa_driver import KasaDriver

        driver = KasaDriver(username='', password='')
        found = driver._devices_for({
            'device_id': '8006KLAP', 'ip': '10.0.0.31', 'model': 'HS103',
            'alias': '', 'children': [], 'protocol': 'klap', 'http_port': 80})

        self.assertEqual(len(found), 1)
        self.assertTrue(driver.is_klap(found[0]))
        # A KLAP announcement carries no alias, so a usable name is invented
        # rather than leaving the row blank.
        self.assertEqual(found[0].name, 'HS103 (KLAP)')

    # -- the cipher --------------------------------------------------------

    def test_each_request_uses_a_new_sequence_number(self):
        """The sequence is part of the IV, so reusing one repeats an IV."""
        session = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)

        _first, one = session.encrypt(b'{}')
        _second, two = session.encrypt(b'{}')

        self.assertEqual(two, one + 1)

    def test_the_sequence_stays_inside_a_signed_32_bit_range(self):
        """The device packs it as a signed int; overflowing would break it."""
        session = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)
        session.seq = 0x7FFFFFFF

        _body, sequence = session.encrypt(b'{}')

        self.assertEqual(sequence, -0x80000000)

    def test_a_payload_round_trips_through_the_cipher(self):
        session = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)
        mirror = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)

        body, _sequence = session.encrypt(b'{"system":{}}')
        mirror.seq = session.seq

        self.assertEqual(mirror.decrypt(body), b'{"system":{}}')

    def test_both_ends_derive_the_same_key_from_the_two_seeds(self):
        one = self.klap.Encryption(b'x' * 16, b'y' * 16, b'z' * 16)
        two = self.klap.Encryption(b'x' * 16, b'y' * 16, b'z' * 16)

        self.assertEqual(one.key, two.key)
        self.assertEqual(len(one.key), 16)
        self.assertEqual(len(one.iv), 12)


class TestKasaDiagnostics(unittest.TestCase):
    """A search that finds nothing has to say why, in order of likelihood."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'diagnostics', 'kasa_lan'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def summary(self, report):
        import diagnostics

        return diagnostics.kasa_summary(report)

    def test_devices_found_are_listed_with_what_to_do_next(self):
        text = self.summary({'devices': [
            {'alias': 'Office Lamp', 'ip': '10.0.0.9', 'model': 'HS103'}]})

        self.assertIn('Office Lamp', text)
        self.assertIn('10.0.0.9', text)
        self.assertIn('Refresh devices', text)

    def test_silence_names_the_causes_and_the_one_with_no_workaround(self):
        text = self.summary({'devices': [], 'listened': 6,
                             'addresses': ['10.0.0.5']})

        self.assertIn('different network', text)
        self.assertIn('firewall', text)
        # Closed firmware is the one outcome the add-on cannot fix, so it says
        # so rather than leaving the user retrying a search forever.
        self.assertIn('firmware has closed the local protocol', text)
        self.assertIn('cannot be worked around', text)
        self.assertIn('10.0.0.5', text)

    def test_a_sweep_that_never_ran_is_not_reported_as_one_that_found_nothing(self):
        """The two need opposite responses, so they must not read alike.

        A sweep that ran and found nothing means the plugs are not there. A
        sweep that never ran means the search could not tell which subnet to
        cover -- which is a fault in the search, not evidence about the plugs.
        """
        never = self.summary({'devices': [], 'broadcast': 0, 'subnets': [],
                              'addresses': ['192.0.2.2']})
        ran = self.summary({'devices': [], 'broadcast': 0,
                            'subnets': ['10.0.0'], 'sweep': 0, 'targets': 253,
                            'addresses': ['10.0.0.5']})

        self.assertIn('did not run', never)
        self.assertNotIn('did not run', ran)
        self.assertIn('10.0.0.0/24', ran)
        self.assertIn('253 hosts', ran)

    def test_a_sweep_that_covered_everything_says_what_is_left(self):
        text = self.summary({
            'devices': [{'alias': 'Egg Maker', 'ip': '10.0.0.25',
                         'model': 'HS103'}],
            'broadcast': 1, 'sweep': 0, 'subnets': ['10.0.0'],
            'targets': 253, 'addresses': ['10.0.0.5']})

        self.assertIn('found nothing further', text)
        self.assertIn('closed the local protocol', text)

    def test_a_send_failure_is_reported_rather_than_read_as_silence(self):
        text = self.summary({'devices': [], 'error': 'no interfaces'})

        self.assertIn('could not be sent', text)
        self.assertIn('no interfaces', text)


class TestKasaDriver(unittest.TestCase):
    """Switching a Kasa plug, against a device speaking the real protocol."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'kasa_driver', 'kasa_lan',
                     'hub'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.real_port = kasa_lan.PORT
        self.plug = None

    def tearDown(self):
        self.kasa_lan.PORT = self.real_port
        if self.plug is not None:
            crashed = self.plug.crashed
            self.plug.close()
            self.assertIsNone(crashed, 'the fake plug crashed: %r' % crashed)
        clean_profile()

    def start(self, **kwargs):
        self.plug = FakeKasaPlug(**kwargs)
        self.kasa_lan.PORT = self.plug.port
        return self.plug

    def driver(self):
        from kasa_driver import KasaDriver

        return KasaDriver(timeout=3.0)

    def device(self, child=None):
        return Device('8006ABCD1234%s' % ('#%s' % child if child else ''),
                      name='Office Lamp', driver='kasa', ip='127.0.0.1',
                      lan=True, native_id='8006ABCD1234',
                      driver_data={'child': child} if child else {})

    # -- switching ---------------------------------------------------------

    def test_a_plug_switches_on_and_off(self):
        plug = self.start(relay_state=0)
        driver = self.driver()

        driver.turn(self.device(), True)
        self.assertEqual(plug.info['relay_state'], 1)

        driver.turn(self.device(), False)
        self.assertEqual(plug.info['relay_state'], 0)

    def test_a_plug_reports_its_state(self):
        self.start(relay_state=1)

        self.assertEqual(self.driver().get_state(self.device()),
                         {'power': 'on'})

    def test_switching_needs_no_key_and_no_setup(self):
        """The whole difference from Tuya: found is the same as usable.

        A Tuya plug is discovered long before it can be switched, and the
        driver has to carry that half-state. A Kasa plug that answers a
        search can be switched with what the search already returned.
        """
        plug = self.start(relay_state=0)
        driver = self.driver()

        found = driver._devices_for({
            'device_id': plug.device_id, 'ip': '127.0.0.1',
            'alias': 'Office Lamp', 'model': 'HS103', 'children': []})

        driver.turn(found[0], True)

        self.assertEqual(plug.info['relay_state'], 1)

    def test_a_device_error_reaches_the_user_as_words(self):
        plug = self.start()
        plug.err_code = -3
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        message = str(caught.exception)
        self.assertIn('Office Lamp', message)
        self.assertIn('device busy', message)

    def test_silence_is_explained_as_closed_firmware(self):
        """A device that answers discovery but not a command has moved on."""
        self.kasa_lan.PORT = _free_port()
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        self.assertIn('Could not reach', str(caught.exception))

    def test_a_long_sysinfo_split_across_reads_is_reassembled(self):
        """Real sysinfo runs to a couple of kilobytes; one recv may not cover it."""
        self.start(alias='x' * 3000)

        state = self.driver().get_state(self.device())

        self.assertEqual(state, {'power': 'off'})

    # -- naming ------------------------------------------------------------

    def test_a_plug_arrives_with_the_name_it_already_has(self):
        """Kasa devices are named at pairing, so there is nothing to identify."""
        found = self.driver()._devices_for({
            'device_id': '8006ABCD1234', 'ip': '10.0.0.9',
            'alias': 'Christmas Tree', 'model': 'HS103', 'children': []})

        self.assertEqual(found[0].name, 'Christmas Tree')
        self.assertEqual(found[0].model, 'HS103')

    def test_a_single_outlet_plug_stays_one_device(self):
        found = self.driver()._devices_for({
            'device_id': '8006ABCD1234', 'ip': '10.0.0.9',
            'alias': 'Office Lamp', 'model': 'HS103', 'children': []})

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_id, '8006ABCD1234')
        self.assertIsNone(self.driver().child_id(found[0]))

    # -- power strips ------------------------------------------------------

    def test_a_strip_is_listed_once_per_outlet_under_its_own_names(self):
        found = self.driver()._devices_for({
            'device_id': '8006AAAA', 'ip': '10.0.0.9', 'model': 'KP303',
            'alias': 'Desk Strip',
            'children': [{'id': '8006AAAA00', 'alias': 'Monitor'},
                         {'id': '8006AAAA01', 'alias': 'Speakers'}],
        })

        self.assertEqual([d.name for d in found], ['Monitor', 'Speakers'])
        self.assertEqual(set(d.native_id for d in found), set(['8006AAAA']))

    def test_each_outlet_of_a_strip_switches_only_itself(self):
        plug = self.start(children=[{'id': '00', 'alias': 'Monitor',
                                     'state': 0},
                                    {'id': '01', 'alias': 'Speakers',
                                     'state': 0}])

        self.driver().turn(self.device(child='8006ABCD123401'), True)

        states = dict((c['id'], c['state']) for c in plug.info['children'])
        self.assertEqual(states, {'00': 0, '01': 1})

    def test_outlets_of_one_strip_share_a_single_round_trip(self):
        plug = self.start(children=[{'id': '00', 'alias': 'Monitor',
                                     'state': 1},
                                    {'id': '01', 'alias': 'Speakers',
                                     'state': 0}])
        outlets = [self.device(child='8006ABCD1234%s' % n)
                   for n in ('00', '01')]

        states = self.driver().get_states(outlets)

        self.assertEqual(plug.connections, 1)
        self.assertEqual(states[outlets[0].device_id], {'power': 'on'})
        self.assertEqual(states[outlets[1].device_id], {'power': 'off'})

    # -- capabilities ------------------------------------------------------

    def test_a_plug_claims_power_and_state_only(self):
        from devices import CAP_COLOR, CAP_COMMANDS, CAP_POWER, CAP_STATE

        caps = self.driver().capabilities(self.device())
        self.assertEqual(caps, set([CAP_POWER, CAP_STATE]))
        self.assertNotIn(CAP_COLOR, caps)
        self.assertNotIn(CAP_COMMANDS, caps)

    def test_test_connection_reports_the_model_and_state(self):
        self.start(relay_state=1)

        ok, message = self.driver().test_connection(self.device())

        self.assertTrue(ok)
        self.assertIn('HS103', message)
        self.assertIn('on', message)


class FakeBlaster(object):
    """A stand-in for a second vendor: emits commands, has no colour.

    Deliberately shaped like an IR blaster rather than a light. If the driver
    seam is real, this needs no changes anywhere else to work -- the registry,
    the Hub and the scene engine should route to it without knowing what it is.
    """

    DRIVER_ID = 'blaster'
    DRIVER_LABEL = 'Test Blaster'

    def __init__(self, devices=None, codes=None):
        self._devices = devices or []
        self._codes = codes or ['AVR Power', 'TV Input']
        self.sent = []

    def discover(self, timeout=3.0):
        return list(self._devices), []

    def capabilities(self, device):
        from devices import CAP_COMMANDS
        return set([CAP_COMMANDS])

    def commands(self, device):
        return list(self._codes)

    def send_command(self, device, name):
        from devices import ControlError
        if name not in self._codes:
            raise ControlError('%s has no code called "%s"'
                               % (device.name, name))
        self.sent.append((device.device_id, name))
        return True

    # State verbs exist but do nothing: this device has no state to set.
    def turn(self, device, on):
        from devices import ControlError
        raise ControlError('%s cannot be switched' % device.name)

    set_brightness = set_color = set_color_temp = turn

    def get_state(self, device):
        return None

    def get_states(self, devices, timeout=3.0):
        return dict((d.device_id, None) for d in devices)


class TestDriverSeam(unittest.TestCase):
    """A second vendor should need no changes outside its own driver."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'hub', 'scenes'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def build(self):
        import hub as hub_mod

        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')
        blaster = FakeBlaster(devices=[blaster_device])
        lights = RecordingController()
        lights.DRIVER_ID = 'govee'
        lights.DRIVER_LABEL = 'Govee'
        lights.discover = lambda timeout=3.0: (
            [Device('AA:BB', name='Lamp', lan=True, ip='10.0.0.1')], [])
        lights.capabilities = lambda device: set(['power', 'brightness',
                                                  'color'])
        lights.commands = lambda device: []
        lights.get_states = lambda devices, timeout=3.0: {}
        return hub_mod.Hub(drivers=[lights, blaster]), lights, blaster

    def test_discovery_merges_both_drivers_and_tags_ownership(self):
        hub, _lights, _blaster = self.build()
        found, warnings = hub.discover()

        self.assertEqual(warnings, [])
        self.assertEqual(sorted(d.driver for d in found),
                         ['blaster', 'govee'])

    def test_one_driver_failing_does_not_hide_the_other(self):
        hub, lights, _blaster = self.build()

        def broken(timeout=3.0):
            raise RuntimeError('port busy')

        lights.discover = broken
        found, warnings = hub.discover()

        self.assertEqual([d.driver for d in found], ['blaster'])
        self.assertTrue(any('port busy' in w for w in warnings))

    def test_commands_route_to_the_owning_driver(self):
        hub, _lights, blaster = self.build()
        device = Device('RM:01', name='Lounge Blaster', driver='blaster')

        hub.send_command(device, 'AVR Power')
        self.assertEqual(blaster.sent, [('RM:01', 'AVR Power')])

    def test_a_light_refuses_commands_it_cannot_emit(self):
        hub, _lights, _blaster = self.build()
        lamp = Device('AA:BB', name='Lamp', lan=True)

        with self.assertRaises(ControlError):
            hub.send_command(lamp, 'AVR Power')

    def test_devices_cached_before_drivers_existed_still_work(self):
        """An old devices.json records no driver; every entry is Govee."""
        restored = Device.from_dict({'device_id': 'AA:BB', 'name': 'Old',
                                     'lan': True})
        self.assertEqual(restored.driver, 'govee')

        hub, lights, _blaster = self.build()
        hub.turn(restored, True)
        self.assertEqual(lights.calls, [('turn', 'AA:BB', True)])

    def test_a_scene_dims_the_lights_and_fires_a_command(self):
        """The whole point: one scene spanning two vendors."""
        import scenes as fresh_scenes

        hub, lights, blaster = self.build()
        lamp = Device('AA:BB', name='Lamp', lan=True, ip='10.0.0.1')
        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')

        scene = fresh_scenes.make_scene(
            'Movie Night', brightness=8, mode=fresh_scenes.MODE_TEMP,
            kelvin=2000, targets=['AA:BB'],
            actions=[{'device': 'RM:01', 'command': 'AVR Power'}])

        applied, errors = fresh_scenes.apply_scene(
            hub, scene, [lamp, blaster_device])

        self.assertEqual(errors, [])
        self.assertEqual(applied, 2)          # one light, one command
        self.assertIn(('brightness', 'AA:BB', 8), lights.calls)
        self.assertEqual(blaster.sent, [('RM:01', 'AVR Power')])

    def test_a_scene_can_be_commands_only(self):
        """Switch the amp on; no lights involved."""
        import scenes as fresh_scenes

        hub, lights, blaster = self.build()
        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')

        scene = fresh_scenes.make_scene(
            'Amp On', targets=['RM:01'],
            actions=[{'device': 'RM:01', 'command': 'AVR Power'}])
        applied, errors = fresh_scenes.apply_scene(hub, scene,
                                                   [blaster_device])

        # The blaster has no state, so the state pass reports it and the
        # command still goes out.
        self.assertEqual(blaster.sent, [('RM:01', 'AVR Power')])
        self.assertEqual(lights.calls, [])

    def test_a_missing_command_is_reported_not_silent(self):
        import scenes as fresh_scenes

        hub, _lights, blaster = self.build()
        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')

        scene = fresh_scenes.make_scene(
            'Bad', targets=['NOBODY'],
            actions=[{'device': 'RM:01', 'command': 'Nonexistent'}])
        _applied, errors = fresh_scenes.apply_scene(hub, scene,
                                                    [blaster_device])

        self.assertEqual(blaster.sent, [])
        self.assertTrue(any('Nonexistent' in e for e in errors))

    def test_an_action_for_an_unknown_device_is_reported(self):
        import scenes as fresh_scenes

        hub, _lights, _blaster = self.build()
        scene = fresh_scenes.make_scene(
            'Bad', actions=[{'device': 'GONE', 'command': 'AVR Power'}])
        _applied, errors = fresh_scenes.apply_scene(hub, scene, [])
        self.assertTrue(any('AVR Power' in e for e in errors))

    def test_actions_survive_a_json_round_trip(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.make_scene(
            'Movie Night',
            actions=[{'device': 'rm:01', 'command': ' AVR Power '}])
        restored = fresh_scenes.normalise(json.loads(json.dumps(scene)))
        self.assertEqual(restored['actions'],
                         [{'device': 'RM:01', 'command': 'AVR Power'}])

    def test_malformed_actions_are_dropped(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.normalise({
            'name': 'Hand edited',
            'actions': ['junk', {'device': 'X'}, {'command': 'Y'},
                        {'device': 'Z', 'command': '   '},
                        {'device': 'OK', 'command': 'Fine'}]})
        self.assertEqual(scene['actions'],
                         [{'device': 'OK', 'command': 'Fine'}])

    def test_state_reads_are_grouped_per_driver(self):
        """Each driver keeps its own batching -- Govee's one-socket sweep."""
        hub, lights, _blaster = self.build()
        seen = []
        lights.get_states = lambda devices, timeout=3.0: (
            seen.append(len(devices)) or
            dict((d.device_id, {'power': 'on'}) for d in devices))

        states = hub.get_states([
            Device('AA:BB', name='One', lan=True),
            Device('CC:DD', name='Two', lan=True),
            Device('RM:01', name='Blaster', driver='blaster'),
        ])

        self.assertEqual(seen, [2])          # one call for both lights
        self.assertEqual(states['AA:BB']['power'], 'on')
        self.assertIsNone(states['RM:01'])

    def test_describe_mentions_commands(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.make_scene(
            'Movie Night', brightness=8,
            actions=[{'device': 'RM:01', 'command': 'AVR Power'}])
        self.assertIn('1 command(s)', fresh_scenes.describe(scene))


# ---------------------------------------------------------------------------
# Device model and transport selection
# ---------------------------------------------------------------------------

def touch_later(app, filename):
    """Make sure the stamp really differs, whatever the clock did.

    Writing a file twice inside one filesystem tick can leave mtime
    identical, which would make a passing test prove nothing.
    """
    import addon_utils

    path = addon_utils.profile_file(filename)
    stamp = os.stat(path)
    os.utime(path, (stamp.st_mtime + 5, stamp.st_mtime + 5))


class TestTheTelevisionHalf(unittest.TestCase):
    """Paragon TV, driven from the remote that lives here.

    The whole of it used to live inside Paragon TV. It reads that add-on from
    the outside now -- its properties on Kodi's home window, its settings2.xml
    and the file its Overlay writes -- so the two can be worked on apart.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        xbmc.reset_rpc()
        for name in ('addon_utils', 'paragon_home', 'sequences', 'gui',
                     'tv', 'remote'):
            if name in sys.modules:
                del sys.modules[name]
        self.tvprofile = os.path.join(tempfile.gettempdir(), 'ptv-under-test')
        if os.path.isdir(self.tvprofile):
            shutil.rmtree(self.tvprofile)

    def tearDown(self):
        clean_profile()
        if os.path.isdir(self.tvprofile):
            shutil.rmtree(self.tvprofile)

    def install_tv(self, channels=((1, 3, 'Action'),)):
        os.makedirs(self.tvprofile)
        lines = ['<settings>']
        for number, kind, name in channels:
            lines.append('<setting id="Channel_%d_type" value="%d" />'
                         % (number, kind))
            lines.append('<setting id="Channel_%d_1" value="%s" />'
                         % (number, name))
        lines.append('</settings>')
        handle = open(os.path.join(self.tvprofile, 'settings2.xml'), 'w')
        try:
            handle.write('\n'.join(lines))
        finally:
            handle.close()
        xbmcaddon.install('script.paragontv', {},
                          profile=self.tvprofile,
                          path='/home/user/script.paragontv')
        import tv

        return tv

    # -- a box with no television ------------------------------------------

    def test_a_box_without_paragon_tv_simply_has_no_television_half(self):
        """A house with lights and no television is a perfectly good house.

        Kodi raises for an add-on that is not installed, and that is the
        answer rather than an error.
        """
        import tv

        self.assertFalse(tv.installed())
        self.assertEqual(tv.snapshot(), {'ready': False, 'installed': False})
        self.assertEqual(tv.profile_dir(), '')
        self.assertEqual(tv.channels(), [])

    def test_a_television_action_on_a_box_without_one_says_so(self):
        import remote as remote_lib

        answer = remote_lib._perform_tv('press', {'button': 'up'})

        self.assertFalse(answer['ok'])
        self.assertIn('not installed', answer['message'])
        self.assertEqual(xbmc.rpc_calls, [])

    # -- a box with one ----------------------------------------------------

    def test_it_reads_the_channels_out_of_paragon_tvs_own_settings(self):
        """Read exactly as Paragon TV reads them, and never written."""
        tv = self.install_tv(((1, 3, 'Action'), (2, 4, 'Horror')))

        self.assertEqual([c['name'] for c in tv.channels()],
                         ['Action TV', 'Horror Movies'])

    def test_it_reads_the_channel_from_the_window_property(self):
        """Which is the one thing Paragon TV publishes for this."""
        tv = self.install_tv()
        xbmcgui.Window(tv.HOME_WINDOW).setProperty(tv.PROP_CHANNEL, '4')

        self.assertEqual(tv.current_channel(), 4)

    def test_a_key_press_reaches_kodi(self):
        tv = self.install_tv()

        ok, _message = tv.press('up')

        self.assertTrue(ok)
        self.assertEqual([c['method'] for c in xbmc.rpc_calls], ['Input.Up'])

    def test_the_logos_come_from_paragon_tvs_folder_not_ours(self):
        """This used to walk up from __file__, which found the logos because
        the code was inside that add-on. From here that would find Paragon
        Home's resources folder, which has no channel logos in it.
        """
        tv = self.install_tv()

        folder = tv.logo_dir().replace(os.sep, '/')

        self.assertIn('script.paragontv', folder)
        self.assertNotIn('script.paragon.home', folder)

    def test_the_utility_scripts_come_from_paragon_tvs_folder_too(self):
        tv = self.install_tv()
        xbmcgui.Window(tv.HOME_WINDOW).setProperty(tv.PROP_CHANNEL, '')

        ok, _message = tv.run_task('bumpers')

        self.assertTrue(ok)
        self.assertEqual(len(xbmc.BUILTINS), 1)
        self.assertIn('script.paragontv', xbmc.BUILTINS[0])
        self.assertIn('nfo_renamer_bumpers.py', xbmc.BUILTINS[0])

    def test_what_is_on_every_channel_comes_from_the_file_the_overlay_writes(self):
        """The one piece that cannot move.

        Only Paragon TV's Overlay can work out what is playing on a channel
        nobody is watching -- it holds the playlists and the reference points.
        So it writes that down and this reads it.
        """
        tv = self.install_tv()
        handle = open(os.path.join(self.tvprofile, tv.SNAPSHOT_FILE), 'w')
        try:
            handle.write(json.dumps({
                'at': int(time.time()),
                'channels': [{'number': 1, 'title': 'Mad Max',
                              'episode': '', 'elapsed': 60,
                              'duration': 1800, 'paused': False}]}))
        finally:
            handle.close()

        on_now = tv.read_now_on()

        self.assertEqual(on_now[1]['title'], 'Mad Max')

    def test_the_snapshot_carries_the_television_under_its_own_key(self):
        """Its own key, because it is its own add-on and a box may not have
        it."""
        import remote as remote_lib
        from paragon_home import ParagonHome

        self.install_tv()
        app = ParagonHome()
        app.controller = RecordingController()

        state = remote_lib.snapshot(app)

        self.assertIn('tv', state)
        self.assertTrue(state['tv']['installed'])
        # And the lights are still where they were.
        self.assertIn('scenes', state)
        self.assertIn('devices', state)

    def test_the_tab_panels_carry_the_height_down_to_the_panes(self):
        """The bug the tabs introduced, and the reason it is not obvious.

        On a wall panel the page itself does not scroll -- body is
        `overflow: hidden` and each pane scrolls inside its own box. That only
        works while every element between .wrap and .pane passes the height
        down: .wrap is a flex column, .deck takes the rest of it, and the pane
        gets a box smaller than its contents to scroll inside.

        Putting a tab panel between .wrap and .deck broke that chain. `flex:
        1` on the deck means nothing when its parent is a plain block, so the
        deck grew to the height of its contents, the pane never had a smaller
        box, and body's overflow: hidden simply cut the rest off. Eighty-five
        channels with ten of them reachable and no way to scroll.

        There is no unit test for a browser's layout, so this checks the rule
        is there and says what it is for.
        """
        page = WebRemote_PAGE = self.page()

        panel = page.index('#homePanel, #tvPanel {')
        rule = page[panel:page.index('}', panel)]

        self.assertIn('flex: 1', rule)
        self.assertIn('min-height: 0', rule)
        self.assertIn('display: flex', rule)
        self.assertIn('flex-direction: column', rule)

    def test_hiding_a_panel_still_beats_showing_it(self):
        """The panels are display: flex, and hidden ones must stay hidden.

        That works only because the [hidden] rule is !important. It was made
        !important for the badge, long before there were panels; this is the
        second thing depending on it, so it is worth a test of its own.
        """
        page = self.page()

        self.assertIn('[hidden] { display: none !important; }', page)

    def page(self):
        import remote as remote_lib

        return remote_lib.PAGE

    def test_the_television_actions_are_named_apart_from_the_lights(self):
        """So neither half can be reached by the other's name, and the server
        can tell at a glance which thread an action belongs on."""
        import remote as remote_lib

        for action in remote_lib.TV_ACTIONS:
            self.assertTrue(action.startswith('tv.'), action)
        for action in remote_lib.IMMEDIATE + remote_lib.BACKGROUND:
            self.assertFalse(action.startswith('tv.'), action)

    def test_the_keys_that_are_pressed_in_runs_skip_the_queue(self):
        """Measured at 950ms on the loop's queue against 2ms straight
        through. A direction key that answers a second later is a key that
        gets pressed twice."""
        import remote as remote_lib

        for action in ('tv.press', 'tv.seek', 'tv.text', 'tv.channelup'):
            self.assertIn(action, remote_lib.TV_DIRECT)
        # Building a window is the loop's job.
        self.assertIn('tv.launch', remote_lib.TV_QUEUED)


class TestNoticingOutsideChanges(unittest.TestCase):
    """Kodi gives a script its own interpreter.

    The service that runs the schedule and serves the web remote is one
    process; the menus opened from the add-on are another. Both hold a
    session, and a list read at startup stays read until something says
    otherwise -- so a sequence edited in the menus left the web remote
    showing the old one until Kodi was restarted.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'sequences', 'gui'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app.controller = RecordingController()
        return app

    def write_sequences(self, steps):
        import addon_utils
        import sequences as sequence_lib

        addon_utils.write_json(sequence_lib.SEQUENCE_FILE, [{
            'name': 'Evening',
            # A scene step needs a target to count as filled -- a half-filled
            # slot is normalised back to an empty one.
            'steps': [{'kind': sequence_lib.KIND_SCENE,
                       'target': 'Scene %d' % n}
                      for n in range(steps)],
        }])

    def filled(self, app):
        """How many steps a sequence actually has something in.

        Every sequence is padded to fifteen slots when it is read, so the raw
        length says nothing -- and it is the filled count the web remote puts
        on the card.
        """
        import sequences as sequence_lib

        return len(sequence_lib.filled_steps(app.sequences[0]))

    def test_a_sequence_edited_elsewhere_is_picked_up(self):
        """The reported bug, in one test: seven steps become eight."""
        self.write_sequences(7)
        app = self.app()
        # The service's first pump, which takes the baseline. A first look
        # never reloads anything -- a session that has only just started has
        # not gone stale.
        app.reload_changed()
        self.assertEqual(self.filled(app), 7)

        # Something else -- the menus, in another interpreter -- rewrites it.
        self.write_sequences(8)
        touch_later(app, 'sequences.json')
        app.reload_changed()

        self.assertEqual(self.filled(app), 8)

    def test_an_untouched_file_is_not_re_read(self):
        """Four stat calls a pass is fine; rebuilding every list is not."""
        self.write_sequences(7)
        app = self.app()
        first = app.sequences

        app.reload_changed()

        self.assertIs(app.sequences, first, 're-read a file nothing changed')

    def test_the_first_look_changes_nothing(self):
        """A session that has only just started has not gone stale."""
        self.write_sequences(7)
        app = self.app()

        self.assertEqual(app.reload_changed(), [])

    def test_it_notices_scenes_too(self):
        """The menus can change any of them, not only sequences."""
        import addon_utils
        import scenes as scene_lib

        app = self.app()
        app.reload_changed()

        addon_utils.write_json(scene_lib.SCENE_FILE,
                               [{'name': 'Made elsewhere'}])
        touch_later(app, 'scenes.json')

        self.assertIn('scenes.json', app.reload_changed())
        self.assertIn('Made elsewhere', [s['name'] for s in app.scenes])

    def test_a_rewrite_in_the_same_second_is_still_noticed(self):
        """Which is what happens when two sequences are edited in a row.

        The size is stamped as well as the time for exactly this reason.
        """
        self.write_sequences(7)
        app = self.app()
        app.reload_changed()

        self.write_sequences(9)
        import addon_utils
        import sequences as sequence_lib
        path = addon_utils.profile_file(sequence_lib.SEQUENCE_FILE)
        os.utime(path, (1600000000, 1600000000))
        app._stamps['sequences.json'] = (1600000000, -1)

        self.assertIn('sequences.json', app.reload_changed())

    def test_a_missing_file_is_not_a_crash(self):
        app = self.app()

        self.assertEqual(app.reload_changed(), [])
        self.assertEqual(app.sequences, [])


class TestPowerOnlyDevices(unittest.TestCase):
    """A light this add-on switches but never styles.

    The case: a strip whose colour is set by something else, or set once by
    hand and meant to stay. It should answer to every on and off the house
    does, and no scene should ever repaint it.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'sequences', 'gui'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def hub(self):
        import hub as hub_mod

        return hub_mod.Hub(drivers=[GoveeController()])

    def app(self, satellite=False):
        from paragon_home import ParagonHome

        xbmcaddon.SETTINGS['satellite_mode'] = 'true' if satellite else 'false'
        xbmcaddon.SETTINGS['master_ip'] = '10.0.0.99' if satellite else ''
        app = ParagonHome()
        app.controller = RecordingController()
        return app

    def strip(self, power_only=True):
        return Device('AA:BB', name='Kitchen Lightstrip', lan=True,
                      power_only=power_only)

    def test_it_is_narrowed_to_switching_and_reporting(self):
        hub = self.hub()

        self.assertEqual(sorted(hub.capabilities(self.strip())),
                         [CAP_POWER, CAP_STATE])
        self.assertEqual(
            sorted(hub.capabilities(self.strip(power_only=False))),
            [CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP, CAP_POWER, CAP_STATE])

    def test_a_scene_does_not_count_it_as_a_light(self):
        """Which is what keeps scenes off it: scene_targets only ever
        collects lights, so this falls out of every scene at once rather than
        needing to be excluded from each."""
        hub = self.hub()

        self.assertFalse(scene_lib.is_a_light(self.strip(), hub))
        self.assertTrue(scene_lib.is_a_light(self.strip(power_only=False),
                                             hub))

    def test_a_colour_scene_passes_over_it(self):
        hub = self.hub()
        strip = self.strip()
        bulb = Device('CC:DD', name='Lamp', lan=True)
        scene = scene_lib.make_scene('Sunset', power=scene_lib.POWER_ON,
                                     color=[255, 100, 0], brightness=40)

        targets = scene_lib.scene_targets(scene, [strip, bulb], hub)

        self.assertEqual([d.name for d in targets], ['Lamp'])

    def test_an_all_off_scene_passes_over_it_too(self):
        """A scene is a statement about how the room looks. Switching this one
        is the job of the all-lights control, a sequence or the remote -- the
        same division a plug already lives under."""
        hub = self.hub()
        strip = self.strip()
        scene = scene_lib.make_scene('All Off', power=scene_lib.POWER_OFF)

        self.assertEqual(scene_lib.scene_targets(scene, [strip], hub), [])

    def test_switching_it_still_works(self):
        """The whole point. It comes out of scenes, not out of the house."""
        sent = []
        lights = RecordingController()
        lights.DRIVER_ID = 'govee'
        lights.turn = lambda device, on: sent.append((device.name, on))
        import hub as hub_mod
        hub = hub_mod.Hub(drivers=[lights])

        hub.turn(self.strip(), True)

        self.assertEqual(sent, [('Kitchen Lightstrip', True)])

    def test_a_bulk_colour_command_skips_it_rather_than_failing(self):
        """"Make all the lights red" does not consult capabilities -- it sends
        the same instruction to everything enabled. Skipping quietly is right:
        a colour command that passes over this strip has done what was asked,
        not gone wrong."""
        sent = []
        lights = RecordingController()
        lights.DRIVER_ID = 'govee'
        lights.set_color = lambda device, r, g, b: sent.append(device.name)
        lights.set_brightness = lambda device, percent: sent.append(device.name)
        lights.set_color_temp = lambda device, kelvin: sent.append(device.name)
        import hub as hub_mod
        hub = hub_mod.Hub(drivers=[lights])
        strip = self.strip()

        hub.set_color(strip, 255, 0, 0)
        hub.set_brightness(strip, 50)
        hub.set_color_temp(strip, 4000)

        self.assertEqual(sent, [])

        hub.set_color(self.strip(power_only=False), 255, 0, 0)
        self.assertEqual(sent, ['Kitchen Lightstrip'])

    def test_the_choice_survives_a_refresh(self):
        """Discovery rebuilds the device from what came back off the wire, so
        anything chosen on this side has to be carried across or a refresh
        silently undoes it -- as it would have for the name and the enabled
        flag."""
        app = self.app()
        strip = Device('AA:BB', name='Kitchen Lightstrip', lan=True,
                       ip='10.0.0.9')
        app._devices = [strip]
        app.set_device_power_only(strip, True)
        app.controller.discover = lambda timeout=3.0: (
            [Device('AA:BB', name='H6159 (AABB)', model='H6159', lan=True,
                    ip='10.0.0.9')], [])

        found, _warnings = app.refresh_devices()

        self.assertTrue(found[0].power_only)
        self.assertEqual(found[0].name, 'Kitchen Lightstrip')

    def test_it_is_written_down(self):
        app = self.app()
        strip = Device('AA:BB', name='Kitchen Lightstrip', lan=True)
        app._devices = [strip]

        self.assertTrue(app.set_device_power_only(strip, True))

        import addon_utils

        saved = addon_utils.read_json(devices_mod.DEVICE_CACHE, default=[])
        self.assertTrue(saved[0]['power_only'])

    def test_a_satellite_cannot_change_it(self):
        """It is a choice about the setup, and the master owns those."""
        app = self.app(satellite=True)
        app._devices = [Device('AA:BB', name='Kitchen Lightstrip', lan=True)]

        self.assertFalse(app.set_device_power_only(app.devices[0], True))
        self.assertFalse(app.devices[0].power_only)

    def test_it_round_trips_through_json(self):
        strip = self.strip()
        restored = Device.from_dict(json.loads(json.dumps(strip.to_dict())))
        self.assertTrue(restored.power_only)

    def test_an_older_cache_reads_as_an_ordinary_light(self):
        """Every device written before this existed has no such key."""
        self.assertFalse(Device.from_dict({'device_id': 'AA:BB'}).power_only)


class TestDeviceModel(unittest.TestCase):

    def test_lan_device_gets_a_placeholder_name(self):
        device = Device('1F:80:C5:32:32:36:72:4E', model='H6159', lan=True)
        self.assertEqual(device.name, 'H6159 (724E)')

    def test_merge_prefers_the_cloud_name(self):
        lan = Device('AA:BB', model='H6159', ip='192.168.1.5', lan=True)
        cloud = Device('AA:BB', name='Living Room Strip', model='H6159',
                       cloud=True, supports=['turn', 'brightness'])
        lan.merge(cloud)

        self.assertEqual(lan.name, 'Living Room Strip')
        self.assertTrue(lan.lan and lan.cloud)
        self.assertEqual(lan.ip, '192.168.1.5')
        self.assertEqual(lan.supports, ['turn', 'brightness'])

    def test_merge_keeps_lan_placeholder_when_cloud_has_none(self):
        lan = Device('AA:BB', model='H6159', lan=True)
        other = Device('AA:BB', model='H6159', cloud=True)
        lan.merge(other)
        self.assertEqual(lan.name, 'H6159 (AABB)'.replace('AABB', 'AABB'))

    def test_supports_cmd_defaults_open_for_lan_devices(self):
        self.assertTrue(Device('AA:BB', lan=True).supports_cmd('colorTem'))
        limited = Device('AA:BB', cloud=True, supports=['turn'])
        self.assertTrue(limited.supports_cmd('turn'))
        self.assertFalse(limited.supports_cmd('colorTem'))

    def test_round_trips_through_json(self):
        device = Device('AA:BB', name='Lamp', model='H6159', ip='10.0.0.2',
                        lan=True, cloud=True, supports=['turn'],
                        temp_range=[2200, 6500], enabled=False)
        restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))
        self.assertEqual(restored.to_dict(), device.to_dict())

    def test_auto_mode_prefers_lan(self):
        controller = GoveeController(lan=object(), cloud=None,
                                     mode=devices_mod.TRANSPORT_AUTO)
        both = Device('AA:BB', ip='10.0.0.2', lan=True, cloud=True)
        self.assertEqual(controller.pick_transport(both),
                         devices_mod.TRANSPORT_LAN)

    def test_lan_only_mode_ignores_cloud_devices(self):
        controller = GoveeController(lan=object(), cloud=object(),
                                     mode=devices_mod.TRANSPORT_LAN)
        cloud_only = Device('AA:BB', cloud=True)
        self.assertIsNone(controller.pick_transport(cloud_only))

    def test_cloud_only_mode_ignores_lan_devices(self):
        controller = GoveeController(lan=object(), cloud=object(),
                                     mode=devices_mod.TRANSPORT_CLOUD)
        lan_only = Device('AA:BB', ip='10.0.0.2', lan=True)
        self.assertIsNone(controller.pick_transport(lan_only))

    def test_unreachable_device_raises_a_readable_error(self):
        controller = GoveeController(lan=None, cloud=None)
        with self.assertRaises(ControlError) as caught:
            controller.turn(Device('AA:BB', name='Ghost'), True)
        self.assertIn('Ghost', str(caught.exception))


# ---------------------------------------------------------------------------
# LAN protocol
# ---------------------------------------------------------------------------

class TestDriverData(unittest.TestCase):
    """What a driver must remember about a device has to survive a restart."""

    def test_driver_data_survives_a_round_trip(self):
        device = Device('wp9abc#2', driver='tuya', native_id='wp9abc',
                        driver_data={'version': '3.3', 'dp': '2'})

        restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))

        self.assertEqual(restored.driver_data, {'version': '3.3', 'dp': '2'})
        self.assertEqual(restored.native_id, 'wp9abc')

    def test_a_device_cached_before_driver_data_existed_still_loads(self):
        restored = Device.from_dict({'device_id': 'AA:BB', 'name': 'Lamp'})

        self.assertEqual(restored.driver_data, {})

    def test_a_fresh_discovery_replaces_stale_driver_data(self):
        """A plug that has been re-flashed announces a new version."""
        cached = Device('wp9abc', driver='tuya', driver_data={'version': '3.1'})
        found = Device('wp9abc', driver='tuya', driver_data={'version': '3.3'})

        cached.merge(found)

        self.assertEqual(cached.driver_data['version'], '3.3')


class TestLANMessages(unittest.TestCase):

    def test_turn_message(self):
        self.assertEqual(govee_lan.turn_message(True),
                         {'msg': {'cmd': 'turn', 'data': {'value': 1}}})
        self.assertEqual(govee_lan.turn_message(False)['msg']['data']['value'],
                         0)

    def test_colour_message_zeroes_the_temperature(self):
        data = govee_lan.color_message(1, 2, 3)['msg']['data']
        self.assertEqual(data['color'], {'r': 1, 'g': 2, 'b': 3})
        self.assertEqual(data['colorTemInKelvin'], 0)

    def test_temperature_message_zeroes_the_colour(self):
        data = govee_lan.color_temp_message(2700)['msg']['data']
        self.assertEqual(data['color'], {'r': 0, 'g': 0, 'b': 0})
        self.assertEqual(data['colorTemInKelvin'], 2700)

    def test_scan_message_shape(self):
        self.assertEqual(
            govee_lan.scan_message(),
            {'msg': {'cmd': 'scan', 'data': {'account_topic': 'reserve'}}})


class TestLANTransport(unittest.TestCase):
    """Drives the real transport against a fake light on loopback."""

    def setUp(self):
        self._saved = (govee_lan.MULTICAST_GROUP, govee_lan.SCAN_PORT,
                       govee_lan.LISTEN_PORT, govee_lan.COMMAND_PORT)
        govee_lan.MULTICAST_GROUP = '127.0.0.1'
        govee_lan.SCAN_PORT = TEST_SCAN_PORT
        govee_lan.LISTEN_PORT = TEST_LISTEN_PORT
        govee_lan.COMMAND_PORT = TEST_COMMAND_PORT

        self.scan_device = FakeGoveeDevice('AA:BB:CC:DD', 'H6159',
                                           TEST_SCAN_PORT)
        self.cmd_device = FakeGoveeDevice('AA:BB:CC:DD', 'H6159',
                                          TEST_COMMAND_PORT)
        self.transport = govee_lan.LANTransport(bind_address='127.0.0.1',
                                                retries=1)

    def tearDown(self):
        self.scan_device.close()
        self.cmd_device.close()
        (govee_lan.MULTICAST_GROUP, govee_lan.SCAN_PORT,
         govee_lan.LISTEN_PORT, govee_lan.COMMAND_PORT) = self._saved

    def test_discovery_parses_a_device_reply(self):
        found = self.transport.discover(timeout=1.5)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['device'], 'AA:BB:CC:DD')
        self.assertEqual(found[0]['sku'], 'H6159')
        self.assertEqual(found[0]['ip'], '127.0.0.1')

    def test_control_message_reaches_the_device(self):
        self.assertTrue(self.transport.send('127.0.0.1',
                                            govee_lan.brightness_message(77)))
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.cmd_device.commands('brightness'):
                break
            time.sleep(0.05)
        sent = self.cmd_device.commands('brightness')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['data']['value'], 77)

    def test_status_round_trip(self):
        state = self.transport.status('127.0.0.1', timeout=1.5)
        self.assertIsNotNone(state)
        self.assertEqual(state['onOff'], 1)
        self.assertEqual(state['brightness'], 42)

    def test_retries_send_the_datagram_more_than_once(self):
        transport = govee_lan.LANTransport(bind_address='127.0.0.1', retries=3,
                                           retry_gap=0.01)
        transport.send('127.0.0.1', govee_lan.turn_message(True))
        deadline = time.time() + 2
        while time.time() < deadline:
            if len(self.cmd_device.commands('turn')) >= 3:
                break
            time.sleep(0.05)
        self.assertEqual(len(self.cmd_device.commands('turn')), 3)

    def test_probe_reports_a_successful_sweep(self):
        report = self.transport.probe(timeout=1.5)
        self.assertTrue(report['bound'])
        self.assertIsNone(report['bind_error'])
        self.assertTrue(any(err is None for _l, err in report['attempts']))
        self.assertTrue(report['raw_replies'])
        self.assertEqual(len(report['devices']), 1)
        self.assertEqual(report['devices'][0]['sku'], 'H6159')

    def test_probe_records_raw_replies_it_cannot_parse(self):
        """A non-Govee answer must show up, not be silently dropped."""
        noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(noise.close)

        original = self.scan_device._reply

        def reply_with_noise(address, message):
            original(address, message)
            noise.sendto(b'not json at all', address)

        self.scan_device._reply = reply_with_noise
        report = self.transport.probe(timeout=1.5)

        raw = ' '.join(text for _ip, text in report['raw_replies'])
        self.assertIn('not json at all', raw)
        self.assertEqual(len(report['devices']), 1)

    def test_scan_goes_out_on_more_than_one_path(self):
        """Multicast per interface plus a broadcast fallback."""
        sock = self.transport._make_socket(want_replies=True)
        try:
            attempts = self.transport._send_scan(sock)
        finally:
            sock.close()

        labels = [label for label, _error in attempts]
        self.assertIn('broadcast', labels)
        self.assertTrue(any(label.startswith('multicast') for label in labels))
        self.assertTrue(any(error is None for _label, error in attempts))

    def test_bind_failure_names_the_likely_culprit(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(blocker.close)
        message = self.transport._bind_error(socket.error('in use'))
        self.assertIn('4002'.replace('4002', str(govee_lan.LISTEN_PORT)),
                      message)
        self.assertIn('Govee Desktop', message)

    def test_local_addresses_excludes_loopback_and_link_local(self):
        for address in govee_lan.local_addresses():
            self.assertFalse(address.startswith('127.'), address)
            self.assertFalse(address.startswith('169.254.'), address)

    def test_bulk_status_queries_several_devices_in_one_pass(self):
        """25 bulbs must cost about one timeout, not 25 of them."""
        second = FakeGoveeDevice('EE:FF:00:11', 'H6008', TEST_COMMAND_PORT,
                                 host='127.0.0.2')
        self.addCleanup(second.close)
        second.state = {'onOff': 0, 'brightness': 10,
                        'color': {'r': 1, 'g': 2, 'b': 3},
                        'colorTemInKelvin': 2700}

        started = time.time()
        states = self.transport.status_many(['127.0.0.1', '127.0.0.2'],
                                            timeout=2.0)
        elapsed = time.time() - started

        self.assertEqual(sorted(states.keys()), ['127.0.0.1', '127.0.0.2'])
        self.assertEqual(states['127.0.0.1']['brightness'], 42)
        self.assertEqual(states['127.0.0.2']['colorTemInKelvin'], 2700)
        # Both answered, so it returns as soon as the set is complete rather
        # than sitting out the full window twice.
        self.assertLess(elapsed, 2.0)

    def test_bulk_status_returns_what_it_got_when_one_is_silent(self):
        states = self.transport.status_many(['127.0.0.1', '127.0.0.9'],
                                            timeout=1.0)
        self.assertIn('127.0.0.1', states)
        self.assertNotIn('127.0.0.9', states)

    def test_controller_get_states_maps_replies_onto_devices(self):
        controller = GoveeController(lan=self.transport, cloud=None,
                                     mode=devices_mod.TRANSPORT_LAN)
        device = Device('AA:BB:CC:DD', name='Lamp', lan=True, ip='127.0.0.1')
        ghost = Device('99:99', name='Ghost', lan=True, ip='127.0.0.9')

        states = controller.get_states([device, ghost], timeout=1.0)
        self.assertEqual(states['AA:BB:CC:DD']['power'], 'on')
        self.assertEqual(states['AA:BB:CC:DD']['brightness'], 42)
        self.assertIsNone(states['99:99'])

    def test_silent_lan_devices_do_not_trigger_a_serial_retry_storm(self):
        """Each fallback read re-binds port 4002 and sits out a timeout.

        With 25 bulbs, retrying every miss individually is half a minute of
        the UI apparently hung. status_many already retries internally.
        """
        calls = []
        real_status = self.transport.status

        def counting_status(ip, timeout=2.0):
            calls.append(ip)
            return real_status(ip, timeout=timeout)

        self.transport.status = counting_status
        controller = GoveeController(lan=self.transport, cloud=None,
                                     mode=devices_mod.TRANSPORT_LAN)
        ghosts = [Device('%02d' % i, name='Ghost %d' % i, lan=True,
                         ip='127.0.0.%d' % (20 + i)) for i in range(5)]

        started = time.time()
        states = controller.get_states(ghosts, timeout=0.6)
        elapsed = time.time() - started

        self.assertTrue(all(state is None for state in states.values()))
        self.assertEqual(calls, [], 'silent LAN devices were re-read serially')
        # Two bulk rounds at 0.6s, not five sequential 2s reads.
        self.assertLess(elapsed, 4.0)

    def test_controller_discovery_builds_devices(self):
        controller = GoveeController(lan=self.transport, cloud=None,
                                     mode=devices_mod.TRANSPORT_LAN)
        found, warnings = controller.discover(timeout=1.5)
        self.assertEqual(warnings, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_id, 'AA:BB:CC:DD')
        self.assertTrue(found[0].lan)
        self.assertEqual(found[0].name, 'H6159 (CCDD)')


# ---------------------------------------------------------------------------
# Cloud transport
# ---------------------------------------------------------------------------

class TestCloudTransport(unittest.TestCase):

    def setUp(self):
        self.cloud = FakeCloud()
        self._saved = (govee_cloud.DEVICES_ENDPOINT,
                       govee_cloud.CONTROL_ENDPOINT,
                       govee_cloud.STATE_ENDPOINT)
        govee_cloud.DEVICES_ENDPOINT = self.cloud.base + '/v1/devices'
        govee_cloud.CONTROL_ENDPOINT = self.cloud.base + '/v1/devices/control'
        govee_cloud.STATE_ENDPOINT = self.cloud.base + '/v1/devices/state'
        self.client = govee_cloud.CloudTransport('test-key', min_interval=0)

    def tearDown(self):
        (govee_cloud.DEVICES_ENDPOINT, govee_cloud.CONTROL_ENDPOINT,
         govee_cloud.STATE_ENDPOINT) = self._saved
        self.cloud.close()

    def test_list_devices_and_api_key_header(self):
        self.cloud.route('GET', '/v1/devices', 200, {
            'code': 200, 'message': 'Success',
            'data': {'devices': [{
                'device': 'AA:BB', 'model': 'H6159',
                'deviceName': 'Strip', 'controllable': True,
                'supportCmds': ['turn', 'brightness', 'color'],
                'properties': {'colorTem': {'range': {'min': 2000,
                                                      'max': 9000}}}}]}})
        found = self.client.list_devices()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['deviceName'], 'Strip')
        self.assertEqual(self.cloud.calls[0]['api_key'], 'test-key')

    def test_control_uses_put_with_the_documented_body(self):
        self.cloud.route('PUT', '/v1/devices/control', 200,
                         {'code': 200, 'message': 'Success'})
        self.client.control('AA:BB', 'H6159', 'brightness', 55)

        call = self.cloud.calls[0]
        self.assertEqual(call['method'], 'PUT')
        self.assertEqual(call['body'], {
            'device': 'AA:BB', 'model': 'H6159',
            'cmd': {'name': 'brightness', 'value': 55}})

    def test_state_is_flattened(self):
        self.cloud.route('GET', '/v1/devices/state', 200, {
            'code': 200, 'data': {'properties': [
                {'online': True}, {'powerState': 'on'}, {'brightness': 60}]}})
        state = self.client.state('AA:BB', 'H6159')
        self.assertEqual(state, {'online': True, 'powerState': 'on',
                                 'brightness': 60})

    def test_rate_limit_is_its_own_error(self):
        self.cloud.route('GET', '/v1/devices', 429, {'code': 429,
                                                     'message': 'slow down'})
        with self.assertRaises(govee_cloud.RateLimited):
            self.client.list_devices()

    def test_bad_key_is_reported_clearly(self):
        self.cloud.route('GET', '/v1/devices', 401, {'code': 401,
                                                     'message': 'bad key'})
        with self.assertRaises(govee_cloud.CloudError) as caught:
            self.client.list_devices()
        self.assertIn('API key', str(caught.exception))

    def test_missing_key_never_hits_the_network(self):
        client = govee_cloud.CloudTransport('', min_interval=0)
        self.assertFalse(client.configured)
        with self.assertRaises(govee_cloud.CloudError):
            client.list_devices()
        self.assertEqual(self.cloud.calls, [])

    def test_error_code_in_a_200_body_is_still_an_error(self):
        self.cloud.route('GET', '/v1/devices', 200, {'code': 400,
                                                     'message': 'nope'})
        with self.assertRaises(govee_cloud.CloudError):
            self.client.list_devices()

    def test_controller_merges_cloud_into_lan_results(self):
        self.cloud.route('GET', '/v1/devices', 200, {
            'code': 200, 'data': {'devices': [{
                'device': 'AA:BB:CC:DD', 'model': 'H6159',
                'deviceName': 'Living Room', 'controllable': True,
                'supportCmds': ['turn']}]}})

        class StubLAN(object):
            def discover(self, timeout=3.0):
                return [{'device': 'AA:BB:CC:DD', 'sku': 'H6159',
                         'ip': '10.0.0.9'}]

        controller = GoveeController(lan=StubLAN(), cloud=self.client,
                                     mode=devices_mod.TRANSPORT_AUTO)
        found, warnings = controller.discover()

        self.assertEqual(warnings, [])
        self.assertEqual(len(found), 1)
        device = found[0]
        self.assertEqual(device.name, 'Living Room')
        self.assertEqual(device.ip, '10.0.0.9')
        self.assertTrue(device.lan and device.cloud)

    def test_cloud_failure_does_not_hide_lan_devices(self):
        self.cloud.route('GET', '/v1/devices', 500, {'code': 500,
                                                     'message': 'boom'})

        class StubLAN(object):
            def discover(self, timeout=3.0):
                return [{'device': 'AA:BB', 'sku': 'H6159', 'ip': '10.0.0.9'}]

        controller = GoveeController(lan=StubLAN(), cloud=self.client)
        found, warnings = controller.discover()
        self.assertEqual(len(found), 1)
        self.assertEqual(len(warnings), 1)

    def test_lan_failure_does_not_hide_cloud_devices(self):
        self.cloud.route('GET', '/v1/devices', 200, {
            'code': 200, 'data': {'devices': [{'device': 'CC:DD',
                                               'model': 'H6104',
                                               'deviceName': 'Bar',
                                               'controllable': True}]}})

        class BrokenLAN(object):
            def discover(self, timeout=3.0):
                raise govee_lan.LANError('port 4002 is busy')

        controller = GoveeController(lan=BrokenLAN(), cloud=self.client)
        found, warnings = controller.discover()
        self.assertEqual(len(found), 1)
        self.assertIn('4002', warnings[0])


# ---------------------------------------------------------------------------
# Session: settings, caching, scene seeding
# ---------------------------------------------------------------------------

class TestSession(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        # addon_utils caches module-level state, so reload it per test.
        for name in ('addon_utils', 'paragon_home'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome

        return ParagonHome()

    def test_a_plug_split_into_outlets_drops_its_old_single_entry(self):
        """The pre-split entry is the same hardware; it would switch nothing."""
        app = self.app()
        app._devices = [Device('wp9abc', name='Office Plug', driver='tuya',
                               native_id='wp9abc')]

        outlets = [Device('wp9abc#%s' % dp, name='Outlet %s' % dp,
                          driver='tuya', native_id='wp9abc')
                   for dp in ('1', '2', '3')]
        app.controller.discover = lambda timeout=3.0: (outlets, [])

        listed, _warnings = app.refresh_devices()

        self.assertEqual(sorted(d.device_id for d in listed),
                         ['WP9ABC#1', 'WP9ABC#2', 'WP9ABC#3'])

    def test_a_device_that_merely_missed_a_search_is_still_kept(self):
        """Superseding is narrow: it is not a licence to forget quiet lights."""
        app = self.app()
        app._devices = [Device('AA:BB', name='Hall Lamp')]
        app.controller.discover = lambda timeout=3.0: ([], [])

        listed, _warnings = app.refresh_devices()

        self.assertEqual([d.name for d in listed], ['Hall Lamp'])

    def test_transport_mode_setting_maps_to_constants(self):
        from paragon_home import ParagonHome

        xbmcaddon.SETTINGS['transport_mode'] = '1'
        self.assertEqual(ParagonHome.read_settings()['mode'],
                         devices_mod.TRANSPORT_LAN)

        xbmcaddon.SETTINGS['transport_mode'] = '2'
        self.assertEqual(ParagonHome.read_settings()['mode'],
                         devices_mod.TRANSPORT_CLOUD)

        # Out-of-range values fall back rather than raising.
        xbmcaddon.SETTINGS['transport_mode'] = '99'
        self.assertEqual(ParagonHome.read_settings()['mode'],
                         devices_mod.TRANSPORT_AUTO)

    def test_scenes_are_seeded_and_persisted_on_first_read(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        names = [s['name'] for s in app.scenes]
        self.assertIn('Movie Night', names)
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'scenes.json')))

        # A second session reads them back off disk unchanged.
        app2 = ParagonHome()
        self.assertEqual([s['name'] for s in app2.scenes], names)

    def test_corrupt_scene_file_falls_back_to_defaults(self):
        from paragon_home import ParagonHome

        os.makedirs(PROFILE)
        handle = open(os.path.join(PROFILE, 'scenes.json'), 'w')
        handle.write('{ this is not json')
        handle.close()

        app = ParagonHome()
        self.assertIn('Movie Night', [s['name'] for s in app.scenes])

    def test_device_cache_round_trips(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('AA:BB', name='Lamp', model='H6159',
                               ip='10.0.0.2', lan=True)]
        app.save_devices()

        reloaded = ParagonHome()
        self.assertEqual(len(reloaded.devices), 1)
        self.assertEqual(reloaded.devices[0].name, 'Lamp')
        self.assertEqual(reloaded.device_by_id('aa:bb').ip, '10.0.0.2')

    def test_refresh_preserves_custom_name_and_disabled_flag(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('AA:BB', name='Behind the TV', model='H6159',
                               ip='10.0.0.2', lan=True, enabled=False)]
        app.save_devices()

        class StubController(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', model='H6159', ip='10.0.0.7',
                               lan=True)], []

        app.controller = StubController()
        found, _warnings = app.refresh_devices()

        self.assertEqual(found[0].name, 'Behind the TV')
        self.assertFalse(found[0].enabled)
        self.assertEqual(found[0].ip, '10.0.0.7')

    def test_cloud_names_survive_switching_to_lan_only(self):
        """The exact round trip: name via cloud, then go LAN-only."""
        from paragon_home import ParagonHome

        app = ParagonHome()

        # First refresh with the API key set: the cloud supplies real names.
        class CloudAndLan(object):
            def discover(self, timeout=3.0):
                device = Device('AA:BB', model='H6008', ip='10.0.0.11',
                                lan=True)
                device.merge(Device('AA:BB', name='KITCHEN RIGHT LOW',
                                    model='H6008', cloud=True))
                return [device], []

        app.controller = CloudAndLan()
        app.refresh_devices()
        self.assertEqual(app.devices[0].name, 'KITCHEN RIGHT LOW')

        # Now LAN only: discovery has no names and offers the placeholder.
        class LanOnly(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', model='H6008', ip='10.0.0.11',
                               lan=True)], []

        reopened = ParagonHome()          # reloads devices.json from disk
        reopened.controller = LanOnly()
        reopened.refresh_devices()

        self.assertEqual(reopened.devices[0].name, 'KITCHEN RIGHT LOW')
        self.assertFalse(reopened.devices[0].cloud)
        self.assertTrue(reopened.devices[0].lan)

        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(saved[0]['name'], 'KITCHEN RIGHT LOW')

    def test_a_light_that_misses_one_search_keeps_its_name(self):
        """A sleeping bulb used to be erased along with its name."""
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [
            Device('AA:BB', name='KITCHEN RIGHT LOW', model='H6008',
                   ip='10.0.0.11', lan=True),
            Device('CC:DD', name='BEDROOM FRONT TOP', model='H6008',
                   ip='10.0.0.12', lan=True, enabled=False),
        ]
        app.save_devices()

        class OnlyOneAnswers(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', model='H6008', ip='10.0.0.11',
                               lan=True)], []

        app.controller = OnlyOneAnswers()
        devices, _warnings = app.refresh_devices()

        self.assertEqual(len(devices), 2)
        names = sorted(d.name for d in devices)
        self.assertEqual(names, ['BEDROOM FRONT TOP', 'KITCHEN RIGHT LOW'])
        self.assertEqual(app.last_refresh_missing, 1)

        # The absent one keeps everything, including being disabled.
        absent = app.device_by_id('CC:DD')
        self.assertFalse(absent.enabled)
        self.assertEqual(absent.ip, '10.0.0.12')

    def test_a_forgotten_light_is_gone_from_disk(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        keep = Device('AA:BB', name='Keep', lan=True)
        drop = Device('CC:DD', name='Drop', lan=True)
        app._devices = [keep, drop]
        app.save_devices()

        self.assertTrue(app.forget_device(drop))
        self.assertEqual([d.name for d in app.devices], ['Keep'])
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual([d['name'] for d in saved], ['Keep'])

        # Forgetting something already gone is a no-op, not an error.
        self.assertFalse(app.forget_device(drop))

    def test_placeholder_names_are_still_replaced_by_discovery(self):
        """Preserving names must not freeze a light on its placeholder."""
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('AA:BB', model='H6008', lan=True)]
        self.assertEqual(app.devices[0].name, 'H6008 (AABB)')
        app.save_devices()

        class CloudNames(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', name='GREATROOM BACK TOP',
                               model='H6008', cloud=True)], []

        app.controller = CloudNames()
        app.refresh_devices()
        self.assertEqual(app.devices[0].name, 'GREATROOM BACK TOP')

    def test_apply_scene_by_name_reports_a_missing_scene(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.assertFalse(app.apply_scene_by_name('Does Not Exist'))
        self.assertTrue(any('Does Not Exist' in message
                            for _heading, message in xbmcgui.NOTIFICATIONS))

    def test_toggle_turns_the_group_off_when_any_light_is_on(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        one = Device('ONE', name='One', lan=True, ip='10.0.0.1')
        two = Device('TWO', name='Two', lan=True, ip='10.0.0.2')
        app._devices = [one, two]

        recorder = RecordingController()
        recorder.get_state = lambda d: {'power': 'on'} if d is one else \
            {'power': 'off'}
        app.controller = recorder

        done, errors = app.toggle_all()
        self.assertEqual(done, 2)
        self.assertEqual(errors, [])
        self.assertTrue(all(call[2] is False for call in recorder.calls))

    def test_toggle_turns_on_when_no_state_can_be_read(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('ONE', name='One', lan=True, ip='10.0.0.1')]
        recorder = RecordingController()
        recorder.get_state = lambda d: None
        app.controller = recorder

        app.toggle_all()
        self.assertTrue(all(call[2] is True for call in recorder.calls))

    def test_notifications_can_be_switched_off(self):
        import addon_utils

        xbmcaddon.SETTINGS['show_notifications'] = 'false'
        addon_utils.notify('quiet please')
        self.assertEqual(xbmcgui.NOTIFICATIONS, [])

        # Errors still get through, because they need acting on.
        addon_utils.force_notify('this one matters')
        self.assertEqual(len(xbmcgui.NOTIFICATIONS), 1)

    def test_settings_helpers_coerce_types(self):
        import addon_utils

        xbmcaddon.SETTINGS.update({'a': 'true', 'b': 'False', 'c': '12',
                                   'd': 'not a number'})
        self.assertTrue(addon_utils.get_bool('a'))
        self.assertFalse(addon_utils.get_bool('b'))
        self.assertTrue(addon_utils.get_bool('missing', True))
        self.assertEqual(addon_utils.get_int('c'), 12)
        self.assertEqual(addon_utils.get_int('d', 7), 7)
        self.assertEqual(addon_utils.get_int('missing', 5), 5)


# ---------------------------------------------------------------------------
# RunScript argument handling
# ---------------------------------------------------------------------------

class TestCollapsingTargets(unittest.TestCase):
    """Folding several targets into one command, where that is safe."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def plug(self):
        master = Device('wp9abc#all', name='Office Plug', driver='tuya',
                        native_id='wp9abc',
                        driver_data={'dp': 'all', 'members': ['1', '2']})
        outlets = [Device('wp9abc#%s' % dp, name='Outlet %s' % dp,
                          driver='tuya', native_id='wp9abc',
                          driver_data={'dp': dp}) for dp in ('1', '2')]
        return master, outlets

    def app(self, devices):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = devices
        return app

    def test_a_bulk_switch_sends_one_command_per_plug(self):
        master, outlets = self.plug()
        app = self.app([master] + outlets)
        switched = []
        app.controller.turn = lambda device, on: switched.append(device.name)

        done, errors = app.power_all(False)

        self.assertEqual(switched, ['Office Plug'])
        self.assertEqual((done, errors), (1, []))

    def test_a_hub_with_no_collapsing_driver_passes_the_list_through(self):
        from hub import Hub

        class Plain(object):
            DRIVER_ID = 'plain'
            DRIVER_LABEL = 'Plain'

        devices = [Device('AA:BB'), Device('CC:DD')]
        self.assertEqual(Hub([Plain()]).collapse(devices), devices)

    def test_a_scene_does_not_reach_a_plug_at_all(self):
        """Collapsing outlets was the worry while scenes could switch plugs.

        They no longer can, named or not, which settles the question rather
        more firmly than not folding them would have. Bulk switching -- the
        collapsing path above -- is where a multi-outlet plug is handled.
        """
        import scenes as scenes_mod

        master, outlets = self.plug()
        app = self.app([master] + outlets)
        switched = []
        app.controller.turn = lambda device, on: switched.append(device.name)
        app.controller.capabilities = lambda device: set(['power', 'state'])

        # Named explicitly: "all" now means the lights, and these are plugs.
        scene = scenes_mod.make_scene(
            'Bedtime', power=scenes_mod.POWER_OFF,
            targets=[d.device_id for d in [master] + outlets])
        app.apply_scene(scene, announce=False)

        self.assertEqual(switched, [])


class TestScriptArguments(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'default'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_parses_comma_and_ampersand_separators(self):
        import default

        self.assertEqual(default.parse_args(['action=toggle']),
                         {'action': 'toggle'})
        self.assertEqual(
            default.parse_args(['action=scene', 'name=Movie Night']),
            {'action': 'scene', 'name': 'Movie Night'})
        self.assertEqual(
            default.parse_args(['action=pick_scene&setting=scene_playing']),
            {'action': 'pick_scene', 'setting': 'scene_playing'})
        self.assertEqual(default.parse_args([]), {})
        self.assertEqual(default.parse_args(['garbage']), {})

    def test_hex_parsing_accepts_short_and_long_forms(self):
        """The colour a keymap names is read the same way everywhere.

        Reads the session's resolver rather than a private helper in
        default.py: the web remote calls the same one, and a test pinned to
        one caller's copy would not notice the two diverging.
        """
        from paragon_home import ParagonHome

        app = ParagonHome()

        self.assertEqual(app.resolve_color('#FF8800'), (255, 136, 0))
        self.assertEqual(app.resolve_color('f80'), (255, 136, 0))
        self.assertIsNone(app.resolve_color('nope'))
        self.assertIsNone(app.resolve_color(''))

    def test_command_action_fires_a_learned_code(self):
        """The same verb the web remote uses, so the two cannot drift."""
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('EE:FF', name='Hall RM', driver='broadlink',
                               lan=True)]
        recorder = RecordingController()
        recorder.command_map = {'EE:FF': ['TV Power']}
        app.controller = recorder

        default.run_action(app, {'action': 'command', 'target': 'Hall RM',
                                 'name': 'TV Power'}, addon_utils)

        self.assertEqual(recorder.calls, [('command', 'EE:FF', 'TV Power')])

    def test_command_action_will_not_fire_at_everything(self):
        """A learned code belongs to the blaster that learned it."""
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('EE:FF', name='Hall RM', driver='broadlink',
                               lan=True)]
        recorder = RecordingController()
        recorder.command_map = {'EE:FF': ['TV Power']}
        app.controller = recorder

        default.run_action(app, {'action': 'command', 'name': 'TV Power'},
                           addon_utils)

        self.assertEqual(recorder.calls, [])

    def test_target_resolves_by_name_and_by_id(self):
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('AA:BB', name='Living Room', lan=True)]

        self.assertIsNone(default.resolve_targets(app, 'all'))
        self.assertIsNone(default.resolve_targets(app, ''))
        self.assertEqual(len(default.resolve_targets(app, 'living room')), 1)
        self.assertEqual(len(default.resolve_targets(app, 'aa:bb')), 1)
        self.assertEqual(default.resolve_targets(app, 'nowhere'), [])

    def test_unknown_target_is_reported_and_nothing_is_sent(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Living Room', lan=True)]
        app.controller = recorder

        handled = default.run_action(app, {'action': 'on', 'target': 'nope'},
                                     addon_utils)
        self.assertTrue(handled)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(any('nope' in message
                            for _h, message in xbmcgui.NOTIFICATIONS))

    def test_actions_drive_the_controller(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'on'}, addon_utils)
        default.run_action(app, {'action': 'brightness', 'value': '25'},
                           addon_utils)
        default.run_action(app, {'action': 'color', 'value': 'FF8800'},
                           addon_utils)
        default.run_action(app, {'action': 'temp', 'value': '2700'},
                           addon_utils)

        self.assertEqual([c[0] for c in recorder.calls],
                         ['turn', 'brightness', 'color', 'temp'])
        self.assertEqual(recorder.calls[1][2], 25)
        self.assertEqual(recorder.calls[2][2:], (255, 136, 0))
        self.assertEqual(recorder.calls[3][2], 2700)

    def test_color_action_accepts_a_saved_colour_name(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder
        app.save_color('Govee Pink', (255, 40, 150))

        default.run_action(app, {'action': 'color', 'value': 'Govee Pink'},
                           addon_utils)
        self.assertEqual(recorder.calls, [('color', 'AA:BB', 255, 40, 150)])

    def test_color_action_rejects_a_name_that_is_not_saved(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'color', 'value': 'Nonexistent'},
                           addon_utils)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(xbmcgui.NOTIFICATIONS)

    def test_out_of_range_values_are_clamped(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'brightness', 'value': '900'},
                           addon_utils)
        self.assertEqual(recorder.calls[0][2], 100)

    def test_bad_value_is_reported_not_sent(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'brightness', 'value': 'loads'},
                           addon_utils)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(xbmcgui.NOTIFICATIONS)

    def test_no_action_falls_through_to_the_panel(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.assertFalse(default.run_action(app, {}, addon_utils))
        self.assertFalse(default.run_action(app, {'action': 'panel'},
                                            addon_utils))


# ---------------------------------------------------------------------------
# Playback service decisions
# ---------------------------------------------------------------------------

class TestSatelliteMode(unittest.TestCase):
    """One box owns the setup; the others copy it and run no schedule."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'satellite', 'scenes',
                     'sequences', 'reracks', 'palette'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def master(self, **files):
        """An ssh runner standing in for a master box."""
        served = {
            'devices.json': json.dumps([{'device_id': 'AA:BB',
                                         'name': 'Master Lamp',
                                         'driver': 'govee'}]),
            'scenes.json': json.dumps([{'name': 'Warshade'}]),
            'sequences.json': json.dumps([{'name': 'Ignition'}]),
        }
        served.update(files)
        self.asked = []

        def run(command):
            self.asked.append(command)
            remote = command[-1]
            for name, body in served.items():
                if remote.endswith(name):
                    if body is None:
                        raise RuntimeError('cat: no such file')
                    return body
            raise RuntimeError('cat: no such file')

        return run

    def app(self, satellite=True, master_ip='10.0.0.99'):
        from paragon_home import ParagonHome

        xbmcaddon.SETTINGS['satellite_mode'] = 'true' if satellite else 'false'
        xbmcaddon.SETTINGS['master_ip'] = master_ip
        app = ParagonHome()
        app.controller = RecordingController()
        return app

    # -- copying -----------------------------------------------------------

    def test_it_copies_the_masters_devices_scenes_and_sequences(self):
        app = self.app()

        copied, problems = app.sync_from_master(run=self.master())

        self.assertEqual(problems, [])
        self.assertEqual(sorted(copied),
                         ['devices.json', 'scenes.json', 'sequences.json'])
        self.assertEqual([d.name for d in app.devices], ['Master Lamp'])
        self.assertEqual([s['name'] for s in app.scenes], ['Warshade'])
        self.assertEqual([s['name'] for s in app.sequences], ['Ignition'])

    def test_it_reads_over_ssh_without_a_password_prompt(self):
        """BatchMode, or a missing key hangs the service instead of failing."""
        app = self.app()

        app.sync_from_master(run=self.master())

        command = self.asked[0]
        self.assertEqual(command[0], 'ssh')
        self.assertIn('BatchMode=yes', command)
        self.assertIn('root@10.0.0.99', command)
        self.assertIn('script.paragon.home', command[-1])

    def test_it_never_writes_to_the_master(self):
        app = self.app()

        app.sync_from_master(run=self.master())

        for command in self.asked:
            self.assertTrue(command[-1].startswith('cat '),
                            'not a read: %s' % command[-1])

    def test_a_master_that_cannot_be_reached_leaves_what_is_here(self):
        """The lights still work; they are just a little out of date."""
        app = self.app()
        app.sync_from_master(run=self.master())

        def unreachable(command):
            raise RuntimeError('ssh: connect to host 10.0.0.99: timed out')

        copied, problems = app.sync_from_master(run=unreachable)

        self.assertEqual(copied, [])
        self.assertTrue(problems)
        self.assertEqual([d.name for d in app.devices], ['Master Lamp'])

    def test_a_truncated_file_does_not_overwrite_a_good_one(self):
        """Half a scene list is worse than yesterday's whole one."""
        app = self.app()
        app.sync_from_master(run=self.master())

        copied, problems = app.sync_from_master(
            run=self.master(**{'scenes.json': '[{"name": "Warsh'}))

        self.assertNotIn('scenes.json', copied)
        self.assertTrue([p for p in problems if 'scenes.json' in p])
        self.assertEqual([s['name'] for s in app.scenes], ['Warshade'])

    def test_a_house_with_no_tuya_keys_is_not_a_problem(self):
        """Absent is the normal case, not a failure worth showing."""
        app = self.app()

        copied, problems = app.sync_from_master(
            run=self.master(**{'tuya_keys.json': None}))

        self.assertEqual(problems, [])
        self.assertNotIn('tuya_keys.json', copied)

    def test_with_no_master_address_it_says_so(self):
        app = self.app(master_ip='')

        copied, problems = app.sync_from_master(run=self.master())

        self.assertEqual(copied, [])
        self.assertIn('master', problems[0].lower())

    def test_a_master_box_does_not_copy_from_anything(self):
        app = self.app(satellite=False)

        copied, problems = app.sync_from_master(run=self.master())

        self.assertEqual(copied, [])
        self.assertEqual(self.asked, [])

    # -- what a satellite does not do --------------------------------------

    def test_a_satellite_runs_no_scheduled_sequence(self):
        """Three boxes on one schedule would send every step three times."""
        import sequences as sequence_lib

        app = self.app()
        app._sequences = [sequence_lib.make_sequence(
            'Ignition', time='18:00', days=[5])]
        saturday = datetime.datetime(2026, 8, 22, 18, 0)

        self.assertEqual(app.run_due_sequences(now=saturday), [])

    def test_the_same_sequence_does_run_on_the_master(self):
        """The other half: this is a satellite rule, not a broken schedule."""
        import sequences as sequence_lib

        app = self.app(satellite=False)
        app._sequences = [sequence_lib.make_sequence(
            'Ignition', time='18:00', days=[5])]
        saturday = datetime.datetime(2026, 8, 22, 18, 0)

        self.assertEqual(app.run_due_sequences(now=saturday), ['Ignition'])

    def _with_a_phase_due(self, satellite=True):
        """A rerack whose phase 2 is due at 07:00 on the Saturday below."""
        import reracks as rerack_lib
        import sequences as sequence_lib

        app = self.app(satellite=satellite)
        app._reracks = rerack_lib.normalise_all([rerack_lib.make_rerack(
            'Alpha', [{}, {'sequence': 'Curtain Up', 'time': '07:00'}])])
        app._week = ['Alpha'] * 7
        app._phase_state = set()
        app._sequences = [sequence_lib.make_sequence('Curtain Up')]
        return app

    def test_a_satellite_runs_no_rerack_phase(self):
        app = self._with_a_phase_due()

        self.assertEqual(
            app.run_due_phases(now=datetime.datetime(2026, 8, 22, 7, 0)), [])

    def test_the_same_phase_does_run_on_the_master(self):
        """Proves the phase really was due, so the test above is not vacuous."""
        app = self._with_a_phase_due(satellite=False)

        self.assertEqual(
            app.run_due_phases(now=datetime.datetime(2026, 8, 22, 7, 0)),
            ['Alpha phase 2'])

    def test_asked_by_hand_a_satellite_still_runs_it(self):
        """It follows the master's schedule, not the master's remote control."""
        import sequences as sequence_lib

        app = self.app()
        scene = scene_lib.make_scene('Warshade', power=scene_lib.POWER_OFF)
        app._scenes = [scene]
        app._devices = [Device('AA:BB', name='Lamp', driver='govee', lan=True)]
        app.controller.capabilities = lambda d: set(
            ['power', 'brightness', 'color', 'color_temp', 'state'])

        ran = app.run_sequence(sequence_lib.make_sequence(
            'By Hand', [{'kind': 'scene', 'target': 'Warshade'}]),
            announce=False)

        self.assertTrue(ran)
        self.assertTrue(app.controller.calls)

    # -- the menu ----------------------------------------------------------

    # -- what a satellite does not own -------------------------------------
    #
    # Everything in SHARED_FILES is overwritten by the master's copy at
    # start-up and every few minutes, and that copy has no idea anything was
    # edited here. So a satellite must refuse to write them at all: accepting
    # the change and deleting it a quarter of an hour later, silently, is the
    # worst of the available answers.

    def test_a_satellite_cannot_save_a_sequence(self):
        import sequences as sequence_lib

        app = self.app()

        self.assertIsNone(
            app.save_sequence(sequence_lib.make_sequence('Ignition')))
        self.assertEqual(app.sequences, [])

    def test_a_refused_sequence_is_not_left_in_memory(self):
        """Half-saving would show it in the menu and lose it at the restart."""
        import sequences as sequence_lib

        app = self.app()
        app._sequences = []

        app.save_sequence(sequence_lib.make_sequence('Ignition'))

        self.assertEqual([s['name'] for s in app.sequences], [])
        self.assertIsNone(utils_read('sequences.json'))

    def test_a_satellite_cannot_delete_a_sequence_it_was_given(self):
        app = self.app()
        app._sequences = [{'name': 'Ignition', 'steps': []}]

        self.assertFalse(app.delete_sequence({'name': 'Ignition'}))
        self.assertEqual([s['name'] for s in app.sequences], ['Ignition'])

    def test_a_satellite_cannot_save_a_scene(self):
        app = self.app()
        before = len(app.scenes)

        self.assertIsNone(app.save_scene({'name': 'Warshade'}))
        self.assertEqual(len(app.scenes), before)

    def test_a_satellite_cannot_rename_or_forget_a_device(self):
        app = self.app()
        app._devices = [Device('AA:BB', name='Master Lamp', lan=True)]

        self.assertFalse(app.rename_device(app.devices[0], 'My Lamp'))
        self.assertFalse(app.set_device_enabled(app.devices[0], False))
        self.assertFalse(app.forget_device(app.devices[0]))

        self.assertEqual(app.devices[0].name, 'Master Lamp')
        self.assertTrue(app.devices[0].enabled)
        self.assertEqual(len(app.devices), 1)

    def test_a_satellite_cannot_edit_the_palette(self):
        app = self.app()
        before = [entry['name'] for entry in app.palette]

        self.assertIsNone(app.save_color('Ember', (255, 90, 26)))
        self.assertFalse(app.remove_color(app.palette[0]))
        self.assertFalse(app.reset_palette())

        self.assertEqual([entry['name'] for entry in app.palette], before)

    def test_a_satellite_says_where_to_type_a_tuya_key(self):
        """Typed here it would be overwritten by the master's copy."""
        app = self.app()
        device = Device('wp9abc', name='Office Plug', driver='tuya')
        app._devices = [device]

        try:
            app.set_local_key(device, '0123456789abcdef')
        except ControlError as exc:
            self.assertIn('master', str(exc))
        else:
            self.fail('a satellite accepted a Tuya key')

    def test_a_satellite_still_refreshes_its_device_cache(self):
        """The one shared file it may write: that one is a cache, not a choice.

        Blocking it would leave a satellite unable to learn the addresses of
        the very lights the master told it about.
        """
        app = self.app()
        app._devices = [Device('AA:BB', name='Master Lamp', lan=True)]

        self.assertTrue(app.save_devices())

    def test_a_satellite_cannot_learn_or_delete_a_command(self):
        """The codes come down with everything else, so it only fires them."""
        app = self.app()
        device = Device('EE:FF', name='Hall RM', driver='broadlink')
        app._devices = [device]

        self.assertFalse(app.save_command(device, 'TV Power', 'abcd'))
        self.assertFalse(app.forget_command(device, 'TV Power'))
        self.assertFalse(app.save_codes())

        try:
            app.start_learning(device)
        except ControlError as exc:
            self.assertIn('master', str(exc))
        else:
            self.fail('a satellite went into learning mode')

        # Radio the same way. The guard sits on the sweep, which is the
        # first thing a radio learn does, so it never reaches the blaster.
        try:
            app.start_rf_sweep(device)
        except ControlError as exc:
            self.assertIn('master', str(exc))
        else:
            self.fail('a satellite started an RF sweep')

    def test_learned_commands_travel_down_with_everything_else(self):
        """A sequence step firing a code fails on a box that has not got it."""
        app = self.app()

        copied, _problems = app.sync_from_master(run=self.master(**{
            'broadlink_codes.json': json.dumps(
                {'EE:FF': {'TV Power': 'abcd'}})}))

        self.assertIn('broadlink_codes.json', copied)
        self.assertEqual(app._codes, {'EE:FF': {'TV Power': 'abcd'}})

    def test_a_house_with_no_blaster_is_not_a_problem_to_report(self):
        app = self.app()

        copied, problems = app.sync_from_master(
            run=self.master(**{'broadlink_codes.json': None}))

        self.assertNotIn('broadlink_codes.json', copied)
        self.assertEqual(problems, [])

    def test_a_sync_reaches_the_drivers_and_not_just_the_session(self):
        """The hub was handed these very dicts; rebinding orphans them."""
        app = self.app()
        codes, keys = app._codes, app._tuya_keys

        app.sync_from_master(run=self.master(**{
            'broadlink_codes.json': json.dumps({'EE:FF': {'TV': 'abcd'}}),
            'tuya_keys.json': json.dumps({'wp9abc': '0123456789abcdef'})}))

        # Still the same objects the drivers are holding, with the new
        # contents in them.
        self.assertIs(app._codes, codes)
        self.assertIs(app._tuya_keys, keys)
        self.assertEqual(codes, {'EE:FF': {'TV': 'abcd'}})
        self.assertEqual(keys, {'wp9abc': '0123456789abcdef'})

    def test_a_satellite_does_not_search_for_devices_of_its_own(self):
        """Discovery invents entries; the master decides what the house has."""
        app = self.app()
        app._devices = [Device('AA:BB', name='Master Lamp', lan=True)]
        app.controller = RecordingController()

        found, warnings = app.refresh_devices()

        self.assertEqual(found, [])
        self.assertIn('master', warnings[0])
        self.assertEqual(app.controller.calls, [])

    # -- and the master still can ------------------------------------------

    def test_the_master_can_do_all_of_it(self):
        """Or the guard would be a way of breaking the box that works."""
        import sequences as sequence_lib

        app = self.app(satellite=False)
        app._devices = [Device('AA:BB', name='Master Lamp', lan=True)]

        self.assertIsNotNone(
            app.save_sequence(sequence_lib.make_sequence('Ignition')))
        self.assertIsNotNone(app.save_scene({'name': 'Warshade'}))
        self.assertIsNotNone(app.save_color('Ember', (255, 90, 26)))
        self.assertTrue(app.rename_device(app.devices[0], 'My Lamp'))
        self.assertTrue(app.delete_sequence({'name': 'Ignition'}))
        self.assertTrue(app.save_codes())

    # -- and the menus do not offer what cannot be done --------------------

    def test_the_menu_offers_no_sequence_editing_on_a_satellite(self):
        import gui

        app = self.app()
        app._sequences = [{'name': 'Ignition', 'steps': []}]
        labels = menu_row(lambda: gui.ControlPanel(app).sequence_menu(),
                          'Ignition')[1]

        self.assertFalse([row for row in labels
                          if row.startswith(('New sequence', 'Manage'))],
                         'editing offered on a satellite: %s' % labels)

    def test_the_menu_offers_sequence_editing_on_the_master(self):
        import gui

        app = self.app(satellite=False)
        labels = menu_row(lambda: gui.ControlPanel(app).sequence_menu(),
                          'New sequence')[1]

        self.assertTrue(labels)

    def test_the_menu_offers_no_scene_editing_on_a_satellite(self):
        import gui

        app = self.app()
        labels = menu_row(lambda: gui.ControlPanel(app).scene_menu(),
                          'All Off')[1]

        self.assertFalse([row for row in labels
                          if row.startswith(('Capture', 'Manage'))],
                         'editing offered on a satellite: %s' % labels)

    def test_the_device_menu_offers_no_edits_on_a_satellite(self):
        import gui

        app = self.app()
        device = Device('AA:BB', name='Master Lamp', lan=True)
        app._devices = [device]
        app.controller = RecordingController()
        labels = menu_row(lambda: gui.ControlPanel(app).device_menu(device),
                          'Show status')[1]

        self.assertFalse([row for row in labels
                          if row.startswith(('Rename', 'Forget', 'Disable',
                                             'Enable', 'Set local key'))],
                         'editing offered on a satellite: %s' % labels)

    def test_the_menu_offers_a_copy_rather_than_a_search_on_a_satellite(self):
        import gui

        app = self.app()
        app._devices = []
        labels = menu_row(lambda: gui.ControlPanel(app).main_menu(),
                          'Copy from the master')[1]

        self.assertFalse([row for row in labels
                          if row.startswith('Refresh devices')],
                         'a satellite offered a device search: %s' % labels)

    def test_the_command_menu_offers_no_learning_on_a_satellite(self):
        import gui

        app = self.app()
        device = Device('EE:FF', name='Hall RM', driver='broadlink')
        app._devices = [device]
        recorder = RecordingController()
        recorder.command_map = {'EE:FF': ['TV Power']}
        app.controller = recorder
        labels = menu_row(lambda: gui.ControlPanel(app).command_menu(device),
                          'TV Power')[1]

        self.assertIn('Test connection', labels)
        self.assertFalse([row for row in labels if row.startswith('Learn')],
                         'learning offered on a satellite: %s' % labels)

    def test_the_menu_offers_no_reracks_on_a_satellite(self):
        import gui

        app = self.app()
        app._devices = []
        labels = menu_row(lambda: gui.ControlPanel(app).main_menu(),
                          'Satellite')[1]

        self.assertFalse([row for row in labels if row.startswith('Rerack')],
                         'reracks offered on a satellite: %s' % labels)

    def test_the_menu_offers_reracks_on_the_master(self):
        import gui

        app = self.app(satellite=False)
        app._devices = []
        labels = menu_row(lambda: gui.ControlPanel(app).main_menu(),
                          'Reracks...')[1]

        self.assertFalse([row for row in labels if row.startswith('Satellite')])


class TestPlaybackService(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        import xbmc
        xbmc.Player.playing_video = False
        xbmc.Player.playing_audio = False
        xbmc.COND_VISIBILITY.clear()
        for name in ('addon_utils', 'paragon_home', 'service'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_the_remote_stays_off_unless_it_is_switched_on(self):
        import service

        svc = service.GoveeService()

        self.assertFalse(svc._apply_remote_settings())
        self.assertIsNone(svc._remote)

    def test_the_remote_starts_and_stops_with_its_setting(self):
        import service

        xbmcaddon.SETTINGS['remote_enabled'] = 'true'
        xbmcaddon.SETTINGS['remote_port'] = str(_free_port())
        xbmcaddon.SETTINGS['remote_pin'] = '424242'
        svc = service.GoveeService()

        try:
            self.assertTrue(svc._apply_remote_settings())
            self.assertTrue(svc._remote.running())

            xbmcaddon.SETTINGS['remote_enabled'] = 'false'
            self.assertFalse(svc._apply_remote_settings())
            self.assertIsNone(svc._remote)
        finally:
            if svc._remote is not None:
                svc._remote.stop()

    def test_an_unrelated_settings_change_does_not_restart_the_remote(self):
        """Restarting it would sign every phone in the house out for nothing."""
        import service

        xbmcaddon.SETTINGS['remote_enabled'] = 'true'
        xbmcaddon.SETTINGS['remote_port'] = str(_free_port())
        xbmcaddon.SETTINGS['remote_pin'] = '424242'
        svc = service.GoveeService()

        try:
            svc._apply_remote_settings()
            first = svc._remote

            xbmcaddon.SETTINGS['debug_logging'] = 'true'
            svc._apply_remote_settings()

            self.assertIs(svc._remote, first)
            self.assertTrue(svc._remote.running())

            # A new PIN is a different matter: it must sign everyone out.
            xbmcaddon.SETTINGS['remote_pin'] = '999999'
            svc._apply_remote_settings()

            self.assertIsNot(svc._remote, first)
            self.assertFalse(first.running())
        finally:
            if svc._remote is not None:
                svc._remote.stop()

    def test_a_settings_change_is_noted_rather_than_acted_on(self):
        """onSettingsChanged runs on Kodi's thread, not the loop's."""
        import service

        svc = service.GoveeService()
        svc._remote_stale = False

        svc.onSettingsChanged()

        self.assertTrue(svc._remote_stale)
        self.assertIsNone(svc._remote)

    def test_every_tick_drains_the_remote_queue(self):
        """Including the ticks inside a sequence pause -- that is the point."""
        import service

        svc = service.GoveeService()

        class StubRemote(object):
            def __init__(self):
                self.pumps = 0

            def pump(self, app, sleep_func=None, on_step=None):
                self.pumps += 1
                return 0

        stub = StubRemote()
        svc._remote = stub

        class StubApp(object):
            def __init__(self):
                self.reloads = 0

            @staticmethod
            def cycle_due():
                return False

            def reload_changed(self):
                # The pump asks the session what has changed underneath it
                # before draining the queue. A stub that does not answer is a
                # stub pretending to be a session it is not.
                self.reloads += 1
                return []

        app = StubApp()
        svc._app = app
        svc._tick()
        svc._tick()

        self.assertEqual(stub.pumps, 2)
        self.assertEqual(app.reloads, 2, 'never asked what had changed')

    def test_a_scheduled_sequence_stops_the_remote_starting_another(self):
        """Two sequences interleaving is the thing v2.22 went to lengths over."""
        import remote as remote_lib
        import service

        svc = service.GoveeService()
        server = remote_lib.RemoteServer(gate=remote_lib.Gate('424242', 'tok'))
        svc._remote = server
        seen = []

        class StubApp(object):
            @staticmethod
            def run_due_sequences(**kwargs):
                seen.append(server.sequence_running)
                return []

            @staticmethod
            def run_due_phases(**kwargs):
                seen.append(server.sequence_running)
                return []

        svc._app = StubApp()

        svc._check_sequences(now=10000.0)

        self.assertEqual(seen, [True, True])
        # And released afterwards, or the remote would refuse sequences for
        # the rest of the Kodi session.
        self.assertFalse(server.sequence_running)

    def test_a_pause_keeps_stepping_a_cycle(self):
        """An hour-long pause must not freeze everything else for an hour."""
        import service

        svc = service.GoveeService()
        ticks = []
        svc._tick = lambda: ticks.append(1)
        waits = []
        svc.waitForAbort = lambda seconds: waits.append(seconds) or False

        aborted = svc._pause(2.0)

        self.assertFalse(aborted)
        # Sliced, not one long block, with the loop's work done between.
        self.assertEqual(waits, [0.5, 0.5, 0.5, 0.5])
        self.assertEqual(len(ticks), 4)

    def test_a_pause_gives_up_promptly_when_kodi_is_closing(self):
        import service

        svc = service.GoveeService()
        svc._tick = lambda: None
        calls = []

        def closing(seconds):
            calls.append(seconds)
            return len(calls) >= 2

        svc.waitForAbort = closing

        self.assertTrue(svc._pause(3600))
        # Two slices, not two hours of them.
        self.assertEqual(len(calls), 2)

    def test_a_pause_is_counted_as_time_the_service_could_not_look(self):
        import service

        svc = service.GoveeService()
        svc._tick = lambda: None
        svc.waitForAbort = lambda seconds: False

        self.assertEqual(svc._blocked_for, 0.0)
        svc._pause(1.0)
        self.assertGreater(svc._blocked_for, 0.0)

    def test_time_spent_aborting_a_pause_still_counts(self):
        """The finally clause matters: an interrupted pause was still time."""
        import service

        svc = service.GoveeService()
        svc._tick = lambda: None
        svc.waitForAbort = lambda seconds: True

        svc._pause(3600)
        self.assertGreaterEqual(svc._blocked_for, 0.0)

    def test_content_filter_video_only(self):
        import xbmc
        import service

        xbmcaddon.SETTINGS['sync_content'] = '0'
        xbmc.Player.playing_audio = True
        self.assertFalse(service.GoveeService._content_allowed())

        xbmc.Player.playing_video = True
        self.assertTrue(service.GoveeService._content_allowed())

    def test_content_filter_video_and_music(self):
        import xbmc
        import service

        xbmcaddon.SETTINGS['sync_content'] = '1'
        xbmc.Player.playing_audio = True
        self.assertTrue(service.GoveeService._content_allowed())

    def test_content_filter_everything_needs_no_player(self):
        import service

        xbmcaddon.SETTINGS['sync_content'] = '2'
        self.assertTrue(service.GoveeService._content_allowed())

    def test_fullscreen_filter(self):
        import xbmc
        import service

        xbmcaddon.SETTINGS['sync_fullscreen_only'] = 'false'
        self.assertTrue(service.GoveeService._fullscreen_ok())

        xbmcaddon.SETTINGS['sync_fullscreen_only'] = 'true'
        self.assertFalse(service.GoveeService._fullscreen_ok())

        xbmc.COND_VISIBILITY['VideoPlayer.IsFullscreen'] = True
        self.assertTrue(service.GoveeService._fullscreen_ok())

    def test_stop_does_not_restore_when_we_never_dimmed(self):
        import service

        xbmcaddon.SETTINGS.update({'playback_sync': 'true',
                                   'scene_stopped': 'Lights Up'})
        svc = service.GoveeService()
        applied = []
        svc._app = type('App', (), {
            'apply_scene_by_name': lambda self, name, announce=True:
                applied.append(name) or True})()

        svc._we_dimmed = False
        svc.handle(service.EVENT_STOP)
        self.assertEqual(applied, [])

    def test_stop_restores_after_we_dimmed(self):
        import service

        xbmcaddon.SETTINGS.update({'playback_sync': 'true',
                                   'scene_stopped': 'Lights Up'})
        svc = service.GoveeService()
        applied = []
        svc._app = type('App', (), {
            'apply_scene_by_name': lambda self, name, announce=True:
                applied.append(name) or True})()

        svc._we_dimmed = True
        svc.handle(service.EVENT_STOP)
        self.assertEqual(applied, ['Lights Up'])
        self.assertFalse(svc._we_dimmed)

    def test_nothing_happens_while_sync_is_off(self):
        import service

        xbmcaddon.SETTINGS['playback_sync'] = 'false'
        svc = service.GoveeService()
        applied = []
        svc._app = type('App', (), {
            'apply_scene_by_name': lambda self, name, announce=True:
                applied.append(name) or True})()

        svc._we_dimmed = True
        svc.handle(service.EVENT_STOP)
        self.assertEqual(applied, [])

    def test_player_callbacks_only_queue(self):
        import service

        svc = service.GoveeService()
        svc.player.onPlayBackPaused()
        self.assertEqual(svc._pending, service.EVENT_PAUSE)
        svc.player.onPlayBackStopped()
        self.assertEqual(svc._pending, service.EVENT_STOP)

    def test_player_subclass_takes_no_constructor_arguments(self):
        """Kodi parses Player() arguments in the base type.

        Passing anything to the subclass constructor fails with
        "an integer is required" before the subclass __init__ runs, which is
        what crashed the service on Krypton. The service must construct the
        player bare and attach itself afterwards.
        """
        import service

        with self.assertRaises(TypeError):
            service.GoveePlayer(object())

        player = service.GoveePlayer()
        self.assertIsNone(player.service)

        svc = service.GoveeService()
        self.assertIs(svc.player.service, svc)

    def test_callbacks_are_inert_before_attach_and_after_detach(self):
        import service

        player = service.GoveePlayer()
        player.onPlayBackStarted()  # must not raise on a bare player

        svc = service.GoveeService()
        svc.player.onPlayBackStarted()
        self.assertEqual(svc._pending, service.EVENT_PLAY)

        svc._pending = None
        svc.player.service = None
        svc.player.onPlayBackStopped()
        self.assertIsNone(svc._pending)


class TestPalette(unittest.TestCase):
    """The colour speed dial: user-editable, persisted, order-sensitive."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'palette'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome
        return ParagonHome()

    def test_seeded_and_persisted_on_first_read(self):
        import palette as palette_lib

        app = self.app()
        self.assertIn('Paragon Purple', [e['name'] for e in app.palette])
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'palette.json')))

        # A second session reads the same list back off disk.
        self.assertEqual([e['name'] for e in self.app().palette],
                         [e['name'] for e in palette_lib.default_palette()])

    def test_add_and_remove_round_trip_to_disk(self):
        app = self.app()
        before = len(app.palette)

        self.assertIsNotNone(app.save_color('Sunset', (255, 94, 20)))
        self.assertEqual(len(app.palette), before + 1)

        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertIn({'name': 'Sunset', 'color': [255, 94, 20]}, saved)

        self.assertTrue(app.remove_color(app.color_by_name('Sunset')))
        self.assertIsNone(app.color_by_name('Sunset'))
        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertNotIn('Sunset', [e['name'] for e in saved])

    def test_saving_an_existing_name_replaces_it_in_place(self):
        """Editing a colour must not shuffle the menu order."""
        app = self.app()
        index = [e['name'] for e in app.palette].index('Amber')

        app.save_color('Amber', (200, 100, 0))
        self.assertEqual([e['name'] for e in app.palette].index('Amber'),
                         index)
        self.assertEqual(app.color_by_name('Amber')['color'], [200, 100, 0])
        self.assertEqual(len([e for e in app.palette
                              if e['name'] == 'Amber']), 1)

    def test_name_lookup_is_case_insensitive(self):
        app = self.app()
        self.assertIsNotNone(app.color_by_name('  deep RED '))
        self.assertIsNone(app.color_by_name('nope'))

    def test_moving_reorders_and_persists(self):
        app = self.app()
        names = [e['name'] for e in app.palette]
        moved = app.move_color(2, -2)

        self.assertEqual(moved, 0)
        self.assertEqual(app.palette[0]['name'], names[2])
        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertEqual(saved[0]['name'], names[2])

    def test_moving_past_the_ends_is_a_no_op(self):
        app = self.app()
        names = [e['name'] for e in app.palette]
        self.assertEqual(app.move_color(0, -1), 0)
        self.assertEqual(app.move_color(len(names) - 1, 1), len(names) - 1)
        self.assertEqual([e['name'] for e in app.palette], names)

    def test_a_hand_edited_file_degrades_rather_than_throwing(self):
        os.makedirs(PROFILE)
        handle = open(os.path.join(PROFILE, 'palette.json'), 'w')
        handle.write(json.dumps([
            {'name': 'Good', 'color': [10, 20, 30]},
            {'name': 'Bad colour', 'color': ['x', 2, 3]},
            {'name': '   '},
            'not a dict',
            {'name': 'good', 'color': [1, 2, 3]},      # duplicate name
        ]))
        handle.close()

        app = self.app()
        self.assertEqual([e['name'] for e in app.palette], ['Good'])

    def test_reset_restores_the_built_in_set(self):
        import palette as palette_lib

        app = self.app()
        app.save_color('Sunset', (255, 94, 20))
        app.reset_palette()

        self.assertEqual([e['name'] for e in app.palette],
                         [e['name'] for e in palette_lib.default_palette()])
        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertNotIn('Sunset', [e['name'] for e in saved])

    def test_to_hex_formatting(self):
        import palette as palette_lib

        self.assertEqual(palette_lib.to_hex([255, 40, 150]), '#FF2896')
        self.assertEqual(palette_lib.to_hex([0, 0, 0]), '#000000')
        self.assertEqual(palette_lib.to_hex('nonsense'), '#FFFFFF')


# ---------------------------------------------------------------------------
# Diagnostics
#
# The whole point of this module is telling apart failures that look identical
# from the control panel, so each verdict gets its own case.
# ---------------------------------------------------------------------------

class TestDiagnostics(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'diagnostics'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    @staticmethod
    def report(**overrides):
        base = {
            'addresses': ['192.168.1.50'], 'bind_address': '',
            'listen_port': 4002, 'bound': True, 'bind_error': None,
            'attempts': [('multicast via 192.168.1.50', None),
                         ('broadcast', None)],
            'raw_replies': [], 'devices': [],
        }
        base.update(overrides)
        return base

    def test_port_busy_is_named_as_such(self):
        import diagnostics

        report = self.report(bound=False, bind_error='port in use')
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_PORT_BUSY)
        report['cause'] = diagnostics.CAUSE_PORT_BUSY
        text = diagnostics.summary(report)
        self.assertIn('Govee Desktop', text)

    def test_send_failure_is_distinguished_from_silence(self):
        import diagnostics

        report = self.report(attempts=[('multicast via eth0', 'no route'),
                                       ('broadcast', 'not permitted')])
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_NO_SEND)
        report['cause'] = diagnostics.CAUSE_NO_SEND
        self.assertIn('no route', diagnostics.summary(report))

    def test_silence_suggests_model_support_firewall_and_lan_toggle(self):
        import diagnostics

        report = self.report()
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_NO_REPLIES)
        report['cause'] = diagnostics.CAUSE_NO_REPLIES
        text = diagnostics.summary(report)
        self.assertIn('do not support the Govee LAN API', text)
        self.assertIn('firewall', text)
        self.assertIn('LAN Control', text)
        self.assertIn('192.168.1.50', text)

    def test_unparsed_replies_are_their_own_verdict(self):
        import diagnostics

        report = self.report(raw_replies=[('192.168.1.9', 'HTTP/1.1 200 OK')])
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_UNPARSED)
        report['cause'] = diagnostics.CAUSE_UNPARSED
        self.assertIn('none was a Govee scan response',
                      diagnostics.summary(report))

    def test_success_lists_the_models_found(self):
        import diagnostics

        report = self.report(
            raw_replies=[('192.168.1.9', '{}')],
            devices=[{'device': 'AA:BB', 'sku': 'H6159', 'ip': '192.168.1.9'},
                     {'device': 'CC:DD', 'sku': 'H6104', 'ip': '192.168.1.10'}])
        self.assertEqual(diagnostics.classify(report), diagnostics.CAUSE_OK)
        report['cause'] = diagnostics.CAUSE_OK
        text = diagnostics.summary(report)
        self.assertIn('H6104', text)
        self.assertIn('H6159', text)

    def test_log_lines_carry_the_detail_needed_to_debug(self):
        import diagnostics

        report = self.report(
            raw_replies=[('192.168.1.9', 'garbage')],
            devices=[{'device': 'AA:BB', 'sku': 'H6159', 'ip': '192.168.1.9'}])
        report['mode'] = 'auto'
        report['api_key_set'] = False
        report['cause'] = diagnostics.CAUSE_OK

        text = '\n'.join(diagnostics.format_lines(report))
        self.assertIn('192.168.1.50', text)      # interfaces enumerated
        self.assertIn('Listening on UDP 4002', text)
        self.assertIn('garbage', text)           # raw reply preserved
        self.assertIn('H6159', text)
        self.assertIn('Verdict', text)

    def test_run_writes_to_the_log_and_returns_a_summary(self):
        import diagnostics
        import xbmc
        from paragon_home import ParagonHome

        app = ParagonHome()

        class StubLAN(object):
            def probe(self, timeout=4.0):
                return TestDiagnostics.report()

        # Reach through the Hub to the Govee driver: the LAN probe belongs to
        # that driver, not to the device layer as a whole.
        app.controller.driver('govee').lan = StubLAN()
        del xbmc.LOG_LINES[:]

        text, report = diagnostics.run(app)
        self.assertEqual(report['cause'], diagnostics.CAUSE_NO_REPLIES)
        self.assertIn('nothing answered', text)
        logged = '\n'.join(message for _level, message in xbmc.LOG_LINES)
        self.assertIn('Paragon Home LAN diagnostics', logged)


class TestStatusRoundTrip(unittest.TestCase):
    """Does this model report back what it was set to?

    Capture, Toggle and Show status all trust devStatus. When a model reports
    a fixed or long-stale payload, all three are quietly wrong, and no single
    capture can tell that apart from a light something else had set.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'diagnostics'):
            if name in sys.modules:
                del sys.modules[name]

        from paragon_home import ParagonHome
        self.app = ParagonHome()
        self.device = Device('AA:BB', name='Lamp', model='H6008', lan=True,
                             ip='10.0.0.11')
        self.app._devices = [self.device]

    def tearDown(self):
        clean_profile()

    def _controller(self, readback):
        recorder = RecordingController()
        states = [{'power': 'on', 'brightness': 1, 'colorTem': 3800,
                   'color': {'r': 0, 'g': 0, 'b': 0}}, readback]

        def get_state(device):
            return states.pop(0) if states else readback

        recorder.get_state = get_state
        self.app.controller = recorder
        return recorder

    def test_a_bulb_that_reports_the_probe_back_is_trustworthy(self):
        import diagnostics

        self._controller({'power': 'on', 'brightness': 40, 'colorTem': 0,
                          'color': {'r': 255, 'g': 0, 'b': 255}})
        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)

        self.assertEqual(report['verdict'], diagnostics.VERDICT_TRACKS)
        self.assertIn('reports back what it was set to',
                      diagnostics.verify_summary(report))

    def test_a_bulb_stuck_on_a_stale_payload_is_called_out(self):
        import diagnostics

        # The real H6008 behaviour under investigation: set to magenta, still
        # reporting 0,0,0 at 3800K.
        self._controller({'power': 'on', 'brightness': 1, 'colorTem': 3800,
                          'color': {'r': 0, 'g': 0, 'b': 0}})
        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)

        self.assertEqual(report['verdict'], diagnostics.VERDICT_STALE)
        text = diagnostics.verify_summary(report)
        self.assertIn('did NOT report back', text)
        self.assertIn('Capture cannot work', text)

    def test_no_readback_is_its_own_verdict(self):
        import diagnostics

        self._controller(None)
        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)
        self.assertEqual(report['verdict'], diagnostics.VERDICT_NO_READBACK)
        self.assertIn('4002', diagnostics.verify_summary(report))

    def test_a_bulb_that_cannot_be_driven_is_distinguished(self):
        import diagnostics

        recorder = RecordingController(fail_on={'AA:BB'})
        recorder.get_state = lambda device: None
        self.app.controller = recorder

        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)
        self.assertEqual(report['verdict'],
                         diagnostics.VERDICT_CONTROL_FAILED)
        self.assertIn('Could not drive', diagnostics.verify_summary(report))

    def test_probe_avoids_the_colour_the_bulb_already_shows(self):
        """Probing with the current colour could not tell stale from correct."""
        import diagnostics

        already = {'power': 'on', 'brightness': 40, 'colorTem': 0,
                   'color': {'r': 255, 'g': 0, 'b': 255}}
        recorder = RecordingController()
        recorder.get_state = lambda device: already
        self.app.controller = recorder

        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)
        self.assertEqual(report['probe'], diagnostics.PROBE_ALT)

    def test_the_bulb_is_put_back_after_the_probe(self):
        import diagnostics

        recorder = self._controller(
            {'power': 'on', 'brightness': 40, 'colorTem': 0,
             'color': {'r': 255, 'g': 0, 'b': 255}})
        diagnostics.verify_status(self.app, self.device,
                                  sleep_func=lambda _s: None)

        # Last colour-ish command should restore the original 3800K white,
        # not leave the bulb sitting on the probe colour.
        temps = [c for c in recorder.calls if c[0] == 'temp']
        self.assertTrue(temps, 'bulb was not restored')
        self.assertEqual(temps[-1][2], 3800)


# ---------------------------------------------------------------------------
# Control panel menu walks
#
# The menus are built by appending to a list and then indexing back into it,
# which is exactly the kind of arithmetic that silently drifts when an entry is
# added. These walk the real menus with scripted dialog answers.
# ---------------------------------------------------------------------------

class TestControlPanel(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'gui'):
            if name in sys.modules:
                del sys.modules[name]

        from paragon_home import ParagonHome
        self.app = ParagonHome()
        self.recorder = RecordingController()
        self.app._devices = [Device('AA:BB', name='Lamp', lan=True,
                                    ip='10.0.0.2')]
        self.app.controller = self.recorder

    def tearDown(self):
        clean_profile()

    def panel(self):
        import gui
        return gui.ControlPanel(self.app)

    def driver_row(self, prefix='Govee'):
        return menu_row(self.panel().main_menu, prefix)[0]

    def test_the_main_menu_lists_a_row_per_kind_of_device(self):
        """One row per driver, not one row per device."""
        self.app._devices = [
            Device('AA:BB', name='Lamp', driver='govee', lan=True),
            Device('CC:DD', name='Plug', driver='tuya', lan=True),
            Device('EE:FF', name='Blaster', driver='broadlink', lan=True),
        ]

        _row, labels = menu_row(self.panel().main_menu, 'Govee')

        self.assertEqual(labels[:3],
                         ['Govee (1)', 'Broadlink (1)', 'Tuya (1)'])
        self.assertNotIn('Lamp', labels)

    def test_a_driver_with_nothing_found_is_not_offered(self):
        """A dead row is worse than no row; the search lives above this."""
        _row, labels = menu_row(self.panel().main_menu, 'Govee')

        self.assertNotIn('Tuya (0)', labels)
        self.assertIn('Diagnose device search...', labels)

    def test_main_menu_group_then_on(self):
        # Govee, then "All Govee", then "On".
        xbmcgui.SELECT_QUEUE.extend([self.driver_row(), 0, 1])
        self.panel().run()
        self.assertEqual(self.recorder.calls, [('turn', 'AA:BB', True)])

    def test_main_menu_device_row_selects_that_device(self):
        self.app._devices.append(Device('CC:DD', name='Strip', lan=True,
                                        ip='10.0.0.3'))
        # Govee, then 0 = All Govee, 1 = first device, 2 = second device.
        xbmcgui.SELECT_QUEUE.extend([self.driver_row(), 2, 2])
        self.panel().run()
        self.assertEqual(self.recorder.calls, [('turn', 'CC:DD', False)])

    def test_every_control_row_does_what_its_label_says(self):
        """Walk each row by label and assert the command that came out."""
        expected = {
            'On': [('turn', 'AA:BB', True)],
            'Off': [('turn', 'AA:BB', False)],
            'Toggle': [('turn', 'AA:BB', True)],  # no state readable -> on
        }
        self.recorder.get_state = lambda d: None

        # Discover the row order the same way the user sees it.
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().control_menu(None, 'All Lights')
        labels = xbmcgui.SELECT_CALLS[-1][1]

        for label, want in expected.items():
            del self.recorder.calls[:]
            xbmcgui.reset()
            xbmcgui.SELECT_QUEUE.extend([labels.index(label)])
            self.panel().control_menu(None, 'All Lights')
            self.assertEqual(self.recorder.calls, want,
                             'row "%s" did not do what it says' % label)

    def test_status_row_is_only_offered_for_a_single_device(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().control_menu(None, 'All Lights')
        self.assertNotIn('Show status', xbmcgui.SELECT_CALLS[-1][1])

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().control_menu([self.app.devices[0]], 'Lamp')
        self.assertIn('Show status', xbmcgui.SELECT_CALLS[-1][1])

    def test_a_plug_is_not_offered_brightness_or_colour(self):
        """The point of sorting by driver: only what the device can do."""
        plug = Device('wp9abc#1', name='Office Plug', driver='tuya', lan=True)
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        _row, labels = menu_row(
            lambda: self.panel().control_menu([plug], 'Office Plug'), 'Toggle')

        self.assertEqual(labels, ['Toggle', 'On', 'Off', 'Show status'])

    def test_a_blaster_opens_its_codes_rather_than_a_light_menu(self):
        """A blaster has no brightness to offer and no colour to apologise for."""
        blaster = Device('EE:FF', name='Bedroom RM', driver='broadlink',
                         lan=True)
        self.app._devices = [blaster]
        self.recorder.caps = ['commands']
        self.recorder.commands = lambda device: ['TV power']

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().device_menu(blaster)

        heading, labels = xbmcgui.SELECT_CALLS[-1]
        self.assertIn('TV power', labels)
        self.assertNotIn('Brightness...', labels)

    def test_a_driver_of_blasters_offers_no_all_row(self):
        """There is nothing to switch as a group, so the row is absent."""
        self.app._devices = [
            Device('EE:FF', name='Bedroom RM', driver='broadlink', lan=True)]
        self.recorder.caps = ['commands']

        _row, labels = menu_row(
            lambda: self.panel().driver_menu('broadlink'), 'Bedroom RM')

        # No [LAN] tag: a blaster has no other way to be reached.
        self.assertEqual(labels[0], 'Bedroom RM')
        self.assertTrue(labels[-1].startswith('Manage Broadlink devices'))

    def test_a_bulb_keeps_every_row_it_had(self):
        """The filtering must not quietly cost the Govee menu anything."""
        _row, labels = menu_row(
            lambda: self.panel().control_menu(
                [self.app.devices[0]], 'Lamp'), 'Toggle')

        self.assertEqual(labels,
                         ['Toggle', 'On', 'Off', 'Brightness...', 'Colour...',
                          'Colour temperature...', 'Show status',
                          'Check status reporting...'])

    def test_managing_from_a_driver_menu_shows_only_that_driver(self):
        self.app._devices = [
            Device('AA:BB', name='Lamp', driver='govee', lan=True),
            Device('CC:DD', name='Plug', driver='tuya', lan=True),
        ]
        # A plug has no colour, so the light-by-light naming walkthrough has
        # nothing to light up and should not be offered.
        self.recorder.caps = ['power', 'state']

        _row, labels = menu_row(
            lambda: self.panel().manage_devices('tuya'), '[x] Plug')

        self.assertEqual(len(labels), 1)
        self.assertNotIn('Name lights one by one...', labels)

    def test_managing_govee_still_offers_the_naming_walkthrough(self):
        _row, labels = menu_row(
            lambda: self.panel().manage_devices('govee'), 'Name lights')

        self.assertEqual(labels[0], 'Name lights one by one...')

    def test_a_plug_row_is_not_tagged_with_a_transport_it_cannot_choose(self):
        plug = Device('wp9abc#1', name='Office Plug', driver='tuya', lan=True)
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        _row, labels = menu_row(
            lambda: self.panel().driver_menu('tuya'), 'Office Plug')

        self.assertIn('Office Plug', labels)
        self.assertNotIn('Office Plug  [LAN]', labels)

    def test_a_govee_row_still_says_how_it_is_reached(self):
        """Where there is a genuine choice, the tag earns its place."""
        self.app._devices = [Device('AA:BB', name='Lamp', driver='govee',
                                    lan=True, cloud=True)]

        _row, labels = menu_row(
            lambda: self.panel().driver_menu('govee'), 'Lamp')

        self.assertIn('Lamp  [LAN+CLOUD]', labels)

    def test_an_unreadable_plug_is_explained_in_its_own_terms(self):
        """Not Govee's. A Kasa plug has never heard of UDP 4002."""
        plug = Device('8006KLAP', name='Christmas Tree', driver='kasa',
                      lan=True, ip='10.0.0.31')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']
        self.recorder.get_state = lambda d: None
        self.app.test_device = lambda d: (False, 'It needs your TP-Link '
                                                 'account. Settings -> Kasa.')

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().show_status(plug)

        text = xbmcgui.OK_DIALOGS[-1][1]
        self.assertIn('TP-Link account', text)
        self.assertNotIn('4002', text)

    def test_a_govee_bulb_keeps_the_explanation_that_fits_it(self):
        """Govee is the one driver with no connection test and a busy port."""
        from devices import ControlError as _Error

        bulb = self.app.devices[0]
        self.recorder.get_state = lambda d: None

        def no_test(device):
            raise _Error('%s cannot be tested' % device.name)
        self.app.test_device = no_test

        self.panel().show_status(bulb)

        self.assertIn('4002', xbmcgui.OK_DIALOGS[-1][1])

    def test_a_plug_is_offered_a_connection_test_not_a_flash(self):
        plug = Device('8006KLAP', name='Christmas Tree', driver='kasa',
                      lan=True, ip='10.0.0.31')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        class _Kasa(object):
            def test_connection(self, device):
                return True, 'ok'
        self.recorder.driver_for = lambda device: _Kasa()

        _row, labels = menu_row(lambda: self.panel()._edit_device(plug),
                                'Rename')

        self.assertIn('Test connection', labels)
        self.assertNotIn('Identify (flash this light)', labels)

    def _sequence_app(self):
        by_driver = {'govee': ['power', 'brightness', 'color', 'color_temp',
                               'state'],
                     'tuya': ['power', 'state'],
                     'broadlink': ['commands']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        self.recorder.commands = lambda device: ['TV power', 'Volume up']
        self.app._devices = [
            Device('AA:BB', name='Back Office Left Low', driver='govee',
                   lan=True),
            Device('WP9ABC#ALL', name='Office Plug All outlets',
                   driver='tuya', lan=True, native_id='wp9abc'),
            Device('EE:FF', name='Bedroom Broadlink', driver='broadlink',
                   lan=True),
        ]
        self.app._scenes = [scene_lib.make_scene('Warshade',
                                                 targets=['AA:BB'])]

    def test_the_main_menu_offers_sequences(self):
        _row, labels = menu_row(self.panel().main_menu, 'Sequences')

        self.assertIn('Sequences...', labels)

    def test_a_sequence_is_built_through_three_choices_per_step(self):
        """Driver, then which one, then what it does -- as spoken aloud."""
        import sequences as sequence_lib

        self._sequence_app()
        self.app._sequences = [sequence_lib.make_sequence('Wind Down')]
        panel = self.panel()

        # Step 1 -- Scene, Warshade.
        kinds = menu_row(lambda: panel.edit_step(self.app.sequences[0], 0),
                         'Scene')[1]
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Scene'), 0])
        panel.edit_step(self.app.sequences[0], 0)

        # Step 2 -- Tuya, the plug, On.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Tuya'), 1, 0])
        panel.edit_step(self.app.sequences[0], 1)

        # Step 3 -- Broadlink, the blaster, TV power.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Broadlink'), 1, 0])
        panel.edit_step(self.app.sequences[0], 2)

        steps = self.app.sequences[0]['steps']
        self.assertEqual(
            [sequence_lib.describe_step(s) for s in steps[:3]],
            ['Scene: Warshade', 'WP9ABC#ALL: On', 'EE:FF: TV power'])
        self.assertEqual(steps[2]['kind'], 'command')

    def test_the_editor_shows_all_fifteen_slots_including_the_empty_ones(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'}])
        self.app._sequences = [sequence]

        _row, labels = menu_row(lambda: self.panel().edit_sequence(sequence),
                                ' 1.')

        slots = [l for l in labels if l[:3].strip().rstrip('.').isdigit()]
        self.assertEqual(len(slots), 15)
        self.assertIn('Scene: Warshade', slots[0])
        self.assertIn('Empty', slots[1])

    def test_the_editor_offers_reordering_once_there_are_two_steps(self):
        import sequences as sequence_lib

        self._sequence_app()
        one = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'}])
        two = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'off'}])
        self.app._sequences = [one, two]
        panel = self.panel()

        alone = menu_row(lambda: panel.edit_sequence(one), 'Runs:')[1]
        xbmcgui.reset()
        pair = menu_row(lambda: panel.edit_sequence(two), 'Runs:')[1]

        self.assertNotIn('Reorder steps...', alone)
        self.assertIn('Reorder steps...', pair)

    def test_a_step_can_be_sent_to_another_slot_from_the_editor(self):
        """Three steps, and the third one wanted first."""
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'off'},
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'TV power'}])
        self.app._sequences = [sequence]
        panel = self.panel()

        # Reorder steps -> pick slot 3 -> move it to slot 1 -> back out.
        rows = menu_row(lambda: panel.edit_sequence(sequence),
                        'Reorder steps...')
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([2, 0, -1])
        panel.reorder_steps(sequence)

        self.assertEqual(
            [sequence_lib.describe_step(step)
             for step in sequence_lib.filled_steps(sequence)],
            ['EE:FF: TV power', 'Scene: Warshade', 'WP9ABC#ALL: Off'])
        self.assertEqual(rows[1].count('Reorder steps...'), 1)

    def test_a_reordered_sequence_is_saved_not_just_shown(self):
        """It has to survive the menu closing, or it did not happen."""
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Second'}])
        self.app._sequences = [sequence]

        xbmcgui.SELECT_QUEUE.extend([1, 0, -1])
        self.panel().reorder_steps(sequence)

        saved = self.app.sequence_by_name('Bedtime')['steps']
        self.assertEqual([step['target'] for step in saved[:2]],
                         ['Second', 'Warshade'])

    def test_the_move_screen_marks_the_step_being_moved(self):
        """Picking a destination among fifteen identical-looking rows needs
        the one you are holding to be obvious."""
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Second'}])
        self.app._sequences = [sequence]

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel()._move_step(sequence, 1)
        labels = xbmcgui.SELECT_CALLS[-1][1]

        self.assertIn('moving this', labels[1])
        self.assertNotIn('moving this', labels[0])
        self.assertEqual(len(labels), 15)

    def test_backing_out_of_the_move_screen_changes_nothing(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Second'}])
        self.app._sequences = [sequence]

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel()._move_step(sequence, 0)

        self.assertEqual([step['target'] for step in sequence['steps'][:2]],
                         ['Warshade', 'Second'])

    def test_an_empty_slot_cannot_be_moved(self):
        """Moving a blank somewhere else only renumbers the blanks."""
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Second'}])
        self.app._sequences = [sequence]

        # Slot 5 is empty. Offered the reorder screen, it is refused and the
        # screen comes back rather than the sequence being rearranged.
        xbmcgui.SELECT_QUEUE.extend([4, -1])
        self.panel().reorder_steps(sequence)

        self.assertEqual([step['target'] for step in sequence['steps'][:2]],
                         ['Warshade', 'Second'])

    def test_the_step_menu_offers_a_move_only_on_a_filled_slot(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Second'}])
        self.app._sequences = [sequence]
        panel = self.panel()

        filled = menu_row(lambda: panel.edit_step(sequence, 0), 'Scene')[1]
        xbmcgui.reset()
        blank = menu_row(lambda: panel.edit_step(sequence, 6), 'Scene')[1]

        self.assertIn('Move this step...', filled)
        self.assertNotIn('Move this step...', blank)

    def test_moving_a_step_from_its_own_menu_reorders_the_sequence(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Bedtime', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'scene', 'target': 'Second'},
            {'kind': 'scene', 'target': 'Third'}])
        self.app._sequences = [sequence]
        panel = self.panel()

        kinds = menu_row(lambda: panel.edit_step(sequence, 0), 'Scene')[1]
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Move this step...'), 2])
        panel.edit_step(sequence, 0)

        self.assertEqual([step['target'] for step in sequence['steps'][:3]],
                         ['Second', 'Third', 'Warshade'])

    def test_clearing_a_step_empties_that_slot_and_no_other(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'on'}])
        self.app._sequences = [sequence]
        panel = self.panel()

        kinds = menu_row(lambda: panel.edit_step(sequence, 0), 'Scene')[1]
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Clear this step')])
        panel.edit_step(sequence, 0)

        self.assertEqual(sequence['steps'][0]['kind'], 'none')
        self.assertEqual(sequence['steps'][1]['action'], 'on')

    def _rm(self):
        """A blaster whose driver records the radio calls made to it."""
        calls = []

        class Emitter(object):
            found_after = 0
            code = '2600abcd'
            sweep_raises = None

            def start_rf_sweep(self, device):
                calls.append('sweep')
                if self.sweep_raises:
                    raise ControlError(self.sweep_raises)
                return True

            def rf_frequency_found(self, device):
                calls.append('check')
                if self.found_after > 0:
                    self.found_after -= 1
                    return False
                return True

            def start_rf_capture(self, device):
                calls.append('capture')
                return True

            def cancel_rf_sweep(self, device):
                calls.append('cancel')
                return True

            def collect_learned(self, device):
                calls.append('collect')
                return self.code

            def save_command(self, device, name, code):
                calls.append('save:%s' % name)
                return True

        emitter = Emitter()
        self.app._emitter = lambda device: emitter
        device = Device('EE:FF', name='Hall RM', driver='broadlink')
        self.app._devices = [device]
        return device, emitter, calls

    def test_learning_an_rf_command_sweeps_then_captures(self):
        """Two passes: find the frequency, then listen on it."""
        device, _emitter, calls = self._rm()

        xbmcgui.INPUT_QUEUE.append('Blinds Open')
        self.panel().learn_rf_command(device, sleep_func=lambda s: None)

        self.assertEqual(calls,
                         ['sweep', 'check', 'capture', 'collect',
                          'save:Blinds Open'])

    def test_the_sweep_is_polled_until_it_locks_on(self):
        device, emitter, calls = self._rm()
        emitter.found_after = 3

        xbmcgui.INPUT_QUEUE.append('Blinds Shut')
        self.panel().learn_rf_command(device, sleep_func=lambda s: None)

        self.assertEqual(calls.count('check'), 4)
        self.assertIn('save:Blinds Shut', calls)

    def test_a_sweep_that_never_locks_on_is_cancelled(self):
        """A blaster left sweeping stays deaf, so the next ordinary command
        would look broken rather than the learn looking failed."""
        device, emitter, calls = self._rm()
        emitter.found_after = 10 ** 6      # never

        self.panel().learn_rf_command(device, sleep_func=lambda s: None)

        self.assertIn('cancel', calls)
        self.assertNotIn('capture', calls)
        self.assertFalse([c for c in calls if c.startswith('save:')])

    def test_a_frequency_found_but_no_code_is_cancelled_too(self):
        device, emitter, calls = self._rm()
        emitter.code = None

        self.panel().learn_rf_command(device, sleep_func=lambda s: None)

        self.assertIn('capture', calls)
        self.assertIn('cancel', calls)
        self.assertFalse([c for c in calls if c.startswith('save:')])

    def test_naming_nothing_cancels_rather_than_saving(self):
        device, _emitter, calls = self._rm()

        xbmcgui.INPUT_QUEUE.append('')
        self.panel().learn_rf_command(device, sleep_func=lambda s: None)

        self.assertIn('cancel', calls)
        self.assertFalse([c for c in calls if c.startswith('save:')])

    def test_a_blaster_with_no_radio_says_so_and_stops(self):
        device, emitter, calls = self._rm()
        emitter.sweep_raises = ('Hall RM would not start an RF sweep. Only '
                                'the Pro blasters have a radio')

        self.panel().learn_rf_command(device, sleep_func=lambda s: None)

        self.assertEqual(calls, ['sweep'])

    def test_the_command_menu_offers_both_kinds_of_learning(self):
        device, _emitter, _calls = self._rm()
        self.app.controller.commands = lambda d: ['TV Power', 'Volume Up']

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().command_menu(device)
        labels = xbmcgui.SELECT_CALLS[-1][1]

        self.assertEqual(labels[:2], ['TV Power', 'Volume Up'])
        self.assertIn('Learn a new command...', labels)
        self.assertIn('Learn an RF command...', labels)
        self.assertIn('Test connection', labels)

    def test_the_menu_rows_after_the_codes_do_what_they_say(self):
        """They used to be found by subtracting from the code count, and
        every version of that broke the first time a row was added."""
        device, _emitter, calls = self._rm()
        self.app.controller.commands = lambda d: ['TV Power', 'Volume Up']

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().command_menu(device)
        labels = list(xbmcgui.SELECT_CALLS[-1][1])
        xbmcgui.reset()

        row = labels.index('Learn an RF command...')
        xbmcgui.INPUT_QUEUE.append('Blinds Open')
        xbmcgui.SELECT_QUEUE.extend([row, -1])
        self.panel().command_menu(device)

        self.assertIn('save:Blinds Open', calls)

    def test_the_scene_editor_offers_a_separate_backlight_brightness(self):
        """Its own row, beside the lightbar one, since the backlight has the
        same problem from the other end."""
        panel = self.panel()
        self.app._scenes = [scene_lib.make_scene('Warshade')]

        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.edit_scene(0)
        labels = xbmcgui.SELECT_CALLS[-1][1]

        self.assertTrue(
            any(l.startswith('Backlight brightness:') for l in labels),
            'no backlight row among %r' % labels)
        self.assertTrue(
            any(l.startswith('Lightbar brightness:') for l in labels))

    def test_setting_a_backlight_brightness_sticks_and_can_be_cleared(self):
        import gui

        panel = self.panel()
        self.app._scenes = [scene_lib.make_scene('Warshade')]
        scene = self.app.scenes[0]
        self.assertIsNone(scene['backlight_brightness'])

        # Row 1 is the first preset, so it sets BRIGHTNESS_STEPS[0].
        xbmcgui.SELECT_QUEUE.extend([1])
        panel._edit_backlight_brightness(scene)
        self.assertEqual(scene['backlight_brightness'],
                         gui.BRIGHTNESS_STEPS[0])

        # Row 0 is "Same as the scene brightness", which clears it again.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([0])
        panel._edit_backlight_brightness(scene)
        self.assertIsNone(scene['backlight_brightness'])

    def test_a_pause_survives_the_step_being_changed(self):
        """The gap belongs to the slot, not to what happens to be in it."""
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade', 'pause': 12}])
        self.app._sequences = [sequence]
        panel = self.panel()

        kinds = menu_row(lambda: panel.edit_step(sequence, 0), 'Scene')[1]
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Tuya'), 1, 1])
        panel.edit_step(sequence, 0)

        self.assertEqual(sequence['steps'][0]['action'], 'off')
        self.assertEqual(sequence['steps'][0]['pause'], 12)

    def test_a_new_sequence_will_not_take_a_name_already_used(self):
        import sequences as sequence_lib

        self._sequence_app()
        self.app._sequences = [sequence_lib.make_sequence('Wind Down')]

        xbmcgui.INPUT_QUEUE.append('wind down')
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().new_sequence()

        self.assertEqual(len(self.app.sequences), 1)
        self.assertIn('already a sequence', xbmcgui.OK_DIALOGS[-1][1])

    def test_the_scene_editor_says_which_all_it_means(self):
        """"All lights" was the old wording and stopped being true."""
        self.app._scenes = [
            scene_lib.make_scene('Warshade', mode=scene_lib.MODE_COLOR,
                                 color=[80, 0, 120]),
            scene_lib.make_scene('All Off', power=scene_lib.POWER_OFF),
        ]

        colour = menu_row(lambda: self.panel().edit_scene(0), 'Lights:')[1]
        xbmcgui.reset()
        plain = menu_row(lambda: self.panel().edit_scene(1), 'Lights:')[1]

        self.assertIn('Lights: all colour lights', colour)
        self.assertIn('Lights: all lights', plain)

    def _blaster_app(self):
        blaster = Device('EE:FF', name='Bedroom Broadlink', driver='broadlink',
                         lan=True)
        self.app._devices = [self.app.devices[0], blaster]
        self.recorder.capabilities = lambda d: set(
            ['commands'] if d.driver == 'broadlink'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        self.recorder.commands = lambda d: ['AVR Power', 'TV power']
        return blaster

    def test_the_target_picker_leaves_out_a_blaster(self):
        """A scene can set nothing on it, so offering it does nothing."""
        blaster = self._blaster_app()
        scene = scene_lib.make_scene('All Off',
                                     power=scene_lib.POWER_OFF)
        self.app._scenes = [scene]

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel()._edit_targets(scene)
        offered = xbmcgui.SELECT_CALLS[-1][1]

        self.assertFalse([row for row in offered if blaster.name in row],
                         'the blaster was offered: %s' % offered)
        self.assertTrue([row for row in offered
                         if self.app.devices[0].name in row])

    def test_the_scene_editor_offers_the_commands_a_scene_sends(self):
        """They worked in the engine all along; there was no way to add one."""
        self._blaster_app()
        self.app._scenes = [scene_lib.make_scene('Movie Night')]

        _row, labels = menu_row(lambda: self.panel().edit_scene(0),
                                'Commands to send')

        self.assertIn('Commands to send: none', labels)

    def test_a_command_is_added_by_picking_a_device_then_a_code(self):
        self._blaster_app()
        scene = scene_lib.make_scene('Movie Night')
        self.app._scenes = [scene]
        panel = self.panel()

        rows = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[1]
        commands = [i for i, l in enumerate(rows)
                    if l.startswith('Commands to send')][0]
        # Commands, "Add a command...", the blaster, "AVR Power", back out,
        # then Save -- the editor works on a copy until then, on purpose.
        xbmcgui.SELECT_QUEUE.extend([commands, 0, 0, 0, -1,
                                     rows.index('Save')])
        panel.edit_scene(0)

        self.assertEqual(self.app.scenes[0]['actions'],
                         [{'device': 'EE:FF', 'command': 'AVR Power'}])

    def test_a_scene_command_fires_when_the_scene_is_applied(self):
        """The whole point of the row: it reaches the engine that existed."""
        blaster = self._blaster_app()
        scene = scene_lib.make_scene(
            'Movie Night', actions=[{'device': 'EE:FF',
                                     'command': 'AVR Power'}])

        fired, errors = scene_lib.fire_actions(
            self.recorder, scene_lib.normalise(scene),
            [self.app.devices[0], blaster])

        self.assertEqual((fired, errors), (1, []))
        self.assertEqual(self.recorder.calls,
                         [('command', 'EE:FF', 'AVR Power')])

    def test_leaving_the_editor_without_saving_changes_nothing(self):
        """The copy is deliberate: cancelling has to discard."""
        self._blaster_app()
        scene = scene_lib.normalise(scene_lib.make_scene('Movie Night'))
        self.app._scenes = [scene]
        panel = self.panel()

        rows = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[1]
        commands = [i for i, l in enumerate(rows)
                    if l.startswith('Commands to send')][0]
        xbmcgui.SELECT_QUEUE.extend([commands, 0, 0, 0, -1, -1])
        panel.edit_scene(0)

        self.assertEqual(self.app.scenes[0]['actions'], [])

    def test_a_command_can_be_taken_off_a_scene_again(self):
        self._blaster_app()
        scene = scene_lib.normalise(scene_lib.make_scene(
            'Movie Night', actions=[{'device': 'EE:FF',
                                     'command': 'AVR Power'}]))
        self.app._scenes = [scene]
        panel = self.panel()

        rows = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[1]
        commands = [i for i, l in enumerate(rows)
                    if l.startswith('Commands to send')][0]
        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.SELECT_QUEUE.extend([commands, 0, -1, rows.index('Save')])
        panel.edit_scene(0)

        self.assertEqual(self.app.scenes[0]['actions'], [])

    def test_the_editor_says_when_nothing_can_send_a_command(self):
        self.app._scenes = [scene_lib.make_scene('Movie Night')]
        self.recorder.caps = ['power', 'color', 'state']
        panel = self.panel()

        row = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[0]
        xbmcgui.SELECT_QUEUE.extend([row, -1])
        panel.edit_scene(0)

        self.assertTrue(any('sends commands' in message
                            for _heading, message in xbmcgui.NOTIFICATIONS))

    def test_every_scene_editor_row_survives_a_new_row_being_added(self):
        """Rows are looked up by label now, not counted.

        This editor grew a row four times and the numbering under it had to be
        re-counted by hand each time. It no longer can be.
        """
        self._blaster_app()
        self.app._scenes = [scene_lib.make_scene('Movie Night')]

        _row, labels = menu_row(lambda: self.panel().edit_scene(0), 'Name:')

        for expected in ('Name:', 'Power:', 'Brightness:', 'Appearance:',
                         'Lights:', 'Commands to send:', 'Cycle colours:',
                         'Test this scene', 'Save', 'Delete'):
            self.assertTrue(any(l.startswith(expected) for l in labels),
                            'lost the "%s" row' % expected)

    def test_any_switchable_device_gets_its_own_switch_rows(self):
        """Not only the drivers that need a key, which was Tuya alone."""
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True, ip='10.0.0.31')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        class _Kasa(object):
            def test_connection(self, device):
                return True, 'ok'
        self.recorder.driver_for = lambda device: _Kasa()

        _row, labels = menu_row(lambda: self.panel()._edit_device(plug),
                                'Rename')

        self.assertIn('Switch on', labels)
        self.assertIn('Switch off', labels)

    def test_a_blaster_gets_no_switch_rows(self):
        blaster = Device('EE:FF', name='Bedroom RM', driver='broadlink',
                         lan=True)
        self.app._devices = [blaster]
        self.recorder.caps = ['commands']

        _row, labels = menu_row(lambda: self.panel()._edit_device(blaster),
                                'Rename')

        self.assertNotIn('Switch on', labels)

    def test_a_tuya_plug_is_offered_its_power_cut_setting(self):
        plug = Device('WP9ABC#ALL', name='Office Plug', driver='tuya',
                      lan=True, ip='10.0.0.99', native_id='wp9abc')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        class _Tuya(object):
            def set_local_key(self, device, key):
                return True

            def local_key(self, device):
                return '0123456789abcdef'

            def test_connection(self, device):
                return True, 'ok'

            def set_power_memory(self, device, value):
                return True
        self.recorder.driver_for = lambda device: _Tuya()

        _row, labels = menu_row(lambda: self.panel()._edit_device(plug),
                                'Rename')

        self.assertIn('After a power cut...', labels)

    def test_a_govee_bulb_is_not_offered_a_power_cut_setting(self):
        bulb = self.app.devices[0]

        _row, labels = menu_row(lambda: self.panel()._edit_device(bulb),
                                'Rename')

        self.assertNotIn('After a power cut...', labels)

    def _dawn(self):
        """A scene with something in every kind of field it can hold."""
        return scene_lib.normalise(scene_lib.make_scene(
            'Dawn', brightness=50, mode=scene_lib.MODE_COLOR,
            color=[255, 180, 90], targets=['AA:BB'],
            actions=[{'device': 'EE:FF', 'command': 'AVR Power'}]))

    def _duplicate(self, name):
        panel = self.panel()
        rows = menu_row(lambda: panel.edit_scene(0), 'Duplicate')[1]
        xbmcgui.INPUT_QUEUE.append(name)
        # Duplicate, then leave the editor the copy opens in.
        xbmcgui.SELECT_QUEUE.extend([rows.index('Duplicate...'), -1])
        panel.edit_scene(0)
        return self.app.scene_by_name(name)

    def test_a_scene_can_be_copied_under_a_new_name(self):
        """Dawn at 50%, copied to Dusk, ready to be turned down to 5%."""
        self.app._scenes = [self._dawn()]

        dusk = self._duplicate('Dusk')

        self.assertIsNotNone(dusk)
        self.assertEqual(dusk['brightness'], 50)
        self.assertEqual(dusk['color'], [255, 180, 90])
        self.assertEqual(dusk['targets'], ['AA:BB'])
        self.assertEqual(dusk['actions'],
                         [{'device': 'EE:FF', 'command': 'AVR Power'}])

    def test_the_original_is_left_exactly_as_it_was(self):
        self.app._scenes = [self._dawn()]
        before = json.dumps(self.app.scenes[0], sort_keys=True)

        self._duplicate('Dusk')

        self.assertEqual(json.dumps(self.app.scene_by_name('Dawn'),
                                    sort_keys=True), before)

    def test_changing_the_copy_does_not_change_the_original(self):
        """The shallow-copy trap: a scene holds lists and a per-device map."""
        self.app._scenes = [self._dawn()]

        dusk = self._duplicate('Dusk')
        dusk['brightness'] = 5
        dusk['targets'].append('CC:DD')
        dusk['color'][0] = 0
        dusk['actions'].append({'device': 'EE:FF', 'command': 'TV power'})

        dawn = self.app.scene_by_name('Dawn')
        self.assertEqual(dawn['brightness'], 50)
        self.assertEqual(dawn['targets'], ['AA:BB'])
        self.assertEqual(dawn['color'], [255, 180, 90])
        self.assertEqual(len(dawn['actions']), 1)

    def test_the_copy_carries_unsaved_changes_from_the_screen(self):
        """What is on screen is what "duplicate" means."""
        self.app._scenes = [self._dawn()]
        panel = self.panel()
        rows = menu_row(lambda: panel.edit_scene(0), 'Duplicate')[1]

        xbmcgui.INPUT_QUEUE.append('Dusk')
        # Power -> "Turn off", then Duplicate, then leave.
        xbmcgui.SELECT_QUEUE.extend([rows.index('Power: on'), 1,
                                     rows.index('Duplicate...'), -1])
        panel.edit_scene(0)

        self.assertEqual(self.app.scene_by_name('Dusk')['power'], 'off')
        # And Dawn keeps its own, because that edit was never saved.
        self.assertEqual(self.app.scene_by_name('Dawn')['power'], 'on')

    def test_the_suggested_name_is_the_next_free_number(self):
        self.app._scenes = [self._dawn()]
        taken = lambda n: scene_lib.find(self.app.scenes, n) is not None

        self.assertEqual(self.panel()._copy_name('Dawn', taken), 'Dawn 2')

        self.app._scenes.append(scene_lib.normalise(
            scene_lib.make_scene('Dawn 2')))
        self.assertEqual(self.panel()._copy_name('Dawn', taken), 'Dawn 3')

    def test_a_name_already_in_use_is_refused(self):
        self.app._scenes = [self._dawn(),
                            scene_lib.normalise(scene_lib.make_scene('Dusk'))]

        self._duplicate('dusk')

        self.assertEqual(len(self.app.scenes), 2)
        self.assertIn('already a scene', xbmcgui.OK_DIALOGS[-1][1])

    def test_backing_out_of_the_name_copies_nothing(self):
        self.app._scenes = [self._dawn()]
        panel = self.panel()
        rows = menu_row(lambda: panel.edit_scene(0), 'Duplicate')[1]

        xbmcgui.INPUT_QUEUE.append('')
        xbmcgui.SELECT_QUEUE.extend([rows.index('Duplicate...'), -1])
        panel.edit_scene(0)

        self.assertEqual(len(self.app.scenes), 1)

    def test_main_menu_shows_the_version(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().run()

        heading, _labels = xbmcgui.SELECT_CALLS[-1]
        self.assertIn(xbmcaddon._INFO['version'], heading)

    def test_capture_is_reachable_through_the_scenes_menu(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}

        scenes_row = menu_row(self.panel().main_menu, 'Scenes')[0]
        capture_row = menu_row(self.panel().scene_menu, 'Capture')[0]

        xbmcgui.SELECT_QUEUE.extend([scenes_row, capture_row, -1])
        xbmcgui.INPUT_QUEUE.append('From The Menu')
        self.panel().main_menu()

        self.assertIsNotNone(self.app.scene_by_name('From The Menu'))

    def test_every_device_row_targets_its_own_light(self):
        """Guards against a loop-variable closure pointing all rows at one light."""
        self.app._devices = [
            Device('AA:BB', name='One', lan=True, ip='10.0.0.1'),
            Device('CC:DD', name='Two', lan=True, ip='10.0.0.2'),
            Device('EE:FF', name='Three', lan=True, ip='10.0.0.3'),
        ]

        driver = self.driver_row()
        _row, labels = menu_row(
            lambda: self.panel().driver_menu('govee'), 'All Govee')

        for device in self.app.devices:
            row = [i for i, label in enumerate(labels)
                   if label.startswith(device.name)][0]
            del self.recorder.calls[:]
            xbmcgui.reset()
            # Govee, the device row, then "Off" inside the control menu.
            xbmcgui.SELECT_QUEUE.extend([driver, row, 2])
            self.panel().main_menu()
            self.assertEqual(self.recorder.calls,
                             [('turn', device.device_id, False)],
                             'row for %s drove the wrong light' % device.name)

    def test_brightness_preset_index_maps_to_the_shown_percentage(self):
        import gui

        # BRIGHTNESS_STEPS[3] is 30%, and the label at that index says "30%".
        self.assertEqual(gui.BRIGHTNESS_STEPS[3], 30)
        xbmcgui.SELECT_QUEUE.extend([3])
        self.panel().brightness_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('brightness', 'AA:BB', 30)])

    def test_brightness_custom_entry_is_clamped(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([len(gui.BRIGHTNESS_STEPS)])
        xbmcgui.INPUT_QUEUE.append('250')
        self.panel().brightness_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('brightness', 'AA:BB', 100)])

    def test_colour_row_matches_its_label(self):
        panel = self.panel()
        index = palette_row(panel, 'Paragon Purple')
        expected = tuple(panel.app.palette[index]['color'])

        xbmcgui.SELECT_QUEUE.extend([index])
        panel.color_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls,
                         [('color', 'AA:BB') + expected])

    def test_colour_rows_show_their_hex(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().color_menu(None, 'All Lights')
        labels = xbmcgui.SELECT_CALLS[-1][1]
        self.assertIn('Paragon Purple  #963CDC', labels)

    def test_colour_custom_hex(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([len(panel.app.palette)])
        xbmcgui.INPUT_QUEUE.append('#00FF00')
        panel.color_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('color', 'AA:BB', 0, 255, 0)])

    def test_eight_digit_govee_code_reaches_the_lights(self):
        xbmcgui.SELECT_QUEUE.extend([len(self.app.palette)])
        xbmcgui.INPUT_QUEUE.append('FFFF2896')
        self.panel().color_menu(None, 'All Lights')

        self.assertEqual(self.recorder.calls,
                         [('color', 'AA:BB', 255, 40, 150)])
        shown = ' '.join(message for _h, message in xbmcgui.NOTIFICATIONS)
        self.assertIn('AARRGGBB', shown)

    def test_an_eight_digit_code_can_be_saved_into_a_scene(self):
        panel = self.panel()
        # Appearance -> Colour -> Custom hex, then Name, then Save.
        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Appearance:'), 1,
                                     len(panel.app.palette),
                                     scene_row(panel, 'Name:'),
                                     scene_row(panel, 'Save')])
        xbmcgui.INPUT_QUEUE.extend(['FFFF2896', 'Pink'])
        panel.edit_scene(None)

        scene = self.app.scene_by_name('Pink')
        self.assertIsNotNone(scene)
        self.assertEqual(scene['color'], [255, 40, 150])

    def test_temperature_preset_index_matches_its_label(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([1])
        self.panel().temp_menu(None, 'All Lights')
        self.assertEqual(gui.TEMP_PRESETS[1][0], 'Warm - 2700K')
        self.assertEqual(self.recorder.calls, [('temp', 'AA:BB', 2700)])

    def test_temperature_custom_entry(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([len(gui.TEMP_PRESETS)])
        xbmcgui.INPUT_QUEUE.append('3300')
        self.panel().temp_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('temp', 'AA:BB', 3300)])

    def test_adding_a_colour_from_the_menu_puts_it_in_the_list(self):
        panel = self.panel()
        before = len(panel.app.palette)

        # Manage colours -> "Add a colour...", hex, then name.
        xbmcgui.SELECT_QUEUE.extend([before])
        xbmcgui.INPUT_QUEUE.extend(['FFFF2896', 'Govee Pink'])
        panel.manage_colors()

        entry = panel.app.color_by_name('Govee Pink')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['color'], [255, 40, 150])

        # And it now shows up as a row in the colour menu.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.color_menu(None, 'All Lights')
        self.assertIn('Govee Pink  #FF2896', xbmcgui.SELECT_CALLS[-1][1])

    def test_deleting_a_colour_removes_it_from_the_menu(self):
        panel = self.panel()
        index = palette_row(panel, 'Lime')

        # The colour, then "Delete", then confirm.
        xbmcgui.SELECT_QUEUE.extend([index, 4])
        xbmcgui.YESNO_QUEUE.append(True)
        panel.manage_colors()

        self.assertIsNone(panel.app.color_by_name('Lime'))
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.color_menu(None, 'All Lights')
        labels = ' '.join(xbmcgui.SELECT_CALLS[-1][1])
        self.assertNotIn('Lime', labels)

    def test_declining_the_delete_keeps_the_colour(self):
        panel = self.panel()
        index = palette_row(panel, 'Lime')

        xbmcgui.SELECT_QUEUE.extend([index, 4])
        xbmcgui.YESNO_QUEUE.append(False)
        panel.manage_colors()

        self.assertIsNotNone(panel.app.color_by_name('Lime'))

    def test_changing_a_colours_hex_keeps_its_place(self):
        panel = self.panel()
        index = palette_row(panel, 'Teal')

        xbmcgui.SELECT_QUEUE.extend([index, 1])   # the colour, "Change colour"
        xbmcgui.INPUT_QUEUE.append('112233')
        panel.manage_colors()

        self.assertEqual(panel.app.color_by_name('Teal')['color'],
                         [17, 34, 51])
        self.assertEqual(palette_row(panel, 'Teal'), index)

    def test_renaming_a_colour_keeps_its_place_and_colour(self):
        panel = self.panel()
        index = palette_row(panel, 'Amber')
        original = list(panel.app.color_by_name('Amber')['color'])

        xbmcgui.SELECT_QUEUE.extend([index, 0])   # the colour, "Rename"
        xbmcgui.INPUT_QUEUE.append('Sunset')
        panel.manage_colors()

        self.assertIsNone(panel.app.color_by_name('Amber'))
        entry = panel.app.color_by_name('Sunset')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['color'], original)
        self.assertEqual(palette_row(panel, 'Sunset'), index)

    def test_building_a_mix_scene_from_the_editor(self):
        panel = self.panel()
        red = palette_row(panel, 'Deep Red')
        blue = palette_row(panel, 'Ocean Blue')
        done = len(panel.app.palette)

        # Appearance -> "Mix of colours..." -> tick two -> Done -> Name -> Save
        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Appearance:'), 2,
                                     red, blue, done,
                                     scene_row(panel, 'Name:'),
                                     scene_row(panel, 'Save')])
        xbmcgui.INPUT_QUEUE.append('Party')
        panel.edit_scene(None)

        scene = panel.app.scene_by_name('Party')
        self.assertIsNotNone(scene)
        self.assertEqual(scene['mode'], scene_lib.MODE_MIX)
        self.assertEqual(sorted(e['name'] for e in scene['colors']),
                         ['Deep Red', 'Ocean Blue'])

    def test_ticking_a_mix_colour_twice_removes_it(self):
        panel = self.panel()
        red = palette_row(panel, 'Deep Red')
        done = len(panel.app.palette)
        scene = scene_lib.make_scene('Test')

        xbmcgui.SELECT_QUEUE.extend([red, red, done])
        panel._edit_mix(scene)
        # Nothing ticked at Done, so it refuses and stays in the picker.
        self.assertEqual(scene.get('colors'), [])

    def test_a_mix_scene_spreads_colours_over_the_real_lights(self):
        panel = self.panel()
        panel.app._devices = [
            Device('AA:BB', name='One', lan=True, ip='10.0.0.1'),
            Device('CC:DD', name='Two', lan=True, ip='10.0.0.2'),
            Device('EE:FF', name='Three', lan=True, ip='10.0.0.3'),
            Device('11:22', name='Four', lan=True, ip='10.0.0.4'),
        ]
        panel.app.save_scene(scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}]))

        panel.app.apply_scene(panel.app.scene_by_name('Party'))

        colors = [c[2:] for c in self.recorder.calls if c[0] == 'color']
        self.assertEqual(len(colors), 4)
        self.assertEqual(sorted([colors.count((255, 0, 0)),
                                 colors.count((0, 0, 255))]), [2, 2])

    def test_deleting_a_palette_colour_does_not_change_a_saved_mix(self):
        """Mix colours are copied into the scene, not referenced by name."""
        panel = self.panel()
        entry = panel.app.color_by_name('Deep Red')
        panel.app.save_scene(scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX, colors=[dict(entry)]))

        panel.app.remove_color(panel.app.color_by_name('Deep Red'))

        scene = panel.app.scene_by_name('Party')
        self.assertEqual(scene['colors'][0]['color'], entry['color'])

    def test_a_custom_colour_is_usable_from_the_scene_editor(self):
        panel = self.panel()
        panel.app.save_color('Govee Pink', (255, 40, 150))
        index = palette_row(panel, 'Govee Pink')

        # Appearance -> Colour -> the new entry, then Name, then Save.
        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Appearance:'), 1, index,
                                     scene_row(panel, 'Name:'),
                                     scene_row(panel, 'Save')])
        xbmcgui.INPUT_QUEUE.append('Pink Scene')
        panel.edit_scene(None)

        scene = panel.app.scene_by_name('Pink Scene')
        self.assertEqual(scene['color'], [255, 40, 150])

    def test_manage_colours_is_reachable_from_the_colour_menu(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.color_menu(None, 'All Lights')
        self.assertIn('Manage colours...', xbmcgui.SELECT_CALLS[-1][1])

    def test_scene_menu_applies_the_selected_scene(self):
        xbmcgui.SELECT_QUEUE.extend([0])  # "Movie Night" is first
        self.panel().scene_menu()
        self.assertEqual(self.app.scenes[0]['name'], 'Movie Night')
        self.assertEqual([c[0] for c in self.recorder.calls],
                         ['turn', 'brightness', 'temp'])

    def test_scene_menu_trailing_rows_are_capture_then_manage(self):
        """Pick the rows by label so adding another cannot silently rewire them."""
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().scene_menu()
        labels = xbmcgui.SELECT_CALLS[-1][1]

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([labels.index('Manage scenes...')])
        self.panel().scene_menu()
        headings = [heading for heading, _options in xbmcgui.SELECT_CALLS]
        self.assertIn('Manage scenes', headings)

        xbmcgui.reset()
        # Capture prompts for a name; declining leaves everything alone.
        xbmcgui.SELECT_QUEUE.extend(
            [labels.index('Capture lights as a new scene...')])
        before = len(self.app.scenes)
        self.panel().scene_menu()
        self.assertEqual(len(self.app.scenes), before)

    def test_capture_saves_what_the_lights_are_doing(self):
        one = self.app.devices[0]
        two = Device('CC:DD', name='Strip', lan=True, ip='10.0.0.3')
        self.app._devices = [one, two]

        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200,
                      'color': {'r': 0, 'g': 0, 'b': 0}},
            'CC:DD': {'power': 'on', 'brightness': 80, 'colorTem': 0,
                      'color': {'r': 255, 'g': 40, 'b': 10}},
        }

        xbmcgui.INPUT_QUEUE.append('Twilight')
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Twilight')
        self.assertIsNotNone(scene)
        self.assertEqual(sorted(scene['devices'].keys()), ['AA:BB', 'CC:DD'])
        self.assertEqual(scene['devices']['AA:BB']['mode'],
                         scene_lib.MODE_TEMP)
        self.assertEqual(scene['devices']['AA:BB']['kelvin'], 2200)
        self.assertEqual(scene['devices']['CC:DD']['mode'],
                         scene_lib.MODE_COLOR)
        self.assertEqual(scene['devices']['CC:DD']['color'], [255, 40, 10])

        # And it survived to disk in a form that reloads.
        saved = json.load(open(os.path.join(PROFILE, 'scenes.json')))
        stored = [s for s in saved if s['name'] == 'Twilight'][0]
        self.assertEqual(stored['devices']['CC:DD']['brightness'], 80)

    def test_capture_reports_lights_that_did_not_answer(self):
        two = Device('CC:DD', name='Strip', lan=True, ip='10.0.0.3')
        self.app._devices = [self.app.devices[0], two]
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200},
            'CC:DD': None,
        }

        xbmcgui.INPUT_QUEUE.append('Partial')
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Partial')
        self.assertEqual(list(scene['devices'].keys()), ['AA:BB'])
        shown = ' '.join(line for _h, line in xbmcgui.OK_DIALOGS)
        self.assertIn('Strip', shown)

    def test_capture_with_no_answers_saves_nothing(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {'AA:BB': None}
        before = len(self.app.scenes)

        xbmcgui.INPUT_QUEUE.append('Nothing')
        self.panel().capture_scene()

        self.assertEqual(len(self.app.scenes), before)
        self.assertIsNone(self.app.scene_by_name('Nothing'))

    def test_capture_asks_before_replacing_an_existing_scene(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}

        xbmcgui.INPUT_QUEUE.append('Movie Night')
        xbmcgui.YESNO_QUEUE.append(False)   # decline the replace
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Movie Night')
        self.assertFalse(scene.get('devices'))  # original, uncaptured

        xbmcgui.INPUT_QUEUE.append('Movie Night')
        xbmcgui.YESNO_QUEUE.append(True)    # accept it this time
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Movie Night')
        self.assertTrue(scene.get('devices'))
        # Replaced in place rather than duplicated.
        self.assertEqual(len([s for s in self.app.scenes
                              if s['name'] == 'Movie Night']), 1)

    def test_new_scene_can_be_named_and_saved(self):
        panel = self.panel()
        before = len(self.app.scenes)
        save = scene_row(panel, 'Save')

        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Name:'), save])
        xbmcgui.INPUT_QUEUE.append('Reading')
        panel.edit_scene(None)

        self.assertEqual(len(self.app.scenes), before + 1)
        self.assertIsNotNone(self.app.scene_by_name('Reading'))
        # And it survived to disk.
        saved = json.load(open(os.path.join(PROFILE, 'scenes.json')))
        self.assertIn('Reading', [s['name'] for s in saved])

    def test_editing_an_existing_scene_keeps_its_slot(self):
        panel = self.panel()
        index = 0
        original = self.app.scenes[index]['name']

        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Name:', index),
                                     scene_row(panel, 'Save', index)])
        xbmcgui.INPUT_QUEUE.append('Cinema')
        panel.edit_scene(index)

        self.assertEqual(self.app.scenes[index]['name'], 'Cinema')
        self.assertIsNone(self.app.scene_by_name(original))

    def test_scene_delete_row_only_exists_when_editing(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.edit_scene(None)
        new_options = xbmcgui.SELECT_CALLS[-1][1]
        self.assertNotIn('Delete', new_options)

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.edit_scene(0)
        edit_options = xbmcgui.SELECT_CALLS[-1][1]
        self.assertIn('Delete', edit_options)

    def test_scene_target_picker_toggles_devices(self):
        panel = self.panel()
        scene = scene_lib.make_scene('Test')

        # Row 0 is "all lights", rows 1..n are devices, last row is Done.
        xbmcgui.SELECT_QUEUE.extend([1, 2])  # toggle device, then Done
        panel._edit_targets(scene)
        self.assertEqual(scene['targets'], ['AA:BB'])

        xbmcgui.SELECT_QUEUE.extend([1, 2])  # toggle it back off, then Done
        panel._edit_targets(scene)
        self.assertEqual(scene['targets'], [])

    def _manage_row(self, matcher):
        """Index of the Manage devices row whose label matches."""
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().manage_devices()
        labels = xbmcgui.SELECT_CALLS[-1][1]
        xbmcgui.reset()
        return [i for i, label in enumerate(labels) if matcher in label][0]

    def test_device_rename_persists(self):
        row = self._manage_row('Lamp')
        xbmcgui.SELECT_QUEUE.extend([row, 0])  # the device, then "Rename"
        xbmcgui.INPUT_QUEUE.append('Behind the TV')
        self.panel().manage_devices()

        self.assertEqual(self.app.devices[0].name, 'Behind the TV')
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(saved[0]['name'], 'Behind the TV')

    def test_device_can_be_disabled_and_drops_out_of_the_group(self):
        row = self._manage_row('Lamp')
        xbmcgui.SELECT_QUEUE.extend([row, 1])  # the device, then "Disable"
        self.panel().manage_devices()

        self.assertFalse(self.app.devices[0].enabled)
        self.assertEqual(self.app.enabled_devices, [])

    def test_naming_walkthrough_lights_each_bulb_and_saves_names(self):
        # Both still on placeholder names, so the walk covers all of them
        # without the "which lights?" prompt.
        self.app._devices = [
            Device('AA:BB', name='H6008 (AABB)', model='H6008', lan=True,
                   ip='10.0.0.2'),
            Device('CC:DD', name='H6008 (CCDD)', model='H6008', lan=True,
                   ip='10.0.0.3'),
        ]
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 40, 'colorTem': 2700},
            'CC:DD': {'power': 'on', 'brightness': 40, 'colorTem': 2700},
        }

        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.extend(['Kitchen Left', 'Kitchen Right'])
        self.panel().name_lights()

        self.assertEqual([d.name for d in self.app.devices],
                         ['Kitchen Left', 'Kitchen Right'])
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(sorted(d['name'] for d in saved),
                         ['Kitchen Left', 'Kitchen Right'])

        # Each bulb was driven to the highlight colour while being asked about.
        highlights = [c for c in self.recorder.calls
                      if c[0] == 'color' and c[2:] == gui_highlight()]
        self.assertEqual(len(highlights), 2)

    def test_naming_puts_each_light_back_before_moving_on(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 40, 'colorTem': 2700}}

        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.append('Kitchen Left')
        self.panel().name_lights()

        # The last thing done to the bulb is the restore, not the highlight.
        self.assertEqual(self.recorder.calls[-1], ('temp', 'AA:BB', 2700))

    def test_cancelling_the_keyboard_stops_and_keeps_earlier_names(self):
        self.app._devices = [
            Device('AA:BB', name='H6008 (AABB)', model='H6008', lan=True,
                   ip='10.0.0.2'),
            Device('CC:DD', name='H6008 (CCDD)', model='H6008', lan=True,
                   ip='10.0.0.3'),
        ]
        self.recorder.get_states = lambda devices, timeout=3.0: {}

        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.append('Kitchen Left')  # then the queue runs dry
        self.panel().name_lights()

        self.assertEqual(self.app.devices[0].name, 'Kitchen Left')
        self.assertEqual(self.app.devices[1].name, 'H6008 (CCDD)')
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertIn('Kitchen Left', [d['name'] for d in saved])

    def test_naming_offers_to_do_only_the_unnamed_ones(self):
        named = self.app.devices[0]          # 'Lamp' -- already named
        unnamed = Device('CC:DD', name='H6008 (CCDD)', model='H6008', lan=True,
                         ip='10.0.0.3')
        self.app._devices = [named, unnamed]
        self.recorder.get_states = lambda devices, timeout=3.0: {}

        xbmcgui.SELECT_QUEUE.extend([0])     # "Only the 1 still unnamed"
        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.append('Kitchen Right')
        self.panel().name_lights()

        self.assertEqual(named.name, 'Lamp')
        self.assertEqual(unnamed.name, 'Kitchen Right')

    def test_declining_the_walkthrough_changes_nothing(self):
        xbmcgui.YESNO_QUEUE.append(False)
        self.panel().name_lights()
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(self.app.devices[0].name, 'Lamp')

    def test_identify_flashes_the_configured_number_of_times(self):
        import gui

        device = self.app.devices[0]
        self.recorder.get_state = lambda d: None
        self.panel()._identify(device, sleep_func=lambda _s: None)

        # One off and one on per flash.
        turns = [c for c in self.recorder.calls if c[0] == 'turn']
        self.assertEqual(len(turns), gui.IDENTIFY_FLASHES * 2)
        self.assertEqual(gui.IDENTIFY_FLASHES, 10)
        self.assertEqual(turns[0], ('turn', 'AA:BB', False))
        self.assertEqual(turns[1], ('turn', 'AA:BB', True))

    def test_identify_can_be_cancelled_early(self):
        device = self.app.devices[0]
        self.recorder.get_state = lambda d: None
        xbmcgui.CANCEL_AFTER.append(3)   # cancel on the third update

        self.panel()._identify(device, sleep_func=lambda _s: None)

        turns = [c for c in self.recorder.calls if c[0] == 'turn']
        self.assertEqual(len(turns), 4)  # two full flashes, then stopped

    def test_identify_puts_a_light_that_was_off_back_off(self):
        """Otherwise identifying a light silently switches it on for good."""
        device = self.app.devices[0]
        self.recorder.get_state = lambda d: {'power': 'off'}

        self.panel()._identify(device, sleep_func=lambda _s: None)

        self.assertEqual(self.recorder.calls[-1], ('turn', 'AA:BB', False))

    def test_identify_restores_the_previous_colour(self):
        device = self.app.devices[0]
        self.recorder.get_state = lambda d: {
            'power': 'on', 'brightness': 25, 'colorTem': 0,
            'color': {'r': 255, 'g': 40, 'b': 150}}

        self.panel()._identify(device, sleep_func=lambda _s: None)

        self.assertIn(('brightness', 'AA:BB', 25), self.recorder.calls)
        self.assertEqual(self.recorder.calls[-1],
                         ('color', 'AA:BB', 255, 40, 150))

    def test_identify_reports_a_light_it_cannot_reach(self):
        device = self.app.devices[0]
        recorder = RecordingController(fail_on={'AA:BB'})
        recorder.get_state = lambda d: None
        self.app.controller = recorder

        self.panel()._identify(device, sleep_func=lambda _s: None)
        self.assertTrue(xbmcgui.NOTIFICATIONS)

    def test_pick_scene_writes_the_setting(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([1])  # row 0 is "(none)"
        gui.pick_scene_for_setting(self.app, 'scene_playing')
        self.assertEqual(xbmcaddon.SETTINGS['scene_playing'], 'Movie Night')

    def test_pick_scene_can_clear_the_setting(self):
        import gui

        xbmcaddon.SETTINGS['scene_playing'] = 'Movie Night'
        xbmcgui.SELECT_QUEUE.extend([0])  # "(none)"
        gui.pick_scene_for_setting(self.app, 'scene_playing')
        self.assertEqual(xbmcaddon.SETTINGS['scene_playing'], '')

    def test_empty_cache_offers_discovery_and_backs_out_cleanly(self):
        self.app._devices = []
        xbmcgui.YESNO_QUEUE.append(False)  # decline the search
        self.panel().run()
        self.assertEqual(self.recorder.calls, [])


# ---------------------------------------------------------------------------
# The web remote
# ---------------------------------------------------------------------------

try:
    from http.client import HTTPConnection
except ImportError:  # pragma: no cover - Python 2 runner
    from httplib import HTTPConnection


class RemoteClient(object):
    """A phone, near enough.

    Keeps its cookie the way a browser would, and sets the guard header the
    page's own JavaScript sets -- so a test that leaves it off is testing what
    happens when the request did not come from the page.
    """

    def __init__(self, port, token=None):
        self.port = port
        self.token = token
        self.cookie = None

    def call(self, method, path, body=None, headers=None, guard=True):
        sent = {}
        if guard:
            sent['X-Paragon-Remote'] = '1'
        if self.token:
            sent['X-Paragon-Token'] = self.token
        if self.cookie:
            sent['Cookie'] = self.cookie
        sent.update(headers or {})

        payload = None
        if body is not None:
            payload = body if isinstance(body, str) else json.dumps(body)
            sent['Content-Type'] = 'application/json'

        connection = HTTPConnection('127.0.0.1', self.port, timeout=15)
        try:
            connection.request(method, path, payload, sent)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            content_type = response.getheader('Content-Type') or ''
            policy = response.getheader('Content-Security-Policy') or ''
            cookie = response.getheader('Set-Cookie')
        finally:
            connection.close()

        if cookie:
            self.cookie = cookie.split(';')[0]
        try:
            data = json.loads(raw.decode('utf-8'))
        except ValueError:
            data = {}
        return {'status': status, 'data': data, 'body': raw,
                'type': content_type, 'csp': policy}

    def login(self, pin):
        return self.call('POST', '/api/login', {'pin': pin})

    def state(self):
        return self.call('GET', '/api/state')

    def act(self, action, **params):
        params['action'] = action
        return self.call('POST', '/api/action', params)


class ServiceLoop(object):
    """Stands in for service.py's loop: the one thread allowed in the session.

    The remote's whole design is that a request never touches ParagonHome --
    it queues work and something else runs it. That something else is this, so
    a test without one running sees actions queue and time out, which is the
    correct behaviour rather than a broken fixture.
    """

    def __init__(self, server, app, interval=0.02):
        self.server = server
        self.app = app
        self.interval = interval
        self.pumped = 0
        self.thread_id = None
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()
        # Wait for it to actually be on its own thread, so a test can compare
        # thread ids without racing the start-up.
        for _ in range(200):
            if self.thread_id is not None:
                break
            time.sleep(0.01)
        return self

    def _run(self):
        self.thread_id = threading.current_thread().ident
        while not self._stop.is_set():
            try:
                self.pumped += self.server.pump(self.app)
            except Exception as exc:  # recorded, not swallowed
                self.error = exc
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None


class ThreadWatchingController(RecordingController):
    """A recorder that also remembers which thread the command came from."""

    def __init__(self, *args, **kwargs):
        RecordingController.__init__(self, *args, **kwargs)
        self.threads = set()

    def _record(self, name, device, *args):
        self.threads.add(threading.current_thread().ident)
        RecordingController._record(self, name, device, *args)


class TestWebRemote(unittest.TestCase):
    """The web remote, over a real socket on loopback."""

    PIN = '135790'
    TOKEN = 'e6a1f0c3d2b4a5968778695a4b3c2d1e0f9a8b7c6d5e4f30'

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'remote'):
            if name in sys.modules:
                del sys.modules[name]

        import remote as remote_lib
        from paragon_home import ParagonHome

        self.lib = remote_lib
        self.app = ParagonHome()
        self.recorder = ThreadWatchingController()
        self.app.controller = self.recorder
        self.app._devices = [
            Device('AA:BB', name='Living Room', lan=True),
            # supports= narrows what the controller reports, which is how a
            # plug and a blaster tell themselves apart from a bulb here.
            Device('CC:DD', name='Desk Plug', driver='tuya', lan=True,
                   supports=['turn']),
            Device('EE:FF', name='Hall Blaster', driver='broadlink', lan=True,
                   supports=['learn']),
        ]
        self.recorder.command_map = {'EE:FF': ['TV Power', 'Volume Up']}
        self.server = None
        self.loop = None

    def tearDown(self):
        if self.loop is not None:
            self.loop.stop()
        if self.server is not None:
            self.server.stop()
        clean_profile()
        xbmcaddon.reset()

    # -- fixtures ----------------------------------------------------------

    def serve(self, pin=None, allow_sequences=True, run_loop=True):
        """Start a real server on loopback and return a client for it."""
        gate = self.lib.Gate(self.PIN if pin is None else pin, self.TOKEN)
        server = self.lib.RemoteServer(port=_free_port(), gate=gate,
                                       address='127.0.0.1',
                                       allow_sequences=allow_sequences)
        self.assertTrue(server.start())
        self.server = server
        server.refresh(self.app)
        if run_loop:
            self.loop = ServiceLoop(server, self.app).start()
        return RemoteClient(server.port)

    def signed_in(self, **kwargs):
        client = self.serve(**kwargs)
        self.assertEqual(client.login(self.PIN)['status'], 200)
        return client

    # -- the page ----------------------------------------------------------

    def test_the_page_is_served_before_anyone_signs_in(self):
        """The PIN box is on the page, so the page cannot need the PIN."""
        client = self.serve()
        answer = client.call('GET', '/', guard=False)

        self.assertEqual(answer['status'], 200)
        self.assertIn('text/html', answer['type'])
        self.assertIn(b'Paragon Home', answer['body'])

    def test_the_page_asks_for_nothing_from_anywhere_else(self):
        """No CDN and no font host: the typeface is served from this box.

        A page that reaches out to a font service looks wrong on a LAN with
        no way out, and tells that service the address of this house.
        """
        page = self.lib.PAGE.encode('utf-8')

        for marker in (b'http://', b'https://', b'//cdn', b'gstatic'):
            self.assertNotIn(marker, page)
        self.assertIn(b"url('/font/paragon-bold.woff2')", page)

    def test_an_unknown_route_says_so_rather_than_serving_the_page(self):
        client = self.serve()
        self.assertEqual(client.call('GET', '/wp-admin')['status'], 404)
        self.assertEqual(client.call('POST', '/api/nope', {})['status'], 404)

    # -- the typeface ------------------------------------------------------

    def test_the_font_is_served_from_this_box(self):
        """Shipped with the add-on, so the page looks right with no internet."""
        client = self.serve()

        answer = client.call('GET', '/font/paragon-bold.woff2', guard=False)

        self.assertEqual(answer['status'], 200)
        self.assertEqual(answer['type'], 'font/woff2')
        # wOF2, the magic number every WOFF2 file opens with.
        self.assertEqual(answer['body'][:4], b'wOF2')

    def test_only_the_shipped_fonts_are_served(self):
        client = self.serve()

        self.assertEqual(
            client.call('GET', '/font/anything.woff2')['status'], 404)

    def test_a_font_name_cannot_walk_out_of_its_directory(self):
        """Above resources/fonts sit the Tuya keys and the Kasa password."""
        client = self.serve()

        for escape in ('/font/../../../settings.xml',
                       '/font/..%2f..%2ftuya_keys.json',
                       '/font/....//paragon-bold.woff2',
                       '/font//etc/passwd',
                       '/icon.png/../addon.xml',
                       '/icon.png/../resources/lib/remote.py'):
            answer = client.call('GET', escape)
            self.assertEqual(answer['status'], 404, escape)
            self.assertNotIn(b'root:', answer['body'])

    def test_the_page_policy_allows_what_the_page_loads_and_no_more(self):
        client = self.serve()

        policy = client.call('GET', '/', guard=False).get('csp') or ''

        self.assertIn("default-src 'none'", policy)
        # Everything the page loads, it loads from this box.
        self.assertIn("font-src 'self'", policy)
        self.assertIn("img-src 'self' data:", policy)
        self.assertIn("manifest-src 'self'", policy)
        self.assertIn("connect-src 'self'", policy)

    # -- launching as an app -----------------------------------------------

    def test_a_tablet_can_launch_it_without_a_browser_around_it(self):
        """A page cannot go full screen by itself, so the manifest carries it."""
        client = self.serve()

        answer = client.call('GET', '/manifest.webmanifest', guard=False)

        self.assertEqual(answer['status'], 200)
        self.assertIn('manifest+json', answer['type'])
        self.assertEqual(answer['data']['display'], 'fullscreen')
        self.assertIn('standalone', answer['data']['display_override'])
        self.assertEqual(answer['data']['start_url'], '/')

    def test_the_manifest_is_readable_before_signing_in(self):
        """A browser fetches it before anyone has typed a PIN."""
        client = self.serve()

        self.assertEqual(
            client.call('GET', '/manifest.webmanifest')['status'], 200)
        self.assertEqual(client.state()['status'], 401)

    def test_the_icon_is_served_for_a_home_screen(self):
        client = self.serve()

        answer = client.call('GET', '/icon.png', guard=False)

        self.assertEqual(answer['status'], 200)
        self.assertEqual(answer['type'], 'image/png')
        self.assertEqual(answer['body'][:4], b'\x89PNG')

    def test_the_page_asks_the_browser_to_drop_its_own_chrome(self):
        page = self.lib.PAGE.encode('utf-8')

        self.assertIn(b'rel="manifest" href="/manifest.webmanifest"', page)
        # iOS reads these rather than the manifest, and honours them over
        # plain HTTP where Android will not.
        self.assertIn(b'apple-mobile-web-app-capable', page)
        self.assertIn(b'apple-touch-icon', page)

    # -- getting in --------------------------------------------------------

    def test_the_api_is_shut_without_a_credential(self):
        client = self.serve()
        self.assertEqual(client.state()['status'], 401)
        self.assertEqual(client.act('off')['status'], 401)
        self.assertEqual(self.recorder.calls, [])

    def test_a_request_without_the_page_header_is_refused(self):
        """The CSRF defence: a form on another site cannot set this header."""
        client = self.signed_in()

        answer = client.call('POST', '/api/action', {'action': 'off'},
                             guard=False)

        self.assertEqual(answer['status'], 403)
        self.assertEqual(self.recorder.calls, [])

    def test_a_request_claiming_another_origin_is_refused(self):
        client = self.signed_in()

        answer = client.call('POST', '/api/action', {'action': 'off'},
                             headers={'Origin': 'http://example.invalid'})

        self.assertEqual(answer['status'], 403)
        self.assertEqual(self.recorder.calls, [])

    def test_the_wrong_pin_gets_nowhere_and_the_right_one_signs_in(self):
        client = self.serve()

        refused = client.login('000000')
        self.assertEqual(refused['status'], 401)
        self.assertFalse(refused['data'].get('ok'))
        self.assertEqual(client.state()['status'], 401)

        self.assertEqual(client.login(self.PIN)['status'], 200)
        self.assertEqual(client.state()['status'], 200)

    def test_the_api_token_needs_no_login_at_all(self):
        """curl and a keymap have no browser to keep a cookie in."""
        client = self.serve()
        machine = RemoteClient(client.port, token=self.TOKEN)

        self.assertEqual(machine.state()['status'], 200)
        self.assertTrue(machine.act('off')['data'].get('ok'))

    def test_signing_out_ends_the_session(self):
        client = self.signed_in()
        self.assertEqual(client.state()['status'], 200)

        self.assertEqual(client.call('POST', '/api/logout', {})['status'], 200)
        client.cookie = client.cookie  # the browser keeps sending the old one
        self.assertEqual(client.state()['status'], 401)

    def test_enough_wrong_pins_stop_the_right_one_working(self):
        """A six-digit PIN is only worth what the lockout behind it is."""
        client = self.serve()

        for _ in range(self.lib.MAX_ATTEMPTS):
            self.assertEqual(client.login('999999')['status'], 401)

        locked = client.login(self.PIN)
        self.assertEqual(locked['status'], 429)
        self.assertGreater(locked['data'].get('locked'), 0)
        self.assertEqual(client.state()['status'], 401)

    def test_the_lockout_lifts_and_gets_longer_each_time(self):
        gate = self.lib.Gate(self.PIN, self.TOKEN)

        for _ in range(self.lib.MAX_ATTEMPTS):
            self.assertIsNone(gate.login('000000', '10.0.0.9', now=1000.0))
        self.assertTrue(gate.locked_for('10.0.0.9', now=1000.0))

        # Waiting it out works, and the round after costs twice as long.
        later = 1000.0 + self.lib.LOCKOUT_SECONDS + 1
        self.assertFalse(gate.locked_for('10.0.0.9', now=later))
        self.assertTrue(gate.login(self.PIN, '10.0.0.9', now=later))

        for _ in range(self.lib.MAX_ATTEMPTS):
            self.assertIsNone(gate.login('000000', '10.0.0.9', now=later))
        self.assertGreater(gate.locked_for('10.0.0.9', now=later),
                           self.lib.LOCKOUT_SECONDS)

    def test_one_address_locked_out_does_not_lock_out_the_house(self):
        gate = self.lib.Gate(self.PIN, self.TOKEN)

        for _ in range(self.lib.MAX_ATTEMPTS):
            gate.login('000000', '10.0.0.9', now=1000.0)

        self.assertTrue(gate.locked_for('10.0.0.9', now=1000.0))
        self.assertFalse(gate.locked_for('10.0.0.4', now=1000.0))
        self.assertTrue(gate.login(self.PIN, '10.0.0.4', now=1000.0))

    def test_a_session_stops_working_once_it_has_expired(self):
        gate = self.lib.Gate(self.PIN, self.TOKEN, session_seconds=60)
        token = gate.login(self.PIN, '10.0.0.4', now=1000.0)

        self.assertTrue(gate.accepts(token, now=1050.0))
        self.assertFalse(gate.accepts(token, now=1100.0))
        # The API token has no expiry; it is not a session.
        self.assertTrue(gate.accepts(self.TOKEN, now=99999.0))

    def test_a_server_with_no_pin_refuses_to_listen(self):
        """Switching the remote on must never quietly mean opening the LAN."""
        server = self.lib.RemoteServer(port=_free_port(),
                                       gate=self.lib.Gate('', self.TOKEN),
                                       address='127.0.0.1')

        self.assertFalse(server.start())
        self.assertFalse(server.running())

    # -- doing things ------------------------------------------------------

    def test_an_action_runs_on_the_service_loop_and_not_the_handler(self):
        """The single-threaded session is the point of the queue."""
        client = self.signed_in()

        self.assertTrue(client.act('off')['data'].get('ok'))

        self.assertEqual(self.recorder.threads, set([self.loop.thread_id]))
        self.assertNotIn(threading.current_thread().ident,
                         self.recorder.threads)

    def test_off_reaches_every_light_and_plug(self):
        client = self.signed_in()

        answer = client.act('off')

        self.assertEqual(answer['status'], 200)
        self.assertTrue(answer['data'].get('ok'))
        self.assertIn(('turn', 'AA:BB', False), self.recorder.calls)
        self.assertIn(('turn', 'CC:DD', False), self.recorder.calls)

    def test_a_named_target_reaches_only_that_one(self):
        client = self.signed_in()

        self.assertTrue(client.act('on', target='Desk Plug')['data']['ok'])

        self.assertEqual(self.recorder.calls, [('turn', 'CC:DD', True)])

    def test_a_target_that_is_not_here_is_said_so_rather_than_ignored(self):
        client = self.signed_in()

        answer = client.act('off', target='Greenhouse')

        self.assertEqual(answer['status'], 200)
        self.assertFalse(answer['data']['ok'])
        self.assertIn('Greenhouse', answer['data']['message'])
        self.assertEqual(self.recorder.calls, [])

    def test_brightness_and_colour_and_temperature_arrive_as_asked(self):
        client = self.signed_in()

        client.act('brightness', value=20, target='Living Room')
        client.act('color', value='FF8800', target='Living Room')
        client.act('temp', value=2700, target='Living Room')

        self.assertIn(('brightness', 'AA:BB', 20), self.recorder.calls)
        self.assertIn(('color', 'AA:BB', 255, 136, 0), self.recorder.calls)
        self.assertIn(('temp', 'AA:BB', 2700), self.recorder.calls)

    def test_a_colour_can_be_named_from_the_speed_dial(self):
        """The same names a keymap can use, without copying hex about."""
        client = self.signed_in()
        saved = self.app.palette[0]

        self.assertTrue(client.act('color', value=saved['name'],
                                   target='Living Room')['data']['ok'])

        self.assertIn(('color', 'AA:BB') + tuple(saved['color']),
                      self.recorder.calls)

    def test_an_out_of_range_brightness_is_clamped_not_refused(self):
        client = self.signed_in()

        client.act('brightness', value=900, target='Living Room')

        self.assertIn(('brightness', 'AA:BB', 100), self.recorder.calls)

    def test_a_brightness_that_is_not_a_number_is_refused(self):
        client = self.signed_in()

        answer = client.act('brightness', value='bright', target='Living Room')

        self.assertFalse(answer['data']['ok'])
        self.assertEqual(self.recorder.calls, [])

    def test_a_learned_code_can_be_fired(self):
        client = self.signed_in()

        answer = client.act('command', target='Hall Blaster', name='TV Power')

        self.assertTrue(answer['data']['ok'])
        self.assertIn(('command', 'EE:FF', 'TV Power'), self.recorder.calls)

    def test_a_code_needs_the_blaster_to_send_it_from(self):
        """There is no "all": a code belongs to the blaster that learned it."""
        client = self.signed_in()

        answer = client.act('command', name='TV Power')

        self.assertFalse(answer['data']['ok'])
        self.assertIn('blaster', answer['data']['message'])
        self.assertEqual(self.recorder.calls, [])

    def test_a_code_needs_a_name(self):
        client = self.signed_in()

        answer = client.act('command', target='Hall Blaster')

        self.assertFalse(answer['data']['ok'])
        self.assertEqual(self.recorder.calls, [])

    def test_a_code_aimed_at_something_that_does_not_send_them(self):
        client = self.signed_in()

        answer = client.act('command', target='Living Room', name='TV Power')

        self.assertFalse(answer['data']['ok'])
        self.assertIn('sends commands', answer['data']['message'])
        self.assertEqual(self.recorder.calls, [])

    def test_a_scene_runs_by_name(self):
        client = self.signed_in()

        answer = client.act('scene', name='All Off')

        self.assertTrue(answer['data']['ok'])
        self.assertTrue(self.recorder.calls)

    def test_a_scene_that_does_not_exist_is_named_in_the_answer(self):
        client = self.signed_in()

        answer = client.act('scene', name='Disco Inferno')

        self.assertFalse(answer['data']['ok'])
        self.assertIn('Disco Inferno', answer['data']['message'])

    def test_an_unknown_action_is_refused_before_it_is_queued(self):
        client = self.signed_in()

        answer = client.act('detonate')

        self.assertEqual(answer['status'], 400)
        self.assertEqual(self.server.commands.pending(), 0)

    def test_a_malformed_body_is_refused(self):
        client = self.signed_in()

        answer = client.call('POST', '/api/action', '{not json')

        self.assertEqual(answer['status'], 400)
        self.assertEqual(self.recorder.calls, [])

    def test_a_sequence_says_it_started_rather_than_waiting_for_it_to_end(self):
        """A sequence can hold an hour of pauses; a phone cannot wait for it."""
        import sequences as sequence_lib

        self.app._sequences = [sequence_lib.make_sequence('Bedtime')]
        client = self.signed_in()

        answer = client.act('sequence', name='Bedtime')

        self.assertEqual(answer['status'], 202)
        self.assertTrue(answer['data']['queued'])

    def test_sequences_can_be_switched_off_for_the_remote(self):
        """A phone in a pocket should not be able to start the bedtime run."""
        import sequences as sequence_lib

        self.app._sequences = [sequence_lib.make_sequence('Bedtime')]
        client = self.signed_in(allow_sequences=False)

        answer = client.act('sequence', name='Bedtime')

        self.assertEqual(answer['status'], 403)
        self.assertFalse(client.state()['data']['allow_sequences'])

    def test_a_second_sequence_cannot_start_inside_the_first(self):
        """The rule the scheduler already follows, kept when a phone can ask."""
        import sequences as sequence_lib

        self.app._sequences = [sequence_lib.make_sequence('Bedtime')]
        client = self.serve(run_loop=False)
        self.server.sequence_running = True

        job = self.server.commands.submit('sequence', {'name': 'Bedtime'})
        self.server.pump(self.app)

        self.assertTrue(job.done.is_set())
        self.assertFalse(job.result['ok'])
        self.assertIn('already running', job.result['message'])
        self.assertEqual(self.recorder.calls, [])
        client.cookie = None  # nothing signed in; the client is only the port

    def test_a_failing_light_answers_rather_than_leaving_the_phone_waiting(self):
        self.recorder.fail_on = set(['AA:BB'])
        client = self.signed_in()

        answer = client.act('off', target='Living Room')

        self.assertEqual(answer['status'], 200)
        self.assertFalse(answer['data']['ok'])
        self.assertIn('unreachable', answer['data']['message'])

    def test_sync_on_a_box_that_is_not_a_satellite_does_nothing(self):
        """It answers before it runs, so the refusal lands in the log."""
        client = self.signed_in()

        answer = client.act('sync')
        self.assertEqual(answer['status'], 202)

        job = self.server.commands.submit('sync', {})
        self.server.pump(self.app)

        self.assertFalse(job.result['ok'])
        self.assertIn('not a satellite', job.result['message'])

    # -- what the page is shown --------------------------------------------

    def test_the_snapshot_lists_everything_with_something_to_press(self):
        """Including the blasters, which have codes rather than a switch."""
        client = self.signed_in()

        state = client.state()['data']
        by_name = dict((d['name'], d) for d in state['devices'])

        self.assertIn('Living Room', by_name)
        self.assertIn('Desk Plug', by_name)
        self.assertIn('Hall Blaster', by_name)
        self.assertEqual(by_name['Hall Blaster']['commands'],
                         ['TV Power', 'Volume Up'])
        self.assertIn('broadlink', [d['id'] for d in state['drivers']])

    def test_a_blaster_with_nothing_learned_is_still_listed(self):
        """Knowing it is found and reachable is most of what you wanted."""
        self.recorder.command_map = {}
        client = self.signed_in()

        blasters = [d for d in client.state()['data']['devices']
                    if d['driver'] == 'broadlink']

        self.assertEqual(len(blasters), 1)
        self.assertEqual(blasters[0]['commands'], [])

    def test_the_snapshot_carries_the_scenes_sequences_and_palette(self):
        import sequences as sequence_lib

        self.app._sequences = [sequence_lib.make_sequence('Bedtime')]
        client = self.signed_in()

        state = client.state()['data']

        self.assertTrue(state['ready'])
        self.assertIn('All Off', [s['name'] for s in state['scenes']])
        self.assertEqual([s['name'] for s in state['sequences']], ['Bedtime'])
        self.assertTrue(state['palette'])
        self.assertEqual(len(state['palette'][0]['hex']), 6)

    def test_the_snapshot_says_when_this_box_is_only_a_satellite(self):
        """A satellite's copy can be a quarter of an hour old; say so."""
        xbmcaddon.SETTINGS['satellite_mode'] = 'true'
        xbmcaddon.SETTINGS['master_ip'] = '192.168.1.10'
        client = self.signed_in()

        satellite = client.state()['data']['satellite']

        self.assertTrue(satellite['mode'])
        self.assertEqual(satellite['master'], '192.168.1.10')

    def test_a_device_name_cannot_smuggle_markup_into_the_page(self):
        """Names come from the Govee app, so they are not to be trusted."""
        self.app._devices = [Device('AA:BB', name='<img onerror=alert(1)>',
                                    lan=True)]
        client = self.signed_in()

        state = client.state()['data']

        # It survives as text -- the page puts it in with textContent, and the
        # page itself never carries a device name.
        self.assertEqual(state['devices'][0]['name'], '<img onerror=alert(1)>')
        self.assertNotIn(b'onerror', self.lib.PAGE.encode('utf-8'))

    def test_a_state_read_is_only_done_when_it_is_asked_for(self):
        """One round trip per light is not something to do twice a second."""
        self.recorder.states = {'AA:BB': {'power': 'on', 'brightness': 40}}
        client = self.signed_in()

        self.assertIsNone(client.state()['data']['devices'][0]['power'])

        self.assertTrue(client.act('states')['data']['ok'])
        after = client.state()['data']['devices'][0]

        self.assertEqual(after['power'], 'on')
        self.assertEqual(after['brightness'], 40)

    # -- the queue ---------------------------------------------------------

    def test_a_full_queue_refuses_work_rather_than_piling_it_up(self):
        queue = self.lib.Commands(limit=2)

        self.assertIsNotNone(queue.submit('off', {}))
        self.assertIsNotNone(queue.submit('off', {}))
        self.assertIsNone(queue.submit('off', {}))
        self.assertEqual(queue.pending(), 2)

    def test_stopping_answers_whoever_is_still_waiting(self):
        """Otherwise a handler blocks to its timeout on a job nobody will run."""
        queue = self.lib.Commands()
        job = queue.submit('off', {})

        queue.close()
        queue.abandon()

        self.assertTrue(job.done.is_set())
        self.assertFalse(job.result['ok'])
        self.assertIsNone(queue.submit('off', {}))

    def test_the_server_stops_cleanly_and_stops_answering(self):
        client = self.signed_in()
        self.assertEqual(client.state()['status'], 200)

        self.loop.stop()
        self.loop = None
        self.server.stop()
        server, self.server = self.server, None

        self.assertFalse(server.running())
        self.assertRaises(Exception, client.state)

    # -- secrets and settings ----------------------------------------------

    def test_a_pin_is_made_the_first_time_and_then_kept(self):
        first = self.lib.ensure_pin()

        self.assertEqual(len(first), self.lib.PIN_LENGTH)
        self.assertTrue(first.isdigit())
        self.assertEqual(self.lib.ensure_pin(), first)
        self.assertEqual(xbmcaddon.SETTINGS['remote_pin'], first)

    def test_the_api_token_is_kept_across_a_restart(self):
        first = self.lib.ensure_token()

        self.assertEqual(len(first), self.lib.TOKEN_BYTES * 2)
        self.assertEqual(self.lib.ensure_token(), first)

    def test_two_pins_in_a_row_are_not_the_same(self):
        made = set(self.lib.generate_pin() for _ in range(50))

        self.assertGreater(len(made), 40)

    def test_the_address_dialog_says_where_to_go_and_what_to_type(self):
        xbmcaddon.SETTINGS['remote_enabled'] = 'true'
        xbmcaddon.SETTINGS['remote_port'] = '8778'

        text = self.lib.describe(pin='424242', address='192.168.1.50')

        self.assertIn('http://192.168.1.50:8778', text)
        self.assertIn('424242', text)

    def test_the_dialog_says_so_when_the_remote_is_switched_off(self):
        text = self.lib.describe(pin='424242', address='192.168.1.50')

        self.assertIn('switched off', text)

    def test_comparing_secrets_does_not_stop_at_the_first_difference(self):
        from compat import same_secret

        self.assertTrue(same_secret('123456', '123456'))
        self.assertFalse(same_secret('123456', '123457'))
        self.assertFalse(same_secret('123456', '12345'))
        self.assertFalse(same_secret('', 'x'))
        self.assertTrue(same_secret('', ''))


class FakeSwitchBotAPI(object):
    """A stand-in for SwitchBot's HTTPS endpoints.

    Records what was asked of it rather than answering cleverly: the point of
    most of these tests is which command went out, not what came back.
    """

    def __init__(self, entries=None, status=None, fail=None):
        self._entries = entries if entries is not None else [
            {'deviceId': 'BT01', 'deviceName': 'Lounge Blinds',
             'deviceType': 'Blind Tilt', 'hubDeviceId': 'HUB2'},
            {'deviceId': 'BOT1', 'deviceName': 'Kettle Bot',
             'deviceType': 'Bot', 'hubDeviceId': 'HUB2'},
            {'deviceId': 'HUB2', 'deviceName': 'Hub 2',
             'deviceType': 'Hub 2', 'hubDeviceId': ''},
        ]
        self._status = status or {'slidePosition': 40, 'battery': 88}
        self._fail = fail
        self.sent = []
        self.configured = True

    def devices(self):
        if self._fail:
            raise self._fail
        return list(self._entries)

    def status(self, device_id):
        if self._fail:
            raise self._fail
        return dict(self._status)

    def command(self, device_id, command, parameter='default',
                command_type='command'):
        if self._fail:
            raise self._fail
        self.sent.append((device_id, command, parameter))
        return {}


def a_blind(name='Lounge Blinds', device_id='BT01', model='Blind Tilt'):
    return Device(device_id, name=name, model=model, cloud=True,
                  driver='switchbot')


class TestSwitchBotSigning(unittest.TestCase):
    """Every request is signed; a wrong signature is a 401 and no blinds."""

    def transport(self, **kwargs):
        import switchbot_cloud
        return switchbot_cloud.SwitchBotTransport(
            token='TOKEN123', secret='SECRET456', **kwargs)

    def test_the_signature_is_hmac_sha256_of_token_stamp_and_nonce(self):
        import base64
        import hashlib
        import hmac

        headers = self.transport()._sign(now=1700000000.0)

        expected = base64.b64encode(hmac.new(
            b'SECRET456',
            msg=('TOKEN123' + headers['t'] + headers['nonce']).encode('utf-8'),
            digestmod=hashlib.sha256).digest()).decode('ascii')
        self.assertEqual(headers['sign'], expected)

    def test_the_timestamp_is_in_milliseconds(self):
        """Seconds would be rejected outright: the API wants epoch millis."""
        headers = self.transport()._sign(now=1700000000.0)

        self.assertEqual(headers['t'], '1700000000000')

    def test_the_token_is_sent_as_the_authorization(self):
        self.assertEqual(self.transport()._sign(now=1.0)['Authorization'],
                         'TOKEN123')

    def test_two_requests_do_not_reuse_a_nonce(self):
        """A replayed nonce is what a nonce exists to stop."""
        first = self.transport()._sign(now=1.0)['nonce']
        second = self.transport()._sign(now=1.0)['nonce']

        self.assertNotEqual(first, second)

    def test_half_a_credential_is_not_configured(self):
        import switchbot_cloud

        make = switchbot_cloud.SwitchBotTransport
        self.assertTrue(make('t', 's').configured)
        self.assertFalse(make('t', '').configured)
        self.assertFalse(make('', 's').configured)


class TestSwitchBotDriver(unittest.TestCase):

    def driver(self, api=None):
        import switchbot_driver
        api = api if api is not None else FakeSwitchBotAPI()
        return switchbot_driver.SwitchBotDriver(transport=api), api

    # -- discovery ---------------------------------------------------------

    def test_only_the_covers_are_adopted(self):
        """A bot and a hub are on the same account and are not blinds."""
        driver, _api = self.driver()

        found, warnings = driver.discover()

        self.assertEqual([d.device_id for d in found], ['BT01'])
        self.assertEqual(found[0].name, 'Lounge Blinds')
        self.assertEqual(found[0].model, 'Blind Tilt')
        self.assertEqual(warnings, [])

    def test_an_account_with_no_blinds_says_so(self):
        api = FakeSwitchBotAPI(entries=[
            {'deviceId': 'BOT1', 'deviceName': 'Bot', 'deviceType': 'Bot'}])
        driver, _api = self.driver(api)

        found, warnings = driver.discover()

        self.assertEqual(found, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn('none of them a blind', warnings[0])

    def test_a_search_with_no_credentials_is_quiet(self):
        """Not an error. Most installs will never have a SwitchBot account."""
        import switchbot_driver

        driver = switchbot_driver.SwitchBotDriver(transport=None)
        self.assertEqual(driver.discover(), ([], []))

    def test_a_failed_search_warns_rather_than_raising(self):
        import switchbot_cloud

        api = FakeSwitchBotAPI(fail=switchbot_cloud.CloudError('no route'))
        driver, _api = self.driver(api)

        found, warnings = driver.discover()

        self.assertEqual(found, [])
        self.assertIn('no route', warnings[0])

    # -- what it claims to be ----------------------------------------------

    def test_a_blind_is_not_a_light_and_not_a_plug(self):
        """The whole reason CAP_POSITION exists.

        Reported as a plug it would be shut by every "everything off" path in
        the add-on; reported as a light it would be dimmed by scenes.
        """
        from devices import (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP,
                             CAP_POSITION)

        driver, _api = self.driver()
        caps = driver.capabilities(a_blind())

        self.assertIn(CAP_POSITION, caps)
        for light_only in (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP):
            self.assertNotIn(light_only, caps)

    def test_a_scene_passes_over_a_blind(self):
        """A scene describes how a room looks. A blind is not in that set."""
        import hub as hub_mod
        import scenes as scene_lib
        import switchbot_driver

        hub = hub_mod.Hub(drivers=[switchbot_driver.SwitchBotDriver(
            transport=FakeSwitchBotAPI())])
        blind = a_blind()

        self.assertFalse(scene_lib.is_a_light(blind, hub))
        self.assertEqual(
            scene_lib.scene_targets(scene_lib.make_scene('All Off'),
                                    [blind], hub),
            [])

    # -- positions ---------------------------------------------------------

    def test_a_position_is_clamped_and_made_even(self):
        """A tilt rejects an odd position rather than rounding it."""
        import switchbot_driver

        clean = switchbot_driver.clean_position
        self.assertEqual(clean(51), 50)
        self.assertEqual(clean(-20), 0)
        self.assertEqual(clean(500), 100)
        self.assertEqual(clean('62'), 62)

    def test_nonsense_is_refused_rather_than_sent(self):
        import switchbot_driver
        from devices import ControlError

        self.assertRaises(ControlError,
                          switchbot_driver.clean_position, 'shut')

    def test_setting_a_position_sends_a_direction_with_it(self):
        driver, api = self.driver()

        driver.set_position(a_blind(), 61)

        self.assertEqual(api.sent, [('BT01', 'setPosition', 'up;60')])

    def test_open_and_close_use_the_tilt_commands(self):
        """turnOn on a tilt is not what "open" means; fullyOpen is."""
        driver, api = self.driver()
        blind = a_blind()

        driver.turn(blind, True)
        driver.turn(blind, False)

        self.assertEqual([c for _id, c, _p in api.sent],
                         ['fullyOpen', 'closeDown'])

    def test_a_curtain_is_switched_the_ordinary_way(self):
        driver, api = self.driver()
        curtain = a_blind(name='Bedroom Curtain', device_id='CT01',
                          model='Curtain3')

        driver.turn(curtain, True)

        self.assertEqual([c for _id, c, _p in api.sent], ['turnOn'])

    def test_the_named_commands_are_the_hardware_own_vocabulary(self):
        driver, api = self.driver()

        self.assertEqual(driver.commands(a_blind()),
                         ['Open', 'Close Up', 'Close Down', 'Pause'])
        driver.send_command(a_blind(), 'Close Up')
        self.assertEqual(api.sent, [('BT01', 'closeUp', 'default')])

    def test_an_unknown_command_is_refused(self):
        from devices import ControlError

        driver, _api = self.driver()
        self.assertRaises(ControlError,
                          driver.send_command, a_blind(), 'Fly Away')

    def test_it_is_not_a_light_and_says_so_plainly(self):
        from devices import ControlError

        driver, _api = self.driver()
        self.assertRaises(ControlError, driver.set_brightness, a_blind(), 50)
        self.assertRaises(ControlError, driver.set_color, a_blind(), 1, 2, 3)

    # -- reading -----------------------------------------------------------

    def test_it_reports_where_the_blind_is(self):
        driver, _api = self.driver()

        state = driver.get_state(a_blind())

        self.assertEqual(state['position'], 40)
        self.assertEqual(state['battery'], 88)

    def test_an_unreadable_blind_is_none_rather_than_an_error(self):
        import switchbot_cloud

        api = FakeSwitchBotAPI(fail=switchbot_cloud.CloudError('offline'))
        driver, _api = self.driver(api)

        self.assertIsNone(driver.get_state(a_blind()))


class TestPositionThroughTheHub(unittest.TestCase):

    def hub(self):
        import hub as hub_mod
        import switchbot_driver

        api = FakeSwitchBotAPI()
        return hub_mod.Hub(drivers=[
            switchbot_driver.SwitchBotDriver(transport=api),
            GoveeController()]), api

    def test_the_hub_routes_a_position_to_the_driver_that_owns_it(self):
        hub, api = self.hub()

        hub.set_position(a_blind(), 30)

        self.assertEqual(api.sent, [('BT01', 'setPosition', 'up;30')])

    def test_a_light_cannot_be_asked_for_a_position(self):
        """The verb exists on the Hub, so the refusal has to live there."""
        from devices import ControlError

        hub, _api = self.hub()
        lamp = Device('AA:BB', name='Lamp', lan=True, driver='govee')

        self.assertRaises(ControlError, hub.set_position, lamp, 30)


class TestBlindsInSequences(unittest.TestCase):
    """A blind belongs in a sequence, which already switches plugs and fires
    infrared. Scenes stay what they are: lights only."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'sequences', 'gui'):
            if name in sys.modules:
                del sys.modules[name]
        import sequences

        self.sequences = sequences

    def tearDown(self):
        clean_profile()

    def test_a_position_step_survives_being_saved(self):
        step = self.sequences.normalise_step(
            {'kind': 'position', 'driver': 'switchbot', 'target': 'BT01',
             'action': '40'})

        self.assertEqual(step['kind'], self.sequences.KIND_POSITION)
        self.assertEqual(step['action'], '40')

    def test_a_position_is_clamped_when_it_is_read_back(self):
        step = self.sequences.normalise_step(
            {'kind': 'position', 'target': 'BT01', 'action': '250'})

        self.assertEqual(step['action'], '100')

    def test_a_step_with_no_number_is_an_empty_slot(self):
        """Half a step that looks filled is worse than a blank one."""
        step = self.sequences.normalise_step(
            {'kind': 'position', 'target': 'BT01', 'action': 'shut'})

        self.assertEqual(step['kind'], self.sequences.KIND_NONE)

    def test_a_step_with_no_target_is_an_empty_slot(self):
        step = self.sequences.normalise_step(
            {'kind': 'position', 'target': '', 'action': '40'})

        self.assertEqual(step['kind'], self.sequences.KIND_NONE)

    def test_the_step_reads_as_what_it_does(self):
        step = self.sequences.normalise_step(
            {'kind': 'position', 'target': 'BT01', 'action': '40'})

        self.assertEqual(
            self.sequences.describe_step(step, device_name='Lounge Blinds'),
            'Lounge Blinds: 40% open')

class TestTuyaCovers(unittest.TestCase):
    """A roller shade on the LAN, against a device speaking the real protocol.

    Tuya sends a shade and a plug down the same datapoints, so the driver has
    to tell them apart from what comes back rather than from a table of
    product ids. Datapoint 1 is the whole tell: a bool on a plug, one of
    'open'/'stop'/'close' on a cover.

    This matters more than it looks. Adopted as a plug -- which is what the
    driver did before this -- a shade would claim CAP_POWER and nothing else,
    and every "switch everything off" path in the add-on would have shut the
    blinds as a side effect of turning off a lamp.
    """

    KEY = '0123456789abcdef'
    # What a battery roller shade answers with. Position 40, target 40,
    # battery 86 -- and datapoint 1 a string, which is the discriminator.
    SHADE_DPS = {'1': 'stop', '2': 40, '3': 40, '7': 'opening', '13': 86}

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub', 'scenes'):
            if name in sys.modules:
                del sys.modules[name]
        import tuya_lan

        self.tuya_lan = tuya_lan
        self.real_port = tuya_lan.CONTROL_PORT
        self.plug = None

    def tearDown(self):
        self.tuya_lan.CONTROL_PORT = self.real_port
        if self.plug is not None:
            crashed = self.plug.crashed
            self.plug.close()
            self.assertIsNone(crashed, 'the fake shade crashed: %r' % crashed)
        clean_profile()

    def start(self, dps=None):
        self.plug = FakeTuyaPlug(key=self.KEY.encode('utf-8'),
                                 dps=dict(dps or self.SHADE_DPS))
        self.tuya_lan.CONTROL_PORT = self.plug.port
        return self.plug

    def driver(self):
        from tuya_driver import TuyaDriver

        return TuyaDriver(keys={'sh4de1': self.KEY}, timeout=3.0)

    def shade(self):
        return Device('sh4de1', name='Lounge Shade', driver='tuya',
                      ip='127.0.0.1', lan=True, native_id='sh4de1',
                      driver_data={'version': '3.3', 'cover': True})

    def plug_device(self):
        return Device('wp9abc', name='Lamp Plug', driver='tuya',
                      ip='127.0.0.1', lan=True, native_id='wp9abc',
                      driver_data={'version': '3.3'})

    # -- telling one from the other ----------------------------------------

    def test_a_string_on_datapoint_one_is_what_marks_a_cover(self):
        import tuya_driver

        self.assertTrue(tuya_driver.looks_like_a_cover({'1': 'open'}))
        self.assertTrue(tuya_driver.looks_like_a_cover({'1': 'stop'}))
        self.assertTrue(tuya_driver.looks_like_a_cover({'1': 'close'}))

    def test_a_plug_is_not_mistaken_for_a_cover(self):
        """Datapoint 1 on a plug is a bool, and False is not 'close'."""
        import tuya_driver

        self.assertFalse(tuya_driver.looks_like_a_cover({'1': False}))
        self.assertFalse(tuya_driver.looks_like_a_cover({'1': True}))
        self.assertFalse(tuya_driver.looks_like_a_cover({}))

    def test_discovery_marks_a_shade_and_does_not_split_it(self):
        """A shade answers on several datapoints and is still one device."""
        self.start()
        driver = self.driver()

        found = driver._devices_for({'device_id': 'sh4de1', 'ip': '127.0.0.1',
                                     'version': '3.3'})

        self.assertEqual(len(found), 1)
        self.assertTrue(driver.is_cover(found[0]))

    # -- what it claims to be ----------------------------------------------

    def test_a_shade_claims_a_position_and_not_a_brightness(self):
        from devices import CAP_BRIGHTNESS, CAP_COLOR, CAP_POSITION

        caps = self.driver().capabilities(self.shade())

        self.assertIn(CAP_POSITION, caps)
        self.assertNotIn(CAP_BRIGHTNESS, caps)
        self.assertNotIn(CAP_COLOR, caps)

    def test_a_plug_is_unchanged_by_any_of_this(self):
        from devices import CAP_POSITION, CAP_POWER, CAP_STATE

        caps = self.driver().capabilities(self.plug_device())

        self.assertEqual(caps, set([CAP_POWER, CAP_STATE]))
        self.assertNotIn(CAP_POSITION, caps)

    def test_a_scene_passes_over_a_shade(self):
        """The reason this could not just be adopted as a plug."""
        import hub as hub_mod
        import scenes as scene_lib

        hub = hub_mod.Hub(drivers=[self.driver()])

        self.assertFalse(scene_lib.is_a_light(self.shade(), hub))
        self.assertEqual(
            scene_lib.scene_targets(scene_lib.make_scene('All Off'),
                                    [self.shade()], hub),
            [])

    # -- driving it --------------------------------------------------------

    def test_a_position_goes_to_the_target_datapoint(self):
        plug = self.start()

        self.driver().set_position(self.shade(), 65)

        self.assertEqual(plug.dps['2'], 65)

    def test_a_position_is_clamped_to_what_a_shade_can_do(self):
        import tuya_driver

        clean = tuya_driver.clean_position
        self.assertEqual(clean(-40), 0)
        self.assertEqual(clean(140), 100)
        self.assertEqual(clean('65'), 65)

    def test_nonsense_is_refused_rather_than_sent(self):
        import tuya_driver
        from devices import ControlError

        self.assertRaises(ControlError, tuya_driver.clean_position, 'shut')

    def test_on_and_off_mean_open_and_closed(self):
        """A sequence step and the web remote only know how to switch."""
        plug = self.start()
        driver = self.driver()

        driver.turn(self.shade(), True)
        self.assertEqual(plug.dps['1'], 'open')
        driver.turn(self.shade(), False)
        self.assertEqual(plug.dps['1'], 'close')

    def test_stop_is_offered_because_it_has_no_percentage(self):
        plug = self.start()
        driver = self.driver()

        self.assertEqual(driver.commands(self.shade()),
                         ['Open', 'Stop', 'Close'])
        driver.send_command(self.shade(), 'Stop')
        self.assertEqual(plug.dps['1'], 'stop')

    def test_a_plug_is_still_offered_no_commands(self):
        self.assertEqual(self.driver().commands(self.plug_device()), [])

    # -- reading it --------------------------------------------------------

    def test_it_reports_where_the_shade_actually_is(self):
        self.start()

        state = self.driver().get_state(self.shade())

        self.assertEqual(state['position'], 40)
        self.assertEqual(state['battery'], 86)

    def test_the_target_stands_in_when_a_motor_reports_no_position(self):
        """Cheaper motors answer only with what they were last told."""
        self.start(dps={'1': 'stop', '2': 70})

        state = self.driver().get_state(self.shade())

        self.assertEqual(state['position'], 70)

    def test_a_shade_that_reports_neither_says_nothing(self):
        """Better than reporting 0, which reads as "shut" and is a guess."""
        self.start(dps={'1': 'stop'})

        self.assertIsNone(self.driver().get_state(self.shade()))

class TestTheQuietButton(unittest.TestCase):
    """The remote's mute button sets a level instead of silencing the room.

    Quiet enough to talk over, not so quiet the television feels switched
    off. Kodi cannot be told a decibel figure -- Application.SetVolume takes
    an integer percentage -- so the level is kept in dB, which is the unit
    the volume bar shows, and converted on the way out.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        xbmc.reset_rpc()
        for name in ('addon_utils', 'paragon_home', 'tv', 'remote'):
            if name in sys.modules:
                del sys.modules[name]
        import tv

        self.tv = tv

    def tearDown(self):
        clean_profile()

    def at(self, percent):
        """Tell the stub Kodi the volume is here."""
        xbmc.rpc_results['Application.GetProperties'] = {
            'volume': percent, 'muted': False}

    def volume_commands(self):
        """Every SetVolume builtin the code ran, as its argument list."""
        return [command[len('SetVolume('):-1].split(',')
                for command in xbmc.BUILTINS
                if command.startswith('SetVolume(')]

    def volumes_set(self):
        """Just the percentages, as floats."""
        return [float(args[0]) for args in self.volume_commands()]

    # -- the arithmetic ----------------------------------------------------

    def test_the_level_converts_to_the_percentage_kodi_takes(self):
        """-39.3 dB on a -60 dB floor is 34.5%."""
        self.assertAlmostEqual(self.tv.db_to_percent(-39.3), 34.5, places=3)

    def test_it_converts_back(self):
        self.assertAlmostEqual(self.tv.percent_to_db(34.5), -39.3, places=3)

    def test_the_ends_are_the_ends(self):
        self.assertAlmostEqual(self.tv.db_to_percent(0.0), 100.0)
        self.assertAlmostEqual(self.tv.db_to_percent(-60.0), 0.0)

    def test_a_level_past_the_floor_is_clamped(self):
        self.assertAlmostEqual(self.tv.db_to_percent(-200.0), 0.0)
        self.assertAlmostEqual(self.tv.db_to_percent(40.0), 100.0)

    def test_a_nonsense_floor_does_not_divide_by_zero(self):
        """A floor at or above zero leaves no range. Full volume, not a crash
        on a button press."""
        self.assertAlmostEqual(self.tv.db_to_percent(-39.3, floor=0.0), 100.0)

    # -- pressing it -------------------------------------------------------

    def test_a_loud_television_goes_quiet(self):
        self.at(80.0)

        ok, message = self.tv.press('mute')

        self.assertTrue(ok)
        self.assertAlmostEqual(self.volumes_set()[-1], 34.5, places=1)
        self.assertIn('-39.3', message)

    def test_a_quiet_television_comes_back_up(self):
        """The second press is the way back, not another trip down."""
        self.at(34.5)

        ok, message = self.tv.press('mute')

        self.assertTrue(ok)
        # -20 dB on a -60 floor is 66.67%.
        self.assertAlmostEqual(self.volumes_set()[-1], 66.67, places=1)
        self.assertIn('-20.0', message)

    def test_the_midpoint_decides_not_a_remembered_flag(self):
        """A level neither button set still goes the sensible way.

        Nothing here remembers which way it went last: the volume can be
        moved by the volume keys, a Kodi remote or another script, and a
        button that has to be pressed twice because it believes it is
        somewhere it is not is worse than one that looks first.
        """
        # Just below the midpoint of -39.3 and -20.0, so: back up.
        self.at(self.tv.db_to_percent(-32.0))
        self.tv.press('mute')
        self.assertAlmostEqual(self.volumes_set()[-1], 66.67, places=1)

        # Just above it, so: down.
        xbmc.reset_rpc()
        self.at(self.tv.db_to_percent(-27.0))
        self.tv.press('mute')
        self.assertAlmostEqual(self.volumes_set()[-1], 34.5, places=1)

    def test_kodis_own_mute_is_cleared_on_the_way(self):
        """A television muted from the Kodi remote comes back, rather than
        silently sitting at a new level."""
        self.at(80.0)

        self.tv.press('mute')

        unmutes = [call for call in xbmc.rpc_calls
                   if call.get('method') == 'Application.SetMute'
                   and call.get('params', {}).get('mute') is False]
        self.assertEqual(len(unmutes), 1)

    def test_it_no_longer_toggles_kodis_mute(self):
        """The whole point. A toggle here would silence the room."""
        self.at(80.0)

        self.tv.press('mute')

        toggles = [call for call in xbmc.rpc_calls
                   if call.get('params', {}).get('mute') == 'toggle']
        self.assertEqual(toggles, [])

    def test_an_unreadable_volume_changes_nothing(self):
        """Better than guessing at a level from nothing."""
        xbmc.rpc_results['Application.GetProperties'] = {'muted': False}

        ok, message = self.tv.press('mute')

        self.assertFalse(ok)
        self.assertEqual(self.volumes_set(), [])
        self.assertIn('volume', message.lower())

    def test_the_volume_bar_comes_up_like_it_does_for_the_volume_keys(self):
        """Pressing Quiet from a phone should look like the remote on screen.

        The word matters: Kodi compares the second parameter against the
        literal "showvolumebar" and silently ignores anything else, so
        "true" leaves the bar off with nothing going wrong to notice.
        """
        self.at(80.0)

        self.tv.press('mute')

        args = self.volume_commands()[-1]
        self.assertEqual(len(args), 2,
                         'SetVolume needs its second parameter for the bar')
        self.assertEqual(args[1], 'showvolumebar')

    def test_the_bar_comes_up_going_back_up_too(self):
        self.at(34.5)

        self.tv.press('mute')

        self.assertEqual(self.volume_commands()[-1][1], 'showvolumebar')

    # -- the levels are settings -------------------------------------------

    def test_the_levels_come_from_settings(self):
        xbmcaddon.SETTINGS['tv_quiet_db'] = '-45.0'
        xbmcaddon.SETTINGS['tv_normal_db'] = '-12.0'
        self.at(90.0)

        self.tv.press('mute')

        # -45 on a -60 floor is 25%.
        self.assertAlmostEqual(self.volumes_set()[-1], 25.0, places=1)

    def test_a_setting_that_is_not_a_number_falls_back(self):
        """A half-typed level should not stop the button working."""
        xbmcaddon.SETTINGS['tv_quiet_db'] = 'minus a lot'
        self.at(90.0)

        self.tv.press('mute')

        self.assertAlmostEqual(self.volumes_set()[-1], 34.5, places=1)

    # -- what the page draws from ------------------------------------------

    def test_the_state_says_when_the_television_is_quiet(self):
        """What lights the button, read from the volume rather than a flag."""
        self.at(34.5)
        self.assertTrue(self.tv.player_state()['quiet'])

        xbmc.reset_rpc()
        self.at(80.0)
        self.assertFalse(self.tv.player_state()['quiet'])

    def test_a_nudge_of_the_volume_keys_does_not_strand_the_button(self):
        """Within the tolerance is still at the level."""
        self.at(self.tv.db_to_percent(-38.5))
        self.assertTrue(self.tv.player_state()['quiet'])

class TestTuningByNumber(unittest.TestCase):
    """Tapping a channel in the list goes to that channel.

    It did not. Paragon TV reads the first digit of a fresh entry as a speed
    dial and jumps straight there -- handleNumberAction takes the speed dial
    branch and returns before the second digit arrives -- so channel 14 on a
    box with a speed dial on 1 went wherever speed dial 1 pointed. Every
    channel from 10 to 49 was affected, and so was every single-digit channel
    with a speed dial on it, which is the half nobody noticed.

    That behaviour is right for the remote in your hand, where pressing 1
    means speed dial 1, so the fix belongs here rather than in Paragon TV: a
    channel tapped in a list is the channel that was asked for.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        xbmc.reset_rpc()
        for name in ('addon_utils', 'paragon_home', 'tv', 'remote'):
            if name in sys.modules:
                del sys.modules[name]
        import tv

        self.tv = tv
        # tune refuses when the television is not on; say that it is.
        window = xbmcgui.Window(tv.HOME_WINDOW)
        window.setProperty(tv.PROP_CHANNEL, '7')

    def tearDown(self):
        clean_profile()

    def actions(self):
        """Every Action() builtin the code ran, in order."""
        return [command[len('Action('):-1] for command in xbmc.BUILTINS
                if command.startswith('Action(')]

    def test_a_two_digit_channel_is_not_read_as_a_speed_dial(self):
        """The bug, in one line: 1 then 4 has to reach 14."""
        ok, _message = self.tv.tune(14)

        self.assertTrue(ok)
        self.assertEqual(self.actions(),
                         ['number0', 'number1', 'number4', 'select'])

    def test_a_single_digit_channel_is_opened_the_same_way(self):
        """The half nobody noticed. Channel 3 on a box with a speed dial on
        3 went to the speed dial, silently and plausibly."""
        self.tv.tune(3)

        self.assertEqual(self.actions(), ['number0', 'number3', 'select'])

    def test_a_three_digit_channel_still_works(self):
        self.tv.tune(142)

        self.assertEqual(
            self.actions(),
            ['number0', 'number1', 'number4', 'number2', 'select'])

    def test_the_entry_is_always_opened_before_any_real_digit(self):
        """Whatever the channel, nothing but the zero may come first."""
        for channel in (1, 9, 10, 19, 20, 29, 30, 39, 40, 49, 99, 100, 999):
            xbmc.reset_rpc()
            self.tv.tune(channel)
            sent = self.actions()
            self.assertEqual(sent[0], 'number0',
                             'channel %d opened with %s' % (channel, sent[0]))
            self.assertEqual(sent[-1], 'select')
            self.assertEqual(sent[1:-1],
                             ['number%s' % d for d in str(channel)])

    def test_a_channel_that_does_not_exist_sends_nothing(self):
        for bad in (0, -1, 1000, 'seven', None):
            xbmc.reset_rpc()
            ok, _message = self.tv.tune(bad)
            self.assertFalse(ok, '%r should be refused' % (bad,))
            self.assertEqual(self.actions(), [])

    def test_it_refuses_when_the_television_is_not_on(self):
        xbmcgui.Window(self.tv.HOME_WINDOW).setProperty(
            self.tv.PROP_CHANNEL, '')

        ok, message = self.tv.tune(14)

        self.assertFalse(ok)
        self.assertEqual(self.actions(), [])
        self.assertIn('not running', message)

class TestDirectorySpeedDial(unittest.TestCase):
    """places.py: the slots, and the guard on what a slot may point at."""

    def setUp(self):
        import places
        self.places = places

    def test_a_slot_may_not_climb_out_of_its_base(self):
        """The whole reason a slot holds a rel and not a path.

        A slot that could hold ".." would let a recursive overwrite reach
        anywhere on the box, and everything above resources/ is credentials.
        """
        for bad in ('../tuya_keys.json', 'a/../../b', '..',
                    'skin.paragon/../../../storage'):
            self.assertIsNone(self.places.clean_rel(bad),
                              '%r should be refused' % (bad,))

    def test_a_slot_may_not_be_absolute_or_a_url(self):
        for bad in ('/storage/.kodi', 'C:/Kodi', 'c:\\Kodi',
                    'smb://box/share', 'special://home/'):
            self.assertIsNone(self.places.clean_rel(bad),
                              '%r should be refused' % (bad,))

    def test_a_rel_is_rebuilt_rather_than_patched(self):
        """Backslashes, doubled slashes and "." collapse to one clean form."""
        self.assertEqual(
            self.places.clean_rel('skin.paragon\\media'), 'skin.paragon/media')
        self.assertEqual(
            self.places.clean_rel('a//b/./c/'), 'a/b/c')
        self.assertEqual(self.places.clean_rel('  addons  '), 'addons')

    def test_empty_rel_means_the_base_itself(self):
        """Distinct from None, which means the slot is not set at all."""
        self.assertEqual(self.places.clean_rel(''), '')
        self.assertIsNone(self.places.clean_rel(None))
        self.assertEqual(self.places.join('smb://box/share/', ''),
                         'smb://box/share/')

    def test_join_keeps_a_url_a_url(self):
        """os.path.join would put a backslash in this on Windows."""
        self.assertEqual(
            self.places.join('smb://10.0.0.99/Addons', 'skin.paragon'),
            'smb://10.0.0.99/Addons/skin.paragon/')
        self.assertIsNone(
            self.places.join('smb://box/share/', '../secrets'))

    def test_slots_are_always_four_long_so_pairing_cannot_shift(self):
        """Slot N is index N-1 on every host, however the file was saved."""
        host = self.places.normalise_host(
            {'id': 'office', 'base': 'smb://box/share/',
             'slots': [{'rel': 'skin.paragon'}]})
        self.assertEqual(len(host['slots']), self.places.SLOT_COUNT)
        self.assertIsNone(host['slots'][3])

        crowded = self.places.normalise_host(
            {'id': 'office', 'base': 'smb://box/share/',
             'slots': [{'rel': 'a'}] * 9})
        self.assertEqual(len(crowded['slots']), self.places.SLOT_COUNT)

    def test_a_bad_slot_empties_its_place_rather_than_shifting_the_rest(self):
        host = self.places.normalise_host({
            'id': 'office', 'base': 'smb://box/share/',
            'slots': [{'rel': 'first'}, {'rel': '../escape'},
                      {'rel': 'third'}, None]})
        self.assertEqual(host['slots'][0]['rel'], 'first')
        self.assertIsNone(host['slots'][1])
        self.assertEqual(host['slots'][2]['rel'], 'third')

    def test_a_slot_names_itself_after_its_folder(self):
        host = self.places.normalise_host(
            {'id': 'x', 'base': '/a/', 'slots': [{'rel': 'addons/skin.paragon'}]})
        self.assertEqual(host['slots'][0]['name'], 'skin.paragon')

    def test_the_local_host_is_always_present_and_first(self):
        hosts = self.places.normalise_all(
            [{'id': 'office', 'base': 'smb://box/share/'}])
        self.assertEqual(hosts[0]['id'], self.places.LOCAL_ID)

        moved = self.places.normalise_all([
            {'id': 'office', 'base': 'smb://box/share/'},
            {'id': 'local', 'base': '/storage/'},
        ])
        self.assertEqual(moved[0]['id'], self.places.LOCAL_ID)
        self.assertEqual(moved[0]['base'], '/storage/')
        self.assertEqual(len(moved), 2)

    def test_duplicate_and_junk_hosts_are_dropped(self):
        hosts = self.places.normalise_all([
            {'id': 'office', 'base': 'smb://box/share/'},
            {'id': 'OFFICE', 'base': 'smb://other/share/'},
            {'id': 'nobase'},
            'not a dict',
        ])
        self.assertEqual([h['id'] for h in hosts], ['local', 'office'])
        self.assertEqual(hosts[1]['base'], 'smb://box/share/')

    def test_setting_and_reading_a_slot_round_trips(self):
        host = self.places.normalise_host({'id': 'x', 'base': 'smb://b/s/'})
        self.assertTrue(self.places.set_slot(host, 0, 'skin.paragon'))
        self.assertEqual(self.places.slot_path(host, 0),
                         'smb://b/s/skin.paragon/')

        self.assertTrue(self.places.set_slot(host, 0, None))
        self.assertIsNone(self.places.slot_path(host, 0))
        self.assertEqual(len(host['slots']), self.places.SLOT_COUNT)

        self.assertFalse(self.places.set_slot(host, 0, '../out'))
        self.assertFalse(self.places.set_slot(host, 9, 'x'))

    def test_pairs_skips_a_host_whose_slot_is_empty(self):
        """A destination that was never set is not guessed at."""
        hosts = self.places.normalise_all([
            {'id': 'local', 'base': '/a/', 'slots': [{'rel': 'skin.paragon'}]},
            {'id': 'office', 'base': 'smb://o/s/',
             'slots': [{'rel': 'skin.paragon'}]},
            {'id': 'bedroom', 'base': 'smb://b/s/', 'slots': []},
        ])
        found = self.places.pairs(hosts, 0, 'local')
        self.assertEqual([dest['id'] for _src, dest in found], ['office'])

        self.assertEqual(self.places.pairs(hosts, 3, 'local'), [])
        self.assertEqual(self.places.pairs(hosts, 0, 'nosuch'), [])


class TestFolderTransfer(unittest.TestCase):
    """transfer.py: the copy, the extras report, and the guarded delete."""

    def setUp(self):
        import transfer
        self.transfer = transfer
        self.root = tempfile.mkdtemp()
        self.source = os.path.join(self.root, 'source')
        self.dest = os.path.join(self.root, 'dest')
        self.build(self.source, {
            'addon.xml': 'skin',
            'media/icon.png': 'icon-bytes',
            'media/deep/flag.txt': 'deep',
            '720p/Home.xml': 'home',
        })

    def tearDown(self):
        import xbmcvfs
        xbmcvfs.SMB_ROOT = None
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, base, tree):
        for rel, body in tree.items():
            path = os.path.join(base, *rel.split('/'))
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            handle = open(path, 'w')
            handle.write(body)
            handle.close()

    def listing(self, base):
        """Every file under `base`, as forward-slashed relative paths."""
        found = set()
        for where, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(where, name)
                found.add(os.path.relpath(full, base).replace(os.sep, '/'))
        return found

    def read(self, base, rel):
        handle = open(os.path.join(base, *rel.split('/')))
        try:
            return handle.read()
        finally:
            handle.close()

    # -- surveying ---------------------------------------------------------

    def test_a_survey_finds_every_file_and_its_size(self):
        plan = self.transfer.survey(self.source)
        self.assertEqual(plan.rels(), {
            'addon.xml', 'media/icon.png', 'media/deep/flag.txt',
            '720p/Home.xml'})
        self.assertEqual(plan.count, 4)
        self.assertEqual(plan.total_bytes,
                         len('skin') + len('icon-bytes') + len('deep')
                         + len('home'))
        self.assertIn('media/deep', plan.dirs)

    def test_a_repository_is_never_copied(self):
        """The one folder a copy must not touch.

        These add-ons are installed as git clones and updated with git pull.
        Copying one box's .git onto another repoints that clone at the
        source's HEAD and refs, so the next pull there is against a history
        the box was never on -- the folder that keeps the destination
        updatable would be the thing the copy broke.
        """
        self.build(self.source, {
            '.git/HEAD': 'ref: refs/heads/main',
            '.git/objects/ab/cdef': 'blob',
            'media/.git/HEAD': 'a nested one counts too',
        })
        plan = self.transfer.survey(self.source)

        self.assertFalse([rel for rel in plan.rels() if '.git' in rel],
                         plan.rels())
        self.assertTrue(plan.skipped)

        self.transfer.copy_tree(self.source, self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.dest, '.git')))

    def test_compiled_python_is_not_copied(self):
        self.build(self.source, {'addon.pyo': 'x', 'lib/thing.pyc': 'x',
                                 '__pycache__/thing.pyc': 'x'})
        plan = self.transfer.survey(self.source)

        self.assertEqual([rel for rel in plan.rels()
                          if rel.endswith(('.pyo', '.pyc'))], [])
        self.assertNotIn('__pycache__', plan.dirs)

    def test_a_skipped_folder_is_not_offered_for_deletion(self):
        """Excluded from both surveys, so it cannot show up as an extra.

        Otherwise a sync would finish by offering to delete the destination's
        own .git -- which is worse than having copied one.
        """
        self.transfer.copy_tree(self.source, self.dest)
        self.build(self.dest, {'.git/HEAD': 'the destination has its own'})

        self.assertEqual(self.transfer.extras(self.source, self.dest), [])

    def test_the_confirmation_says_when_something_was_skipped(self):
        self.build(self.source, {'.git/HEAD': 'x'})
        plan = self.transfer.survey(self.source)
        self.assertIn('skipped', self.transfer.describe(plan))

        clean = self.transfer.survey(os.path.join(self.source, 'media'))
        self.assertNotIn('skipped', self.transfer.describe(clean))

    def test_a_survey_of_a_missing_folder_says_so(self):
        self.assertRaises(self.transfer.TransferError,
                          self.transfer.survey,
                          os.path.join(self.root, 'nowhere'))

    def test_a_tree_deeper_than_the_cap_stops_instead_of_looping(self):
        """xbmcvfs cannot report a symlink, so depth is the only defence."""
        deep = os.path.join(self.root, 'deep')
        os.makedirs(os.path.join(deep, *['x'] * (self.transfer.MAX_DEPTH + 3)))
        plan = self.transfer.survey(deep)
        self.assertTrue(any('nested deeper' in e for e in plan.errors))

    # -- copying -----------------------------------------------------------

    def test_a_copy_reproduces_the_whole_tree(self):
        result = self.transfer.copy_tree(self.source, self.dest)
        self.assertTrue(result.ok, result.failed)
        self.assertEqual(result.done, 4)
        self.assertEqual(self.listing(self.dest), self.listing(self.source))
        self.assertEqual(self.read(self.dest, 'media/deep/flag.txt'), 'deep')

    def test_a_copy_overwrites_what_is_already_there(self):
        self.build(self.dest, {'addon.xml': 'STALE'})
        self.transfer.copy_tree(self.source, self.dest)
        self.assertEqual(self.read(self.dest, 'addon.xml'), 'skin')

    def test_a_copy_never_deletes_what_it_did_not_bring(self):
        """The safety property the whole design turns on.

        This is the shape of the operation that destroyed a folder of weather
        icons: a stale tree copied over a good one. Overwriting is asked for;
        deleting is not, and must never happen as a side effect of a sync.
        """
        self.build(self.dest, {
            'mine.txt': 'keep me',
            'media/also-mine.png': 'keep me too',
        })
        self.transfer.copy_tree(self.source, self.dest)

        self.assertEqual(self.read(self.dest, 'mine.txt'), 'keep me')
        self.assertEqual(self.read(self.dest, 'media/also-mine.png'),
                         'keep me too')

    def test_a_copy_can_be_cancelled_part_way(self):
        seen = []

        def cancel():
            seen.append(1)
            return len(seen) > 3

        result = self.transfer.copy_tree(self.source, self.dest,
                                         should_cancel=cancel)
        self.assertEqual(result.status, self.transfer.CANCELLED)
        self.assertLess(len(self.listing(self.dest)), 4)

    def test_progress_counts_towards_the_surveyed_total(self):
        seen = []
        self.transfer.copy_tree(
            self.source, self.dest,
            on_progress=lambda done, total, rel: seen.append((done, total)))
        self.assertEqual(seen[-1], (4, 4))
        self.assertEqual([done for done, _total in seen], [1, 2, 3, 4])

    def test_a_copy_creates_a_destination_that_is_not_there_yet(self):
        self.assertFalse(os.path.isdir(self.dest))
        self.transfer.copy_tree(self.source, self.dest)
        self.assertTrue(os.path.isdir(self.dest))

    def test_a_copy_onto_a_share_goes_through_the_vfs(self):
        """The destination is usually smb://, which os.path cannot see."""
        import xbmcvfs
        share = os.path.join(self.root, 'share')
        os.makedirs(share)
        xbmcvfs.SMB_ROOT = share

        result = self.transfer.copy_tree(self.source,
                                         'smb://10.0.0.99/Addons/')
        self.assertTrue(result.ok, result.failed)
        self.assertEqual(self.listing(share), self.listing(self.source))

    # -- the guards --------------------------------------------------------

    def test_a_copy_into_its_own_source_is_refused(self):
        """It would grow the source as it walked it and never finish."""
        inside = os.path.join(self.source, 'media')
        self.assertRaises(self.transfer.TransferError,
                          self.transfer.copy_tree, self.source, inside)
        self.assertRaises(self.transfer.TransferError,
                          self.transfer.copy_tree, inside, self.source)

    def test_a_neighbour_with_a_shared_prefix_is_not_an_overlap(self):
        self.assertFalse(self.transfer.overlaps(
            '/a/skin.paragon/', '/a/skin.paragon2/'))
        self.assertTrue(self.transfer.overlaps(
            '/a/skin.paragon/', '/a/skin.paragon/media/'))

    def test_a_copy_from_a_folder_that_is_not_there_is_refused(self):
        self.assertRaises(self.transfer.TransferError,
                          self.transfer.copy_tree,
                          os.path.join(self.root, 'nowhere'), self.dest)

    # -- extras, and the separate delete ------------------------------------

    def test_extras_reports_only_what_the_source_lacks(self):
        self.transfer.copy_tree(self.source, self.dest)
        self.build(self.dest, {
            'leftover.txt': 'old',
            'media/gone.png': 'old',
        })

        found = self.transfer.extras(self.source, self.dest)
        self.assertEqual(found, ['leftover.txt', 'media/gone.png'])

    def test_extras_is_empty_after_a_clean_copy(self):
        self.transfer.copy_tree(self.source, self.dest)
        self.assertEqual(self.transfer.extras(self.source, self.dest), [])

    def test_extras_reads_and_removes_nothing(self):
        self.transfer.copy_tree(self.source, self.dest)
        self.build(self.dest, {'leftover.txt': 'old'})
        before = self.listing(self.dest)

        self.transfer.extras(self.source, self.dest)
        self.assertEqual(self.listing(self.dest), before)

    def test_removing_takes_only_what_it_was_handed(self):
        self.transfer.copy_tree(self.source, self.dest)
        self.build(self.dest, {'leftover.txt': 'old', 'other.txt': 'old'})

        result = self.transfer.remove(self.dest, ['leftover.txt'])
        self.assertTrue(result.ok, result.failed)
        self.assertEqual(result.done, 1)
        self.assertNotIn('leftover.txt', self.listing(self.dest))
        self.assertIn('other.txt', self.listing(self.dest))
        self.assertIn('addon.xml', self.listing(self.dest))

    def test_removing_refuses_a_path_that_climbs_out(self):
        """remove() deletes, so it re-checks rather than trusting its caller.

        The danger a rebuilt path leaves behind is not escape -- join() cannot
        escape, because it assembles from a base rather than resolving what it
        is handed. It is that an unrefused ".." *flattens*: "../keep.txt"
        becomes "keep.txt", which is a real file here and not the one named.
        So the test puts that file in the destination, where a flattened path
        would hit it, and a refused one leaves it alone.
        """
        self.build(self.root, {'keep.txt': 'outside the destination'})
        self.transfer.copy_tree(self.source, self.dest)
        self.build(self.dest, {'keep.txt': 'inside, and not in the list'})

        result = self.transfer.remove(
            self.dest, ['../keep.txt', '/etc/passwd', 'C:/Windows'])

        self.assertEqual(result.done, 0)
        self.assertEqual(len(result.failed), 3)
        self.assertIn('keep.txt', self.listing(self.dest))
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'keep.txt')))

    def test_removing_can_be_cancelled(self):
        self.transfer.copy_tree(self.source, self.dest)
        self.build(self.dest, {'a.txt': 'x', 'b.txt': 'x'})

        result = self.transfer.remove(self.dest, ['a.txt', 'b.txt'],
                                      should_cancel=lambda: True)
        self.assertEqual(result.status, self.transfer.CANCELLED)
        self.assertIn('a.txt', self.listing(self.dest))

    # -- describing --------------------------------------------------------

    def test_a_plan_describes_itself_for_the_confirmation(self):
        plan = self.transfer.survey(self.source)
        self.assertEqual(self.transfer.describe(plan), '4 files, 22 B')

    def test_sizes_read_as_words_not_bytes(self):
        self.assertEqual(self.transfer.describe_size(0), '0 B')
        self.assertEqual(self.transfer.describe_size(512), '512 B')
        self.assertEqual(self.transfer.describe_size(2048), '2.0 KB')
        self.assertEqual(self.transfer.describe_size(5 * 1024 * 1024),
                         '5.0 MB')
        self.assertEqual(self.transfer.describe_size('nonsense'), '0 B')


class TestMovingFiles(unittest.TestCase):
    """The menus over places.py and transfer.py."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'gui', 'places',
                     'transfer'):
            if name in sys.modules:
                del sys.modules[name]

        self.root = tempfile.mkdtemp()
        self.here = os.path.join(self.root, 'here')
        self.there = os.path.join(self.root, 'there')
        for base in (self.here, self.there):
            os.makedirs(os.path.join(base, 'skin.paragon'))
        self.write(self.here, 'skin.paragon/addon.xml', 'new')
        self.write(self.here, 'skin.paragon/media/icon.png', 'new-icon')

        from paragon_home import ParagonHome
        import places as places_lib

        self.app = ParagonHome()
        self.app._places = places_lib.normalise_all([
            {'id': 'local', 'name': 'This box', 'base': self.here,
             'slots': [{'rel': 'skin.paragon'}]},
            {'id': 'office', 'name': 'Office', 'base': self.there,
             'slots': [{'rel': 'skin.paragon'}]},
        ])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        clean_profile()

    def write(self, base, rel, body):
        path = os.path.join(base, *rel.split('/'))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        handle = open(path, 'w')
        handle.write(body)
        handle.close()

    def read(self, base, rel):
        handle = open(os.path.join(base, *rel.split('/')))
        try:
            return handle.read()
        finally:
            handle.close()

    def panel(self):
        import gui
        return gui.ControlPanel(self.app)

    def office(self):
        return self.app.host_by_id('office')

    # -- the menu ----------------------------------------------------------

    def test_moving_files_is_on_the_main_menu(self):
        _row, labels = menu_row(self.panel().main_menu, 'Move files')
        self.assertIn('Move files...', labels)

    def test_a_slot_says_what_it_holds_and_where_it_goes(self):
        _row, labels = menu_row(self.panel().transfer_menu, '1 ')
        self.assertEqual(labels[0], '1  skin.paragon  to Office')
        self.assertEqual(labels[1], '2  (not set)')

    def test_a_slot_with_nowhere_to_send_says_so(self):
        import places as places_lib

        places_lib.set_slot(self.office(), 0, None)
        _row, labels = menu_row(self.panel().transfer_menu, '1 ')
        self.assertEqual(labels[0], '1  skin.paragon  (nowhere to send it)')

    def test_several_destinations_are_counted(self):
        self.app.save_host({'id': 'bed', 'name': 'Bedroom',
                            'base': self.there, 'slots': [{'rel': 'x'}]})
        _row, labels = menu_row(self.panel().transfer_menu, '1 ')
        self.assertEqual(labels[0], '1  skin.paragon  to 2 boxes')

    # -- the confirmation --------------------------------------------------

    def test_the_confirmation_names_the_direction_in_words(self):
        """An arrow is the easiest thing to read backwards.

        Reading this one backwards overwrites the wrong box, so FROM and TO
        are spelled out and the destination is named twice.
        """
        xbmcgui.YESNO_QUEUE.append(False)
        self.panel().sync_slot(0, self.office())

        _heading, line1, line2, line3 = xbmcgui.YESNO_CALLS[0]
        self.assertEqual(line1, 'FROM  this box')
        self.assertEqual(line2, 'TO  Office')
        self.assertIn('2 files', line3)
        self.assertIn('overwritten', line3)
        self.assertIn('Office', line3)

    def test_declining_the_confirmation_copies_nothing(self):
        xbmcgui.YESNO_QUEUE.append(False)

        self.assertFalse(self.panel().sync_slot(0, self.office()))
        self.assertFalse(os.path.exists(
            os.path.join(self.there, 'skin.paragon', 'addon.xml')))

    # -- the sync ----------------------------------------------------------

    def test_a_confirmed_sync_copies_the_folder(self):
        xbmcgui.YESNO_QUEUE.append(True)

        self.assertTrue(self.panel().sync_slot(0, self.office()))
        self.assertEqual(self.read(self.there, 'skin.paragon/addon.xml'),
                         'new')
        self.assertEqual(self.read(self.there, 'skin.paragon/media/icon.png'),
                         'new-icon')

    def test_a_sync_overwrites_a_stale_copy_but_keeps_the_rest(self):
        self.write(self.there, 'skin.paragon/addon.xml', 'STALE')
        self.write(self.there, 'skin.paragon/extra.xml', 'only there')
        xbmcgui.YESNO_QUEUE.extend([True, False])

        self.panel().sync_slot(0, self.office())

        self.assertEqual(self.read(self.there, 'skin.paragon/addon.xml'),
                         'new')
        self.assertEqual(self.read(self.there, 'skin.paragon/extra.xml'),
                         'only there')

    def test_an_empty_slot_on_either_end_is_refused(self):
        import places as places_lib

        places_lib.set_slot(self.office(), 0, None)
        self.assertFalse(self.panel().sync_slot(0, self.office()))
        self.assertEqual(xbmcgui.YESNO_CALLS, [])

    # -- extras ------------------------------------------------------------

    def test_extras_are_offered_after_a_sync_and_not_deleted_by_it(self):
        self.write(self.there, 'skin.paragon/gone.xml', 'stale')
        # Yes to the copy, then no to the deletion.
        xbmcgui.YESNO_QUEUE.extend([True, False])

        self.panel().sync_slot(0, self.office())

        offer = xbmcgui.YESNO_CALLS[1]
        self.assertIn('1 file(s) only on Office', offer[0])
        self.assertIn('gone.xml', offer[2])
        self.assertEqual(self.read(self.there, 'skin.paragon/gone.xml'),
                         'stale')

    def test_extras_are_deleted_only_on_a_second_yes(self):
        self.write(self.there, 'skin.paragon/gone.xml', 'stale')
        xbmcgui.YESNO_QUEUE.extend([True, True])

        self.panel().sync_slot(0, self.office())

        self.assertFalse(os.path.exists(
            os.path.join(self.there, 'skin.paragon', 'gone.xml')))
        self.assertEqual(self.read(self.there, 'skin.paragon/addon.xml'),
                         'new')

    def test_nothing_is_offered_when_the_two_ends_match(self):
        xbmcgui.YESNO_QUEUE.append(True)
        self.panel().sync_slot(0, self.office())
        self.assertEqual(len(xbmcgui.YESNO_CALLS), 1)

    # -- everywhere --------------------------------------------------------

    def test_sending_everywhere_asks_once_and_names_every_box(self):
        third = os.path.join(self.root, 'third')
        os.makedirs(os.path.join(third, 'skin.paragon'))
        self.app.save_host({'id': 'bed', 'name': 'Bedroom', 'base': third,
                            'slots': [{'rel': 'skin.paragon'}]})
        xbmcgui.YESNO_QUEUE.append(False)

        self.panel().sync_everywhere(0)

        self.assertEqual(len(xbmcgui.YESNO_CALLS), 1)
        self.assertIn('Office', xbmcgui.YESNO_CALLS[0][2])
        self.assertIn('Bedroom', xbmcgui.YESNO_CALLS[0][2])

    def test_sending_everywhere_copies_to_every_box(self):
        third = os.path.join(self.root, 'third')
        os.makedirs(os.path.join(third, 'skin.paragon'))
        self.app.save_host({'id': 'bed', 'name': 'Bedroom', 'base': third,
                            'slots': [{'rel': 'skin.paragon'}]})
        xbmcgui.YESNO_QUEUE.append(True)

        self.panel().sync_everywhere(0)

        self.assertEqual(self.read(self.there, 'skin.paragon/addon.xml'),
                         'new')
        self.assertEqual(self.read(third, 'skin.paragon/addon.xml'), 'new')

    # -- configuring -------------------------------------------------------

    def slot_rows(self, host_id, index):
        """The labels the slot picker offers, without choosing one."""
        xbmcgui.SELECT_QUEUE.append(-1)
        self.panel().edit_slot(host_id, index)
        labels = xbmcgui.SELECT_CALLS[-1][1]
        xbmcgui.reset()
        return labels

    def pick_slot_row(self, host_id, index, label):
        """Choose the picker row called `label`."""
        labels = self.slot_rows(host_id, index)
        self.assertIn(label, labels)
        xbmcgui.SELECT_QUEUE.append(labels.index(label))
        return labels

    def test_the_slot_picker_offers_what_is_actually_under_the_base(self):
        """Typing a path with a remote is what the speed dial exists to avoid."""
        os.makedirs(os.path.join(self.here, 'script.paragontv'))
        os.makedirs(os.path.join(self.here, 'script.paragon.home'))
        self.write(self.here, 'loose.txt', 'not a folder')

        labels = self.slot_rows('local', 1)

        self.assertEqual(labels, ['script.paragon.home', 'script.paragontv',
                                  'skin.paragon', 'Type it instead...'])
        self.assertNotIn('loose.txt', labels)

    def test_picking_a_folder_sets_the_slot(self):
        import places as places_lib

        os.makedirs(os.path.join(self.here, 'script.paragontv'))
        self.pick_slot_row('local', 1, 'script.paragontv')

        self.panel().edit_slot('local', 1)

        entry = places_lib.slot(self.app.local_host(), 1)
        self.assertEqual(entry['rel'], 'script.paragontv')
        self.assertEqual(entry['name'], 'script.paragontv')

    def test_clearing_is_offered_only_for_a_slot_that_is_set(self):
        self.assertIn('Clear this slot', self.slot_rows('local', 0))
        self.assertNotIn('Clear this slot', self.slot_rows('local', 1))

    def test_a_box_that_cannot_be_reached_can_still_be_typed_into(self):
        self.app.save_host({'id': 'off', 'name': 'Off',
                            'base': 'smb://10.9.9.9/nothing/', 'slots': []})
        self.assertEqual(self.slot_rows('off', 0), ['Type it instead...'])

    def test_a_typed_slot_cannot_be_pointed_out_of_its_base(self):
        import places as places_lib

        self.pick_slot_row('local', 1, 'Type it instead...')
        xbmcgui.INPUT_QUEUE.append('../../tuya_keys.json')

        self.panel().edit_slot('local', 1)

        self.assertIsNone(places_lib.slot(self.app.local_host(), 1))
        self.assertTrue(any('not a folder inside' in message
                            for _heading, message in xbmcgui.NOTIFICATIONS))

    def test_a_typed_slot_may_be_nested(self):
        """The one thing the list cannot offer: it is only one level deep."""
        import places as places_lib

        self.pick_slot_row('local', 1, 'Type it instead...')
        xbmcgui.INPUT_QUEUE.append('skin.paragon/media')

        self.panel().edit_slot('local', 1)

        entry = places_lib.slot(self.app.local_host(), 1)
        self.assertEqual(entry['rel'], 'skin.paragon/media')
        self.assertEqual(entry['name'], 'media')

    def test_backing_out_of_the_keyboard_keeps_the_slot(self):
        """Cancelling must not read as "delete it" -- clearing has its own row."""
        import places as places_lib

        self.pick_slot_row('local', 0, 'Type it instead...')
        xbmcgui.INPUT_QUEUE.append('')

        self.panel().edit_slot('local', 0)

        self.assertEqual(
            places_lib.slot(self.app.local_host(), 0)['rel'], 'skin.paragon')

    def test_clearing_a_slot_leaves_the_others_numbered_as_they_were(self):
        import places as places_lib

        local = self.app.local_host()
        places_lib.set_slot(local, 2, 'script.paragontv')
        self.pick_slot_row('local', 0, 'Clear this slot')

        self.panel().edit_slot('local', 0)

        local = self.app.local_host()
        self.assertIsNone(places_lib.slot(local, 0))
        self.assertEqual(places_lib.slot(local, 2)['name'],
                         'script.paragontv')

    def test_a_new_box_can_take_this_box_s_slots(self):
        """The step that otherwise stops anyone setting up a third box."""
        import places as places_lib

        xbmcgui.INPUT_QUEUE.extend(['Bedroom', self.there])
        xbmcgui.YESNO_QUEUE.append(True)

        self.panel().add_host()

        added = self.app.host_by_id('bedroom')
        self.assertIsNotNone(added)
        self.assertEqual(places_lib.slot(added, 0)['rel'], 'skin.paragon')

    def test_a_new_box_can_decline_them(self):
        import places as places_lib

        xbmcgui.INPUT_QUEUE.extend(['Bedroom', self.there])
        xbmcgui.YESNO_QUEUE.append(False)

        self.panel().add_host()

        self.assertIsNone(places_lib.slot(self.app.host_by_id('bedroom'), 0))

    def test_the_local_box_cannot_be_forgotten(self):
        self.assertFalse(self.app.remove_host('local'))
        self.assertIsNotNone(self.app.local_host())
        self.assertTrue(self.app.remove_host('office'))

    # -- where the slots live ----------------------------------------------

    def test_slots_are_not_shared_with_satellites(self):
        """The box you are standing at is the one whose slots you want to set.

        Everything in SHARED_FILES is overwritten from the master and refused
        on a satellite. That is right for scenes and wrong for these: a slot
        says where files live on *this* box, and the box you would configure
        is usually a satellite, because the master is the one in the cabinet.
        """
        import places as places_lib
        import satellite as satellite_lib

        self.assertNotIn(places_lib.PLACES_FILE, satellite_lib.SHARED_FILES)

    def test_slots_can_be_saved_on_a_satellite(self):
        import places as places_lib

        xbmcaddon.SETTINGS['satellite_mode'] = 'true'
        # Asserted, not assumed: without this the test would pass on a box
        # that was never a satellite and prove nothing.
        self.assertFalse(self.app.owns_data)
        self.assertFalse(self.app.save_palette(),
                         'a satellite should refuse a shared list')

        local = self.app.local_host()
        places_lib.set_slot(local, 3, 'script.paragon.home')
        self.assertTrue(self.app.save_host(local))
        self.assertEqual(
            places_lib.slot(self.app.local_host(), 3)['name'],
            'script.paragon.home')


if __name__ == '__main__':
    unittest.main(verbosity=2)
