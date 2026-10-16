# Step 3 — Length-prefixed JSON over TCP (`comms.py`)

## Goal

The message format between LMProxyHandler and LMHandler: message dataclasses
plus helpers that send and receive one JSON message over a TCP socket. Each
message is a **4-byte big-endian length followed by that many bytes of UTF-8
JSON**. There's no LMHandler yet, so the tests use `socket.socketpair()`, fake
sockets, and a tiny one-shot server written inside the test file.

## Tasks at a glance

1. **Define the two messages.** `LMRequest` (one `prompt`, or a batch of
   `prompts`, plus `model`) and `LMResponse` (`error`, `response`, or one
   nested `LMResponse` per prompt in `responses`). Both convert to and from
   plain dicts and leave out `None` fields.
2. **Frame messages on a byte stream.** `socket_send` writes the 4-byte length
   of the real UTF-8 payload, then the payload. `socket_recv` reads exactly 4
   bytes, then exactly that many payload bytes, looping because `recv` may
   return less than asked for.
3. **Fail safely on bad input.** A connection closed part-way raises
   `ConnectionError`. A header claiming more than `MAX_MESSAGE_BYTES`, or a
   payload that isn't a JSON object, raises `ValueError`.
4. **Add request helpers that never raise.** `socket_request` makes one
   connection per request. `send_lm_request` and `send_lm_request_batched`
   turn every failure into `LMResponse(error=...)`, including a server that
   closes without replying and a batch reply with the wrong number of answers.
5. **Write the unit tests.** `tests/test_comms.py` has 31 tests, using
   `socket.socketpair()`, fake sockets that read or write a little at a time,
   and a one-shot server.
6. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 31 more passing tests, and the CI table shows
step 3 as ✅.

## What you'll learn

* **TCP is a byte stream, not a message stream.** One `send` can arrive as
  several `recv`s, and the reverse. Framing is how you get messages back out.
* **`struct.pack(">I", n)`:** `>` means big-endian, `I` means unsigned 32-bit.
* **Characters vs bytes.** `len("é") == 1` but `len("é".encode()) == 2`, and
  the length prefix must count bytes. Note that `json.dumps` hides this by
  default: it writes `é` as the ASCII text `é`. We pass
  `ensure_ascii=False` to send real UTF-8.
* **Defensive network code:** never trust a length you received, and never
  treat "no reply" as success.
* **Serialising dataclasses** with `to_dict` and `from_dict`, including nested
  objects.
* **Test doubles for sockets:** small fake classes that behave like a slow or
  busy network, to test cases that a fast local socket never produces.

## Depends on

Steps 0 and 2 (`DEFAULT_TIMEOUT` from `dockerrepl.constants`, and the `Prompt`
type from `dockerrepl.clients.base_lm`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/comms.py` | constants, dataclasses and socket helpers |
| `tests/test_comms.py` | the tests below |

## Interface

```python
HEADER_FORMAT = ">I"                  # 4-byte big-endian unsigned length
HEADER_SIZE = 4                       # struct.calcsize(HEADER_FORMAT)
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # 64 MiB; larger messages are refused

@dataclass
class LMRequest:                     # Prompt = str | dict[str, Any], from step 2
    prompt: Prompt | None = None
    prompts: list[Prompt] | None = None
    model: str | None = None
    @property
    def is_batched(self) -> bool: ...                 # prompts is a non-empty list
    def to_dict(self) -> dict[str, Any]: ...          # omits keys whose value is None
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LMRequest": ...

@dataclass
class LMResponse:
    error: str | None = None
    response: str | None = None
    responses: list["LMResponse"] | None = None
    @property
    def success(self) -> bool: ...                    # error is None
    def to_dict(self) -> dict[str, Any]: ...          # omits None; nested responses → list of dicts
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LMResponse": ...

def socket_send(sock: socket.socket, data: dict) -> None: ...
def socket_recv(sock: socket.socket) -> dict: ...
def socket_request(address: tuple[str, int], data: dict,
                   timeout: float = DEFAULT_TIMEOUT) -> dict: ...
def send_lm_request(address: tuple[str, int], request: LMRequest,
                    timeout: float = DEFAULT_TIMEOUT) -> LMResponse: ...
def send_lm_request_batched(address: tuple[str, int],
                            prompts: list[Prompt],
                            model: str | None = None,
                            timeout: float = DEFAULT_TIMEOUT) -> list[LMResponse]: ...
```

`timeout` is a `float` (seconds) rather than an `int`, so tests can use short
timeouts such as `0.5`. Passing an `int` like `DEFAULT_TIMEOUT` still works.

## Implementation guide

1. **Dataclasses first.**
   - `to_dict()` leaves out fields that are `None`, so messages stay small.
   - `LMResponse.to_dict()` turns each nested response into a dict with
     `r.to_dict()`.
   - `from_dict` does the reverse, using `.get()` so missing keys become
     `None`, and rebuilding nested responses with `LMResponse.from_dict`.
2. **`socket_send`:**
   - `payload = json.dumps(data, ensure_ascii=False).encode("utf-8")`.
   - Raise `ValueError` if `len(payload) > MAX_MESSAGE_BYTES`.
   - `sock.sendall(struct.pack(HEADER_FORMAT, len(payload)) + payload)`. Use
     `sendall`, not `send`, because `send` may write only part of the data.
3. **A private `_recv_exact(sock, n) -> bytes`** that loops on `sock.recv`
   until it has `n` bytes. If `recv` returns `b""` (the peer closed) first, it
   returns what it has so far. Collect the pieces in a `bytearray`; repeated
   `bytes + bytes` copies everything each time.
4. **`socket_recv`:**
   - Read the header with `_recv_exact(sock, HEADER_SIZE)`.
   - **0 bytes:** the connection closed cleanly before a message. Return `{}`.
   - **1–3 bytes:** raise `ConnectionError` ("closed during the message
     header").
   - Unpack the length. If it's larger than `MAX_MESSAGE_BYTES`, raise
     `ValueError` **before** reading the payload.
   - Read exactly `length` bytes. If fewer arrive, raise `ConnectionError`,
     saying how many of how many arrived.
   - Decode the JSON. If it isn't a `dict`, raise `ValueError`.
5. **`socket_request`:**
   - Open a socket with `socket.create_connection(address, timeout=timeout)`
     inside a `with` block.
   - `socket_send` the data, then return `socket_recv`. That's one connection
     per request.
6. **`send_lm_request`** wraps `socket_request` in `try/except Exception`.
   Any failure becomes `LMResponse(error=f"Request failed: {e}")`. **An empty
   reply (`{}`) is also an error:** the server closed without answering.
   This function never raises.
7. **`send_lm_request_batched`:**
   - Call `send_lm_request(address, LMRequest(prompts=prompts, model=model), timeout)`.
   - If it failed, return one `LMResponse(error=...)` per prompt.
   - If `responses` is missing, or its length differs from
     `len(prompts)`, also return one error per prompt.
   - Otherwise return `responses`: one entry per prompt, in order.

## Tests — `tests/test_comms.py`

Helpers in the test file:

* **`frame(data)`:** the exact bytes `socket_send` writes for `data`.
* **`ChunkySocket(data)`:** a fake socket whose `recv` returns at most **one
  byte** at a time, then `b""`.
* **`ShortWriteSocket()`:** a fake socket whose `send` accepts at most **1,000
  bytes** per call, like a busy network. Its `sendall` loops over `send`, as
  the real one does.
* **`one_shot_server(reply=None, raw=None, hold=False)`:** a context manager.
  It listens on `127.0.0.1`, accepts **one** connection and reads one request.
  It then sends `raw` bytes, or `reply` as a message, or nothing; with
  `hold=True` it waits without replying. It yields
  `(address, received_requests)`.
* **`closed_port_address()`:** binds a free port and closes it, giving an
  address with nothing listening.

| Test | Checks |
|---|---|
| `test_lm_request_roundtrip_single` | `LMRequest.from_dict(r.to_dict()) == r`; `is_batched` is `False` |
| `test_lm_request_roundtrip_batched` | the same for `prompts`; `is_batched` is `True` |
| `test_empty_prompts_list_is_not_batched` | `LMRequest(prompts=[]).is_batched` is `False` |
| `test_lm_request_to_dict_omits_none` | `LMRequest(prompt="p").to_dict() == {"prompt": "p"}` |
| `test_lm_request_dict_prompt` | a `dict` prompt survives the round-trip |
| `test_lm_response_roundtrip_nested` | a batched response with one success and one error survives the round-trip, including through real JSON text |
| `test_lm_response_success_flag` | `success` is `True` without an error, `False` with one |
| `test_send_recv_roundtrip` | over `socket.socketpair()`, the dict received equals the dict sent |
| `test_length_prefix_counts_bytes` | for `{"t": "é"}`, the 4-byte prefix equals the UTF-8 **byte** length, which differs from the character count |
| `test_non_ascii_roundtrip` | accented, Chinese and emoji text arrives unchanged |
| `test_large_message` | a 5 MB string arrives intact |
| `test_partial_writes_are_completed` | with `ShortWriteSocket`, every byte of a 5 KB message is written, not just the first 1,000 |
| `test_two_messages_back_to_back` | two sends, then two receives, give both messages in order |
| `test_partial_reads_are_reassembled` | `socket_recv(ChunkySocket(frame(...)))` returns the full message, header included |
| `test_closed_before_header_returns_empty` | the peer closes without sending: `{}` |
| `test_closed_during_header_raises` | 2 header bytes, then close: `ConnectionError` |
| `test_closed_mid_payload_raises` | the header says 100 bytes, 10 arrive, then close: `ConnectionError` mentioning "10 of 100" |
| `test_oversized_incoming_message_rejected` | a header claiming `MAX_MESSAGE_BYTES + 1`: `ValueError`, without waiting for the payload |
| `test_oversized_outgoing_message_rejected` | with `MAX_MESSAGE_BYTES` patched to 10, sending a longer message raises `ValueError` |
| `test_non_object_payload_rejected` | a payload of `[1, 2, 3]`: `ValueError` |
| `test_socket_request_against_server` | `socket_request` returns the server's reply, and the server received the request |
| `test_send_lm_request_single` | a `{"response": "pong"}` reply gives `LMResponse(response="pong")`; the request sent was `{"prompt": ..., "model": ...}` |
| `test_send_lm_request_connection_refused` | to a closed port: an `LMResponse` with `error` set, no exception |
| `test_send_lm_request_server_closes_without_reply` | the server reads the request and closes: an error mentioning "without a reply" |
| `test_send_lm_request_invalid_reply` | the server sends a frame containing invalid JSON: an error, no exception |
| `test_send_lm_request_times_out` | the server never replies; with `timeout=0.5`, an error mentioning "timed out" |
| `test_send_lm_request_batched_unpacks` | a reply with 3 nested responses (one an error) gives a list of 3, in order |
| `test_send_lm_request_batched_failure_fills_all` | connection refused with 3 prompts: 3 responses, each with an error |
| `test_send_lm_request_batched_wrong_count` | 2 prompts but 1 response: 2 errors mentioning "expected 2 responses, got 1" |
| `test_send_lm_request_batched_missing_responses` | a single-style reply to a batch: an error for every prompt |
| `test_send_helpers_never_raise` | an invalid address (`256.0.0.1`): both helpers return errors instead of raising |

## Run

```bash
pytest -v tests/test_comms.py
pytest -q -W error tests/test_comms.py     # also fails on ResourceWarning (unclosed sockets)
```

## Done when

- [ ] All 31 tests pass, and steps 0–2 still pass.
- [ ] `pytest -q -W error tests/test_comms.py` passes, so no socket is left
      open.
- [ ] No test uses `time.sleep`.
- [ ] `send_lm_request` and `send_lm_request_batched` can't raise: every code
      path ends in a `return`.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 3 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **`sock.recv(4)` for the header** | it can return 1–3 bytes, and the length is then garbage | `test_partial_reads_are_reassembled` and 3 others |
| **Length = character count** (`len(json_text)`) | for non-ASCII text the reader stops early or waits forever | `test_length_prefix_counts_bytes`, `test_non_ascii_roundtrip` |
| **Default `ensure_ascii`** | works, but hides the bytes-vs-characters problem and inflates non-English text | `test_length_prefix_counts_bytes` |
| **`sock.send` instead of `sock.sendall`** | large messages are silently cut short. A fast local socket hides this, which is why the test uses `ShortWriteSocket` | `test_partial_writes_are_completed` |
| **Treating an empty reply as success** | a server that closes without answering looks like "no error, no response" | `test_send_lm_request_server_closes_without_reply` |
| **No count check on batch replies** | answers get paired with the wrong prompts | `test_send_lm_request_batched_wrong_count` |
| **No size limit on incoming messages** | a malformed header can make the reader allocate up to 4 GB | `test_oversized_incoming_message_rejected` |
| **Catching only `OSError`** | invalid JSON raises `ValueError`, which escapes a function documented never to raise | `test_send_lm_request_invalid_reply` |

Also use `with` blocks for every socket, in the code and in the tests.
`pytest -W error` turns a forgotten `close()` into a failure.
