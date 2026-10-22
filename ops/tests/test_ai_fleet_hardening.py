from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_contracts import (
    Capability,
    Intent,
    JobAction,
    JobState,
    PlanCreatePayload,
    PolicySnapshot,
    ProfileRef,
    ProfileSnapshot,
    Role,
    StepOutcome,
    StepReceipt,
    TargetRef,
    TargetSnapshot,
)
from ops.ai_fleet_jobs import JobStore, LeaseError


class FleetHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "isolated.sqlite3")
        self.profiles = ProfileSnapshot(
            profiles=(ProfileRef(profile_id="p", provider="fake", model="m", endpoint_origin="https://fake.invalid", configured=True),),
            role_bindings={Role.PLANNER: "p", Role.EXECUTOR: "p", Role.VERIFIER: "p"},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _approved(self):
        targets = TargetSnapshot(targets=(TargetRef(target_id="phone-1", serial="serial-1", label="Phone 1", online=True),))
        policy = PolicySnapshot(capabilities=frozenset({Capability.OBSERVE, Capability.NAVIGATE}))
        created = self.store.create_plan(PlanCreatePayload(intent=Intent(text="Navigate safely"), policy=policy), targets, self.profiles)
        return self.store.approve_plan(created.plan.plan_id, created.approval_token, "ownerownerowner")

    def test_pause_fences_running_child_and_resume_does_not_replay_it(self):
        detail = self._approved()
        lease = self.store.claim_next("worker")
        assert lease is not None
        self.store.authorize(lease.child_id, lease.serial, Capability.NAVIGATE, lease.fence)

        paused = self.store.control_job(detail.job_id, JobAction.PAUSE)
        child = paused.children[0]
        self.assertEqual(paused.state, JobState.PAUSED)
        self.assertEqual(child.state, JobState.WAITING_FOR_OWNER)
        self.assertTrue(paused.checkpoints)
        with self.assertRaises(LeaseError):
            self.store.append_step(
                lease.child_id,
                lease.worker_id,
                lease.fence,
                StepReceipt(
                    step_id="late-step",
                    child_id=lease.child_id,
                    sequence=1,
                    capability=Capability.NAVIGATE,
                    outcome=StepOutcome.SUCCEEDED,
                    summary="must be fenced",
                ),
            )

        resumed = self.store.control_job(detail.job_id, JobAction.RESUME)
        self.assertEqual(resumed.state, JobState.WAITING_FOR_OWNER)
        self.assertIsNone(self.store.claim_next("new-worker"))

    def test_receipts_are_bounded_to_job_and_preserve_safe_metadata_only(self):
        detail = self._approved()
        lease = self.store.claim_next("worker")
        assert lease is not None
        self.store.authorize(lease.child_id, lease.serial, Capability.NAVIGATE, lease.fence)
        self.store.append_step(
            lease.child_id,
            lease.worker_id,
            lease.fence,
            StepReceipt(
                step_id="safe-step",
                child_id=lease.child_id,
                sequence=1,
                capability=Capability.NAVIGATE,
                outcome=StepOutcome.SUCCEEDED,
                summary="navigated",
                evidence={"screenshot": "not persisted", "visible": "Home"},
            ),
        )
        receipts = self.store.get_step_receipts(detail.job_id, child_id=lease.child_id, limit=1)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].summary, "navigated")
        self.assertNotIn("screenshot", receipts[0].evidence)
        self.assertEqual(receipts[0].evidence.get("visible"), "Home")
        self.assertRaises(ValueError, self.store.get_step_receipts, detail.job_id, limit=101)


if __name__ == "__main__":
    unittest.main()


class WorkerErrorSummaryTests(unittest.TestCase):
    def test_generic_error_payload_never_reaches_summary(self):
        from ops.ai_fleet_controller import _worker_error_summary
        value = _worker_error_summary(RuntimeError('password=private-test-value provider UI must not be printed'))
        self.assertIn('RuntimeError', value)
        self.assertNotIn('private-test-value', value)
        self.assertIn('native state requires review', value)

    def test_only_identifier_sdk_error_chain_is_accepted(self):
        from ops.ai_fleet_controller import _worker_error_summary
        from gitd.services.sdk_worker_protocol import WorkerProtocolError
        self.assertIn('WorkerProtocolError/ValueError/TypeError', _worker_error_summary(WorkerProtocolError('SDK failed: ValueError/TypeError')))
        self.assertNotIn('password', _worker_error_summary(WorkerProtocolError('SDK failed: password=private-test-value')))
