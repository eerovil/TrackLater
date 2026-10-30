from tracklater.timemodules.interfaces import AbstractProvider


class _Provider(AbstractProvider):
    @staticmethod
    def _helper(value):
        return value * 2

    @classmethod
    def _make(cls):
        return cls()

    def fetch(self):
        return 'real'

    def test_fetch(self):
        return 'fake'


def test_static_and_class_methods_are_not_swapped_for_test_doubles():
    # Since Python 3.10 these are callable; wrapping them raised
    # NotImplementedError for want of a test_ method (Kimai, Toggl).
    assert _Provider._helper(3) == 6
    assert _Provider()._helper(3) == 6
    assert isinstance(_Provider._make(), _Provider)


def test_plain_methods_use_their_test_double_while_testing():
    assert _Provider().fetch() == 'fake'
