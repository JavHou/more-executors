from concurrent.futures import ThreadPoolExecutor
import traceback
from pytest import fixture
from hamcrest import assert_that, equal_to, instance_of, calling, raises

from more_executors.map import MapExecutor


@fixture
def executor():
    return ThreadPoolExecutor()


def div(x, y):
    return x / y


def div10(x):
    return div(10, x)


def test_basic_map(executor):
    map_executor = MapExecutor(executor, lambda x: x * 10)

    inputs = [1, 2, 3]
    expected_result = [20, 40, 60]

    futures = [map_executor.submit(lambda x: x * 2, x) for x in inputs]
    results = [f.result() for f in futures]

    assert_that(results, equal_to(expected_result))


def get_traceback(future):
    exception = future.exception()
    if "__traceback__" in dir(exception):
        return exception.__traceback__
    return future.exception_info()[1]


def test_map_exception(executor):
    map_executor = MapExecutor(executor, div10)

    inputs = [1, 2, 0]

    futures = [map_executor.submit(lambda v: v, x) for x in inputs]

    # First two should succeed and give the mapped value
    assert_that(futures[0].result(), equal_to(10))
    assert_that(futures[1].result(), equal_to(5))

    # The third should have crashed
    assert_that(futures[2].exception(), instance_of(ZeroDivisionError))
    assert_that(calling(futures[2].result), raises(ZeroDivisionError))

    # It should give an accurate traceback
    tb = get_traceback(futures[2])
    formatted = traceback.format_tb(tb)
    assert "div10" in "".join(formatted)


def test_exception_propagate(executor):
    map_executor = MapExecutor(executor, lambda x: x)
    f = map_executor.submit(div10, 0.0)

    # It should have failed
    assert_that(f.exception(), instance_of(ZeroDivisionError))

    # It should give an accurate traceback
    tb = get_traceback(f)
    formatted = traceback.format_tb(tb)
    assert "div10" in "".join(formatted)
