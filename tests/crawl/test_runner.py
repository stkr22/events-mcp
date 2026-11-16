from datetime import UTC, datetime, timedelta

from events_mcp.crawl.runner import SourceState, is_due

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def state(success_ago: timedelta | None, failures: int = 0, finished_ago: timedelta | None = None) -> SourceState:
    ok = NOW - success_ago if success_ago is not None else None
    fin = NOW - finished_ago if finished_ago is not None else ok
    return SourceState(ok, fin, failures, None)


def test_never_run_is_due() -> None:
    assert is_due(DAY, SourceState(None, None, 0, None), NOW)


def test_waits_for_the_interval() -> None:
    assert not is_due(DAY, state(timedelta(hours=23)), NOW)
    assert is_due(DAY, state(timedelta(hours=23, minutes=59)), NOW)  # inside the slack
    assert is_due(DAY, state(timedelta(hours=25)), NOW)


def test_retry_backoff_after_failures() -> None:
    # 1st failure: retry after 15 minutes; 2nd: 30; 3rd: 1 hour; then the normal interval.
    assert not is_due(DAY, state(timedelta(hours=30), 1, timedelta(minutes=5)), NOW)
    assert is_due(DAY, state(timedelta(hours=30), 1, timedelta(minutes=14)), NOW)
    assert not is_due(DAY, state(timedelta(hours=30), 2, timedelta(minutes=20)), NOW)
    assert is_due(DAY, state(timedelta(hours=30), 3, timedelta(minutes=59)), NOW)
    assert not is_due(DAY, state(timedelta(hours=30), 4, timedelta(hours=5)), NOW)
    assert is_due(DAY, state(timedelta(hours=30), 4, timedelta(hours=23, minutes=59)), NOW)


def test_backoff_never_exceeds_a_short_interval() -> None:
    assert is_due(timedelta(minutes=20), state(timedelta(hours=1), 3, timedelta(minutes=19)), NOW)


def test_failing_from_the_start_retries() -> None:
    assert is_due(DAY, SourceState(None, NOW - timedelta(minutes=20), 1, None), NOW)
