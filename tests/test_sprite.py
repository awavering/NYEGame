"""tools/make_sprite.py: recovering a tiny sprite from blown-up, blurry pixel art."""

import numpy as np
import pytest
from helpers import ROOT
from PIL import Image, ImageFilter

from tools.make_sprite import cut_out, detect_grid, flatten, main, process

PALETTE = np.array(
    [(200, 40, 40), (40, 120, 200), (60, 170, 70), (230, 190, 40), (120, 60, 160), (30, 30, 30)],
    np.uint8,
)
WHITE = (255, 255, 255)


def toy_canvas(w=12, h=16, margin=2, seed=1):
    """A random colourful sprite surrounded by white cells. Returns (canvas, sprite)."""
    sprite = PALETTE[np.random.default_rng(seed).integers(0, len(PALETTE), size=(h, w))]
    canvas = np.full((h + 2 * margin, w + 2 * margin, 3), 255, np.uint8)
    canvas[margin:-margin, margin:-margin] = sprite
    return canvas, sprite


def blow_up(canvas, block=20, shift=(7, 13), blur=2):
    """Enlarge like a downloaded picture: big blocks, odd offset, softened edges."""
    h, w = canvas.shape[:2]
    img = Image.fromarray(canvas).resize((w * block, h * block), Image.NEAREST)
    big = Image.new("RGB", (img.width + shift[0] + 9, img.height + shift[1] + 4), WHITE)
    big.paste(img, shift)
    return big.filter(ImageFilter.GaussianBlur(blur)) if blur else big


def test_recovers_the_exact_sprite_from_a_blurry_enlargement():
    canvas, sprite = toy_canvas()
    result, grid = process(blow_up(canvas))
    assert (grid["size"], grid["x0"], grid["y0"]) == (20, 7, 13)
    pixels = np.asarray(result)
    assert pixels.shape == (16, 12, 4)
    assert (pixels[..., :3] == sprite).all()
    assert (pixels[..., 3] == 255).all()


@pytest.mark.parametrize("block", [8, 12, 20, 33])
def test_finds_different_block_sizes(block):
    canvas, sprite = toy_canvas(w=10, h=14)
    _, grid = process(blow_up(canvas, block=block, shift=(3, 5), blur=1))
    assert grid["size"] == block


def test_an_explicit_block_size_gives_the_same_result():
    canvas, _ = toy_canvas()
    img = blow_up(canvas)
    assert np.array_equal(np.asarray(process(img)[0]), np.asarray(process(img, size=20)[0]))


def test_light_patches_inside_the_subject_are_kept_but_the_outside_is_removed():
    canvas, sprite = toy_canvas()
    canvas[2 + 6 : 2 + 8, 2 + 5 : 2 + 7] = 250  # a near-white chest patch, fully surrounded by colour
    result = np.asarray(process(blow_up(canvas))[0])
    assert result.shape[:2] == (16, 12)
    assert (result[6:8, 5:7, 3] == 255).all()  # the patch stays opaque
    assert result[6:8, 5:7, :3].min() >= 240


def test_a_shape_touching_the_edge_loses_its_white_background_but_keeps_its_shape():
    canvas, sprite = toy_canvas(margin=2)
    canvas[2:4, 2:6] = 255  # a white notch in the top-left corner of the sprite, open to the outside
    result = np.asarray(process(blow_up(canvas))[0])
    assert result.shape[:2] == (16, 12)
    assert (result[0:2, 0:4, 3] == 0).all()  # notch is now transparent
    assert (result[2:, :, 3] == 255).all()


def test_stray_specks_are_dropped_and_the_result_is_trimmed():
    canvas = np.full((24, 30, 3), 255, np.uint8)
    canvas[5:20, 4:16] = PALETTE[np.random.default_rng(3).integers(0, 6, size=(15, 12))]
    canvas[1, 27] = PALETTE[0]  # a lone speck far from the subject
    result, _ = process(blow_up(canvas))
    assert (result.height, result.width) == (15, 12)


def test_transparent_backgrounds_work_too():
    canvas, sprite = toy_canvas()
    rgba = np.zeros((*canvas.shape[:2], 4), np.uint8)
    rgba[2:-2, 2:-2, :3] = sprite
    rgba[2:-2, 2:-2, 3] = 255
    big = Image.fromarray(rgba, "RGBA").resize((rgba.shape[1] * 20, rgba.shape[0] * 20), Image.NEAREST)
    result, _ = process(big)
    assert np.array_equal(np.asarray(result)[..., :3], sprite)


def test_keep_background_only_recovers_the_grid():
    canvas, _ = toy_canvas()
    result, _ = process(blow_up(canvas), keep_background=True)
    assert result.size == (canvas.shape[1], canvas.shape[0])  # untrimmed: white margin still there


def test_flatten_treats_transparent_as_white():
    rgb, alpha = flatten(Image.new("RGBA", (2, 2), (10, 20, 30, 0)))
    assert (rgb == 255).all() and (alpha == 0).all()


def test_cutting_out_nothing_is_a_clear_error():
    blank = np.zeros((6, 6, 4), np.uint8)
    with pytest.raises(ValueError, match="Nothing left"):
        cut_out(blank)


def test_pixel_art_scores_a_clear_grid_and_a_smooth_picture_does_not():
    canvas, _ = toy_canvas()
    x = np.linspace(0, 255, 400)
    smooth = np.stack(np.broadcast_arrays(x[None, :], x[:, None], x[None, :] * 0 + 90), axis=2)
    assert detect_grid(flatten(blow_up(canvas))[0])[3] > 0.3
    assert detect_grid(smooth)[3] < 0.15


def test_heavy_blur_does_not_fool_the_detector():
    canvas, sprite = toy_canvas()
    result, grid = process(blow_up(canvas, blur=4))
    assert grid["size"] == 20  # a finer grid (5 or 10) would capture the smeared borders better
    # Colours near a border pick up a little of their neighbour when the source is this blurry.
    off_by = np.abs(np.asarray(result)[..., :3].astype(int) - sprite.astype(int)).max()
    assert off_by <= 15


def test_no_grid_at_all_is_a_clear_error():
    with pytest.raises(ValueError, match="pass --block"):
        detect_grid(np.zeros((3, 3, 3)))


def test_command_line(tmp_path, capsys):
    canvas, _ = toy_canvas()
    source, out, preview = tmp_path / "in.png", tmp_path / "out" / "sprite.png", tmp_path / "prev.png"
    blow_up(canvas).save(source)
    assert main([str(source), str(out), "--preview", str(preview)]) == 0
    assert Image.open(out).size == (12, 16)
    assert preview.exists()
    assert "big pixels are 20x20" in capsys.readouterr().out


# ------------------------------------------------------------ the sprite that ships with the game


def test_the_shipped_dog_is_a_small_transparent_sprite():
    dog = Image.open(ROOT / "pages" / "assets" / "dog.png")
    assert dog.mode == "RGBA"
    assert dog.width < 100 and dog.height < 100  # real pixel size; the browser enlarges it
    assert dog.getpixel((0, 0))[3] == 0  # corner is transparent
    assert np.asarray(dog)[..., 3].astype(bool).mean() > 0.3  # and there is a dog in it
