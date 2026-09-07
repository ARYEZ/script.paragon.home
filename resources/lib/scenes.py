# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Named lighting presets.

A scene is a plain dict so it round-trips through JSON without a schema layer.
It describes what to do to a set of devices, not what state they must end in,
which is what lets a single scene ("Movie Night") drive a mixed set of lights
whose capabilities differ.

Fields:
    name        display name, and the key used by RunScript() and settings
    power       'on', 'off' or 'keep' (leave the current power state alone)
    brightness  1-100, or None to leave brightness untouched
    mode        'color', 'temp' or 'none'
    color       [r, g, b], used when mode == 'color'
    kelvin      colour temperature, used when mode == 'temp'
    targets     list of device ids; empty means every enabled device
    bar_brightness
                1-100, or None. When set, lightbars use this instead of
                `brightness`. A lightbar throws far more light than a bulb
                at the same percentage, so one figure rarely suits both.
    backlight_brightness
                1-100, or None. The same idea for a backlight, which has the
                opposite problem: a strip washing a wall behind a screen is
                either glare or nothing at the level that suits the bulbs
                lighting the room.
    strip_brightness
                1-100, or None. The third of the same: a strip run along a
                counter or a shelf is a line of light rather than a source
                filling a room, and lands somewhere between the two.
"""

import random

from devices import (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP,
                     CAP_POWER)

# What makes a device a light: something a scene can describe the look of.
# A plug has power and state only, and a blaster has neither, so neither is
# ever a scene target -- see is_a_light.
LIGHT_CAPABILITIES = (CAP_COLOR, CAP_COLOR_TEMP, CAP_BRIGHTNESS)

POWER_ON = 'on'
POWER_OFF = 'off'
POWER_KEEP = 'keep'

MODE_COLOR = 'color'
MODE_TEMP = 'temp'
MODE_MIX = 'mix'
MODE_NONE = 'none'

SCENE_FILE = 'scenes.json'

# Govee SKUs that are lightbars rather than bulbs. Matched case-insensitively
# against Device.model. A device whose name contains "lightbar" also counts,
# which covers a model not listed here without needing a code change.
LIGHTBAR_MODELS = ('H610A',)

# A backlight is matched on its name alone. There is no SKU list to go with
# it because nothing so far has needed one -- what makes a light a backlight
# is where it is pointed, not what it is, and a bulb behind a screen is a
# backlight while the identical bulb in a lamp is not. The name is the only
# place that fact is recorded.
BACKLIGHT_WORD = 'BACKLIGHT'

# And a light strip, on the same reasoning. "Strip" rather than "lightstrip"
# so that "Kitchen Light Strip" and "LED Strip" both match without anyone
# having to name a device a particular way. Neither BACKLIGHT nor LIGHTBAR
# contains it, so the three words never collide on a name by accident -- and
# where a name does carry more than one, apply_scene settles the order.
STRIP_WORD = 'STRIP'

# How far apart the RGB channels must be before a reading counts as a real
# colour rather than a shade of white. Govee bulbs report white either as
# 0,0,0 or as an equal-channel value alongside a colour temperature.
GREY_TOLERANCE = 12


def parse_hex_color(text):
    """Parse a hex colour string. Returns ((r, g, b), note) or (None, reason).

    Accepts 3, 6 and 8 digit forms. The Govee app shows 8-digit codes, which
    carry an alpha byte the LAN protocol has no use for.

    Govee emits AARRGGBB with the alpha always FF. That is confirmed from real
    codes out of the app -- FF3C447F and FF7F3C3C -- where the alternative
    RRGGBBAA reading would mean alphas of 7F and 3C, i.e. bulbs at 50% and 23%
    transparency, which is meaningless for a light. Read as AARRGGBB they are
    a deep slate blue and a deep brick red, which is what the app showed.

    Other sources are not consistent, though: Android writes AARRGGBB and CSS
    Color 4 writes RRGGBBAA, so the end is inferred rather than hardcoded. An
    alpha in a colour picker is almost always FF, so whichever end reads FF is
    taken as the alpha; when both ends are FF or neither is, the two readings
    are indistinguishable and alpha-first wins, which is also the right answer
    for Govee. `note` always says which reading was used, so a wrong inference
    shows up in the dialog rather than only on the wall.
    """
    if not text:
        return None, 'No colour entered'

    cleaned = text.strip().lstrip('#').replace(' ', '')
    for char in cleaned:
        if char not in '0123456789abcdefABCDEF':
            return None, 'That is not a hex colour'

    if len(cleaned) == 3:
        cleaned = ''.join(char * 2 for char in cleaned)
        note = ''
    elif len(cleaned) == 6:
        note = ''
    elif len(cleaned) == 8:
        head, tail = cleaned[0:2].upper(), cleaned[6:8].upper()
        if head == 'FF' and tail != 'FF':
            cleaned, note = cleaned[2:], 'read as AARRGGBB'
        elif tail == 'FF' and head != 'FF':
            cleaned, note = cleaned[:6], 'read as RRGGBBAA'
        else:
            cleaned, note = cleaned[2:], 'read as AARRGGBB (ambiguous)'
    else:
        return None, 'Enter 6 or 8 hex digits, e.g. FF8800 or FFFF8800'

    try:
        rgb = (int(cleaned[0:2], 16), int(cleaned[2:4], 16),
               int(cleaned[4:6], 16))
    except ValueError:
        return None, 'That is not a hex colour'
    return rgb, note


def _normalise_mix_entry(entry):
    """One colour of a mix: {'name':..., 'color':[r,g,b]} or a bare [r,g,b]."""
    if isinstance(entry, dict):
        name = entry.get('name') or ''
        rgb = entry.get('color')
    else:
        name, rgb = '', entry

    try:
        rgb = [max(0, min(255, int(c))) for c in rgb][:3]
    except (TypeError, ValueError):
        return None
    if len(rgb) != 3:
        return None

    name = (name or '').strip() or '#%02X%02X%02X' % (rgb[0], rgb[1], rgb[2])
    return {'name': name, 'color': rgb}


def deal_colors(colors, count, shuffle_func=None):
    """Spread `colors` over `count` lights: evenly, in a random arrangement.

    Even means each colour is used floor(count/n) or ceil(count/n) times --
    with 25 lights and 3 colours that is 9/8/8, not whatever chance happens
    to produce. Random means which light gets which is shuffled, so the same
    scene applied twice arranges differently.

    The colour order is shuffled before the pool is built as well as after.
    Building the pool by repeating and truncating always hands the spare
    light to whichever colour comes first, so without that the same colour
    would win the extra every single time.
    """
    if not colors or count <= 0:
        return []

    shuffle = shuffle_func or random.shuffle

    order = list(colors)
    shuffle(order)

    repeats = (count // len(order)) + 1
    pool = (order * repeats)[:count]
    shuffle(pool)
    return pool


def _normalise_action(entry):
    """One command a scene fires: {'device': id, 'command': name}.

    Actions are how a scene reaches a device that has no state to set -- an IR
    blaster has no colour, it has "AVR Power". A scene holds both, so one
    "Movie Night" can dim the lights and switch the amp on.
    """
    if not isinstance(entry, dict):
        return None
    device_id = entry.get('device')
    command = entry.get('command')
    if not device_id or not command or not hasattr(command, 'strip'):
        return None
    command = command.strip()
    if not command:
        return None
    return {'device': str(device_id).upper(), 'command': command}


def deal_assignment(color_count, device_ids, shuffle_func=None):
    """Map each device id to a colour index: evenly spread, randomly arranged.

    Kept as indices rather than colours because a cycling scene has to hand
    this between two Kodi processes -- the control panel that starts it and
    the service that steps it -- and small integers survive that trip better
    than colour triples.
    """
    if not color_count or not device_ids:
        return {}
    indices = deal_colors(list(range(color_count)), len(device_ids),
                          shuffle_func)
    return dict(zip(device_ids, indices))


def rotate_assignment(assignment, color_count):
    """Advance every device to the next colour in the mix.

    Rotation is what makes cycling keep its even spread for free: shifting
    every light by one turns a 9/8/8 split into 8/8/9, never into 12/7/6. It
    also guarantees each light actually changes colour, which re-dealing at
    random would not.
    """
    if not assignment or not color_count:
        return dict(assignment or {})
    return dict((device_id, (index + 1) % color_count)
                for device_id, index in assignment.items())


def make_scene(name, power=POWER_ON, brightness=None, mode=MODE_NONE,
               color=None, kelvin=None, targets=None, devices=None,
               colors=None, cycle=0, actions=None, bar_brightness=None,
               backlight_brightness=None, strip_brightness=None):
    return {
        'name': name,
        'power': power,
        'brightness': brightness,
        'bar_brightness': bar_brightness,
        'backlight_brightness': backlight_brightness,
        'strip_brightness': strip_brightness,
        'mode': mode,
        'color': list(color) if color else [255, 255, 255],
        'kelvin': kelvin or 2700,
        'targets': list(targets or []),
        'devices': dict(devices or {}),
        'colors': list(colors or []),
        'cycle': int(cycle or 0),
        'actions': list(actions or []),
    }


def _normalise_settings(raw, fallback_power=POWER_ON):
    """Clamp one set of light settings. Shared by scenes and per-device entries."""
    if not isinstance(raw, dict):
        return None

    power = raw.get('power', fallback_power)
    if power not in (POWER_ON, POWER_OFF, POWER_KEEP):
        power = fallback_power

    brightness = raw.get('brightness')
    if brightness is not None:
        try:
            brightness = max(1, min(100, int(brightness)))
        except (TypeError, ValueError):
            brightness = None

    mode = raw.get('mode', MODE_NONE)
    if mode not in (MODE_COLOR, MODE_TEMP, MODE_MIX, MODE_NONE):
        mode = MODE_NONE

    color = raw.get('color') or [255, 255, 255]
    try:
        color = [max(0, min(255, int(c))) for c in color][:3]
    except (TypeError, ValueError):
        color = [255, 255, 255]
    while len(color) < 3:
        color.append(255)

    try:
        kelvin = int(raw.get('kelvin') or 2700)
    except (TypeError, ValueError):
        kelvin = 2700
    kelvin = max(1500, min(12000, kelvin))

    return {'power': power, 'brightness': brightness, 'mode': mode,
            'color': color, 'kelvin': kelvin}


def _normalise_level(value):
    """A brightness percentage, or None when it is absent or unreadable.

    Shared by the lightbar and backlight figures, which are clamped the same
    way and were about to be clamped twice.
    """
    if value is None:
        return None
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def is_lightbar(device):
    """Whether `device` should take a scene's separate lightbar brightness."""
    model = (getattr(device, 'model', '') or '').strip().upper()
    if model in LIGHTBAR_MODELS:
        return True
    return 'LIGHTBAR' in (getattr(device, 'name', '') or '').upper()


def is_backlight(device):
    """Whether `device` should take a scene's separate backlight brightness.

    By name only, and deliberately: see BACKLIGHT_WORD. Note that "Lightbar"
    does not contain "Backlight", so the two never collide by accident on a
    name -- but a backlight built from a lightbar would match both, and
    apply_scene settles that in the backlight's favour.
    """
    return BACKLIGHT_WORD in (getattr(device, 'name', '') or '').upper()


def is_lightstrip(device):
    """Whether `device` should take a scene's separate light strip brightness.

    By name, like a backlight, and for the same reason: what makes a light a
    strip is its shape, and the name is the only place that is recorded.

    Deliberately the last of the three to be asked. A backlight is usually a
    strip and a strip is sometimes a bar, so a name carrying two of these
    words has to resolve the same way every time -- see apply_scene.
    """
    return STRIP_WORD in (getattr(device, 'name', '') or '').upper()


def settings_for(scene, device_id):
    """The settings a scene applies to one device.

    A captured scene stores a per-device entry; everything else falls back to
    the scene's own uniform values. This is what lets one scene hold 25
    different bulb states without every other scene growing a device map.
    """
    per_device = (scene.get('devices') or {}).get((device_id or '').upper())
    if per_device:
        return per_device
    return {
        'power': scene.get('power', POWER_ON),
        'brightness': scene.get('brightness'),
        'mode': scene.get('mode', MODE_NONE),
        'color': scene.get('color') or [255, 255, 255],
        'kelvin': scene.get('kelvin', 2700),
    }


def detect_brightness_scale(states):
    """Work out which brightness scale a set of readings uses.

    Govee documents brightness as 0-100, but some models report the raw 0-254
    register. A single reading of 51 is ambiguous -- it could be 51% or 20% --
    so the scale cannot be inferred per bulb. Across a whole capture it can:
    one reading above 100 proves the wider scale, and every bulb in a capture
    is answering the same way.

    Returns 100 or 254. When every bulb happens to read at or below 100 on a
    254-scale model the two are indistinguishable and this returns 100; the
    captured values are then self-consistently low rather than wrong in a way
    that changes which bulb is brighter than which.
    """
    for state in (states or {}).values():
        if not state:
            continue
        value = state.get('brightness')
        try:
            if value is not None and int(value) > 100:
                return 254
        except (TypeError, ValueError):
            continue
    return 100


def state_to_settings(state, brightness_scale=100):
    """Turn a device state reading into scene settings, or None if unusable.

    Which appearance mode a bulb is in has to be inferred: Govee reports a
    colour temperature of 0 when the bulb is showing RGB, and reports RGB of
    0,0,0 when it is showing white, so whichever one is non-zero is the live
    one.

    `brightness_scale` comes from detect_brightness_scale() over the whole
    capture; see there for why it cannot be decided from one reading.
    """
    if not state:
        return None

    power = state.get('power')
    if power not in ('on', 'off'):
        return None
    if power == 'off':
        return {'power': POWER_OFF, 'brightness': None, 'mode': MODE_NONE,
                'color': [255, 255, 255], 'kelvin': 2700}

    settings = {'power': POWER_ON, 'brightness': None, 'mode': MODE_NONE,
                'color': [255, 255, 255], 'kelvin': 2700}

    brightness = state.get('brightness')
    if brightness is not None:
        try:
            brightness = int(brightness)
        except (TypeError, ValueError):
            brightness = None
    if brightness is not None:
        if brightness_scale and brightness_scale != 100:
            brightness = int(round(brightness * 100.0 / brightness_scale))
        settings['brightness'] = max(1, min(100, brightness))

    try:
        kelvin = int(state.get('colorTem') or 0)
    except (TypeError, ValueError):
        kelvin = 0

    rgb = None
    color = state.get('color')
    if isinstance(color, dict):
        try:
            rgb = [max(0, min(255, int(color.get(k) or 0)))
                   for k in ('r', 'g', 'b')]
        except (TypeError, ValueError):
            rgb = None

    # Which mode a bulb is in has to be inferred, and the two fields can
    # disagree. A bulb showing white reports either 0,0,0 or the white
    # equivalent next to its temperature; a bulb showing an actual colour
    # reports that colour. So a *tinted* RGB is the live value even when a
    # stale temperature sits beside it -- taking kelvin first would capture a
    # pink bulb as white. A grey or zero RGB carries no colour information,
    # so there the temperature wins.
    lit = bool(rgb) and any(rgb)
    tinted = lit and (max(rgb) - min(rgb)) > GREY_TOLERANCE

    if tinted:
        settings['mode'] = MODE_COLOR
        settings['color'] = rgb
    elif kelvin > 0:
        settings['mode'] = MODE_TEMP
        settings['kelvin'] = max(1500, min(12000, kelvin))
    elif lit:
        settings['mode'] = MODE_COLOR
        settings['color'] = rgb

    return settings


def capture_scene(name, devices, states):
    """Build a scene from what the lights are doing right now.

    Returns (scene, captured_count, skipped_names). This is how a Govee
    Tap-to-Run gets into Kodi: run it in the Govee app, then snapshot the
    result here. Replaying the snapshot is pure LAN, so it needs no account
    credentials and no cloud round-trip.
    """
    per_device = {}
    skipped = []
    scale = detect_brightness_scale(states)

    for device in devices:
        settings = state_to_settings(states.get(device.device_id),
                                     brightness_scale=scale)
        if settings is None:
            skipped.append(device.name)
            continue
        per_device[device.device_id] = settings

    scene = make_scene(name, targets=sorted(per_device.keys()),
                       devices=per_device)
    return scene, len(per_device), skipped


def _can(controller, device, capability, command):
    """Whether it is worth sending `command` to this device.

    A scene is written once and applied to whatever is enabled, which now
    includes devices that have no colour and no brightness at all. Asking the
    driver what a device claims keeps a plug in a scene from being sent a
    brightness it can only refuse -- the Govee-era question, "does the bulb
    list this command", is now the fallback rather than the rule.
    """
    getter = getattr(controller, 'capabilities', None)
    if getter is not None:
        capabilities = getter(device)
        if capabilities is not None:
            return capability in capabilities
    return device.supports_cmd(command)


def apply_settings(controller, device, settings, colors_only=False):
    """Drive one device to one settings dict. Raises ControlError on failure.

    Shared by scene application, the status round-trip's restore step, and the
    naming walkthrough's highlight-and-put-back, so the ordering rules live in
    one place rather than being re-derived at each call site.

    `colors_only` sends just the colour, for a cycle step where power and
    brightness are already where the previous step left them. That is a third
    of the datagrams -- worth having when a step means talking to every light
    at once and a dropped packet leaves a bulb visibly stuck on the last
    colour until the next tick.
    """
    can_color = _can(controller, device, CAP_COLOR, 'color')
    can_temp = _can(controller, device, CAP_COLOR_TEMP, 'colorTem')

    if colors_only:
        if settings['mode'] == MODE_COLOR and can_color:
            controller.set_color(device, *settings['color'])
        elif settings['mode'] == MODE_TEMP and can_temp:
            controller.set_color_temp(device, settings['kelvin'])
        return

    can_power = _can(controller, device, CAP_POWER, 'turn')

    if settings['power'] == POWER_OFF:
        if can_power:
            controller.turn(device, False)
        return

    if settings['power'] == POWER_ON and can_power:
        controller.turn(device, True)

    # Brightness before colour: on several Govee models a colour command
    # re-asserts the previous brightness, so setting colour last keeps the
    # two from fighting.
    if settings['brightness'] is not None \
            and _can(controller, device, CAP_BRIGHTNESS, 'brightness'):
        controller.set_brightness(device, settings['brightness'])

    if settings['mode'] == MODE_COLOR and can_color:
        controller.set_color(device, *settings['color'])
    elif settings['mode'] == MODE_TEMP and can_temp:
        controller.set_color_temp(device, settings['kelvin'])


def default_scenes():
    """The starter set, written on first run and editable afterwards."""
    return [
        make_scene('Movie Night', power=POWER_ON, brightness=8,
                   mode=MODE_TEMP, kelvin=2000),
        make_scene('Paused', power=POWER_ON, brightness=35,
                   mode=MODE_TEMP, kelvin=2400),
        make_scene('Lights Up', power=POWER_ON, brightness=100,
                   mode=MODE_TEMP, kelvin=4000),
        make_scene('Warm Evening', power=POWER_ON, brightness=55,
                   mode=MODE_TEMP, kelvin=2700),
        make_scene('Paragon Purple', power=POWER_ON, brightness=60,
                   mode=MODE_COLOR, color=[150, 60, 220]),
        make_scene('All Off', power=POWER_OFF),
    ]


def normalise(scene):
    """Coerce a loaded scene into the expected shape and value ranges.

    Scenes are user-editable JSON, so anything read back off disk is treated
    as untrusted: a hand-edited file should degrade to sane values rather than
    throw somewhere deep in a playback callback.
    """
    if not isinstance(scene, dict):
        return None
    name = scene.get('name')
    # `str` is not enough of a check on Python 2, where a name loaded from
    # JSON comes back as `unicode`; duck-typing on strip() covers both.
    if not name or not hasattr(name, 'strip'):
        return None
    name = name.strip()
    if not name:
        return None

    settings = _normalise_settings(scene) or _normalise_settings({})

    targets = scene.get('targets') or []
    if not isinstance(targets, list):
        targets = []
    targets = [str(t).upper() for t in targets if t]

    per_device = {}
    raw_devices = scene.get('devices')
    if isinstance(raw_devices, dict):
        for device_id, entry in raw_devices.items():
            cleaned = _normalise_settings(entry)
            if cleaned is not None and device_id:
                per_device[str(device_id).upper()] = cleaned

    colors = []
    raw_colors = scene.get('colors')
    if isinstance(raw_colors, list):
        for entry in raw_colors:
            item = _normalise_mix_entry(entry)
            if item is not None:
                colors.append(item)

    actions = []
    raw_actions = scene.get('actions')
    if isinstance(raw_actions, list):
        for entry in raw_actions:
            item = _normalise_action(entry)
            if item is not None:
                actions.append(item)

    bar_brightness = _normalise_level(scene.get('bar_brightness'))
    backlight_brightness = _normalise_level(scene.get('backlight_brightness'))
    strip_brightness = _normalise_level(scene.get('strip_brightness'))

    try:
        cycle = max(0, int(scene.get('cycle') or 0))
    except (TypeError, ValueError):
        cycle = 0

    mode = settings['mode']
    if mode == MODE_MIX and not colors:
        # A mix with nothing in it would silently do nothing to the colour.
        mode = MODE_NONE

    return {
        'name': name,
        'power': settings['power'],
        'brightness': settings['brightness'],
        'bar_brightness': bar_brightness,
        'backlight_brightness': backlight_brightness,
        'strip_brightness': strip_brightness,
        'mode': mode,
        'color': settings['color'],
        'kelvin': settings['kelvin'],
        'targets': targets,
        'devices': per_device,
        'colors': colors,
        # Only a mix has anything to cycle through.
        'cycle': cycle if mode == MODE_MIX else 0,
        'actions': actions,
    }


def normalise_all(raw):
    """Normalise a loaded list, dropping entries that cannot be salvaged."""
    if not isinstance(raw, list):
        return []
    cleaned = []
    seen = set()
    for entry in raw:
        scene = normalise(entry)
        if scene is None:
            continue
        key = scene['name'].lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(scene)
    return cleaned


def find(scenes, name):
    """Look a scene up by name, case-insensitively. Returns None if absent."""
    if not name:
        return None
    wanted = name.strip().lower()
    for scene in scenes or []:
        if scene.get('name', '').strip().lower() == wanted:
            return scene
    return None


def describe(scene):
    """One-line summary for list rows, e.g. '35%, 2400K'."""
    bits = []
    per_device = scene.get('devices') or {}
    if per_device:
        # A captured scene has no single brightness or colour to report.
        lit = len([s for s in per_device.values()
                   if s.get('power') != POWER_OFF])
        return 'captured, %d light(s), %d on' % (len(per_device), lit)
    if scene.get('power') == POWER_OFF:
        return 'Off'
    if scene.get('power') == POWER_KEEP:
        bits.append('keep power')
    if scene.get('brightness') is not None:
        bits.append('%d%%' % scene['brightness'])
    if scene.get('bar_brightness') is not None:
        bits.append('bars %d%%' % scene['bar_brightness'])
    if scene.get('backlight_brightness') is not None:
        bits.append('backlight %d%%' % scene['backlight_brightness'])
    if scene.get('strip_brightness') is not None:
        bits.append('strip %d%%' % scene['strip_brightness'])
    if scene.get('mode') == MODE_COLOR:
        color = scene.get('color') or [255, 255, 255]
        bits.append('RGB %d,%d,%d' % tuple(color[:3]))
    elif scene.get('mode') == MODE_MIX:
        bits.append('mix of %d colours' % len(scene.get('colors') or []))
    elif scene.get('mode') == MODE_TEMP:
        bits.append('%dK' % scene.get('kelvin', 2700))
    targets = scene.get('targets') or []
    bits.append('%d light(s)' % len(targets) if targets else 'all lights')
    actions = scene.get('actions') or []
    if actions:
        bits.append('%d command(s)' % len(actions))
    return ', '.join(bits)


def scene_expresses(scene):
    """The capabilities a scene actually describes.

    A scene that sets a colour is a statement about things that have a
    colour. A scene that only says "off" is a statement about anything that
    switches.
    """
    needed = set()
    mode = scene.get('mode')
    if mode in (MODE_COLOR, MODE_MIX):
        needed.add(CAP_COLOR)
    elif mode == MODE_TEMP:
        needed.add(CAP_COLOR_TEMP)
    if scene.get('brightness') is not None:
        needed.add(CAP_BRIGHTNESS)
    return needed


def _caps(controller, device):
    """What this device can be asked to do, via the controller when there is one."""
    getter = getattr(controller, 'capabilities', None) if controller else None
    if getter is None:
        return None
    return set(getter(device) or [])


def is_a_light(device, controller=None):
    """Whether this device is a light, which is the only thing a scene touches.

    A light has a colour, a colour temperature or a brightness. A plug reports
    power and state and nothing else; an infrared blaster reports neither. A
    scene describes how a room looks, so neither belongs in one -- switching a
    plug or firing a blaster is a sequence's job, and a sequence can do both.

    This is the single rule: it decides what the target picker offers, what
    "all" means, and what a scene touches even when targets were named. A
    scene saved before this that names a plug simply passes over it now,
    rather than the picker and the engine disagreeing about it.
    """
    caps = _caps(controller, device)
    if caps is None:
        return True
    return bool(caps & set(LIGHT_CAPABILITIES))


def scene_targets(scene, devices, controller=None):
    """Resolve a scene's target list against the known devices.

    A scene with no targets of its own means "everything this scene can
    actually say something about", not "every device in the house".

    Scenes predate plugs. When a scene named no targets it meant all the
    lights, because lights were all there was -- and then plugs arrived and
    "all" silently widened to include them. A colour scene would switch every
    plug in the house as a side effect of the power setting that came with
    the colour, which is not something anyone asked it to do.

    A scene only ever touches lights -- see is_a_light. With no targets named
    that means all of them; with targets named it means those of them. An "all
    off" scene that also cut the plugs was the older reading, and it made a
    scene quietly reach past what it describes.

    A scene that expresses a colour narrows further, to devices that have one.
    """
    lights = [d for d in devices if d.enabled and is_a_light(d, controller)]

    targets = scene.get('targets') or []
    if targets:
        wanted = set(targets)
        return [d for d in lights if d.device_id in wanted]

    needed = scene_expresses(scene)
    if not needed or controller is None:
        return lights

    getter = getattr(controller, 'capabilities', None)
    if getter is None:
        return lights
    return [d for d in lights if needed & set(getter(d) or [])]


def apply_scene(controller, scene, devices, log_func=None,
                shuffle_func=None, assignment=None, colors_only=False,
                gap=0.004, sleep_func=None):
    """Apply `scene` and return (applied_count, [error strings]).

    Every device is attempted even if an earlier one failed, so one
    unreachable light does not leave the rest of the room untouched.
    """
    from devices import ControlError  # local import keeps this module standalone

    log = log_func or (lambda message: None)
    scene = normalise(scene)
    if scene is None:
        return 0, ['That scene is not valid']

    targets = scene_targets(scene, devices, controller)
    if not targets:
        # A scene may be nothing but commands -- switch the amp on, no lights
        # involved -- so an empty target list is only a problem when there is
        # also nothing to fire.
        if scene.get('actions'):
            return fire_actions(controller, scene, devices, log)
        return 0, ['No lights matched that scene']

    # A mix is dealt once, here, rather than per device: spreading colours
    # evenly is a property of the whole set of lights, so it cannot be decided
    # one light at a time. A caller that is cycling passes the assignment in
    # so each step rotates the arrangement already on the wall instead of
    # scattering afresh.
    dealt = None
    if scene['mode'] == MODE_MIX and scene['colors']:
        dealt = assignment
        if dealt is None:
            dealt = deal_assignment(len(scene['colors']),
                                    [d.device_id for d in targets],
                                    shuffle_func)

    import time as _time
    sleep = sleep_func or _time.sleep

    applied = 0
    errors = []
    per_device_map = scene.get('devices') or {}
    bar_brightness = scene.get('bar_brightness')
    backlight_brightness = scene.get('backlight_brightness')
    strip_brightness = scene.get('strip_brightness')
    for index, device in enumerate(targets):
        # A few milliseconds between lights. Sending 25 lights' worth of
        # datagrams as fast as the loop runs is the shape of traffic consumer
        # access points drop, which was already measured to cost replies on
        # this hardware; the whole pause is a tenth of a second over 25
        # lights and buys a far better chance every command lands.
        if index and gap:
            sleep(gap)
        # A captured scene carries this device's own recorded settings; every
        # other scene falls back to its single uniform set.
        settings = settings_for(scene, device.device_id)
        # A lightbar puts out far more light than a bulb at the same
        # percentage, so a scene can carry a second figure just for them, a
        # third for a backlight and a fourth for a light strip.
        #
        # The order is most specific first, and it matters because these
        # names overlap in real rooms: a backlight is usually a strip, and a
        # strip is sometimes a bar. "TV Backlight Strip" carries two of the
        # three words and has to resolve the same way every time.
        #
        # Strip is last for a second reason. It was added after the other
        # two, and going last means no device that already took a bar or
        # backlight figure quietly started taking a different one.
        if backlight_brightness is not None and is_backlight(device):
            settings = dict(settings)
            settings['brightness'] = backlight_brightness
        elif bar_brightness is not None and is_lightbar(device):
            settings = dict(settings)
            settings['brightness'] = bar_brightness
        elif strip_brightness is not None and is_lightstrip(device):
            settings = dict(settings)
            settings['brightness'] = strip_brightness
        if dealt is not None and device.device_id not in per_device_map:
            slot = dealt.get(device.device_id)
            if slot is not None:
                # Copy before editing: settings_for hands back the scene's own
                # dict when there is a per-device entry, and this must not
                # write a dealt colour back into the saved scene.
                settings = dict(settings)
                settings['mode'] = MODE_COLOR
                settings['color'] = list(
                    scene['colors'][slot % len(scene['colors'])]['color'])
        try:
            apply_settings(controller, device, settings,
                           colors_only=colors_only)
            applied += 1
        except ControlError as exc:
            log('Scene "%s" failed on %s: %s' % (scene['name'], device.name, exc))
            errors.append(str(exc))

    fired, action_errors = fire_actions(controller, scene, devices, log)
    return applied + fired, errors + action_errors


def fire_actions(controller, scene, devices, log_func=None):
    """Send a scene's commands. Returns (fired, [error strings]).

    Separate from the state loop because these are not settings: an IR code
    has no before or after, it either goes out or it does not. Fired after the
    state changes so the lights are already moving when the amp clicks on.
    """
    from devices import ControlError

    log = log_func or (lambda message: None)
    actions = scene.get('actions') or []
    if not actions:
        return 0, []

    by_id = dict((d.device_id, d) for d in devices if d.enabled)
    fired = 0
    errors = []

    for action in actions:
        device = by_id.get(action['device'])
        if device is None:
            errors.append('No device for command "%s"' % action['command'])
            continue
        try:
            controller.send_command(device, action['command'])
            fired += 1
        except ControlError as exc:
            log('Scene "%s" command %s failed on %s: %s'
                % (scene['name'], action['command'], device.name, exc))
            errors.append(str(exc))

    return fired, errors
