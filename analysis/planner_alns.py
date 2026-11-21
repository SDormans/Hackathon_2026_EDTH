"""Benchmark entry point: runs ../planner.py with --alns (for benchmark_search.py --planner)."""
import runpy
import sys
from pathlib import Path

sys.argv.append("--alns")
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
runpy.run_path(str(root / "planner.py"), run_name="__main__")
