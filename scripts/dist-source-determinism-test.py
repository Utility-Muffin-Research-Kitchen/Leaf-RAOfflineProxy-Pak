#!/usr/bin/env python3
"""Re-encoded upstream archives must produce identical source distributions."""
import gzip
import hashlib
import importlib.util
import io
import json
import pathlib
import tarfile
import tempfile

spec = importlib.util.spec_from_file_location(
    "dist_source", pathlib.Path(__file__).with_name("dist-source.py"))
dist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dist)


def encode(path, variant, changed=False):
    raw = io.BytesIO()
    files = [("LICENSE", b"source license", 0o644),
             ("linux/raofflineproxy/module.py", b"changed" if changed else b"print('fixture')", 0o644),
             ("tools/build.sh", b"#!/bin/sh\n", 0o755)]
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data, mode in files[::1 if variant == 0 else -1]:
            entry = tarfile.TarInfo(f"commit-export-{variant}/{name}")
            entry.size, entry.mode = len(data), mode
            entry.uid = entry.gid = variant + 100
            entry.uname = entry.gname = f"owner-{variant}"
            entry.mtime = variant + 1000
            archive.addfile(entry, io.BytesIO(data))
    with path.open("wb") as output:
        with gzip.GzipFile(fileobj=output, mode="wb", filename=f"download-{variant}",
                           mtime=variant + 2000) as archive:
            archive.write(raw.getvalue())


with tempfile.TemporaryDirectory() as tmp:
    dist.ROOT = root = pathlib.Path(tmp)
    (root / "locks").mkdir()
    (root / "workdir/sources").mkdir(parents=True)
    path = root / "workdir/sources/upstream.tar.gz"
    encode(path, 0)
    scope = "linux/raofflineproxy"
    content = dist.content_sha256(path, scope)
    (root / "locks/runtime.lock.json").write_text(json.dumps(
        {"source_date_epoch": 1234, "source_inputs": []}))
    (root / "locks/upstream.lock.json").write_text(json.dumps({
        "commit": "fixture", "archive": {"filename": path.name, "content_scope": scope,
        "content_sha256": content, "url": "https://example.invalid/fixture"}}))
    receipts, outputs = [], []
    for variant in range(2):
        encode(path, variant)
        receipts.append(dist.sha256_file(path))
        writer = dist.Writer(1234)
        inputs = []
        for item, data in dist.locked_inputs():
            assert item["sha256"] == hashlib.sha256(data).hexdigest()
            inputs.append(item)
            writer.add("sources/" + item["filename"], data, False)
            with tarfile.open(fileobj=io.BytesIO(data)) as normalized:
                assert normalized.extractfile("upstream/LICENSE").read() == b"source license"
                assert normalized.getmember("upstream/tools/build.sh").mode == 0o755
        writer.add("corresponding-source.json", json.dumps(inputs).encode(), False)
        outputs.append(writer.bytes())
    assert receipts[0] != receipts[1], "fixture must change the downloaded bytes"
    assert outputs[0] == outputs[1], "source distribution must ignore transport metadata"
    encode(path, 2, changed=True)
    try:
        dist.locked_inputs()
    except SystemExit as error:
        assert "locked upstream content" in str(error)
    else:
        raise AssertionError("changed upstream content was accepted")

print("dist-source-determinism-test: normalized source and manifest match; changed content refused")
