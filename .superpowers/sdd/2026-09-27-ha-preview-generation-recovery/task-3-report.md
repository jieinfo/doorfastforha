# Task 3 Report: Cancelled start cleanup

## RED

Added `test_cancelled_pending_start_stops_accepted_generation` with a gated
`start_monitor` that accepts generation 8 before waiting. The test cancels
`async_acquire_viewer`, releases the gated response, and requires cancellation,
remote `stop(..., 8)`, zero local leases, and no late stop for generation 7.

Command:

```text
python3 -m unittest tests.test_monitor
```

Result before the implementation: 34 tests ran, with one failure. The new test
failed because the only remote call was `start`; generation 8 was not stopped.

## GREEN

`MonitorCoordinator` now runs each start request in an eager task and awaits it
through `asyncio.shield`. If the acquire task is cancelled while HTTP is still
pending, it waits for that same start task, validates its response, stops only
the returned generation, then re-raises `CancelledError`. Start exceptions and
invalid responses are propagated without guessing a generation or stopping a
different one. The start acceptance logic also preserves the existing
recoverable-generation race behavior.

Commands and results:

```text
python3 -m unittest tests.test_monitor
Ran 34 tests in 0.064s
OK

python3 -m unittest discover -s tests
Ran 179 tests in 3.267s
OK

python3 -m compileall -q custom_components tests
```

`git diff --check` completed with no output.

## Self-review

- Cancellation cleanup is serialized by the existing lifecycle lock because
  `_await_start_response` runs inside `_start_generation` or
  `_recover_generation`.
- The cleanup generation comes only from the accepted start response.
- If the start task raises before returning a response, no stop request is
  issued and the original exception propagates.
- Local lease registration happens only after the response is adopted and the
  generation is ready; the cancellation path never registers a lease.

## Concerns

The implementation uses `asyncio.Task(..., eager_start=True)` to preserve the
existing synchronous ordering of start requests. This requires a Python
runtime that supports the eager task start option; the current test runtime is
Python 3.14.7.
