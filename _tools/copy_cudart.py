"""cudart64_110.dll を cudart64_11.dll としてコピーする"""
import sys
import shutil
import pathlib

tmp_dir = pathlib.Path(sys.argv[1])
dst_dir = pathlib.Path(sys.argv[2])

matches = list(tmp_dir.rglob("cudart64_110.dll"))
if matches:
    dst = dst_dir / "cudart64_11.dll"
    shutil.copy2(matches[0], dst)
    print(f"[copy_cudart] Copied {matches[0]} -> {dst}")
else:
    print("[copy_cudart] WARN: cudart64_110.dll not found in", tmp_dir)
