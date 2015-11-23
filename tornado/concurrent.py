from __future__ import absolute_import, division, print_function, with_statement

import functools
import platform
import traceback
import sys

from tornado.stack_context import ExceptionStackContext, wrap
from tornado.util import raise_exc_info

try:
    from concurrent import futures
except ImportError:
    futures = None

class ReturnValueIgnoredError(Exception):
    pass

class _TracebackLogger(object):
    __slots__ = ('exc_info', 'formatted_tb')

    def __init__(self, exc_info):
        self.exc_info = exc_info
        self.formatted_tb = None

    def activate(self):
        exc_info = self.exc_info
        if exc_info is not None:
            self.exc_info = None
            self.formatted_tb = traceback.format_exception(*exc_info)

    def clear(self):
        self.exc_info = None
        self.formatted_tb = None

    def __del__(self):
        if self.formatted_tb:
            print('Future exception was never retrieved: %s' % ''.join(self.formatted_tb).rstrip())


class Future(object):
    def __init__(self):
        self._done = False
        self._result = None
        self._exc_info = None

        self._log_traceback = False   # Used for Python >= 3.4
        self._tb_logger = None        # Used for Python <= 3.3

        self._callbacks = []

    def cancel(self):
        return False

    def cancelled(self):
        return False

    def running(self):
        return not self._done

    def done(self):
        return self._done

    def _clear_tb_log(self):
        self._log_traceback = False
        if self._tb_logger is not None:
            self._tb_logger.clear()
            self._tb_logger = None

    def result(self, timeout=None):
        self._clear_tb_log()
        if self._result is not None:
            return self._result
        if self._exc_info is not None:
            raise_exc_info(self._exc_info)
        self._check_done()
        return self._result

    def exception(self, timeout=None):
        self._clear_tb_log()
        if self._exc_info is not None:
            return self._exc_info[1]
        else:
            self._check_done()
            return None

    def add_done_callback(self, fn):
        if self._done:
            fn(self)
        else:
            self._callbacks.append(fn)

    def set_result(self, result):
        self._result = result
        self._set_done()

    def set_exception(self, exception):
        self.set_exc_info(
            (exception.__class__,
             exception,
             getattr(exception, '__traceback__', None)))

    def exc_info(self):
        self._clear_tb_log()
        return self._exc_info

    def set_exc_info(self, exc_info):
        self._exc_info = exc_info
        self._log_traceback = True
        self._tb_logger = _TracebackLogger(exc_info)

        try:
            self._set_done()
        finally:
            if self._log_traceback and self._tb_logger is not None:
                self._tb_logger.activate()
        self._exc_info = exc_info

    def _check_done(self):
        if not self._done:
            raise Exception("DummyFuture does not support blocking for results")

    def _set_done(self):
        self._done = True
        for cb in self._callbacks:
            try:
                cb(self)
            except Exception:
                print('Exception in callback %r for %r' % (cb, self))
        self._callbacks = None

TracebackFuture = Future

if futures is None:
    FUTURES = Future
else:
    FUTURES = (futures.Future, Future)


def is_future(x):
    return isinstance(x, FUTURES)


class DummyExecutor(object):
    def submit(self, fn, *args, **kwargs):
        future = TracebackFuture()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception:
            future.set_exc_info(sys.exc_info())
        return future

    def shutdown(self, wait=True):
        pass

dummy_executor = DummyExecutor()


def run_on_executor(*args, **kwargs):
    def run_on_executor_decorator(fn):
        executor = kwargs.get("executor", "executor")
        io_loop = kwargs.get("io_loop", "io_loop")
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            callback = kwargs.pop("callback", None)
            future = getattr(self, executor).submit(fn, self, *args, **kwargs)
            if callback:
                getattr(self, io_loop).add_future(
                    future, lambda future: callback(future.result()))
            return future
        return wrapper
    if args and kwargs:
        raise ValueError("cannot combine positional and keyword args")
    if len(args) == 1:
        return run_on_executor_decorator(args[0])
    elif len(args) != 0:
        raise ValueError("expected 1 argument, got %d", len(args))
    return run_on_executor_decorator

def chain_future(a, b):
    def copy(future):
        assert future is a
        if b.done():
            return
        if (isinstance(a, TracebackFuture) and isinstance(b, TracebackFuture)
                and a.exc_info() is not None):
            b.set_exc_info(a.exc_info())
        elif a.exception() is not None:
            b.set_exception(a.exception())
        else:
            b.set_result(a.result())
    a.add_done_callback(copy)
