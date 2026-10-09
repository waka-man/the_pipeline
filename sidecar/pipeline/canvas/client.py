"""Canvas REST client.

Carries forward the hard-won behaviour from `grader/grader_v2/grader/canvas_client.py`:
browser-shaped headers and jittered exponential backoff on 403/429, because
Canvas behind CloudFront returns a raw edge/WAF block as a plain 403 that is
indistinguishable from a permission error at the status-code level.

Unlike the original this does not shell out to `curl_cffi`: plain `requests`
with a browser User-Agent is accepted by this instance. `probe()` re-checks
that at runtime instead of assuming it.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable, Iterator, TypeVar

import requests

T = TypeVar("T")

RETRY_DELAYS: tuple[float, ...] = (5.0, 10.0, 30.0)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}


class CanvasError(RuntimeError):
    pass


def _fingerprint(payload: list) -> str:
    """Cheap identity for a page of results, used to detect no-progress loops."""
    try:
        ids = [item.get("id") for item in payload if isinstance(item, dict)]
        if ids and all(i is not None for i in ids):
            return ",".join(str(i) for i in ids)
        return str(len(payload)) + ":" + repr(payload[:1])
    except Exception:
        return repr(payload[:2])


def _next_link(resp: Any) -> str | None:
    """The `rel="next"` URL from a response's Link header, if present."""
    for part in (resp.headers.get("Link") or "").split(","):
        if 'rel="next"' in part:
            start, end = part.find("<"), part.find(">")
            if 0 <= start < end:
                return part[start + 1 : end]
    return None


class AuthError(CanvasError):
    pass


class NotFoundError(CanvasError):
    pass


class CanvasClient:
    def __init__(self, base_url: str, token: str, timeout: int = 90,
                 max_attempts: int = 4):
        if not base_url or not token:
            raise AuthError("canvas base_url and api_token are required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)
        self.session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------ plumbing

    def _url(self, path: str) -> str:
        # Pagination hands back absolute next-page URLs; prefixing those again
        # would produce .../api/v1https://host/api/v1/...
        if path.startswith("http"):
            return path
        return f"{self.base_url}/api/v1{path}"

    def request(self, method: str, path: str, **kw: Any) -> Any:
        """One request with jittered backoff. Raises AuthError on hard 401."""
        url = path if path.startswith("http") else self._url(path)
        last: Exception | None = None
        delays = (0.0, *RETRY_DELAYS)
        for attempt, delay in enumerate(delays[: self.max_attempts]):
            if delay:
                time.sleep(delay + random.uniform(0, delay * 0.2))
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as exc:
                last = exc
                continue
            if resp.status_code in RETRYABLE_STATUS:
                last = CanvasError(f"{method} {path} -> {resp.status_code}")
                continue
            self._raise_for_status(resp, method, path)
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
        raise CanvasError(
            f"{method} {path} failed after {self.max_attempts} attempts "
            f"(likely Canvas/CloudFront throttling): {last}"
        )

    @staticmethod
    def _raise_for_status(resp: Any, method: str, path: str) -> None:
        if resp.status_code == 401:
            raise AuthError(f"Canvas rejected the token (401) on {method} {path}. "
                            "It may have been revoked or rotated.")
        if resp.status_code == 404:
            raise NotFoundError(f"{method} {path} -> 404")
        if not resp.ok:
            raise CanvasError(
                f"{method} {path} -> {resp.status_code}: {getattr(resp, 'text', '')[:300]}")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def put(self, path: str, **kw: Any) -> Any:
        return self.request("PUT", path, **kw)

    def paginate(self, path: str, per_page: int = 100,
                 include: Iterable[str] = ()) -> Iterator[dict[str, Any]]:
        """Follow Canvas pagination until the collection is exhausted.

        `include` names Canvas associations to embed; it becomes repeated
        `include[]=` query parameters, not a request kwarg.

        The `Link` header lives on the *response*. Reading it from the session
        — which is where an earlier version looked — silently yields only the
        first page whenever an endpoint does paginate, which for a large cohort
        looks exactly like a smaller roster.
        """
        query = [f"per_page={per_page}"]
        query += [f"include[]={i}" for i in include]
        sep = "&" if "?" in path else "?"
        url: str | None = f"{path}{sep}{'&'.join(query)}"
        seen: set[str] = set()
        fingerprint: str | None = None

        while url:
            if url in seen:
                # A `rel="next"` that does not advance would loop forever.
                break
            seen.add(url)
            resp = self.session.request("GET", self._url(url), timeout=self.timeout)
            self._raise_for_status(resp, "GET", url)
            if resp.status_code == 204 or not resp.content:
                return
            try:
                payload = resp.json()
            except ValueError:
                return
            if not isinstance(payload, list):
                return
            # Guard on progress, not just on the URL: a server can advertise a
            # next page formatted differently while returning identical rows,
            # and comparing strings alone would spin forever.
            mark = _fingerprint(payload)
            if mark == fingerprint:
                return
            fingerprint = mark
            yield from payload
            url = _next_link(resp)

    def download(self, url: str, dest, max_bytes: int = 200 * 1024 * 1024) -> int:
        """Fetch a Canvas file URL with the bearer token. Returns bytes written."""
        resp = self.session.get(url, timeout=self.timeout, stream=True)
        if not resp.ok:
            raise CanvasError(f"download {url} -> {resp.status_code}")
        written = 0
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise CanvasError(f"{url} exceeded {max_bytes} bytes")
                fh.write(chunk)
        return written

    # ------------------------------------------------------------- domain

    def probe(self) -> dict[str, Any]:
        """Verify credentials and return the current user."""
        return self.get("/users/self")

    def list_courses(self) -> list[dict[str, Any]]:
        return list(self.paginate("/courses", include="term"))

    def get_assignment(self, course_id: int, assignment_id: int) -> dict[str, Any]:
        return self.get(f"/courses/{course_id}/assignments/{assignment_id}?include[]=rubric")

    def get_submissions(self, course_id: int, assignment_id: int,
                        grouped: bool = False) -> list[dict[str, Any]]:
        path = f"/courses/{course_id}/assignments/{assignment_id}/submissions"
        if grouped:
            path += "?grouped=true"
        return list(self.paginate(path, include=["user", "group"]))

    def course_students(self, course_id: int) -> list[dict[str, Any]]:
        return list(self.paginate(f"/courses/{course_id}/students",
                                  include=["email", "groups"]))

    def grade(self, course_id: int, assignment_id: int, user_id: int,
              posted_grade: float | None = None,
              rubric_assessment: dict[str, Any] | None = None,
              comment: str | None = None,
              excuse: bool | None = None) -> Any:
        body: dict[str, Any] = {}
        if posted_grade is not None:
            body["posted_grade"] = str(posted_grade)
        if rubric_assessment is not None:
            body["rubric_assessment"] = rubric_assessment
        if comment is not None:
            # Accept either a plain string or an already-shaped comment object.
            body["comment"] = (comment if isinstance(comment, dict)
                               else {"text_comment": comment})
        if excuse is not None:
            body["excuse"] = excuse
        if not body:
            raise CanvasError("nothing to publish")
        return self.put(f"/courses/{course_id}/assignments/{assignment_id}"
                        f"/submissions/{user_id}", json=body)

    def group_members(self, group_id: int) -> list[int]:
        """User IDs in a Canvas group."""
        try:
            group = self.get(f"/groups/{group_id}")
        except NotFoundError:
            return []
        return [u["id"] for u in group.get("users") or []]