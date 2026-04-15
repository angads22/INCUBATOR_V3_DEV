# Egg-Counting AI

Local TensorFlow Lite object-detection pipeline for counting eggs from still
images captured by the incubator camera.

## Directory Layout

```
ai/
├── models/          # Runtime TFLite model + labels (not committed to git)
│   ├── egg_counter.tflite
│   └── labels.txt
├── train/           # Training scripts and notebooks (run off-device)
└── README.md        # This file
```

## Model Contract

| Artifact              | Purpose                              |
|-----------------------|--------------------------------------|
| `egg_counter.tflite`  | Quantised SSD-style detection model  |
| `labels.txt`          | One class label per line (`egg`)     |

The runtime expects **SSD MobileNet-style** output tensors:

| Index | Tensor          | Shape              |
|-------|-----------------|--------------------|
| 0     | bounding boxes  | `[1, N, 4]` (ymin, xmin, ymax, xmax normalised) |
| 1     | class indices   | `[1, N]`           |
| 2     | confidence      | `[1, N]`           |
| 3     | detection count | `[1]`              |

## Inference Path

`app/services/vision.py` loads the interpreter once at import time and exposes:

```python
result = count_eggs(image_path, confidence_threshold=0.5)
# -> {"ok": True, "count": 3, "detections": [...]}
```

If the model file is missing the function returns a structured error instead of
raising an exception, so the rest of the application continues to work.

## Training

Training is done off-device (desktop / cloud workstation).  See `ai/train/`
for scripts and data-prep utilities.  After training, export the model with:

```bash
tflite_convert --saved_model_dir=saved_model --output_file=ai/models/egg_counter.tflite
```

Then copy the `.tflite` and `labels.txt` to the target device under
`ai/models/`.

## Hardware Target

Optimised for **Raspberry Pi Zero 2 W** — single-image inference only, no
video streaming.  Expect ~300-800 ms per frame depending on input size and
quantisation.
