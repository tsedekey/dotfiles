#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools"]
# ///
"""Build AgentIcons.otf: the Claude / OpenCode / Copilot logos as font glyphs.

Why a font: a terminal cell holds one character, and no Nerd Font has a Claude or
OpenCode logo. Ghostty can map chosen codepoints to a specific font
(`font-codepoint-map`), so the logos live at private-use codepoints E400-E403.

Metrics are copied from JetBrainsMono Nerd Font (unitsPerEm 1000, ascent 1020,
descent -300, cell advance 600) so glyph coordinates land 1:1 on Ghostty's cell.
Each logo is scaled to fit a BOX x BOX square, centred on the line box, and its
advance is CELLS cells wide: write the glyph followed by ONE space ("\\uE400 ")
and the terminal lets it spread over both cells.

Usage:  ./build.py                 write AgentIcons.otf next to this script
        ./build.py --box 1000      smaller logos (font units; 1000 = 1 em)
        ./build.py --dump DIR      also export every glyph FROM THE BUILT FONT as SVG
"""
import argparse
import re
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.svgLib.path import parse_path
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent

# JetBrainsMono Nerd Font Regular (the font in ghostty/config).
UPM, ASCENT, DESCENT, CELL_W = 1000, 1020, -300, 600

# name -> codepoint. claudecode (pixel mascot) and githubcopilot are spares: swap
# the codepoint in workmux/config.yaml (sidebar.agent_icons) to use them.
GLYPHS = {
    "claude": 0xE400,         # the starburst
    "claudecode": 0xE401,     # the pixel mascot
    "opencode": 0xE402,
    "githubcopilot": 0xE403,
}


# Optical correction on top of --box: solid blocks carry more visual weight than the
# spiky burst, so they are drawn a little smaller to read at the same size.
OPTICAL = {"claude": 1.0, "claudecode": 0.9, "opencode": 0.9, "githubcopilot": 1.0}


def svg_path_d(svg: Path) -> str:
    text = svg.read_text()
    paths = re.findall(r'<path\b[^>]*?\sd="([^"]+)"', text)
    if len(paths) != 1:
        raise SystemExit(f"{svg.name}: expected exactly one <path>, found {len(paths)}")
    return paths[0]


def build(box: int, cells: int, out: Path) -> None:
    advance = CELL_W * cells
    centre_y = (ASCENT + DESCENT) / 2  # middle of the line box, above the baseline
    order = [".notdef", *GLYPHS]
    strings = {}
    for name in GLYPHS:
        d = svg_path_d(HERE / "src" / f"{name}.svg")
        bp = BoundsPen(None)
        parse_path(d, bp)
        x0, y0, x1, y1 = bp.bounds
        s = box * OPTICAL[name] / max(x1 - x0, y1 - y0)
        # SVG is y-down: flip y, then move the logo's centre to the middle of the cell box.
        tx = advance / 2 - s * (x0 + x1) / 2
        ty = centre_y + s * (y0 + y1) / 2
        pen = T2CharStringPen(advance, None)
        parse_path(d, TransformPen(pen, (s, 0, 0, -s, tx, ty)))
        strings[name] = pen.getCharString()
    nd = T2CharStringPen(advance, None)
    strings[".notdef"] = nd.getCharString()

    fb = FontBuilder(UPM, isTTF=False)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({cp: n for n, cp in GLYPHS.items()})
    fb.setupCFF("AgentIcons-Regular",
                {"FullName": "AgentIcons Regular", "FamilyName": "AgentIcons", "Weight": "Regular"},
                strings, {})
    metrics = {}
    for n in order:
        gb = BoundsPen(None)
        strings[n].draw(gb)
        metrics[n] = (advance, int(round(gb.bounds[0])) if gb.bounds else 0)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT, lineGap=0)
    fb.setupNameTable({"familyName": "AgentIcons", "styleName": "Regular",
                       "uniqueFontIdentifier": "AgentIcons-Regular",
                       "fullName": "AgentIcons Regular", "psName": "AgentIcons-Regular",
                       "version": "Version 1.000",
                       "licenseDescription": "Logo shapes from Simple Icons (CC0 1.0). "
                                             "Claude, OpenCode and GitHub Copilot marks are trademarks of their owners."})
    fb.setupOS2(version=4, sTypoAscender=ASCENT, sTypoDescender=DESCENT, sTypoLineGap=0,
                usWinAscent=ASCENT, usWinDescent=-DESCENT, sxHeight=550, sCapHeight=730,
                fsSelection=0x40 | 0x80)
    fb.setupPost()
    fb.save(out)
    print(f"wrote {out} ({out.stat().st_size} bytes): box={box} units, advance={advance} ({cells} cells)")


def dump(font: Path, directory: Path) -> None:
    """Export every glyph back out of the compiled font, so what we inspect is what ships."""
    directory.mkdir(parents=True, exist_ok=True)
    f = TTFont(font)
    gs, cmap = f.getGlyphSet(), f.getBestCmap()
    for cp, name in cmap.items():
        pen = SVGPathPen(gs)
        gs[name].draw(pen)
        adv = f["hmtx"][name][0]
        top, bottom = ASCENT, DESCENT
        # flip back to y-down for SVG; the box is the full line box x advance.
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {-top} {adv} {top - bottom}" '
               f'width="{adv // 4}" height="{(top - bottom) // 4}">'
               f'<rect x="0" y="{-top}" width="{adv}" height="{top - bottom}" fill="#fff"/>'
               f'<rect x="0" y="{-top}" width="{CELL_W}" height="{top - bottom}" fill="none" stroke="#c33" stroke-width="4"/>'
               f'<rect x="{CELL_W}" y="{-top}" width="{adv - CELL_W}" height="{top - bottom}" fill="none" stroke="#38c" stroke-width="4"/>'
               f'<line x1="0" x2="{adv}" y1="0" y2="0" stroke="#999" stroke-width="3"/>'
               f'<g transform="scale(1,-1)"><path d="{pen.getCommands()}" fill="#222" fill-rule="nonzero"/></g></svg>')
        (directory / f"{name}-U+{cp:04X}.svg").write_text(svg)
    print(f"dumped {len(cmap)} glyphs to {directory}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--box", type=int, default=1100, help="logo box side in font units (default 1100)")
    ap.add_argument("--cells", type=int, default=2, help="cells the glyph spans (default 2)")
    ap.add_argument("--out", type=Path, default=HERE / "AgentIcons.otf")
    ap.add_argument("--dump", type=Path, help="export glyphs of the built font as SVG into this directory")
    a = ap.parse_args()
    build(a.box, a.cells, a.out)
    if a.dump:
        dump(a.out, a.dump)
