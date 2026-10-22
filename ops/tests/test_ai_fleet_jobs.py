from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_contracts import (  # noqa: E402
    Capability, Intent, JobAction, JobState, PlanCreatePayload, PolicySnapshot,
    ProfileRef, ProfileSnapshot, Role, ScopeKind, StepOutcome, StepReceipt,
    TargetRef, TargetSnapshot,
)
from ops.ai_fleet_jobs import (  # noqa: E402
    ApprovalError, BudgetError, IdempotencyError, JobStore, LeaseError,
    ScopeError, TransitionError,
)


class FleetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "isolated-fleet.sqlite3"
        self.store = JobStore(self.db, lease_seconds=30)
        self.profiles = ProfileSnapshot(
            profiles=(ProfileRef(profile_id="test-profile", provider="fake", model="fake-model", endpoint_origin="https://fake.invalid", configured=True),),
            role_bindings={Role.PLANNER: "test-profile", Role.EXECUTOR: "test-profile", Role.VERIFIER: "test-profile"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _targets(self, count=1):
        return TargetSnapshot(targets=tuple(TargetRef(target_id=f"phone-{i+1}", serial=f"fake-serial-{i+1}", label=f"Phone {i+1}", online=True) for i in range(count)))

    def _plan(self, count=1, *, scope=ScopeKind.NAVIGATION, capabilities=None, key="fixturefixture"):
        policy = PolicySnapshot(scope=scope, capabilities=frozenset(capabilities or {Capability.OBSERVE, Capability.NAVIGATE, Capability.INPUT, Capability.APP_STATE}))
        payload = PlanCreatePayload(intent=Intent(text="Home to Settings and back", metadata={"source": "unit-test", "api_key": "must-not-persist"}), policy=policy, idempotency_key=key)
        created = self.store.create_plan(payload, self._targets(count), self.profiles)
        return created, payload

    def _approved(self, count=1, **kwargs):
        created, _ = self._plan(count, **kwargs)
        return self.store.approve_plan(created.plan.plan_id, created.approval_token, "ownerownerowner")

    def test_twenty_targets_are_durable_and_worker_cap_is_three(self):
        detail = self._approved(20)
        self.assertEqual(detail.target_count, 20)
        leases = [self.store.claim_next(f"worker-{i}") for i in range(4)]
        self.assertEqual(sum(lease is not None for lease in leases), 3)
        self.assertEqual({lease.target_id for lease in leases if lease}, {"phone-1", "phone-2", "phone-3"})
        self.assertIsNone(self.store.claim_next("worker-4"))
        reopened = JobStore(self.db)
        persisted = reopened.get_job(detail.job_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.target_count, 20)
        self.assertEqual(persisted.policy_snapshot.max_active_workers, 3)

    def test_atomic_race_and_per_device_exclusion(self):
        detail = self._approved(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.store.claim_next, ["race-a", "race-b"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        lease = next(result for result in results if result)
        self.assertIsNone(self.store.claim_next("another-worker"))
        self.store.heartbeat(lease.child_id, lease.worker_id, lease.fence)

    def test_approval_token_and_submission_are_idempotent(self):
        created, _ = self._plan()
        with self.assertRaises(ApprovalError):
            self.store.approve_plan(created.plan.plan_id, "wrongwrongwrong", "ownerownerowner")
        first = self.store.approve_plan(created.plan.plan_id, created.approval_token, "ownerownerowner")
        same = self.store.approve_plan(created.plan.plan_id, "not-reused-after-first", "different-key-999999")
        self.assertEqual(first.job_id, same.job_id)
        self.assertEqual(len(self.store.list_jobs()), 1)
        with self.assertRaises(IdempotencyError):
            self._plan(key="fixturefixture")

    def test_stale_fence_cannot_authorize_or_append_after_stop(self):
        detail = self._approved(1)
        lease = self.store.claim_next("worker-a")
        self.assertIsNotNone(lease)
        self.store.authorize(lease.child_id, lease.serial, Capability.NAVIGATE, lease.fence)
        self.store.control_job(detail.job_id, JobAction.STOP)
        with self.assertRaises(LeaseError):
            self.store.heartbeat(lease.child_id, lease.worker_id, lease.fence)
        receipt = StepReceipt(step_id="step-after-stop", child_id=lease.child_id, sequence=1, capability=Capability.NAVIGATE, outcome=StepOutcome.SUCCEEDED, summary="should be fenced")
        with self.assertRaises(LeaseError):
            self.store.append_step(lease.child_id, lease.worker_id, lease.fence, receipt)
        self.assertEqual(self.store.get_job(detail.job_id).state, JobState.STOPPED)

    def test_uncertain_is_not_auto_replayed_and_reconcile_is_explicit(self):
        detail = self._approved(1)
        lease = self.store.claim_next("worker-a")
        self.store.authorize(lease.child_id, lease.serial, Capability.NAVIGATE, lease.fence)
        receipt = StepReceipt(step_id="step-uncertain", child_id=lease.child_id, sequence=1, capability=Capability.NAVIGATE, outcome=StepOutcome.UNCERTAIN, summary="native result was unavailable", evidence={"status": "timeout", "screenshot": "secret-image"})
        stored = self.store.append_step(lease.child_id, lease.worker_id, lease.fence, receipt)
        self.assertEqual(stored.outcome, StepOutcome.UNCERTAIN)
        self.assertIsNone(self.store.claim_next("new-worker"))
        self.assertEqual(self.store.get_job(detail.job_id).state, JobState.UNCERTAIN)
        reconciled = self.store.reconcile_child(lease.child_id, JobState.SUCCEEDED, summary="owner verified safe outcome")
        self.assertEqual(reconciled.state, JobState.SUCCEEDED)

    def test_metadata_redaction_never_persists_raw_material(self):
        detail = self._approved(1)
        lease = self.store.claim_next("worker-a")
        self.store.authorize(lease.child_id, lease.serial, Capability.NAVIGATE, lease.fence)
        receipt = StepReceipt(step_id="redacted-step", child_id=lease.child_id, sequence=1, capability=Capability.NAVIGATE, outcome=StepOutcome.SUCCEEDED, summary="safe", evidence={"provider_response": "provider-secret", "screenshot": "raw-image-bytes", "visible": "Home"})
        self.store.append_step(lease.child_id, lease.worker_id, lease.fence, receipt)
        raw = self.db.read_bytes()
        self.assertNotIn(b"provider-secret", raw)
        self.assertNotIn(b"raw-image-bytes", raw)
        self.assertNotIn(b"must-not-persist", raw)
        self.assertIn(b"Home", raw)

    def test_scope_and_budget_fail_closed(self):
        _, payload = self._plan()
        with self.assertRaises(ScopeError):
            self.store.authorize("missing-child", "serial", Capability.REBOOT, 1)
        detail = self.store.get_job(self._approved(key="plan-key-budget-123456").job_id)
        reservation = self.store.reserve_budget(detail.job_id, Decimal("1.25"), "turn-1", price_known=True)
        self.assertEqual(reservation.amount_usd, Decimal("1.250000"))
        self.assertEqual(self.store.settle_budget(reservation.reservation_id, actual_amount_usd=Decimal("0.80")).settled_usd, Decimal("0.800000"))
        with self.assertRaises(BudgetError):
            self.store.reserve_budget(detail.job_id, Decimal("1"), "unknown-price", price_known=False)
        with self.assertRaises(BudgetError):
            self.store.reserve_budget(detail.job_id, Decimal("19"), "over-ceiling")

    def test_pause_resume_and_safe_retry(self):
        detail = self._approved(2)
        self.store.control_job(detail.job_id, JobAction.PAUSE)
        self.assertEqual(self.store.get_job(detail.job_id).state, JobState.PAUSED)
        self.assertIsNone(self.store.claim_next("paused-worker"))
        self.store.control_job(detail.job_id, JobAction.RESUME)
        lease = self.store.claim_next("active-worker")
        self.store.authorize(lease.child_id, lease.serial, Capability.NAVIGATE, lease.fence)
        self.store.finish_child(lease.child_id, lease.worker_id, lease.fence, JobState.FAILED, "known transient")
        self.store.control_job(detail.job_id, JobAction.RETRY)
        self.assertIn(self.store.get_job(detail.job_id).state, {JobState.RUNNING, JobState.RETRY})


if __name__ == "__main__":
    unittest.main()
