"""Tests for cronjob action='run' immediate execution (#41037, #52705).

Before this fix, `cronjob(action='run')` only set next_run_at=now and returned
success, relying on the scheduler ticker to actually run the job. With no
gateway/ticker active (e.g. a CLI-only Windows setup) the job never executed and
last_run_at stayed null forever. `run` now claims the job (at-most-once) and
queues it onto the shared scheduler pools immediately, so the run starts right
away without blocking the calling agent turn.
"""
import json
import threading
import time
from unittest.mock import patch

from tools.cronjob_tools import cronjob, _execute_job_now


_JOB = {"id": "job-run-1", "name": "manual run", "prompt": "hi",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


class TestCronjobRunExecutesImmediately:
    def test_run_action_claims_and_dispatches_via_scheduler_pool(self):
        """action='run' must claim the job then queue it on the scheduler pool."""
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True) as m_claim, \
             patch("cron.scheduler.dispatch_job", return_value=object()) as m_dispatch, \
             patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["success"] is True
        assert out["job"]["executed"] is True
        assert out["job"]["execution_pending"] is True
        assert out["job"]["execution_mode"] == "background"
        m_claim.assert_called_once_with("job-run-1")   # at-most-once claim taken
        m_dispatch.assert_called_once()                # queued onto shared pool

    def test_run_skips_when_claim_lost(self):
        """If the scheduler already holds the fire claim, do NOT double-run."""
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=False), \
             patch("cron.scheduler.dispatch_job") as m_dispatch, \
             patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["success"] is True
        assert out["job"]["executed"] is False
        assert out["job"]["execution_success"] is False
        assert "execution_skipped" in out["job"]
        m_dispatch.assert_not_called()  # claim lost -> never queued

    def test_run_skips_when_job_is_already_running(self):
        """If the running-set guard rejects dispatch, surface the skip cleanly."""
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.dispatch_job", return_value=None), \
             patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["job"]["executed"] is False
        assert out["job"]["execution_success"] is False
        assert "already running" in out["job"]["execution_skipped"].lower()

    def test_run_returns_before_background_job_finishes(self):
        """The tool call should return without waiting for a slow run_one_job."""
        import cron.scheduler as sched

        sched._parallel_pool = None
        sched._parallel_pool_max_workers = None
        sched._running_job_ids.clear()

        barrier = threading.Barrier(2, timeout=5)

        def slow_run_one_job(_job, **_kwargs):
            barrier.wait()
            return True

        try:
            with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
                 patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
                 patch("cron.scheduler.run_one_job", side_effect=slow_run_one_job), \
                 patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
                start = time.monotonic()
                out = json.loads(cronjob(action="run", job_id="job-run-1"))
                elapsed = time.monotonic() - start

            assert out["job"]["executed"] is True
            assert out["job"]["execution_pending"] is True
            assert elapsed < 1.0
        finally:
            barrier.wait()
            time.sleep(0.1)
            sched._shutdown_parallel_pool()

    def test_execute_job_now_bails_without_claim(self):
        """_execute_job_now never queues a job when the claim is lost."""
        with patch("tools.cronjob_tools.claim_job_for_fire", return_value=False), \
             patch("cron.scheduler.dispatch_job") as m_dispatch:
            res = _execute_job_now(dict(_JOB))
        assert res["claimed"] is False
        assert res["started"] is False
        assert res["success"] is False
        m_dispatch.assert_not_called()

    def test_execute_job_now_marks_failure_on_exception(self):
        """A dispatch failure is captured, marked failed, not propagated."""
        with patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.dispatch_job", side_effect=RuntimeError("boom")), \
             patch("tools.cronjob_tools.mark_job_run") as m_mark, \
             patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
            res = _execute_job_now(dict(_JOB))
        assert res["claimed"] is True
        assert res["started"] is False
        assert res["success"] is False
        assert "boom" in res["error"]
        m_mark.assert_called_once()
