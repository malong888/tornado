from __future__ import absolute_import, division, print_function, with_statement

import collections
import errno
import numbers
import os
import socket
import sys
import re

from tornado.concurrent import TracebackFuture
from tornado import ioloop
from tornado import stack_context
from tornado.util import errno_from_exception
from tornado.platform.posix import _set_nonblocking

_ERRNO_WOULDBLOCK = (errno.EWOULDBLOCK, errno.EAGAIN)

_ERRNO_CONNRESET = (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE,
                    errno.ETIMEDOUT)

if sys.platform == 'darwin':
    _ERRNO_CONNRESET += (errno.EPROTOTYPE,)

_ERRNO_INPROGRESS = (errno.EINPROGRESS,)

class StreamClosedError(IOError):
    pass

class UnsatisfiableReadError(Exception):
    pass

class StreamBufferFullError(Exception):
    """Exception raised by `IOStream` methods when the buffer is full.
    """


class BaseIOStream(object):
    def __init__(self, io_loop=None, max_buffer_size=None,
                 read_chunk_size=None, max_write_buffer_size=None):
        self.io_loop = io_loop or ioloop.IOLoop.current()
        self.max_buffer_size = max_buffer_size or 104857600
        self.read_chunk_size = min(read_chunk_size or 65536,
                                   self.max_buffer_size // 2)
        self.max_write_buffer_size = max_write_buffer_size
        self.error = None
        self._read_buffer = collections.deque()
        self._write_buffer = collections.deque()
        self._read_buffer_size = 0
        self._write_buffer_size = 0
        self._write_buffer_frozen = False
        self._read_delimiter = None
        self._read_regex = None
        self._read_max_bytes = None
        self._read_bytes = None
        self._read_partial = False
        self._read_until_close = False
        self._read_callback = None
        self._read_future = None
        self._streaming_callback = None
        self._write_callback = None
        self._write_future = None
        self._close_callback = None
        self._connect_callback = None
        self._connect_future = None
        self._ssl_connect_future = None
        self._connecting = False
        self._state = None
        self._pending_callbacks = 0
        self._closed = False

    def fileno(self):
        raise NotImplementedError()

    def close_fd(self):
        raise NotImplementedError()

    def write_to_fd(self, data):
        raise NotImplementedError()

    def read_from_fd(self):
        raise NotImplementedError()

    def get_fd_error(self):
        return None

    def read_until_regex(self, regex, callback=None, max_bytes=None):
        future = self._set_read_callback(callback)
        self._read_regex = re.compile(regex)
        self._read_max_bytes = max_bytes
        try:
            self._try_inline_read()
        except UnsatisfiableReadError as e:
            print("Unsatisfiable read, closing connection: %s" % e)
            self.close(exc_info=True)
            return future
        except:
            if future is not None:
                future.add_done_callback(lambda f: f.exception())
            raise
        return future

    def read_until(self, delimiter, callback=None, max_bytes=None):
        future = self._set_read_callback(callback)
        self._read_delimiter = delimiter
        self._read_max_bytes = max_bytes
        try:
            self._try_inline_read()
        except UnsatisfiableReadError as e:
            print("Unsatisfiable read, closing connection: %s" % e)
            self.close(exc_info=True)
            return future
        except:
            if future is not None:
                future.add_done_callback(lambda f: f.exception())
            raise
        return future

    def read_bytes(self, num_bytes, callback=None, streaming_callback=None,
                   partial=False):
        future = self._set_read_callback(callback)
        assert isinstance(num_bytes, numbers.Integral)
        self._read_bytes = num_bytes
        self._read_partial = partial
        self._streaming_callback = stack_context.wrap(streaming_callback)
        try:
            self._try_inline_read()
        except:
            if future is not None:
                future.add_done_callback(lambda f: f.exception())
            raise
        return future

    def read_until_close(self, callback=None, streaming_callback=None):
        future = self._set_read_callback(callback)
        self._streaming_callback = stack_context.wrap(streaming_callback)
        if self.closed():
            if self._streaming_callback is not None:
                self._run_read_callback(self._read_buffer_size, True)
            self._run_read_callback(self._read_buffer_size, False)
            return future
        self._read_until_close = True
        try:
            self._try_inline_read()
        except:
            future.add_done_callback(lambda f: f.exception())
            raise
        return future

    def write(self, data, callback=None):
        assert isinstance(data, bytes)
        self._check_closed()
        if data:
            if (self.max_write_buffer_size is not None and
                    self._write_buffer_size + len(data) > self.max_write_buffer_size):
                raise StreamBufferFullError("Reached maximum write buffer size")
            WRITE_BUFFER_CHUNK_SIZE = 128 * 1024
            for i in range(0, len(data), WRITE_BUFFER_CHUNK_SIZE):
                self._write_buffer.append(data[i:i + WRITE_BUFFER_CHUNK_SIZE])
            self._write_buffer_size += len(data)
        if callback is not None:
            self._write_callback = stack_context.wrap(callback)
            future = None
        else:
            future = self._write_future = TracebackFuture()
            future.add_done_callback(lambda f: f.exception())
        if not self._connecting:
            self._handle_write()
            if self._write_buffer:
                self._add_io_state(self.io_loop.WRITE)
            self._maybe_add_error_listener()
        return future

    def set_close_callback(self, callback):
        self._close_callback = stack_context.wrap(callback)
        self._maybe_add_error_listener()

    def close(self, exc_info=False):
        if not self.closed():
            if exc_info:
                if not isinstance(exc_info, tuple):
                    exc_info = sys.exc_info()
                if any(exc_info):
                    self.error = exc_info[1]
            if self._read_until_close:
                if (self._streaming_callback is not None and
                        self._read_buffer_size):
                    self._run_read_callback(self._read_buffer_size, True)
                self._read_until_close = False
                self._run_read_callback(self._read_buffer_size, False)
            if self._state is not None:
                self.io_loop.remove_handler(self.fileno())
                self._state = None
            self.close_fd()
            self._closed = True
        self._maybe_run_close_callback()

    def _maybe_run_close_callback(self):
        # If there are pending callbacks, don't run the close callback
        # until they're done (see _maybe_add_error_handler)
        if self.closed() and self._pending_callbacks == 0:
            futures = []
            if self._read_future is not None:
                futures.append(self._read_future)
                self._read_future = None
            if self._write_future is not None:
                futures.append(self._write_future)
                self._write_future = None
            if self._connect_future is not None:
                futures.append(self._connect_future)
                self._connect_future = None
            if self._ssl_connect_future is not None:
                futures.append(self._ssl_connect_future)
                self._ssl_connect_future = None
            for future in futures:
                if self._is_connreset(self.error):
                    future.set_exception(StreamClosedError())
                else:
                    future.set_exception(self.error or StreamClosedError())
            if self._close_callback is not None:
                cb = self._close_callback
                self._close_callback = None
                self._run_callback(cb)
            self._read_callback = self._write_callback = None
            self._write_buffer = None

    def reading(self):
        return self._read_callback is not None or self._read_future is not None

    def writing(self):
        return bool(self._write_buffer)

    def closed(self):
        return self._closed

    def set_nodelay(self, value):
        pass

    def _handle_events(self, fd, events):
        if self.closed():
            # gen_log.warning("Got events for closed stream %s", fd)
            print("Got events for closed stream %s" % fd)
            return
        try:
            if self._connecting:
                self._handle_connect()
            if self.closed():
                return
            if events & self.io_loop.READ:
                self._handle_read()
            if self.closed():
                return
            if events & self.io_loop.WRITE:
                self._handle_write()
            if self.closed():
                return
            if events & self.io_loop.ERROR:
                self.error = self.get_fd_error()
                self.io_loop.add_callback(self.close)
                return
            state = self.io_loop.ERROR
            if self.reading():
                state |= self.io_loop.READ
            if self.writing():
                state |= self.io_loop.WRITE
            if state == self.io_loop.ERROR and self._read_buffer_size == 0:
                state |= self.io_loop.READ
            if state != self._state:
                assert self._state is not None, \
                    "shouldn't happen: _handle_events without self._state"
                self._state = state
                self.io_loop.update_handler(self.fileno(), self._state)
        except UnsatisfiableReadError as e:
            # gen_log.info("Unsatisfiable read, closing connection: %s" % e)
            print("Unsatisfiable read, closing connection: %s" % e)
            self.close(exc_info=True)
        except Exception:
            # gen_log.error("Uncaught exception, closing connection.",
                          # exc_info=True)
            print("Uncaught exception, closing connection.")
            self.close(exc_info=True)
            raise

    def _run_callback(self, callback, *args):
        def wrapper():
            self._pending_callbacks -= 1
            try:
                return callback(*args)
            except Exception:
                # app_log.error("Uncaught exception, closing connection.",
                              # exc_info=True)
                print("Uncaught exception, closing connection.")
                self.close(exc_info=True)
                raise
            finally:
                self._maybe_add_error_listener()
        with stack_context.NullContext():
            self._pending_callbacks += 1
            self.io_loop.add_callback(wrapper)

    def _read_to_buffer_loop(self):
        try:
            if self._read_bytes is not None:
                target_bytes = self._read_bytes
            elif self._read_max_bytes is not None:
                target_bytes = self._read_max_bytes
            elif self.reading():
                target_bytes = None
            else:
                target_bytes = 0
            next_find_pos = 0
            self._pending_callbacks += 1
            while not self.closed():
                if self._read_to_buffer() == 0:
                    break

                self._run_streaming_callback()

                if (target_bytes is not None and
                        self._read_buffer_size >= target_bytes):
                    break

                if self._read_buffer_size >= next_find_pos:
                    pos = self._find_read_pos()
                    if pos is not None:
                        return pos
                    next_find_pos = self._read_buffer_size * 2
            return self._find_read_pos()
        finally:
            self._pending_callbacks -= 1

    def _handle_read(self):
        try:
            pos = self._read_to_buffer_loop()
        except UnsatisfiableReadError:
            raise
        except Exception:
            gen_log.warning("error on read", exc_info=True)
            self.close(exc_info=True)
            return
        if pos is not None:
            self._read_from_buffer(pos)
            return
        else:
            self._maybe_run_close_callback()

    def _set_read_callback(self, callback):
        assert self._read_callback is None, "Already reading"
        assert self._read_future is None, "Already reading"
        if callback is not None:
            self._read_callback = stack_context.wrap(callback)
        else:
            self._read_future = TracebackFuture()
        return self._read_future

    def _run_read_callback(self, size, streaming):
        if streaming:
            callback = self._streaming_callback
        else:
            callback = self._read_callback
            self._read_callback = self._streaming_callback = None
            if self._read_future is not None:
                assert callback is None
                future = self._read_future
                self._read_future = None
                future.set_result(self._consume(size))
        if callback is not None:
            assert (self._read_future is None) or streaming
            self._run_callback(callback, self._consume(size))
        else:
            # If we scheduled a callback, we will add the error listener
            # afterwards.  If we didn't, we have to do it now.
            self._maybe_add_error_listener()

    def _try_inline_read(self):
        """Attempt to complete the current read operation from buffered data.

        If the read can be completed without blocking, schedules the
        read callback on the next IOLoop iteration; otherwise starts
        listening for reads on the socket.
        """
        # See if we've already got the data from a previous read
        self._run_streaming_callback()
        pos = self._find_read_pos()
        if pos is not None:
            self._read_from_buffer(pos)
            return
        self._check_closed()
        try:
            pos = self._read_to_buffer_loop()
        except Exception:
            self._maybe_run_close_callback()
            raise
        if pos is not None:
            self._read_from_buffer(pos)
            return
        if self.closed():
            self._maybe_run_close_callback()
        else:
            self._add_io_state(ioloop.IOLoop.READ)

    def _read_to_buffer(self):
        try:
            chunk = self.read_from_fd()
        except (socket.error, IOError, OSError) as e:
            if self._is_connreset(e):
                self.close(exc_info=True)
                return
            self.close(exc_info=True)
            raise
        if chunk is None:
            return 0
        self._read_buffer.append(chunk)
        self._read_buffer_size += len(chunk)
        if self._read_buffer_size > self.max_buffer_size:
            # gen_log.error("Reached maximum read buffer size")
            print("Reached maximum read buffer size")
            self.close()
            raise StreamBufferFullError("Reached maximum read buffer size")
        return len(chunk)

    def _run_streaming_callback(self):
        if self._streaming_callback is not None and self._read_buffer_size:
            bytes_to_consume = self._read_buffer_size
            if self._read_bytes is not None:
                bytes_to_consume = min(self._read_bytes, bytes_to_consume)
                self._read_bytes -= bytes_to_consume
            self._run_read_callback(bytes_to_consume, True)

    def _read_from_buffer(self, pos):
        self._read_bytes = self._read_delimiter = self._read_regex = None
        self._read_partial = False
        self._run_read_callback(pos, False)

    def _find_read_pos(self):
        if (self._read_bytes is not None and
            (self._read_buffer_size >= self._read_bytes or
             (self._read_partial and self._read_buffer_size > 0))):
            num_bytes = min(self._read_bytes, self._read_buffer_size)
            return num_bytes
        elif self._read_delimiter is not None:
            if self._read_buffer:
                while True:
                    loc = self._read_buffer[0].find(self._read_delimiter)
                    if loc != -1:
                        delimiter_len = len(self._read_delimiter)
                        self._check_max_bytes(self._read_delimiter,
                                              loc + delimiter_len)
                        return loc + delimiter_len
                    if len(self._read_buffer) == 1:
                        break
                    _double_prefix(self._read_buffer)
                self._check_max_bytes(self._read_delimiter,
                                      len(self._read_buffer[0]))
        elif self._read_regex is not None:
            if self._read_buffer:
                while True:
                    m = self._read_regex.search(self._read_buffer[0])
                    if m is not None:
                        self._check_max_bytes(self._read_regex, m.end())
                        return m.end()
                    if len(self._read_buffer) == 1:
                        break
                    _double_prefix(self._read_buffer)
                self._check_max_bytes(self._read_regex,
                                      len(self._read_buffer[0]))
        return None

    def _check_max_bytes(self, delimiter, size):
        if (self._read_max_bytes is not None and
                size > self._read_max_bytes):
            raise UnsatisfiableReadError(
                "delimiter %r not found within %d bytes" % (
                    delimiter, self._read_max_bytes))

    def _handle_write(self):
        while self._write_buffer:
            try:
                if not self._write_buffer_frozen:
                    _merge_prefix(self._write_buffer, 128 * 1024)
                num_bytes = self.write_to_fd(self._write_buffer[0])
                if num_bytes == 0:
                    self._write_buffer_frozen = True
                    break
                self._write_buffer_frozen = False
                _merge_prefix(self._write_buffer, num_bytes)
                self._write_buffer.popleft()
                self._write_buffer_size -= num_bytes
            except (socket.error, IOError, OSError) as e:
                if e.args[0] in _ERRNO_WOULDBLOCK:
                    self._write_buffer_frozen = True
                    break
                else:
                    if not self._is_connreset(e):
                        # gen_log.warning("Write error on %s: %s",
                                        # self.fileno(), e)
                        print("Write error on %s: %s" % (self,fileno(), e))
                    self.close(exc_info=True)
                    return
        if not self._write_buffer:
            if self._write_callback:
                callback = self._write_callback
                self._write_callback = None
                self._run_callback(callback)
            if self._write_future:
                future = self._write_future
                self._write_future = None
                future.set_result(None)

    def _consume(self, loc):
        if loc == 0:
            return b""
        _merge_prefix(self._read_buffer, loc)
        self._read_buffer_size -= loc
        return self._read_buffer.popleft()

    def _check_closed(self):
        if self.closed():
            raise StreamClosedError("Stream is closed")

    def _maybe_add_error_listener(self):
        if self._pending_callbacks != 0:
            return
        if self._state is None or self._state == ioloop.IOLoop.ERROR:
            if self.closed():
                self._maybe_run_close_callback()
            elif (self._read_buffer_size == 0 and
                  self._close_callback is not None):
                self._add_io_state(ioloop.IOLoop.READ)

    def _add_io_state(self, state):
        if self.closed():
            return
        if self._state is None:
            self._state = ioloop.IOLoop.ERROR | state
            with stack_context.NullContext():
                self.io_loop.add_handler(
                    self.fileno(), self._handle_events, self._state)
        elif not self._state & state:
            self._state = self._state | state
            self.io_loop.update_handler(self.fileno(), self._state)

    def _is_connreset(self, exc):
        return (isinstance(exc, (socket.error, IOError)) and
                errno_from_exception(exc) in _ERRNO_CONNRESET)


class IOStream(BaseIOStream):
    def __init__(self, socket, *args, **kwargs):
        self.socket = socket
        self.socket.setblocking(False)
        super(IOStream, self).__init__(*args, **kwargs)

    def fileno(self):
        return self.socket

    def close_fd(self):
        self.socket.close()
        self.socket = None

    def get_fd_error(self):
        errno = self.socket.getsockopt(socket.SOL_SOCKET,
                                       socket.SO_ERROR)
        return socket.error(errno, os.strerror(errno))

    def read_from_fd(self):
        try:
            chunk = self.socket.recv(self.read_chunk_size)
        except socket.error as e:
            if e.args[0] in _ERRNO_WOULDBLOCK:
                return None
            else:
                raise
        if not chunk:
            self.close()
            return None
        return chunk

    def write_to_fd(self, data):
        return self.socket.send(data)

    def connect(self, address, callback=None, server_hostname=None):
        self._connecting = True
        if callback is not None:
            self._connect_callback = stack_context.wrap(callback)
            future = None
        else:
            future = self._connect_future = TracebackFuture()
        try:
            self.socket.connect(address)
        except socket.error as e:
            if (errno_from_exception(e) not in _ERRNO_INPROGRESS and
                    errno_from_exception(e) not in _ERRNO_WOULDBLOCK):
                if future is None:
                    print("Connect error on fd %s: %s" % (self.socket.fileno(), e))
                self.close(exc_info=True)
                return future
        self._add_io_state(self.io_loop.WRITE)
        return future

    def _handle_connect(self):
        err = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            self.error = socket.error(err, os.strerror(err))
            if self._connect_future is None:
                # gen_log.warning("Connect error on fd %s: %s",
                                # self.socket.fileno(), errno.errorcode[err])
                print("Connect error on fd %s: %s", (self.socket.fileno(), errno.errorcode[err]))
            self.close()
            return
        if self._connect_callback is not None:
            callback = self._connect_callback
            self._connect_callback = None
            self._run_callback(callback)
        if self._connect_future is not None:
            future = self._connect_future
            self._connect_future = None
            future.set_result(self)
        self._connecting = False

    def set_nodelay(self, value):
        if (self.socket is not None and
                self.socket.family in (socket.AF_INET, socket.AF_INET6)):
            try:
                self.socket.setsockopt(socket.IPPROTO_TCP,
                                       socket.TCP_NODELAY, 1 if value else 0)
            except socket.error as e:
                if e.errno != errno.EINVAL and not self._is_connreset(e):
                    raise


def _double_prefix(deque):
    new_len = max(len(deque[0]) * 2,
                  (len(deque[0]) + len(deque[1])))
    _merge_prefix(deque, new_len)


def _merge_prefix(deque, size):
    if len(deque) == 1 and len(deque[0]) <= size:
        return
    prefix = []
    remaining = size
    while deque and remaining > 0:
        chunk = deque.popleft()
        if len(chunk) > remaining:
            deque.appendleft(chunk[remaining:])
            chunk = chunk[:remaining]
        prefix.append(chunk)
        remaining -= len(chunk)
    if prefix:
        deque.appendleft(type(prefix[0])().join(prefix))
    if not deque:
        deque.appendleft(b"")
