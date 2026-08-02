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
    """Returns queued responses in order and records every call."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.queue.pop(0) if self.queue else FakeResponse(200, {})

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
