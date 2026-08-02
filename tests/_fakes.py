"""Shared test doubles: no network, no real tokens, no sleeping."""


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Returns queued responses in order and records every call.

    A queued item that is an exception instance is raised instead of
    returned, so callers can simulate a transport failure (connection
    error, timeout) without a separate mode flag or callback — just queue
    the exception where a response would otherwise go.
    """

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        # Recorded before the raise: a transport failure still means the
        # call was attempted, and retry-count assertions need it counted.
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.queue:
            return FakeResponse(200, {})
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, **kwargs)


class FakeProvider:
    def __init__(self, token="tok"):
        self.token = token
        self.invalidated = 0

    def access_token(self):
        return self.token

    def invalidate(self):
        self.invalidated += 1
        self.token = "fresh"


def indexed_payload(coverage="Submitted and indexed", verdict="PASS", crawl=None):
    result = {"coverageState": coverage, "verdict": verdict}
    if crawl:
        result["lastCrawlTime"] = crawl
    return {"inspectionResult": {"indexStatusResult": result}}
