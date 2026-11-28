"""Client HTTP minimal (bibliothèque standard), sans suivi automatique des redirections."""
import json
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        try:
            return json.loads(self.body)
        except Exception:
            return None

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


def request(method, url, headers=None, json_body=None, form=None, timeout=30):
    h = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        h.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        r = _opener.open(req, timeout=timeout)
        return Resp(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return Resp(e.code, dict(e.headers), e.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return Resp(0, {}, str(e).encode())
