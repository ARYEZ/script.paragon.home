# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Script entry point.

With no arguments this opens the control panel. With arguments it performs a
single action and exits, which is what makes the add-on usable from a remote
button, a keymap or a favourite:

    RunScript(script.paragon.home,action=toggle)
    RunScript(script.paragon.home,action=scene,name=Movie Night)
    RunScript(script.paragon.home,action=sequence,name=Bedtime)
    RunScript(script.paragon.home,action=brightness,value=20)
    RunScript(script.paragon.home,action=color,value=FF8800)
    RunScript(script.paragon.home,action=color,value=Paragon Purple)
    RunScript(script.paragon.home,action=temp,value=2700)
    RunScript(script.paragon.home,action=off,target=Living Room Strip)
    RunScript(script.paragon.home,action=command,target=Hall RM,name=TV Power)
    RunScript(script.paragon.home,action=remote)
    RunScript(script.paragon.home,action=speeddial)

`target` accepts a device name or a Govee device id; leave it out to act on
every enabled light.
"""

import os
import sys

import xbmc
import xbmcaddon
import xbmcgui

_ADDON_PATH = xbmcaddon.Addon().getAddonInfo('path')
_LIB_PATH = os.path.join(_ADDON_PATH, 'resources', 'lib')
if _LIB_PATH not in sys.path:
    sys.path.insert(0, _LIB_PATH)


def parse_args(argv):
    """Turn Kodi's RunScript arguments into a dict.

    Kodi splits RunScript() parameters on commas, but the `key=value&key=value`
    convention is just as common in the wild, so both separators are accepted
    and a value is allowed to contain spaces (scene names do).
    """
    params = {}
    joined = '&'.join(part for part in argv if part)
    for chunk in joined.replace(',', '&').split('&'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        key, _sep, value = chunk.partition('=')
        params[key.strip().lower()] = value.strip()
    return params


def resolve_targets(app, target):
    """Resolve a `target` argument to a device list, or None for 'all'.

    The rule itself lives on the session, so a keymap and the web remote
    cannot drift apart about what a name means.
    """
    return app.resolve_targets(target)


def _fire_dial_via_remote(number):
    """Fire a speed-dial slot through the live web-remote service.

    The web remote runs inside the background service, where the device
    controllers and sessions are already live, so a slot fired this way behaves
    exactly as it does from a phone. Running it here in this cold RunScript
    process instead rebuilds the controller from cache, which some devices (IR
    blasters, cloud sessions) do not take to as kindly -- which is why the
    phone remote is flawless and a cold press was not always.

    Sends the API token straight up in a header and skips the PIN, the path the
    web remote's own docs reserve for another add-on. Returns True if the
    service accepted the job, False (so the caller can fall back to a cold run)
    when the remote server is not running or refuses it.
    """
    try:
        import json
        import addon_utils as utils
        import remote as remote_lib
        try:
            from urllib.request import Request, urlopen
        except ImportError:
            from urllib2 import Request, urlopen

        port = utils.get_int('remote_port', remote_lib.DEFAULT_PORT)
        token = remote_lib.ensure_token()
        if not token:
            return False
        body = json.dumps({'action': 'dial', 'slot': int(number)})
        if not isinstance(body, bytes):
            body = body.encode('utf-8')
        req = Request('http://127.0.0.1:%d/api/action' % port, data=body)
        req.add_header(remote_lib.TOKEN_HEADER, token)
        req.add_header('Content-Type', 'application/json')
        resp = urlopen(req, timeout=5)
        return resp.getcode() in (200, 202)
    except Exception as exc:
        try:
            import addon_utils as utils
            utils.log('speed dial via web remote failed (%s); running locally'
                      % exc)
        except Exception:
            pass
        return False


def run_action(app, params, utils):
    """Execute a single non-interactive action. Returns True if handled."""
    action = params.get('action', '').lower()
    if not action or action == 'panel':
        return False

    targets = resolve_targets(app, params.get('target'))
    if targets == []:
        utils.force_notify('No light called "%s"' % params.get('target'))
        return True

    def report(result, message):
        done, errors = result
        if done:
            utils.notify(message)
        elif errors:
            utils.force_notify(errors[0])
        else:
            utils.force_notify('No lights to control. Run a device refresh.')

    if action == 'on':
        report(app.power_all(True, targets), 'Lights on')
    elif action == 'off':
        report(app.power_all(False, targets), 'Lights off')
    elif action == 'toggle':
        report(app.toggle_all(targets), 'Lights toggled')
    elif action == 'brightness':
        value = utils.clamp_int(params.get('value'), 1, 100)
        if value is None:
            utils.force_notify('brightness needs value=1-100')
        else:
            report(app.brightness_all(value, targets), 'Brightness %d%%' % value)
    elif action == 'color':
        # A hex code or the name of a saved colour: a colour added to the
        # speed dial can be bound to a remote button by name rather than
        # having its hex copied into the keymap.
        rgb = app.resolve_color(params.get('value'))
        if rgb is None:
            utils.force_notify('color needs value=RRGGBB, AARRGGBB, or the '
                               'name of a saved colour')
        else:
            report(app.color_all(rgb, targets), 'Colour set')
    elif action == 'temp':
        value = utils.clamp_int(params.get('value'), 1500, 12000)
        if value is None:
            utils.force_notify('temp needs value in Kelvin')
        else:
            report(app.color_temp_all(value, targets), '%dK' % value)
    elif action == 'scene':
        name = params.get('name') or params.get('value')
        if not name:
            utils.force_notify('scene needs name=<scene name>')
        else:
            app.apply_scene_by_name(name)
    elif action in ('sequence', 'rerack'):
        # "rerack" still works: it is what this was called until v2.14, and a
        # keymap or favourite holding one should not stop working over a
        # rename.
        name = params.get('name') or params.get('value')
        if not name:
            utils.force_notify('sequence needs name=<sequence name>')
        else:
            app.run_sequence_by_name(name)
    elif action == 'refresh':
        devices, warnings = app.refresh_devices()
        if warnings and not devices:
            utils.force_notify(warnings[0])
        else:
            utils.notify('Found %d light(s)' % len(devices))
    elif action == 'diagnose':
        import diagnostics
        text, _report = diagnostics.run(app)
        xbmcgui.Dialog().ok('%s - LAN diagnostics' % utils.ADDON_NAME,
                            text)
    elif action == 'verifystatus':
        import diagnostics
        chosen = targets or app.enabled_devices
        if not chosen:
            utils.force_notify('No lights to test. Run a device refresh.')
        else:
            # Not named `report`: that is the local result-reporting helper
            # above, and shadowing it here would be a trap for the next edit.
            outcome = diagnostics.verify_status(app, chosen[0])
            xbmcgui.Dialog().ok('%s - status check' % utils.ADDON_NAME,
                                diagnostics.verify_summary(outcome))
    elif action == 'pick_scene':
        # Fired by the "choose scene" buttons in the add-on settings.
        import gui
        setting_id = params.get('setting')
        if setting_id:
            gui.pick_scene_for_setting(app, setting_id)
    elif action == 'command':
        # A learned infrared code, fired from the blaster that learned it.
        # No "all" here: the code belongs to that blaster.
        name = params.get('name') or params.get('value')
        if not name:
            utils.force_notify('command needs name=<learned command>')
        elif targets is None:
            utils.force_notify('command needs target=<blaster name>')
        else:
            report(app.send_command_all(name, targets), 'Sent %s' % name)
    elif action == 'remote':
        # The "show the address and PIN" button in the settings. Typing an
        # address into a phone is the whole friction of the web remote, so it
        # gets a dialog rather than a line in the log.
        import remote as remote_lib
        xbmcgui.Dialog().ok('%s - web remote' % utils.ADDON_NAME,
                            remote_lib.describe())
    elif action == 'speeddial':
        # A fire launcher for the speed dial, used by Paragon TV's sidebar
        # (RunScript(script.paragon.home,action=speeddial)). List the filled
        # slots and run whichever one is picked -- the "one press from the
        # couch" path, rather than the full management menu in the panel.
        import speeddial as dial_lib
        filled = dial_lib.filled(app.dial)
        if not filled:
            utils.force_notify('Speed dial is empty')
        else:
            labels = [app.dial_label(slot) for _number, slot in filled]
            choice = xbmcgui.Dialog().select(
                '%s - speed dial' % utils.ADDON_NAME, labels)
            if choice >= 0:
                number = filled[choice][0]
                # Fire it through the warm web-remote service (live device
                # controllers, exactly as the phone remote does); fall back to
                # running it in this cold process if that server is not up.
                if not _fire_dial_via_remote(number):
                    app.run_dial_slot(number)
    elif action == 'sync':
        # The "copy from the master now" button in the settings.
        import gui
        gui.ControlPanel(app)._sync_now()
    elif action == 'settings':
        utils.open_settings()
    else:
        utils.force_notify('Unknown action "%s"' % action)
    return True


def main():
    import addon_utils as utils

    params = parse_args(sys.argv[1:])
    utils.debug('Started with %s' % (params or 'no arguments'))

    try:
        from paragon_home import ParagonHome
        app = ParagonHome()
    except Exception as exc:
        utils.log('Failed to start: %s' % exc, xbmc.LOGERROR)
        xbmcgui.Dialog().ok(utils.ADDON_NAME,
                            'Could not start the add-on:\n\n%s' % exc)
        return

    try:
        if run_action(app, params, utils):
            return
        import gui
        gui.ControlPanel(app).run()
    except Exception as exc:
        utils.log('Unhandled error: %s' % exc, xbmc.LOGERROR)
        import traceback
        utils.log(traceback.format_exc(), xbmc.LOGERROR)
        xbmcgui.Dialog().ok(utils.ADDON_NAME,
                            'Something went wrong:\n\n%s' % exc)


if __name__ == '__main__':
    main()
