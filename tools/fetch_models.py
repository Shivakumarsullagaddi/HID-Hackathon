"""Download the InsightFace buffalo_sc model pack into models/buffalo_sc and
verify every model file against its recorded SHA256.

    python tools/fetch_models.py            # models only
    python tools/fetch_models.py --samples  # also public sample photos for the tests

Run once while online. After that the app works offline (apart from the two
CDN scripts the pages load: MediaPipe Face Detection and Chart.js).
"""
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_sc.zip"

EXPECTED = {
    "models/minifasnet_v2.onnx": "7503fa5ce1494f152a15ca276b23b03b9e21f3a3e4d1f247bcc4c2c0ce4e1db8",
    "models/minifasnet_v1se.onnx": "a9230a34c7df3de8dceaa518a2da33511a6f5679c20bb63f801dca2ce43741fd",
    "models/buffalo_sc/det_500m.onnx": "5e4447f50245bbd7966bd6c0fa52938c61474a04ec7def48753668a9d8b4ea3a",
    "models/buffalo_sc/w600k_mbf.onnx": "9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
}


# Public sample photos used only by tests/test_pipeline.py (not committed).
# image_T1 is upstream's known-real example, image_F1/F2 its known-fake examples.
_FAS = ("https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/"
        "b6d5f04ad78778917853b25c778acef6d5626d15/images/sample/")
SAMPLES = {
    "image_T1.jpg": _FAS + "image_T1.jpg",
    "image_F1.jpg": _FAS + "image_F1.jpg",
    "image_F2.jpg": _FAS + "image_F2.jpg",
    "group.jpg": ("https://raw.githubusercontent.com/deepinsight/insightface/master/"
                  "python-package/insightface/data/images/t1.jpg"),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    dest = ROOT / "models" / "buffalo_sc"
    if not all((dest / n).exists() for n in ("det_500m.onnx", "w600k_mbf.onnx")):
        print("downloading", URL)
        with urllib.request.urlopen(URL, timeout=120) as r:
            blob = r.read()
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for name in ("det_500m.onnx", "w600k_mbf.onnx"):
                (dest / name).write_bytes(z.read(name))
    if "--samples" in sys.argv:
        fixtures = ROOT / "tests" / "fixtures"
        fixtures.mkdir(parents=True, exist_ok=True)
        for name, url in SAMPLES.items():
            if not (fixtures / name).exists():
                print("downloading", name)
                with urllib.request.urlopen(url, timeout=60) as r:
                    (fixtures / name).write_bytes(r.read())
    ok = True
    for rel, want in EXPECTED.items():
        path = ROOT / rel
        if not path.exists():
            print("MISSING ", rel)
            ok = False
            continue
        got = sha256(path)
        print("ok      " if got == want else "MISMATCH", rel, got)
        ok = ok and got == want
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
