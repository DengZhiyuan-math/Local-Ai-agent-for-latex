"""macOS FSEvents adapter. Kept separate so other platforms never load Apple libraries.

Uses file events on a serial libdispatch queue. Overflow/root-change flags bypass
filename filtering and invalidate the snapshot, as Apple's FSEvents API requires.
"""
from __future__ import annotations

import ctypes as c
from pathlib import Path


class Watch:
    def __init__(self, root, changed, excluded, accept):
        self.root, self.changed, self.excluded, self.accept = root, changed, excluded, accept
        self.cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.fs = c.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
        self.dispatch = c.CDLL('/usr/lib/system/libdispatch.dylib')
        self.callback_type = c.CFUNCTYPE(None, c.c_void_p, c.c_void_p, c.c_size_t, c.c_void_p,
                                       c.POINTER(c.c_uint32), c.POINTER(c.c_uint64))
        self.barrier_type = c.CFUNCTYPE(None, c.c_void_p)
        for lib, name, args, result in (
            (self.cf, 'CFStringCreateWithCString', [c.c_void_p, c.c_char_p, c.c_uint32], c.c_void_p),
            (self.cf, 'CFArrayCreate', [c.c_void_p, c.POINTER(c.c_void_p), c.c_long, c.c_void_p], c.c_void_p),
            (self.cf, 'CFRelease', [c.c_void_p], None),
            (self.fs, 'FSEventStreamCreate', [c.c_void_p, self.callback_type, c.c_void_p, c.c_void_p,
                                           c.c_uint64, c.c_double, c.c_uint32], c.c_void_p),
            (self.fs, 'FSEventStreamSetDispatchQueue', [c.c_void_p, c.c_void_p], None),
            (self.fs, 'FSEventStreamStart', [c.c_void_p], c.c_ubyte),
            (self.fs, 'FSEventStreamStop', [c.c_void_p], None),
            (self.fs, 'FSEventStreamInvalidate', [c.c_void_p], None),
            (self.fs, 'FSEventStreamRelease', [c.c_void_p], None),
            (self.dispatch, 'dispatch_queue_create', [c.c_char_p, c.c_void_p], c.c_void_p),
            (self.dispatch, 'dispatch_sync_f', [c.c_void_p, c.c_void_p, self.barrier_type], None),
            (self.dispatch, 'dispatch_release', [c.c_void_p], None),
        ):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
        self.callback = self.callback_type(self._events)
        self.stream = self.queue = self.paths = self.string = None
        self.scheduled = self.started = False
        try:
            self.string = self.cf.CFStringCreateWithCString(None, str(root).encode('utf-8'), 0x08000100)
            if not self.string:
                raise OSError('Cannot create FSEvents path')
            self.paths = self.cf.CFArrayCreate(None, (c.c_void_p * 1)(self.string), 1, None)
            self.queue = self.dispatch.dispatch_queue_create(b'prism.directory-events', None)
            if not self.paths or not self.queue:
                raise OSError('Cannot allocate FSEvents stream')
            # SinceNow, NoDefer | WatchRoot | FileEvents; do not use CFTypes for paths.
            self.stream = self.fs.FSEventStreamCreate(None, self.callback, None, self.paths,
                                                     0xffffffffffffffff, 0.05, 0x16)
            if not self.stream:
                raise OSError('Cannot create FSEvents stream')
            self.fs.FSEventStreamSetDispatchQueue(self.stream, self.queue)
            self.scheduled = True
            if not self.fs.FSEventStreamStart(self.stream):
                raise OSError('Cannot start FSEvents stream')
            self.started = True
        except OSError:
            self.close()
            raise

    def _events(self, stream, context, count, paths, flags, ids):
        names = c.cast(paths, c.POINTER(c.c_char_p))
        for i in range(count):
            # MustScanSubDirs, dropped events, wrapped IDs, root changed, mount/unmount.
            if flags[i] & 0xef:
                self.changed()
                return
            path = Path(names[i].decode('utf-8', 'surrogateescape'))
            if any(path.is_relative_to(p) for p in self.excluded):
                continue
            try:
                name = str(path.relative_to(self.root))
            except ValueError:
                self.changed()
                return
            if self.accept(name):
                self.changed()
                return

    def is_alive(self):
        return self.started

    def close(self):
        if self.stream:
            if self.started:
                self.fs.FSEventStreamStop(self.stream)
            if self.scheduled:
                self.fs.FSEventStreamInvalidate(self.stream)
            if self.queue:
                barrier = self.barrier_type(lambda _: None)
                self.dispatch.dispatch_sync_f(self.queue, None, barrier)
            self.fs.FSEventStreamRelease(self.stream)
            self.stream = None
        self.started = False
        if self.queue:
            self.dispatch.dispatch_release(self.queue)
            self.queue = None
        for name in ('paths', 'string'):
            value = getattr(self, name)
            if value:
                self.cf.CFRelease(value)
                setattr(self, name, None)
