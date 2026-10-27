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

## Tasks at a glance

1. **`LMProxyHandler.do_POST`:** read the handler address once, read and
   check the JSON body, dispatch on the path, and always reply with JSON:
   200, 400, 404 or 500.
2. **The two routes:** `/llm_query` sends one `LMRequest`;
   `/llm_query_batched` sends one batched request and turns each failed slot
   into an `"Error: ..."` string.
3. **`_respond`:** JSON body with `Content-Type` and `Content-Length`,
   ignoring a caller that has already gone.
4. **`ThreadingProxyServer`:** a `ThreadingHTTPServer` with daemon threads and
   a listen backlog of 128.
5. **`make_handler_class`:** a subclass per owner, made with `type()`, that
   holds a reference to the owner.
6. **`start_proxy_server`:** start it in a daemon thread and return
   `(server, thread)`.
7. **Write the unit tests.** `tests/test_proxy.py` has 21 tests (25 test
   cases with parametrize), ending with two integration tests that join steps
   4, 6 and 7.
8. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 25 more passing test cases, and the CI table
shows step 7 as ✅.

## What you'll learn

* **`http.server.BaseHTTPRequestHandler` and `ThreadingHTTPServer`.**
* **Why per-request handler classes can't take constructor arguments,** and
  how to bind data with `type("Handler", (Base,), {"manager": obj})`.
* **References vs copies:** storing the owner object, so updates are seen
  immediately.
* **Duck typing:** the proxy only needs an object with a
  `lm_handler_address` attribute.
* **HTTP status codes as part of an API:** 400 for the caller's mistakes,
  404 for unknown paths, 500 for the server's own errors, and 200 with an
  `error` field when the request was fine but the LM couldn't answer.
* **The listen backlog** again (as in step 4), this time found by an HTTP
  test.

## Depends on

Steps 3 and 4. The integration tests also use step 6.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/proxy.py` | `LMProxyHandler`, `ThreadingProxyServer`, `make_handler_class`, `start_proxy_server` |
| `tests/test_proxy.py` | tests below |

## Interface

```python
class LMProxyHandler(BaseHTTPRequestHandler):
    manager: Any            # set on the subclass; anything with .lm_handler_address
    timeout: float = DEFAULT_TIMEOUT
    def do_POST(self) -> None: ...
    def log_message(self, format: str, *args: Any) -> None: ...   # silence logs

class ThreadingProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

def make_handler_class(manager: Any) -> type[LMProxyHandler]: ...
    # return type("Handler", (LMProxyHandler,), {"manager": manager})

def start_proxy_server(manager: Any, host: str = "0.0.0.0",
                       port: int = 0) -> tuple[ThreadingProxyServer, threading.Thread]: ...
```

**Replies.** `llm_query` in the container (step 6) reads the JSON body
whatever the status, so every case below reaches the user's code as a reply
or an `"Error: ..."` string:

| Situation | Status | Body |
|---|---|---|
| `/llm_query` answered | 200 | `{"response": "..."}` |
| `/llm_query` failed in the LM, or the LMHandler can't be reached | 200 | `{"error": "..."}` |
| `/llm_query_batched` | 200 | `{"responses": [...]}`, a failed prompt as `"Error: ..."` in its place |
| No handler address set | 200 | `{"error": "No LM handler configured"}` |
| No `Content-Length`, bad JSON, a body that isn't an object, a missing or wrongly typed `prompt`/`prompts` | 400 | `{"error": "Bad request: ..."}` |
| Any other path | 404 | `{"error": "Not found: /path"}` |
| The handler itself raises | 500 | `{"error": "Proxy handler error: ..."}` |

## Implementation guide

1. **`do_POST`:** wrap everything in `try`.
   - `address = self.manager.lm_handler_address`. Read it **once**, at the
     start of the request, so one request never mixes two rounds.
   - `_read_json()`: `Content-Length` must be present, an integer, and at
     most `MAX_MESSAGE_BYTES` (step 3). Read exactly that many bytes and
     `json.loads` them; the result must be a dict. Each failure raises a small
     `BadRequest` exception.
   - Dispatch on `self.path`.
   - `except BadRequest` → 400; `except Exception` → 500. The proxy must
     always answer, never drop the connection.
2. **`/llm_query`:** `prompt` must be a `str` or a `dict` (step 2's `Prompt`).
   If `address` is `None`, return the "No LM handler configured" error.
   Otherwise `send_lm_request(address, LMRequest(prompt=..., model=...))`,
   and return `{"response": r.response}` or `{"error": r.error}`.
3. **`/llm_query_batched`:** `prompts` must be a list of `str` or `dict`.
   Check the address the same way. An empty list returns `{"responses": []}`
   without contacting the LMHandler. Otherwise
   `send_lm_request_batched(address, prompts, model)` and return
   `{"responses": [r.response if r.success else f"Error: {r.error}" for r in rs]}`.
4. **`_respond(status, data)`** sets `Content-Type: application/json` and
   `Content-Length`, then writes the body. Ignore `OSError` while writing,
   because the caller may have timed out.
5. **`ThreadingProxyServer`:** set `request_queue_size = 128`. The default
   of 5 means that when more than 5 callers connect at once, for example
   threads in the user's code each calling `llm_query`, the extra connections
   are refused and only retry about a second later.
6. **`start_proxy_server`:** build the server with
   `make_handler_class(manager)`, start `serve_forever(poll_interval=0.05)`
   in a daemon thread, and return `(server, thread)`. The caller reads the
   real port from `server.server_address[1]` and stops the server with
   `shutdown()` and then `server_close()`.

## Tests — `tests/test_proxy.py`

Test LMs and fixtures:

* `PrefixLM(prefix)` answers `f"{prefix}:{prompt}"`, which makes it obvious
  which handler answered. `PickyLM` raises on the prompt `"boom"`. `SlowLM`
  sleeps before answering.
* `proxy_for(lm)` is a context manager. It starts an `LMHandler(lm)`, makes
  `manager = SimpleNamespace(lm_handler_address=handler.address)`, starts a
  proxy on `127.0.0.1`, and yields `(manager, url, handler)`.
* The `proxy` fixture is `proxy_for(MockLM())`.
* `post(url, path, body)` calls `requests.post(..., json=body, timeout=5)`.

| Test | Checks |
|---|---|
| **The two routes** | |
| `test_llm_query` | `{"prompt": "hi"}` gives `{"response": "Mock: hi"}`, with a JSON `Content-Type` and a correct `Content-Length` |
| `test_llm_query_with_model` | a `model` field is accepted and passed along |
| `test_llm_query_dict_prompt` | a `dict` prompt is accepted |
| `test_llm_query_batched` | `{"prompts": ["a", "b"]}` gives `{"responses": ["Mock: a", "Mock: b"]}` |
| `test_empty_batch` | `{"prompts": []}` gives `{"responses": []}` |
| `test_batched_error_slot` | with `PickyLM`, `["a", "boom", "c"]` gives the middle entry as `"Error: ..."` and the others answered |
| `test_lm_error_returned` | with `PickyLM`, `"boom"` gives `{"error": ...}` mentioning `boom` |
| **The handler address** | |
| `test_no_handler_configured` | `manager.lm_handler_address = None` gives `{"error": "No LM handler configured"}` on both routes |
| `test_address_is_read_live` | point `manager.lm_handler_address` at a second LMHandler with `PrefixLM("B")`; the next request returns `"B:hi"`, with no proxy restart |
| `test_handler_gone_returns_error` | stop the LMHandler: status 200 with `"Request failed..."`, and a batch gets one error per prompt |
| **Bad requests** | |
| `test_unknown_path_404` | `POST /nope` gives 404 |
| `test_bad_json_400` | a body of `"{not json"` gives 400 |
| `test_invalid_body_400` (5 cases) | a list body, no `prompt`, `prompt: 5`, `prompts: "a"`, `prompts: ["a", 5]` |
| `test_missing_content_length_400` | a request without `Content-Length` gives 400, not 500 |
| `test_owner_error_gives_500` | an owner whose `lm_handler_address` raises gives 500 with the message |
| **The handler class** | |
| `test_subclasses_are_independent` | `make_handler_class(m1).manager is m1`, the same for `m2`, and `LMProxyHandler` itself has no `manager` set |
| `test_handler_has_a_timeout` | `LMProxyHandler.timeout` is a finite number |
| `test_no_request_logging` | requests print nothing to stderr |
| `test_concurrent_requests` | 10 threads calling `/llm_query` with a `SlowLM(0.3 s)` all succeed in under `3 × 0.3 s` (in series: 3 s) |
| **Integration** | |
| `test_exec_script_reaches_lm_through_proxy` | `run_script` from step 6 with `proxy_url = url` and code `print(llm_query("ping")); print(llm_query_batched(["x", "y"]))` prints `Mock: ping` and `['Mock: x', 'Mock: y']` |
| `test_exec_script_sees_new_handler` | two runs with the same proxy URL: the second, after the address changes, reaches the new handler |

The integration tests import `run_script` from `tests/test_exec_script.py`.
That works because pytest puts the `tests/` folder on `sys.path` when it
loads test files from it.

## Run

```bash
pytest -q tests/test_proxy.py
```

## Done when

- [ ] All 25 test cases pass, and steps 0–6 still pass.
- [ ] `test_exec_script_reaches_lm_through_proxy` passes. The whole path —
      exec script, HTTP, proxy, TCP, LMHandler, MockLM — works with no Docker.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 7 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **Setting `LMProxyHandler.manager = m` directly** | it changes for every proxy in the process | `test_subclasses_are_independent` |
| **Copying the address into the class**, as in `{"lm_handler_address": m.lm_handler_address}` | the proxy never sees the next round's handler | `test_address_is_read_live`, `test_exec_script_sees_new_handler` and others |
| **The default backlog of 5** | with 10 callers at once the test takes 1.4 s instead of 0.3 s | `test_concurrent_requests` |
| **No catch-all `except Exception`** | the connection is dropped with no reply | `test_owner_error_gives_500` |
| **Reading `self.headers["Content-Length"]` unguarded** | a missing header gives 500 instead of 400 | `test_missing_content_length_400` |
| **Not checking `prompt`/`prompts`, or that the body is an object** | the LMHandler gets a request it can't use | `test_invalid_body_400` |
| **Returning `r.response` for a failed batch slot** | the user's code gets `None` instead of an `"Error: ..."` string | `test_batched_error_slot`, `test_handler_gone_returns_error` |
| **No handler timeout** | a stalled caller holds a thread forever | `test_handler_has_a_timeout` |
| **Not silencing `log_message`** | every request prints a line to stderr | `test_no_request_logging` |
| **Forgetting `Content-Length` in the reply** | see below | `test_llm_query` |

**About `Content-Length`.** Leaving it out breaks nothing today:
`BaseHTTPRequestHandler` speaks HTTP/1.0 by default and closes the connection
after each reply, and the close marks the end of the body. With HTTP/1.1
keep-alive, the caller couldn't tell where the body ends and would wait. So
`test_llm_query` checks the header directly.

**About the empty-batch shortcut.** Without it, an empty batch still returns
`[]`, because step 3 maps a failed request to one error per prompt, and there
are none. The shortcut only saves a pointless TCP round trip, so no test can
see it.
