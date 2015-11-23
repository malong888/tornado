from __future__ import absolute_import, division, print_function, with_statement

import socket

from tornado.escape import native_str
from tornado.http1connection import HTTP1ServerConnection, HTTP1ConnectionParameters
from tornado import gen
from tornado import httputil
from tornado import iostream
from tornado.tcpserver import TCPServer
from tornado.util import Configurable


class HTTPServer(TCPServer, Configurable,
                 httputil.HTTPServerConnectionDelegate):
    def __init__(self, *args, **kwargs):
        pass

    def initialize(self, request_callback, no_keep_alive=False, io_loop=None,
                   xheaders=False, protocol=None,
                   decompress_request=False,
                   chunk_size=None, max_header_size=None,
                   idle_connection_timeout=None, body_timeout=None,
                   max_body_size=None, max_buffer_size=None):
        self.request_callback = request_callback
        self.no_keep_alive = no_keep_alive
        self.xheaders = xheaders
        self.protocol = protocol
        self.conn_params = HTTP1ConnectionParameters(
            decompress=decompress_request,
            chunk_size=chunk_size,
            max_header_size=max_header_size,
            header_timeout=idle_connection_timeout or 3600,
            max_body_size=max_body_size,
            body_timeout=body_timeout)
        TCPServer.__init__(self, io_loop=io_loop, 
                           max_buffer_size=max_buffer_size,
                           read_chunk_size=chunk_size)
        self._connections = set()

    @classmethod
    def configurable_base(cls):
        return HTTPServer

    @classmethod
    def configurable_default(cls):
        return HTTPServer

    @gen.coroutine
    def close_all_connections(self):
        while self._connections:
            conn = next(iter(self._connections))
            yield conn.close()

    def handle_stream(self, stream, address):
        context = _HTTPRequestContext(stream, address,
                                      self.protocol)
        conn = HTTP1ServerConnection(
            stream, self.conn_params, context)
        self._connections.add(conn)
        conn.start_serving(self)

    def start_request(self, server_conn, request_conn):
        return _ServerRequestAdapter(self, server_conn, request_conn)

    def on_close(self, server_conn):
        self._connections.remove(server_conn)


class _HTTPRequestContext(object):
    def __init__(self, stream, address, protocol):
        self.address = address
        self.protocol = protocol
        if stream.socket is not None:
            self.address_family = stream.socket.family
        else:
            self.address_family = None
        if (self.address_family in (socket.AF_INET, socket.AF_INET6) and
                address is not None):
            self.remote_ip = address[0]
        else:
            self.remote_ip = '0.0.0.0'
        if protocol:
            self.protocol = protocol
        else:
            self.protocol = "http"
        self._orig_remote_ip = self.remote_ip
        self._orig_protocol = self.protocol

    def __str__(self):
        if self.address_family in (socket.AF_INET, socket.AF_INET6):
            return self.remote_ip
        elif isinstance(self.address, bytes):
            return native_str(self.address)
        else:
            return str(self.address)

    def _apply_xheaders(self, headers):
        ip = headers.get("X-Forwarded-For", self.remote_ip)
        ip = ip.split(',')[-1].strip()
        ip = headers.get("X-Real-Ip", ip)
        if is_valid_ip(ip):
            self.remote_ip = ip
        proto_header = headers.get(
            "X-Scheme", headers.get("X-Forwarded-Proto",
                                    self.protocol))
        if proto_header in ("http", "https"):
            self.protocol = proto_header

    def _unapply_xheaders(self):
        self.remote_ip = self._orig_remote_ip
        self.protocol = self._orig_protocol


class _ServerRequestAdapter(httputil.HTTPMessageDelegate):
    def __init__(self, server, server_conn, request_conn):
        self.server = server
        self.connection = request_conn
        self.request = None
        if isinstance(server.request_callback,
                      httputil.HTTPServerConnectionDelegate):
            self.delegate = server.request_callback.start_request(
                server_conn, request_conn)
            self._chunks = None
        else:
            self.delegate = None
            self._chunks = []

    def headers_received(self, start_line, headers):
        if self.server.xheaders:
            self.connection.context._apply_xheaders(headers)
        if self.delegate is None:
            self.request = httputil.HTTPServerRequest(
                connection=self.connection, start_line=start_line,
                headers=headers)
        else:
            return self.delegate.headers_received(start_line, headers)

    def data_received(self, chunk):
        if self.delegate is None:
            self._chunks.append(chunk)
        else:
            return self.delegate.data_received(chunk)

    def finish(self):
        if self.delegate is None:
            self.request.body = b''.join(self._chunks)
            self.request._parse_body()
            self.server.request_callback(self.request)
        else:
            self.delegate.finish()
        self._cleanup()

    def on_connection_close(self):
        if self.delegate is None:
            self._chunks = None
        else:
            self.delegate.on_connection_close()
        self._cleanup()

    def _cleanup(self):
        if self.server.xheaders:
            self.connection.context._unapply_xheaders()


HTTPRequest = httputil.HTTPServerRequest

def is_valid_ip(ip):
    if not ip or '\x00' in ip:
        return False
    try:
        res = socket.getaddrinfo(ip, 0, socket.AF_UNSPEC,
                                 socket.SOCK_STREAM,
                                 0, socket.AI_NUMERICHOST)
        return bool(res)
    except socket.gaierror as e:
        if e.args[0] == socket.EAI_NONAME:
            return False
        raise
    return True
