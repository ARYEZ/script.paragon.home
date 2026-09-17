# -*- coding: utf-8 -*-
"""
Paragon Home - manifest and settings validation.

Kodi fails quietly on these: a mistyped setting id reads back as an empty
string, a missing asset shows a blank tile, and a malformed addon.xml means the
add-on simply never appears. None of that raises anywhere the tests can see it,
so it is checked here instead.

    python3 tests/validate_addon.py
"""

from __future__ import print_function

import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ADDON_ID = 'script.paragon.home'

# Settings read through a computed id, so the regex scan cannot see them.
DYNAMIC_SETTINGS = {'scene_playing', 'scene_paused', 'scene_stopped'}

# Settings that exist purely as buttons in the settings screen; nothing reads
# their value.
ACTION_ONLY = {'run_panel', 'run_discover', 'pick_playing', 'pick_paused',
               'pick_stopped'}

# _setting_float is tv.py's own reader: the levels are floats and the shared
# helpers do not do floats. Listed here so a setting read through it does not
# read to this scan as a setting nothing reads.
SETTING_CALL = re.compile(
    r"""(?:get_setting|get_bool|get_int|set_setting|_setting_float)\(\s*['"]([\w.]+)['"]""")

problems = []
notes = []


def fail(message):
    problems.append(message)


def check_addon_xml():
    path = os.path.join(ROOT, 'addon.xml')
    if not os.path.isfile(path):
        fail('addon.xml is missing')
        return None

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        fail('addon.xml is not valid XML: %s' % exc)
        return None

    if root.get('id') != ADDON_ID:
        fail('addon.xml id is %r, expected %r' % (root.get('id'), ADDON_ID))
    for attribute in ('name', 'version', 'provider-name'):
        if not root.get(attribute):
            fail('addon.xml is missing the %s attribute' % attribute)

    points = [ext.get('point') for ext in root.findall('extension')]
    for required in ('xbmc.python.script', 'xbmc.service',
                     'xbmc.addon.metadata'):
        if required not in points:
            fail('addon.xml has no %s extension point' % required)

    # Every library= file must actually exist, or Kodi errors on launch.
    for ext in root.findall('extension'):
        library = ext.get('library')
        if library and not os.path.isfile(os.path.join(ROOT, library)):
            fail('addon.xml points at %s, which does not exist' % library)

    # Krypton needs an explicit xbmc.python import.
    imports = {imp.get('addon'): imp.get('version')
               for imp in root.findall('requires/import')}
    if 'xbmc.python' not in imports:
        fail('addon.xml does not import xbmc.python')
    else:
        version = imports['xbmc.python'] or ''
        if version.startswith('3.'):
            notes.append('xbmc.python is %s - that targets Kodi 19+, not '
                         'Krypton' % version)

    metadata = root.find("extension[@point='xbmc.addon.metadata']")
    if metadata is not None:
        assets = metadata.find('assets')
        if assets is None:
            fail('addon.xml has no <assets> block')
        else:
            for asset in assets:
                if asset.text and not os.path.isfile(
                        os.path.join(ROOT, asset.text)):
                    fail('asset %s (%s) does not exist'
                         % (asset.tag, asset.text))
        if metadata.find('license') is None:
            fail('addon.xml declares no license')
    return root


def check_settings_xml():
    path = os.path.join(ROOT, 'resources', 'settings.xml')
    if not os.path.isfile(path):
        fail('resources/settings.xml is missing')
        return set()

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        fail('settings.xml is not valid XML: %s' % exc)
        return set()

    declared = set()
    for setting in root.iter('setting'):
        setting_id = setting.get('id')
        if not setting_id:
            continue  # <setting type="sep"/> and friends carry no id
        if setting_id in declared:
            fail('settings.xml declares %s more than once' % setting_id)
        declared.add(setting_id)

        # Krypton reads the old format; a <settings><section> tree is the
        # Kodi 18+ one and silently shows an empty settings screen.
        if root.find('section') is not None:
            fail('settings.xml uses the Kodi 18+ format; Krypton needs the '
                 'category/setting format')

    for setting in root.iter('setting'):
        if setting.get('type') == 'action':
            action = setting.get('action') or ''
            if ADDON_ID not in action:
                fail('action setting %s does not RunScript this add-on'
                     % setting.get('id'))
    return declared


def check_setting_usage(declared):
    used = set()
    sources = [os.path.join(ROOT, 'default.py'),
               os.path.join(ROOT, 'service.py')]
    lib = os.path.join(ROOT, 'resources', 'lib')
    for name in sorted(os.listdir(lib)):
        if name.endswith('.py'):
            sources.append(os.path.join(lib, name))

    for path in sources:
        handle = open(path, 'r')
        try:
            source = handle.read()
        finally:
            handle.close()
        used.update(SETTING_CALL.findall(source))

    for setting_id in sorted(used):
        if setting_id not in declared:
            fail('code reads setting %r, which settings.xml does not declare'
                 % setting_id)

    unused = declared - used - DYNAMIC_SETTINGS - ACTION_ONLY
    for setting_id in sorted(unused):
        notes.append('settings.xml declares %r but no code reads it'
                     % setting_id)


def check_scene_defaults(declared):
    """The default scene names in settings.xml must exist in the seeded set."""
    sys.path.insert(0, os.path.join(ROOT, 'resources', 'lib'))
    import scenes as scene_lib

    seeded = {s['name'] for s in scene_lib.default_scenes()}
    path = os.path.join(ROOT, 'resources', 'settings.xml')
    root = ET.parse(path).getroot()

    for setting in root.iter('setting'):
        if setting.get('id') in DYNAMIC_SETTINGS:
            default = setting.get('default') or ''
            if default and default not in seeded:
                fail('settings.xml defaults %s to scene %r, which is not one '
                     'of the seeded scenes' % (setting.get('id'), default))


def check_readme_actions():
    """Actions documented in the README must be handled by default.py."""
    readme = os.path.join(ROOT, 'README.md')
    if not os.path.isfile(readme):
        notes.append('no README.md')
        return

    handle = open(readme, 'r')
    try:
        text = handle.read()
    finally:
        handle.close()

    documented = set(re.findall(r'action=(\w+)', text))
    handle = open(os.path.join(ROOT, 'default.py'), 'r')
    try:
        source = handle.read()
    finally:
        handle.close()
    handled = set(re.findall(r"action == '(\w+)'", source))
    # An action can also be one of several spellings, which is how a renamed
    # verb keeps its old name working. Reading only the == form reported a
    # documented action as unhandled when it was handled perfectly well.
    for group in re.findall(r"action in \(([^)]*)\)", source):
        handled.update(re.findall(r"'(\w+)'", group))
    handled.update(['panel'])

    for action in sorted(documented - handled):
        fail('README documents action=%s, which default.py does not handle'
             % action)


def main():
    check_addon_xml()
    declared = check_settings_xml()
    check_setting_usage(declared)
    check_scene_defaults(declared)
    check_readme_actions()

    for note in notes:
        print('note    %s' % note)
    for problem in problems:
        print('FAIL    %s' % problem)

    if problems:
        print('\n%d problem(s)' % len(problems))
        return 1
    print('\naddon.xml and settings.xml validate; %d setting(s) declared'
          % len(declared))
    return 0


if __name__ == '__main__':
    sys.exit(main())
