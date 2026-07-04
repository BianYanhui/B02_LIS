#!/usr/bin/env python3
"""Download a model from ModelScope and print its local path.

Usage:
  source ~/B02/poc/.venv/bin/activate
  python download_modelscope.py [model_id]

Example:
  python download_modelscope.py qwen/Qwen2.5-1.5B-Instruct
"""
import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "model_id",
        nargs="?",
        default="qwen/Qwen2.5-1.5B-Instruct",
        help="ModelScope model id, e.g. qwen/Qwen2.5-1.5B-Instruct",
    )
    ap.add_argument(
        "--cache-dir",
        default=os.path.expanduser("~/.cache/modelscope"),
        help="Where to place the model files",
    )
    args = ap.parse_args()

    print(f"== ModelScope download ==")
    print(f"   model_id  = {args.model_id}")
    print(f"   cache_dir = {args.cache_dir}")
    print()

    # Import inside main so missing dep shows clean error.
    from modelscope import snapshot_download

    print(f"[step] calling snapshot_download ... (may take a few minutes)")
    local_dir = snapshot_download(
        args.model_id,
        cache_dir=args.cache_dir,
    )
    print(f"[OK] local_dir = {local_dir}")

    # Echo to a file for downstream scripts to consume
    out_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "model_path.txt",
    )
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w") as f:
        f.write(local_dir + "\n")
    print(f"[OK] wrote {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
