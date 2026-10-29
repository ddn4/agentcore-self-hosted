import pytest

pytestmark = pytest.mark.unit


def test_package_imports() -> None:
    import payments_mock

    assert payments_mock.__doc__
