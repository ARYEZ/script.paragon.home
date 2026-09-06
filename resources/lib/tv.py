# -*- coding: utf-8 -*-
#
#   Copyright (C) 2025 Aryez
#
# This file is part of Paragon TV.
#
# Paragon TV is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Paragon TV is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Paragon TV.  If not, see <http://www.gnu.org/licenses/>.
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Reading and driving Paragon TV from outside it.

This began inside Paragon TV, where it could reach anything. It lives here
now, so the television and the remote can be developed apart -- and almost
nothing had to change, because Paragon TV publishes what it knows in places
another add-on can read: the channel and the artwork as properties on Kodi's
home window, the channel list as its own settings2.xml, and what is on every
channel as a file it writes beside it.

Three things did have to change, and they are the three that assumed this
code was inside Paragon TV: where the channel logos live, where the utility
scripts live, and which profile holds the files. All three now resolve
through xbmcaddon.Addon("script.paragontv"), the way paragon_tv.py already
reads the Rerack schedule.

**One piece cannot move.** What is playing on the *other* eighty-four
channels can only be worked out by Paragon TV's Overlay -- it holds a Channel
object per channel, with that channel's playlist and the point it was last
known at, and nothing outside it has those objects. So the Overlay writes
that down every few seconds and this reads the file. That hook is the whole
of what Paragon TV still carries for the remote's sake.

Nothing here writes to Paragon TV's own files. It reads them, and drives the
television the way any remote does -- through Kodi.

Everything degrades when Paragon TV is not installed: `installed()` answers
False and the remote simply has no television half.
"""

import json
import os
import re
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from compat import string_types, unquote

ADDON_ID = 'script.paragontv'

# Kodi's home window. Every PTV property lands here -- see Overlay.setProperty,
# which redirects to this window precisely so the skin can read them.
HOME_WINDOW = 10000

# What the Overlay publishes about what is on. Read only; never set from here.
#
# Not PTV.ChannelNumber, which is the obvious-looking one and is wrong: that
# is the on-screen channel label, set by showChannelLabel and cleared by
# hideChannelLabel about ten seconds later. Reading it means asking "was the
# channel changed in the last few seconds", which answers no almost always.
# PTV.Remote.Channel is set beside self.currentChannel in setChannel and
# cleared in end(), so it holds for as long as the television is on.
PROP_CHANNEL = 'PTV.Remote.Channel'
PROP_BROWSING = 'PTV.Browsing'

# The artwork for what is playing. Paragon TV works this out itself on every
# show start rather than trusting Player.Art, which lags when a channel turns
# over on its own -- see Overlay.updateNowPlayingArt. It is a plain path to a
# file beside the media, which is what makes it servable.
PROP_ART = 'PTV.NowPlaying.Landscape'

# What may be served as artwork. Not a general file server: the page cannot
# name a file, and even the file the Overlay names has to look like a picture.
ART_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
}

# The channel logo files Paragon TV keeps, best first.
#
# `_c` is the clear wordmark -- white on transparent -- which is the one the
# EPG grid shows and the one that suits a dark page. `_c2` is the same
# wordmark in colour. The plain name is the full tile with its own background,
# which is busy at the size a channel row gives it, so it is the last resort
# before falling back to simply writing the channel's name out.
LOGO_SUFFIXES = ('_c.png', '_c2.png', '.png')

# Where Paragon TV keeps them. The stored default still points at PseudoTV's
# folder, which is why the Overlay checks the folder exists before trusting
# it; this does the same, and lands on the add-on's own logos when it does not.
LOGO_SETTING = 'ChannelLogoFolder'

# A logo is a small PNG. This is loose enough never to refuse a real one and
# tight enough that a mistake in the folder setting cannot ask a phone to
# download something enormous.
LOGO_MAX_BYTES = 4 * 1024 * 1024

# Fanart runs to a few megabytes; past this something is wrong and a phone
# should not be made to wait for it.
ART_MAX_BYTES = 12 * 1024 * 1024

# Paragon TV's own channel store, beside its settings.xml in the add-on's
# profile. Flat keys, one line each, exactly as Settings.py writes them.
SETTINGS_FILE = 'settings2.xml'

# Matched the way Settings.loadSettings matches it, line by line rather than
# as XML: a half-written file has to read as the settings that are intact
# rather than as a parse error, and that is the behaviour Paragon TV already
# relies on.
SETTING_LINE = re.compile(r'setting id="(.*?)"(?:.*?) value="(.*?)"')

# Paragon TV allows up to this many channels.
MAX_CHANNELS = 999

# The mute button sets a level rather than silencing the television: quiet
# enough to talk over, not so quiet that the room feels switched off.
#
# Kodi has no way to be told a decibel figure. Application.SetVolume takes an
# integer percentage, and Kodi maps percentage linearly onto its volume range
# -- so a level in dB converts cleanly, and dB is the right unit to keep it
# in because that is what the volume bar shows you.
#
# The floor is Kodi's own VOLUME_MINIMUM. It is a setting because a build
# with a different floor would put every level in the wrong place, and one
# number here is a better fix than a rebuild.
VOLUME_FLOOR_DB = -60.0
QUIET_LEVEL_DB = -39.3
NORMAL_LEVEL_DB = -20.0

# How near a level counts as being at it. Wide enough that a nudge of the
# volume keys does not strand the button, narrow enough that the two levels
# never overlap.
LEVEL_TOLERANCE_DB = 2.0

# What is on every channel, written by the Overlay every few seconds. See
# WebRemoteSnapshot: only the Overlay can work this out, so it writes it down
# and this reads it.
# The file Paragon TV's Overlay writes with what is on every channel. Named
# here rather than imported, because the module that writes it stays over
# there -- see the note about the Overlay above.
SNAPSHOT_FILE = 'webremote_now.json'

# Past this the snapshot is stale enough to be misleading -- the television
# has probably been shut down, and the last thing it was showing is not what
# is on now.
SNAPSHOT_STALE_AFTER = 90

# The actions Paragon TV's onAction already answers to, by the names Kodi's
# Action() builtin knows them by. Sending these is exactly what pressing the
# button on the physical remote does: Kodi delivers them on its own thread,
# through onAction and its semaphore, into the same handlers.
#
# That is the whole reason for going this way round rather than reaching into
# the Overlay from here. Every one of the twelve places that changes channel
# does it from Kodi's thread, and a web server's thread is not that thread.
ACTION_CHANNEL_UP = 'pageup'
ACTION_CHANNEL_DOWN = 'pagedown'
ACTION_SELECT = 'select'

# Kodi queues builtins, but the Overlay reads digits into a buffer that a
# select then commits, so they have to arrive as separate actions rather than
# in one breath.
KEY_PAUSE = 0.12

# ---------------------------------------------------------------------------
# Channel naming.
#
# Mirrored from ChannelList.getChannelName, which is the source of truth. It
# is copied rather than imported because importing ChannelList pulls in
# Globals, FileAccess and a good deal of the add-on, and this only needs to
# turn two strings into a label.
#
# A copy can go stale. If Paragon TV grows a channel type, one appears here
# unnamed rather than wrongly named -- see channel_name.
# ---------------------------------------------------------------------------

TYPE_PLAYLIST = 0
TYPE_TV_GENRE = 3
TYPE_MOVIE_GENRE = 4
TYPE_MUSIC_GENRE = 12


def _addon():
    """Paragon TV's add-on handle, or None when it is not installed.

    Kodi raises for an add-on that is not there, which is the answer rather
    than an error: a box with lights and no television is a perfectly good
    box, and the remote should simply have no television half.
    """
    try:
        return xbmcaddon.Addon(id=ADDON_ID)
    except Exception:
        return None


def installed():
    """Whether this box has Paragon TV at all."""
    return _addon() is not None


def addon_path():
    """Where Paragon TV is installed, or '' -- its logos and scripts are
    under here."""
    addon = _addon()
    if addon is None:
        return ''
    return _translate(addon.getAddonInfo('path'))


def _translate(path):
    """A Kodi path made real, on either Kodi generation."""
    if not path:
        return ''
    translate = getattr(xbmc, 'translatePath', None)
    if translate is None:  # pragma: no cover - Kodi 19+ only
        translate = xbmcvfs.translatePath
    resolved = translate(path)
    if isinstance(resolved, bytes):
        resolved = resolved.decode('utf-8', 'replace')
    return resolved


def profile_dir():
    """Paragon TV's own profile directory, however Kodi spells it.

    Empty when Paragon TV is not installed, which every reader treats as
    "nothing to read" rather than as a failure.
    """
    addon = _addon()
    if addon is None:
        return ''
    return _translate(addon.getAddonInfo('profile'))


def read_channel_settings(path=None):
    """Every key in settings2.xml, as a dict. Empty if it is not there yet."""
    path = path or os.path.join(profile_dir(), SETTINGS_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        handle = open(path, 'r')
        try:
            raw = handle.read()
        finally:
            handle.close()
    except (OSError, IOError):
        return {}

    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')

    found = {}
    for line in raw.splitlines():
        match = SETTING_LINE.search(line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def playlist_name(path):
    """The <name> inside a smart playlist, or '' if it cannot be read."""
    if not path:
        return ''
    translate = getattr(xbmc, 'translatePath', None)
    if translate is not None:
        path = translate(path)
    if isinstance(path, bytes):
        path = path.decode('utf-8', 'replace')
    try:
        handle = open(path, 'r')
        try:
            raw = handle.read()
        finally:
            handle.close()
    except (OSError, IOError):
        return ''
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    match = re.search(r'<name>(.*?)</name>', raw, re.DOTALL)
    return match.group(1).strip() if match else ''


def channel_name(chtype, setting1):
    """A channel's label from its type and first setting.

    Follows ChannelList.getChannelName. A type this does not know returns an
    empty string, and the caller falls back to the channel number -- an
    unnamed channel that still tunes is a better answer than a confidently
    wrong name.
    """
    if not setting1:
        return ''
    try:
        chtype = int(chtype)
    except (TypeError, ValueError):
        return ''

    if chtype == TYPE_PLAYLIST:
        return playlist_name(setting1)
    if chtype == TYPE_TV_GENRE:
        return '%s TV' % setting1
    if chtype == TYPE_MOVIE_GENRE:
        return '%s Movies' % setting1
    if chtype == TYPE_MUSIC_GENRE:
        return 'Music Genre - %s' % setting1
    return ''


def channels(settings=None):
    """The configured channels, in order, as {number, name, type}.

    A channel counts as configured when it has a type and a first setting.
    Paragon TV numbers from one and allows gaps, so this walks the numbers
    rather than counting entries.
    """
    settings = read_channel_settings() if settings is None else settings
    found = []
    for number in range(1, MAX_CHANNELS + 1):
        chtype = settings.get('Channel_%d_type' % number)
        if chtype is None:
            continue
        setting1 = settings.get('Channel_%d_1' % number, '')
        if not setting1:
            continue
        name = channel_name(chtype, setting1)
        found.append({
            'number': number,
            'name': name or 'Channel %d' % number,
            'named': bool(name),
            'type': chtype,
        })
    return found


# ---------------------------------------------------------------------------
# Channel logos
# ---------------------------------------------------------------------------

def _ascii(text):
    """The channel name as Paragon TV spells it when building a file name.

    Globals.ascii drops anything outside ASCII before the name becomes a
    path, so a channel with an accent in it resolves to the file the Overlay
    would have written. Matching that here is the difference between finding
    the logo and not.
    """
    if isinstance(text, bytes):
        return text.decode('utf-8', 'replace').encode('ascii', 'ignore').decode('ascii')
    try:
        return text.encode('ascii', 'ignore').decode('ascii')
    except Exception:
        return text


def logo_dir():
    """The folder Paragon TV takes channel logos from, with a trailing slash.

    The setting's shipped default names PseudoTV's folder, which is not there
    on most boxes -- so, exactly as the Overlay does, an unusable setting
    falls back to the logos this add-on carries itself.
    """
    addon = _addon()
    folder = ''
    if addon is not None:
        try:
            folder = addon.getSetting(LOGO_SETTING) or ''
        except Exception:
            folder = ''
    if folder:
        folder = _translate(folder)
        try:
            if xbmcvfs.exists(folder):
                return folder if folder.endswith(('/', '\\')) else folder + '/'
        except Exception:
            pass

    # Paragon TV's own logos, not ours. This file used to sit inside that
    # add-on, where walking up from __file__ found them; from here that would
    # find Paragon Home's resources folder, which has no channel logos in it.
    here = addon_path()
    if not here:
        return ''
    return os.path.join(here, 'resources', 'logos') + os.sep


def channel_backdrop(name):
    """The channel's own landscape tile, or '' when it has none.

    The plain `<name>.png` in the logo folder, and only that one: the `_c`
    files are wordmarks on transparent, which are right on a button and wrong
    as the background of a card. This one is the full picture -- 1000x563 in
    the shipped set, sixteen by nine like the card it fills.
    """
    name = _ascii((name or '').strip())
    if not name or '/' in name or '\\' in name or '..' in name:
        return ''

    candidate = logo_dir() + name + '.png'
    exists, size, _modified = art_stat(candidate)
    if exists and size <= ART_MAX_BYTES:
        return candidate
    return ''


def channel_logo(name):
    """The logo file for a channel name, or '' when it has none.

    Names are the caller's, but the file name is not: only the suffixes in
    LOGO_SUFFIXES are ever tried, and a name carrying a slash or a `..` is
    refused outright rather than being allowed to walk out of the folder.
    """
    name = _ascii((name or '').strip())
    if not name or '/' in name or '\\' in name or '..' in name:
        return ''

    folder = logo_dir()
    for suffix in LOGO_SUFFIXES:
        candidate = folder + name + suffix
        exists, size, _modified = art_stat(candidate)
        if exists and size <= LOGO_MAX_BYTES:
            return candidate
    return ''


# ---------------------------------------------------------------------------
# What is on right now
# ---------------------------------------------------------------------------

def _home_property(name):
    try:
        return xbmcgui.Window(HOME_WINDOW).getProperty(name) or ''
    except Exception:
        return ''


def current_channel():
    """The channel Paragon TV is showing, or None when it is not running.

    The Overlay sets this as it tunes and clears it on the way out, so an
    empty value is the signal that nothing is watching -- there is no separate
    "am I running" flag, and this doubles as one.
    """
    raw = (_home_property(PROP_CHANNEL) or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def now_playing():
    """What Kodi is playing, as far as it will say. Never raises.

    Read straight from the player rather than from the Overlay: the Overlay
    publishes its own labels for the skin, but the title and the time left are
    on the player already and do not need a second copy that can disagree.
    """
    playing = {'playing': False, 'title': '', 'elapsed': 0, 'total': 0}
    try:
        player = xbmc.Player()
        if not player.isPlaying():
            return playing
        playing['playing'] = True
        try:
            playing['title'] = player.getPlayingFile()
        except Exception:
            pass
        try:
            info = player.getVideoInfoTag()
            title = info.getTitle()
            if title:
                playing['title'] = title
        except Exception:
            # Music, or a stream still opening. The file name stands in.
            pass
        try:
            playing['elapsed'] = int(player.getTime())
            playing['total'] = int(player.getTotalTime())
        except Exception:
            # Both throw if playback stops between the check and the call.
            pass
    except Exception:
        return {'playing': False, 'title': '', 'elapsed': 0, 'total': 0}

    if playing['title'] and '/' in playing['title']:
        playing['title'] = playing['title'].rsplit('/', 1)[-1]
    if playing['title'] and '\\' in playing['title']:
        playing['title'] = playing['title'].rsplit('\\', 1)[-1]
    return playing


def read_now_on():
    """What is on each channel, from the Overlay's snapshot. {} if unusable.

    Absent means the television has not been on since Kodi started; stale
    means it was, and is not now. Both come back empty rather than as an old
    programme presented as a current one.
    """
    path = os.path.join(profile_dir(), SNAPSHOT_FILE)
    try:
        handle = open(path, 'r')
        try:
            payload = json.loads(handle.read())
        finally:
            handle.close()
    except (OSError, IOError, ValueError):
        return {}

    if not isinstance(payload, dict):
        return {}
    written = payload.get('at') or 0
    if time.time() - written > SNAPSHOT_STALE_AFTER:
        return {}

    found = {}
    for entry in payload.get('channels') or []:
        try:
            found[int(entry['number'])] = entry
        except (KeyError, TypeError, ValueError):
            continue
    return found


def art_stat(path):
    """(exists, size, modified) for a Kodi path. Never raises.

    Everything here goes through xbmcvfs rather than os.path, because what
    the Overlay publishes is a Kodi path and a Kodi path is not always a
    filesystem path. Media on a NAS arrives as `smb://box/share/...`, which
    os.path cannot see at all -- it reports every such file as missing, and
    the artwork silently never appeared for anyone whose media is on a share.
    The Overlay finds these files with FileAccess.exists, which is xbmcvfs;
    reading them back has to go the same way.
    """
    try:
        if not xbmcvfs.exists(path):
            return False, 0, 0
    except Exception:
        return False, 0, 0

    try:
        stat = xbmcvfs.Stat(path)
        return True, int(stat.st_size()), int(stat.st_mtime())
    except Exception:
        # Stat is the nicety, not the answer. Some VFS backends do not
        # implement it, and a picture that is there but unmeasurable is
        # still a picture -- reading it will find out soon enough.
        return True, 0, 0


# Where a picture for the current show might come from, best first.
#
# The Overlay's own property is best because Paragon TV computes it on every
# show start precisely because Kodi's answer lags when a channel turns over on
# its own. But it only ever names a file sitting beside the media -- a library
# whose art lives in Kodi's own cache rather than in the show's folder has
# nothing there to find, and that is a normal way to keep a library. So Kodi's
# answer is worth having as a second choice: stale by a show is much better
# than blank forever.
ART_LABELS = ('Player.Art(landscape)', 'Player.Art(fanart)',
              'Player.Art(thumb)')


def _decode_image_url(path):
    """`image://...` unwrapped to the path inside it.

    Kodi hands back its own cache URLs for artwork it has already seen. The
    real path is url-encoded inside, with a trailing slash on the outside.
    """
    if not path.startswith('image://'):
        return path
    inner = path[len('image://'):].rstrip('/')
    try:
        return unquote(inner)
    except Exception:
        return path


def _usable_art(raw, quiet=False):
    """`raw` if it is a picture that is really there, else ''."""
    if not raw:
        return ''
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'replace')
    raw = _decode_image_url(raw.strip())
    if raw.startswith('special://'):
        raw = _translate(raw)

    if os.path.splitext(raw)[1].lower() not in ART_TYPES:
        if not quiet:
            _reject(raw, 'not a picture')
        return ''

    exists, size, _modified = art_stat(raw)
    if not exists:
        if not quiet:
            _reject(raw, 'not there')
        return ''
    if size > ART_MAX_BYTES:
        if not quiet:
            _reject(raw, 'too large (%s bytes)' % size)
        return ''
    return raw


def now_art(channel=None):
    """The artwork file for what is playing, or '' if there is none.

    Three places, in the order of how specific each one is to what is
    actually on:

    1. What the Overlay worked out for this show -- landscape.jpg beside the
       media, then fanart, then a banner.
    2. What Kodi says is playing, for a library that keeps its art in Kodi's
       own cache rather than in the show's folder.
    3. The channel's own landscape tile.

    The third is why this exists: plenty of shows have no artwork of their own
    at all, and a channel that has one is a better answer than a blank card.
    It is last because it says which channel rather than which programme, and
    the programme is the more useful of the two when there is a choice.

    `channel` is the tuned channel's name when the caller already knows it --
    the snapshot does, and looking it up again would mean re-reading the
    channel settings on every poll.

    Checked here rather than at the point of serving so that the page is told
    whether there is a picture before it asks for one.
    """
    found = _usable_art(_home_property(PROP_ART))
    if found:
        return found

    # The Overlay had nothing usable. Ask Kodi what it is showing.
    for label in ART_LABELS:
        try:
            found = _usable_art(xbmc.getInfoLabel(label), quiet=True)
        except Exception:
            found = ''
        if found:
            return found

    # Nothing for the programme. Fall back to the channel it is on.
    if channel is None:
        channel = _tuned_channel_name()
    if channel:
        return channel_backdrop(channel)
    return ''


def _tuned_channel_name():
    """The name of the channel now on, or '' -- reading the settings to do it.

    Only for callers that do not already know it. The snapshot passes the
    name it has just resolved rather than sending this round again.
    """
    number = current_channel()
    if number is None:
        return ''
    for entry in channels():
        if entry['number'] == number:
            return entry['name']
    return ''


_rejected = ['']


def _reject(path, why):
    """Say once, in the log, why a picture the Overlay named is not being used.

    Once, because now_art runs on every poll and the same rejection would
    otherwise fill the log. Said at all, because the first version of this
    rejected every path on a network share and looked exactly like a show
    that simply had no artwork.
    """
    if _rejected[0] == path:
        return
    _rejected[0] = path
    xbmc.log('[Paragon TV] Web remote: not showing artwork %s: %s'
             % (path, why), xbmc.LOGNOTICE)


def art_key(path=None):
    """A short tag that changes when the artwork does.

    The page hangs this on the image's address so a new show fetches a new
    picture, while the same show does not refetch the same one every time the
    page looks for news. The server ignores it -- what /art serves is whatever
    is playing now, and nothing a caller says can change that.
    """
    path = now_art() if path is None else path
    if not path:
        return ''
    _exists, size, modified = art_stat(path)
    digest = 0
    for character in '%s|%s|%s' % (path, size, modified):
        digest = (digest * 131 + ord(character)) & 0xFFFFFFFF
    return '%08x' % digest


# ---------------------------------------------------------------------------
# Driving Kodi itself
# ---------------------------------------------------------------------------

# Every button the page may press, and the JSON-RPC method behind it.
#
# JSON-RPC rather than HTTP: Kore and YARC are separate applications and have
# to reach Kodi across the network, through the web server, with a password.
# This add-on is already inside Kodi, so xbmc.executeJSONRPC runs the same API
# in-process -- no port to open, no credentials to keep, nothing for anyone to
# configure. Paragon TV has driven itself this way since long before the
# remote existed; this is the same door, not a new one.
#
# An allow-list, and the point of it: the page names a button, never a method.
# Nothing a caller sends can reach a method that is not on this list, so the
# remote cannot be talked into wiping the library or installing an add-on --
# which JSON-RPC is perfectly capable of doing.
BUTTONS = {
    # -- moving about -----------------------------------------------------
    'up': ('Input.Up', None),
    'down': ('Input.Down', None),
    'left': ('Input.Left', None),
    'right': ('Input.Right', None),
    'select': ('Input.Select', None),
    'back': ('Input.Back', None),
    'home': ('Input.Home', None),
    'info': ('Input.Info', None),
    'context': ('Input.ContextMenu', None),
    'osd': ('Input.ShowOSD', None),
    'codec': ('Input.ShowCodec', None),

    # -- playback ---------------------------------------------------------
    # These carry a player id, filled in when the button is pressed: there is
    # no "the player" in JSON-RPC, only whichever ones are active.
    'playpause': ('Player.PlayPause', 'player'),
    'stop': ('Player.Stop', 'player'),
    'next': ('Player.GoTo', 'next'),
    'previous': ('Player.GoTo', 'previous'),
    'forward': ('Player.SetSpeed', 'increment'),
    'rewind': ('Player.SetSpeed', 'decrement'),

    # -- sound ------------------------------------------------------------
    'volumeup': ('Application.SetVolume', 'increment'),
    'volumedown': ('Application.SetVolume', 'decrement'),
    # Not Application.SetMute: this drops to a level you can talk over
    # rather than silencing the room. See toggle_quiet.
    'mute': (None, 'quiet'),
}


def _rpc(method, params=None, quiet=False):
    """One JSON-RPC call. Returns the result dict, or None.

    Never raises: a button press that cannot be delivered should report
    itself, not take down the loop that delivers the next one.

    `quiet` is for a call that is expected to fail sometimes -- one spelling
    of Player.Seek being tried before another. A refusal there is not worth
    a line in the log.
    """
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method}
    if params is not None:
        request['params'] = params
    try:
        raw = xbmc.executeJSONRPC(json.dumps(request))
    except Exception as exc:
        xbmc.log('[Paragon TV] Web remote: %s failed: %s' % (method, exc),
                 xbmc.LOGERROR)
        return None
    try:
        answer = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if 'error' in answer:
        if not quiet:
            xbmc.log('[Paragon TV] Web remote: %s: %s'
                     % (method, answer['error']), xbmc.LOGERROR)
        return None
    return answer.get('result')


def active_player():
    """The id of whatever is playing, or None.

    JSON-RPC has no notion of "the" player -- it answers with a list, which is
    empty when nothing is on. Every playback button needs this first.
    """
    players = _rpc('Player.GetActivePlayers')
    if not isinstance(players, list) or not players:
        return None
    try:
        return int(players[0].get('playerid'))
    except (AttributeError, TypeError, ValueError, IndexError):
        return None


def press(button):
    """Press one of BUTTONS. Returns (ok, message).

    Called on the service loop's thread, like everything else that touches
    Kodi -- see the queue in WebRemote.
    """
    entry = BUTTONS.get(button)
    if entry is None:
        return False, 'Unknown button'
    method, shape = entry

    if shape is None:
        return (_rpc(method) is not None), ''

    if shape == 'quiet':
        return toggle_quiet()

    if shape == 'increment' and method == 'Application.SetVolume':
        return (_rpc(method, {'volume': 'increment'}) is not None), ''
    if shape == 'decrement' and method == 'Application.SetVolume':
        return (_rpc(method, {'volume': 'decrement'}) is not None), ''

    # Everything left is a player verb, and needs to know which player.
    player = active_player()
    if player is None:
        return False, 'Nothing is playing'

    if shape == 'player':
        return (_rpc(method, {'playerid': player}) is not None), ''
    if shape in ('next', 'previous'):
        return (_rpc(method, {'playerid': player, 'to': shape})
                is not None), ''
    if shape in ('increment', 'decrement'):
        return (_rpc(method, {'playerid': player, 'speed': shape})
                is not None), ''
    return False, 'Unknown button'


def seek(percent):
    """Move the current show to a point in itself. Returns (ok, message).

    The bar the page draws is the player's own position in the file it is
    playing, so a fraction of the bar is a fraction of the file, and this is
    the same seek any Kodi remote does.

    Two spellings, because they changed. Krypton speaks JSON-RPC 8, where the
    value is a bare number meaning a percentage; Kodi 18 and later want
    {"percentage": n}. This add-on is Krypton-only, so the number is tried
    first and the object is the fallback -- one line rather than a version
    check that would be wrong the moment the version moved.
    """
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return False, 'That is not a position'
    percent = max(0.0, min(100.0, percent))

    player = active_player()
    if player is None:
        return False, 'Nothing is playing'

    if _rpc('Player.Seek', {'playerid': player, 'value': percent},
            quiet=True) is not None:
        return True, ''
    if _rpc('Player.Seek',
            {'playerid': player, 'value': {'percentage': percent}}) is not None:
        return True, ''
    return False, 'Could not seek'


def db_to_percent(db, floor=None):
    """A decibel level as the percentage Kodi's volume is set in."""
    floor = VOLUME_FLOOR_DB if floor is None else float(floor)
    if floor >= 0:
        # A floor at or above zero leaves no range to map onto. Nonsense in,
        # full volume out, rather than a division by zero on a button press.
        return 100.0
    ratio = (float(db) - floor) / (0.0 - floor)
    return max(0.0, min(100.0, ratio * 100.0))


def percent_to_db(percent, floor=None):
    """The other way, for reading back where the volume actually is."""
    floor = VOLUME_FLOOR_DB if floor is None else float(floor)
    if floor >= 0:
        return 0.0
    ratio = max(0.0, min(100.0, float(percent))) / 100.0
    return floor + ratio * (0.0 - floor)


def _setting_float(name, fallback):
    """One of this add-on's own settings as a number, or the fallback.

    Read on each press rather than cached: changing a level in settings
    should take effect on the next press, not the next restart.
    """
    try:
        raw = xbmcaddon.Addon().getSetting(name)
    except Exception:
        return fallback
    try:
        return float((raw or '').strip())
    except (TypeError, ValueError):
        return fallback


def levels():
    """(quiet, normal) in dB, from settings, falling back to the defaults."""
    return (_setting_float('tv_quiet_db', QUIET_LEVEL_DB),
            _setting_float('tv_normal_db', NORMAL_LEVEL_DB))


def volume_percent():
    """Where the volume is now, as a percentage, or None."""
    app = _rpc('Application.GetProperties', {'properties': ['volume']})
    if not isinstance(app, dict):
        return None
    value = app.get('volume')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def set_volume_percent(percent):
    """Set the volume, keeping the fraction.

    The builtin rather than Application.SetVolume, because the JSON-RPC
    method takes an integer and these levels do not land on whole
    percentages: -39.3 dB is 34.5%, and 34 or 35 is a different level.
    """
    xbmc.executebuiltin('SetVolume(%.2f)' % max(0.0, min(100.0, percent)))


def at_quiet_level(percent=None):
    """Whether the volume is sitting at the quiet level.

    What lights the button. Read from the volume itself rather than from a
    flag this module set, so it stays right when the level is changed by the
    volume keys, a Kodi remote or anything else.
    """
    if percent is None:
        percent = volume_percent()
    if percent is None:
        return False
    quiet, _normal = levels()
    return abs(percent_to_db(percent) - quiet) <= LEVEL_TOLERANCE_DB


def toggle_quiet():
    """The mute button: down to the quiet level, or back up to the normal one.

    Stateless on purpose. Remembering which way it went last would be wrong
    the moment the volume moved by any other route, and a button that needs
    pressing twice because it thinks it is somewhere it is not is worse than
    one that simply looks first.

    Kodi's own mute is cleared on the way, so a television muted from the
    Kodi remote comes back rather than silently staying off at a new level.
    """
    quiet, normal = levels()
    percent = volume_percent()
    if percent is None:
        return False, 'Could not read the volume'

    here = percent_to_db(percent)
    # The midpoint decides, so anywhere down at the quiet end comes back up
    # and anywhere above it goes down -- including levels neither button set.
    target = normal if here <= (quiet + normal) / 2.0 else quiet

    _rpc('Application.SetMute', {'mute': False}, quiet=True)
    set_volume_percent(db_to_percent(target))
    return True, '%.1f dB' % target


def player_state():
    """Volume, mute and what the player is doing. Never raises.

    What the page needs to draw the remote honestly -- a play button that
    knows it is a pause button, a mute button that knows it is muted.
    """
    state = {'playing': False, 'paused': False, 'speed': 0,
             'volume': None, 'muted': False, 'quiet': False}

    # Shape-checked, not assumed. This is one call inside the snapshot every
    # poll builds, and an answer that is not the dict it should be would
    # otherwise take the channel list down with it -- which is how this was
    # found: the whole page went blank because of the volume.
    app = _rpc('Application.GetProperties',
               {'properties': ['volume', 'muted']})
    if not isinstance(app, dict):
        app = {}
    if 'volume' in app:
        state['volume'] = app.get('volume')
        state['quiet'] = at_quiet_level(app.get('volume'))
    state['muted'] = bool(app.get('muted'))

    player = active_player()
    if player is None:
        return state

    props = _rpc('Player.GetProperties',
                 {'playerid': player, 'properties': ['speed']})
    if not isinstance(props, dict):
        props = {}
    speed = props.get('speed') or 0
    state['playing'] = True
    state['speed'] = speed
    state['paused'] = speed == 0
    return state


# ---------------------------------------------------------------------------
# Typing into a box on the television
# ---------------------------------------------------------------------------

# Kodi's own dialog ids. The keyboard is 10103 and the number pad is 10109 on
# Krypton, and have been since long before it.
KEYBOARD_DIALOG = 10103
NUMERIC_DIALOG = 10109

# The same two asked for by name. Both are checked, because an id is a number
# that could change under us and a name is a string that could be spelled
# wrong, and the two fail in different directions.
DIALOG_NAMES = (('keyboard', 'Window.IsVisible(virtualkeyboard)'),
                ('numeric', 'Window.IsVisible(numericinput)'))

# How many characters may be sent in one go. A search box is a line of text;
# this is loose enough never to cut a real one short.
MAX_TEXT = 2048

_seen_dialog = [None]


def text_input():
    """Whether the television is waiting for something to be typed.

    Returns {'open': bool, 'kind': 'keyboard'|'numeric'|''}.

    Two ways of asking, on purpose. The id is what Kodi hands back directly;
    the name is what its own condition language calls the same window. If the
    ids ever move, the names still answer -- and if a dialog turns up that
    neither recognises, it is named in the log once, so working out what it
    was is a look at the log rather than another guess.
    """
    try:
        current = int(xbmcgui.getCurrentWindowDialogId())
    except Exception:
        current = 0

    if current == KEYBOARD_DIALOG:
        return {'open': True, 'kind': 'keyboard'}
    if current == NUMERIC_DIALOG:
        return {'open': True, 'kind': 'numeric'}

    for kind, condition in DIALOG_NAMES:
        try:
            if xbmc.getCondVisibility(condition):
                return {'open': True, 'kind': kind}
        except Exception:
            pass

    # Nothing we know. Say so once per dialog rather than on every poll.
    # WINDOW_INVALID is 9999 and means no dialog at all, which is the normal
    # case and not worth a word.
    if current and current != 9999 and _seen_dialog[0] != current:
        _seen_dialog[0] = current
        xbmc.log('[Paragon TV] Web remote: dialog %s is open and is not one '
                 'the remote types into' % current, xbmc.LOGDEBUG)
    return {'open': False, 'kind': ''}


def send_text(text, done=True):
    """Type `text` into whatever is asking for it. Returns (ok, message).

    Input.SendText is how every Kodi remote does this -- it puts the text in
    and, with done, closes the box as though OK had been pressed.
    """
    if not isinstance(text, string_types):
        return False, 'That is not text'
    text = text[:MAX_TEXT]

    if not text_input()['open']:
        return False, 'Nothing on the television is asking for text'

    result = _rpc('Input.SendText', {'text': text, 'done': bool(done)})
    if result is None:
        return False, 'Could not send that'
    return True, 'Sent'


# ---------------------------------------------------------------------------
# The maintenance jobs from Settings -> Utility Tools
# ---------------------------------------------------------------------------

# What each job is, and how Paragon TV itself runs it.
#
# Taken from resources/settings.xml rather than invented: these are the same
# entries as Settings -> Utility Tools, with the same labels, so what the
# remote offers and what the television offers cannot drift into meaning
# different things. A script is named by its file name only -- the folder
# comes from the add-on -- so nothing a caller sends is ever part of a path.
#
# The order is the order they are useful in: rename the files, then let Kodi
# read them, then send the result to the satellites.
# The last field says where the entry came from. Most are the utility menu's
# own, and a test reads settings.xml back to hold them to it; the two at the
# end are the remote's, asked for because walking to the box to reload a skin
# rather defeats the point of a remote.
FROM_MENU = 'menu'
FROM_REMOTE = 'remote'

TASKS = (
    ('bumpers', 'Rename bumpers', 'script', 'nfo_renamer_bumpers.py',
     FROM_MENU),
    ('movies', 'Rename movies', 'script', 'nfo_renamer_movies.py', FROM_MENU),
    ('shows', 'Rename TV shows', 'script', 'nfo_renamer_television.py',
     FROM_MENU),
    ('update', 'Update video library', 'builtin', 'UpdateLibrary(video)',
     FROM_MENU),
    ('clean', 'Clean video library', 'builtin', 'CleanLibrary(video)',
     FROM_MENU),
    ('push', 'Push to satellites', 'script', 'ptv_push_to_satellites.py',
     FROM_MENU),
    ('align', 'Satellite alignment', 'script', 'ptv_satellite_alignment.py',
     FROM_MENU),
    ('skin', 'Reload skin', 'builtin', 'ReloadSkin()', FROM_REMOTE),
    ('reboot', 'Reboot the box', 'builtin', 'Reboot', FROM_REMOTE),
)

# Jobs that want asking twice. Reloading a skin is a blink; rebooting is the
# whole machine, and a wall tablet is a thing people brush past.
CONFIRM = ('reboot',)

TASKS_BY_NAME = dict((entry[0], entry) for entry in TASKS)


def task_list():
    """The jobs, as the page wants them: a name and a label, nothing else.

    Deliberately not the script names. The page has no business knowing what
    runs, and telling it would be handing out half of a path.
    """
    return [{'name': name, 'label': label, 'confirm': name in CONFIRM}
            for name, label, _kind, _what, _origin in TASKS]


def run_task(name):
    """Start one of TASKS. Returns (ok, message).

    Refused while Paragon TV is running, and that is not only the page being
    tidy. These rewrite the NFO files the channels are built from, make Kodi
    re-read the library underneath them, and copy the lot to other boxes. Run
    against a television that is playing, the mildest outcome is a channel
    losing its place. The page hides the section, and this refuses anyway --
    a page left open on a tablet since this morning does not know yet.
    """
    entry = TASKS_BY_NAME.get(name)
    if entry is None:
        return False, 'Unknown job'
    if current_channel() is not None:
        return False, 'Stop Paragon TV first'

    _name, label, kind, what, _origin = entry
    if kind == 'builtin':
        command = what
    else:
        here = addon_path()
        if not here:
            return False, 'Paragon TV is not installed'
        command = 'RunScript(%s)' % os.path.join(here, 'resources', 'lib',
                                                 what)

    try:
        xbmc.executebuiltin(command)
    except Exception as exc:
        xbmc.log('[Paragon TV] Web remote: %s failed: %s' % (label, exc),
                 xbmc.LOGERROR)
        return False, 'Could not start %s' % label
    if name == 'reboot':
        return True, 'Rebooting'
    if name == 'skin':
        return True, 'Reloading the skin'
    # Started, not finished. These run for minutes and say so on the
    # television; the remote has no way to watch them and does not pretend to.
    return True, '%s started' % label


def send_action(name):
    """Ask Kodi to deliver an action, the way a remote control would.

    Through executebuiltin because that runs on Kodi's own application
    thread, which is where an action has to be handled. Called from a request
    handler's thread it would be reaching into a window Kodi is rendering.
    """
    xbmc.executebuiltin('Action(%s)' % name)


def channel_up():
    send_action(ACTION_CHANNEL_UP)
    return True, 'Channel up'


def channel_down():
    send_action(ACTION_CHANNEL_DOWN)
    return True, 'Channel down'


def tune(number):
    """Go to a channel by typing its number, exactly as you would by hand.

    The digits go in one at a time and a select commits them, because that is
    what Paragon TV's own handlers expect: handleNumberAction gathers them
    and handleSelectAction acts on them.
    """
    try:
        number = int(number)
    except (TypeError, ValueError):
        return False, 'That is not a channel number'
    if number < 1 or number > MAX_CHANNELS:
        return False, 'There is no channel %s' % number
    if current_channel() is None:
        return False, 'Paragon TV is not running'

    for digit in str(number):
        send_action('number%s' % digit)
        xbmc.sleep(int(KEY_PAUSE * 1000))
    send_action(ACTION_SELECT)
    return True, 'Channel %d' % number


def snapshot():
    """The television half of what the page draws itself from.

    `installed` false and nothing else when Paragon TV is not on this box.
    """
    if not installed():
        return {'ready': False, 'installed': False}

    on_now = current_channel()
    found = channels()
    by_number = dict((entry['number'], entry) for entry in found)

    # What is playing on each, where the Overlay has said so.
    playing = read_now_on()
    for entry in found:
        showing = playing.get(entry['number'])
        entry['showing'] = showing.get('title', '') if showing else ''
        entry['episode'] = showing.get('episode', '') if showing else ''
        entry['elapsed'] = showing.get('elapsed', 0) if showing else 0
        entry['duration'] = showing.get('duration', 0) if showing else 0
        # Whether, not where: the page asks for a channel's logo by channel
        # number, and never gets to name a file.
        entry['logo'] = bool(channel_logo(entry['name']))

    art = now_art(by_number.get(on_now, {}).get('name', '') if on_now else '')

    return {
        'ready': True,
        # Whether this box has Paragon TV at all. The page draws no
        # television half without it.
        'installed': True,
        # What the remote needs to draw itself honestly: a play button that
        # knows it is a pause button, a mute button that knows it is muted.
        'player': player_state(),
        'art': bool(art),
        'art_key': art_key(art),
        'running': on_now is not None,
        'channel': on_now,
        'channel_name': (by_number.get(on_now, {}).get('name', '')
                         if on_now else ''),
        'browsing': _home_property(PROP_BROWSING) == 'true',
        # Only offered with the television off -- see run_task.
        'tasks': task_list(),
        # Whether a box on the television is waiting to be typed into.
        'input': text_input(),
        'channels': found,
        'now': now_playing(),
    }


# ---------------------------------------------------------------------------
# The one thing this stage can do
# ---------------------------------------------------------------------------

def start_paragon_tv():
    """Ask Kodi to launch Paragon TV. Returns (ok, message).

    Through executebuiltin rather than by calling into the add-on: the builtin
    is queued onto Kodi's own application thread, which is where a window has
    to be created. Called from a request handler's thread it would be creating
    a Kodi window from somewhere Kodi does not expect one.
    """
    if current_channel() is not None:
        return False, 'Paragon TV is already running'
    try:
        xbmc.executebuiltin('RunScript(%s)' % ADDON_ID)
    except Exception as exc:
        return False, str(exc)
    return True, 'Starting Paragon TV'
