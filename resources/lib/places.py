# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The directory speed dial.

The third speed dial in Paragon, after Paragon TV's channels and palette.py's
colours, and it works the way the other two do: a short numbered list, the
number jumps straight there, and the list lives as plain JSON in the profile
so it can be hand-edited and copied between boxes.

What it is for is moving folders between Kodi boxes -- pushing a new
skin.paragon to the three boxes in the house rather than scp-ing it three
times. So the slots are paired by number across hosts: slot 1 here and slot 1
on the office box are the two ends of one copy, which is what makes "sync 1"
and "sync 1 everywhere" possible at all.

A host is a base, a slot is a path relative to it
-------------------------------------------------
A slot does not hold a whole path. It holds a `rel` -- a relative path -- and
the host it belongs to holds the `base` that rel hangs off. The full path is
only ever assembled by join(), from a base this add-on holds and a rel that
has been through clean_rel().

That split is doing three jobs:

  * It is the same guard remote.py documents for its static routes. A rel is
    checked for "..", for a leading slash and for a drive letter, and the
    path is then *rebuilt* rather than sanitised in place. A slot that could
    hold "../../../storage/.kodi/userdata" would make a folder copy able to
    reach anywhere on the box, and one of these copies overwrites its
    destination.
  * It makes pairing fall out for free. Slot 1 is usually the same rel on
    every host -- "skin.paragon" under one base here and another there -- so
    copying a slot to every box is copying one rel, not editing four paths.
  * It keeps credentials in one place. If a base ever needs a user and
    password in its URL, they sit on the host and not smeared across slots.

Empty rel is legal and means the base itself. An unconfigured slot is None,
which is a different thing and is why the two are not both spelled "".
"""

# Four, because that is a row of buttons you can hit without looking -- the
# same reason Paragon TV's speed dial stops where it does. Kept as a name
# because the pairing logic reads better against it than against a literal.
SLOT_COUNT = 4

PLACES_FILE = 'places.json'

# The id the local box always has. Reserved: a host read off disk claiming to
# be "local" is folded into the real one rather than added beside it, so there
# is exactly one "this box" no matter what the file says.
LOCAL_ID = 'local'

# Where a fresh install points the local host. special:// is Kodi's own
# indirection and resolves on any platform, which a hardcoded /storage path
# would not.
DEFAULT_LOCAL_BASE = 'special://home/addons/'


def default_places():
    """The starter set: this box, pointed at its add-ons, with empty slots."""
    return [{
        'id': LOCAL_ID,
        'name': 'This box',
        'base': DEFAULT_LOCAL_BASE,
        'slots': [None] * SLOT_COUNT,
    }]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def clean_rel(rel):
    """A relative path safe to hang off a base, or None if it is not one.

    Returns a forward-slashed path with no leading or trailing slash. The
    empty string is a valid answer and means "the base itself"; None means
    the input could not be made into a relative path at all.

    Refused rather than repaired: anything with a ".." segment, anything
    absolute, and anything with a drive letter. Repairing a traversal by
    stripping the ".." quietly turns a path the user cannot have meant into a
    path that works, and the thing on the other end of this is a recursive
    copy that overwrites what it lands on.
    """
    if rel is None:
        return None
    if not hasattr(rel, 'strip'):
        return None

    # Backslashes first: a Windows-shaped path is a reasonable thing to type
    # and "skin.paragon\\media" should not read as one long segment.
    text = rel.strip().replace('\\', '/')

    # A drive letter makes it absolute on Windows even without a leading
    # slash, so it is checked before the leading-slash test rather than after.
    if len(text) >= 2 and text[1] == ':':
        return None
    if text.startswith('/'):
        return None
    # A scheme means a whole URL was handed in where a relative path belongs.
    if '://' in text:
        return None

    parts = []
    for segment in text.split('/'):
        segment = segment.strip()
        if not segment or segment == '.':
            continue
        if segment == '..':
            return None
        parts.append(segment)
    return '/'.join(parts)


def clean_base(base):
    """A base directory, trailing-slashed, or None if it is unusable.

    Kodi's VFS wants a trailing slash on a directory and is inconsistent about
    adding one itself, so it is added here once rather than at each call site.
    """
    if base is None or not hasattr(base, 'strip'):
        return None
    text = base.strip().replace('\\', '/')
    if not text:
        return None
    if not text.endswith('/'):
        text += '/'
    return text


def join(base, rel):
    """Assemble the full path for `rel` under `base`, or None.

    Deliberately not os.path.join: on Windows that joins with a backslash,
    which would corrupt an smb:// URL, and these paths are VFS URLs as often
    as they are local paths.
    """
    base = clean_base(base)
    if base is None:
        return None
    rel = clean_rel(rel)
    if rel is None:
        return None
    if not rel:
        return base
    return base + rel + '/'


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def normalise_slot(entry):
    """One slot as it comes off disk, or None if it is empty or unsalvageable.

    None is the ordinary answer for an unconfigured slot, so a file with three
    slots set and one blank loads without complaint.
    """
    if not isinstance(entry, dict):
        return None

    rel = clean_rel(entry.get('rel'))
    if rel is None:
        return None

    name = entry.get('name')
    if name and hasattr(name, 'strip'):
        name = name.strip()
    else:
        name = ''
    if not name:
        # Fall back to the last segment, which is the folder's own name and
        # is what the user would have typed anyway. An empty rel points at the
        # base, so it is labelled for that rather than left blank.
        name = rel.rsplit('/', 1)[-1] if rel else 'Top level'

    return {'name': name, 'rel': rel}


def normalise_slots(raw):
    """Exactly SLOT_COUNT slots, padded and truncated as needed.

    The list is always full length so that slot N is always index N-1 -- the
    pairing is positional, and a short list read off disk would silently
    renumber every slot after the gap.
    """
    entries = list(raw) if isinstance(raw, list) else []
    slots = [normalise_slot(entry) for entry in entries[:SLOT_COUNT]]
    while len(slots) < SLOT_COUNT:
        slots.append(None)
    return slots


def normalise_host(entry):
    """One host as it comes off disk, or None if it cannot be salvaged."""
    if not isinstance(entry, dict):
        return None

    host_id = entry.get('id')
    if not host_id or not hasattr(host_id, 'strip'):
        return None
    host_id = host_id.strip().lower()
    if not host_id:
        return None

    base = clean_base(entry.get('base'))
    if base is None:
        return None

    name = entry.get('name')
    if name and hasattr(name, 'strip'):
        name = name.strip()
    else:
        name = ''

    return {
        'id': host_id,
        'name': name or host_id,
        'base': base,
        'slots': normalise_slots(entry.get('slots')),
    }


def normalise_all(raw):
    """Clean a loaded place list, dropping junk and duplicate ids.

    The local host is guaranteed present and first. Everything else keeps the
    order it was saved in, because that is the order of the host picker.
    """
    hosts = []
    seen = set()
    for entry in (raw if isinstance(raw, list) else []):
        host = normalise_host(entry)
        if host is None or host['id'] in seen:
            continue
        seen.add(host['id'])
        hosts.append(host)

    if LOCAL_ID not in seen:
        hosts.insert(0, default_places()[0])
    else:
        # Keep "this box" at the head of the picker wherever it was saved.
        local = [h for h in hosts if h['id'] == LOCAL_ID]
        hosts = local + [h for h in hosts if h['id'] != LOCAL_ID]
    return hosts


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def find_host(hosts, host_id):
    """Look a host up by id, case-insensitively. None if absent."""
    if not host_id or not hasattr(host_id, 'strip'):
        return None
    wanted = host_id.strip().lower()
    for host in hosts or []:
        if host.get('id') == wanted:
            return host
    return None


def slot(host, index):
    """Slot `index` (0-based) of `host`, or None if empty or out of range."""
    if not host:
        return None
    slots = host.get('slots') or []
    if index < 0 or index >= len(slots):
        return None
    return slots[index]


def slot_path(host, index):
    """The full path slot `index` points at, or None if it points nowhere."""
    entry = slot(host, index)
    if entry is None:
        return None
    return join(host.get('base'), entry.get('rel'))


def set_slot(host, index, rel, name=None):
    """Point slot `index` at `rel`. Returns True if it took.

    `rel` of None clears the slot, which is how a slot is removed -- the list
    stays SLOT_COUNT long so the numbering never shifts under the pairing.
    """
    if not host or index < 0 or index >= SLOT_COUNT:
        return False
    slots = host.setdefault('slots', [None] * SLOT_COUNT)
    while len(slots) < SLOT_COUNT:
        slots.append(None)

    if rel is None:
        slots[index] = None
        return True

    entry = normalise_slot({'rel': rel, 'name': name})
    if entry is None:
        return False
    slots[index] = entry
    return True


def pairs(hosts, index, source_id):
    """Every (source, destination) this slot number could copy between.

    The source host paired with each *other* host that has the same slot
    filled in. This is what "sync slot 1 everywhere" is built on, and it is
    also how a single sync is described -- one pair rather than a special
    case.

    A host whose slot `index` is empty is left out rather than guessed at: a
    destination that has not been set is not the same as a destination that
    happens to match the source's rel, and copying into a guess would put
    files somewhere the user never named.
    """
    source = find_host(hosts, source_id)
    if source is None or slot(source, index) is None:
        return []
    found = []
    for host in hosts or []:
        if host.get('id') == source.get('id'):
            continue
        if slot(host, index) is None:
            continue
        found.append((source, host))
    return found
