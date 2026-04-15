# Egg-Counter Training Workspace

Training scripts, notebooks, and data-preparation utilities live here.

Training is always performed **off-device** (cloud workstation, desktop, or CI
runner).  The resulting TFLite model is exported and copied to `ai/models/` on
the target Raspberry Pi.

## Workflow

1. Collect and annotate egg images (COCO or Pascal VOC format).
2. Fine-tune an SSD MobileNet v2 checkpoint with TensorFlow Object Detection API.
3. Export to SavedModel, then convert to TFLite with quantisation.
4. Copy `egg_counter.tflite` and `labels.txt` to `ai/models/` on-device.

See `ai/README.md` for the full model contract.
