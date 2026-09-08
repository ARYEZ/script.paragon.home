# -*- coding: utf-8 -*-
"""Enough of Kodi's virtual filesystem to test against.

The point of this stub is the thing that broke on hardware: Kodi paths are
not always filesystem paths. Media on a NAS arrives as `smb://box/share/...`,
and Python's own os.path cannot see it at all. So this stub understands a
fake `smb://` scheme, mapped onto a real directory, which lets a test say
"the artwork is on a share" and mean it.
"""

import os
import shutil

# Set by a test to stand for one box: each directory directly inside it is a
# share, so smb://box/ lists the shares and smb://box/Share/a resolves to
# <SMB_ROOT>/Share/a. Modelled this way because listing the shares on a host
# is a real thing the add-on does -- it is how a box's base is chosen.
SMB_ROOT = None


def _real(path):
    """The filesystem path behind a Kodi path, or None if there isn't one."""
    if path is None:
        return None
    if path.startswith('smb://'):
        if SMB_ROOT is None:
            return None
        rest = path[len('smb://'):]
        # Drop the host. What is left is the share and the path inside it:
        # smb://host/Share/a/b -> <SMB_ROOT>/Share/a/b, and smb://host/ ->
        # <SMB_ROOT>, which lists the shares.
        parts = [part for part in rest.split('/')[1:] if part]
        return os.path.join(SMB_ROOT, *parts) if parts else SMB_ROOT
    if '://' in path:
        return None
    return path


def exists(path):
    real = _real(path)
    return bool(real) and os.path.exists(real)


def delete(path):
    real = _real(path)
    if real and os.path.isfile(real):
        os.remove(real)
        return True
    return False


def copy(source, target):
    left, right = _real(source), _real(target)
    if not left or not right:
        return False
    # Faithful to Kodi: copying into a folder that does not exist fails
    # rather than creating it. The caller is expected to have made the tree.
    if not os.path.isdir(os.path.dirname(right)):
        return False
    shutil.copyfile(left, right)
    return True


def listdir(path):
    """(dirs, files), as Kodi returns them: names only, not paths."""
    real = _real(path)
    if not real or not os.path.isdir(real):
        return [], []
    dirs, files = [], []
    for name in sorted(os.listdir(real)):
        if os.path.isdir(os.path.join(real, name)):
            dirs.append(name)
        else:
            files.append(name)
    return dirs, files


def mkdirs(path):
    real = _real(path)
    if not real:
        return False
    if os.path.isdir(real):
        return True
    try:
        os.makedirs(real)
    except OSError:
        return False
    return True


def rmdir(path):
    real = _real(path)
    if not real or not os.path.isdir(real):
        return False
    try:
        os.rmdir(real)
    except OSError:
        return False
    return True


class Stat(object):
    def __init__(self, path):
        real = _real(path)
        if not real or not os.path.exists(real):
            raise IOError('no such file: %s' % path)
        self._stat = os.stat(real)

    def st_size(self):
        return self._stat.st_size

    def st_mtime(self):
        return self._stat.st_mtime


class File(object):
    def __init__(self, path, mode='r'):
        real = _real(path)
        if not real:
            raise IOError('cannot open %s' % path)
        self._handle = open(real, 'wb' if 'w' in mode else 'rb')

    def read(self, count=None):
        return self._handle.read() if count is None else self._handle.read(count)

    readBytes = read

    def write(self, data):
        return self._handle.write(data)

    def size(self):
        here = self._handle.tell()
        self._handle.seek(0, 2)
        total = self._handle.tell()
        self._handle.seek(here)
        return total

    def seek(self, offset, whence=0):
        return self._handle.seek(offset, whence)

    def close(self):
        self._handle.close()
