"""Face detection, embedding and matching.

Uses the InsightFace buffalo_sc models (SCRFD det_500m + ArcFace w600k_mbf)
directly through onnxruntime. The pre/post-processing mirrors the insightface
package (scrfd.py, arcface_onnx.py, face_align.py), which is not installed
because it needs a C++ compiler on Windows.

If the models cannot be loaded the error is logged and kept in `error`;
detection falls back to OpenCV's Haar cascade and identity is unavailable
(nobody matches, so nothing can be ALLOWed).
"""
import logging
import os
from typing import Optional

import cv2
import numpy as np

from . import config

log = logging.getLogger("spoof.identity")

ARCFACE_DST = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


def _umeyama(src, dst):
    """Least-squares similarity transform (2x3) mapping src points onto dst."""
    src, dst = src.astype(np.float64), dst.astype(np.float64)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    s, d = src - mu_s, dst - mu_d
    cov = d.T @ s / len(src)
    U, S, Vt = np.linalg.svd(cov)
    D = np.ones(2)
    if np.linalg.det(cov) < 0:
        D[1] = -1
    R = U @ np.diag(D) @ Vt
    scale = (S * D).sum() / s.var(0).sum()
    M = np.zeros((2, 3))
    M[:, :2] = scale * R
    M[:, 2] = mu_d - scale * R @ mu_s
    return M.astype(np.float32)


def align_face(img, kps, size=112):
    """ArcFace alignment: warp so the 5 keypoints land on the standard template."""
    M = _umeyama(np.asarray(kps, dtype=np.float32), ARCFACE_DST * (size / 112.0))
    return cv2.warpAffine(img, M, (size, size), borderValue=0.0)


def _nms(dets, thresh):
    x1, y1, x2, y2, scores = dets.T
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][iou <= thresh]
    return keep


class FaceEngine:
    """Detector + embedder. One instance is shared by gate, liveness and identity."""

    DET_SIZE = 640
    DET_THRESH = 0.5
    NMS_THRESH = 0.4
    STRIDES = (8, 16, 32)
    NUM_ANCHORS = 2

    def __init__(self, models_dir=None):
        import onnxruntime as ort
        self.error = None
        self.det = self.rec = self.haar = None
        d = (models_dir or config.MODELS_DIR) / "buffalo_sc"
        try:
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            self.det = ort.InferenceSession(str(d / "det_500m.onnx"), opts,
                                            providers=["CPUExecutionProvider"])
            self.rec = ort.InferenceSession(str(d / "w600k_mbf.onnx"), opts,
                                            providers=["CPUExecutionProvider"])
            self.det_input = self.det.get_inputs()[0].name
            self.rec_input = self.rec.get_inputs()[0].name
            n_out = len(self.det.get_outputs())
            if n_out != 9:
                raise RuntimeError(f"det_500m.onnx has {n_out} outputs, expected 9")
        except Exception as e:  # noqa: BLE001 - reported, then labelled fallback
            self.error = f"{type(e).__name__}: {e}"
            self.det = self.rec = None
            log.error("buffalo_sc failed to load (%s). Falling back to Haar-cascade "
                      "detection; identity matching is DISABLED. Run "
                      "`python tools/fetch_models.py`.", self.error)
            self.haar = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    @property
    def identity_available(self):
        return self.rec is not None

    # ---- detection -----------------------------------------------------
    def detect(self, img):
        """Returns faces sorted largest first: {bbox:[x1,y1,x2,y2], kps:5x2|None, score}."""
        if self.det is None:
            return self._detect_haar(img)
        h, w = img.shape[:2]
        size = self.DET_SIZE
        scale = min(size / w, size / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        canvas = np.zeros((size, size, 3), dtype=np.uint8)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128, (size, size),
                                     (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(None, {self.det_input: blob})
        boxes, kpss, scores = [], [], []
        for i, stride in enumerate(self.STRIDES):
            sc = outs[i].reshape(-1)
            bb = outs[i + 3].reshape(-1, 4) * stride
            kp = outs[i + 6].reshape(-1, 10) * stride
            n = size // stride
            ys, xs = np.mgrid[:n, :n]
            centers = np.stack([xs, ys], -1).astype(np.float32).reshape(-1, 2) * stride
            centers = np.repeat(centers, self.NUM_ANCHORS, axis=0)
            pos = np.where(sc >= self.DET_THRESH)[0]
            if pos.size == 0:
                continue
            c, b, k = centers[pos], bb[pos], kp[pos]
            boxes.append(np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1],
                                   c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], -1))
            kpss.append((k.reshape(-1, 5, 2) + c[:, None, :]))
            scores.append(sc[pos])
        if not boxes:
            return []
        boxes = np.vstack(boxes) / scale
        kpss = np.vstack(kpss) / scale
        scores = np.concatenate(scores)
        keep = _nms(np.hstack([boxes, scores[:, None]]).astype(np.float32), self.NMS_THRESH)
        faces = [{"bbox": boxes[i].tolist(), "kps": kpss[i], "score": float(scores[i])}
                 for i in keep]
        faces.sort(key=lambda f: -(f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]))
        return faces

    def _detect_haar(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
        faces = [{"bbox": [float(x), float(y), float(x + w), float(y + h)],
                  "kps": None, "score": 1.0} for (x, y, w, h) in found]
        faces.sort(key=lambda f: -(f["bbox"][2] - f["bbox"][0]))
        return faces

    # ---- embedding -----------------------------------------------------
    def embed(self, img, face):
        """512-dim L2-normalised ArcFace embedding, or None if unavailable."""
        if self.rec is None or face.get("kps") is None:
            return None
        aligned = align_face(img, face["kps"])
        blob = cv2.dnn.blobFromImage(aligned, 1.0 / 127.5, (112, 112),
                                     (127.5, 127.5, 127.5), swapRB=True)
        emb = self.rec.run(None, {self.rec_input: blob})[0][0]
        return emb / (np.linalg.norm(emb) + 1e-9)


def mean_embedding(embeddings):
    embeddings = [e for e in embeddings if e is not None]
    if not embeddings:
        return None
    m = np.mean(embeddings, axis=0)
    return m / (np.linalg.norm(m) + 1e-9)


def scale_similarity(cosine):
    """Map cosine similarity to the 0-100 scale used by the decision policy.

    Piecewise linear through config.COSINE_ANCHORS -> SIMILARITY_ANCHORS
    (0.30 -> 80, 0.50 -> 95, 1.0 -> 100). Raw cosine is always logged too.
    """
    return float(np.interp(max(float(cosine), 0.0),
                           config.COSINE_ANCHORS, config.SIMILARITY_ANCHORS))


def match(embedding, users, user_id=None):
    """Compare against enrolled users (or only `user_id` for a 1:1 claim).

    Returns (user | None, similarity 0-100, cosine). The best score is always
    returned; `user` is None when it is below MATCH_MIN_SIMILARITY.
    """
    if embedding is None:
        return None, 0.0, 0.0
    if user_id is not None:
        users = [u for u in users if u["id"] == user_id]
    best, best_cos = None, -1.0
    for u in users:
        cos = float(np.dot(embedding, np.asarray(u["embedding"], dtype=np.float32)))
        if cos > best_cos:
            best, best_cos = u, cos
    if best is None:
        return None, 0.0, 0.0
    sim = scale_similarity(best_cos)
    return (best if sim >= config.MATCH_MIN_SIMILARITY else None), sim, best_cos


class RekognitionIdentity:
    """AWS Rekognition face identification provider (MODE=aws)."""

    def __init__(self, collection_id: Optional[str] = None, region: Optional[str] = None):
        self.collection_id = collection_id or os.environ.get("REKOGNITION_COLLECTION_ID", "spot-a-spoof-faces")
        self.region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._client = None
        self.identity_available = False
        self.error = None
        self._init_client()

    def _init_client(self):
        try:
            import boto3
            self._client = boto3.client("rekognition", region_name=self.region)
            self.identity_available = True
        except Exception as e:
            self.identity_available = False
            self.error = str(e)

    def create_collection(self) -> dict:
        if not self._client:
            self._init_client()
        if not self._client:
            return {"StatusCode": 500, "error": self.error}
        try:
            return self._client.create_collection(CollectionId=self.collection_id)
        except Exception as e:
            if "ResourceAlreadyExistsException" in type(e).__name__:
                return {"StatusCode": 200, "message": "Collection already exists"}
            logger.warning(f"Failed to create Rekognition collection: {e}")
            return {"StatusCode": 500, "error": str(e)}

    def index_faces(self, user_id: str, image_bytes: bytes) -> dict:
        if not self._client:
            self._init_client()
        if not self._client:
            raise RuntimeError(f"Rekognition client unavailable: {self.error}")
        return self._client.index_faces(
            CollectionId=self.collection_id,
            Image={"Bytes": image_bytes},
            ExternalImageId=user_id,
            DetectionAttributes=["DEFAULT"],
            MaxFaces=1,
        )

    def search_faces_by_image(self, image_bytes: bytes, max_faces: int = 1, threshold: float = 80.0):
        if not self._client:
            self._init_client()
        if not self._client:
            return None, 0.0, 0.0
        try:
            res = self._client.search_faces_by_image(
                CollectionId=self.collection_id,
                Image={"Bytes": image_bytes},
                MaxFaces=max_faces,
                FaceMatchThreshold=threshold,
            )
            matches = res.get("FaceMatches", [])
            if not matches:
                return None, 0.0, 0.0
            best = matches[0]
            matched_id = best.get("Face", {}).get("ExternalImageId")
            similarity = float(best.get("Similarity", 0.0))
            confidence = float(best.get("Face", {}).get("Confidence", 0.0))
            return matched_id, similarity, confidence
        except Exception as e:
            logger.warning(f"Rekognition search_faces_by_image failed: {e}")
            return None, 0.0, 0.0
