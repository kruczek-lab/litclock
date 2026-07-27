"""Unit tests for src/display_driver.py — the any-panel driver abstraction.

The vendored waveshare_epd library is hardware-bound and absent on dev
boxes/CI, so these tests inject fake driver modules into sys.modules and
exercise the selection, geometry, and adapter contracts:

- model selection: env → env.sh → default, with validation
- display_geometry(): env override → KNOWN_GEOMETRY → driver constants →
  default, always landscape-normalized, never touching hardware
- EinkPanel: vendor-shaped delegation (init/getbuffer/display/Clear/sleep),
  Clear() signature papering, init failure surfacing, watchdog timeout,
  EINK_ROTATE=180
- renderer integration: eink_display / literary_clock geometry follows the
  configured panel on reload
"""

from __future__ import annotations

import importlib
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import display_driver  # noqa: E402

_HAS_PIL = True
try:
    from PIL import Image
except ImportError:
    _HAS_PIL = False


def _make_epd_class(
    width: int,
    height: int,
    *,
    init_ret=0,
    clear_needs_arg=False,
    init_needs_arg=False,
    init_hang_s: float = 0,
):
    class FakeEPD:
        instances: list = []

        def __init__(self):
            self.width = width
            self.height = height
            self.calls: list = []
            self.buffers: list = []
            FakeEPD.instances.append(self)

        if init_needs_arg:

            def init(self, update):
                if init_hang_s:
                    time.sleep(init_hang_s)
                self.calls.append(("init", update))
                return init_ret
        else:

            def init(self):
                if init_hang_s:
                    time.sleep(init_hang_s)
                self.calls.append("init")
                return init_ret

        def getbuffer(self, image):
            self.calls.append("getbuffer")
            self.buffers.append(image)
            return b"buf"

        def display(self, buffer):
            self.calls.append(("display", buffer))

        if clear_needs_arg:

            def Clear(self, color):  # noqa: N802 — vendor casing
                self.calls.append(("clear", color))
        else:

            def Clear(self):  # noqa: N802 — vendor casing
                self.calls.append("clear")

        def sleep(self):
            self.calls.append("sleep")

    return FakeEPD


@pytest.fixture
def fake_driver(monkeypatch):
    """Install a fake waveshare_epd package; returns a registrar function."""
    pkg = types.ModuleType("waveshare_epd")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "waveshare_epd", pkg)

    def register(name: str, width: int, height: int, **epd_kwargs):
        mod = types.ModuleType(f"waveshare_epd.{name}")
        mod.EPD_WIDTH = width
        mod.EPD_HEIGHT = height
        mod.EPD = _make_epd_class(width, height, **epd_kwargs)
        monkeypatch.setitem(sys.modules, f"waveshare_epd.{name}", mod)
        return mod

    return register


@pytest.fixture(autouse=True)
def _clean_eink_env(monkeypatch):
    for key in ("EINK_MODEL", "EINK_ROTATE", "EINK_WIDTH", "EINK_HEIGHT", "EINK_OP_TIMEOUT_S"):
        monkeypatch.delenv(key, raising=False)
    # Point env.sh reads at a nonexistent file so a developer's real env.sh
    # can't leak into assertions.
    monkeypatch.setenv("LITCLOCK_ENV_FILE", "/nonexistent/litclock-test-env.sh")


class TestModelSelection:
    def test_default_model(self):
        assert display_driver.model_name() == "epd7in5_V2"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd2in7_V2")
        assert display_driver.model_name() == "epd2in7_V2"

    def test_invalid_name_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd2in7; rm -rf /")
        assert display_driver.model_name() == "epd7in5_V2"

    def test_env_sh_fallback(self, monkeypatch, tmp_path):
        env_sh = tmp_path / "env.sh"
        env_sh.write_text('# comment\nexport WEATHER_UNITS=imperial\nexport EINK_MODEL="epd4in2"\n')
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env_sh))
        assert display_driver.model_name() == "epd4in2"

    def test_process_env_wins_over_env_sh(self, monkeypatch, tmp_path):
        env_sh = tmp_path / "env.sh"
        env_sh.write_text("export EINK_MODEL=epd4in2\n")
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env_sh))
        monkeypatch.setenv("EINK_MODEL", "epd2in9_V2")
        assert display_driver.model_name() == "epd2in9_V2"

    def test_unreadable_env_sh_is_harmless(self, monkeypatch):
        monkeypatch.setenv("LITCLOCK_ENV_FILE", "/nonexistent/nope.sh")
        assert display_driver.model_name() == "epd7in5_V2"


class TestDisplayGeometry:
    def test_default(self):
        assert display_driver.display_geometry() == (800, 480)

    def test_known_model_no_driver_needed(self, monkeypatch):
        # No waveshare_epd is installed in the test env — the table answers.
        monkeypatch.setenv("EINK_MODEL", "epd2in7_V2")
        assert display_driver.display_geometry() == (264, 176)

    def test_env_size_override_and_portrait_normalization(self, monkeypatch):
        monkeypatch.setenv("EINK_WIDTH", "176")
        monkeypatch.setenv("EINK_HEIGHT", "264")
        assert display_driver.display_geometry() == (264, 176)

    def test_invalid_env_size_ignored(self, monkeypatch):
        monkeypatch.setenv("EINK_WIDTH", "abc")
        monkeypatch.setenv("EINK_HEIGHT", "264")
        assert display_driver.display_geometry() == (800, 480)

    def test_unknown_model_reads_driver_constants(self, monkeypatch, fake_driver):
        fake_driver("epd_custom", 128, 296)
        monkeypatch.setenv("EINK_MODEL", "epd_custom")
        # Portrait constants are landscape-normalized.
        assert display_driver.display_geometry() == (296, 128)

    def test_unknown_model_without_driver_falls_back(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd_not_a_panel")
        assert display_driver.display_geometry() == (800, 480)


class TestEinkPanel:
    def test_vendor_shaped_delegation(self, monkeypatch, fake_driver):
        fake_driver("epd_fake", 250, 122)
        monkeypatch.setenv("EINK_MODEL", "epd_fake")
        panel = display_driver.get_panel()
        assert (panel.width, panel.height) == (250, 122)
        assert panel.init() == 0
        buf = panel.getbuffer(Image.new("1", (250, 122), 255)) if _HAS_PIL else b"buf"
        if not _HAS_PIL:
            pytest.skip("PIL required for getbuffer")
        panel.display(buf)
        panel.Clear()
        panel.sleep()
        epd = panel.epd
        assert "init" in epd.calls
        assert ("display", b"buf") in epd.calls
        assert "clear" in epd.calls
        assert "sleep" in epd.calls

    def test_portrait_panel_reports_landscape_logical_size(self, monkeypatch, fake_driver):
        fake_driver("epd_portrait", 122, 250)
        monkeypatch.setenv("EINK_MODEL", "epd_portrait")
        panel = display_driver.get_panel()
        assert (panel.width, panel.height) == (250, 122)

    def test_clear_signature_fallback(self, monkeypatch, fake_driver):
        fake_driver("epd_argclear", 250, 122, clear_needs_arg=True)
        monkeypatch.setenv("EINK_MODEL", "epd_argclear")
        panel = display_driver.get_panel()
        panel.Clear()
        assert ("clear", 0xFF) in panel.epd.calls

    def test_init_signature_fallback(self, monkeypatch, fake_driver):
        """epd2in13_V2-style drivers require init(update); the adapter must
        retry with FULL_UPDATE (0) rather than dying on the TypeError."""
        fake_driver("epd_arginit", 250, 122, init_needs_arg=True)
        monkeypatch.setenv("EINK_MODEL", "epd_arginit")
        panel = display_driver.get_panel()
        assert panel.init() == 0
        assert ("init", 0) in panel.epd.calls

    def test_init_failure_raises(self, monkeypatch, fake_driver):
        fake_driver("epd_dead", 250, 122, init_ret=-1)
        monkeypatch.setenv("EINK_MODEL", "epd_dead")
        panel = display_driver.get_panel()
        with pytest.raises(RuntimeError, match="init"):
            panel.init()

    def test_watchdog_timeout(self, monkeypatch, fake_driver):
        fake_driver("epd_hung", 250, 122, init_hang_s=5)
        monkeypatch.setenv("EINK_MODEL", "epd_hung")
        monkeypatch.setenv("EINK_OP_TIMEOUT_S", "0.2")
        panel = display_driver.get_panel()
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="not responding"):
            panel.init()
        assert time.monotonic() - start < 2, "timeout must fire well before the hung call returns"

    @pytest.mark.skipif(not _HAS_PIL, reason="PIL required")
    def test_rotate_180(self, monkeypatch, fake_driver):
        fake_driver("epd_fake", 250, 122)
        monkeypatch.setenv("EINK_MODEL", "epd_fake")
        monkeypatch.setenv("EINK_ROTATE", "180")
        panel = display_driver.get_panel()
        img = Image.new("1", (250, 122), 255)
        img.putpixel((0, 0), 0)
        panel.getbuffer(img)
        seen = panel.epd.buffers[0]
        assert seen.getpixel((249, 121)) == 0, "180° rotation must move the (0,0) mark to the far corner"
        assert seen.getpixel((0, 0)) != 0

    def test_missing_driver_raises_for_get_panel(self, monkeypatch):
        monkeypatch.setenv("EINK_MODEL", "epd_not_a_panel")
        with pytest.raises(ModuleNotFoundError):
            display_driver.get_panel()


@pytest.mark.skipif(not _HAS_PIL, reason="PIL required")
class TestRendererGeometryIntegration:
    """eink_display / literary_clock capture DISPLAY_SIZE at import — verify
    it follows the configured panel across a reload, then restore."""

    def _reload_with_size(self, module_name: str, width: str, height: str):
        os.environ["EINK_WIDTH"] = width
        os.environ["EINK_HEIGHT"] = height
        module = importlib.import_module(module_name)
        return importlib.reload(module)

    def _restore(self, module_name: str):
        os.environ.pop("EINK_WIDTH", None)
        os.environ.pop("EINK_HEIGHT", None)
        importlib.reload(importlib.import_module(module_name))

    def test_eink_display_follows_configured_geometry(self):
        try:
            mod = self._reload_with_size("eink_display", "400", "300")
            assert mod.DISPLAY_SIZE == (400, 300)
            img = mod.create_status_image("LitClock", message="Starting...")
            assert img.size == (400, 300)
            # 400×300 is below the compact threshold — the handoff splash
            # must take the collision-free compact layout and still render.
            splash = mod.create_handoff_splash_image({"has_location": True}, "http://192.168.1.2")
            assert splash.size == (400, 300)
        finally:
            self._restore("eink_display")

    def test_literary_clock_corner_qr_gate(self):
        try:
            mod = self._reload_with_size("literary_clock", "264", "176")
            assert mod.DISPLAY_SIZE == (264, 176)
            assert mod.QR_CORNER_FITS is False
        finally:
            self._restore("literary_clock")
            mod = importlib.import_module("literary_clock")
            # Locked 7.5" geometry restored (matches test_literary_clock.py).
            assert mod.QR_CORNER_FITS is True
            assert mod.QR_POSITION == (713, 0)


@pytest.mark.skipif(not _HAS_PIL, reason="PIL required")
class TestTopStripLayout:
    """Small-panel top strip (literary_clock._draw_top_strip). Locks the
    defects found on 2.7" hardware: temps pinned at the size floor beside a
    much larger date, and the date crowding the vertical rule."""

    @staticmethod
    def _clock(width: str, height: str):
        import importlib

        os.environ["EINK_WIDTH"], os.environ["EINK_HEIGHT"] = width, height
        return importlib.reload(importlib.import_module("literary_clock"))

    @staticmethod
    def _restore():
        import importlib

        os.environ.pop("EINK_WIDTH", None)
        os.environ.pop("EINK_HEIGHT", None)
        importlib.reload(importlib.import_module("literary_clock"))

    def _strip(self, lc, temps="95°F / 73°F", compact="95/73°F"):
        from datetime import datetime

        from PIL import Image, ImageDraw

        img = Image.new("1", lc.DISPLAY_SIZE, 255)
        lc._draw_top_strip(ImageDraw.Draw(img), datetime(2026, 7, 26, 20, 36), temps, compact)
        return img

    def test_ink_never_crosses_the_horizontal_rule_or_edges(self):
        try:
            for w, h in (("264", "176"), ("250", "122"), ("400", "300")):
                lc = self._clock(w, h)
                bbox = self._strip(lc).point(lambda p: 255 - p).getbbox()
                assert bbox is not None
                assert bbox[2] <= lc.DISPLAY_SIZE[0], f"{w}x{h}: ink past right edge"
                assert bbox[3] <= lc.DIVIDER_Y + lc.DIVIDER_WIDTH, f"{w}x{h}: ink below the rule"
        finally:
            self._restore()

    def test_temperatures_are_not_pinned_at_the_size_floor(self):
        """Regression: sizing temps independently bottomed out at 10px next
        to an 18px date, which shredded the degree glyphs on hardware."""
        try:
            lc = self._clock("264", "176")
            assert lc.TOP_STRIP_TEMP_MIN >= 11
            # Temps ride off the date size, so they track it rather than floor.
            assert lc.TOP_STRIP_TEMP_RATIO >= 0.7
        finally:
            self._restore()

    def test_date_has_breathing_room_after_the_rule(self):
        """The date must not butt against the vertical rule (reported from
        hardware). Assert a real gap of blank columns between them."""
        try:
            lc = self._clock("264", "176")
            img = self._strip(lc)
            px = img.load()
            band = range(0, max(1, lc.DIVIDER_Y - 2))
            inked = [x for x in range(lc.DISPLAY_SIZE[0]) if any(px[x, y] == 0 for y in band)]
            # The vertical rule is a fully-inked column; find it, then require
            # clear columns before the date's first ink.
            rule_x = max(x for x in inked if all(px[x, y] == 0 for y in band))
            after = [x for x in inked if x > rule_x + lc.DIVIDER_WIDTH]
            assert after, "no date ink right of the rule"
            assert after[0] - rule_x >= 5, f"date only {after[0] - rule_x}px from the rule"
        finally:
            self._restore()

    def test_falls_back_to_date_only_when_impossibly_narrow(self):
        try:
            lc = self._clock("120", "80")
            bbox = self._strip(lc).point(lambda p: 255 - p).getbbox()
            assert bbox is not None and bbox[2] <= 120
        finally:
            self._restore()

    def test_reference_panel_keeps_the_locked_rule_weight(self):
        import importlib

        lc = importlib.reload(importlib.import_module("literary_clock"))
        assert lc.DISPLAY_SIZE == (800, 480)
        assert lc.DIVIDER_WIDTH == 4
