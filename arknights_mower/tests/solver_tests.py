import unittest
from threading import Event
from unittest.mock import MagicMock, call, patch

from arknights_mower.utils import config
from arknights_mower.utils.csleep import MowerExit
from arknights_mower.utils.solver import BaseSolver


class TestSolverStartupLaunch(unittest.TestCase):
    """默认重试三次；任务入口可让首次连接快速失败并立即恢复模拟器。"""

    def setUp(self):
        self.enterContext(patch.object(config.conf, "close_simulator_when_idle", False))
        self.enterContext(patch.object(config.conf, "adb", "127.0.0.1:16384"))
        self.enterContext(patch.object(config.conf.droidcast, "enable", False))
        self.enterContext(patch.object(config.conf, "touch_method", "scrcpy"))
        self.stop = Event()
        self.enterContext(patch.object(config, "stop_mower", self.stop))
        self.scrcpy = self.enterContext(patch("arknights_mower.utils.solver.Scrcpy"))
        self.device_mock = self.enterContext(
            patch("arknights_mower.utils.solver.Device")
        )
        self.device_mock.side_effect = RuntimeError("Device connection failure")
        self.enterContext(patch("arknights_mower.utils.solver.Session"))
        self.recog = self.enterContext(patch("arknights_mower.utils.solver.Recognizer"))

    def test_first_connection_fails_fast_without_retry(self):
        with self.assertRaisesRegex(ConnectionError, "设备连接 1 次失败"):
            BaseSolver(connection_retries=1)
        self.device_mock.assert_called_once_with(wait_for_device=False)
        self.recog.assert_not_called()

    def test_transient_failure_retries_connection(self):
        device = MagicMock()
        self.device_mock.side_effect = [ConnectionError("offline")] * 2 + [device]
        solver = BaseSolver()
        self.assertIs(solver.device, device)
        self.assertEqual(self.device_mock.call_count, 3)
        self.recog.assert_called_once_with(device)

    def test_no_configured_adb_still_retries_connection(self):
        device = MagicMock()
        self.device_mock.side_effect = [ConnectionError("no device"), device]
        with patch.object(config.conf, "adb", ""):
            solver = BaseSolver()
        self.assertIs(solver.device, device)
        self.assertEqual(self.device_mock.call_count, 2)

    def test_droidcast_failure_prevents_startup_success(self):
        device = MagicMock()
        device.start_droidcast.return_value = False
        self.device_mock.side_effect = None
        self.device_mock.return_value = device
        with patch.object(config.conf.droidcast, "enable", True):
            with self.assertRaisesRegex(ConnectionError, "设备连接 3 次失败"):
                BaseSolver()
        self.assertEqual(device.start_droidcast.call_count, 3)
        self.recog.assert_not_called()
        self.scrcpy.assert_not_called()

    def test_droidcast_recovers_before_touch_and_recognition(self):
        device = MagicMock()
        device.start_droidcast.side_effect = [False, False, True]
        self.device_mock.side_effect = None
        self.device_mock.return_value = device
        actions = MagicMock()
        actions.attach_mock(device.start_droidcast, "droidcast")
        actions.attach_mock(self.scrcpy, "scrcpy")
        actions.attach_mock(self.recog, "recog")
        with patch.object(config.conf.droidcast, "enable", True):
            solver = BaseSolver()
        self.assertIs(solver.device, device)
        self.assertEqual(
            actions.mock_calls,
            [call.droidcast()] * 3 + [call.scrcpy(device.client), call.recog(device)],
        )

    def test_running_device_connects_once(self):
        self.device_mock.side_effect = None
        solver = BaseSolver()
        self.assertIs(solver.device, self.device_mock.return_value)
        self.device_mock.assert_called_once_with(wait_for_device=True)

    def test_persistent_failure_bounds_retries_and_preserves_cause(self):
        failure = ConnectionError("offline")
        self.device_mock.side_effect = failure
        with self.assertRaisesRegex(ConnectionError, "设备连接 3 次失败") as raised:
            BaseSolver()
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(self.device_mock.call_count, 3)
        self.recog.assert_not_called()

    def test_mower_exit_does_not_retry(self):
        self.device_mock.side_effect = MowerExit
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.device_mock.assert_called_once_with(wait_for_device=True)

    def test_unsupported_resolution_does_not_retry(self):
        self.device_mock.side_effect = None
        self.device_mock.return_value.check_resolution.return_value = False
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.device_mock.assert_called_once_with(wait_for_device=True)
        self.recog.assert_not_called()

    def test_stop_before_initialization_does_not_connect(self):
        self.stop.set()
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.device_mock.assert_not_called()

    def test_stop_during_connection_failure_does_not_retry(self):
        def fail_and_stop(**kwargs):
            self.stop.set()
            raise ConnectionError("offline")

        self.device_mock.side_effect = fail_and_stop
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.device_mock.assert_called_once_with(wait_for_device=True)


class TestTapElement(unittest.TestCase):
    """tap_element：find 返回空时不 tap、不抛错、返回 False；找到时 tap 中心、返回 True。"""

    def setUp(self):
        self.solver = BaseSolver.__new__(BaseSolver)
        self.find_patch = patch.object(BaseSolver, "find")
        self.tap_patch = patch.object(BaseSolver, "tap")
        self.find_mock = self.find_patch.start()
        self.tap_mock = self.tap_patch.start()
        self.addCleanup(self.find_patch.stop)
        self.addCleanup(self.tap_patch.stop)

    def test_missing_element_no_tap_returns_false(self):
        self.find_mock.return_value = None
        result = self.solver.tap_element("confirm_blue")
        self.assertFalse(result)
        self.tap_mock.assert_not_called()

    def test_found_element_taps_center_returns_true(self):
        self.find_mock.return_value = [[100, 200], [300, 400]]
        result = self.solver.tap_element("confirm_blue")
        self.assertTrue(result)
        self.tap_mock.assert_called_once_with([[100, 200], [300, 400]], 0.5, 0.5, 1)


if __name__ == "__main__":
    unittest.main()
