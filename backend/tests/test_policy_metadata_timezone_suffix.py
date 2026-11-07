"""An unsupported timezone suffix must not expose a trusted-looking prefix."""
import pytest
from test_policy_metadata_dates import check
from test_policy_metadata_instant import TIMES


@pytest.mark.parametrize("text", [
    "有效性判定时点：2036-05-03T10:11:12 GMT+01:00",
    "有效性判定时点：2036-05-03T10:11:12（GMT+01:00）",
    "失效时点：2036-05-04T10:00:00 GMT+01:00",
    "失效时点：2036-05-04T10:00:00（GMT+01:00）",
])
def test_timezone_suffix_cannot_be_dropped_to_match_only_a_utc_prefix(text):
    check(text, TIMES, blocked=True)
