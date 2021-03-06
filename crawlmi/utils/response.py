import os
import re
import tempfile
import urlparse
import webbrowser

from twisted.web.http import RESPONSES

from crawlmi.http import HtmlResponse, TextResponse
from crawlmi.utils.html import remove_entities
from crawlmi.utils.python import to_str
from crawlmi.utils.regex import (html_script_re, html_noscript_re,
                                 html_comment_re)
from crawlmi.utils.url import requote_url


def response_http_repr(response):
    '''Return raw HTTP representation (as string) of the given response. This
    is provided only for reference, since it's not the exact stream of bytes
    that was received (that's not exposed by Twisted).
    '''

    s = 'HTTP/1.1 %d %s\r\n' % (response.status, RESPONSES.get(response.status, ''))
    if response.headers:
        s += response.headers.to_string() + '\r\n'
    s += '\r\n'
    s += response.body
    return s


def open_in_browser(response, _openfunc=webbrowser.open):
    '''Open the given response in a local web browser, populating the <base>
    tag for external links to work.
    '''
    body = response.body
    if isinstance(response, HtmlResponse):
        if '<base' not in body:
            body = body.replace('<head>', '<head><base href="%s">' % response.url)
        ext = '.html'
    elif isinstance(response, TextResponse):
        ext = '.txt'
    else:
        raise TypeError('Unsupported response type: %s' %
                        response.__class__.__name__)
    fd, fname = tempfile.mkstemp(ext)
    os.write(fd, body)
    os.close(fd)
    return _openfunc('file://%s' % fname)


_meta_refresh_re = re.compile(ur'<meta[^>]*http-equiv[^>]*refresh[^>]*content\s*=\s*(?P<quote>["\'])(?P<int>(\d*\.)?\d+)\s*;\s*url=(?P<url>.*?)(?P=quote)', re.DOTALL | re.IGNORECASE)

def get_meta_refresh(response):
    '''Parse the http-equiv refrsh parameter from the given HTML response.
    Return tuple (interval, url).'''
    text = remove_entities(response.text[0:4096])
    text = html_comment_re.sub(u'', text)
    text = html_noscript_re.sub(u'', text)
    text = html_script_re.sub(u'', text)

    m = _meta_refresh_re.search(text)
    if m:
        interval = float(m.group('int'))
        url = requote_url(to_str(m.group('url').strip(' "\''), response.encoding))
        url = urlparse.urljoin(response.url, url)
        return (interval, url)
    else:
        return (None, None)
