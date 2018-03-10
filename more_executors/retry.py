from collections import namedtuple
from concurrent.futures import Executor, Future
from threading import RLock, Thread, Event
from datetime import datetime
from datetime import timedelta

import logging

_LOG = logging.getLogger('RetryExecutor')


def _total_seconds(td):
    # Python 2.6 compat - equivalent of timedelta.total_seconds
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6


class RetryPolicy(object):

    def should_retry(self, attempt, return_value, exception):
        return False

    def sleep_time(self, attempt, return_value, exception):
        return 0


class ExceptionRetryPolicy(RetryPolicy):
    """Retries on any exceptions under the given base class,
    up to a fixed number of attempts, with an exponential
    backoff between each attempt."""

    def __init__(self, max_attempts, exponent, max_sleep, exception_base):
        self._max_attempts = max_attempts
        self._exponent = exponent
        self._max_sleep = max_sleep
        self._exception_base = exception_base

    @classmethod
    def new_default(cls):
        return cls(10, 2.0, 120, Exception)

    def should_retry(self, attempt, return_value, exception):
        if not exception:
            return False
        if attempt >= self._max_attempts:
            return False
        return isinstance(exception, self._exception_base)

    def sleep_time(self, attempt, return_value, exception):
        return min(attempt ** self._exponent, self._max_sleep)


_RetryJob = namedtuple('_RetryJob',
                       ['policy', 'delegate_future', 'future',
                        'attempt', 'when', 'fn', 'args', 'kwargs'])


class _RetryFuture(Future):

    def __init__(self, executor):
        super(_RetryFuture, self).__init__()
        self.delegate_future = None
        self._executor = executor

    def running(self):
        if self.delegate_future:
            return self.delegate_future.running()
        return False

    def done(self):
        if self.delegate_future:
            return self.delegate_future.done()
        return super(_RetryFuture, self).done()

    def cancel(self):
        with self._executor._lock:
            if not self._executor._cancel(self):
                return False
            return super(_RetryFuture, self).cancel()


class RetryExecutor(Executor):
    """An Executor which delegates to another Executor while adding
    transparent retry behavior.

    Callables submitted to this Executor have the following semantics:

    - The callables are submitted to the delegate Executor, and may be
      submitted more than once if retries are required.

    - The callables may be submitted to that executor from a different
      thread than the calling thread.

    - The returned Futures from this executor are only resolved or
      failed once the callable either succeeded, or all retries
      were exhausted.
    """

    def __init__(self, delegate, retry_policy):
        self._delegate = delegate
        self._default_retry_policy = retry_policy
        self._jobs = []
        self._submit_thread = Thread(
            name='RetryExecutor', target=self._submit_loop)
        self._submit_thread.daemon = True
        self._submit_event = Event()
        self._shutdown = False

        # This lock should be held when:
        # - mutating _jobs
        # - cancelling a future
        self._lock = RLock()

        self._submit_thread.start()

    def shutdown(self, wait=True):
        _LOG.info("Shutting down.")
        self._shutdown = True
        self._wake_thread()
        self._delegate.shutdown(wait)
        if wait:
            _LOG.info("Waiting for thread")
            self._submit_thread.join()
        _LOG.debug("Shutdown complete")

    @classmethod
    def new_default(cls, delegate):
        """Returns an executor with a reasonable default retry policy."""
        return cls(delegate, ExceptionRetryPolicy.new_default())

    def submit(self, fn, *args, **kwargs):
        """Submit a callable with this executor's default retry policy."""
        return self.submit_retry(
            self._default_retry_policy, fn, *args, **kwargs)

    def submit_retry(self, retry_policy, fn, *args, **kwargs):
        """Submit a callable with a specific retry policy."""
        future = _RetryFuture(self)

        job = _RetryJob(retry_policy, None, future, 0,
                        datetime.utcnow(), fn, args, kwargs)
        self._append_job(job)

        # Let the submit thread know it should wake up to check for new jobs
        self._wake_thread()

        _LOG.debug("Returning future %s", future)
        return future

    def _wake_thread(self):
        self._submit_event.set()

    def _get_next_job(self):
        # Find and return the next job to be executed, if any.
        # This means a job with when < now, or if there's none,
        # then the job with the minimum value of when.
        min_job = None
        now = datetime.utcnow()
        for job in self._jobs:
            if job.delegate_future:
                # It's already running, skip
                continue
            if job.when <= now:
                # job is overdue, just do it
                return job
            elif not min_job:
                min_job = job
            elif job.when < min_job.when:
                min_job = job
        return min_job

    def _submit_wait(self, timeout=None):
        self._submit_event.wait(timeout)
        self._submit_event.clear()

    def _submit_loop(self):
        # Runs in a separate thread continuously submitting to the delegate
        # executor until no jobs are ready, or waiting until next job is ready
        while not self._shutdown:
            _LOG.debug("_submit_loop iter")
            job = self._get_next_job()
            if not job:
                _LOG.debug("No jobs at all. Waiting...")
                self._submit_wait()
                continue

            now = datetime.utcnow()
            if job.when < now:
                # Can submit immediately and check for next job
                self._submit_now(job)
                continue

            # There is nothing to submit immediately.
            # Sleep until either:
            # - reaching the time of the nearest job, or...
            # - woken up by condvar
            delta = _total_seconds(job.when - now)
            _LOG.debug("No ready job.  Waiting: %s", delta)
            self._submit_wait(delta)

    def _submit_now(self, job):
        # Pop job since we'll replace it.
        # We need to hold the lock for the entire duration so that other
        # threads won't see _jobs between our removal and re-add of the job
        with self._lock:
            self._pop_job(job)

            if job.future.cancelled():
                _LOG.debug(
                    "future cancelled %s - not submitting to delegate",
                    job.future)
                return

            delegate_future = self._delegate.submit(
                job.fn, *job.args, **job.kwargs)
            job.future.delegate_future = delegate_future
            _LOG.debug("Submitted: %s", job)

            new_job = _RetryJob(
                job.policy,
                delegate_future,
                job.future,
                job.attempt + 1,
                None,
                job.fn, job.args, job.kwargs
            )
            self._append_job(new_job)

        delegate_future.add_done_callback(self._delegate_callback)

    def _pop_job(self, job):
        with self._lock:
            for idx, pending in enumerate(self._jobs):
                if pending is job:
                    return self._jobs.pop(idx)

    def _append_job(self, job):
        with self._lock:
            self._jobs.append(job)

    def _job_for_delegate(self, delegate_future):
        with self._lock:
            for idx, job in enumerate(self._jobs):
                if job.delegate_future == delegate_future:
                    return self._jobs.pop(idx)

    def _retry(self, job, sleep_time):
        _LOG.debug("Will retry: %s", job)

        new_job = _RetryJob(
            job.policy,
            None,
            job.future,
            job.attempt,
            datetime.utcnow() + timedelta(seconds=sleep_time),
            job.fn,
            job.args,
            job.kwargs
        )
        self._append_job(new_job)

        self._wake_thread()

    def _cancel(self, future):
        with self._lock:
            if future.cancelled():
                return True
            if future.done():
                return False
            for idx, job in enumerate(self._jobs):
                if job.future is future:
                    _LOG.debug("Try cancel: %s", job)
                    delegate_future = job.delegate_future

                    if not delegate_future:
                        _LOG.debug("Successful cancel - no delegate: %s", job)
                        self._jobs.pop(idx)
                        return True

                    _LOG.debug("Try cancel delegate: %s", job)

                    if delegate_future.cancel():
                        _LOG.debug("Successful cancel: %s", job)
                        future.delegate_future = None
                        # Don't remove from _jobs here,
                        # the callback attached to delegate_future is expected
                        # to take care of that
                        return True

                    _LOG.debug("Could not cancel: %s", job)
                    return False
        # We don't have the job at all then we probably cancelled it already
        _LOG.debug("cancel called on orphan future %s", future)
        return True

    def _delegate_callback(self, delegate_future):
        assert delegate_future.done(), \
            "BUG: callback invoked while future not done!"

        _LOG.debug("Callback activated for %s", delegate_future)

        job = self._job_for_delegate(delegate_future)
        if not job:
            return

        exception = delegate_future.exception()
        if exception:
            result = None
        else:
            result = delegate_future.result()

        should_retry = job.policy.should_retry(job.attempt, result, exception)
        if should_retry:
            sleep_time = job.policy.sleep_time(job.attempt, result, exception)
            self._retry(job, sleep_time)
            return

        _LOG.debug("Finalizing %s", job)

        # OK, it won't be retried.  Resolve the future.
        if delegate_future.cancelled():
            # nothing to do
            return

        if exception:
            job.future.set_exception(exception)
        else:
            job.future.set_result(result)

        _LOG.debug("Finalized %s", job)
