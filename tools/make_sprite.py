"""Turn a pixel-art picture into a clean, transparent, native-size sprite.

    python tools/make_sprite.py downloaded-dog.png pages/assets/dog.png
    python tools/make_sprite.py in.png out.png --preview preview.png

Pixel art found online is usually a tiny picture blown up (each "big pixel"
is, say, 20x20 real pixels) with soft edges from resizing. This tool:

  1. finds the size and position of the big-pixel grid,
  2. reads one colour per cell, giving the real, tiny sprite,
  3. makes the background transparent (transparent or near-white areas that
     connect to the edge of the picture), and drops stray specks,
  4. trims the empty border.

Save the result at its tiny native size. The game scales it up in the
browser with `image-rendering: pixelated`, so the pixels stay sharp at any
size. Needs numpy (installed by requirements-dev.txt).
"""

import argparse
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image


def flatten(img):
    """Return (rgb floats over white, alpha floats) for any PIL image."""
    rgba = np.asarray(img.convert("RGBA")).astype(float)
    alpha = rgba[..., 3]
    rgb = rgba[..., :3] * (alpha[..., None] / 255) + 255 * (1 - alpha[..., None] / 255)
    return rgb, alpha


def _autocorrelation(profile):
    """How strongly a 1-D signal repeats itself at each shift (1.0 at shift 0)."""
    x = profile - profile.mean()
    n = len(x)
    spectrum = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(spectrum * np.conj(spectrum))[:n] / np.arange(n, 0, -1)  # fewer samples at long shifts
    return ac / (ac[0] + 1e-12)


def _find_size(jumps_x, jumps_y, smallest, largest):
    """The spacing at which the pattern of colour jumps repeats, and how clear it is (0 to 1)."""
    n = min(len(jumps_x), len(jumps_y))
    ac = (_autocorrelation(jumps_x)[:n] + _autocorrelation(jumps_y)[:n]) / 2
    peaks = [
        lag
        for lag in range(smallest, min(largest, n // 4) + 1)
        if ac[lag] > 0 and ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1]
    ]
    if not peaks:
        raise ValueError(
            "Could not find a pixel grid in this picture. If you know the size of one big pixel, pass --block N."
        )
    best = max(ac[lag] for lag in peaks)
    # Multiples of the true size repeat just as well (2x, 3x...), so take the smallest strong peak.
    size = min(lag for lag in peaks if ac[lag] >= 0.6 * best)
    return size, float(ac[size])


def _grid_start(jumps, size):
    """Where cells begin along one axis: the line spacing with the most colour change on it."""
    smooth = jumps + np.roll(jumps, 1) + np.roll(jumps, -1)  # blurry borders are a few pixels wide
    best = max(range(size), key=lambda off: smooth[off::size].sum())
    return (best + 1) % size  # a jump at index i is the border between pixel i and i+1


def detect_grid(rgb, size=None, smallest=4, largest=80):
    """Find the big-pixel size and where the grid starts.

    Colour only changes sharply on cell borders, so the sum of colour jumps
    along each axis is a comb: a spike every `size` pixels. The distance
    between spikes is where that signal repeats, found with an autocorrelation
    (which, unlike counting jumps per spacing, is not fooled by blur or by
    divisors of the real size). Pass `size` to skip the search.

    Returns (size, x0, y0, confidence). x0 and y0 are where the first full cell
    starts. Confidence is 0 to 1: real pixel art scores about 0.3 or more, a
    smooth picture close to 0.
    """
    jumps_x = np.abs(np.diff(rgb, axis=1)).sum(axis=(0, 2))  # one value per vertical border
    jumps_y = np.abs(np.diff(rgb, axis=0)).sum(axis=(1, 2))
    confidence = 1.0
    if size is None:
        size, confidence = _find_size(jumps_x, jumps_y, smallest, largest)
    return size, _grid_start(jumps_x, size), _grid_start(jumps_y, size), confidence


def sample_cells(rgb, alpha, size, x0, y0):
    """One RGBA colour per cell: the median of its middle, away from blurry borders."""
    cols, rows = (rgb.shape[1] - x0) // size, (rgb.shape[0] - y0) // size
    margin = max(1, size // 4)
    out = np.zeros((rows, cols, 4), np.uint8)
    for r in range(rows):
        ys = slice(y0 + r * size + margin, y0 + (r + 1) * size - margin)
        for c in range(cols):
            xs = slice(x0 + c * size + margin, x0 + (c + 1) * size - margin)
            out[r, c, :3] = np.median(rgb[ys, xs].reshape(-1, 3), axis=0)
            out[r, c, 3] = 255 if np.median(alpha[ys, xs]) >= 128 else 0
    return out


def background_mask(cells, tolerance):
    """True for cells that are empty or near-white (a pure white/grey backdrop)."""
    rgb = cells[..., :3].astype(int)
    lightest = rgb.min(axis=2) >= 255 - tolerance
    greyish = (rgb.max(axis=2) - rgb.min(axis=2)) <= tolerance
    return (cells[..., 3] < 128) | (lightest & greyish)


def _flood(allowed, starts, neighbours):
    seen = np.zeros_like(allowed)
    queue = deque(s for s in starts if allowed[s])
    for s in queue:
        seen[s] = True
    rows, cols = allowed.shape
    while queue:
        r, c = queue.popleft()
        for dr, dc in neighbours:
            rr, cc = r + dr, c + dc
            if 0 <= rr < rows and 0 <= cc < cols and allowed[rr, cc] and not seen[rr, cc]:
                seen[rr, cc] = True
                queue.append((rr, cc))
    return seen


FOUR = [(-1, 0), (1, 0), (0, -1), (0, 1)]
EIGHT = FOUR + [(-1, -1), (-1, 1), (1, -1), (1, 1)]


def cut_out(cells, tolerance=30):
    """Make the outside background transparent, keep only the biggest shape, trim."""
    rows, cols = cells.shape[:2]
    border = [(r, c) for r in range(rows) for c in (0, cols - 1)] + [
        (r, c) for c in range(cols) for r in (0, rows - 1)
    ]
    # Only background that connects to the picture's edge is removed, so light
    # patches inside the subject (a white chest, a highlight) are kept.
    outside = _flood(background_mask(cells, tolerance), border, FOUR)
    subject = ~outside

    # keep the largest connected shape, dropping stray specks and shadow fragments
    best, remaining = np.zeros_like(subject), subject.copy()
    while remaining.any():
        start = tuple(np.argwhere(remaining)[0])
        blob = _flood(remaining, [start], EIGHT)
        remaining &= ~blob
        if blob.sum() > best.sum():
            best = blob
    if not best.any():
        raise ValueError(
            "Nothing left after removing the background. Try --tolerance 10 or --keep-background."
        )

    out = cells.copy()
    out[~best] = (0, 0, 0, 0)
    ys, xs = np.where(best)
    return out[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]


def process(img, size=None, tolerance=30, keep_background=False):
    """PIL image in, (native-size RGBA PIL image, grid info) out."""
    rgb, alpha = flatten(img)
    if size:
        # an explicit size still needs to find where the grid starts
        size, x0, y0, confidence = detect_grid(rgb, size)
    else:
        size, x0, y0, confidence = detect_grid(rgb)
    cells = sample_cells(rgb, alpha, size, x0, y0)
    if not keep_background:
        cells = cut_out(cells, tolerance)
    return Image.fromarray(cells, "RGBA"), {"size": size, "x0": x0, "y0": y0, "confidence": confidence}


def preview(sprite, scale=8):
    """The sprite enlarged on a checkerboard, to judge the cut-out by eye."""
    big = sprite.resize((sprite.width * scale, sprite.height * scale), Image.NEAREST)
    board = Image.new("RGBA", big.size, (255, 255, 255, 255))
    tile = 16
    for y in range(0, big.height, tile):
        for x in range(0, big.width, tile):
            if (x // tile + y // tile) % 2:
                board.paste((225, 225, 225, 255), (x, y, x + tile, y + tile))
    board.alpha_composite(big)
    return board


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--block", type=int, help="size of one big pixel (default: detect it)")
    parser.add_argument(
        "--tolerance", type=int, default=30, help="how close to white counts as background (default 30)"
    )
    parser.add_argument(
        "--keep-background", action="store_true", help="only recover the grid; leave the background"
    )
    parser.add_argument("--preview", type=Path, help="also save an enlarged preview on a checkerboard")
    args = parser.parse_args(argv)

    sprite, grid = process(Image.open(args.source), args.block, args.tolerance, args.keep_background)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sprite.save(args.output)
    print(f"big pixels are {grid['size']}x{grid['size']} (grid starts at {grid['x0']},{grid['y0']})")
    if grid["confidence"] < 0.15 and not args.block:
        print(
            "warning: this picture does not look like blocky pixel art. If the size above is wrong, pass --block N."
        )
    print(f"sprite is {sprite.width}x{sprite.height} pixels -> {args.output}")
    if args.preview:
        preview(sprite).save(args.preview)
        print(f"preview -> {args.preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
