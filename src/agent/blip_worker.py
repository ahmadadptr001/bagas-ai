"""Worker CPU BLIP mandiri. Stdout hanya protokol JSON, tanpa impor agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

# Pembatas ini hanya berlaku di worker ini, tidak mengubah proses aplikasi.
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"


class BlipEngine:
    def __init__(self, folder):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        options.log_severity_level = 3
        folder = Path(folder)
        self.encoder = ort.InferenceSession(str(folder / "blip_vision_encoder_int8.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"])
        self.decoder = ort.InferenceSession(str(folder / "blip_text_decoder_int8.onnx"),
            sess_options=options, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))

    def caption(self, path, max_tokens=32):
        import numpy as np
        from PIL import Image
        with Image.open(path) as image:
            image = image.convert("RGB").resize((384, 384), Image.Resampling.BICUBIC)
            pixels = np.asarray(image, dtype=np.float32) / 255.0
        pixels = (pixels - np.array([.48145466, .4578275, .40821073], dtype=np.float32)) / np.array(
            [.26862954, .26130258, .27577711], dtype=np.float32)
        pixels = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])
        hidden = self.encoder.run(None, {"pixel_values": pixels})[0]
        tokens = [30522]  # BLIP [DEC]; EOS gambar menggunakan [SEP].
        for _ in range(max_tokens):
            ids = np.array([tokens], dtype=np.int64)
            inputs = {"input_ids": ids, "attention_mask": np.ones_like(ids),
                "encoder_hidden_states": hidden,
                "encoder_attention_mask": np.ones(hidden.shape[:2], dtype=np.int64)}
            inputs = {node.name: inputs[node.name] for node in self.decoder.get_inputs()}
            logits = self.decoder.run(None, inputs)[0]
            token = int(np.argmax(logits[0, -1]))
            if token in (0, 102, 30522):
                break
            tokens.append(token)
        return self.tokenizer.decode(tokens[1:], skip_special_tokens=True).strip()


def main():
    if "--prepare-model" in sys.argv:
        # HTTP lebih cocok untuk unduhan dua berkas ini daripada worker Xet.
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from blip_local import model_directory
        model_directory(download=True)
        print(json.dumps({"ready": True}), flush=True)
        return
    engine = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if engine is None:
                engine = BlipEngine(request["folder"])
            print(json.dumps({"caption": engine.caption(request["path"])}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__}), flush=True)


if __name__ == "__main__":
    main()
