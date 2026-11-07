"""Unsupported fractional timestamps cannot borrow a trusted integral prefix."""
import pytest
from test_policy_metadata_dates import check

TIMES = {"policy_as_of": ["2034-07-08T11:12:13Z"],
         "policy_effective_at": ["2034-07-08T11:12:12Z"],
         "policy_expires_at": ["2034-07-09T11:12:14Z"]}

CASES = [
    ("截至时点：2034-07-08T11:12:13Z", TIMES, False),
    ("截至时点：2034-07-08 11:12:13 UTC", TIMES, False),
    ("截至时点：2034-07-08T13:12:13+02:00", TIMES, False),
    ("截至时点：2034-07-08T11:12:13Z", {"policy_as_of": ["2034-07-08T11:12:13.000Z"]}, False),
    ("生效时点：2034-07-08T11:12:12Z", TIMES, False),
    ("失效时点：2034-07-09T07:12:14-04:00", TIMES, False),
    ("生效时点：2034年7月8日（UTC）", TIMES, False),
    ("截至时点：2034-07-08T11:12:12Z", TIMES, True),
    ("生效时点：2034-07-08T11:12:13Z", TIMES, True),
    ("保证2034-07-08T11:12:13Z送达。", TIMES, True),
    ("截至时点：2034-07-08T11:12:13Z，届时付款。", TIMES, True),
    ("截至时点：2034-07-08T11:12:13Z；费用998美元。", TIMES, True),
    ("截至时点：2034-07-08T11:12:13.500Z", {"policy_as_of": ["2034-07-08T11:12:13.500Z"]}, True),
    ("截至时点：2034-07-08T11:12:13Z", {"policy_as_of": ["2034-07-08T11:12:13.500Z"]}, True),
    ("截至时点：2034-07-08T11:12:13Z.500", TIMES, True),
    ("截至时点：2034-07-08T11:12:13+00:00.500", TIMES, True),
    ("该版本自2034-07-08T11:12:12.500Z生效。", TIMES, True),
    ("截至2034-07-08T11:12:13.500Z，本次检索，未找到可引用的已发布档案归集政策。", TIMES, True),
]
CASES += [(f"{label}：{clock}{fraction}{zone}", TIMES, True)
          for label, clock in [("截至时点", "2034-07-08T11:12:13"),
                               ("生效时点", "2034-07-08T11:12:12"),
                               ("失效时点", "2034-07-09T11:12:14")]
          for fraction in (".5", ".500", ".000", ".0000001", ",5", ",500", ".5e-3", ". 500", " .500", ".500.0")
          for zone in ("Z", "+00:00")]


@pytest.mark.parametrize("text,times,blocked", CASES)
def test_complete_fractional_or_malformed_clock_never_matches_only_integral_prefix(text, times, blocked):
    # The shared regression helper exercises batch and 1/7-character streams,
    # requiring identical text and structured violations.
    check(text, times, blocked=blocked)
