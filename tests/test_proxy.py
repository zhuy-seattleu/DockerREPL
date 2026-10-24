"""Step 7: LMProxyHandler, the HTTP proxy between the container and LMHandler."""

import contextlib
import http.client
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import requests

from dockerrepl.clients import BaseLM, MockLM
from dockerrepl.environments.proxy import LMProxyHandler, make_handler_class, start_proxy_server
from dockerrepl.lm_handler import LMHandler
from test_exec_script import run_script  # step 6's helper: build and run an exec script


class PrefixLM(BaseLM):
    """Answers f"{prefix}:{prompt}", so a test can tell which handler answered."""

    def __init__(self, prefix: str, **kwargs: Any) -> None:
        super().__init__(model_name=f"prefix-{prefix}", **kwargs)
        self.prefix = prefix

    def _completion(self, prompt: Any) -> str:
        return f"{self.prefix}:{prompt}"


class PickyLM(BaseLM):
    """Fails on the prompt "boom"; echoes everything else."""

    def __init__(self) -> None:
        super().__init__(model_name="picky")

    def _completion(self, prompt: Any) -> str:
        if prompt == "boom":
            raise RuntimeError("boom")
        return f"ok:{prompt}"


class SlowLM(BaseLM):
    """Takes `delay` seconds per prompt."""

    def __init__(self, delay: float, **kwargs: Any) -> None:
        super().__init__(model_name="slow", **kwargs)
        self.delay = delay

    def _completion(self, prompt: Any) -> str:
        time.sleep(self.delay)
        return f"slow:{prompt}"


@contextlib.contextmanager
def proxy_for(lm: BaseLM) -> Iterator[tuple[SimpleNamespace, str, LMHandler]]:
    """Start an LMHandler for `lm` and a proxy pointing at it; yield (manager, url, handler)."""
    with LMHandler(lm) as handler:
        manager = SimpleNamespace(lm_handler_address=handler.address)
        server, thread = start_proxy_server(manager, "127.0.0.1", 0)
        try:
            yield manager, f"http://127.0.0.1:{server.server_address[1]}", handler
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture
def proxy() -> Iterator[tuple[SimpleNamespace, str, LMHandler]]:
    with proxy_for(MockLM()) as p:
        yield p


def post(url: str, path: str, body: Any = None, **kwargs: Any) -> requests.Response:
    return requests.post(url + path, json=body, timeout=5, **kwargs)


# ---------------------------------------------------------------- the two routes


def test_llm_query(proxy) -> None:
    _, url, _ = proxy
    r = post(url, "/llm_query", {"prompt": "hi"})
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/json"
    assert r.headers["Content-Length"] == str(len(r.content))
    assert r.json() == {"response": "Mock: hi"}


def test_llm_query_with_model(proxy) -> None:
    _, url, _ = proxy
    # model is passed along; LMHandler accepts it and ignores it (one LM client)
    assert post(url, "/llm_query", {"prompt": "hi", "model": "m"}).json() == {"response": "Mock: hi"}


def test_llm_query_dict_prompt(proxy) -> None:
    _, url, _ = proxy
    assert post(url, "/llm_query", {"prompt": {"role": "user"}}).json()["response"].startswith("Mock: ")


def test_llm_query_batched(proxy) -> None:
    _, url, _ = proxy
    r = post(url, "/llm_query_batched", {"prompts": ["a", "b"]})
    assert r.json() == {"responses": ["Mock: a", "Mock: b"]}


def test_empty_batch(proxy) -> None:
    _, url, _ = proxy
    assert post(url, "/llm_query_batched", {"prompts": []}).json() == {"responses": []}


def test_batched_error_slot() -> None:
    with proxy_for(PickyLM()) as (_, url, _):
        responses = post(url, "/llm_query_batched", {"prompts": ["a", "boom", "c"]}).json()["responses"]
        assert responses[0] == "ok:a" and responses[2] == "ok:c"
        assert responses[1].startswith("Error:") and "boom" in responses[1]


def test_lm_error_returned() -> None:
    with proxy_for(PickyLM()) as (_, url, _):
        data = post(url, "/llm_query", {"prompt": "boom"}).json()
        assert set(data) == {"error"} and "boom" in data["error"]


# ---------------------------------------------------------------- the handler address


def test_no_handler_configured(proxy) -> None:
    manager, url, _ = proxy
    manager.lm_handler_address = None
    assert post(url, "/llm_query", {"prompt": "hi"}).json() == {"error": "No LM handler configured"}
    assert post(url, "/llm_query_batched", {"prompts": ["a"]}).json() == {"error": "No LM handler configured"}


def test_address_is_read_live(proxy) -> None:
    manager, url, _ = proxy
    assert post(url, "/llm_query", {"prompt": "hi"}).json() == {"response": "Mock: hi"}
    with LMHandler(PrefixLM("B")) as second:
        manager.lm_handler_address = second.address  # a new round: no proxy restart
        assert post(url, "/llm_query", {"prompt": "hi"}).json() == {"response": "B:hi"}


def test_handler_gone_returns_error(proxy) -> None:
    _, url, handler = proxy
    handler.stop()
    r = post(url, "/llm_query", {"prompt": "hi"})
    assert r.status_code == 200
    assert r.json()["error"].startswith("Request failed")
    responses = post(url, "/llm_query_batched", {"prompts": ["a", "b"]}).json()["responses"]
    assert len(responses) == 2 and all(s.startswith("Error: Request failed") for s in responses)


# ---------------------------------------------------------------- bad requests


def test_unknown_path_404(proxy) -> None:
    _, url, _ = proxy
    r = post(url, "/nope", {"prompt": "hi"})
    assert r.status_code == 404 and "error" in r.json()


def test_bad_json_400(proxy) -> None:
    _, url, _ = proxy
    r = requests.post(url + "/llm_query", data="{not json", timeout=5)
    assert r.status_code == 400 and r.json()["error"].startswith("Bad request")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/llm_query", [1, 2]),  # not an object
        ("/llm_query", {}),  # no prompt
        ("/llm_query", {"prompt": 5}),
        ("/llm_query_batched", {"prompts": "a"}),  # a string, not a list
        ("/llm_query_batched", {"prompts": ["a", 5]}),
    ],
)
def test_invalid_body_400(proxy, path: str, body: Any) -> None:
    _, url, _ = proxy
    r = post(url, path, body)
    assert r.status_code == 400 and r.json()["error"].startswith("Bad request")


def test_missing_content_length_400(proxy) -> None:
    _, url, _ = proxy
    conn = http.client.HTTPConnection(url.removeprefix("http://"), timeout=5)
    conn.putrequest("POST", "/llm_query")
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400
    conn.close()


def test_owner_error_gives_500() -> None:
    class Broken:
        @property
        def lm_handler_address(self) -> Any:
            raise RuntimeError("no address for you")

    server, thread = start_proxy_server(Broken(), "127.0.0.1", 0)
    try:
        r = post(f"http://127.0.0.1:{server.server_address[1]}", "/llm_query", {"prompt": "hi"})
        assert r.status_code == 500
        assert "Proxy handler error" in r.json()["error"] and "no address for you" in r.json()["error"]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- the handler class


def test_subclasses_are_independent() -> None:
    m1, m2 = SimpleNamespace(lm_handler_address=None), SimpleNamespace(lm_handler_address=None)
    h1, h2 = make_handler_class(m1), make_handler_class(m2)
    assert h1.manager is m1 and h2.manager is m2
    assert issubclass(h1, LMProxyHandler)
    assert "manager" not in vars(LMProxyHandler)  # the base class itself is never changed


def test_handler_has_a_timeout() -> None:
    assert 0 < LMProxyHandler.timeout < float("inf")


def test_no_request_logging(proxy, capsys: pytest.CaptureFixture[str]) -> None:
    _, url, _ = proxy
    post(url, "/llm_query", {"prompt": "hi"})
    post(url, "/nope", {})
    assert capsys.readouterr().err == ""


def test_concurrent_requests() -> None:
    delay, n = 0.3, 10
    with proxy_for(SlowLM(delay, max_concurrent=n)) as (_, url, _):
        results: list[Any] = [None] * n
        barrier = threading.Barrier(n)

        def call(i: int) -> None:
            barrier.wait()
            results[i] = post(url, "/llm_query", {"prompt": str(i)}).json()

        threads = [threading.Thread(target=call, args=(i,), daemon=True) for i in range(n)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.perf_counter() - start
        assert results == [{"response": f"slow:{i}"} for i in range(n)]
        # In series it would take n * delay; a backlog of 5 adds about 1 s.
        assert elapsed < 3 * delay


# ---------------------------------------------------------------- integration


def test_exec_script_reaches_lm_through_proxy(proxy, tmp_path: Path) -> None:
    # exec script (step 6) → HTTP → proxy → TCP → LMHandler (step 4) → MockLM (step 2)
    _, url, _ = proxy
    code = 'print(llm_query("ping"))\nprint(llm_query_batched(["x", "y"]))'
    result = run_script(tmp_path, code, proxy_url=url)
    assert result["stdout"] == "Mock: ping\n['Mock: x', 'Mock: y']\n"
    assert result["stderr"] == ""


def test_exec_script_sees_new_handler(proxy, tmp_path: Path) -> None:
    # Two rounds: the same proxy URL reaches whichever LMHandler is current.
    manager, url, _ = proxy
    assert run_script(tmp_path, 'print(llm_query("a"))', proxy_url=url)["stdout"] == "Mock: a\n"
    with LMHandler(PrefixLM("round2")) as second:
        manager.lm_handler_address = second.address
        assert run_script(tmp_path, 'print(llm_query("a"))', proxy_url=url)["stdout"] == "round2:a\n"
