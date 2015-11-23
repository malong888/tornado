from __future__ import absolute_import, division, print_function, with_statement

import sys
import threading
from tornado.util import raise_exc_info

class StackContextInconsistentError(Exception):
    pass

class _State(threading.local):
    def __init__(self):
        self.contexts = (tuple(), None)
_state = _State()

class StackContext(object):
    def __init__(self, context_factory):
        self.context_factory = context_factory
        self.contexts = []
        self.active = True

    def _deactivate(self):
        self.active = False

    # StackContext protocol
    def enter(self):
        context = self.context_factory()
        self.contexts.append(context)
        context.__enter__()

    def exit(self, type, value, traceback):
        context = self.contexts.pop()
        context.__exit__(type, value, traceback)

    def __enter__(self):
        self.old_contexts = _state.contexts
        self.new_contexts = (self.old_contexts[0] + (self,), self)
        _state.contexts = self.new_contexts

        try:
            self.enter()
        except:
            _state.contexts = self.old_contexts
            raise

        return self._deactivate

    def __exit__(self, type, value, traceback):
        try:
            self.exit(type, value, traceback)
        finally:
            final_contexts = _state.contexts
            _state.contexts = self.old_contexts

            if final_contexts is not self.new_contexts:
                raise StackContextInconsistentError(
                    'stack_context inconsistency (may be caused by yield '
                    'within a "with StackContext" block)')

            self.new_contexts = None


class ExceptionStackContext(object):
    def __init__(self, exception_handler):
        self.exception_handler = exception_handler
        self.active = True

    def _deactivate(self):
        self.active = False

    def exit(self, type, value, traceback):
        if type is not None:
            return self.exception_handler(type, value, traceback)

    def __enter__(self):
        self.old_contexts = _state.contexts
        self.new_contexts = (self.old_contexts[0], self)
        _state.contexts = self.new_contexts

        return self._deactivate

    def __exit__(self, type, value, traceback):
        try:
            if type is not None:
                return self.exception_handler(type, value, traceback)
        finally:
            final_contexts = _state.contexts
            _state.contexts = self.old_contexts

            if final_contexts is not self.new_contexts:
                raise StackContextInconsistentError(
                    'stack_context inconsistency (may be caused by yield '
                    'within a "with StackContext" block)')

            # Break up a reference to itself to allow for faster GC on CPython.
            self.new_contexts = None


class NullContext(object):
    def __enter__(self):
        self.old_contexts = _state.contexts
        _state.contexts = (tuple(), None)

    def __exit__(self, type, value, traceback):
        _state.contexts = self.old_contexts


def _remove_deactivated(contexts):
    stack_contexts = tuple([h for h in contexts[0] if h.active])

    head = contexts[1]
    while head is not None and not head.active:
        head = head.old_contexts[1]

    ctx = head
    while ctx is not None:
        parent = ctx.old_contexts[1]

        while parent is not None:
            if parent.active:
                break
            ctx.old_contexts = parent.old_contexts
            parent = parent.old_contexts[1]

        ctx = parent

    return (stack_contexts, head)


def wrap(fn):
    if fn is None or hasattr(fn, '_wrapped'):
        return fn

    cap_contexts = [_state.contexts]

    if not cap_contexts[0][0] and not cap_contexts[0][1]:
        def null_wrapper(*args, **kwargs):
            try:
                current_state = _state.contexts
                _state.contexts = cap_contexts[0]
                return fn(*args, **kwargs)
            finally:
                _state.contexts = current_state
        null_wrapper._wrapped = True
        return null_wrapper

    def wrapped(*args, **kwargs):
        ret = None
        try:
            current_state = _state.contexts

            cap_contexts[0] = contexts = _remove_deactivated(cap_contexts[0])

            _state.contexts = contexts

            exc = (None, None, None)
            top = None

            last_ctx = 0
            stack = contexts[0]

            for n in stack:
                try:
                    n.enter()
                    last_ctx += 1
                except:
                    exc = sys.exc_info()
                    top = n.old_contexts[1]

            if top is None:
                try:
                    ret = fn(*args, **kwargs)
                except:
                    exc = sys.exc_info()
                    top = contexts[1]

            if top is not None:
                exc = _handle_exception(top, exc)
            else:
                while last_ctx > 0:
                    last_ctx -= 1
                    c = stack[last_ctx]

                    try:
                        c.exit(*exc)
                    except:
                        exc = sys.exc_info()
                        top = c.old_contexts[1]
                        break
                else:
                    top = None

                if top is not None:
                    exc = _handle_exception(top, exc)

            if exc != (None, None, None):
                raise_exc_info(exc)
        finally:
            _state.contexts = current_state
        return ret

    wrapped._wrapped = True
    return wrapped


def _handle_exception(tail, exc):
    while tail is not None:
        try:
            if tail.exit(*exc):
                exc = (None, None, None)
        except:
            exc = sys.exc_info()

        tail = tail.old_contexts[1]

    return exc
