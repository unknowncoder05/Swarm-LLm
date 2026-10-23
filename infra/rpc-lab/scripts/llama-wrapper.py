#!/usr/bin/env python3
"""Adapt legacy CLI flags and retain device allocation logs for the experiment."""
import os
from pathlib import Path
import sys


def command(kind, args):
    if kind == "llama-server":
        # The project passes --no-mmap; current llama.cpp uses --load-mode. Using
        # mmap also avoids allocating the entire 20 GB model in 16 GiB host RAM.
        args = [arg for arg in args if arg != "--no-mmap"]
        # The agent forces 999 GPU layers. Let upstream fit the split to actual
        # free memory, including context/scratch and fractional-GPU overhead.
        for option in ("-ngl", "--gpu-layers", "--n-gpu-layers"):
            if option in args:
                args[args.index(option) + 1] = "auto"
        return ["/opt/peerfoundry/llama.cpp/build/bin/llama-server"] + args + [
            "--load-mode", "mmap", "--parallel", "1", "--flash-attn", "on",
            "--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--predict", "256",
            "--batch-size", "128", "--ubatch-size", "64",
            "--fit", "on", "--fit-target", "512", "-lv", "4",
            "--jinja",
        ]
    return ["/opt/peerfoundry/llama.cpp/build/bin/ggml-rpc-server", "--cache"] + args


def main():
    kind = Path(sys.argv[0]).name
    log_path = "/var/log/peerfoundry-llama.log" if kind == "llama-server" else "/var/log/swarm-rpc.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    argv = command(kind, sys.argv[1:])
    print("Launching", " ".join(argv), flush=True)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
