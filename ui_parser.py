"""UI element detection using OmniParser v2 models.

Models:
- YOLO (icon_detect/model.pt) — detects interactive UI elements
- Florence-2 (icon_caption/) — captions each detected element
- EasyOCR — detects text with bounding box coordinates
"""

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Module-level model storage
_models = {
    "yolo": None,
    "florence_model": None,
    "florence_processor": None,
    "easyocr": None,
}
_model_dir = None


def load_models() -> None:
    """Download and load all 3 models. Idempotent — safe to call multiple times."""
    global _model_dir

    if _models["yolo"] is not None:
        return  # Already loaded

    from huggingface_hub import snapshot_download

    # Download OmniParser v2 model files
    _model_dir = Path(snapshot_download("microsoft/OmniParser-v2.0"))

    # 1) YOLO icon detector
    from ultralytics import YOLO
    yolo_path = _model_dir / "icon_detect" / "model.pt"
    _models["yolo"] = YOLO(str(yolo_path))

    # 2) Florence-2 caption model
    #    Processor from base Florence-2 (has tokenizer + image processor)
    #    Weights from OmniParser's fine-tuned icon_caption
    from transformers import AutoProcessor, AutoModelForCausalLM
    florence_path = _model_dir / "icon_caption"
    _models["florence_processor"] = AutoProcessor.from_pretrained(
        "microsoft/Florence-2-base-ft", trust_remote_code=True
    )
    _models["florence_model"] = AutoModelForCausalLM.from_pretrained(
        str(florence_path), trust_remote_code=True
    )

    # Move to GPU if available
    import torch
    if torch.cuda.is_available():
        _models["florence_model"] = _models["florence_model"].to("cuda")

    # 3) EasyOCR reader
    import easyocr
    _models["easyocr"] = easyocr.Reader(["en"], gpu=torch.cuda.is_available())


def _caption_icon(cropped: Image.Image) -> str:
    """Use Florence-2 to caption a cropped icon image."""
    import torch

    processor = _models["florence_processor"]
    model = _models["florence_model"]
    device = next(model.parameters()).device

    prompt = "<CAPTION>"
    inputs = processor(text=prompt, images=cropped, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        generated_ids = model.generate(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"],
            max_new_tokens=50,
            num_beams=3,
        )

    result = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
    caption = result.strip()
    # Florence-2 often appends a trailing period — strip it
    if caption.endswith("."):
        caption = caption[:-1].rstrip()
    return caption


def parse_ui(image: Image.Image, confidence_threshold: float = 0.5) -> dict:
    """Run full UI detection pipeline. Returns dict with screen_size and elements."""
    if _models["yolo"] is None:
        raise RuntimeError("UI parser models not loaded. Call load_models() first.")

    width, height = image.size
    elements = []
    element_id = 1

    # 1) YOLO icon detection
    yolo = _models["yolo"]
    results = yolo(image, conf=confidence_threshold, verbose=False)

    if results and len(results) > 0:
        boxes = results[0].boxes
        if boxes is not None:
            for box in boxes:
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                conf = float(box.conf[0])

                # Crop and caption with Florence-2
                cropped = image.crop((x1, y1, x2, y2))
                try:
                    label = _caption_icon(cropped)
                except Exception:
                    label = "UI element"

                cx = (x1 + x2) // 2
                cy = (y1 + y2) // 2

                elements.append({
                    "id": element_id,
                    "type": "icon",
                    "label": label,
                    "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                    "center": {"x": cx, "y": cy},
                    "confidence": round(conf, 3),
                })
                element_id += 1

    # 2) EasyOCR text detection
    ocr = _models["easyocr"]
    import numpy as np
    img_array = np.array(image)
    ocr_results = ocr.readtext(img_array)

    for bbox_pts, text, conf in ocr_results:
        if conf < confidence_threshold:
            continue

        # EasyOCR returns [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        xs = [int(p[0]) for p in bbox_pts]
        ys = [int(p[1]) for p in bbox_pts]
        x1, y1 = min(xs), min(ys)
        x2, y2 = max(xs), max(ys)
        cx = (x1 + x2) // 2
        cy = (y1 + y2) // 2

        elements.append({
            "id": element_id,
            "type": "text",
            "content": text,
            "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            "center": {"x": cx, "y": cy},
            "confidence": round(float(conf), 3),
        })
        element_id += 1

    return {
        "screen_size": {"width": width, "height": height},
        "elements": elements,
    }


def annotate_image(image: Image.Image, elements: list[dict]) -> Image.Image:
    """Draw colored rectangles + numbered labels on a copy of the image.
    Green = icons, Blue = text."""
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)

    # Try to get a readable font, fall back to default
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for el in elements:
        bbox = el["bbox"]
        x1, y1, x2, y2 = bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]
        el_id = el["id"]

        if el["type"] == "icon":
            color = (0, 200, 0)  # green
            label = f'{el_id}: {el.get("label", "")}'
        else:
            color = (0, 120, 255)  # blue
            label = f'{el_id}: {el.get("content", "")}'

        # Draw rectangle
        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

        # Draw label background + text
        text_bbox = draw.textbbox((x1, y1 - 16), label, font=font)
        draw.rectangle(text_bbox, fill=color)
        draw.text((x1, y1 - 16), label, fill="white", font=font)

    return annotated
