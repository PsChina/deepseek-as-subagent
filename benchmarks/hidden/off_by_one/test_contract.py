"""Host-side acceptance checks for the summation task."""


def run(candidate, _fixtures):
    cases = (([], 0), ([2, 3, -1], 4), ([0, 0, 7, 7], 14), ([-9], -9))
    for values, expected in cases:
        actual = candidate.call("total", values)
        assert type(actual) is int and actual == expected
    return len(cases)
