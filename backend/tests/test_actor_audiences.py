"""Which policy audiences a staff role may read in the knowledge workbench."""
from app.core.actor import ActorContext
from app.core.policy_scope import PolicyMetadata


def test_the_default_wildcard_audience_is_readable_by_every_staff_role():
    assert PolicyMetadata().audience == "*"
    for roles in ({"employee"}, {"operations"}):
        assert "*" in ActorContext("a", frozenset(roles), "staff").readable_audiences()
    assert ActorContext("a", frozenset({"admin"}), "staff").readable_audiences() is None


def test_operations_audience_stays_limited_to_operations_and_admin():
    assert "operations" not in ActorContext("a", frozenset({"employee"}), "staff").readable_audiences()
    assert "operations" in ActorContext("a", frozenset({"operations"}), "staff").readable_audiences()
