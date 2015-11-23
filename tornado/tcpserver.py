"""A non-blocking, single-threaded TCP server."""
from __future__ import absolute_import, division, print_function, with_statement

import errno
import os
import socket

from tornado.ioloop import IOLoop
from tornado.iostream import IOStream
from tornado.util import errno_from_exception

class TCPServer(object):
    def __init__(self, io_loop=None, max_buffer_size=None,
                 read_chunk_size=None):
        self.io_loop = io_loop
        self._sockets = {}  # fd -> socket object
        self._pending_sockets = []
        self._started = False
        self.max_buffer_size = max_buffer_size
        self.read_chunk_size = read_chunk_size

    def listen(self, port, address=""):
        sockets = bind_sockets(port, address=address)
        self.add_sockets(sockets)

    def add_sockets(self, sockets):
        if self.io_loop is None:
            self.io_loop = IOLoop.current()

        for sock in sockets:
            self._sockets[sock.fileno()] = sock
            add_accept_handler(sock, self._handle_connection,
                               io_loop=self.io_loop)

    def add_socket(self, socket):
        self.add_sockets([socket])

    def bind(self, port, address=None, family=socket.AF_UNSPEC, backlog=128):
        sockets = bind_sockets(port, address=address, family=family,
                               backlog=backlog)
        if self._started:
            self.add_sockets(sockets)
        else:
            self._pending_sockets.extend(sockets)

    def start(self, num_processes=1):
        assert not self._started
        self._started = True
        if num_processes != 1:
            process.fork_processes(num_processes)
        sockets = self._pending_sockets
        self._pending_sockets = []
        self.add_sockets(sockets)

    def stop(self):
        for fd, sock in self._sockets.items():
            self.io_loop.remove_handler(fd)
            sock.close()

    def handle_stream(self, stream, address):
        raise NotImplementedError()

    def _handle_connection(self, connection, address):
        try:
            stream = IOStream(connection, io_loop=self.io_loop,
                              max_buffer_size=self.max_buffer_size,
                              read_chunk_size=self.read_chunk_size)
            future = self.handle_stream(stream, address)
            if future is not None:
                self.io_loop.add_future(future, lambda f: f.result())
        except Exception:
            print("Error in connection callback")

_DEFAULT_BACKLOG = 128
from tornado.platform.posix import set_close_exec
from tornado.util import Configurable, errno_from_exception

_ERRNO_WOULDBLOCK = (errno.EWOULDBLOCK, errno.EAGAIN)

def bind_sockets(port, address=None, family=socket.AF_UNSPEC,
                 backlog=_DEFAULT_BACKLOG, flags=None):
    sockets = []
    if address == "":
        address = None
    if flags is None:
        flags = socket.AI_PASSIVE
    bound_port = None
    for res in set(socket.getaddrinfo(address, port, family, socket.SOCK_STREAM,
                                      0, flags)):
        af, socktype, proto, canonname, sockaddr = res
        try:
            sock = socket.socket(af, socktype, proto)
        except socket.error as e:
            if errno_from_exception(e) == errno.EAFNOSUPPORT:
                continue
            raise
        set_close_exec(sock.fileno())
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if af == socket.AF_INET6:
            if hasattr(socket, "IPPROTO_IPV6"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

        host, requested_port = sockaddr[:2]
        if requested_port == 0 and bound_port is not None:
            sockaddr = tuple([host, bound_port] + list(sockaddr[2:]))

        sock.setblocking(0)
        sock.bind(sockaddr)
        bound_port = sock.getsockname()[1]
        sock.listen(backlog)
        sockets.append(sock)
    return sockets

def add_accept_handler(sock, callback, io_loop=None):
    if io_loop is None:
        io_loop = IOLoop.current()

    def accept_handler(fd, events):
        for i in xrange(_DEFAULT_BACKLOG):
            try:
                connection, address = sock.accept()
            except socket.error as e:
                if errno_from_exception(e) in _ERRNO_WOULDBLOCK:
                    return
                if errno_from_exception(e) == errno.ECONNABORTED:
                    continue
                raise
            callback(connection, address)
    io_loop.add_handler(sock, accept_handler, IOLoop.READ)
