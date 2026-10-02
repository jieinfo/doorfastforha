# Task 1 Report: Generation lease and precise release

## Implementation

- Added immutable `MonitorLease(generation, lease_id, epoch)` to `monitor.py`.
- Replaced the drifting global viewer counter with `_leases`, `_next_lease_id`, and `_terminal_epoch`.
- `async_acquire_viewer()` now registers and returns a lease after the existing ready check.
- `async_release_viewer(lease)` removes only an exact currently registered lease. Late or duplicate releases are no-ops. Viewer false and grace stop are sent only when the current generation has no remaining leases.
- Terminal status, stop, preempt, and unload clear leases for the terminated generation.
- Updated WebRTC sessions and all provider release paths to retain and release the exact lease while preserving producer retry timing.
- Migrated monitor and WebRTC fakes/tests to the lease interface and added a late old-generation release regression test.

## Files changed

- `custom_components/doorfast/monitor.py`
- `custom_components/doorfast/webrtc.py`
- `tests/test_monitor.py`
- `tests/test_webrtc.py`

## TDD evidence

RED:

```text
$ python3 -m unittest tests.test_monitor
Ran 22 tests ...
FAILED (errors=2)
AttributeError: 'int' object has no attribute 'generation'
TypeError: MonitorCoordinator.async_release_viewer() takes 1 positional argument but 2 were given
```

GREEN:

```text
$ python3 -m unittest tests.test_monitor tests.test_webrtc
Ran 34 tests in 1.093s
OK

$ python3 -m unittest discover -s tests
Ran 166 tests in 3.263s
OK
```

Additional check: `git diff --check` passed.

## Self-review and concerns

- Lease equality is value-based through the frozen dataclass; the release path additionally requires the lease id to still be present in `_leases`, so an old lease cannot affect a later generation.
- `_terminal_epoch` is initialized to zero as required; lifecycle epoch advancement belongs to Task 2 and is intentionally not implemented here.
- The WebRTC negotiation retry loop and delay were left unchanged; one lease is retained across retries and released only by the existing session/error cleanup paths.
- No unresolved concerns identified for Task 1.

## Follow-up review fix

Review identified a lease leak when cancellation arrived while `ws_connect()` was
still pending: the inner handler marked the lease released even though no session
existed, causing the outer cleanup to skip release. Added a regression test using a
blocking WebSocket connector, and changed the flag to be set only after an existing
session has actually been cleaned up and released.

Also corrected the `async_acquire_viewer()` docstring to describe its lease return.

Follow-up RED:

```text
$ python3 -m unittest tests.test_webrtc.ProviderTest.test_ws_connect_cancellation_releases_viewer_lease
FAILED (failures=1)
AssertionError: 1 != 0
```

Follow-up GREEN:

```text
$ python3 -m unittest tests.test_webrtc tests.test_monitor
Ran 35 tests in 1.097s
OK

$ python3 -m unittest discover -s tests
Ran 167 tests in 3.249s
OK

$ git diff --check
```
