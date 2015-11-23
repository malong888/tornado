from __future__ import absolute_import, division, print_function, with_statement

import calendar
import collections
import copy
import datetime
import email.utils
import numbers
import re
import time

from tornado.escape import native_str, parse_qs_bytes, utf8
from tornado.util import ObjectDict

from httplib import responses  # py2
responses
from urllib import urlencode  # py2

_CRLF_RE = re.compile(r'\r?\n')

class _NormalizedHeaderCache(dict):
    def __init__(self, size):
        super(_NormalizedHeaderCache, self).__init__()
        self.size = size
        self.queue = collections.deque()

    def __missing__(self, key):
        normalized = "-".join([w.capitalize() for w in key.split("-")])
        self[key] = normalized
        self.queue.append(key)
        if len(self.queue) > self.size:
            old_key = self.queue.popleft()
            del self[old_key]
        return normalized

_normalized_headers = _NormalizedHeaderCache(1000)


class HTTPHeaders(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self)
        self._as_list = {}
        self._last_key = None
        if (len(args) == 1 and len(kwargs) == 0 and
                isinstance(args[0], HTTPHeaders)):
            for k, v in args[0].get_all():
                self.add(k, v)
        else:
            self.update(*args, **kwargs)

    def add(self, name, value):
        """Adds a new value for the given key."""
        norm_name = _normalized_headers[name]
        self._last_key = norm_name
        if norm_name in self:
            # bypass our override of __setitem__ since it modifies _as_list
            dict.__setitem__(self, norm_name,
                             native_str(self[norm_name]) + ',' +
                             native_str(value))
            self._as_list[norm_name].append(value)
        else:
            self[norm_name] = value

    def get_list(self, name):
        """Returns all values for the given header as a list."""
        norm_name = _normalized_headers[name]
        return self._as_list.get(norm_name, [])

    def get_all(self):
        for name, values in self._as_list.items():
            for value in values:
                yield (name, value)

    def parse_line(self, line):
        if line[0].isspace():
            # continuation of a multi-line header
            new_part = ' ' + line.lstrip()
            self._as_list[self._last_key][-1] += new_part
            dict.__setitem__(self, self._last_key,
                             self[self._last_key] + new_part)
        else:
            name, value = line.split(":", 1)
            self.add(name, value.strip())

    @classmethod
    def parse(cls, headers):
        h = cls()
        for line in _CRLF_RE.split(headers):
            if line:
                h.parse_line(line)
        return h

    # dict implementation overrides

    def __setitem__(self, name, value):
        norm_name = _normalized_headers[name]
        dict.__setitem__(self, norm_name, value)
        self._as_list[norm_name] = [value]

    def __getitem__(self, name):
        return dict.__getitem__(self, _normalized_headers[name])

    def __delitem__(self, name):
        norm_name = _normalized_headers[name]
        dict.__delitem__(self, norm_name)
        del self._as_list[norm_name]

    def __contains__(self, name):
        norm_name = _normalized_headers[name]
        return dict.__contains__(self, norm_name)

    def get(self, name, default=None):
        return dict.get(self, _normalized_headers[name], default)

    def update(self, *args, **kwargs):
        for k, v in dict(*args, **kwargs).items():
            self[k] = v

    def copy(self):
        return HTTPHeaders(self)

    __copy__ = copy

    def __deepcopy__(self, memo_dict):
        return self.copy()


class HTTPServerRequest(object):
    def __init__(self, method=None, uri=None, version="HTTP/1.0", headers=None,
                 body=None, host=None, files=None, connection=None,
                 start_line=None):
        if start_line is not None:
            method, uri, version = start_line
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers or HTTPHeaders()
        self.body = body or b""

        # set remote IP and protocol
        context = getattr(connection, 'context', None)
        self.remote_ip = getattr(context, 'remote_ip', None)
        self.protocol = getattr(context, 'protocol', "http")

        self.host = host or self.headers.get("Host") or "127.0.0.1"
        self.files = files or {}
        self.connection = connection
        self._start_time = time.time()
        self._finish_time = None

        self.path, sep, self.query = uri.partition('?')
        self.arguments = parse_qs_bytes(self.query, keep_blank_values=True)
        self.query_arguments = copy.deepcopy(self.arguments)
        self.body_arguments = {}

    def write(self, chunk, callback=None):
        assert isinstance(chunk, bytes)
        assert self.version.startswith("HTTP/1."), \
            "deprecated interface only supported in HTTP/1.x"
        self.connection.write(chunk, callback=callback)

    def finish(self):
        self.connection.finish()
        self._finish_time = time.time()

    def full_url(self):
        """Reconstructs the full URL for this request."""
        return self.protocol + "://" + self.host + self.uri

    def request_time(self):
        """Returns the amount of time it took for this request to execute."""
        if self._finish_time is None:
            return time.time() - self._start_time
        else:
            return self._finish_time - self._start_time

    def _parse_body(self):
        parse_body_arguments(
            self.headers.get("Content-Type", ""), self.body,
            self.body_arguments, self.files,
            self.headers)

        for k, v in self.body_arguments.items():
            self.arguments.setdefault(k, []).extend(v)

    def __repr__(self):
        attrs = ("protocol", "host", "method", "uri", "version", "remote_ip")
        args = ", ".join(["%s=%r" % (n, getattr(self, n)) for n in attrs])
        return "%s(%s, headers=%s)" % (
            self.__class__.__name__, args, dict(self.headers))


class HTTPInputError(Exception):
    pass


class HTTPOutputError(Exception):
    pass


class HTTPServerConnectionDelegate(object):
    def start_request(self, server_conn, request_conn):
        raise NotImplementedError()

    def on_close(self, server_conn):
        pass


class HTTPMessageDelegate(object):
    def headers_received(self, start_line, headers):
        pass

    def data_received(self, chunk):
        pass

    def finish(self):
        pass

    def on_connection_close(self):
        pass


class HTTPConnection(object):
    def write_headers(self, start_line, headers, chunk=None, callback=None):
        raise NotImplementedError()

    def write(self, chunk, callback=None):
        raise NotImplementedError()

    def finish(self):
        raise NotImplementedError()


def url_concat(url, args):
    if not args:
        return url
    if url[-1] not in ('?', '&'):
        url += '&' if ('?' in url) else '?'
    return url + urlencode(args)

class HTTPFile(ObjectDict):
    pass

def _parse_request_range(range_header):
    unit, _, value = range_header.partition("=")
    unit, value = unit.strip(), value.strip()
    if unit != "bytes":
        return None
    start_b, _, end_b = value.partition("-")
    try:
        start = _int_or_none(start_b)
        end = _int_or_none(end_b)
    except ValueError:
        return None
    if end is not None:
        if start is None:
            if end != 0:
                start = -end
                end = None
        else:
            end += 1
    return (start, end)


def _get_content_range(start, end, total):
    start = start or 0
    end = (end or total) - 1
    return "bytes %s-%s/%s" % (start, end, total)


def _int_or_none(val):
    val = val.strip()
    if val == "":
        return None
    return int(val)


def parse_body_arguments(content_type, body, arguments, files, headers=None):
    if headers and 'Content-Encoding' in headers:
        print("Unsupported Content-Encoding: %s" % headers['Content-Encoding'])
        return
    if content_type.startswith("application/x-www-form-urlencoded"):
        try:
            uri_arguments = parse_qs_bytes(native_str(body), keep_blank_values=True)
        except Exception as e:
            print('Invalid x-www-form-urlencoded body: %s' % e)
            uri_arguments = {}
        for name, values in uri_arguments.items():
            if values:
                arguments.setdefault(name, []).extend(values)
    elif content_type.startswith("multipart/form-data"):
        try:
            fields = content_type.split(";")
            for field in fields:
                k, sep, v = field.strip().partition("=")
                if k == "boundary" and v:
                    parse_multipart_form_data(utf8(v), body, arguments, files)
                    break
            else:
                raise ValueError("multipart boundary not found")
        except Exception as e:
            print("Invalid multipart/form-data: %s" % e)


def parse_multipart_form_data(boundary, data, arguments, files):
    if boundary.startswith(b'"') and boundary.endswith(b'"'):
        boundary = boundary[1:-1]
    final_boundary_index = data.rfind(b"--" + boundary + b"--")
    if final_boundary_index == -1:
        print("Invalid multipart/form-data: no final boundary")
        return
    parts = data[:final_boundary_index].split(b"--" + boundary + b"\r\n")
    for part in parts:
        if not part:
            continue
        eoh = part.find(b"\r\n\r\n")
        if eoh == -1:
            print("multipart/form-data missing headers")
            continue
        headers = HTTPHeaders.parse(part[:eoh].decode("utf-8"))
        disp_header = headers.get("Content-Disposition", "")
        disposition, disp_params = _parse_header(disp_header)
        print("disposition: ", disposition)
        if disposition != "form-data" or not part.endswith(b"\r\n"):
            print("Invalid multipart/form-data")
            continue
        print("disp_params: ", disp_params)
        value = part[eoh + 4:-2]
        if not disp_params.get("name"):
            print("multipart/form-data value missing name")
            continue
        name = disp_params["name"]
        if disp_params.get("filename"):
            ctype = headers.get("Content-Type", "application/unknown")
            files.setdefault(name, []).append(HTTPFile(
                filename=disp_params["filename"], body=value,
                content_type=ctype))
        else:
            arguments.setdefault(name, []).append(value)


def format_timestamp(ts):
    if isinstance(ts, numbers.Real):
        pass
    elif isinstance(ts, (tuple, time.struct_time)):
        ts = calendar.timegm(ts)
    elif isinstance(ts, datetime.datetime):
        ts = calendar.timegm(ts.utctimetuple())
    else:
        raise TypeError("unknown timestamp type: %r" % ts)
    return email.utils.formatdate(ts, usegmt=True)


RequestStartLine = collections.namedtuple(
    'RequestStartLine', ['method', 'path', 'version'])


def parse_request_start_line(line):
    try:
        method, path, version = line.split(" ")
    except ValueError:
        raise HTTPInputError("Malformed HTTP request line")
    if not re.match(r"^HTTP/1\.[0-9]$", version):
        raise HTTPInputError(
            "Malformed HTTP version in HTTP Request-Line: %r" % version)
    return RequestStartLine(method, path, version)


ResponseStartLine = collections.namedtuple(
    'ResponseStartLine', ['version', 'code', 'reason'])


def parse_response_start_line(line):
    line = native_str(line)
    match = re.match("(HTTP/1.[0-9]) ([0-9]+) ([^\r]*)", line)
    if not match:
        raise HTTPInputError("Error parsing response start line")
    return ResponseStartLine(match.group(1), int(match.group(2)),
                             match.group(3))


def _parseparam(s):
    while s[:1] == ';':
        s = s[1:]
        end = s.find(';')
        while end > 0 and (s.count('"', 0, end) - s.count('\\"', 0, end)) % 2:
            end = s.find(';', end + 1)
        if end < 0:
            end = len(s)
        f = s[:end]
        yield f.strip()
        s = s[end:]


def _parse_header(line):
    parts = _parseparam(';' + line)
    key = next(parts)
    pdict = {}
    for p in parts:
        i = p.find('=')
        if i >= 0:
            name = p[:i].strip().lower()
            value = p[i + 1:].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
                value = value.replace('\\\\', '\\').replace('\\"', '"')
            pdict[name] = value
        else:
            pdict[p] = None
    return key, pdict


def _encode_header(key, pdict):
    if not pdict:
        return key
    out = [key]
    # Sort the parameters just to make it easy to test.
    for k, v in sorted(pdict.items()):
        if v is None:
            out.append(k)
        else:
            # TODO: quote if necessary.
            out.append('%s=%s' % (k, v))
    return '; '.join(out)

def split_host_and_port(netloc):
    match = re.match(r'^(.+):(\d+)$', netloc)
    if match:
        host = match.group(1)
        port = int(match.group(2))
    else:
        host = netloc
        port = None
    return (host, port)
