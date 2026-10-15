# Step 3 — Length-prefixed JSON over TCP (`comms.py`)

## Goal

The message format between LMProxyHandler and LMHandler: message dataclasses
plus helpers that send and receive one JSON message over a TCP socket. Each
message is a **4-byte big-endian length followed by that many bytes of UTF-8
JSON**. This step has no LMHandler yet; tests use `socket.socketpair()` and a
tiny echo server written inside the test file.

## What you'll learn

* **TCP is a byte stream, not a message stream.** One `send` can arrive as
  several `recv`s, and the reverse. Framing is how you get messages back out.
* **`struct.pack(">I", n)`:** `>` means big-endian, `I` means unsigned 32-bit.
* **Characters vs bytes.** `len("é") == 1` but `len("é".encode()) == 2`, and
  the length prefix must count bytes.
* **Serialising dataclasses** with `to_dict` and `from_dict`, including nested
  objects.

## Depends on

Steps 0 and 2 (`DEFAULT_TIMEOUT`, and the `Prompt` type from `dockerrepl.clients`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/comms.py` | dataclasses and socket helpers |
| `tests/test_comms.py` | tests below |

## Interface

```python
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
    def to_dict(self) -> dict[str, Any]: ...          # nested responses → list of dicts
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LMResponse": ...

def socket_send(sock: socket.socket, data: dict) -> None: ...
def socket_recv(sock: socket.socket) -> dict: ...
def socket_request(address: tuple[str, int], data: dict,
                   timeout: int = DEFAULT_TIMEOUT) -> dict: ...
def send_lm_request(address: tuple[str, int], request: LMRequest,
                    timeout: int = DEFAULT_TIMEOUT) -> LMResponse: ...
def send_lm_request_batched(address: tuple[str, int],
                            prompts: list[Prompt],
                            model: str | None = None,
                            timeout: int = DEFAULT_TIMEOUT) -> list[LMResponse]: ...
```

## Implementation guide

1. **Dataclasses first.**
   - `LMResponse.to_dict()` turns each nested response into a dict with
     `r.to_dict()`.
   - `from_dict` does the reverse, using `.get()` so missing keys become
     `None`.
2. **`socket_send`:**
   - `payload = json.dumps(data).encode("utf-8")`.
   - `sock.sendall(struct.pack(">I", len(payload)) + payload)`. Use
     `sendall`, not `send`, because `send` may write only part of the data.
3. **A private `_recv_exact(sock, n) -> bytes`** that loops on `sock.recv`
   until it has `n` bytes. If `recv` returns `b""` (the peer closed) before
   then, it returns what it has so far.
4. **`socket_recv`:**
   - Read the header with `_recv_exact(sock, 4)`.
   - **0 bytes:** the connection closed cleanly before a message. Return `{}`.
   - **1–3 bytes:** raise `ConnectionError("closed during header")`.
   - Otherwise unpack the length and read exactly that many bytes. If fewer
     arrive, raise `ConnectionError("closed before message complete")`.
   - Return the decoded JSON.
5. **`socket_request`:**
   - Open a socket with `socket.create_connection(address, timeout=timeout)`
     inside a `with` block.
   - `socket_send` the data, then return `socket_recv`. That's one connection
     per request.
6. **`send_lm_request`** wraps `socket_request` in `try/except Exception`.
   Any failure becomes `LMResponse(error=f"Request failed: {e}")`. It never
   raises.
7. **`send_lm_request_batched`:**
   - Send `LMRequest(prompts=prompts, model=model)` and read an `LMResponse`.
   - If the call failed, or `responses` is missing, return
     `[LMResponse(error=...)] * len(prompts)`.
   - Otherwise return `response.responses`, which has one entry per prompt,
     in order.

## Tests — `tests/test_comms.py`

Helpers to write in the test file:

* `one_shot_server(reply: dict | None)` is a context manager. It starts a
  listening socket on `("127.0.0.1", 0)` with a thread that accepts **one**
  connection, `socket_recv`s the request into a list, `socket_send`s `reply`,
  and closes. It yields `(address, received_list)`.
* `ChunkySocket` is a fake socket whose `recv(n)` returns at most 1 byte at a
  time from a preset `bytes` buffer, then `b""`.

| Test | Checks |
|---|---|
| `test_lm_request_roundtrip_single` | `LMRequest.from_dict(r.to_dict()) == r` for a single prompt |
| `test_lm_request_roundtrip_batched` | the same for `prompts`; `is_batched` is `True` |
| `test_lm_request_to_dict_omits_none` | `LMRequest(prompt="p").to_dict() == {"prompt": "p"}` |
| `test_lm_request_dict_prompt` | a `dict` prompt survives the round-trip |
| `test_lm_response_roundtrip_nested` | a batched `LMResponse` with one success and one error survives the round-trip |
| `test_lm_response_success_flag` | `success` is `True` without an error, `False` with one |
| `test_send_recv_roundtrip` | over `socket.socketpair()`, the dict received equals the dict sent |
| `test_length_prefix_counts_bytes` | send `{"t": "é"}`; the first 4 bytes read raw equal `len(json.dumps({"t": "é"}).encode())` |
| `test_large_message` | a 5 MB string arrives intact |
| `test_two_messages_back_to_back` | two `socket_send`s, then two `socket_recv`s, give both messages in order |
| `test_partial_reads_are_reassembled` | `socket_recv(ChunkySocket(frame))` returns the full message |
| `test_closed_before_header_returns_empty` | the peer closes without sending, giving `{}` |
| `test_closed_during_header_raises` | 2 header bytes then close raises `ConnectionError` |
| `test_closed_mid_payload_raises` | the header says 100 bytes, 10 are sent, then close: raises `ConnectionError` |
| `test_socket_request_against_server` | with `one_shot_server({"ok": 1})`, `socket_request` returns `{"ok": 1}` and the server received the request |
| `test_send_lm_request_connection_refused` | to a closed port (bind and close a socket to get a free one): returns an `LMResponse` with `error` set, no exception |
| `test_send_lm_request_batched_unpacks` | a server reply with 3 nested responses gives a list of 3 in order |
| `test_send_lm_request_batched_failure_fills_all` | connection refused with 3 prompts gives 3 responses, each with `error` set |

## Run

```bash
pytest -q tests/test_comms.py
```

## Done when

- [ ] All tests pass, and steps 0–2 still pass.
- [ ] No test uses `time.sleep`.
- [ ] `send_lm_request` and `send_lm_request_batched` can't raise. Check that
      every code path ends in a `return`.

## Pitfalls

* **`sock.recv(4)` for the header.** It can return 1–3 bytes. Always use
  `_recv_exact`, for the header too.
* **Using `len(json_str)` as the length** instead of the encoded byte length.
  `test_length_prefix_counts_bytes` catches it.
* **`sock.send` instead of `sock.sendall`.** It works in small tests and
  silently cuts large messages short.
* **Leaving sockets open in tests.** Use `with` blocks, or `pytest` may print
  `ResourceWarning`s.
