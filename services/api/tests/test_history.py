import datetime as dt

import pytest

from app.history import MAX_POINTS, choose_resolution

T0 = dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize(
    "span, source, bucket",
    [
        (dt.timedelta(minutes=15), "raw", 1),
        (dt.timedelta(hours=1), "raw", 5),
        (dt.timedelta(hours=8), "raw", 30),
        (dt.timedelta(hours=24), "1m", 60),
        (dt.timedelta(days=7), "1m", 600),
        (dt.timedelta(days=30), "1m", 1800),
        (dt.timedelta(days=90), "1h", 7200),
        (dt.timedelta(days=365), "1h", 21600),
    ],
)
def test_choose_resolution(span, source, bucket):
    res = choose_resolution(T0, T0 + span)
    assert (res.source, res.bucket_s) == (source, bucket)
    assert span.total_seconds() / res.bucket_s <= MAX_POINTS


def test_zero_span_does_not_divide_by_zero():
    assert choose_resolution(T0, T0).source == "raw"
