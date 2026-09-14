
import atexit
import contextlib
import logging
import logging.handlers
import os
import queue
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, Union

import AppKit
import pyautogui
import rumps
from pynput import keyboard, mouse


def _disable_pynput_keycode_translation() -> None:
    """
    pynput's macOS keyboard Listener calls Carbon's TISGetInputSourceProperty
    once, when its own listener thread starts, to build a keycode -> character
    translation table (pynput/_util/darwin.py: keycode_context()). Apple
    requires that specific call to happen on the main thread; pynput runs it
    on its own background thread instead, which hard-crashes the whole
    process (dispatch_assert_queue_fail / SIGTRAP, uncatchable) once this
    script runs as a launched .app rather than a bare terminal script.

    pynput's own Listener never actually reads the resulting table again
    (key characters come from CGEventKeyboardGetUnicodeString instead), and
    we don't use pynput's sending side (Controller), so it's safe to just
    replace it with a no-op.
    """
    import pynput.keyboard._darwin as _pynput_darwin_keyboard

    @contextlib.contextmanager
    def _noop_keycode_context():
        yield (None, None)

    _pynput_darwin_keyboard.keycode_context = _noop_keycode_context


# ============================================================
# Logging
# ============================================================

# Absolute path: when launched from a double-clicked .app bundle (rather
# than a terminal cd'd into this folder), the working directory is not
# this script's directory, so a plain relative "office.log" can resolve
# somewhere unwritable (e.g. "/").
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(message)s"
LOG_FILE = os.path.join(SCRIPT_DIR, "office.log")

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=1_000_000,
            backupCount=3,
        ),
    ],
)

logger = logging.getLogger(__name__)

try:
    _disable_pynput_keycode_translation()
except Exception:
    logger.exception(
        "Could not patch pynput's keycode translation; "
        "the keyboard listener may crash the process"
    )


# ============================================================
# Configuration
# ============================================================

@dataclass
class Config:
    # Mouse
    mouse_offset_min: int = 30
    mouse_offset_max: int = 250
    mouse_move_duration_min: float = 0.3
    mouse_move_duration_max: float = 1.0
    mouse_rest_min: float = 1.5
    mouse_rest_max: float = 5.0
    large_move_probability: float = 0.15

    # Application activation
    app_activation_retries: int = 3
    app_activation_delay: float = 1.5

    # Max seconds to wait for a single osascript call (activating an app,
    # or asking which one is frontmost) before giving up on it. Without
    # this, a stuck osascript/System Events call (e.g. behind an
    # unanswered permission prompt) would hang the automation thread
    # forever, since is_frontmost() is now checked before every action.
    osascript_timeout: float = 5.0

    # Scrolling
    scroll_min: int = 3
    scroll_max: int = 8

    # Action delays
    action_delay_min: float = 2.0
    action_delay_max: float = 6.0

    # Application switching
    app_switch_min: float = 5.0
    app_switch_max: float = 10.0

    # Number of actions per application
    actions_min: int = 3
    actions_max: int = 7

    # How long to pause mouse/automation workers after real user
    # input (mouse or keyboard) is detected, in seconds.
    user_activity_pause: float = 10.0

    # How long after WE send an input event (move/click/scroll/key)
    # to keep ignoring listener callbacks, so our own synthetic
    # events aren't mistaken for real user activity. Kept generous
    # for mouse moves since those play out over `duration` seconds.
    self_input_grace: float = 0.3

    # Max seconds a worker thread waits for the main thread to run a
    # pyautogui_call()-queued action, so a worker can never hang forever
    # if the main thread stops servicing the queue (e.g. mid-shutdown).
    action_queue_timeout: float = 15.0


CONFIG = Config()


# ============================================================
# Global state
# ============================================================

stop_event = threading.Event()
caffeinate_process: Optional[subprocess.Popen] = None

# Keep the cursor away from the screen corners so it never lands on a
# pyautogui FAILSAFE trigger point (0,0)/(w-1,0)/(0,h-1)/(w-1,h-1).
MOUSE_EDGE_MARGIN = 5

# mouse_worker and automation_worker call pyautogui from background
# threads, but macOS requires the keyboard-layout lookups pyautogui does
# for hotkey()/press() (Text Services Manager APIs) to run on the main
# thread -- calling them off it crashes the whole process once this runs
# as a bundled .app instead of a plain script. So actual pyautogui calls
# are queued here and only ever executed by main()'s loop, on the main
# thread; pyautogui_call() blocks the caller until that's done. This also
# serializes them, so a hotkey can never fire mid-mouse-move.
action_queue: "queue.Queue" = queue.Queue()

# The global keyboard/mouse listeners see every input event, including
# the synthetic ones pyautogui sends. This timestamp lets the listener
# callbacks tell "we just sent this ourselves" apart from real user
# input, both to avoid the script stopping itself on its own ESC press
# (see PhpStormController.focus_editor) and to detect genuine user
# activity (see mark_user_active/is_user_active below).
self_input_suppressed_until = 0.0

# If real user activity was detected recently, workers pause until
# this monotonic timestamp instead of moving the mouse / sending keys.
user_active_until = 0.0


# ============================================================
# Helpers
# ============================================================

def wait(seconds: float) -> bool:
    """
    Wait while allowing the program to stop immediately.

    Returns:
        True if the wait completed.
        False if stop_event was triggered.
    """
    return not stop_event.wait(seconds)


def random_delay(min_value: float, max_value: float) -> bool:
    return wait(random.uniform(min_value, max_value))


def mark_self_input(duration: float = CONFIG.self_input_grace) -> None:
    """Call right before sending a synthetic input event."""
    global self_input_suppressed_until
    self_input_suppressed_until = max(
        self_input_suppressed_until,
        time.monotonic() + duration,
    )


def is_self_input(now: Optional[float] = None) -> bool:
    """True if an input event arriving right now was very likely ours."""
    return (now if now is not None else time.monotonic()) < self_input_suppressed_until


def mark_user_active() -> None:
    global user_active_until

    if not is_user_active():
        logger.info(
            "Real user activity detected, pausing automation for %.0fs",
            CONFIG.user_activity_pause,
        )

    user_active_until = time.monotonic() + CONFIG.user_activity_pause


def is_user_active() -> bool:
    return time.monotonic() < user_active_until


def pyautogui_call(action: Callable[[], None], suppress_duration: float = CONFIG.self_input_grace) -> None:
    """
    Queue one pyautogui call for the main thread and block until it has
    run there (see `action_queue` above for why). Marks it as
    self-generated input right away, before it's actually executed, so
    the listeners don't react to it regardless of queue delay.
    """
    mark_self_input(suppress_duration)

    done = threading.Event()
    errors: List[Exception] = []

    action_queue.put((action, done, errors))

    # Bounded wait: if the main thread ever stops servicing the queue
    # (e.g. it already quit), this must not hang the caller forever.
    if not done.wait(timeout=CONFIG.action_queue_timeout):
        raise TimeoutError("Timed out waiting for the main thread to run a queued pyautogui call")

    if errors:
        raise errors[0]


# ============================================================
# Prevent macOS sleep
# ============================================================

def start_caffeinate() -> None:
    """
    Prevent macOS from turning off the display or sleeping
    while the script is running.
    """
    global caffeinate_process

    logger.info("Starting caffeinate")

    caffeinate_process = subprocess.Popen(
        [
            "caffeinate",
            "-dims",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_caffeinate() -> None:
    """
    Stop caffeinate when the application exits.
    """
    global caffeinate_process

    if caffeinate_process is None:
        return

    logger.info("Stopping caffeinate")

    caffeinate_process.terminate()

    try:
        caffeinate_process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        caffeinate_process.kill()

    caffeinate_process = None


# ============================================================
# Application Controller
# ============================================================

class ApplicationController:
    @staticmethod
    def activate(app_name: str) -> None:
        script = f'''
        tell application "{app_name}"
            activate
        end tell
        '''

        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CONFIG.osascript_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timed out activating %s", app_name)

    @staticmethod
    def get_frontmost_app() -> Optional[str]:
        script = '''
        tell application "System Events"
            name of first application process whose frontmost is true
        end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=CONFIG.osascript_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Timed out asking for the frontmost application")
            return None

        app_name = result.stdout.strip()

        return app_name if app_name else None

    @classmethod
    def is_frontmost(cls, app_name: str) -> bool:
        frontmost = cls.get_frontmost_app()
        return frontmost is not None and frontmost.lower() == app_name.lower()

    @classmethod
    def activate_and_verify(cls, app_name: str) -> bool:
        for attempt in range(1, CONFIG.app_activation_retries + 1):
            if stop_event.is_set():
                return False

            logger.info(
                "Activating %s (attempt %d/%d)",
                app_name,
                attempt,
                CONFIG.app_activation_retries,
            )

            cls.activate(app_name)

            if not wait(CONFIG.app_activation_delay):
                return False

            frontmost = cls.get_frontmost_app()

            logger.info("Frontmost application: %s", frontmost)

            if (
                frontmost is not None
                and frontmost.lower() == app_name.lower()
            ):
                logger.info("%s is active", app_name)
                return True

        logger.warning("Could not activate %s", app_name)

        return False


# ============================================================
# Mouse Controller
# ============================================================

class MouseController:
    def __init__(self) -> None:
        self.screen_width, self.screen_height = pyautogui.size()

    def move_nearby(self) -> None:
        current_x, current_y = pyautogui.position()

        offset_x = random.randint(
            CONFIG.mouse_offset_min,
            CONFIG.mouse_offset_max,
        )

        offset_y = random.randint(
            CONFIG.mouse_offset_min,
            CONFIG.mouse_offset_max,
        )

        if random.choice([True, False]):
            offset_x *= -1

        if random.choice([True, False]):
            offset_y *= -1

        target_x = max(
            MOUSE_EDGE_MARGIN,
            min(self.screen_width - 1 - MOUSE_EDGE_MARGIN, current_x + offset_x),
        )

        target_y = max(
            MOUSE_EDGE_MARGIN,
            min(self.screen_height - 1 - MOUSE_EDGE_MARGIN, current_y + offset_y),
        )

        duration = random.uniform(
            CONFIG.mouse_move_duration_min,
            CONFIG.mouse_move_duration_max,
        )

        pyautogui_call(
            lambda: pyautogui.moveTo(target_x, target_y, duration=duration),
            suppress_duration=duration + CONFIG.self_input_grace,
        )

    def move_large(self) -> None:
        target_x = random.randint(
            50,
            self.screen_width - 50,
        )

        target_y = random.randint(
            50,
            self.screen_height - 50,
        )

        duration = random.uniform(
            CONFIG.mouse_move_duration_min,
            CONFIG.mouse_move_duration_max,
        )

        pyautogui_call(
            lambda: pyautogui.moveTo(target_x, target_y, duration=duration),
            suppress_duration=duration + CONFIG.self_input_grace,
        )

    def move_randomly(self) -> None:
        if random.random() < CONFIG.large_move_probability:
            self.move_large()
        else:
            self.move_nearby()


# ============================================================
# Mouse Worker
# ============================================================

def mouse_worker() -> None:
    logger.info("Mouse worker started")

    mouse_controller = MouseController()

    while not stop_event.is_set():
        try:
            if is_user_active():
                if not wait(1.0):
                    break
                continue

            mouse_controller.move_randomly()

            rest = random.uniform(
                CONFIG.mouse_rest_min,
                CONFIG.mouse_rest_max,
            )

            if not wait(rest):
                break

        except Exception:
            logger.exception("Mouse worker error")

            if not wait(2):
                break

    logger.info("Mouse worker stopped")


# ============================================================
# Scrolling
# ============================================================

def scroll_randomly() -> None:
    amount = random.randint(
        CONFIG.scroll_min,
        CONFIG.scroll_max,
    )

    direction = random.choice([-1, 1])
    amount *= direction

    logger.info("Scrolling: %d", amount)

    pyautogui_call(lambda: pyautogui.scroll(amount))


# ============================================================
# PhpStorm Controller
# ============================================================

class PhpStormController:
    """
    Safe/read-only PhpStorm interactions.
    """

    @staticmethod
    def next_tab() -> None:
        logger.info("PhpStorm: next tab")
        pyautogui_call(lambda: pyautogui.hotkey("command", "shift", "]"))

    @staticmethod
    def previous_tab() -> None:
        logger.info("PhpStorm: previous tab")
        pyautogui_call(lambda: pyautogui.hotkey("command", "shift", "["))

    @staticmethod
    def recent_files() -> None:
        logger.info("PhpStorm: recent files")
        pyautogui_call(lambda: pyautogui.hotkey("command", "e"))

    @staticmethod
    def search_everywhere() -> None:
        logger.info("PhpStorm: Search Everywhere")
        pyautogui_call(
            lambda: pyautogui.press("shift", presses=2, interval=0.15),
            suppress_duration=0.15 * 2 + CONFIG.self_input_grace,
        )

    @staticmethod
    def toggle_terminal() -> None:
        logger.info("PhpStorm: toggle terminal")
        pyautogui_call(lambda: pyautogui.hotkey("alt", "f12"))

    @staticmethod
    def next_method() -> None:
        logger.info("PhpStorm: next method")
        pyautogui_call(lambda: pyautogui.hotkey("ctrl", "shift", "down"))

    @staticmethod
    def previous_method() -> None:
        logger.info("PhpStorm: previous method")
        pyautogui_call(lambda: pyautogui.hotkey("ctrl", "shift", "up"))

    @staticmethod
    def maximize_editor() -> None:
        logger.info("PhpStorm: toggle maximize editor")
        pyautogui_call(lambda: pyautogui.hotkey("ctrl", "shift", "f12"))

    @staticmethod
    def focus_editor() -> None:
        logger.info("PhpStorm: focus editor")
        pyautogui_call(lambda: pyautogui.press("esc"))

    @staticmethod
    def scroll() -> None:
        scroll_randomly()


# ============================================================
# Chrome Controller
# ============================================================

class ChromeController:
    """
    Safe/read-only Chrome interactions.
    """

    @staticmethod
    def scroll() -> None:
        scroll_randomly()

    @staticmethod
    def next_tab() -> None:
        logger.info("Chrome: next tab")
        pyautogui_call(lambda: pyautogui.hotkey("command", "option", "right"))

    @staticmethod
    def page_down() -> None:
        logger.info("Chrome: page down")
        pyautogui_call(lambda: pyautogui.press("pagedown"))

    @staticmethod
    def page_up() -> None:
        logger.info("Chrome: page up")
        pyautogui_call(lambda: pyautogui.press("pageup"))


# ============================================================
# App Profiles
#
# Each profile is an app name (for AppleScript activate/frontmost checks)
# plus a weighted pool of (label, no-arg callable) actions. To automate a
# new app: add a Controller class of static methods like the ones above,
# list its actions below, and add an AppProfile to APP_PROFILES -- nothing
# else needs to change.
# ============================================================

PHPSTORM_ACTIONS: Tuple[Tuple[str, Callable[[], None]], ...] = (
    ("next_tab", PhpStormController.next_tab),
    ("next_tab", PhpStormController.next_tab),
    ("previous_tab", PhpStormController.previous_tab),
    ("scroll", PhpStormController.scroll),
    ("scroll", PhpStormController.scroll),
    ("recent_files", PhpStormController.recent_files),
    ("search_everywhere", PhpStormController.search_everywhere),
    ("toggle_terminal", PhpStormController.toggle_terminal),
    ("next_method", PhpStormController.next_method),
    ("previous_method", PhpStormController.previous_method),
    ("focus_editor", PhpStormController.focus_editor),
)

CHROME_ACTIONS: Tuple[Tuple[str, Callable[[], None]], ...] = (
    ("scroll", ChromeController.scroll),
    ("scroll", ChromeController.scroll),
    ("next_tab", ChromeController.next_tab),
    ("page_down", ChromeController.page_down),
    ("page_up", ChromeController.page_up),
)


@dataclass
class AppProfile:
    name: str
    actions: Tuple[Tuple[str, Callable[[], None]], ...]
    # Short pause right after an action, before maybe running settle_action.
    post_action_delay: Tuple[float, float] = (0.0, 0.0)
    # Chance (0-1) to run settle_action once post_action_delay is over.
    settle_probability: float = 0.0
    settle_action: Optional[Callable[[], None]] = None


APP_PROFILES: Tuple[AppProfile, ...] = (
    AppProfile(
        name="PhpStorm",
        actions=PHPSTORM_ACTIONS,
        post_action_delay=(1.0, 2.5),
        settle_probability=0.35,
        settle_action=PhpStormController.focus_editor,
    ),
    AppProfile(
        name="Google Chrome",
        actions=CHROME_ACTIONS,
    ),
)


# ============================================================
# App Automation
# ============================================================

def execute_random_action(
    label: str,
    actions: Tuple[Tuple[str, Callable[[], None]], ...],
) -> None:
    action_name, action = random.choice(actions)
    logger.info("%s action: %s", label, action_name)
    action()


def automate_app(profile: AppProfile) -> None:
    if not ApplicationController.activate_and_verify(profile.name):
        return

    action_count = random.randint(
        CONFIG.actions_min,
        CONFIG.actions_max,
    )

    logger.info("%s: executing %d actions", profile.name, action_count)

    for index in range(action_count):
        if stop_event.is_set() or is_user_active():
            return

        if not ApplicationController.is_frontmost(profile.name):
            logger.info(
                "%s is no longer frontmost, stopping this cycle",
                profile.name,
            )
            return

        execute_random_action(profile.name, profile.actions)

        if profile.post_action_delay != (0.0, 0.0):
            if not random_delay(*profile.post_action_delay):
                return

        if (
            profile.settle_action is not None
            and random.random() < profile.settle_probability
            and ApplicationController.is_frontmost(profile.name)
        ):
            profile.settle_action()

        if index < action_count - 1:
            if not random_delay(
                CONFIG.action_delay_min,
                CONFIG.action_delay_max,
            ):
                return


# ============================================================
# Automation Worker
# ============================================================

def automation_worker() -> None:
    logger.info("Automation worker started")

    profiles = list(APP_PROFILES)

    while not stop_event.is_set():
        if is_user_active():
            if not wait(1.0):
                break
            continue

        random.shuffle(profiles)

        for profile in profiles:
            if stop_event.is_set():
                break

            if is_user_active():
                logger.info(
                    "User is active, skipping the rest of this cycle",
                )
                break

            try:
                automate_app(profile)
            except Exception:
                logger.exception("Automation worker error")

            if stop_event.is_set():
                break

            logger.info(
                "Waiting before switching application",
            )

            if not random_delay(
                CONFIG.app_switch_min,
                CONFIG.app_switch_max,
            ):
                break

    logger.info("Automation worker stopped")


# ============================================================
# Input Listeners
#
# These see every keyboard/mouse event on the system, including the
# synthetic ones this script sends via pyautogui. is_self_input()
# filters those out so they don't get treated as the user stopping
# the script (ESC) or being back at the keyboard (everything else).
# ============================================================

def on_press(key: Union[keyboard.Key, keyboard.KeyCode, None]) -> None:
    if is_self_input():
        return

    if key == keyboard.Key.esc:
        logger.info("ESC pressed. Stopping...")
        stop_event.set()
        return

    mark_user_active()


def on_move(x: int, y: int) -> None:
    if not is_self_input():
        mark_user_active()


def on_click(x: int, y: int, button: mouse.Button, pressed: bool) -> None:
    if pressed and not is_self_input():
        mark_user_active()


def on_scroll(x: int, y: int, dx: int, dy: int) -> None:
    if not is_self_input():
        mark_user_active()


# ============================================================
# Startup Dialog
# ============================================================

def prompt_duration() -> Optional[float]:
    """
    Ask the user how long to run, via a native NSAlert (rumps.alert()).

    A separate GUI toolkit (e.g. tkinter) must not be used here: Tk leaves
    CFRunLoop state behind even after root.destroy(), which then crashes
    the process with a fatal "Tcl_Panic" abort once the menu bar app's own
    run loop (rumps/AppKit) starts afterward in the same process.

    Returns:
        The chosen duration in seconds, or None for "no limit".
    """
    # Without this, the alert can render behind whatever launched us (e.g.
    # the Terminal window from Start Office Automation.command), making it
    # look like nothing happened.
    AppKit.NSApplication.sharedApplication()
    AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
        AppKit.NSApplicationActivateIgnoringOtherApps
    )

    # NSAlert's three buttons map to fixed return codes: ok -> 1,
    # cancel -> 0, other -> -1. There's no fourth way to dismiss it (no
    # close box on a modal alert), so every branch is covered.
    result = rumps.alert(
        title="Office Automation",
        message="На сколько запустить?",
        ok="1 час",
        cancel="2 часа",
        other="Без ограничений",
    )

    if result == 1:
        return 3600.0
    elif result == 0:
        return 7200.0
    else:
        return None


# ============================================================
# Menu Bar App
# ============================================================

class OfficeAutomationApp(rumps.App):
    """
    Minimal menu bar front-end. Shows a running/paused icon plus a status
    line, and its built-in Quit item (relabeled "Stop") is the click-to-stop
    control -- ESC keeps working too, from anywhere, via the global
    keyboard listener.
    """

    def __init__(self, run_duration: Optional[float]) -> None:
        super().__init__("▶️", quit_button="Stop")

        self.run_duration = run_duration
        self.start_time = time.monotonic()

        self.status_item = rumps.MenuItem("Status: running")
        self.menu = [self.status_item]

    @rumps.timer(0.1)
    def _tick(self, _sender: object) -> None:
        if stop_event.is_set():
            rumps.quit_application()
            return

        if (
            self.run_duration is not None
            and time.monotonic() - self.start_time >= self.run_duration
        ):
            logger.info("Run duration elapsed, stopping")
            stop_event.set()
            rumps.quit_application()
            return

        # Actually run any pyautogui call queued by pyautogui_call(), right
        # here on the main thread (see action_queue's comment above it).
        try:
            action, action_done, action_errors = action_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            try:
                action()
            except Exception as exc:
                action_errors.append(exc)
            finally:
                action_done.set()

        if is_user_active():
            self.title = "⏸"
            self.status_item.title = "Status: paused (user is active)"
        else:
            self.title = "▶️"
            self.status_item.title = "Status: running"


# ============================================================
# Main
# ============================================================

def main() -> None:
    run_duration = prompt_duration()

    logger.info("Starting automation")

    if run_duration is None:
        logger.info("Run duration: unlimited")
    else:
        logger.info("Run duration: %.0f minutes", run_duration / 60)

    logger.info("Press ESC, or Stop in the menu bar, to stop")

    pyautogui.FAILSAFE = True

    # Safety net: fires on normal interpreter exit even if something
    # below raises before the try/finally gets a chance to clean up.
    atexit.register(stop_caffeinate)

    mouse_thread = threading.Thread(
        target=mouse_worker,
        name="MouseWorker",
        daemon=True,
    )

    automation_thread = threading.Thread(
        target=automation_worker,
        name="AutomationWorker",
        daemon=True,
    )

    keyboard_listener = keyboard.Listener(
        on_press=on_press,
    )

    mouse_listener = mouse.Listener(
        on_move=on_move,
        on_click=on_click,
        on_scroll=on_scroll,
    )

    shutdown_done = threading.Event()

    def shutdown() -> None:
        if shutdown_done.is_set():
            return
        shutdown_done.set()

        stop_event.set()

        # Safety net: if shutdown below somehow hangs (e.g. a stuck
        # subprocess or a wedged thread), force the process to exit anyway
        # after a few seconds rather than leaving ESC/Stop looking like
        # they did nothing -- and leaving caffeinate running forever.
        def _force_exit() -> None:
            logger.warning("Shutdown taking too long, forcing exit")
            stop_caffeinate()
            os._exit(1)

        watchdog = threading.Timer(10.0, _force_exit)
        watchdog.daemon = True
        watchdog.start()

        try:
            keyboard_listener.stop()
        except Exception:
            logger.exception("Error stopping keyboard listener")

        try:
            mouse_listener.stop()
        except Exception:
            logger.exception("Error stopping mouse listener")

        for thread in (mouse_thread, automation_thread):
            if thread.is_alive():
                thread.join(timeout=2)

        stop_caffeinate()
        watchdog.cancel()

        logger.info("Program stopped")

    # rumps.quit_application() (called by our own timer, or by clicking the
    # built-in "Stop" item) tears down the Cocoa app in a way that can skip
    # right past a wrapping Python try/finally -- so cleanup is hooked here,
    # into rumps' own pre-quit event, rather than relied on to run after
    # app.run() returns. shutdown() is idempotent, so the try/finally below
    # is just a defensive fallback for any other, unexpected exit path.
    @rumps.events.before_quit
    def _on_before_quit() -> None:
        shutdown()

    try:
        start_caffeinate()

        mouse_thread.start()
        automation_thread.start()
        keyboard_listener.start()
        mouse_listener.start()

        OfficeAutomationApp(run_duration).run()

    except KeyboardInterrupt:
        logger.info("Ctrl+C pressed")

    finally:
        shutdown()


if __name__ == "__main__":
    main()

# ============================================================
# source .venv/bin/activate
# python3 office.py
# ============================================================