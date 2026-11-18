"""Host-side acceptance checks for the string normalization task."""


def run(candidate, _fixtures):
    cases = ((" HeLLo ", "hello"), ("   ", ""), ("A B", "a b"), ("\tÉLEVE\n", "éleve"))
    for value, expected in cases:
        actual = candidate.call("slug", value)
        assert isinstance(actual, str) and actual == expected
    return len(cases)
