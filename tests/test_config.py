"""Settings: config.toml template, precedence, counterhole options, init."""

import os
import tomllib
from types import SimpleNamespace

import pytest

from pcbfab2step.config import (ConfigError, config_template, init_fab_folder, parse_counterhole_spec, resolve)
from pcbfab2step.pcbfab2step import build_parser, join_counterhole_args


def test_template_parses_and_defers_to_fab_data():
    cfg = tomllib.loads(config_template("test"))
    o = resolve(SimpleNamespace(), cfg)
    assert o.mask_thickness is None and o.board_thickness is None and o.plating_thickness is None
    assert o.via_fill is None and o.ncdrill is None
    assert o.counterhole["top"] == {"type": None, "angle": None, "depth": None}
    assert o.sources.get("mask_thickness") is None


def test_precedence_cli_over_config_over_builtin():
    cfg = {"soldermask": {"thickness": 0.015, "color": "blue"}, "backdrill": {"stub": 0.1}}
    cli = SimpleNamespace(mask_thickness=0.03)
    o = resolve(cli, cfg, "config.toml")
    assert o.mask_thickness == 0.03 and o.sources["mask_thickness"] == "--mask-thickness"
    assert o.mask_color == pytest.approx((0x1f / 255, 0x4e / 255, 0x9c / 255))
    assert o.backdrill_stub == 0.1 and o.sources["backdrill_stub"] == "config.toml [backdrill] stub"
    assert o.silk_thickness is None and o.cap_thickness == 0.015


def test_counterhole_sides():
    cfg = {"counterholes": {"type": "sink", "top": {"type": "bore"}}}
    cli = SimpleNamespace(counterhole=["angle=82"], counterhole_top=[], counterhole_bottom=["depth=0.5"])
    o = resolve(cli, cfg, "config.toml")
    top, bot = o.counterhole["top"], o.counterhole["bottom"]
    assert top["type"] == ("counterbore", "config.toml [counterholes.top]")
    assert top["angle"] == (82.0, "--counterhole")
    assert bot["type"] == ("countersink", "config.toml [counterholes]")
    assert bot["depth"] == (0.5, "--counterhole-bottom")


def test_counterhole_spec_parsing():
    assert parse_counterhole_spec("type=sink,angle=82") == {"type": "countersink", "angle": 82.0}
    assert parse_counterhole_spec("bore depth=1.2") == {"type": "counterbore", "depth": 1.2}
    with pytest.raises(ConfigError):
        parse_counterhole_spec("type=hole")
    with pytest.raises(ConfigError):
        parse_counterhole_spec("angle=200")


def test_counterhole_words_do_not_swallow_inputs():
    argv = ["--counterhole-top", "type=sink", "angle=82", "board.tgz", "--counterhole", "bore"]
    assert join_counterhole_args(argv) == ["--counterhole-top", "type=sink,angle=82", "board.tgz",
                                           "--counterhole", "bore"]
    a = build_parser().parse_args(join_counterhole_args(argv))
    assert a.inputs == ["board.tgz"] and a.counterhole_top == ["type=sink,angle=82"]


def test_init(tmp_path):
    fab = tmp_path / "FAB"
    out = init_fab_folder(str(fab), "test")
    for d in ("ODB++", "IPC2581", "GERBER", "NCDRILL"):
        assert (fab / d).is_dir()
    assert (fab / "config.toml").is_file() and any("wrote" in s for s in out)
    (fab / "config.toml").write_text("# mine\n")
    init_fab_folder(str(fab), "test")
    assert (fab / "config.toml").read_text() == "# mine\n"            # kept without --force
    init_fab_folder(str(fab), "test", force=True)
    assert "[counterholes.top]" in (fab / "config.toml").read_text()


def test_bad_values():
    with pytest.raises(ConfigError):
        resolve(SimpleNamespace(), {"soldermask": {"color": "chartreuse-ish"}})
    with pytest.raises(ConfigError):
        resolve(SimpleNamespace(), {"vias": {"tent": "sideways"}})
    assert os.path.basename(__file__)
