# Step 4 — `LMHandler` (threaded TCP server)

## Goal

A server that answers framed `LMRequest`s using a `BaseLM`. It handles each
connection in its own thread, runs batched prompts concurrently, and also
offers a direct `completion()` method for the Client.

## What you'll learn

* **`socketserver.ThreadingTCPServer`** with a `StreamRequestHandler`, which
  gives one thread per connection.
* **Running a server in a background thread**, and shutting it down cleanly.
* **`concurrent.futures.ThreadPoolExecutor.map`**, which keeps results in
  input order.
* **Isolating failures:** one bad prompt must not fail a whole batch.
* **Ephemeral ports:** bind to port 0, then read the real port back.

## Depends on

Step 2 (`BaseLM`, `MockLM`) and step 3 (`comms`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/lm_handler.py` | `LMRequestHandler`, `ThreadingLMServer`, `LMHandler` |
| `tests/test_lm_handler.py` | tests below |

## Interface

```python
class LMRequestHandler(socketserver.StreamRequestHandler):
    timeout: float = DEFAULT_TIMEOUT       # socketserver applies it to each connection
    def handle(self) -> None: ...          # socket_recv → dispatch → socket_send

class ThreadingLMServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    lm_handler: "LMHandler"                # set by LMHandler.start()

class LMHandler:
    def __init__(self, client: BaseLM, host: str = "127.0.0.1", port: int = 0) -> None: ...
    client: BaseLM
    host: str
    _server: ThreadingLMServer | None
    _thread: threading.Thread | None
    @property
    def address(self) -> tuple[str, int]: ...
    def start(self) -> tuple[str, int]: ...
    def stop(self) -> None: ...
    def completion(self, prompt: Prompt | Messages, model: str | None = None) -> str: ...
    def __enter__(self) -> "LMHandler": ...
    def __exit__(self, *exc: object) -> None: ...
```

## Implementation guide

1. **`LMRequestHandler.timeout = DEFAULT_TIMEOUT`** (a class attribute).
   `StreamRequestHandler` calls `settimeout()` on every connection with it.
   Without it, a client that sends half a message and then stalls, without
   closing, would block that handler thread forever.
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
     `handler = self.server.lm_handler`.
   - **Batched:** call `self._batched(...)`.
   - **Single with a prompt:** wrap `handler.client.completion(request.prompt)`
     in `try/except Exception`. Success gives `LMResponse(response=...)`; an
     exception gives `LMResponse(error=str(e))`.
   - **Neither:** `LMResponse(error="Missing 'prompt' or 'prompts'")`.
   - Send the response with `socket_send(self.connection, response.to_dict())`.
     Wrap the send in `try/except OSError: pass`, because the client may
     already have disconnected.
3. **`_batched`:**
   - Write a helper `one(p)` that returns `LMResponse(response=client.completion(p))`,
     or `LMResponse(error=str(e))` if it raises.
   - Run the helper over all prompts with
     `ThreadPoolExecutor(max_workers=min(len(prompts), client.max_concurrent)).map(one, prompts)`
     inside a `with` block.
   - Return `LMResponse(responses=list(results))`.
4. **`start()`:**
   - Create `ThreadingLMServer((host, port), LMRequestHandler)` and set
     `server.lm_handler = self`.
   - Start `Thread(target=server.serve_forever, daemon=True)`.
   - Return `self.address`. If called again, return the address without
     starting a second server.
5. **`address`** returns `(self.host, self._server.server_address[1])` once
   started. Port 0 has become a real port by then.
6. **`stop()`:** `shutdown()`, then `server_close()`, then `join` the thread,
   then set both attributes to `None`. A second call does nothing.
7. **`completion()`** is the direct path:
   `return self.client.completion(prompt)`. It goes through the same
   `BaseLM.completion`, so the same concurrency cap applies.

## Tests — `tests/test_lm_handler.py`

Test LMs: `MockLM`, `SlowLM` (sleeps 0.2 s, then returns `"slow"`), and
`PickyLM` (raises on the prompt `"boom"`, otherwise returns `f"ok:{prompt}"`).

| Test | Checks |
|---|---|
| `test_start_assigns_real_port` | after `start()`, `address[1] > 0` and `address[0] == "127.0.0.1"` |
| `test_context_manager_starts_and_stops` | inside `with LMHandler(...)` a request succeeds; after it, the connection is refused |
| `test_single_request` | `send_lm_request(addr, LMRequest(prompt="hi"))` returns `"Mock: hi"` |
| `test_batched_request_preserves_order` | 5 prompts give 5 responses matching the prompts in order |
| `test_batched_isolates_failures` | `PickyLM` with `["a", "boom", "c"]`: responses 0 and 2 succeed, response 1 has `error` set |
| `test_single_request_error_is_reported` | `PickyLM` with `"boom"` gives an `LMResponse` with `error` set, and the server keeps working afterwards |
| `test_missing_prompt_is_an_error` | a request with neither field gives an error response |
| `test_concurrent_clients` | `SlowLM(max_concurrent=10)`, 10 threads each sending one request: all succeed, and the total time is under 1.0 s, which proves they ran in parallel (in series it would take 2.0 s) |
| `test_batched_respects_cap` | `SlowLM(max_concurrent=1)` with 3 prompts: still correct, and takes at least 0.6 s |
| `test_direct_completion_uses_client` | `handler.completion([{"role": "user", "content": "x"}])` returns `MockLM`'s first scripted reply |
| `test_two_handlers_coexist` | two handlers on port 0 get different ports and both answer |
| `test_stop_is_idempotent` | calling `stop()` twice doesn't raise |
| `test_client_disconnect_does_not_crash_server` | open a socket, send half a frame, close it; the next normal request still succeeds |
| `test_stalled_client_times_out` | with `LMRequestHandler.timeout` patched to 0.5 s, a client sends 2 header bytes and then waits without closing. The server closes that connection (the client's `recv()` returns `b""`) within a few seconds, and meanwhile a normal request from another client still succeeds |
| `test_malformed_request_gets_error_reply` | a frame whose payload is invalid JSON gets an `LMResponse` with `error` starting `"Bad request"`, and the server keeps working |

## Run

```bash
pytest -q tests/test_lm_handler.py
```

## Done when

- [ ] All tests pass, and steps 0–3 still pass.
- [ ] Every test that starts a handler stops it, through `with` or a fixture
      with `yield`.
- [ ] No test sleeps to wait for a server to start.

## Pitfalls

* **No `timeout` on the request handler.** `StreamRequestHandler` sockets
  wait forever by default, so a client that stalls mid-message pins a server
  thread for good. `test_stalled_client_times_out` catches it.
* **Not catching `OSError` around `socket_recv`.** A timeout or a dropped
  connection then escapes `handle()`, and socketserver prints a traceback for
  something that's normal on a network.
* **Forgetting `daemon_threads = True`.** Leftover request threads keep the
  test process alive after the tests finish.
* **Calling `shutdown()` from inside a request handler.** It deadlocks.
  `shutdown()` waits for `serve_forever` to return, which waits for the
  handler. Only call it from outside.
* **A shared results list written from worker threads.** It isn't needed:
  `map` already returns results in order.
* **Letting an exception escape `handle()`.** socketserver prints a traceback
  and closes the connection, so the proxy sees a dropped connection instead
  of a structured error.
