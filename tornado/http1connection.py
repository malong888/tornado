from __future__ import absolute_import, division, print_function, with_statement

import re

from tornado.concurrent import Future
from tornado.escape import native_str, utf8
from tornado import gen
from tornado import httputil
from tornado import iostream
from tornado import stack_context

class _QuietException(Exception):
    def __init__(self):
        pass

class _ExceptionLoggingContext(object):
    def __enter__(self):
        pass

    def __exit__(self, typ, value, tb):
        if value is not None:
            print("Uncaught exception")
            raise _QuietException


class HTTP1ConnectionParameters(object):
    def __init__(self, no_keep_alive=False, chunk_size=None,
                 max_header_size=None, header_timeout=None, max_body_size=None,
                 body_timeout=None, decompress=False):
        self.no_keep_alive = no_keep_alive
        self.chunk_size = chunk_size or 65536
        self.max_header_size = max_header_size or 65536
        self.header_timeout = header_timeout
        self.max_body_size = max_body_size
        self.body_timeout = body_timeout
        self.decompress = decompress

class HTTP1Connection(httputil.HTTPConnection):
    def __init__(self, stream, is_client, params=None, context=None):
        self.is_client = is_client
        self.stream = stream
        if params is None:
            params = HTTP1ConnectionParameters()
        self.params = params
        self.context = context
        self.no_keep_alive = params.no_keep_alive
        self._max_body_size = (self.params.max_body_size or
                               self.stream.max_buffer_size)
        self._body_timeout = self.params.body_timeout
        self._write_finished = False
        self._read_finished = False
        self._finish_future = Future()
        self._disconnect_on_finish = False
        self._clear_callbacks()
        self._request_start_line = None
        self._response_start_line = None
        self._request_headers = None
        self._chunking_output = None
        self._expected_content_remaining = None
        self._pending_write = None

    def read_response(self, delegate):
        return self._read_message(delegate)

    @gen.coroutine
    def _read_message(self, delegate):
        need_delegate_close = False
        try:
            header_future = self.stream.read_until_regex(
                b"\r?\n\r?\n",
                max_bytes=self.params.max_header_size)
            if self.params.header_timeout is None:
                header_data = yield header_future
            else:
                try:
                    header_data = yield gen.with_timeout(
                        self.stream.io_loop.time() + self.params.header_timeout,
                        header_future,
                        io_loop=self.stream.io_loop,
                        quiet_exceptions=iostream.StreamClosedError)
                except gen.TimeoutError:
                    self.close()
                    raise gen.Return(False)
            start_line, headers = self._parse_headers(header_data)
            if self.is_client:
                start_line = httputil.parse_response_start_line(start_line)
                self._response_start_line = start_line
            else:
                start_line = httputil.parse_request_start_line(start_line)
                self._request_start_line = start_line
                self._request_headers = headers

            self._disconnect_on_finish = not self._can_keep_alive(
                start_line, headers)
            need_delegate_close = True
            with _ExceptionLoggingContext():
                header_future = delegate.headers_received(start_line, headers)
                if header_future is not None:
                    yield header_future
            if self.stream is None:
                # We've been detached.
                need_delegate_close = False
                raise gen.Return(False)
            skip_body = False
            if self.is_client:
                if (self._request_start_line is not None and
                        self._request_start_line.method == 'HEAD'):
                    skip_body = True
                code = start_line.code
                if code == 304:
                    skip_body = True
                if code >= 100 and code < 200:
                    if ('Content-Length' in headers or
                            'Transfer-Encoding' in headers):
                        raise httputil.HTTPInputError(
                            "Response code %d cannot have body" % code)
                    yield self._read_message(delegate)
            else:
                if (headers.get("Expect") == "100-continue" and
                        not self._write_finished):
                    self.stream.write(b"HTTP/1.1 100 (Continue)\r\n\r\n")
            if not skip_body:
                body_future = self._read_body(
                    start_line.code if self.is_client else 0, headers, delegate)
                if body_future is not None:
                    if self._body_timeout is None:
                        yield body_future
                    else:
                        try:
                            yield gen.with_timeout(
                                self.stream.io_loop.time() + self._body_timeout,
                                body_future, self.stream.io_loop,
                                quiet_exceptions=iostream.StreamClosedError)
                        except gen.TimeoutError:
                            print("Timeout reading body from %s" % self.context)
                            self.stream.close()
                            raise gen.Return(False)
            self._read_finished = True
            if not self._write_finished or self.is_client:
                need_delegate_close = False
                with _ExceptionLoggingContext():
                    delegate.finish()
            if (not self._finish_future.done() and
                    self.stream is not None and
                    not self.stream.closed()):
                self.stream.set_close_callback(self._on_connection_close)
                yield self._finish_future
            if self.is_client and self._disconnect_on_finish:
                self.close()
            if self.stream is None:
                raise gen.Return(False)
        except httputil.HTTPInputError as e:
            print("Malformed HTTP message from %s: %s", (self.context, e))
            self.close()
            raise gen.Return(False)
        finally:
            if need_delegate_close:
                with _ExceptionLoggingContext():
                    delegate.on_connection_close()
            self._clear_callbacks()
        raise gen.Return(True)

    def _clear_callbacks(self):
        self._write_callback = None
        self._write_future = None
        self._close_callback = None
        if self.stream is not None:
            self.stream.set_close_callback(None)

    def set_close_callback(self, callback):
        self._close_callback = stack_context.wrap(callback)

    def _on_connection_close(self):
        if self._close_callback is not None:
            callback = self._close_callback
            self._close_callback = None
            callback()
        if not self._finish_future.done():
            self._finish_future.set_result(None)
        self._clear_callbacks()

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self._clear_callbacks()
        if not self._finish_future.done():
            self._finish_future.set_result(None)

    def detach(self):
        self._clear_callbacks()
        stream = self.stream
        self.stream = None
        if not self._finish_future.done():
            self._finish_future.set_result(None)
        return stream

    def set_body_timeout(self, timeout):
        self._body_timeout = timeout

    def set_max_body_size(self, max_body_size):
        self._max_body_size = max_body_size

    def write_headers(self, start_line, headers, chunk=None, callback=None):
        lines = []
        if self.is_client:
            self._request_start_line = start_line
            lines.append(utf8('%s %s HTTP/1.1' % (start_line[0], start_line[1])))
            self._chunking_output = (
                start_line.method in ('POST', 'PUT', 'PATCH') and
                'Content-Length' not in headers and
                'Transfer-Encoding' not in headers)
        else:
            self._response_start_line = start_line
            lines.append(utf8('HTTP/1.1 %s %s' % (start_line[1], start_line[2])))
            self._chunking_output = (
                self._request_start_line.version == 'HTTP/1.1' and
                start_line.code != 304 and
                'Content-Length' not in headers and
                'Transfer-Encoding' not in headers)
            if (self._request_start_line.version == 'HTTP/1.0' and
                (self._request_headers.get('Connection', '').lower()
                 == 'keep-alive')):
                headers['Connection'] = 'Keep-Alive'
        if self._chunking_output:
            headers['Transfer-Encoding'] = 'chunked'
        if (not self.is_client and
            (self._request_start_line.method == 'HEAD' or
             start_line.code == 304)):
            self._expected_content_remaining = 0
        elif 'Content-Length' in headers:
            self._expected_content_remaining = int(headers['Content-Length'])
        else:
            self._expected_content_remaining = None
        lines.extend([utf8(n) + b": " + utf8(v) for n, v in headers.get_all()])
        for line in lines:
            if b'\n' in line:
                raise ValueError('Newline in header: ' + repr(line))
        future = None
        if self.stream.closed():
            future = self._write_future = Future()
            future.set_exception(iostream.StreamClosedError())
            future.exception()
        else:
            if callback is not None:
                self._write_callback = stack_context.wrap(callback)
            else:
                future = self._write_future = Future()
            data = b"\r\n".join(lines) + b"\r\n\r\n"
            if chunk:
                data += self._format_chunk(chunk)
            self._pending_write = self.stream.write(data)
            self._pending_write.add_done_callback(self._on_write_complete)
        return future

    def _format_chunk(self, chunk):
        if self._expected_content_remaining is not None:
            self._expected_content_remaining -= len(chunk)
            if self._expected_content_remaining < 0:
                # Close the stream now to stop further framing errors.
                self.stream.close()
                raise httputil.HTTPOutputError(
                    "Tried to write more data than Content-Length")
        if self._chunking_output and chunk:
            # Don't write out empty chunks because that means END-OF-STREAM
            # with chunked encoding
            return utf8("%x" % len(chunk)) + b"\r\n" + chunk + b"\r\n"
        else:
            return chunk

    def write(self, chunk, callback=None):
        future = None
        if self.stream.closed():
            future = self._write_future = Future()
            self._write_future.set_exception(iostream.StreamClosedError())
            self._write_future.exception()
        else:
            if callback is not None:
                self._write_callback = stack_context.wrap(callback)
            else:
                future = self._write_future = Future()
            self._pending_write = self.stream.write(self._format_chunk(chunk))
            self._pending_write.add_done_callback(self._on_write_complete)
        return future

    def finish(self):
        """Implements `.HTTPConnection.finish`."""
        if (self._expected_content_remaining is not None and
                self._expected_content_remaining != 0 and
                not self.stream.closed()):
            self.stream.close()
            raise httputil.HTTPOutputError(
                "Tried to write %d bytes less than Content-Length" %
                self._expected_content_remaining)
        if self._chunking_output:
            if not self.stream.closed():
                self._pending_write = self.stream.write(b"0\r\n\r\n")
                self._pending_write.add_done_callback(self._on_write_complete)
        self._write_finished = True
        if not self._read_finished:
            self._disconnect_on_finish = True
        # No more data is coming, so instruct TCP to send any remaining
        # data immediately instead of waiting for a full packet or ack.
        self.stream.set_nodelay(True)
        if self._pending_write is None:
            self._finish_request(None)
        else:
            self._pending_write.add_done_callback(self._finish_request)

    def _on_write_complete(self, future):
        exc = future.exception()
        if exc is not None and not isinstance(exc, iostream.StreamClosedError):
            future.result()
        if self._write_callback is not None:
            callback = self._write_callback
            self._write_callback = None
            self.stream.io_loop.add_callback(callback)
        if self._write_future is not None:
            future = self._write_future
            self._write_future = None
            future.set_result(None)

    def _can_keep_alive(self, start_line, headers):
        if self.params.no_keep_alive:
            return False
        connection_header = headers.get("Connection")
        if connection_header is not None:
            connection_header = connection_header.lower()
        if start_line.version == "HTTP/1.1":
            return connection_header != "close"
        elif ("Content-Length" in headers
              or headers.get("Transfer-Encoding", "").lower() == "chunked"
              or start_line.method in ("HEAD", "GET")):
            return connection_header == "keep-alive"
        return False

    def _finish_request(self, future):
        self._clear_callbacks()
        if not self.is_client and self._disconnect_on_finish:
            self.close()
            return
        self.stream.set_nodelay(False)
        if not self._finish_future.done():
            self._finish_future.set_result(None)

    def _parse_headers(self, data):
        data = native_str(data.decode('latin1')).lstrip("\r\n")
        # RFC 7230 section allows for both CRLF and bare LF.
        eol = data.find("\n")
        start_line = data[:eol].rstrip("\r")
        try:
            headers = httputil.HTTPHeaders.parse(data[eol:])
        except ValueError:
            # probably form split() if there was no ':' in the line
            raise httputil.HTTPInputError("Malformed HTTP headers: %r" %
                                          data[eol:100])
        return start_line, headers

    def _read_body(self, code, headers, delegate):
        if "Content-Length" in headers:
            if "," in headers["Content-Length"]:
                pieces = re.split(r',\s*', headers["Content-Length"])
                if any(i != pieces[0] for i in pieces):
                    raise httputil.HTTPInputError(
                        "Multiple unequal Content-Lengths: %r" %
                        headers["Content-Length"])
                headers["Content-Length"] = pieces[0]
            content_length = int(headers["Content-Length"])

            if content_length > self._max_body_size:
                raise httputil.HTTPInputError("Content-Length too long")
        else:
            content_length = None

        if code == 204:
            if ("Transfer-Encoding" in headers or
                    content_length not in (None, 0)):
                raise httputil.HTTPInputError(
                    "Response with code %d should not have body" % code)
            content_length = 0

        if content_length is not None:
            return self._read_fixed_body(content_length, delegate)
        if headers.get("Transfer-Encoding") == "chunked":
            return self._read_chunked_body(delegate)
        if self.is_client:
            return self._read_body_until_close(delegate)
        return None

    @gen.coroutine
    def _read_fixed_body(self, content_length, delegate):
        while content_length > 0:
            body = yield self.stream.read_bytes(
                min(self.params.chunk_size, content_length), partial=True)
            content_length -= len(body)
            if not self._write_finished or self.is_client:
                with _ExceptionLoggingContext():
                    yield gen.maybe_future(delegate.data_received(body))

    @gen.coroutine
    def _read_chunked_body(self, delegate):
        # TODO: "chunk extensions" http://tools.ietf.org/html/rfc2616#section-3.6.1
        total_size = 0
        while True:
            chunk_len = yield self.stream.read_until(b"\r\n", max_bytes=64)
            chunk_len = int(chunk_len.strip(), 16)
            if chunk_len == 0:
                return
            total_size += chunk_len
            if total_size > self._max_body_size:
                raise httputil.HTTPInputError("chunked body too large")
            bytes_to_read = chunk_len
            while bytes_to_read:
                chunk = yield self.stream.read_bytes(
                    min(bytes_to_read, self.params.chunk_size), partial=True)
                bytes_to_read -= len(chunk)
                if not self._write_finished or self.is_client:
                    with _ExceptionLoggingContext():
                        yield gen.maybe_future(delegate.data_received(chunk))
            # chunk ends with \r\n
            crlf = yield self.stream.read_bytes(2)
            assert crlf == b"\r\n"

    @gen.coroutine
    def _read_body_until_close(self, delegate):
        body = yield self.stream.read_until_close()
        if not self._write_finished or self.is_client:
            with _ExceptionLoggingContext():
                delegate.data_received(body)


class HTTP1ServerConnection(object):
    """An HTTP/1.x server."""
    def __init__(self, stream, params=None, context=None):
        self.stream = stream
        if params is None:
            params = HTTP1ConnectionParameters()
        self.params = params
        self.context = context
        self._serving_future = None

    @gen.coroutine
    def close(self):
        self.stream.close()
        try:
            yield self._serving_future
        except Exception:
            pass

    def start_serving(self, delegate):
        assert isinstance(delegate, httputil.HTTPServerConnectionDelegate)
        self._serving_future = self._server_request_loop(delegate)
        self.stream.io_loop.add_future(self._serving_future,
                                       lambda f: f.result())

    @gen.coroutine
    def _server_request_loop(self, delegate):
        try:
            while True:
                conn = HTTP1Connection(self.stream, False,
                                       self.params, self.context)
                request_delegate = delegate.start_request(self, conn)
                try:
                    ret = yield conn.read_response(request_delegate)
                except (iostream.StreamClosedError,
                        iostream.UnsatisfiableReadError):
                    return
                except _QuietException:
                    conn.close()
                    return
                except Exception:
                    print("Uncaught exception")
                    conn.close()
                    return
                if not ret:
                    return
                yield gen.moment
        finally:
            delegate.on_close(self)
