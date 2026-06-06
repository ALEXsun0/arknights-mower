import json
import socket
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from os import system
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from arknights_mower import __system__
from arknights_mower.utils import config
from arknights_mower.utils.csleep import MowerExit, csleep
from arknights_mower.utils.device.adb_client.session import Session
from arknights_mower.utils.log import logger


class Simulator_Type(Enum):
    Nox = "夜神"
    MuMu12 = "MuMu12"
    Leidian9 = "雷电9"
    Waydroid = "Waydroid"
    ReDroid = "ReDroid"
    MuMuPro = "MuMuPro"
    Genymotion = "Genymotion"


@dataclass
class SimulatorCommandSet:
    stop: str
    start: str
    blocking: bool = False


MUMUPRO_MAC_MANAGER_PORT_RANGE = range(20000, 20021)
MUMUPRO_MAC_BUNDLE_ID = "com.netease.mumu.nemux"
MUMUPRO_KEEP_RENDERING_INTERVAL = 120
MAC_DESKTOP_KEY_CODES = {
    1: 18,
    2: 19,
    3: 20,
    4: 21,
    5: 23,
    6: 22,
    7: 26,
    8: 28,
    9: 25,
}
_last_mumupro_render_keepalive = 0.0


def restart_simulator(stop: bool = True, start: bool = True) -> bool:
    return _restart_simulator(stop=stop, start=start, allow_retry=True)


def _restart_simulator(stop: bool, start: bool, allow_retry: bool) -> bool:
    data = config.conf.simulator
    simulator_type = data.name

    if simulator_type not in [item.value for item in Simulator_Type]:
        logger.warning(f"尚未支持{simulator_type}重启/自动启动")
        csleep(10)
        return False

    if should_use_mumupro_mac_compat(simulator_type, data.simulator_folder):
        return restart_mumupro_mac(data, stop, start, allow_retry)

    commands = build_command_set(simulator_type, data.index)

    if stop:
        logger.info(f"关闭{simulator_type}模拟器")
        run_command(commands.stop, data.simulator_folder, 0, commands.blocking)
        if (
            simulator_type == Simulator_Type.MuMu12.value
            and config.conf.fix_mumu12_adb_disconnect
        ):
            logger.info("结束adb进程")
            system("taskkill /f /t /im adb.exe")

    if not start:
        return True

    csleep(3)
    logger.info(f"启动{simulator_type}模拟器")
    started = run_command(
        commands.start,
        data.simulator_folder,
        data.wait_time,
        commands.blocking,
    )
    if not started and allow_retry:
        logger.warning(f"{simulator_type}重启后ADB未恢复，重试一次")
        return _restart_simulator(stop=True, start=True, allow_retry=False)
    if not started:
        return False

    press_hotkey(data.hotkey)
    return True


def press_hotkey(hotkey: str) -> None:
    hotkey = hotkey.strip()
    if hotkey:
        import pyautogui

        pyautogui.FAILSAFE = False
        pyautogui.hotkey(*hotkey.split("+"))


def build_command_set(simulator_type: str, index) -> SimulatorCommandSet:
    idx = normalize_index(index)

    if simulator_type == Simulator_Type.Nox.value:
        base = "Nox.exe"
        if idx >= 0:
            base += f" -clone:Nox_{idx}"
        return SimulatorCommandSet(stop=f"{base} -quit", start=base)

    if simulator_type == Simulator_Type.MuMu12.value:
        cmd = "MuMuManager.exe api -v "
        if idx >= 0:
            cmd += f"{idx} "
        return SimulatorCommandSet(
            stop=cmd + "shutdown_player",
            start=cmd + "launch_player",
        )

    if simulator_type == Simulator_Type.Waydroid.value:
        return SimulatorCommandSet(
            stop="waydroid session stop",
            start="waydroid show-full-ui",
        )

    if simulator_type == Simulator_Type.Leidian9.value:
        if idx < 0:
            idx = 0
        return SimulatorCommandSet(
            stop=f"ldconsole.exe quit --index {idx}",
            start=f"ldconsole.exe launch --index {idx}",
        )

    if simulator_type == Simulator_Type.ReDroid.value:
        return SimulatorCommandSet(
            stop=f"docker stop {index} -t 0",
            start=f"docker start {index}",
        )

    if simulator_type == Simulator_Type.MuMuPro.value:
        return SimulatorCommandSet(
            stop=f"Contents/MacOS/mumutool close {index}",
            start=f"Contents/MacOS/mumutool open {index}",
        )

    if __system__ == "windows":
        gmtool = "gmtool.exe"
    elif __system__ == "darwin":
        gmtool = "Contents/MacOS/gmtool"
    else:
        gmtool = "./gmtool"
    return SimulatorCommandSet(
        stop=f'{gmtool} admin stop "{index}"',
        start=f'{gmtool} admin start "{index}"',
        blocking=True,
    )


def should_use_mumupro_mac_compat(simulator_type: str, folder_path: str) -> bool:
    """MuMu Pro 1.4.x on macOS no longer ships Contents/MacOS/mumutool."""
    if simulator_type != Simulator_Type.MuMuPro.value or __system__ != "darwin":
        return False
    folder = Path(folder_path).expanduser() if folder_path else Path()
    return not folder.joinpath("Contents", "MacOS", "mumutool").is_file()


def restart_mumupro_mac(data, stop: bool, start: bool, allow_retry: bool) -> bool:
    idx = normalize_index(data.index)
    if idx < 0:
        logger.error("MuMuPro（Mac）需要填写非负的多开编号")
        return False

    if stop:
        logger.info("关闭MuMuPro模拟器")
        stop_mumupro_mac(idx)

    if not start:
        return True

    csleep(3)
    logger.info("启动MuMuPro模拟器")
    desktop = normalize_index(getattr(data, "mac_desktop", 0))
    switch_macos_desktop(desktop)
    ensure_mumupro_mac_manager(data.simulator_folder)
    switch_macos_desktop(desktop)
    start_mumupro_mac(idx, desktop)
    started = wait_for_target_adb(data.wait_time)
    if not started and allow_retry:
        logger.warning("MuMuPro重启后ADB未恢复，重试一次")
        return _restart_simulator(stop=True, start=True, allow_retry=False)
    if not started:
        return False

    press_hotkey(data.hotkey)
    keep_mumupro_rendering(force=True)
    return True


def stop_mumupro_mac(index: int) -> bool:
    ok = post_mumupro_mac_api("terminateWithoutCheckAgain", index)
    if ok and wait_for_mumupro_mac_process_exit(index, 10):
        return True
    logger.warning("MuMuPro本地API未能关闭实例，尝试按进程编号关闭")
    return kill_mumupro_mac_process(index)


def start_mumupro_mac(index: int, desktop: int = 0) -> bool:
    if find_mumupro_mac_pids(index):
        return True

    # 部分版本会返回成功但不真正拉起播放器；后面用进程检测校验。
    ok = post_mumupro_mac_api("restart", index)
    if not ok:
        ok = post_mumupro_mac_api("show_main", index)
    if ok and wait_for_mumupro_mac_process(index, 5):
        return True

    logger.info("通过MuMuPro管理器窗口启动模拟器")
    clicked = click_mumupro_mac_start_button(index)
    if clicked:
        switch_macos_desktop(desktop)
    return clicked and wait_for_mumupro_mac_process(index, 30)


def ensure_mumupro_mac_manager(folder_path: str) -> None:
    if find_mumupro_mac_manager_port() is not None:
        return

    app_path = (
        Path(folder_path).expanduser()
        if folder_path
        else Path("/Applications/MuMuPlayer.app")
    )
    if app_path.is_dir():
        subprocess.Popen(
            ["open", str(app_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["open", "-b", MUMUPRO_MAC_BUNDLE_ID],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    for _ in range(60):
        if find_mumupro_mac_manager_port() is not None:
            return
        csleep(1)
    logger.warning("未检测到MuMuPro管理端口")


def post_mumupro_mac_api(endpoint: str, index: int) -> bool:
    port = find_mumupro_mac_manager_port()
    if port is None:
        logger.debug("MuMuPro manager port not found")
        return False
    return post_mumupro_mac_api_to_port(endpoint, index, port)


def find_mumupro_mac_manager_port() -> int | None:
    for port in MUMUPRO_MAC_MANAGER_PORT_RANGE:
        if not is_port_open("127.0.0.1", port):
            continue
        if post_mumupro_mac_api_to_port("authentication", 0, port, log_failure=False):
            return port
    return None


def post_mumupro_mac_api_to_port(
    endpoint: str, index: int, port: int, log_failure: bool = True
) -> bool:
    url = f"http://127.0.0.1:{port}/api/player/{endpoint}"
    data = json.dumps({"index": index}).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as e:
        if log_failure:
            logger.debug(f"MuMuPro API {endpoint} failed on port {port}: {e}")
        return False
    if payload.get("code") == 0:
        return True
    if log_failure:
        logger.debug(f"MuMuPro API {endpoint} response on port {port}: {payload}")
    return False


def kill_mumupro_mac_process(index: int) -> bool:
    pids = find_mumupro_mac_pids(index)
    for pid in pids:
        try:
            subprocess.run(["kill", pid], check=False)
        except Exception as e:
            logger.debug(e)
    return len(pids) > 0


def wait_for_mumupro_mac_process_exit(index: int, wait_time: int) -> bool:
    for _ in range(wait_time):
        if not find_mumupro_mac_pids(index):
            return True
        csleep(1)
    return not find_mumupro_mac_pids(index)


def wait_for_mumupro_mac_process(index: int, wait_time: int) -> bool:
    for _ in range(wait_time):
        if find_mumupro_mac_pids(index):
            return True
        csleep(1)
    return bool(find_mumupro_mac_pids(index))


def click_mumupro_mac_start_button(index: int) -> bool:
    row_number = index + 1
    background_script = f"""
tell application "System Events"
  tell process "MuMuPlayer"
    tell window 1
      tell scroll area 1
        tell table 1
          tell row {row_number}
            tell UI element 1
              tell button 2
                perform action "AXPress"
              end tell
            end tell
          end tell
        end tell
      end tell
    end tell
  end tell
end tell
"""
    if run_osascript(background_script, timeout=5):
        return True

    script = f"""
tell application "System Events"
  tell process "MuMuPlayer"
    set frontmost to true
    delay 0.3
    tell window 1
      tell scroll area 1
        tell table 1
          tell row {row_number}
            tell UI element 1
              click button 2
            end tell
          end tell
        end tell
      end tell
    end tell
  end tell
end tell
"""
    return run_osascript(script, timeout=10)


def switch_macos_desktop(desktop) -> bool:
    desktop = normalize_index(desktop)
    if __system__ != "darwin" or desktop <= 0:
        return False
    key_code = MAC_DESKTOP_KEY_CODES.get(desktop)
    if key_code is None:
        logger.warning("macOS桌面编号仅支持1-9")
        return False
    logger.info(f"切换到macOS桌面{desktop}")
    script = f"""
tell application "System Events"
  key code {key_code} using {{control down}}
end tell
"""
    ok = run_osascript(script, timeout=5)
    if ok:
        csleep(0.5)
    return ok


def keep_mumupro_rendering(force: bool = False) -> None:
    global _last_mumupro_render_keepalive
    if __system__ != "darwin":
        return
    data = config.conf.simulator
    if (
        data.name != Simulator_Type.MuMuPro.value
        or not getattr(data, "mac_keep_rendering", False)
    ):
        return

    now = time.monotonic()
    if not force and now - _last_mumupro_render_keepalive < MUMUPRO_KEEP_RENDERING_INTERVAL:
        return
    _last_mumupro_render_keepalive = now

    idx = normalize_index(data.index)
    if idx < 0:
        return
    if ensure_mumupro_mac_window_visible(idx):
        logger.debug("已保持MuMuPro窗口可见")


def ensure_mumupro_mac_window_visible(index: int) -> bool:
    for pid in find_mumupro_mac_pids(index):
        script = f"""
tell application "System Events"
  set targetProcess to first process whose unix id is {pid}
  tell targetProcess
    try
      repeat with targetWindow in windows
        set value of attribute "AXMinimized" of targetWindow to false
      end repeat
    end try
    set frontmost to true
  end tell
end tell
"""
        if run_osascript(script, timeout=5):
            return True
    return False


def run_osascript(script: str, timeout: int) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as e:
        logger.debug(e)
        return False
    if result.returncode == 0:
        return True
    logger.debug(result.stderr.strip() or result.stdout.strip())
    return False


def find_mumupro_mac_pids(index: int) -> list[str]:
    try:
        output = subprocess.check_output(
            ["ps", "axww", "-o", "pid=,command="],
            text=True,
        )
    except Exception as e:
        logger.debug(e)
        return []

    pids = []
    needle = f"MuMuEmulator --index {index} "
    for line in output.splitlines():
        if needle not in line:
            continue
        pid = line.strip().split(maxsplit=1)[0]
        pids.append(pid)
    return pids


def is_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def normalize_index(index) -> int:
    try:
        return int(index)
    except (TypeError, ValueError):
        return -1


def run_command(cmd: str, folder_path: str, wait_time: int, blocking: bool) -> bool:
    logger.debug(cmd)
    process = subprocess.Popen(
        cmd,
        shell=True,
        cwd=folder_path or None,
        creationflags=subprocess.CREATE_NO_WINDOW if __system__ == "windows" else 0,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    if blocking:
        return wait_for_process(process, wait_time)
    if wait_time <= 0:
        return True
    return wait_for_adb(process, wait_time)


def wait_for_process(process: subprocess.Popen, wait_time: int) -> bool:
    while wait_time > 0:
        try:
            csleep(0)
            logger.debug(process.communicate(timeout=1))
            return process.returncode == 0
        except MowerExit:
            raise
        except subprocess.TimeoutExpired:
            wait_time -= 1
    return False


def wait_for_adb(process: subprocess.Popen, wait_time: int) -> bool:
    for _ in range(wait_time):
        try:
            if wait_for_target_adb(0):
                return True
        except MowerExit:
            raise
        except Exception as e:
            logger.debug(e)
        if process.poll() is not None and process.returncode not in (0, None):
            logger.debug(process.communicate())
        csleep(1)
    return adb_ready()


def wait_for_target_adb(wait_time: int) -> bool:
    for _ in range(wait_time):
        if adb_ready():
            return True
        csleep(1)
    return adb_ready()


def adb_ready() -> bool:
    target = config.conf.adb
    if not target:
        return len(Session().devices_list()) > 0
    Session().connect(target, throw_error=True)
    devices = [
        device for device, status in Session().devices_list() if status != "offline"
    ]
    return target in devices
