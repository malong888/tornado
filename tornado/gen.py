from __future__ import absolute_import, division, print_function, with_statement

import collections
import functools
import itertools
import sys
import types
import weakref

from tornado.concurrent import Future, TracebackFuture, is_future, chain_future
from tornado.ioloop import IOLoop
from tornado import stack_context
from tornado.util import raise_exc_info

class LeakedCallbackError(Exception):
    pass

class BadYieldError(Exception):
    pass

class ReturnValueIgnoredError(Exception):
    pass

class TimeoutError(Exception):
    """Exception raised by ``with_timeout``."""

def coroutine(func, replace_callback=True):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        future = TracebackFuture()

        if replace_callback and 'callback' in kwargs:
            callback = kwargs.pop('callback')
            IOLoop.current().add_future(
                future, lambda future: callback(future.result()))

        try:
            result = func(*args, **kwargs)
        except (Return, StopIteration) as e:
            result = getattr(e, 'value', None)
        except Exception:
            future.set_exc_info(sys.exc_info())
            return future
        else:
            if isinstance(result, types.GeneratorType):
                try:
                    orig_stack_contexts = stack_context._state.contexts
                    yielded = next(result)
                    if stack_context._state.contexts is not orig_stack_contexts:
                        yielded = TracebackFuture()
                        yielded.set_exception(
                            stack_context.StackContextInconsistentError(
                                'stack_context inconsistency (probably caused '
                                'by yield within a "with StackContext" block)'))
                except (StopIteration, Return) as e:
                    future.set_result(getattr(e, 'value', None))
                except Exception:
                    future.set_exc_info(sys.exc_info())
                else:
                    Runner(result, future, yielded)
                try:
                    return future
                finally:
                    future = None
        future.set_result(result)
        return future
    return wrapper


class Return(Exception):
    def __init__(self, value=None):
        super(Return, self).__init__()
        self.value = value


def multi_future(children, quiet_exceptions=()):
    if isinstance(children, dict):
        keys = list(children.keys())
        children = children.values()
    else:
        keys = None
    assert all(is_future(i) for i in children)
    unfinished_children = set(children)

    future = Future()
    if not children:
        future.set_result({} if keys is not None else [])

    def callback(f):
        unfinished_children.remove(f)
        if not unfinished_children:
            result_list = []
            for f in children:
                try:
                    result_list.append(f.result())
                except Exception as e:
                    if future.done():
                        if not isinstance(e, quiet_exceptions):
                            print("Multiple exceptions in yield list")
                    else:
                        future.set_exc_info(sys.exc_info())
            if not future.done():
                if keys is not None:
                    future.set_result(dict(zip(keys, result_list)))
                else:
                    future.set_result(result_list)

    listening = set()
    for f in children:
        if f not in listening:
            listening.add(f)
            f.add_done_callback(callback)
    return future


def maybe_future(x):
    if is_future(x):
        return x
    else:
        fut = Future()
        fut.set_result(x)
        return fut


def with_timeout(timeout, future, io_loop=None, quiet_exceptions=()):
    result = Future()
    chain_future(future, result)
    if io_loop is None:
        io_loop = IOLoop.current()

    def error_callback(future):
        try:
            future.result()
        except Exception as e:
            if not isinstance(e, quiet_exceptions):
                print("Exception in Future %r after timeout" % future)

    def timeout_callback():
        result.set_exception(TimeoutError("Timeout"))
        # In case the wrapped future goes on to fail, log it.
        future.add_done_callback(error_callback)
    timeout_handle = io_loop.add_timeout(
        timeout, timeout_callback)
    if isinstance(future, Future):
        future.add_done_callback(
            lambda future: io_loop.remove_timeout(timeout_handle))
    else:
        io_loop.add_future(
            future, lambda future: io_loop.remove_timeout(timeout_handle))
    return result


def sleep(duration):
    f = Future()
    IOLoop.current().call_later(duration, lambda: f.set_result(None))
    return f


_null_future = Future()
_null_future.set_result(None)

moment = Future()
moment.__doc__ = \
moment.set_result(None)


class Runner(object):
    def __init__(self, gen, result_future, first_yielded):
        self.gen = gen
        self.result_future = result_future
        self.future = _null_future
        self.yield_point = None
        self.pending_callbacks = None
        self.results = None
        self.running = False
        self.finished = False
        self.had_exception = False
        self.io_loop = IOLoop.current()
        self.stack_context_deactivate = None
        if self.handle_yield(first_yielded):
            self.run()

    def run(self):
        if self.running or self.finished:
            return
        try:
            self.running = True
            while True:
                future = self.future
                if not future.done():
                    return
                self.future = None
                try:
                    orig_stack_contexts = stack_context._state.contexts
                    exc_info = None

                    try:
                        value = future.result()
                    except Exception:
                        self.had_exception = True
                        exc_info = sys.exc_info()

                    if exc_info is not None:
                        yielded = self.gen.throw(*exc_info)
                        exc_info = None
                    else:
                        yielded = self.gen.send(value)

                    if stack_context._state.contexts is not orig_stack_contexts:
                        self.gen.throw(
                            stack_context.StackContextInconsistentError(
                                'stack_context inconsistency (probably caused '
                                'by yield within a "with StackContext" block)'))
                except (StopIteration, Return) as e:
                    self.finished = True
                    self.future = _null_future
                    if self.pending_callbacks and not self.had_exception:
                        raise LeakedCallbackError(
                            "finished without waiting for callbacks %r" %
                            self.pending_callbacks)
                    self.result_future.set_result(getattr(e, 'value', None))
                    self.result_future = None
                    self._deactivate_stack_context()
                    return
                except Exception:
                    self.finished = True
                    self.future = _null_future
                    self.result_future.set_exc_info(sys.exc_info())
                    self.result_future = None
                    self._deactivate_stack_context()
                    return
                if not self.handle_yield(yielded):
                    return
        finally:
            self.running = False

    def handle_yield(self, yielded):
        try:
            self.future = convert_yielded(yielded)
        except BadYieldError:
            self.future = TracebackFuture()
            self.future.set_exc_info(sys.exc_info())

        if not self.future.done() or self.future is moment:
            self.io_loop.add_future(
                self.future, lambda f: self.run())
            return False
        return True

    def _deactivate_stack_context(self):
        if self.stack_context_deactivate is not None:
            self.stack_context_deactivate()
            self.stack_context_deactivate = None

def convert_yielded(yielded):
    if isinstance(yielded, (list, dict)):
        return multi_future(yielded)
    elif is_future(yielded):
        return yielded
    else:
        raise BadYieldError("yielded unknown object %r" % (yielded,))
