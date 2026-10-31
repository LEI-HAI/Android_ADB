"""核心逻辑的离线测试（不需要手机）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import time
import unittest

from android_adb.adb_client import AdbClient
from android_adb.backends.adb_backend import AdbMediaBackend
from android_adb.backends.base import BackendError, BackendUnavailable, MediaBackend
from android_adb.backends.scrcpy_backend import ScrcpyMediaBackend
from android_adb.config import Config
from android_adb.controller import Action, MediaController
from android_adb.hotkeys import ExitHotkey, canonical, parse_combo, resolve_key
from android_adb.media_state import (
    PlaybackState,
    media_button_session,
    parse_media_session,
    pick_state,
)
from android_adb.play_pause import KeyeventPlayPauser, SmartPlayPauser, make_play_pauser
from android_adb.mouse_listener import Debouncer, HookSupervisor, KeyboardHook
from android_adb.rewind import KeyeventRewinder, SwipeRewinder, make_rewinder


class FakeClient:
    def __init__(self) -> None:
        self.sent = []

    def keyevent(self, keycode) -> None:
        self.sent.append(int(keycode))


class FakeBackend(MediaBackend):
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.calls = []
        self.fail = fail

    def play_pause(self) -> None:
        if self.fail:
            raise BackendError("模拟失败")
        self.calls.append(Action.PLAY_PAUSE)

    def rewind(self) -> None:
        self.calls.append(Action.REWIND)


class ParseDevicesTest(unittest.TestCase):
    def test_parse_normal_device(self) -> None:
        output = (
            "List of devices attached\n"
            "5889912f\tdevice product:PD1936G model:V1936AL device:PD1936G transport_id:2\n"
            "\n"
        )
        devices = AdbClient._parse_devices(output)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].serial, "5889912f")
        self.assertEqual(devices[0].state, "device")
        self.assertEqual(devices[0].model, "V1936AL")
        self.assertEqual(devices[0].transport_id, "2")

    def test_parse_unauthorized_and_empty(self) -> None:
        devices = AdbClient._parse_devices("List of devices attached\nabc123\tunauthorized\n")
        self.assertEqual(devices[0].state, "unauthorized")
        self.assertEqual(AdbClient._parse_devices("List of devices attached\n\n"), [])


class ConfigTest(unittest.TestCase):
    def test_empty_strings_become_none(self) -> None:
        cfg = Config()
        cfg.merge({"adb_path": "", "device_serial": "", "rewind_repeat": "3"})
        cfg.validate()
        self.assertIsNone(cfg.adb_path)
        self.assertIsNone(cfg.device_serial)
        self.assertEqual(cfg.rewind_repeat, 3)

    def test_rejects_same_button(self) -> None:
        cfg = Config(rewind_button="x1", play_pause_button="x1")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_rewind_mode_validation(self) -> None:
        cfg = Config(rewind_mode="magic")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_bad_swipe_ratio(self) -> None:
        cfg = Config(swipe_ms_per_px=0)
        with self.assertRaises(ValueError):
            cfg.validate()
        cfg = Config(swipe_seconds=0.05)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_swipe_defaults(self) -> None:
        cfg = Config()
        cfg.validate()
        self.assertEqual(cfg.rewind_mode, "swipe")
        self.assertEqual(cfg.swipe_seconds, 5.0)

    def test_play_pause_mode_validation(self) -> None:
        cfg = Config()
        cfg.validate()
        self.assertEqual(cfg.play_pause_mode, "auto")
        with self.assertRaises(ValueError):
            Config(play_pause_mode="magic").validate()

    def test_play_pause_ratio_is_clamped(self) -> None:
        cfg = Config(play_pause_x_ratio=-3, play_pause_y_ratio=9, play_pause_verify_ms="250")
        cfg.validate()
        self.assertEqual(cfg.play_pause_x_ratio, 0.01)
        self.assertEqual(cfg.play_pause_y_ratio, 0.99)
        self.assertEqual(cfg.play_pause_verify_ms, 250)


class DebouncerTest(unittest.TestCase):
    def test_window_blocks_repeat(self) -> None:
        debouncer = Debouncer(100)
        self.assertTrue(debouncer.allow("x1"))
        self.assertFalse(debouncer.allow("x1"))
        self.assertTrue(debouncer.allow("x2"))
        time.sleep(0.12)
        self.assertTrue(debouncer.allow("x1"))

    def test_zero_window_always_allows(self) -> None:
        debouncer = Debouncer(0)
        self.assertTrue(debouncer.allow("x1"))
        self.assertTrue(debouncer.allow("x1"))


class ControllerTest(unittest.TestCase):
    def test_dispatch(self) -> None:
        backend = FakeBackend()
        controller = MediaController(backend)
        self.assertTrue(controller.handle(Action.PLAY_PAUSE))
        self.assertTrue(controller.handle(Action.REWIND))
        self.assertEqual(backend.calls, [Action.PLAY_PAUSE, Action.REWIND])

    def test_backend_error_is_contained(self) -> None:
        controller = MediaController(FakeBackend(fail=True))
        self.assertFalse(controller.handle(Action.PLAY_PAUSE))


class AdbBackendTest(unittest.TestCase):
    def _config(self, **kwargs) -> Config:
        kwargs.setdefault("rewind_mode", "media_key")
        cfg = Config()
        cfg.merge(kwargs)
        cfg.validate()
        return cfg

    def test_play_pause_sends_85(self) -> None:
        client = FakeClient()
        backend = AdbMediaBackend(client, self._config())
        backend.play_pause()
        self.assertEqual(client.sent, [85])

    def test_rewind_repeats(self) -> None:
        client = FakeClient()
        backend = AdbMediaBackend(client, self._config(rewind_repeat=3, rewind_interval_ms=0))
        backend.rewind()
        self.assertEqual(client.sent, [89, 89, 89])

    def test_rewind_uses_fallback_on_error(self) -> None:
        class BrokenClient(FakeClient):
            def keyevent(self, keycode) -> None:
                if int(keycode) == 89:
                    raise __import__("android_adb.adb_client", fromlist=["AdbError"]).AdbError("boom")
                super().keyevent(keycode)

        client = BrokenClient()
        backend = AdbMediaBackend(client, self._config(rewind_fallback_keycode=21))
        backend.rewind()
        self.assertEqual(client.sent, [21])


SAMPLE_DUMP = """Media session service dumpsys
Sessions Stack - have 2 sessions:
  * Stack #0:
    package=com.android.server.telecom
    active=false
    metadata: null
  * Stack #1:
    package=tv.danmaku.bili
    launchIntent=null
    active=true
    flags=3
    state=PlaybackState {state=3, position=102310, buffered position=0, speed=1.0, updated=410264938, actions=823, custom actions=[], active item id=-1, error=null}
  * Stack #2:
    package=com.netease.cloudmusic
    active=false
    state=PlaybackState {state=2, position=5000, buffered position=0, speed=0.0, updated=410000000, actions=0, custom actions=[], active item id=-1, error=null}
"""


class MediaStateTest(unittest.TestCase):
    def test_parse_and_pick_active(self) -> None:
        states = parse_media_session(SAMPLE_DUMP)
        self.assertEqual(len(states), 2)
        self.assertEqual(states[0].package, "tv.danmaku.bili")
        self.assertTrue(states[0].active)
        self.assertEqual(states[0].position_ms, 102310)
        self.assertEqual(states[0].speed, 1.0)
        picked = pick_state(states)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.package, "tv.danmaku.bili")

    def test_pick_prefers_package(self) -> None:
        states = parse_media_session(SAMPLE_DUMP)
        picked = pick_state(states, "com.netease.cloudmusic")
        self.assertEqual(picked.package, "com.netease.cloudmusic")

    def test_empty_dump(self) -> None:
        self.assertEqual(parse_media_session(""), [])
        self.assertIsNone(pick_state([]))

    def test_advance_between_cancels_playback(self) -> None:
        before = PlaybackState(package="a", state=3, position_ms=100000, speed=1.0, updated_ms=1000)
        # 后退 5 秒，期间正常播放了 0.8 秒
        after = PlaybackState(package="a", state=3, position_ms=95700, speed=1.0, updated_ms=1800)
        self.assertEqual(-before.advance_between(after), 5100)

    def test_advance_between_paused(self) -> None:
        before = PlaybackState(package="a", state=2, position_ms=100000, speed=0.0, updated_ms=1000)
        after = PlaybackState(package="a", state=2, position_ms=94900, speed=0.0, updated_ms=1800)
        self.assertEqual(-before.advance_between(after), 5100)


class FakeSwipeClient:
    """只实现 SwipeRewinder 用到的四个方法，模拟"滑动 1px 后退 N 毫秒"。"""

    def __init__(self, position_ms: int = 100000, ms_per_px: float = 31.0) -> None:
        self.position_ms = position_ms
        self.ms_per_px = ms_per_px
        self.swipes = []
        self.updated = 1_000_000

    def screen_size(self):
        return (1080, 2340)

    def rotation(self):
        return 1

    def swipe(self, x1, y1, x2, y2, duration_ms=200):
        self.swipes.append((x1, y1, x2, y2))
        self.position_ms -= int((x1 - x2) * self.ms_per_px)
        self.updated += int(duration_ms) + 50

    def media_session_dump(self):
        return (
            "Sessions Stack - have 1 sessions:\n"
            "  * Stack #0:\n"
            "    package=tv.danmaku.bili\n"
            "    active=true\n"
            "    state=PlaybackState {state=2, position=%d, buffered position=0, "
            "speed=0.0, updated=%d, actions=823}\n" % (self.position_ms, self.updated)
        )


class SwipeRewindTest(unittest.TestCase):
    def _config(self, **kwargs) -> Config:
        kwargs.setdefault("rewind_mode", "swipe")
        kwargs.setdefault("swipe_seconds", 5.0)
        kwargs.setdefault("swipe_ms_per_px", 31.0)
        kwargs.setdefault("swipe_settle_ms", 0)
        cfg = Config()
        cfg.merge(kwargs)
        cfg.validate()
        return cfg

    def test_swipe_lands_near_target(self) -> None:
        client = FakeSwipeClient(ms_per_px=31.0)
        SwipeRewinder(client, self._config()).rewind()
        self.assertEqual(len(client.swipes), 1)
        x1, _y1, x2, _y2 = client.swipes[0]
        self.assertLess(x2, x1)  # 向左滑 = 后退
        moved = 100000 - client.position_ms
        self.assertAlmostEqual(moved / 1000.0, 5.0, delta=0.3)

    def test_auto_calibration_corrects_wrong_ratio(self) -> None:
        client = FakeSwipeClient(ms_per_px=62.0)  # 实际比预估慢一倍
        rewinder = SwipeRewinder(client, self._config(swipe_ms_per_px=31.0))
        rewinder.rewind()
        self.assertEqual(len(client.swipes), 2)
        _x1, _y1, x2, _y2 = client.swipes[1]
        self.assertGreater(x2, client.swipes[0][2])  # 第二刀向右，补回多退的部分
        self.assertGreater(rewinder._ms_per_px, 31.0)

    def test_no_media_session_still_swipes(self) -> None:
        class BlindClient(FakeSwipeClient):
            def media_session_dump(self):
                return "Sessions Stack - have 0 sessions:"

        client = BlindClient()
        SwipeRewinder(client, self._config()).rewind()
        self.assertEqual(len(client.swipes), 1)

    def test_factory_chooses_mode(self) -> None:
        client = FakeSwipeClient()
        self.assertIsInstance(make_rewinder(client, self._config()), SwipeRewinder)
        self.assertIsInstance(
            make_rewinder(client, self._config(rewind_mode="media_key")), KeyeventRewinder
        )

    def test_rotation_swaps_dimensions(self) -> None:
        client = FakeSwipeClient()
        rewinder = SwipeRewinder(client, self._config())
        self.assertEqual(rewinder._display(), (2340, 1080))


class FakePlayPauseClient:
    """模拟播放状态：媒体键/点击是否生效、媒体按钮会话是否存在。"""

    def __init__(
        self,
        state: int = 3,
        session: str = "tv.danmaku.bili/HeadsetControlPlayAdapter.Session (userId=0)",
        speed: float = 1.0,
        key_works: bool = True,
        tap_works: bool = True,
    ) -> None:
        self.state = state
        self.session = session
        self.speed = speed
        self.key_works = key_works
        self.tap_works = tap_works
        self.sent = []
        self.taps = []

    def keyevent(self, keycode) -> None:
        self.sent.append(int(keycode))
        if self.key_works:
            self._toggle()

    def tap(self, x, y) -> None:
        self.taps.append((x, y))
        if self.tap_works:
            self._toggle()

    def screen_size(self):
        return (1080, 2340)

    def rotation(self):
        return 1

    def media_session_dump(self) -> str:
        session = self.session if self.session is not None else "null"
        return (
            "Sessions Stack - have 1 sessions:\n"
            "  * Stack #0:\n"
            "    package=tv.danmaku.bili\n"
            "    active=true\n"
            "    state=PlaybackState {state=%d, position=5000, buffered position=0, "
            "speed=%.1f, updated=1000, actions=823}\n"
            "\n"
            "  Media button session is %s\n" % (self.state, self.speed, session)
        )

    def _toggle(self) -> None:
        self.state = 3 if self.state == 2 else 2


class PlayPauseTest(unittest.TestCase):
    """播放/暂停：媒体键可用时只按键，失效时点画面按钮。"""

    def _config(self, **kwargs) -> Config:
        kwargs.setdefault("play_pause_verify_ms", 0)
        kwargs.setdefault("play_pause_tap_settle_ms", 0)
        cfg = Config()
        cfg.merge(kwargs)
        cfg.validate()
        return cfg

    def test_playing_uses_media_key_only(self) -> None:
        client = FakePlayPauseClient(state=3)
        make_play_pauser(client, self._config()).play_pause()
        self.assertEqual(client.sent, [85])
        self.assertEqual(client.taps, [])

    def test_paused_without_media_button_session_taps(self) -> None:
        client = FakePlayPauseClient(state=2, session=None)
        make_play_pauser(client, self._config()).play_pause()
        self.assertEqual(client.sent, [])        # 媒体键没人接收，不再空发
        self.assertEqual(len(client.taps), 1)
        self.assertEqual(client.state, 3)
        self.assertEqual(client.taps[0], (91, 976))   # 2340*0.039, 1080*0.904

    def test_paused_with_session_falls_back_to_tap(self) -> None:
        client = FakePlayPauseClient(state=2, key_works=False)
        make_play_pauser(client, self._config()).play_pause()
        self.assertEqual(client.sent, [85])      # 先试媒体键
        self.assertEqual(len(client.taps), 1)    # 没生效才点按钮
        self.assertEqual(client.state, 3)

    def test_taps_twice_when_controls_hidden(self) -> None:
        class HiddenControlsClient(FakePlayPauseClient):
            def tap(self, x, y) -> None:
                self.taps.append((x, y))
                if len(self.taps) >= 2:          # 第一次只是把控件栏唤出来
                    self._toggle()

        client = HiddenControlsClient(state=2, session=None)
        make_play_pauser(client, self._config()).play_pause()
        self.assertEqual(len(client.taps), 2)
        self.assertEqual(client.state, 3)

    def test_tap_failure_raises_backend_error(self) -> None:
        client = FakePlayPauseClient(state=2, session=None, tap_works=False)
        with self.assertRaises(BackendError):
            make_play_pauser(client, self._config()).play_pause()

    def test_unknown_state_sends_key_only(self) -> None:
        class BlindClient(FakePlayPauseClient):
            def media_session_dump(self) -> str:
                return "Sessions Stack - have 0 sessions:"

        client = BlindClient()
        make_play_pauser(client, self._config()).play_pause()
        self.assertEqual(client.sent, [85])
        self.assertEqual(client.taps, [])

    def test_media_key_mode_never_taps(self) -> None:
        client = FakePlayPauseClient(state=2, session=None, key_works=False)
        make_play_pauser(client, self._config(play_pause_mode="media_key")).play_pause()
        self.assertEqual(client.sent, [85])
        self.assertEqual(client.taps, [])

    def test_tap_mode_skips_media_key(self) -> None:
        client = FakePlayPauseClient(state=3)
        make_play_pauser(client, self._config(play_pause_mode="tap")).play_pause()
        self.assertEqual(client.sent, [])
        self.assertEqual(len(client.taps), 1)

    def test_factory_returns_expected_types(self) -> None:
        client = FakePlayPauseClient()
        self.assertIsInstance(make_play_pauser(client, self._config()), SmartPlayPauser)
        self.assertIsInstance(
            make_play_pauser(client, self._config(play_pause_mode="media_key")),
            KeyeventPlayPauser,
        )

    def test_media_button_session_parse(self) -> None:
        self.assertIsNone(media_button_session("  Media button session is null\n"))
        self.assertIsNone(media_button_session(""))
        self.assertEqual(
            media_button_session(
                "  Media button session is tv.danmaku.bili/x (userId=0)\n"
            ),
            "tv.danmaku.bili/x (userId=0)",
        )


class ScrcpyBackendTest(unittest.TestCase):
    def test_is_reserved(self) -> None:
        with self.assertRaises(BackendUnavailable):
            ScrcpyMediaBackend(Config())


class HotkeyTest(unittest.TestCase):
    def test_resolve(self) -> None:
        from pynput import keyboard

        self.assertIs(resolve_key("esc"), keyboard.Key.esc)
        with self.assertRaises(ValueError):
            resolve_key("not-a-key")

    def test_parse_combo(self) -> None:
        self.assertEqual(parse_combo("Ctrl+Alt+Left"), frozenset({"ctrl", "alt", "left"}))
        self.assertEqual(parse_combo("f9"), frozenset({"f9"}))
        with self.assertRaises(ValueError):
            parse_combo("")
        with self.assertRaises(ValueError):
            parse_combo("ctrl+alt")     # 只有修饰键

    def test_canonical(self) -> None:
        from pynput import keyboard

        self.assertEqual(canonical(keyboard.Key.ctrl_l), "ctrl")
        self.assertEqual(canonical(keyboard.Key.ctrl_r), "ctrl")
        self.assertEqual(canonical(keyboard.Key.alt_l), "alt")
        self.assertEqual(canonical(keyboard.KeyCode.from_char("A")), "a")
        self.assertEqual(canonical(keyboard.Key.left), "left")


class KeyboardHookTest(unittest.TestCase):
    """键盘备用绑定：组合键判定与防抖（不真的挂系统钩子）。"""

    class FakeDispatcher:
        def __init__(self) -> None:
            self.actions = []

        def submit(self, action) -> bool:
            self.actions.append(action)
            return True

    def _hook(self, rewind_key="ctrl+alt+left", play_pause_key=None):
        cfg = Config(rewind_key=rewind_key, play_pause_key=play_pause_key, debounce_ms=0)
        cfg.validate()
        dispatcher = self.FakeDispatcher()
        return KeyboardHook(cfg, dispatcher), dispatcher

    def test_combo_triggers_action(self) -> None:
        from pynput import keyboard

        hook, dispatcher = self._hook()
        self.assertTrue(hook.enabled)
        hook._on_press(keyboard.Key.ctrl_l)
        self.assertEqual(dispatcher.actions, [])
        hook._on_press(keyboard.Key.alt_l)
        self.assertEqual(dispatcher.actions, [])
        hook._on_press(keyboard.Key.left)
        self.assertEqual(dispatcher.actions, [Action.REWIND])
        # 松开 alt 后再按 left 不应再触发
        hook._on_release(keyboard.Key.alt_l)
        hook._on_release(keyboard.Key.left)
        hook._on_press(keyboard.Key.left)
        self.assertEqual(dispatcher.actions, [Action.REWIND])

    def test_disabled_without_binding(self) -> None:
        hook, dispatcher = self._hook(rewind_key=None)
        self.assertFalse(hook.enabled)
        hook._on_press(__import__("pynput").keyboard.Key.left)
        self.assertEqual(dispatcher.actions, [])

    def test_config_rejects_bad_key(self) -> None:
        with self.assertRaises(Exception):
            Config(rewind_key="ctrl+nonsense", debounce_ms=0).validate()

    def test_config_rejects_exit_conflict(self) -> None:
        with self.assertRaises(Exception):
            Config(rewind_key="ctrl+alt+left", exit_key="ctrl+alt+left").validate()

    def test_config_rejects_bad_exit_key(self) -> None:
        with self.assertRaises(Exception):
            Config(exit_key="ctrl+alt").validate()       # 只有修饰键
        with self.assertRaises(Exception):
            Config(exit_key="ctrl+nonsense").validate()

    def test_default_exit_key_is_combo(self) -> None:
        cfg = Config()
        cfg.validate()
        self.assertEqual(cfg.exit_key, "ctrl+alt+q")
        self.assertIn("+", cfg.exit_key)


class ExitHotkeyTest(unittest.TestCase):
    """退出键：组合键才触发，单独的 Esc 不能把程序关掉。"""

    def _hook(self, spec="ctrl+alt+q"):
        fired = []
        return ExitHotkey(spec, on_trigger=lambda: fired.append(True)), fired

    def test_combo_triggers_once(self) -> None:
        from pynput import keyboard

        hook, fired = self._hook()
        hook._on_press(keyboard.Key.ctrl_l)
        hook._on_press(keyboard.Key.alt_l)
        self.assertEqual(fired, [])
        hook._on_press(keyboard.KeyCode.from_char("q"))
        self.assertEqual(fired, [True])
        self.assertTrue(hook._triggered.is_set())
        self.assertTrue(hook.has_modifier)
        self.assertFalse(hook._on_press(keyboard.KeyCode.from_char("q")))  # 返回 False 停止监听

    def test_esc_does_not_trigger_combo(self) -> None:
        from pynput import keyboard

        hook, fired = self._hook()
        hook._on_press(keyboard.Key.esc)
        hook._on_press(keyboard.Key.esc)
        self.assertEqual(fired, [])

    def test_requires_all_keys(self) -> None:
        from pynput import keyboard

        hook, fired = self._hook()
        hook._on_press(keyboard.Key.ctrl_l)
        hook._on_press(keyboard.KeyCode.from_char("q"))   # 缺 alt
        self.assertEqual(fired, [])

    def test_rearm_after_release(self) -> None:
        from pynput import keyboard

        hook, fired = self._hook("f8")
        hook._on_press(keyboard.Key.f8)
        self.assertEqual(fired, [True])
        self.assertFalse(hook.has_modifier)
        hook._on_release(keyboard.Key.f8)
        hook._armed = True   # 模拟监听仍在跑
        hook._on_press(keyboard.Key.f8)
        self.assertEqual(fired, [True, True])


class FakeWatchHook:
    """假的监听钩子：用来验证看门狗的重启逻辑，不需要真的起 pynput。"""

    def __init__(self, alive: bool = False, recover: bool = True) -> None:
        self.alive = alive
        self.recover = recover
        self.restarts = 0

    @property
    def is_alive(self) -> bool:
        return self.alive

    def restart(self) -> None:
        self.restarts += 1
        if self.recover:
            self.alive = True


class HookSupervisorTest(unittest.TestCase):
    def _supervisor(self, hook, **kwargs):
        gave_up = []
        sup = HookSupervisor([hook], interval=0.05, on_give_up=lambda: gave_up.append(True), **kwargs)
        return sup, gave_up

    def test_restarts_dead_hook(self) -> None:
        hook = FakeWatchHook(alive=False, recover=True)
        sup, gave_up = self._supervisor(hook)
        sup._check(hook)
        self.assertEqual(hook.restarts, 1)
        self.assertTrue(hook.is_alive)
        self.assertEqual(gave_up, [])
        # 恢复后再检查一次，失败计数应清零
        sup._check(hook)
        self.assertEqual(hook.restarts, 1)
        self.assertEqual(sup._failures[id(hook)], 0)

    def test_gives_up_after_limit(self) -> None:
        hook = FakeWatchHook(alive=False, recover=False)
        sup, gave_up = self._supervisor(hook, limit=2)
        for _ in range(3):
            sup._check(hook)
        self.assertEqual(hook.restarts, 2)          # 只重启了 2 次
        self.assertEqual(gave_up, [True])           # 第 3 次放弃
        self.assertFalse(hook.is_alive)

    def test_restart_disabled_gives_up_immediately(self) -> None:
        hook = FakeWatchHook(alive=False, recover=True)
        sup, gave_up = self._supervisor(hook, restart=False)
        sup._check(hook)
        self.assertEqual(hook.restarts, 0)
        self.assertEqual(gave_up, [True])

    def test_alive_hook_is_left_alone(self) -> None:
        hook = FakeWatchHook(alive=True)
        sup, gave_up = self._supervisor(hook)
        sup._check(hook)
        self.assertEqual(hook.restarts, 0)
        self.assertEqual(gave_up, [])

    def test_stop_ends_loop(self) -> None:
        hook = FakeWatchHook(alive=True)
        sup, _ = self._supervisor(hook)
        sup.start()
        sup.stop()
        sup.join(timeout=2)
        self.assertFalse(sup.is_alive())


class LogConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = Config()
        cfg.validate()
        self.assertEqual(cfg.log_file, "logs/controller.log")
        self.assertEqual(cfg.log_file_level, "DEBUG")
        self.assertTrue(cfg.hook_restart)
        self.assertEqual(cfg.hook_restart_limit, 30)
        self.assertEqual(cfg.heartbeat_seconds, 300)

    def test_empty_log_file_disables_file_logging(self) -> None:
        cfg = Config(log_file="  ")
        cfg.validate()
        self.assertIsNone(cfg.log_file)

    def test_bad_log_level_rejected(self) -> None:
        cfg = Config(log_level="LOUD")
        with self.assertRaises(Exception):
            cfg.validate()

    def test_abnormal_values_are_clamped(self) -> None:
        cfg = Config(hook_restart_limit=-5, heartbeat_seconds="-1", log_file_level="debug")
        cfg.validate()
        self.assertEqual(cfg.hook_restart_limit, 0)
        self.assertEqual(cfg.heartbeat_seconds, 0)
        self.assertEqual(cfg.log_file_level, "DEBUG")

    def test_bad_heartbeat_rejected(self) -> None:
        cfg = Config(heartbeat_seconds="often")
        with self.assertRaises(Exception):
            cfg.validate()


if __name__ == "__main__":
    unittest.main()
