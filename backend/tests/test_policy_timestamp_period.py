"""Timestamp integrity across a period must not change ordinary sentence ends."""
import pytest

from app.core.answer_policy import AnswerPolicy, PolicyStreamGuard, split_sentences

PREFIX = "截至时点：2034-07-08T11:12:13"
TIMES = {"policy_as_of": ["2034-07-08T11:12:13Z"]}
NORMAL = [
    ("$980. We will review.", ["$980.", " We will review."]),
    ("$12.50. We will review.", ["$12.50.", " We will review."]),
    ("Dr. Lee reviewed it. Done.", ["Dr. Lee reviewed it.", " Done."]),
    ("材料已保存。下一步待审核。", ["材料已保存。", "下一步待审核。"]),
    (PREFIX + ". Next statement.", [PREFIX + ".", " Next statement."]),
    (PREFIX + ". 500 items are listed.", [PREFIX + ".", " 500 items are listed."]),
    (PREFIX + ". 3.5 units are listed.", [PREFIX + ".", " 3.5 units are listed."]),
]


@pytest.mark.parametrize("text,expected", NORMAL)
def test_normal_sentence_boundaries_and_lossless_stream_reassembly(text, expected):
    sentences, tail = split_sentences(text)
    assert sentences == expected and tail == ""
    for size in (1, 2, 7):
        buffer, complete = "", []
        for i in range(0, len(text), size):
            buffer += text[i:i + size]
            output, buffer = split_sentences(buffer, final=False)
            complete.extend(output)
        output, buffer = split_sentences(buffer, final=True)
        complete.extend(output)
        assert complete == expected and buffer == ""
        assert "".join(complete) == text


@pytest.mark.parametrize("suffix", ["500Z", "500+00:00"])
def test_timestamp_period_waits_for_following_digits_in_each_stream_fragment(suffix):
    stream = PolicyStreamGuard(AnswerPolicy(), [], reference_times=TIMES)
    for char in PREFIX + ". ":
        assert stream.feed(char) == ""
    output = "".join(stream.feed(char) for char in suffix) + stream.flush()
    batch = AnswerPolicy().enforce(PREFIX + ". " + suffix, [], reference_times=TIMES)
    assert batch.blocked and stream.violations
    assert output == batch.text
    assert [v.to_dict() for v in stream.violations] == [v.to_dict() for v in batch.violations]


@pytest.mark.parametrize("ending", [".", ". ", ". Next statement.", ". 500 items are listed."])
def test_complete_integral_timestamp_sentence_is_unchanged_after_final_decision(ending):
    text = PREFIX + ending
    batch = AnswerPolicy().enforce(text, [], reference_times=TIMES)
    assert not batch.blocked and batch.text == text
    for size in (1, 7):
        stream = PolicyStreamGuard(AnswerPolicy(), [], reference_times=TIMES)
        output = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size)) + stream.flush()
        assert output == text and not stream.violations
