from __future__ import absolute_import, division, print_function, with_statement

import re
import sys
from tornado.util import unicode_type, basestring_type
from urlparse import parse_qs as _parse_qs  # Python 2.6+
import urllib as urllib_parse  # py2
import json

def json_encode(value):
    return json.dumps(value).replace("</", "<\\/")

def json_decode(value):
    """Returns Python objects for the given JSON string."""
    return json.loads(to_basestring(value))

def url_escape(value, plus=True):
    quote = urllib_parse.quote_plus if plus else urllib_parse.quote
    return quote(utf8(value))

def url_unescape(value, encoding='utf-8', plus=True):
    unquote = (urllib_parse.unquote_plus if plus else urllib_parse.unquote)
    if encoding is None:
        return unquote(utf8(value))
    else:
        return unicode_type(unquote(utf8(value)), encoding)

parse_qs_bytes = _parse_qs

_UTF8_TYPES = (bytes, type(None))

def utf8(value):
    if isinstance(value, _UTF8_TYPES):
        return value
    if not isinstance(value, unicode_type):
        raise TypeError(
            "Expected bytes, unicode, or None; got %r" % type(value)
        )
    return value.encode("utf-8")

_TO_UNICODE_TYPES = (unicode_type, type(None))

def to_unicode(value):
    if isinstance(value, _TO_UNICODE_TYPES):
        return value
    if not isinstance(value, bytes):
        raise TypeError(
            "Expected bytes, unicode, or None; got %r" % type(value)
        )
    return value.decode("utf-8")

_unicode = to_unicode

if str is unicode_type:
    native_str = to_unicode
else:
    native_str = utf8

_BASESTRING_TYPES = (basestring_type, type(None))

def to_basestring(value):
    if isinstance(value, _BASESTRING_TYPES):
        return value
    if not isinstance(value, bytes):
        raise TypeError(
            "Expected bytes, unicode, or None; got %r" % type(value)
        )
    return value.decode("utf-8")
