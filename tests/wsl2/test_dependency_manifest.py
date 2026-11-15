import hashlib

import pytest

from coding4game_gui.wsl2.dependency_manifest import (
    ArtifactCache, lock_id, normalize_environment, validate_lock,
)


HASH = "a" * 64


def sample_lock():
    return {
        "format": "c4g-wsl-env-lock-v1", "baseline_sha256": "b" * 64,
        "python": "3.13.5", "platform": "linux-x86_64",
        "requirements": ["Demo_Pkg[OCR]==1.2.3"],
        "indexes": ["https://pypi.org/simple"],
        "wheels": [{"filename": "demo_pkg-1.2.3-py3-none-any.whl",
                    "name": "Demo_Pkg", "version": "1.2.3", "sha256": HASH,
                    "bytes": 12}],
        "assets": [{"path": "models/ocr.bin", "url": "https://example.com/ocr.bin",
                    "sha256": "c" * 64, "bytes": 20}],
    }


def test_environment_normalizes_and_defaults_index():
    value = normalize_environment({
        "requirements": ["Demo_Pkg[OCR,fast_mode]==1.2.3+cpu"],
        "assets": [{"path": "models/ocr.bin", "url": "https://example.com/ocr.bin",
                    "sha256": HASH}],
    })
    assert value["requirements"] == ["demo-pkg[fast-mode,ocr]==1.2.3+cpu"]
    assert value["indexes"] == ["https://pypi.org/simple"]


def test_environment_accepts_absolute_guest_asset_source():
    value = normalize_environment({"assets": [{"path": "models/ocr.bin",
        "source_path": "/workspace/.EasyOCR/model/ocr.bin", "sha256": HASH}]})
    assert value["assets"][0]["source_path"] == "/workspace/.EasyOCR/model/ocr.bin"


@pytest.mark.parametrize("asset", [
    {"path": "a", "sha256": HASH},
    {"path": "a", "url": "https://example.com/a", "source_path": "/workspace/a",
     "sha256": HASH},
    {"path": "a", "source_path": "relative/a", "sha256": HASH},
    {"path": "a", "source_path": "/workspace/../secret", "sha256": HASH},
])
def test_environment_requires_one_safe_asset_source(asset):
    with pytest.raises(ValueError):
        normalize_environment({"assets": [asset]})


@pytest.mark.parametrize("requirement", [
    "demo", "demo>=1", "demo==1; python_version>'3'", "demo @ https://example.com/x.whl",
    "-r other.txt", "../demo==1", "demo==1 --hash=x",
])
def test_environment_rejects_nonexact_or_active_requirements(requirement):
    with pytest.raises(ValueError):
        normalize_environment({"requirements": [requirement]})


@pytest.mark.parametrize("url", [
    "http://example.com/simple", "https://user@example.com/simple",
    "https://example.com/simple?token=x", "https://localhost/simple",
    "https://127.0.0.1/simple", "https://127.1/simple", "https://10.0.0.1/simple",
    "https://example.com\\@evil.example/simple",
])
def test_environment_rejects_nonpublic_or_credential_capable_urls(url):
    with pytest.raises(ValueError):
        normalize_environment({"indexes": [url]})


@pytest.mark.parametrize("path", ["/model.bin", "../model.bin", "a/../model.bin", "a\\b"])
def test_environment_rejects_unsafe_asset_paths(path):
    with pytest.raises(ValueError):
        normalize_environment({"assets": [{"path": path, "url": "https://example.com/a",
                                            "sha256": HASH}]})


def test_environment_rejects_duplicate_and_parent_child_paths():
    for paths in (["a", "A"], ["models", "models/a.bin"]):
        with pytest.raises(ValueError, match="colliding"):
            normalize_environment({"assets": [
                {"path": path, "url": "https://example.com/a", "sha256": HASH}
                for path in paths]})


def test_lock_is_strict_normalized_and_has_stable_identity():
    value = validate_lock(sample_lock(), baseline_sha256="b" * 64)
    assert value["requirements"] == ["demo-pkg[ocr]==1.2.3"]
    assert value["wheels"][0]["name"] == "demo-pkg"
    assert lock_id(sample_lock()) == lock_id(value)
    assert len(lock_id(value)) == 64


def test_lock_accepts_local_captured_asset_without_upstream_url():
    lock = sample_lock(); lock["assets"][0]["url"] = None
    assert validate_lock(lock)["assets"][0]["url"] is None


def test_lock_accepts_assets_without_python_requirements_or_wheels():
    lock = sample_lock(); lock["requirements"] = []; lock["wheels"] = []
    assert validate_lock(lock)["wheels"] == []


def test_lock_rejects_unknown_fields_duplicates_bad_sizes_and_baseline():
    lock = sample_lock(); lock["extra"] = True
    with pytest.raises(ValueError): validate_lock(lock)
    lock = sample_lock(); lock["wheels"] *= 2
    with pytest.raises(ValueError): validate_lock(lock)
    lock = sample_lock(); lock["assets"][0]["bytes"] = 0
    with pytest.raises(ValueError): validate_lock(lock)
    with pytest.raises(ValueError, match="baseline"): validate_lock(sample_lock(), "d" * 64)
    lock = sample_lock(); lock["wheels"][0]["version"] = "1.2.4"
    with pytest.raises(ValueError, match="requested version"): validate_lock(lock)


def test_artifact_cache_streams_verifies_reuses_and_detects_corruption(tmp_path):
    data = b"streamed artifact"
    digest = hashlib.sha256(data).hexdigest()
    cache = ArtifactCache(tmp_path / "cache")
    assert not cache.contains(digest, len(data))
    path = cache.put(digest, len(data), [data[:4], data[4:]])
    assert path == cache.path(digest) and cache.contains(digest, len(data))
    assert cache.put(digest, len(data), (_ for _ in () if False)) == path
    path.write_bytes(b"x" * len(data))
    with pytest.raises(ValueError, match="hash"): cache.contains(digest, len(data))


def test_artifact_cache_removes_partial_temp_on_failure(tmp_path):
    cache = ArtifactCache(tmp_path / "cache")
    with pytest.raises(ValueError, match="size"):
        cache.put(HASH, 10, [b"short"])
    assert list((tmp_path / "cache" / "blobs").glob("*.tmp")) == []
    with pytest.raises(ValueError, match="chunks"):
        cache.put(HASH, 1, [bytearray(b"x")])
