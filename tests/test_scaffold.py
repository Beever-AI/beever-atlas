"""Tests for project scaffold: package imports and module structure."""

import importlib
from pathlib import Path

import pytest

SUBMODULES = [
    "agents",
    "adapters",
    "services",
    "stores",
    "retrieval",
    "wiki",
    "server",
    "infra",
]


def test_beever_atlas_importable():
    import beever_atlas

    assert beever_atlas is not None


@pytest.mark.parametrize("submodule", SUBMODULES)
def test_submodule_importable(submodule: str):
    mod = importlib.import_module(f"beever_atlas.{submodule}")
    assert mod is not None


@pytest.mark.parametrize("submodule", SUBMODULES)
def test_submodule_directory_exists(submodule: str):
    src_root = Path(__file__).parent.parent / "src" / "beever_atlas"
    subdir = src_root / submodule
    assert subdir.is_dir(), f"{subdir} does not exist"
    assert (subdir / "__init__.py").is_file(), f"{subdir}/__init__.py missing"


def test_all_expected_submodules_present():
    src_root = Path(__file__).parent.parent / "src" / "beever_atlas"
    actual = {d.name for d in src_root.iterdir() if d.is_dir() and not d.name.startswith("_")}
    expected = set(SUBMODULES)
    assert expected.issubset(actual), f"Missing submodules: {expected - actual}"
