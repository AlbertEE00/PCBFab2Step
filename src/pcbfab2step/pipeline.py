# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Albert van Zyl

"""The stages of one run, shared by the command line, the viewer and the GUI.

    job = Job(args, mode)    inputs found, settings merged (nothing read yet)
    job.read()               the fab data read (kept in the Session per inputs
                             and reader settings: a GUI change does not re-read)
    job.prepare()            the model: stackup, holes, via protection
    job.build()              the B-rep solids, with the ECAD STEP's components
    job.write_model()        the STEP model; job.write_export(): the --export files

scene.model_scene(job) and scene.layer_scene(job, spec) make the viewer's
meshes from the same job.

The command line runs one Job. The GUI keeps a Session and makes a Job for
every change of its command line; what a change does not touch is reused.
"""

from __future__ import annotations

import os
import re
import time
from typing import List, Optional

from .board import parse_via_rule
from .common import info, warn
from .config import ConfigError, check_config, load_config, resolve


def safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", s).strip()


class Session:
    """What jobs share: boards read from the fab data (the expensive part)."""

    def __init__(self, keep: int = 2):
        self.keep = keep
        self.boards: dict = {}          # read key -> (board, reader)

    def board(self, key, read):
        if key in self.boards:
            return self.boards[key]
        got = read()
        self.boards[key] = got
        while len(self.boards) > self.keep:
            self.boards.pop(next(iter(self.boards)))
        return got


class Job:
    def __init__(self, args, mode: str = "model", session: Optional[Session] = None):
        """mode: model, export or info."""
        from .builder import parse_size_rule
        from .sources import choose, discover
        from .cli import settings_from_args
        self.args, self.mode = args, mode
        self.session = session or Session()
        self.t0 = time.time()
        srcs = discover(args.inputs, args.config, args.no_config)
        cfg, label = None, "config.toml"
        if srcs.config:
            cfg = load_config(srcs.config)
            label = os.path.basename(srcs.config)
            for w in check_config(cfg, label):
                warn(w)
        cli = settings_from_args(args)
        try:
            opts = resolve(cli, cfg, label)
            opts.via_rules = [parse_via_rule(s) for s in opts.via_protection]
        except (ConfigError, ValueError) as exc:
            raise SystemExit(str(exc))
        for spec in opts.counterbore_rules:
            parse_size_rule(spec, "counterbore")
        for spec in opts.countersink_rules:
            parse_size_rule(spec, "countersink")
        opts.list = mode == "info"
        opts.external_only = opts.sanitize != "none"
        opts.outer_copper = {"exposed": "exposed", "pads": "pads"}.get(opts.sanitize, "all")
        opts.vias = cli.vias_drilled
        opts.export_outline = getattr(args, "outline", None)
        opts.export_invert = getattr(args, "invert", False)
        choose(srcs, opts.use_sources)
        self.srcs, self.opts, self.cli, self.label = srcs, opts, cli, label
        self.board = self.reader = self.model = None
        self.built = False

    # .. settings that live in module globals ...................................
    def apply_globals(self):
        from .brep import ArcFit, Brep
        from .geometry import Tess
        o = self.opts
        Tess.tol = max(1e-5, o.arc_tolerance)
        ArcFit.enabled = o.arc_fit != "off"
        ArcFit.strict = o.arc_fit == "strict"
        Brep.max_holes = o.max_holes_per_face

    def input_lines(self) -> List[str]:
        from .sources import LABEL
        s = self.srcs
        out = [f"Input    : {LABEL[s.primary]} {s.primary_path}"]
        for k, path in (("ipc", s.ipc_hints), ("odb", s.odb_types), ("step", s.step)):
            if path:
                out.append(f"           {LABEL[k]} {path}")
        for d in s.drill:
            out.append(f"           NC drill {d}")
        if s.config:
            out.append(f"Config   : {s.config}")
        out += ["  " + nt for nt in s.notes]
        return out

    # .. read ................................................................
    def read_key(self):
        s, o = self.srcs, self.opts

        def stamp(p):
            try:
                return (p, os.path.getmtime(p))
            except OSError:
                return (p, None)
        files = [s.primary_path, s.ipc_hints, s.odb_types] + list(s.drill) + list(s.explicit.get("drill", []))
        return (tuple(stamp(p) for p in files if p), tuple(s.use), o.arc_tolerance, getattr(o, "odb_step", None),
                o.fill_material, o.plug_depth)

    def read(self):
        if self.board is None:
            self.apply_globals()
            board, reader = self.session.board(self.read_key(), self._read)
            self.board, self.reader = board, reader
        return self.board

    def _read(self):
        from . import ipc2581, ncdrill, odbpp
        s, o = self.srcs, self.opts
        reader = odbpp.OdbReader(s.primary_path, o) if s.primary == "odb" else ipc2581.IpcReader(s.primary_path, o)
        board = reader.read()
        if s.ipc_hints:
            hints, notes = ipc2581.read_hints(s.ipc_hints)
            board.hints += hints
            board.hint_notes += notes
        if s.odb_types:
            board.prot_notes += odbpp.apply_via_types(s.odb_types, o, board)
        if "drill" in s.use:
            inside = ncdrill.pool_from_files("inside the ODB++ input", reader.inside_files()) \
                if s.primary == "odb" else None
            chosen, notes = ncdrill.find_reports(list(s.explicit["drill"]), inside, [], ncdrill._stem(s.primary_path),
                                                 s.drill)
            hints, notes2 = ncdrill.read_hints(chosen)
            board.hints = hints + board.hints          # NC drill first: it has exact counterbore depths
            board.hint_notes += notes + notes2
        return board, reader

    # .. prepare ..............................................................
    def prepare(self):
        from .builder import Model
        if self.model is None:
            board = self.read().fresh()
            self.apply_globals()
            self.model = Model(board, self.opts)
            self.model.prepare()
        return self.model

    @property
    def base(self) -> str:
        return safe_name(self.read().name)

    # .. outputs ..............................................................
    def model_path(self) -> str:
        o, s = self.opts, self.srcs
        out = o.output
        if out and o.sources.get("output", "").startswith(self.label) and s.config:
            out = os.path.join(os.path.dirname(os.path.abspath(s.config)), out)
        if not out:
            suffix = f"_sanitized-{o.sanitize}" if o.sanitize != "none" else ""
            out = os.path.join(s.out_dir, f"{self.base}{suffix}.step")
        return out

    def build(self):
        """The B-rep solids, with the ECAD STEP's components."""
        from .builder import write_svgs
        m = self.prepare()
        if self.built:
            return m
        self.apply_globals()
        m.build()
        if m.board.after_build is not None:
            m.board.after_build()
        if self.opts.svg_dir:
            write_svgs(self.opts.svg_dir, m.svg, m.B)
        s, o = self.srcs, self.opts
        if s.step and o.components:
            from . import components
            st = m.stk
            t_mask = {side: (0.0 if o.no_mask else max((L.t for L in st.coats[side].get("mask", [])), default=0.0))
                      for side in ("top", "bottom")}
            info("\nComponents:")
            parts = components.read(s.step, m.B.bounds, st.z_top + t_mask["top"], st.z_bot - t_mask["bottom"])
            if parts:
                for c in m.components:
                    c["path"] = ("PCB",)
                m.components += parts
        self.built = True
        return m

    def write_model(self) -> str:
        from .brep import write_step
        from .sources import LABEL
        m = self.build()
        out = self.model_path()
        os.makedirs(self.srcs.out_dir, exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        info(f"Writing {out} ...")
        root = f"{m.board.name} {m.board.step}".strip()
        write_step(out, root, m.components, self.opts.schema, fmt=LABEL[self.srcs.primary])
        return out

    def write_export(self) -> List[str]:
        from . import exports
        m = self.prepare()
        os.makedirs(self.srcs.out_dir, exist_ok=True)
        return exports.export(m, self.args.export, self.opts, self.srcs.out_dir, self.base, self.args.output)
