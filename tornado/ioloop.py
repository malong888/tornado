from __future__ import absolute_import, division, print_function, with_statement

import datetime
import errno
import functools
import heapq
import itertools
import logging
import numbers
import os
import select
import sys
import threading
import time
import traceback
import math

from tornado.concurrent import TracebackFuture, is_future
from tornado import stack_context
from tornado.util import Configurable, errno_from_exception, timedelta_to_seconds

import signal
import thread

from tornado.platform.auto import set_close_exec, Waker


_POLL_TIMEOUT = 3600.0


class TimeoutError(Exception):
    pass


class IOLoop(Configurable):
    _EPOLLIN = 0x001
    _EPOLLPRI = 0x002
    _EPOLLOUT = 0x004
    _EPOLLERR = 0x008
    _EPOLLHUP = 0x010
    _EPOLLRDHUP = 0x2000
    _EPOLLONESHOT = (1 << 30)
    _EPOLLET = (1 << 31)

    NONE = 0
    READ = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP

    _instance_lock = threading.Lock()

    _current = threading.local()

    @staticmethod
    def instance():
        if not hasattr(IOLoop, "_instance"):
            with IOLoop._instance_lock:
                if not hasattr(IOLoop, "_instance"):
                    # New instance after double check
                    IOLoop._instance = IOLoop()
        return IOLoop._instance

    @staticmethod
    def current(instance=True):
        current = getattr(IOLoop._current, "instance", None)
        if current is None and instance:
            return IOLoop.instance()
        return current

    def make_current(self):
        IOLoop._current.instance = self

    @staticmethod
    def clear_current():
        IOLoop._current.instance = None

    @classmethod
    def configurable_base(cls):
        return IOLoop

    @classmethod
    def configurable_default(cls):
        if hasattr(select, "epoll"):
            from tornado.platform.epoll import EPollIOLoop
            return EPollIOLoop
        if hasattr(select, "kqueue"):
            # Python 2.6+ on BSD or Mac
            from tornado.platform.kqueue import KQueueIOLoop
            return KQueueIOLoop
        from tornado.platform.select import SelectIOLoop
        return SelectIOLoop

    def initialize(self, make_current=None):
        if make_current is None:
            if IOLoop.current(instance=False) is None:
                self.make_current()
        elif make_current:
            if IOLoop.current(instance=False) is None:
                raise RuntimeError("current IOLoop already exists")
            self.make_current()

    def close(self, all_fds=False):
        raise NotImplementedError()

    def add_handler(self, fd, handler, events):
        raise NotImplementedError()

    def update_handler(self, fd, events):
        raise NotImplementedError()

    def remove_handler(self, fd):
        raise NotImplementedError()

    def set_blocking_signal_threshold(self, seconds, action):
        raise NotImplementedError()

    def set_blocking_log_threshold(self, seconds):
        self.set_blocking_signal_threshold(seconds, self.log_stack)

    def start(self):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()

    def run_sync(self, func, timeout=None):
        future_cell = [None]

        def run():
            try:
                result = func()
            except Exception:
                future_cell[0] = TracebackFuture()
                future_cell[0].set_exc_info(sys.exc_info())
            else:
                if is_future(result):
                    future_cell[0] = result
                else:
                    future_cell[0] = TracebackFuture()
                    future_cell[0].set_result(result)
            self.add_future(future_cell[0], lambda future: self.stop())
        self.add_callback(run)
        if timeout is not None:
            timeout_handle = self.add_timeout(self.time() + timeout, self.stop)
        self.start()
        if timeout is not None:
            self.remove_timeout(timeout_handle)
        if not future_cell[0].done():
            raise TimeoutError('Operation timed out after %s seconds' % timeout)
        return future_cell[0].result()

    def time(self):
        return time.time()

    def add_timeout(self, deadline, callback, *args, **kwargs):
        if isinstance(deadline, numbers.Real):
            return self.call_at(deadline, callback, *args, **kwargs)
        elif isinstance(deadline, datetime.timedelta):
            return self.call_at(self.time() + timedelta_to_seconds(deadline),
                                callback, *args, **kwargs)
        else:
            raise TypeError("Unsupported deadline %r" % deadline)

    def call_later(self, delay, callback, *args, **kwargs):
        return self.call_at(self.time() + delay, callback, *args, **kwargs)

    def call_at(self, when, callback, *args, **kwargs):
        return self.add_timeout(when, callback, *args, **kwargs)

    def remove_timeout(self, timeout):
        raise NotImplementedError()

    def add_callback(self, callback, *args, **kwargs):
        raise NotImplementedError()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        raise NotImplementedError()

    def spawn_callback(self, callback, *args, **kwargs):
        with stack_context.NullContext():
            self.add_callback(callback, *args, **kwargs)

    def add_future(self, future, callback):
        assert is_future(future)
        callback = stack_context.wrap(callback)
        future.add_done_callback(
            lambda future: self.add_callback(callback, future))

    def _run_callback(self, callback):
        try:
            ret = callback()
            if ret is not None and is_future(ret):
                self.add_future(ret, lambda f: f.result())
        except Exception:
            self.handle_callback_exception(callback)

    def handle_callback_exception(self, callback):
        print("Exception in callback %r" % callback)

    def split_fd(self, fd):
        try:
            return fd.fileno(), fd
        except AttributeError:
            return fd, fd

    def close_fd(self, fd):
        try:
            try:
                fd.close()
            except AttributeError:
                os.close(fd)
        except OSError:
            pass


class PollIOLoop(IOLoop):
    def initialize(self, impl, time_func=None, **kwargs):
        super(PollIOLoop, self).initialize(**kwargs)
        self._impl = impl
        if hasattr(self._impl, 'fileno'):
            set_close_exec(self._impl.fileno())
        self.time_func = time_func or time.time
        self._handlers = {}
        self._events = {}
        self._callbacks = []
        self._callback_lock = threading.Lock()
        self._timeouts = []
        self._cancellations = 0
        self._running = False
        self._stopped = False
        self._closing = False
        self._thread_ident = None
        self._blocking_signal_threshold = None
        self._timeout_counter = itertools.count()

        self._waker = Waker()
        self.add_handler(self._waker.fileno(),
                         lambda fd, events: self._waker.consume(),
                         self.READ)

    def close(self, all_fds=False):
        with self._callback_lock:
            self._closing = True
        self.remove_handler(self._waker.fileno())
        if all_fds:
            for fd, handler in self._handlers.values():
                self.close_fd(fd)
        self._waker.close()
        self._impl.close()
        self._callbacks = None
        self._timeouts = None

    def add_handler(self, fd, handler, events):
        fd, obj = self.split_fd(fd)
        self._handlers[fd] = (obj, stack_context.wrap(handler))
        self._impl.register(fd, events | self.ERROR)

    def update_handler(self, fd, events):
        fd, obj = self.split_fd(fd)
        self._impl.modify(fd, events | self.ERROR)

    def remove_handler(self, fd):
        fd, obj = self.split_fd(fd)
        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._impl.unregister(fd)
        except Exception:
            print("Error deleting fd from IOLoop")

    def set_blocking_signal_threshold(self, seconds, action):
        self._blocking_signal_threshold = seconds
        if seconds is not None:
            signal.signal(signal.SIGALRM,
                          action if action is not None else signal.SIG_DFL)

    def start(self):
        if self._running:
            raise RuntimeError("IOLoop is already running")
        if self._stopped:
            self._stopped = False
            return
        old_current = getattr(IOLoop._current, "instance", None)
        IOLoop._current.instance = self
        self._thread_ident = thread.get_ident()
        self._running = True

        old_wakeup_fd = None
        if hasattr(signal, 'set_wakeup_fd') and os.name == 'posix':
            try:
                old_wakeup_fd = signal.set_wakeup_fd(self._waker.write_fileno())
                if old_wakeup_fd != -1:
                    signal.set_wakeup_fd(old_wakeup_fd)
                    old_wakeup_fd = None
            except ValueError:
                old_wakeup_fd = None

        try:
            while True:
                with self._callback_lock:
                    callbacks = self._callbacks
                    self._callbacks = []

                due_timeouts = []
                if self._timeouts:
                    now = self.time()
                    while self._timeouts:
                        if self._timeouts[0].callback is None:
                            heapq.heappop(self._timeouts)
                            self._cancellations -= 1
                        elif self._timeouts[0].deadline <= now:
                            due_timeouts.append(heapq.heappop(self._timeouts))
                        else:
                            break
                    if (self._cancellations > 512
                            and self._cancellations > (len(self._timeouts) >> 1)):
                        self._cancellations = 0
                        self._timeouts = [x for x in self._timeouts
                                          if x.callback is not None]
                        heapq.heapify(self._timeouts)

                for callback in callbacks:
                    self._run_callback(callback)
                for timeout in due_timeouts:
                    if timeout.callback is not None:
                        self._run_callback(timeout.callback)
                callbacks = callback = due_timeouts = timeout = None

                if self._callbacks:
                    poll_timeout = 0.0
                elif self._timeouts:
                    poll_timeout = self._timeouts[0].deadline - self.time()
                    poll_timeout = max(0, min(poll_timeout, _POLL_TIMEOUT))
                else:
                    poll_timeout = _POLL_TIMEOUT

                if not self._running:
                    break

                if self._blocking_signal_threshold is not None:
                    signal.setitimer(signal.ITIMER_REAL, 0, 0)

                try:
                    event_pairs = self._impl.poll(poll_timeout)
                except Exception as e:
                    if errno_from_exception(e) == errno.EINTR:
                        continue
                    else:
                        raise

                if self._blocking_signal_threshold is not None:
                    signal.setitimer(signal.ITIMER_REAL,
                                     self._blocking_signal_threshold, 0)

                self._events.update(event_pairs)
                while self._events:
                    fd, events = self._events.popitem()
                    try:
                        fd_obj, handler_func = self._handlers[fd]
                        handler_func(fd_obj, events)
                    except (OSError, IOError) as e:
                        if errno_from_exception(e) == errno.EPIPE:
                            pass
                        else:
                            self.handle_callback_exception(self._handlers.get(fd))
                    except Exception:
                        self.handle_callback_exception(self._handlers.get(fd))
                fd_obj = handler_func = None

        finally:
            self._stopped = False
            if self._blocking_signal_threshold is not None:
                signal.setitimer(signal.ITIMER_REAL, 0, 0)
            IOLoop._current.instance = old_current
            if old_wakeup_fd is not None:
                signal.set_wakeup_fd(old_wakeup_fd)

    def stop(self):
        self._running = False
        self._stopped = True
        self._waker.wake()

    def time(self):
        return self.time_func()

    def call_at(self, deadline, callback, *args, **kwargs):
        timeout = _Timeout(
            deadline,
            functools.partial(stack_context.wrap(callback), *args, **kwargs),
            self)
        heapq.heappush(self._timeouts, timeout)
        return timeout

    def remove_timeout(self, timeout):
        timeout.callback = None
        self._cancellations += 1

    def add_callback(self, callback, *args, **kwargs):
        with self._callback_lock:
            if self._closing:
                raise RuntimeError("IOLoop is closing")
            list_empty = not self._callbacks
            self._callbacks.append(functools.partial(
                stack_context.wrap(callback), *args, **kwargs))
            if list_empty and thread.get_ident() != self._thread_ident:
                self._waker.wake()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        with stack_context.NullContext():
            if thread.get_ident() != self._thread_ident:
                self.add_callback(callback, *args, **kwargs)
            else:
                self._callbacks.append(functools.partial(
                    stack_context.wrap(callback), *args, **kwargs))


class _Timeout(object):
    """An IOLoop timeout, a UNIX timestamp and a callback"""

    __slots__ = ['deadline', 'callback', 'tiebreaker']

    def __init__(self, deadline, callback, io_loop):
        if not isinstance(deadline, numbers.Real):
            raise TypeError("Unsupported deadline %r" % deadline)
        self.deadline = deadline
        self.callback = callback
        self.tiebreaker = next(io_loop._timeout_counter)

    def __lt__(self, other):
        return ((self.deadline, self.tiebreaker) <
                (other.deadline, other.tiebreaker))

    def __le__(self, other):
        return ((self.deadline, self.tiebreaker) <=
                (other.deadline, other.tiebreaker))