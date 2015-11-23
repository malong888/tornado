from __future__ import absolute_import, division, print_function, with_statement

import os
from tornado.platform.posix import set_close_exec, Waker

try:
    # monotime monkey-patches the time module to have a monotonic function
    # in versions of python before 3.3.
    import monotime
    # Silence pyflakes warning about this unused import
    monotime
except ImportError:
    pass
try:
    from time import monotonic as monotonic_time
except ImportError:
    monotonic_time = None

__all__ = ['Waker', 'set_close_exec', 'monotonic_time']
