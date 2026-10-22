import unittest
from gitd.services.fleet_sdk_broker import _asks_reboot, _is_read_only_report


class ReadOnlyAndRebootIntentTests(unittest.TestCase):
    def test_read_only_report_intent(self):
        self.assertTrue(_is_read_only_report("Report this phone's group, proxy, stream, Sync and recovery status without changing anything"))
        self.assertTrue(_is_read_only_report("Read-only check of proxy status"))
        self.assertFalse(_is_read_only_report("Open the Google Play Store app"))
        self.assertFalse(_is_read_only_report("Reboot this phone"))
        self.assertFalse(_is_read_only_report("Report status"))  # must state it is read-only / no changes

    def test_reboot_intent(self):
        self.assertTrue(_asks_reboot("Reboot this phone once"))
        self.assertTrue(_asks_reboot("Restart the device"))
        self.assertFalse(_asks_reboot("Open Settings"))


class QueueKeepsDispatchingTests(unittest.TestCase):
    def test_claim_query_allows_waiting_and_uncertain_parents_only(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "ai_fleet_jobs.py").read_text(encoding="utf-8")
        self.assertIn("AND parent.state IN (?,?,?,?,?)", src)
        self.assertIn("JobState.WAITING_FOR_OWNER.value, JobState.UNCERTAIN.value, JobState.RETRY.value, JobState.RUNNING.value, now.isoformat()", src)
        # Paused, stopping, stopped and manual-recovery parents remain excluded from dispatch.
        claim = src[src.index("def claim_next"):src.index("def heartbeat")]
        self.assertNotIn("parent.state IN (?,?,?,?,?,?", claim)
        self.assertNotIn("JobState.PAUSED", claim)
        self.assertNotIn("JobState.STOPPING", claim)
        # Owner approval closes the open checkpoint for that phone.
        rec = src[src.index("def reconcile_child"):src.index("def reserve_budget")]
        self.assertIn("UPDATE checkpoints SET status=? WHERE child_id=? AND status=?", rec)


if __name__ == "__main__":
    unittest.main()
