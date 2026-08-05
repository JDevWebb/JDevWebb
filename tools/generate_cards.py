#!/usr/bin/env python3
"""Generate the profile SVG cards.

Renders two artefacts, each in a light and a dark variant:

  assets/nameplate-{theme}.svg  -- the identity header
  assets/stack-{theme}.svg      -- language distribution across every repo I own

The stack card reads live data from the GitHub API (via `gh`), so it counts
private repositories too. Off-the-shelf stat widgets only see public repos,
which for this account is a handful of forks -- i.e. the wrong answer.

Usage:
    python3 tools/generate_cards.py            # refresh from the API
    python3 tools/generate_cards.py --offline  # re-render from assets/langs.json
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# ---------------------------------------------------------------------------
# Design tokens
#
# Concept: an industrial HMI panel -- a machined bezel with a readout screen
# behind it, the kind bolted to the plant Jason's software actually runs.
# Dark mode is an amber-phosphor CRT; light mode is a reflective LCD. The bezel
# carries the craft, the screen carries the data.
#
# Every accent is validated, not eyeballed: bar fills pass the OKLCH lightness
# band and >= 3:1 against their own screen surface; the dimmest text on each
# screen clears 4.5:1.
# ---------------------------------------------------------------------------

THEMES = {
    # Amber phosphor behind gunmetal.
    "dark": {
        "plate_hi": "#2B3138",
        "plate_mid": "#1D2126",
        "plate_lo": "#12151A",
        "bevel_hi": "#3C444D",
        "bevel_lo": "#0B0D10",
        "screen_hi": "#0C0F0D",
        "screen_lo": "#070908",
        "ink": "#F7C368",        # 12.1:1
        "ink_mid": "#E8A33D",    #  9.1:1
        "ink_dim": "#A9762F",    #  5.0:1
        "rule": "#4A3A22",
        "bar": "#C67A24",        # L 0.65, passes band + contrast
        "bar_dim": "#4A4034",
        "track": "#16191A",
        "seg_gap": "#070908",
        "scan": "#000000",
        "scan_op": "0.30",
        "grid_op": "0",          # scanlines only, no vertical grid
        "vignette_op": "0.45",
        "brush": "#FFFFFF",
        "brush_op": "0.022",
        "rivet_hi": "#59626C",
        "rivet_lo": "#141820",
    },
    # Reflective LCD behind brushed aluminium.
    "light": {
        "plate_hi": "#EAEEF2",   # kept off pure white so the bezel's top
        "plate_mid": "#D3D9DF",  # edge still reads against a white page
        "plate_lo": "#B7BFC7",
        "bevel_hi": "#FDFDFE",
        "bevel_lo": "#98A1AA",
        "screen_hi": "#C3C7B6",
        "screen_lo": "#CDD1C0",
        "ink": "#16180F",        # 10.4:1
        "ink_mid": "#3A3F2E",    #  6.3:1
        "ink_dim": "#4E5344",    #  4.6:1
        "rule": "#8E9482",
        "bar": "#7D5716",        # L 0.47, passes band + contrast
        "bar_dim": "#9AA08C",
        "track": "#B4B9A6",
        "seg_gap": "#C8CCBB",
        "scan": "#000000",
        "scan_op": "0.05",
        "grid_op": "0.05",       # full pixel grid, as an LCD has
        "vignette_op": "0.10",
        "brush": "#FFFFFF",
        "brush_op": "0.34",
        "rivet_hi": "#FFFFFF",
        "rivet_lo": "#8F979F",
    },
}

# Everything on a readout is monospace. There is no second face.
MONO = ("ui-monospace,SFMono-Regular,&#39;SF Mono&#39;,Menlo,Consolas,"
        "&#39;Liberation Mono&#39;,monospace")

# Screen inset from the card edge, and the clearance content keeps inside the
# screen. Card heights are derived from these plus the content, so the readout
# is never crowded against the bezel.
BEZEL = 26
PAD = 28


def ceil_even(content_bottom: int | float) -> int:
    """Card height that leaves PAD below the lowest element, then the bezel."""
    return int(content_bottom + PAD + BEZEL)


# The neofetch info block.
PLATE_ROWS = [
    ("Role", "Full-stack developer"),
    ("Based", "Christchurch, NZ"),
    ("Clients", "NZ small business"),
    ("Stack", "Vue · Django · Docker"),
    ("Builds", "Business systems"),
    ("Since", "2017"),
]


def sun(cols=37, rows=15, r_ring=4.2, radii=(5.8,), n_rays=16, AR=1.92):
    """The heliosIT mark in ASCII: an open ring inside a corona of dashes.

    Drawn from polar coordinates rather than typed by hand, so it stays
    symmetric. AR compensates for monospace cells being about half as wide
    as they are tall; each ray takes the glyph closest to its own direction.
    """
    g = [[" "] * cols for _ in range(rows)]
    cx, cy = (cols - 1) / 2, (rows - 1) / 2

    def put(x, y, ch):
        c, r = round(cx + x * AR), round(cy + y)
        if 0 <= r < rows and 0 <= c < cols and g[r][c] == " ":
            g[r][c] = ch

    for i in range(240):
        a = 2 * math.pi * i / 240
        put(r_ring * math.cos(a), r_ring * math.sin(a), "·")

    for k in range(n_rays):
        a = 2 * math.pi * k / n_rays
        dx, dy = math.cos(a), math.sin(a)      # dy positive = down the screen
        if abs(dy) < 0.36:
            ch = "-"
        elif abs(dx) < 0.36:
            ch = "|"
        elif dx * dy > 0:
            ch = "\\"
        else:
            ch = "/"
        for r in radii:
            put(r * dx, r * dy, ch)

    out = ["".join(r).rstrip() for r in g]
    while out and not out[0]:          # the grid is sized for the corona;
        out.pop(0)                     # drop whatever rows it didn't reach
    while out and not out[-1]:
        out.pop()
    return out


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_language_bytes() -> tuple[dict[str, int], int]:
    """Aggregate Linguist byte counts across every non-fork repo I own."""
    raw = subprocess.run(
        ["gh", "api", "--paginate", "/user/repos?affiliation=owner&per_page=100"],
        capture_output=True, text=True, check=True,
    ).stdout
    repos = [r for r in json.loads(raw) if not r["fork"]]

    totals: collections.Counter[str] = collections.Counter()
    for repo in repos:
        out = subprocess.run(
            ["gh", "api", f"/repos/{repo['full_name']}/languages"],
            capture_output=True, text=True,
        ).stdout
        for lang, count in json.loads(out or "{}").items():
            totals[lang] += count

    return dict(totals), len(repos)


def to_rows(totals: dict[str, int], top: int = 6) -> list[tuple[str, float, bool]]:
    """Top-N languages by share, with the tail folded into a single 'Other'.

    Returns (label, percent, is_tail) sorted descending. Folding the tail keeps
    every bar readable -- sub-1% slivers are noise, not information.
    """
    total = sum(totals.values())
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    rows = [(name, 100 * n / total, False) for name, n in ranked[:top]]
    tail = sum(n for _, n in ranked[top:])
    if tail:
        rows.append(("Other", 100 * tail / total, True))
    return rows


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def text(x, y, s, *, font=MONO, size=13, weight=400, fill="#000",
         track=0, anchor="start", opacity=None, preserve=False):
    attrs = [
        f'x="{x}"', f'y="{y}"', f'font-family="{font}"', f'font-size="{size}"',
        f'font-weight="{weight}"', f'fill="{fill}"',
    ]
    if track:
        attrs.append(f'letter-spacing="{track}"')
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if opacity is not None:
        attrs.append(f'opacity="{opacity}"')
    if preserve:                       # ASCII art depends on leading spaces
        attrs.append('xml:space="preserve"')
    return f'<text {" ".join(attrs)}>{esc(s)}</text>'


def spans(x, y, pieces, *, size=13, weight=400, track=0):
    """One text run made of differently-coloured pieces.

    Uses tspans rather than separate <text> elements so the renderer advances
    the cursor itself -- monospace advance varies just enough between Menlo,
    SF Mono and Consolas to drift if we compute x by hand.
    """
    body = "".join(
        f'<tspan fill="{fill}"'
        + (f' font-weight="{w}"' if w else "")
        + f'>{esc(s)}</tspan>'
        for s, fill, w in pieces
    )
    trk = f' letter-spacing="{track}"' if track else ""
    return (f'<text x="{x}" y="{y}" font-family="{MONO}" font-size="{size}" '
            f'font-weight="{weight}"{trk} xml:space="preserve">{body}</text>')


def prompt(x, y, cmd, d, *, size=12):
    """A shell prompt line: user@host in phosphor, path dim, command bright."""
    return spans(x, y, [
        ("jason@heliosit", d["ink_mid"], 600),
        (":~$ ", d["ink_dim"], None),
        (cmd, d["ink"], 600),
    ], size=size)


def bar_path(x, y, w, h, r=2.0):
    """Horizontal bar: square at the origin, lightly rounded at the growing end."""
    r = min(r, w / 2, h / 2)
    if w <= 0:
        return ""
    return (f'M{x},{y} H{x + w - r} A{r},{r} 0 0 1 {x + w},{y + r} '
            f'V{y + h - r} A{r},{r} 0 0 1 {x + w - r},{y + h} H{x} Z')


def panel(t, w, h):
    """Machined bezel + readout screen: brushed body, glass, scanlines, rivets."""
    d = THEMES[t]
    sx, sy, sw, sh = 56, 26, w - 112, h - 52

    rivets = "".join(
        f'<circle cx="{cx}" cy="{cy}" r="5.5" fill="url(#rivet)"/>'
        f'<circle cx="{cx}" cy="{cy}" r="5.5" fill="none" '
        f'stroke="{d["bevel_lo"]}" stroke-opacity="0.55" stroke-width="0.9"/>'
        f'<circle cx="{cx - 1.4}" cy="{cy - 1.4}" r="1.6" '
        f'fill="{d["rivet_hi"]}" opacity="0.75"/>'
        for cx, cy in ((34, 34), (w - 34, 34), (34, h - 34), (w - 34, h - 34))
    )
    return f'''
  <rect x="4" y="4" width="{w - 8}" height="{h - 8}" rx="13" fill="url(#body)"/>
  <rect x="4" y="4" width="{w - 8}" height="{h - 8}" rx="13" fill="url(#brush)"/>
  <rect x="4.75" y="4.75" width="{w - 9.5}" height="{h - 9.5}" rx="12.5"
        fill="none" stroke="url(#bevel)" stroke-width="1.5"/>
  <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="4" fill="url(#screen)"/>
  <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="4" fill="url(#scan)"/>
  <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="4" fill="url(#vig)"/>
  <rect x="{sx + 0.5}" y="{sy + 0.5}" width="{sw - 1}" height="{sh - 1}" rx="3.5"
        fill="none" stroke="url(#glassEdge)" stroke-width="1"/>
  {rivets}'''


def defs(t, h):
    d = THEMES[t]
    grid = ""
    if float(d["grid_op"]) > 0:
        grid = (f'<line x1="0.5" y1="0" x2="0.5" y2="3" stroke="{d["scan"]}" '
                f'stroke-opacity="{d["grid_op"]}" stroke-width="1"/>')
    return f'''<defs>
    <linearGradient id="body" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{d["plate_hi"]}"/>
      <stop offset="0.45" stop-color="{d["plate_mid"]}"/>
      <stop offset="1" stop-color="{d["plate_lo"]}"/>
    </linearGradient>
    <linearGradient id="bevel" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{d["bevel_hi"]}"/>
      <stop offset="0.5" stop-color="{d["bevel_hi"]}" stop-opacity="0.15"/>
      <stop offset="1" stop-color="{d["bevel_lo"]}"/>
    </linearGradient>
    <linearGradient id="screen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{d["screen_hi"]}"/>
      <stop offset="1" stop-color="{d["screen_lo"]}"/>
    </linearGradient>
    <linearGradient id="glassEdge" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{d["bevel_lo"]}"/>
      <stop offset="1" stop-color="{d["bevel_hi"]}" stop-opacity="0.45"/>
    </linearGradient>
    <radialGradient id="vig" cx="0.5" cy="0.46" r="0.78">
      <stop offset="0.5" stop-color="#000000" stop-opacity="0"/>
      <stop offset="1" stop-color="#000000" stop-opacity="{d["vignette_op"]}"/>
    </radialGradient>
    <radialGradient id="rivet" cx="0.35" cy="0.32" r="0.85">
      <stop offset="0" stop-color="{d["rivet_hi"]}"/>
      <stop offset="1" stop-color="{d["rivet_lo"]}"/>
    </radialGradient>
    <pattern id="brush" width="6" height="3" patternUnits="userSpaceOnUse">
      <line x1="0" y1="0.5" x2="6" y2="0.5" stroke="{d["brush"]}"
            stroke-opacity="{d["brush_op"]}" stroke-width="1"/>
    </pattern>
    <pattern id="scan" width="3" height="3" patternUnits="userSpaceOnUse">
      <line x1="0" y1="2.5" x2="3" y2="2.5" stroke="{d["scan"]}"
            stroke-opacity="{d["scan_op"]}" stroke-width="1"/>
      {grid}
    </pattern>
  </defs>'''


def svg(t, w, h, body, title, desc):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" '
            f'aria-labelledby="t d">\n'
            f'  <title id="t">{esc(title)}</title>\n'
            f'  <desc id="d">{esc(desc)}</desc>\n'
            f'{defs(t, h)}\n{panel(t, w, h)}\n{body}\n</svg>\n')


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def nameplate(t: str) -> str:
    """A neofetch readout: the heliosIT mark on the left, the facts on the right."""
    d, W = THEMES[t], 900
    left, info_x = 84, 350

    # The first line's cap-top sits PAD below the screen's top edge; the height
    # is derived from whatever ends up lowest so the bottom gets the same PAD.
    prompt_y = BEZEL + PAD + 9
    parts = [prompt(left, prompt_y, "neofetch", d)]

    # -- info block ------------------------------------------------------
    title_y = prompt_y + 35
    parts += [
        spans(info_x, title_y, [("jason", d["ink"], 700),
                                ("@", d["ink_dim"], None),
                                ("heliosit", d["ink"], 700)], size=15),
        f'<line x1="{info_x}" y1="{title_y + 12}" x2="{W - 84}" '
        f'y2="{title_y + 12}" stroke="{d["rule"]}" stroke-width="1"/>',
    ]
    y = title_y + 38
    for label, value in PLATE_ROWS:
        parts.append(spans(info_x, y, [
            # width must exceed the longest label + colon, or it runs together
            (f"{label + ':':<9}", d["ink_mid"], 700),
            (value, d["ink"], None),
        ], size=13.5))
        y += 24

    # -- intensity ramp, the swatch row neofetch signs off with ----------
    swatch_y = y + 20
    parts.append(text(info_x, y + 6, "# I build the systems small businesses "
                      "run on.", size=12, fill=d["ink_dim"]))
    for i in range(8):
        parts.append(
            f'<rect x="{info_x + i * 22}" y="{swatch_y}" width="16" height="9" '
            f'fill="{d["ink"]}" opacity="{round(0.18 + i * 0.117, 3)}"/>')

    # -- ASCII mark, centred against the info block as neofetch does -----
    art, art_step = sun(), 15
    info_mid = ((title_y - 11) + (swatch_y + 9)) / 2
    art_y = info_mid - (len(art) - 1) * art_step / 2
    for i, line in enumerate(art):
        parts.append(text(left, round(art_y + i * art_step, 1), line, size=13,
                          weight=600, fill=d["ink_mid"], preserve=True))

    H = ceil_even(max(art_y + (len(art) - 1) * art_step + 4,   # descender
                      swatch_y + 9))

    return svg(t, W, H, "\n  ".join(parts),
               "jason@heliosit",
               "A terminal readout in the style of neofetch. On the left, the "
               "heliosIT sun mark drawn in ASCII. On the right: jason@heliosit, "
               + "; ".join(f"{k.lower()}, {v}" for k, v in PLATE_ROWS)
               + ". I build the systems small businesses run on.")


def stack_card(t: str, rows, repo_count: int, total_bytes: int) -> str:
    d, W = THEMES[t], 900
    row_h = 27
    head_y = BEZEL + PAD + 9
    top = head_y + 35                       # first bar, clear of the rule
    H = ceil_even(top + row_h * (len(rows) - 1) + 13)

    label_x, bar_x, bar_w, val_x = 84, 226, 534, W - 84

    # LED bargraph: a 4px pitch with a 1px gap, aligned to the track origin.
    # At 0.75% of full scale per segment the quantisation is invisible, and
    # every row carries its exact value as a direct label regardless.
    parts = [
        f'''<defs><pattern id="seg" width="4" height="14"
        patternUnits="userSpaceOnUse" patternTransform="translate({bar_x},0)">
        <rect x="3" y="0" width="1" height="14" fill="{d["seg_gap"]}"/>
      </pattern></defs>''',
        prompt(label_x, head_y, "langs --all", d),
        text(val_x, head_y, f"{repo_count} repos · {total_bytes / 1e6:.1f} MB",
             font=MONO, size=11, weight=500, fill=d["ink_dim"], anchor="end"),
        f'<line x1="{label_x}" y1="{head_y + 13}" x2="{val_x}" '
        f'y2="{head_y + 13}" stroke="{d["rule"]}" stroke-width="1"/>',
    ]

    y = top
    for name, pct, is_tail in rows:
        fill = d["bar_dim"] if is_tail else d["bar"]
        w = max(bar_w * pct / 100, 2.5)
        parts += [
            text(label_x, y + 10, name, font=MONO, size=12.5, weight=500,
                 fill=d["ink"]),
            f'<rect x="{bar_x}" y="{y}" width="{bar_w}" height="13" rx="2" '
            f'fill="{d["track"]}"/>',
            f'<path d="{bar_path(bar_x, y, w, 13)}" fill="{fill}"/>',
            f'<rect x="{bar_x}" y="{y}" width="{bar_w}" height="13" '
            f'fill="url(#seg)"/>',
            text(val_x, y + 10, f"{pct:.1f}%", font=MONO, size=12.5,
                 weight=600, fill=d["ink"], anchor="end"),
        ]
        y += row_h

    summary = ", ".join(f"{n} {p:.1f} percent" for n, p, _ in rows)
    return svg(t, W, H, "\n  ".join(parts),
               "Language distribution across my repositories",
               f"Bar chart of language share across {repo_count} repositories, "
               f"public and private: {summary}.")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="re-render from assets/langs.json instead of the API")
    args = ap.parse_args()

    ASSETS.mkdir(exist_ok=True)
    cache = ASSETS / "langs.json"

    if args.offline:
        if not cache.exists():
            print(f"no cache at {cache}; run without --offline first",
                  file=sys.stderr)
            return 1
        data = json.loads(cache.read_text())
        totals, repo_count = data["languages"], data["repos"]
    else:
        totals, repo_count = fetch_language_bytes()
        cache.write_text(json.dumps(
            {"repos": repo_count, "languages": totals}, indent=2) + "\n")

    rows = to_rows(totals)
    total_bytes = sum(totals.values())

    for theme in THEMES:
        (ASSETS / f"nameplate-{theme}.svg").write_text(nameplate(theme))
        (ASSETS / f"stack-{theme}.svg").write_text(
            stack_card(theme, rows, repo_count, total_bytes))

    print(f"wrote 4 cards to {ASSETS} "
          f"({repo_count} repos, {total_bytes:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
