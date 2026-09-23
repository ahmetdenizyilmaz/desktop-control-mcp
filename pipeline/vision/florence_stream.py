"""Watch Florence-2 read a screen one token at a time, and time each one.

The point is to see WHY it is slow, rather than take the number on trust.
Florence is autoregressive: it does not "look and report", it writes the
answer out token by token, and each token needs a full forward pass through
the language decoder. So its cost is not a property of the picture — it is a
property of how much there is to say about the picture.

Run it beside Windows OCR on the same pixels:

    python florence_stream.py                  a text-heavy region
    python florence_stream.py --full           the whole screen (slow)

Expect two phases, clearly separated in the output: a long pause while the
vision encoder runs once, then a steady drip of tokens at roughly the same
interval each.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vision import ocr                    # noqa: E402
from vision.capture import Screen         # noqa: E402


class Ticker:
    """A transformers streamer that timestamps every token as it lands."""

    def __init__(self, tokenizer) -> None:
        self.tok = tokenizer
        self.times: list[float] = []
        self.pieces: list[str] = []
        self.t0 = time.perf_counter()
        self.first: float | None = None

    def put(self, value) -> None:
        # The first call is the prompt being echoed back, not generation.
        ids = value[0] if hasattr(value, "shape") and len(value.shape) > 1 else value
        if not self.times and len(ids) > 1:
            return
        now = time.perf_counter() - self.t0
        if self.first is None:
            self.first = now
        text = self.tok.decode(ids, skip_special_tokens=False)
        gap = now - (self.times[-1] if self.times else self.first)
        self.times.append(now)
        self.pieces.append(text)
        n = len(self.times)
        if n <= 40 or n % 10 == 0:
            print(f"  token {n:>3}  t={now:6.2f}s  (+{gap * 1000:5.0f} ms)  "
                  f"{text!r}", flush=True)

    def end(self) -> None:
        pass


def main(argv: list[str]) -> int:
    from huggingface_hub import snapshot_download
    from transformers import AutoModelForCausalLM, AutoProcessor
    import torch

    full = "--full" in argv
    screen = Screen()
    region = None if full else (0, 0, 1100, 620)
    frame = screen.grab(region)
    print(f"region {frame.region}, {frame.width}x{frame.height}px\n")

    # Same pixels, both engines.
    reading = ocr.read(frame)
    print(f"Windows OCR : {reading.ms:6.0f} ms   "
          f"{len(reading.words)} words, {len(reading.lines)} lines\n")

    md = Path(snapshot_download("microsoft/OmniParser-v2.0"))
    proc = AutoProcessor.from_pretrained("microsoft/Florence-2-base-ft",
                                         trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(str(md / "icon_caption"),
                                                 trust_remote_code=True).eval()

    inputs = proc(text="<OCR>", images=frame.image, return_tensors="pt")
    ticker = Ticker(proc.tokenizer)
    print("Florence-2 <OCR>, streaming:")
    t0 = time.perf_counter()
    with torch.inference_mode():
        model.generate(input_ids=inputs["input_ids"],
                       pixel_values=inputs["pixel_values"],
                       max_new_tokens=400, num_beams=1, do_sample=False,
                       streamer=ticker)
    total = time.perf_counter() - t0

    n = len(ticker.times)
    encoder = ticker.first or 0.0
    decode = total - encoder
    per = (decode / max(n - 1, 1)) * 1000
    print(f"\n  first token at {encoder:.2f}s   <- the vision encoder, "
          f"paid once")
    print(f"  {n} tokens in {decode:.2f}s   <- the decoder, "
          f"~{per:.0f} ms each")
    print(f"  total {total:.2f}s\n")
    print(f"Florence read {len(''.join(ticker.pieces).split())} words in "
          f"{total * 1000:.0f} ms")
    print(f"Windows OCR read {len(reading.words)} words in {reading.ms:.0f} ms"
          f"  ->  {total * 1000 / max(reading.ms, 1):.0f}x faster")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
