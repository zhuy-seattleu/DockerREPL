# Step 4 — `LMHandler` (threaded TCP server)

## Goal

A server that answers framed `LMRequest`s (step 3) using a `BaseLM` (step 2).
It handles each connection in its own thread, runs batched prompts
concurrently, copes with misbehaving clients, and offers a direct
`completion()` method for the Client.

## Tasks at a glance

1. **Write the request handler.** `LMRequestHandler`, a
   `socketserver.StreamRequestHandler`, reads one request with `socket_recv`,
   answers it, and sends one `LMResponse`. A request carries either one prompt
   or a batch.
2. **Make each prompt fail on its own.** An exception from the LM becomes
   that prompt's `error` (for example `"RuntimeError: ..."`); in a batch, the
   other prompts still succeed.
3. **Run batches concurrently.** Use `ThreadPoolExecutor.map`, which returns
   results in input order, with at most `client.max_concurrent` workers.
4. **Survive bad clients.** Each connection gets a timeout, so a client that
   stalls can't hold a thread forever. A dropped or timed-out connection ends
   quietly; a malformed request gets a `"Bad request: ..."` reply.
5. **Configure the server.** `ThreadingLMServer` uses daemon threads, allows
   up to 128 connections to wait to be accepted, and is polled every 0.05 s
   so it stops quickly.
6. **Wrap it in `LMHandler`.** `start()` binds port 0 and serves in a
   background thread. `address` gives the real port. `stop()` shuts down and
   frees the port. `completion()` is the Client's direct path. It's also a
   context manager.
7. **Write the unit tests.** `tests/test_lm_handler.py` has 23 tests.
8. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 23 more passing tests, and the CI table shows
step 4 as ✅.

## What you'll learn

* **`socketserver.ThreadingTCPServer`** with a `StreamRequestHandler`: one
  thread per connection, and handler settings as class attributes.
* **Running a server in a background thread**, and shutting it down cleanly
  and quickly.
* **`concurrent.futures.ThreadPoolExecutor.map`**, which keeps results in
  input order.
* **Isolating failures:** one bad prompt must not fail a whole batch.
* **Server defaults that bite:** sockets with no timeout, and a listen queue
  of only 5 connections.
* **Ephemeral ports:** bind to port 0, then read the real port back.
* **Making silent failures visible in tests:** an exception that escapes a
  socketserver handler only prints a traceback, so the tests intercept that.

## Depends on

Step 2 (`BaseLM`, `MockLM`, `Prompt`, `Messages`) and step 3 (`comms`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/lm_handler.py` | `LMRequestHandler`, `ThreadingLMServer`, `LMHandler` |
| `tests/test_lm_handler.py` | the tests below |

## Interface

```python
POLL_INTERVAL: float = 0.05            # how often serve_forever() checks for shutdown

class LMRequestHandler(socketserver.StreamRequestHandler):
    timeout: float = DEFAULT_TIMEOUT   # socketserver applies it to each connection
    def handle(self) -> None: ...      # socket_recv → dispatch → socket_send

class ThreadingLMServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128           # the default, 5, is too small
    lm_handler: "LMHandler"            # set by LMHandler.start()

class LMHandler:
    def __init__(self, client: BaseLM, host: str = "127.0.0.1", port: int = 0) -> None: ...
    client: BaseLM
    host: str
    _server: ThreadingLMServer | None
    _thread: threading.Thread | None
    @property
    def address(self) -> tuple[str, int]: ...   # RuntimeError if not started
    def start(self) -> tuple[str, int]: ...     # a second call does nothing
    def stop(self) -> None: ...                 # a second call does nothing
    def completion(self, prompt: Prompt | Messages, model: str | None = None) -> str: ...
    def __enter__(self) -> "LMHandler": ...     # start()
    def __exit__(self, *exc: object) -> None: ...   # stop()
```

The `model` field of a request (and `completion()`'s `model` parameter) is
accepted but ignored: this MVP has a single LM client.

## Implementation guide

1. **`LMRequestHandler.timeout = DEFAULT_TIMEOUT`** (a class attribute).
   `StreamRequestHandler` calls `settimeout()` on every connection with it.
   Its own default is `None`, which waits forever, so without this a client
   that sends half a message and then stalls would block that thread forever.
2. **`LMRequestHandler.handle`:**
   - Read the request with `data = socket_recv(self.connection)`, inside
     `try`:
     - `except OSError: return`. This covers `TimeoutError` (the client
       stalled) and `ConnectionError` (it closed part-way). The request never
       fully arrived, so there's nobody to answer.
     - `except ValueError as e:` (invalid JSON, not an object, too large).
       The client is still connected, so reply with
       `LMResponse(error=f"Bad request: {e}")` and return.
   - If `data` is `{}`, return (the client went away).
   - `request = LMRequest.from_dict(data)` and
     `client = self.server.lm_handler.client`.
   - **Batched** (`request.is_batched`): `self._batched(client, request.prompts)`.
   - **Single** (`request.prompt is not None`): `self._single(client, request.prompt)`.
   - **Neither:** `LMResponse(error="Bad request: missing 'prompt' or 'prompts'")`.
   - Send the reply with `socket_send`, inside `try/except OSError: pass`,
     because the client may already have gone away.
3. **`_single(client, prompt)`:** return `LMResponse(response=client.completion(prompt))`.
   On any exception, return `LMResponse(error=...)` with the exception's
   **type and message**, for example `"RuntimeError: cannot answer"`. Some
   exceptions have an empty message (`raise ValueError()`), and `str(e)`
   alone would then give an empty error.
4. **`_batched(client, prompts)`:**
   `with ThreadPoolExecutor(max_workers=min(len(prompts), client.max_concurrent)) as pool:`
   `results = list(pool.map(lambda p: _single(client, p), prompts))`.
   Return `LMResponse(responses=results)`. `_single` already catches errors,
   so each failure stays in its own slot. There's no shared list, so no lock
   is needed.
5. **`ThreadingLMServer`:** three class attributes.
   - `daemon_threads = True`: leftover request threads don't keep Python
     running, and `server_close()` doesn't wait for them.
   - `allow_reuse_address = True`.
   - `request_queue_size = 128`. This is the listen queue: connections
     waiting to be accepted. With the default of 5, the sixth simultaneous
     connection is dropped and only retried about a second later. That
     happens as soon as the container makes several `llm_query` calls at once.
6. **`start()`:**
   - If already started, just return `self.address`.
   - Create `ThreadingLMServer((host, port), LMRequestHandler)` and set
     `server.lm_handler = self`.
   - Start `Thread(target=server.serve_forever, kwargs={"poll_interval": POLL_INTERVAL}, daemon=True)`.
     `serve_forever` checks for a shutdown request only every
     `poll_interval` seconds (default 0.5), so a small value makes `stop()`
     fast.
   - Return `self.address`.
7. **`address`:** `(self.host, self._server.server_address[1])`. Port 0 has
   become a real port by then. Raise `RuntimeError` if not started.
8. **`stop()`:** if started, `shutdown()`, then `server_close()`, then
   `join` the thread, then set both attributes to `None`. A second call does
   nothing. Only call it from outside a request handler (see Pitfalls).
9. **`completion()`** is the Client's direct path:
   `return self.client.completion(prompt)`. It goes through the same
   `BaseLM.completion`, so the same concurrency cap applies.

## Tests — `tests/test_lm_handler.py`

Test LMs and helpers in the test file:

* `SlowLM`: sleeps 0.2 s, then returns `"slow"`.
* `PickyLM`: raises `RuntimeError("cannot answer boom")` on the prompt
  `"boom"`, otherwise returns `f"ok:{prompt}"`.
* `SilentFailLM`: raises `ValueError()`, with an empty message.
* `handler` fixture: a running `LMHandler(MockLM(["first scripted reply"]))`,
  stopped after the test.
* `run_in_threads(n, target)`: as in step 2, starts `n` daemon threads
  together with a `Barrier`.
* **`no_unhandled_server_errors`** (applied to every test automatically): it
  replaces `ThreadingLMServer.handle_error` with a recorder and fails the
  test if it was called. `handle_error` is what socketserver calls when an
  exception escapes `handle()`; normally it only prints a traceback.

| Test | Checks |
|---|---|
| `test_start_assigns_real_port` | after `start()`, the host is `"127.0.0.1"` and the port is `> 0` |
| `test_address_before_start_is_an_error` | `address` raises `RuntimeError` before `start()` |
| `test_start_twice_keeps_one_server` | a second `start()` returns the same address |
| `test_context_manager_starts_and_stops` | inside `with`, a request succeeds; afterwards, connecting fails |
| `test_stop_is_idempotent` | calling `stop()` twice doesn't raise |
| `test_stop_is_quick` | `stop()` takes under 0.4 s |
| `test_two_handlers_coexist` | two handlers on port 0 get different ports, and each answers with its own LM |
| `test_single_request` | `LMRequest(prompt="hi")` gets `"Mock: hi"` |
| `test_dict_prompt_request` | a `dict` prompt gets an answer |
| `test_batched_request_preserves_order` | 5 prompts give 5 responses, in order |
| `test_batched_isolates_failures` | `PickyLM` with `["a", "boom", "c"]`: successes `[True, False, True]`, and slot 1's error names the problem |
| `test_single_request_error_is_reported` | `"boom"` gives the error `"RuntimeError: cannot answer boom"`, and the next request still works |
| `test_error_with_empty_message_still_names_the_type` | `SilentFailLM` gives the error `"ValueError"`, not `""` |
| `test_missing_prompt_is_an_error` | a request with only `model` gets an error mentioning "missing" |
| `test_empty_batch_is_an_error` | `prompts=[]` gets an error |
| `test_direct_completion_uses_client` | `handler.completion([...])` returns MockLM's first scripted reply |
| `test_concurrent_clients` | `SlowLM(max_concurrent=10)` with 10 threads each sending one request: all succeed in under 1.0 s, where one at a time would take 2.0 s |
| `test_batched_runs_in_parallel` | `SlowLM(max_concurrent=5)` with a batch of 5: under 0.6 s, where one at a time would take 1.0 s |
| `test_batched_respects_cap` | `SlowLM(max_concurrent=1)` with a batch of 3: still correct, and takes at least 0.6 s |
| `test_client_disconnect_does_not_crash_server` | a client sends half a message and closes; the next request still succeeds |
| `test_handler_has_a_timeout_by_default` | `LMRequestHandler.timeout == DEFAULT_TIMEOUT` |
| `test_stalled_client_times_out` | with the timeout patched to 0.5 s, a client sends 2 header bytes and waits. Another client is still served, and the server closes the stalled connection (its `recv` returns `b""`) within 4 s |
| `test_malformed_request_gets_error_reply` | a payload of `{not json` gets a reply whose `error` starts with `"Bad request"`, and the server keeps working |

## Run

```bash
pytest -v tests/test_lm_handler.py
pytest -q -W error tests/test_lm_handler.py     # also fails on unclosed sockets
```

## Done when

- [ ] All 23 tests pass, and steps 0–3 still pass.
- [ ] Three runs in a row all pass (the timing tests aren't flaky).
- [ ] Every test that starts a handler stops it, through `with` or a fixture
      with `yield`.
- [ ] No test sleeps to wait for a server to start: `start()` returns once
      the port is bound, so a client can connect straight away.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 4 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **No `timeout` on the request handler** | a client that stalls mid-message pins a server thread forever | `test_handler_has_a_timeout_by_default`. The stalled-client test alone can't catch it, because it sets its own timeout. |
| **Not catching `OSError` around `socket_recv`** | a timeout or dropped connection escapes `handle()`; socketserver prints a traceback for something normal on a network | the `no_unhandled_server_errors` check, in `test_client_disconnect_does_not_crash_server` and `test_stalled_client_times_out` |
| **Not catching `ValueError` around `socket_recv`** | a malformed request gets no reply at all, just a dropped connection | `test_malformed_request_gets_error_reply` |
| **Not catching LM exceptions per prompt** | one failing prompt drops the whole connection; the client only sees "connection closed without a reply" | `test_batched_isolates_failures` and 2 others |
| **Default `request_queue_size` (5)** | with more than 5 simultaneous connections, the extra ones wait about a second to be retried | `test_concurrent_clients` (1.2 s instead of 0.2 s) |
| **Default `poll_interval` (0.5 s)** | every `stop()` waits up to half a second; it adds up across rounds and tests | `test_stop_is_quick` |
| **Error text as `str(e)`** | `raise ValueError()` gives an empty, useless error | `test_error_with_empty_message_still_names_the_type` |
| **Batch run with one worker** | a batch takes as long as its prompts one after another | `test_batched_runs_in_parallel` |

Two more to know about, which aren't practical to test:

* **Calling `stop()` (that is, `shutdown()`) from inside a request handler**
  deadlocks. `shutdown()` waits for `serve_forever` to return, which waits
  for the handler that called it. Only call it from outside.
* **Forgetting `daemon_threads = True`** makes `server_close()` wait for
  every request thread, including a stalled one, and keeps the test process
  alive.
