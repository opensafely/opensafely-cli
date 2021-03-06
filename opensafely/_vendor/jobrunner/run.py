"""
Script which polls the database for active (i.e. non-terminated) jobs, takes
the appropriate action for each job depending on its current state, and then
updates its state as appropriate.
"""
import datetime
import logging
import sys
import time

from .log_utils import configure_logging, set_log_context
from . import config
from .database import find_where, count_where, update, select_values
from .models import Job, State, StatusCode
from .manage_jobs import (
    JobError,
    start_job,
    job_still_running,
    finalise_job,
    cleanup_job,
)


log = logging.getLogger(__name__)


def main(exit_when_done=False, raise_on_failure=False):
    log.info("jobrunner.run loop started")
    while True:
        active_jobs = handle_jobs(raise_on_failure=raise_on_failure)
        if exit_when_done and len(active_jobs) == 0:
            break
        time.sleep(config.JOB_LOOP_INTERVAL)


def handle_jobs(raise_on_failure=False):
    active_jobs = find_where(Job, state__in=[State.PENDING, State.RUNNING])
    for job in active_jobs:
        # `set_log_context` ensures that all log messages triggered anywhere
        # further down the stack will have `job` set on them
        with set_log_context(job=job):
            if job.state == State.PENDING:
                handle_pending_job(job)
            elif job.state == State.RUNNING:
                handle_running_job(job)
        if raise_on_failure and job.state == State.FAILED:
            raise JobError("Job failed")
    return active_jobs


def handle_pending_job(job):
    awaited_states = get_states_of_awaited_jobs(job)
    if State.FAILED in awaited_states:
        mark_job_as_failed(
            job, "Not starting as dependency failed", code=StatusCode.DEPENDENCY_FAILED
        )
    elif any(state != State.SUCCEEDED for state in awaited_states):
        set_message(
            job, "Waiting on dependencies", code=StatusCode.WAITING_ON_DEPENDENCIES
        )
    else:
        if not job_running_capacity_available():
            set_message(
                job, "Waiting for available workers", code=StatusCode.WAITING_ON_WORKERS
            )
        else:
            try:
                set_message(job, "Preparing")
                start_job(job)
            except JobError as exception:
                mark_job_as_failed(job, exception)
                cleanup_job(job)
            except Exception:
                mark_job_as_failed(job, "Internal error when starting job")
                raise
            else:
                mark_job_as_running(job)


def handle_running_job(job):
    if job_still_running(job):
        set_message(job, "Running")
    else:
        try:
            set_message(job, "Finished, checking status and extracting outputs")
            job = finalise_job(job)
            # We expect the job to be transitioned into its final state at this
            # point
            assert job.state in [State.SUCCEEDED, State.FAILED]
        except JobError as exception:
            mark_job_as_failed(job, exception)
            # Question: do we want to clean up failed jobs? Given that we now
            # tag all job-runner volumes and containers with a specific label
            # we could leave them around for debugging purposes and have a
            # cronjob which cleans them up a few days after they've stopped.
            cleanup_job(job)
        except Exception:
            mark_job_as_failed(job, "Internal error when finalising job")
            # We deliberately don't clean up after an internal error so we have
            # some change of debugging. It's also possible, after fixing the
            # error, to manually flip the state of the job back to "running" in
            # the database and the code will then be able to finalise it
            # correctly without having to re-run the job.
            raise
        else:
            mark_job_as_completed(job)
            cleanup_job(job)


def get_states_of_awaited_jobs(job):
    job_ids = job.wait_for_job_ids
    if not job_ids:
        return []
    return select_values(Job, "state", id__in=job_ids)


def mark_job_as_failed(job, error, code=None):
    if isinstance(error, str):
        message = error
    else:
        message = f"{type(error).__name__}: {error}"
    set_state(job, State.FAILED, message, code=code)


def mark_job_as_running(job):
    set_state(job, State.RUNNING, "Running")


def mark_job_as_completed(job):
    # Completed means either SUCCEEDED or FAILED. We just save the job to the
    # database exactly as is with the exception of setting the completed at
    # timestamp
    assert job.state in [State.SUCCEEDED, State.FAILED]
    job.completed_at = int(time.time())
    update(job)
    log.info(job.status_message, extra={"status_code": job.status_code})


def set_state(job, state, message, code=None):
    timestamp = int(time.time())
    if state == State.RUNNING:
        job.started_at = timestamp
    elif state == State.FAILED or state == State.SUCCEEDED:
        job.completed_at = timestamp
    job.state = state
    job.status_message = message
    job.status_code = code
    job.updated_at = timestamp
    update(
        job,
        update_fields=[
            "state",
            "status_message",
            "status_code",
            "updated_at",
            "started_at",
            "completed_at",
        ],
    )
    log.info(job.status_message, extra={"status_code": job.status_code})


def set_message(job, message, code=None):
    timestamp = int(time.time())
    # If message has changed then update and log
    if job.status_message != message:
        job.status_message = message
        job.status_code = code
        job.updated_at = timestamp
        update(job, update_fields=["status_message", "status_code", "updated_at"])
        log.info(job.status_message, extra={"status_code": job.status_code})
    # If the status message hasn't changed then we only update the timestamp
    # once a minute. This gives the user some confidence that the job is still
    # active without writing to the database every single time we poll
    elif timestamp - job.updated_at >= 60:
        job.updated_at = timestamp
        update(job, update_fields=["updated_at"])
        # For long running jobs we don't want to fill the logs up with "Job X
        # is still running" messages, but it is useful to have semi-regular
        # confirmations in the logs that it is still running. The below will
        # log approximately once every 10 minutes.
        if datetime.datetime.fromtimestamp(timestamp).minute % 10 == 0:
            log.info(job.status_message, extra={"status_code": job.status_code})


def job_running_capacity_available():
    running_jobs = count_where(Job, state=State.RUNNING)
    return running_jobs < config.MAX_WORKERS


if __name__ == "__main__":
    configure_logging()

    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
