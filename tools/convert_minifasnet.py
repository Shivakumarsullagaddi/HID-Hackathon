"""Convert the upstream MiniFASNet .pth weights to ONNX.

Run this in a separate environment that has torch and onnx (the app itself
only needs onnxruntime):

    git clone https://github.com/minivision-ai/Silent-Face-Anti-Spoofing upstream
    git -C upstream checkout b6d5f04ad78778917853b25c778acef6d5626d15
    pip install torch onnx
    python tools/convert_minifasnet.py upstream models

The exported graphs output raw logits for 3 classes; softmax is applied in
backend/liveness.py, as upstream does in src/anti_spoof_predict.py.
"""
import sys
from collections import OrderedDict
from pathlib import Path

import torch

JOBS = [
    # (upstream weight file, model class, output name)
    ("2.7_80x80_MiniFASNetV2.pth", "MiniFASNetV2", "minifasnet_v2.onnx"),
    ("4_0_0_80x80_MiniFASNetV1SE.pth", "MiniFASNetV1SE", "minifasnet_v1se.onnx"),
]


def main(upstream, out_dir):
    upstream, out_dir = Path(upstream), Path(out_dir)
    sys.path.insert(0, str(upstream))
    from src.model_lib import MiniFASNet  # upstream code

    out_dir.mkdir(parents=True, exist_ok=True)
    for weight_name, cls_name, out_name in JOBS:
        # 80x80 input -> conv6 kernel ((80+15)//16, (80+15)//16) = (5, 5), as in src/utility.py
        model = getattr(MiniFASNet, cls_name)(conv6_kernel=(5, 5))
        state = torch.load(upstream / "resources" / "anti_spoof_models" / weight_name,
                           map_location="cpu")
        state = OrderedDict((k[7:] if k.startswith("module.") else k, v)
                            for k, v in state.items())
        model.load_state_dict(state)  # strict: fails loudly on any mismatch
        model.eval()
        dummy = torch.zeros(1, 3, 80, 80)
        torch.onnx.export(model, dummy, str(out_dir / out_name), opset_version=12,
                          input_names=["input"], output_names=["logits"],
                          dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}})
        print("wrote", out_dir / out_name)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
