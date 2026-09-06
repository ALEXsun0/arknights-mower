import unittest
from threading import Event
from unittest.mock import MagicMock, call, patch

from arknights_mower.utils import config
from arknights_mower.utils.csleep import MowerExit
from arknights_mower.utils.solver import BaseSolver


class TestSolverStartupLaunch(unittest.TestCase):
    """首次连接失败立即重启，任务结束后的关闭选项不限制连接恢复。"""

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
        self.restart_mock = self.enterContext(
            patch("arknights_mower.utils.solver.restart_simulator", return_value=True)
        )
        self.recog = self.enterContext(patch("arknights_mower.utils.solver.Recognizer"))

    def test_first_failure_restarts_before_retry_regardless_of_idle_option(self):
        for close_when_idle in (False, True):
            with (
                self.subTest(close_when_idle=close_when_idle),
                patch.object(config.conf, "close_simulator_when_idle", close_when_idle),
            ):
                device = MagicMock()
                self.device_mock.side_effect = [ConnectionError("offline"), device]
                actions = MagicMock()
                actions.attach_mock(self.device_mock, "device")
                actions.attach_mock(self.restart_mock, "restart")
                solver = BaseSolver()
                self.assertIs(solver.device, device)
                self.assertEqual(
                    actions.mock_calls,
                    [
                        call.device(wait_for_device=False),
                        call.restart(),
                        call.device(wait_for_device=True),
                    ],
                )
                self.recog.assert_called_with(device)
                device._safe_reconnect.assert_not_called()

    def test_no_configured_adb_still_restarts_and_connects(self):
        device = MagicMock()
        self.device_mock.side_effect = [ConnectionError("no device"), device]
        with patch.object(config.conf, "adb", ""):
            solver = BaseSolver()
        self.assertIs(solver.device, device)
        self.restart_mock.assert_called_once_with()

    def test_droidcast_failure_prevents_startup_success(self):
        device = MagicMock()
        device.start_droidcast.return_value = False
        self.device_mock.side_effect = None
        self.device_mock.return_value = device
        with patch.object(config.conf.droidcast, "enable", True):
            with self.assertRaises(ConnectionError):
                BaseSolver()
        self.assertEqual(device.start_droidcast.call_count, 3)
        self.recog.assert_not_called()
        self.scrcpy.assert_not_called()
        self.assertEqual(self.restart_mock.call_count, 2)
        device._safe_reconnect.assert_not_called()

    def test_droidcast_failure_restarts_before_touch_and_recognition(self):
        device = MagicMock()
        device.start_droidcast.side_effect = [False, True]
        self.device_mock.side_effect = None
        self.device_mock.return_value = device
        actions = MagicMock()
        actions.attach_mock(device.start_droidcast, "droidcast")
        actions.attach_mock(self.restart_mock, "restart")
        actions.attach_mock(self.scrcpy, "scrcpy")
        actions.attach_mock(self.recog, "recog")
        with patch.object(config.conf.droidcast, "enable", True):
            solver = BaseSolver()
        self.assertIs(solver.device, device)
        self.assertEqual(
            actions.mock_calls,
            [
                call.droidcast(),
                call.restart(),
                call.droidcast(),
                call.scrcpy(device.client),
                call.recog(device),
            ],
        )

    def test_running_device_does_not_need_restart(self):
        self.device_mock.side_effect = None
        solver = BaseSolver()
        self.assertIs(solver.device, self.device_mock.return_value)
        self.device_mock.assert_called_once_with(wait_for_device=False)
        self.restart_mock.assert_not_called()

    def test_failed_restart_does_not_continue_device_initialization(self):
        self.restart_mock.return_value = False
        with self.assertRaisesRegex(ConnectionError, "首次任务重启模拟器失败"):
            BaseSolver()
        self.device_mock.assert_called_once_with(wait_for_device=False)
        self.restart_mock.assert_called_once_with()
        self.recog.assert_not_called()

    def test_persistent_failure_bounds_restarts_and_preserves_cause(self):
        failure = ConnectionError("offline")
        self.device_mock.side_effect = failure
        with self.assertRaises(ConnectionError) as raised:
            BaseSolver()
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(self.device_mock.call_count, 3)
        self.assertEqual(self.restart_mock.call_count, 2)
        self.recog.assert_not_called()

    def test_mower_exit_does_not_restart(self):
        self.device_mock.side_effect = MowerExit
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.restart_mock.assert_not_called()

    def test_stop_before_initialization_does_not_connect_or_restart(self):
        self.stop.set()
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.device_mock.assert_not_called()
        self.restart_mock.assert_not_called()

    def test_stop_during_connection_failure_does_not_restart(self):
        def fail_and_stop(**kwargs):
            self.stop.set()
            raise ConnectionError("offline")

        self.device_mock.side_effect = fail_and_stop
        with self.assertRaises(MowerExit):
            BaseSolver()
        self.restart_mock.assert_not_called()


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
