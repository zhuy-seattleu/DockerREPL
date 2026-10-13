# Step 7 — `LMProxyHandler` (HTTP proxy)

## Goal

The HTTP server that code in the container calls. It accepts
`POST /llm_query` and `POST /llm_query_batched`, forwards each one to the
current LMHandler using `send_lm_request` and `send_lm_request_batched` from
step 3, and returns JSON. The address of "the current LMHandler" is read
**live** from the object that owns the proxy (the ContainerManager in step 8),
so it can change every round.

In this step, the owner is a stand-in, `types.SimpleNamespace(lm_handler_address=...)`.
At the end, steps 4, 6 and 7 are joined up: a script from step 6 runs on your
machine and its `llm_query` reaches a real LMHandler through this proxy, with
no Docker involved.

## What you'll learn

* **`http.server.BaseHTTPRequestHandler` and `ThreadingHTTPServer`.**
* **Why per-request handler classes can't take constructor arguments,** and
  how to bind data with `type("Handler", (Base,), {"manager": obj})`.
* **References vs copies:** storing the owner object, so updates are seen
  immediately.
* **Duck typing:** the proxy only needs an object with a
  `lm_handler_address` attribute.

## Depends on

Steps 3 and 4. The final integration test also uses step 6.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/proxy.py` | `LMProxyHandler`, `make_handler_class`, `start_proxy_server` |
| `tests/test_proxy.py` | tests below |

## Interface

```python
class LMProxyHandler(BaseHTTPRequestHandler):
    manager: Any            # set on the subclass; anything with .lm_handler_address
    def do_POST(self) -> None: ...
    def log_message(self, format: str, *args: Any) -> None: ...   # silence logs

def make_handler_class(manager: Any) -> type[LMProxyHandler]: ...
    # return type("Handler", (LMProxyHandler,), {"manager": manager})

def start_proxy_server(manager: Any, host: str = "0.0.0.0",
                       port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread]: ...
```

## Implementation guide

1. **`do_POST`:**
   - Read `Content-Length` bytes and `json.loads` them. On failure, reply
     `400 {"error": "Bad request: ..."}`.
   - `address = self.manager.lm_handler_address`. Read it **once**, at the
     start of the request. If it's `None`, reply
     `200 {"error": "No LM handler configured"}`.
   - **`/llm_query`:** `send_lm_request(address, LMRequest(prompt=..., model=...))`.
     Reply `{"response": r.response}`, or `{"error": r.error}`.
   - **`/llm_query_batched`:**
     `send_lm_request_batched(address, body["prompts"], body.get("model"))`.
     Reply `{"responses": [r.response if r.success else f"Error: {r.error}" for r in rs]}`.
   - **Any other path:** `404 {"error": "Not found"}`.
   - Wrap the dispatch in `try/except Exception`, replying
     `500 {"error": "Proxy handler error: ..."}`. The proxy must always
     answer, never drop the connection.
2. **A private `_respond(status, data)`** sets `Content-Type: application/json`
   and `Content-Length`, then writes the body. Ignore
   `BrokenPipeError`/`ConnectionError` while writing, because the caller may
   have timed out.
3. **`start_proxy_server`:**
   - `server = ThreadingHTTPServer((host, port), make_handler_class(manager))`,
     then `server.daemon_threads = True`.
   - Start `serve_forever` in a daemon thread.
   - Return `(server, thread)`.
   - The caller reads the real port from `server.server_address[1]`.

## Tests — `tests/test_proxy.py`

Fixtures:

* `lm_handler`: a started `LMHandler(MockLM())`.
* `PrefixLM(prefix)`: a test LM whose `_completion` returns `f"{prefix}:{prompt}"`.
  It makes it obvious which handler answered.
* `proxy`: `manager = SimpleNamespace(lm_handler_address=lm_handler.address)`,
  a proxy started on `127.0.0.1`, yielding `(manager, url)`, with shutdown on
  teardown.

Use `requests.post(url + path, json=..., timeout=5)` to call it.

| Test | Checks |
|---|---|
| `test_llm_query` | `{"prompt": "hi"}` gives `{"response": "Mock: hi"}` |
| `test_llm_query_batched` | `{"prompts": ["a", "b"]}` gives `{"responses": ["Mock: a", "Mock: b"]}` |
| `test_batched_error_slot` | with a `PickyLM` handler, `["a", "boom"]` gives the second entry starting with `"Error:"` |
| `test_no_handler_configured` | `manager.lm_handler_address = None` gives `{"error": "No LM handler configured"}` |
| `test_address_is_read_live` | start a second LMHandler with `PrefixLM("B")`, a test LM that returns `f"B:{prompt}"`. Set `manager.lm_handler_address` to it. The next request returns `"B:hi"`, with no proxy restart. |
| `test_handler_gone_returns_error` | stop the LMHandler; a request returns JSON with `error` set and HTTP status 200 |
| `test_unknown_path_404` | `POST /nope` gives 404 |
| `test_bad_json_400` | a body of `"{not json"` gives 400 |
| `test_subclasses_are_independent` | `make_handler_class(m1).manager is m1`, `make_handler_class(m2).manager is m2`, and `LMProxyHandler` itself has no `manager` set |
| `test_concurrent_requests` | 10 threads calling `/llm_query` with a `SlowLM` all succeed, faster than running them in series |
| `test_exec_script_reaches_lm_through_proxy` | **integration:** `run_script` from step 6 with `proxy_url = url` and code `print(llm_query("ping")); print(llm_query_batched(["x", "y"]))` prints `Mock: ping` and `['Mock: x', 'Mock: y']` |

## Run

```bash
pytest -q tests/test_proxy.py
```

## Done when

- [ ] All tests pass, and steps 0–6 still pass.
- [ ] `test_exec_script_reaches_lm_through_proxy` passes. The whole path —
      exec script, HTTP, proxy, TCP, LMHandler, MockLM — works with no Docker.

## Pitfalls

* **Setting `LMProxyHandler.manager = m` directly.** That changes it for every
  proxy in the process. `test_subclasses_are_independent` catches it.
* **Copying the address into the class**, as in
  `{"lm_handler_address": m.lm_handler_address}`. The proxy then never sees
  the next round's handler. `test_address_is_read_live` catches it.
* **Forgetting `Content-Length` in the reply.** `requests` may hang waiting
  for the body to end.
* **Not silencing `log_message`.** Every request then prints a line to stderr
  during tests.
