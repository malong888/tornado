from __future__ import absolute_import, division, print_function, with_statement

import errno
import socket

from tornado.platform import interface


class Waker(interface.Waker):
    def __init__(self):
        self.writer = socket.socket()
        self.writer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        count = 0
        while 1:
            count += 1
            a = socket.socket()
            a.bind(("127.0.0.1", 0))
            a.listen(1)
            connect_address = a.getsockname()  # assigned (host, port) pair
            try:
                self.writer.connect(connect_address)
                break    # success
            except socket.error as detail:
                if (not hasattr(errno, 'WSAEADDRINUSE') or
                        detail[0] != errno.WSAEADDRINUSE):
                    raise
                if count >= 10:  # I've never seen it go above 2
                    a.close()
                    self.writer.close()
                    raise socket.error("Cannot bind trigger!")
                a.close()

        self.reader, addr = a.accept()
        self.reader.setblocking(0)
        self.writer.setblocking(0)
        a.close()
        self.reader_fd = self.reader.fileno()

    def fileno(self):
        return self.reader.fileno()

    def write_fileno(self):
        return self.writer.fileno()

    def wake(self):
        try:
            self.writer.send(b"x")
        except (IOError, socket.error):
            pass

    def consume(self):
        try:
            while True:
                result = self.reader.recv(1024)
                if not result:
                    break
        except (IOError, socket.error):
            pass

    def close(self):
        self.reader.close()
        self.writer.close()
