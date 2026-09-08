# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Folder copying between boxes, over Kodi's virtual filesystem.

This is the half of the directory speed dial that touches files. places.py
decides what a slot points at; this module walks it, copies it and reports
what it found on the other end.

Everything goes through xbmcvfs rather than shutil, because a destination is
usually smb://box/share/... and os.path cannot see that at all. xbmcvfs.copy
handles one file, so the recursion here is hand-written -- there is no
recursive copy in the Kodi API to call.

Copy over, then report
----------------------
A sync overwrites what matches and *never* deletes. Files on the destination
that the source does not have are gathered up and handed back by extras(),
for the caller to show and for the user to confirm separately, and deleting
them is a second explicit call to remove().

That split is the whole safety design. A mirror that deletes as it goes is
one wrong direction away from destroying the destination, and the direction
is the easy thing to get wrong on a two-pane screen. Nothing in a sync is
irreversible; the irreversible part is a separate decision made against a
list the user can actually read.

The guards
----------
Three things are refused rather than handled, because each one is a way for a
copy to do something the user cannot have meant:

  * A destination inside its own source (or the reverse). Copying
    addons/ into addons/skin.paragon/ grows the source as it walks it and
    does not terminate.
  * A walk deeper than MAX_DEPTH. xbmcvfs does not report symlinks, so a
    loop on the source is indistinguishable from a very deep tree until it
    has run for ever.
  * A tree larger than MAX_FILES. Not a real limit for a skin or an add-on;
    it is there so that a slot pointed at / by accident stops instead of
    trying to copy the whole box over the network.
"""

import xbmcvfs

from compat import to_text
from places import clean_rel, join

# Deep enough for anything in a Kodi tree, shallow enough that a symlink loop
# gives up in a moment. skin.paragon's deepest path is about six.
MAX_DEPTH = 24

# A skin is a few hundred files. Five figures means a slot is pointed
# somewhere it should not be.
MAX_FILES = 20000

# Never copied, whatever a slot points at.
#
# .git is the one that matters. These add-ons are installed as git clones and
# updated with `git pull`, so copying one box's .git onto another does not
# merely waste time on a few thousand small files -- it repoints that clone at
# the source's HEAD, refs and branch, and the next pull there is against a
# history the box was never on. The folder that keeps the destination
# updatable is the one folder a copy must not touch.
#
# The compiled Python goes for a smaller reason: Kodi regenerates it, a copied
# .py lands with a current timestamp so the stale .pyo beside it is recompiled
# anyway, and they are the files this project untracked from git for the same
# reason.
SKIP_FOLDERS = ('.git', '.svn', '__pycache__')
SKIP_SUFFIXES = ('.pyo', '.pyc')


def is_skipped_folder(name):
    return name in SKIP_FOLDERS


def is_skipped_file(name):
    return name.endswith(SKIP_SUFFIXES)


# What a copy reports back. `failed` holds (rel, reason) rather than raising,
# because one unreadable file out of six hundred should leave the other five
# hundred and ninety-nine copied and say so.
COPIED = 'copied'
CANCELLED = 'cancelled'
REFUSED = 'refused'


class TransferError(Exception):
    """A copy could not be started at all."""


class Survey(object):
    """What is in a tree, gathered before anything is written.

    Separate from the copy so the confirmation can say "689 files, 41 MB"
    before the user commits to it, and so the progress bar has a total to
    count against rather than crawling towards an unknown end.
    """

    def __init__(self):
        self.files = []      # (rel, size)
        self.dirs = []       # rel, parents before children
        self.total_bytes = 0
        self.errors = []
        self.stopped = False  # hit a cap or was cancelled part-way
        # Files and folders passed over as SKIP_FOLDERS/SKIP_SUFFIXES.
        # Counted rather than merely dropped so the confirmation can say a
        # copy is leaving something behind instead of quietly doing it.
        self.skipped = 0

    @property
    def count(self):
        return len(self.files)

    def rels(self):
        """Just the file paths, as a set, for comparing two trees."""
        return set(rel for rel, _size in self.files)


class Result(object):
    """The outcome of a copy or a delete."""

    def __init__(self, status=COPIED):
        self.status = status
        self.done = 0
        self.failed = []     # (rel, reason)
        self.reason = ''

    @property
    def ok(self):
        return self.status == COPIED and not self.failed


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------

def _listdir(path):
    """xbmcvfs.listdir as (dirs, files) of text, tolerating a bad path.

    Kodi hands these back as byte strings on Python 2 and the names go into
    paths and onto the screen, so they are decoded once here rather than at
    every use.
    """
    try:
        dirs, files = xbmcvfs.listdir(path)
    except (IOError, OSError, ValueError) as exc:
        raise TransferError('%s' % exc)
    return ([to_text(name) for name in (dirs or [])],
            [to_text(name) for name in (files or [])])


def _size_of(path):
    try:
        return int(xbmcvfs.Stat(path).st_size())
    except (IOError, OSError, ValueError, AttributeError):
        return 0


def survey(root, should_cancel=None):
    """Walk `root`, returning a Survey of everything under it.

    Breadth-first with an explicit queue rather than recursion: the depth cap
    is then a number on the queue entry instead of a Python recursion limit
    that would raise somewhere unhelpful.
    """
    found = Survey()
    if not root:
        raise TransferError('No folder to copy from')
    if not xbmcvfs.exists(root):
        raise TransferError('%s is not there' % root)

    queue = [('', 0)]
    while queue:
        if should_cancel is not None and should_cancel():
            found.stopped = True
            return found

        rel, depth = queue.pop(0)
        here = join(root, rel)
        if here is None:
            found.errors.append('%s could not be read' % rel)
            continue

        try:
            dirs, files = _listdir(here)
        except TransferError as exc:
            found.errors.append('%s: %s' % (rel or '.', exc))
            continue

        for name in files:
            if is_skipped_file(name):
                found.skipped += 1
                continue
            child = ('%s/%s' % (rel, name)) if rel else name
            if clean_rel(child) is None:
                # A name the filesystem allows but a relative path cannot
                # hold. Skipped rather than copied to a repaired path.
                found.errors.append('%s has an unusable name' % child)
                continue
            found.files.append((child, _size_of(here + name)))
            found.total_bytes += found.files[-1][1]
            if len(found.files) >= MAX_FILES:
                found.errors.append(
                    'Stopped at %d files -- this folder is far larger than '
                    'anything meant to be copied this way' % MAX_FILES)
                found.stopped = True
                return found

        if depth >= MAX_DEPTH:
            if dirs:
                found.errors.append('%s is nested deeper than %d folders'
                                    % (rel or '.', MAX_DEPTH))
            continue

        for name in dirs:
            if is_skipped_folder(name):
                found.skipped += 1
                continue
            child = ('%s/%s' % (rel, name)) if rel else name
            if clean_rel(child) is None:
                found.errors.append('%s has an unusable name' % child)
                continue
            found.dirs.append(child)
            queue.append((child, depth + 1))

    return found


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def reachable(path):
    """Whether Kodi can see `path` right now.

    For telling someone their box's base is wrong at the moment they set it,
    rather than at the moment a copy fails. Not a reason to refuse the
    setting: a box that is switched off is unreachable and its base is still
    correct.
    """
    if not path:
        return False
    try:
        return bool(xbmcvfs.exists(path))
    except (IOError, OSError, ValueError):
        return False


def folders(path):
    """The folder names directly under `path`, sorted. Empty if unreadable.

    For offering a slot's choices rather than asking the user to type one. An
    unreachable box gives an empty list rather than an error: the menu can
    still offer to type a name, which is the only useful thing to do when the
    box is off.
    """
    if not path:
        return []
    try:
        dirs, _files = _listdir(path)
    except TransferError:
        return []
    return sorted(dirs)


def overlaps(source, dest):
    """Whether one of these paths contains the other.

    Compared as trailing-slashed strings so that a shared prefix only counts
    at a folder boundary: addons/skin.paragon2/ does not contain, and is not
    contained by, addons/skin.paragon/.
    """
    if not source or not dest:
        return False
    left = source if source.endswith('/') else source + '/'
    right = dest if dest.endswith('/') else dest + '/'
    return left.startswith(right) or right.startswith(left)


def check(source, dest):
    """Raise TransferError unless this copy is safe to attempt."""
    if not source or not dest:
        raise TransferError('Both ends of a copy have to be set')
    if overlaps(source, dest):
        raise TransferError('One of these folders is inside the other, so '
                            'copying between them would not terminate')
    if not xbmcvfs.exists(source):
        raise TransferError('%s is not there' % source)


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------

def copy_tree(source, dest, plan=None, on_progress=None, should_cancel=None):
    """Copy everything under `source` into `dest`, overwriting, never deleting.

    `plan` is a Survey from survey(); one is taken if not supplied. Passing
    the same Survey that was shown in the confirmation means the copy does
    exactly what the user was told it would, rather than re-walking and
    possibly finding something new.

    Folders are created before the files that go in them, and a folder that
    cannot be created is recorded once rather than once per file inside it.
    """
    check(source, dest)
    if plan is None:
        plan = survey(source, should_cancel=should_cancel)
        if plan.stopped:
            result = Result(CANCELLED)
            result.reason = 'Stopped while looking at %s' % source
            return result

    result = Result()

    if not xbmcvfs.exists(dest):
        if not _mkdirs(dest):
            raise TransferError('Could not create %s' % dest)

    unusable = set()
    for rel in plan.dirs:
        target = join(dest, rel)
        if target is None or not _mkdirs(target):
            unusable.add(rel)
            result.failed.append((rel, 'could not create the folder'))

    total = plan.count
    for index, (rel, _size) in enumerate(plan.files):
        if should_cancel is not None and should_cancel():
            result.status = CANCELLED
            return result

        parent = rel.rsplit('/', 1)[0] if '/' in rel else ''
        if parent and parent in unusable:
            continue

        left = join(source, rel)
        right = join(dest, rel)
        # join() trailing-slashes a directory; these are files, so the slash
        # comes back off. Going through join() anyway is what keeps the ".."
        # check on the path actually used.
        if left is None or right is None:
            result.failed.append((rel, 'unusable name'))
            continue
        left, right = left.rstrip('/'), right.rstrip('/')

        try:
            if xbmcvfs.copy(left, right):
                result.done += 1
            else:
                result.failed.append((rel, 'refused by the filesystem'))
        except (IOError, OSError, ValueError) as exc:
            result.failed.append((rel, '%s' % exc))

        if on_progress is not None:
            on_progress(index + 1, total, rel)

    return result


def _mkdirs(path):
    """Create `path` and its parents. True if it exists afterwards."""
    if xbmcvfs.exists(path):
        return True
    try:
        xbmcvfs.mkdirs(path)
    except (IOError, OSError, ValueError):
        return False
    return xbmcvfs.exists(path)


# ---------------------------------------------------------------------------
# The report, and the separate delete
# ---------------------------------------------------------------------------

def extras(source, dest, should_cancel=None):
    """Files on `dest` that `source` does not have, as a sorted list of rels.

    This is the "then report" half. It is a read -- nothing is removed here
    and nothing about the result commits the user to removing it.

    An unreadable destination gives an empty list rather than an error: the
    copy has already happened by this point, and failing here would report a
    successful sync as a failure.
    """
    if not dest or not xbmcvfs.exists(dest):
        return []
    try:
        here = survey(dest, should_cancel=should_cancel)
        theirs = survey(source, should_cancel=should_cancel)
    except TransferError:
        return []
    if here.stopped or theirs.stopped:
        return []
    return sorted(here.rels() - theirs.rels())


def remove(dest, rels, on_progress=None, should_cancel=None):
    """Delete the named files from under `dest`. Files only, never folders.

    Every rel is re-checked with clean_rel and the path rebuilt from `dest`,
    exactly as places.py does -- the list handed in came from extras(), but
    this function deletes, and a delete should not trust a path just because
    something upstream said it was fine.

    Empty folders left behind are not removed. Leaving a stray directory is
    harmless; recursing to clean them up would mean this function walking and
    deleting folders it was not handed, which is not what the user confirmed.
    """
    result = Result()
    total = len(rels or [])
    for index, rel in enumerate(rels or []):
        if should_cancel is not None and should_cancel():
            result.status = CANCELLED
            return result

        safe = clean_rel(rel)
        if not safe:
            result.failed.append((rel, 'not a path under this folder'))
            continue
        target = join(dest, safe)
        if target is None:
            result.failed.append((rel, 'not a path under this folder'))
            continue
        target = target.rstrip('/')

        try:
            if xbmcvfs.delete(target):
                result.done += 1
            else:
                result.failed.append((rel, 'refused by the filesystem'))
        except (IOError, OSError, ValueError) as exc:
            result.failed.append((rel, '%s' % exc))

        if on_progress is not None:
            on_progress(index + 1, total, rel)

    return result


# ---------------------------------------------------------------------------
# Describing, for the confirmation
# ---------------------------------------------------------------------------

def describe_size(total):
    """A byte count as something readable on a television."""
    try:
        value = float(total)
    except (TypeError, ValueError):
        return '0 B'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024.0 or unit == 'GB':
            return ('%d %s' % (value, unit) if unit == 'B'
                    else '%.1f %s' % (value, unit))
        value /= 1024.0
    return '%.1f GB' % value


def describe(plan):
    """"689 files, 41.2 MB" for a surveyed tree.

    Says so when something was passed over. A copy that silently leaves .git
    behind is the right copy, but it should not be a silent one -- the whole
    confirmation exists so that what is about to happen is on the screen.
    """
    if plan is None:
        return 'nothing'
    files = '%d file%s' % (plan.count, '' if plan.count == 1 else 's')
    described = '%s, %s' % (files, describe_size(plan.total_bytes))
    if plan.skipped:
        described += ' (%d skipped)' % plan.skipped
    return described
