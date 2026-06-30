"""Unit tests for the incubation-stage estimator — pure mapping + contract.

These exercise the heuristic feature→day mapping, stage boundaries, and the
predict_stage return contract. The mock + heuristic backends require no
hardware and no GPU, so they keep CI green.
"""

import io

import pytest

from app.services.vision_service import (
    STAGES,
    VisionService,
    extract_candle_features,
    heuristic_day_from_features,
    stage_from_day,
    stage_to_day_range,
)


def _assert_contract(result, days=21):
    assert set(result) >= {"ok", "backend", "day_estimate", "day_range", "stage", "confidence", "features", "path"}
    assert isinstance(result["day_estimate"], float)
    lo, hi = result["day_range"]
    assert 0.0 <= lo <= result["day_estimate"] <= hi <= float(days)
    assert result["stage"] in STAGES
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["features"], dict)


# --------------------------------------------------------------------------
# Pure mapping
# --------------------------------------------------------------------------

def test_day_increases_monotonically_with_opaque_mass():
    base = {"air_cell_fraction": 0.2, "vein_density": 0.3}
    low = heuristic_day_from_features({**base, "opaque_fraction": 0.2})
    high = heuristic_day_from_features({**base, "opaque_fraction": 0.6})
    assert high["day_estimate"] > low["day_estimate"]


def test_more_air_cell_advances_estimate():
    base = {"opaque_fraction": 0.4, "vein_density": 0.3}
    small = heuristic_day_from_features({**base, "air_cell_fraction": 0.1})
    big = heuristic_day_from_features({**base, "air_cell_fraction": 0.6})
    assert big["day_estimate"] >= small["day_estimate"]


def test_blank_image_is_unclear():
    out = heuristic_day_from_features({"opaque_fraction": 0.0, "air_cell_fraction": 0.0, "vein_density": 0.0})
    assert out["stage"] == "unclear"
    assert out["day_estimate"] == 0.0


def test_clear_translucent_egg_is_infertile():
    out = heuristic_day_from_features({"opaque_fraction": 0.03, "air_cell_fraction": 0.05, "vein_density": 0.02, "brightness_mean": 0.7})
    assert out["stage"] == "infertile"
    assert out["day_estimate"] == 0.0


def test_stages_progress_early_to_hatching():
    early = heuristic_day_from_features({"opaque_fraction": 0.20, "air_cell_fraction": 0.10, "vein_density": 0.4})
    mid = heuristic_day_from_features({"opaque_fraction": 0.55, "air_cell_fraction": 0.30, "vein_density": 0.5})
    late = heuristic_day_from_features({"opaque_fraction": 0.92, "air_cell_fraction": 0.55, "vein_density": 0.2})
    hatch = heuristic_day_from_features({"opaque_fraction": 0.99, "air_cell_fraction": 0.95, "vein_density": 0.1})
    assert early["stage"] == "early"
    assert mid["stage"] == "mid"
    assert late["stage"] in ("late", "hatching")
    assert hatch["stage"] == "hatching"
    assert early["day_estimate"] < mid["day_estimate"] < late["day_estimate"] <= hatch["day_estimate"]


def test_day_range_brackets_estimate_and_respects_window():
    out = heuristic_day_from_features({"opaque_fraction": 0.5, "air_cell_fraction": 0.3, "vein_density": 0.4}, incubation_days=21)
    lo, hi = out["day_range"]
    assert 0.0 <= lo <= out["day_estimate"] <= hi <= 21.0


def test_incubation_days_scales_estimate():
    feats = {"opaque_fraction": 0.6, "air_cell_fraction": 0.4, "vein_density": 0.3}
    chicken = heuristic_day_from_features(feats, incubation_days=21)
    duck = heuristic_day_from_features(feats, incubation_days=28)
    assert duck["day_estimate"] > chicken["day_estimate"]


def test_stage_from_day_boundaries():
    assert stage_from_day(0, 21) == "early"
    assert stage_from_day(5, 21) == "early"
    assert stage_from_day(10, 21) == "mid"
    assert stage_from_day(16, 21) == "late"
    assert stage_from_day(20, 21) == "hatching"


def test_stage_to_day_range_contract():
    day, (lo, hi) = stage_to_day_range("mid", 21)
    assert 0 <= lo <= day <= hi <= 21
    inf_day, (ilo, ihi) = stage_to_day_range("infertile", 21)
    assert inf_day == 0.0 and ilo == 0.0


# --------------------------------------------------------------------------
# predict_stage contract — no hardware
# --------------------------------------------------------------------------

def test_mock_backend_needs_no_file_and_returns_contract():
    svc = VisionService(stage_backend="mock", incubation_days=21)
    result = svc.predict_stage("/does/not/exist.jpg")
    assert result["ok"] is True
    assert result["backend"] == "mock"
    _assert_contract(result)


def test_heuristic_backend_on_real_image(tmp_path):
    from PIL import Image, ImageDraw

    # A candled-egg-like image: warm oval with a darker interior mass.
    img = Image.new("RGB", (320, 240), (20, 14, 8))
    draw = ImageDraw.Draw(img)
    draw.ellipse([90, 40, 230, 200], fill=(220, 180, 120))
    draw.ellipse([140, 110, 200, 175], fill=(110, 70, 40))
    path = tmp_path / "egg.jpg"
    img.save(path, format="JPEG")

    svc = VisionService(stage_backend="heuristic", incubation_days=21)
    result = svc.predict_stage(str(path))
    assert result["ok"] is True
    assert result["backend"] == "heuristic"
    _assert_contract(result)
    # Features are present and normalised.
    for key in ("opaque_fraction", "air_cell_fraction", "vein_density", "brightness_mean"):
        assert 0.0 <= result["features"][key] <= 1.0


def test_heuristic_unreadable_image_is_graceful():
    svc = VisionService(stage_backend="heuristic")
    result = svc.predict_stage("/no/such/image.jpg")
    assert result["ok"] is False
    assert result["stage"] == "unclear"
    assert result["backend"] == "heuristic"


def test_extract_features_returns_normalised_dict(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (128, 128), (180, 140, 90))
    path = tmp_path / "flat.jpg"
    img.save(path, format="JPEG")
    feats = extract_candle_features(str(path))
    assert set(feats) == {"opaque_fraction", "air_cell_fraction", "vein_density", "brightness_mean"}
    assert all(0.0 <= v <= 1.0 for v in feats.values())
