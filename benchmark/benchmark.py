#!/usr/bin/env python3
# Part of the aflplusplus project, requires Python 3.9+.
# Author: Chris Ball <chris@printf.net>, ported from Marc "van Hauser" Heuse's "benchmark.sh".
import argparse
import asyncio
import datetime
import json
import multiprocessing
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto
from pathlib import Path

blue   = lambda text: f"\033[1;94m{text}\033[0m"; gray = lambda text: f"\033[1;90m{text}\033[0m"
green  = lambda text: f"\033[0;32m{text}\033[0m"; red  = lambda text: f"\033[0;31m{text}\033[0m"
yellow = lambda text: f"\033[0;33m{text}\033[0m"

class Mode(Enum):
    multicore  = auto()
    singlecore = auto()

@dataclass
class Target:
    source: Path
    binary: Path

all_modes = [Mode.singlecore, Mode.multicore]
all_targets = [
    Target(source=Path("../utils/persistent_mode/test-instr.c").resolve(), binary=Path("test-instr-persist-shmem")),
    Target(source=Path("../test-instr.c").resolve(), binary=Path("test-instr"))
]
mode_names = [mode.name for mode in all_modes]
target_names = [str(target.binary) for target in all_targets]
cpu_count = multiprocessing.cpu_count()

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("-b", "--basedir", help="directory to use for temp files", type=str, default="/tmp/aflpp-benchmark")
parser.add_argument("-d", "--debug", help="show verbose debugging output", action="store_true")
parser.add_argument("-r", "--runs", help="how many runs to average results over", type=int, default=5)
parser.add_argument("-f", "--fuzzers", help="how many afl-fuzz workers to use", type=int, default=cpu_count)
parser.add_argument("-m", "--mode", help="pick modes", action="append", default=["multicore"], choices=mode_names)
parser.add_argument(
    "-t", "--target", help="pick targets", action="append", default=["test-instr-persist-shmem"], choices=target_names
)
args = parser.parse_args()
# Really unsatisfying argparse behavior: we want a default and to allow multiple choices, but if there's a manual choice
# it should override the default.  Seems like we have to remove the default to get that and have correct help text?
if len(args.target) > 1: args.target = args.target[1:]
if len(args.mode) > 1: args.mode = args.mode[1:]

targets = [target for target in all_targets if str(target.binary) in args.target]
modes = [mode for mode in all_modes if mode.name in args.mode]
results: dict[str, dict] = {
    "config": {}, "hardware": {}, "targets": {str(t.binary): {m.name: {} for m in modes} for t in targets}
}
debug = lambda text: args.debug and print(blue(text))
if Mode.multicore in modes:
    print(blue(f" [*] Using {args.fuzzers} fuzzers for multicore fuzzing "), end="")
    print(blue("(use --fuzzers to override)" if args.fuzzers == cpu_count else f"(the default is {cpu_count})"))

async def clean_up_tempfiles() -> None:
    shutil.rmtree(f"{args.basedir}/in")
    for target in targets:
        target.binary.unlink()
        for mode in modes:
            shutil.rmtree(f"{args.basedir}/out-{mode.name}-{str(target.binary)}")

async def check_afl_persistent() -> bool:
    with open("/proc/cmdline", "r") as cpuinfo:
        return "mitigations=off" in cpuinfo.read().split(" ")

async def check_afl_system() -> bool:
    sysctl = next((s for s in ["sysctl", "/sbin/sysctl"] if shutil.which(s)), None)
    if sysctl:
        (returncode, stdout, _) = await run_command([sysctl, "kernel.randomize_va_space"], None)
        return returncode == 0 and stdout.decode().rstrip().split(" = ")[1] == "0"
    return False

async def check_deps() -> None:
    if not (plat := platform.system()) == "Linux": sys.exit(red(f" [*] {plat} is not supported by this script yet."))
    if not os.access(Path("../afl-fuzz").resolve(), os.X_OK) and os.access(Path("../afl-cc").resolve(), os.X_OK) and (
        os.path.exists(Path("../SanitizerCoveragePCGUARD.so").resolve())):
        sys.exit(red(" [*] Compile AFL++: we need afl-fuzz, afl-clang-fast and SanitizerCoveragePCGUARD.so built."))

    # Pick some sample settings from afl-{persistent,system}-config to try to see whether they were run.
    cmd_checks = {"afl-persistent-config": check_afl_persistent, "afl-system-config": check_afl_system}
    for cmd, checker in cmd_checks.items():
        results["config"][cmd] = await checker()
        if not results["config"][cmd]:
            print(yellow(f" [*] {cmd} was not run. You can run it to improve performance (and decrease security)."))

async def prep_env() -> dict:
    Path(f"{args.basedir}/in").mkdir(exist_ok=True, parents=True)
    with open(f"{args.basedir}/in/in.txt", "wb") as seed: seed.write(b"\x00" * 10240)
    return {
        "AFL_BENCH_JUST_ONE": "1", "AFL_DISABLE_TRIM": "1", "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES": "1",
        "AFL_NO_UI": "1", "AFL_TRY_AFFINITY": "1", "PATH": str(Path("../").resolve()),
    }

async def compile_target(source: Path, binary: Path) -> None:
    print(f" [*] Compiling the {binary} fuzzing harness for the benchmark to use.")
    (returncode, stdout, stderr) = await run_command(
        [str(Path("../afl-cc").resolve()), "-o", str(Path(binary.resolve())), str(Path(source).resolve())],
        env={"AFL_INSTRUMENT": "PCGUARD"},
    )
    if returncode != 0: sys.exit(red(f" [*] Error: afl-cc is unable to compile: {stderr.decode()} {stdout.decode()}"))

async def run_command(cmd: list[str], env: dict | None) -> tuple[int | None, bytes, bytes]:
    debug(f"Launching command: {cmd} with env {env}")
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    stdout, stderr = await p.communicate()
    debug(f"Output: {stdout.decode()} {stderr.decode()}")
    return (p.returncode, stdout, stderr)

async def colon_value_or_none(filename: str, searchKey: str) -> str | None:
    """Return a colon-separated value given a key in a file, e.g. 'cpu MHz         : 4976.109')"""
    with open(filename, "r") as fh:
        kv_pairs = (line.split(": ", 1) for line in fh if ": " in line)
        return next((v.rstrip() for k, v in kv_pairs if k.rstrip() == searchKey), None)

async def save_benchmark_results() -> None:
    """Append a single row to the benchmark results in JSON Lines format (which is simple to write and diff)."""
    with open("benchmark-results.jsonl", "a") as jsonfile:
        json.dump(results, jsonfile, sort_keys=True)
        jsonfile.write("\n")
        print(blue(f" [*] Results have been written to {jsonfile.name}"))


async def main() -> None:
    try:
        await clean_up_tempfiles()
    except FileNotFoundError:
        pass
    await check_deps()
    results["hardware"] = { # Only record the first core's speed for now, even though it can vary between cores.
        "cpu_mhz":     float(await colon_value_or_none("/proc/cpuinfo", "cpu MHz") or ""),
        "cpu_model":   await colon_value_or_none("/proc/cpuinfo", "model name") or "",
        "cpu_threads": cpu_count
    }
    env_vars = await prep_env()
    print(f" [*] Ready, starting benchmark...")
    for target in targets:
        await compile_target(target.source, target.binary)
        binary = str(target.binary)
        for mode in modes:
            execs_per_sec, execs_total, run_time_total = ([] for _ in range(3))
            for run in range(0, args.runs):
                print(gray(f" [*] {mode.name} {binary} run {run+1} of {args.runs}, execs/s: "), end="", flush=True)
                fuzzers = range(0, args.fuzzers if mode == Mode.multicore else 1)
                outdir = f"{args.basedir}/out-{mode.name}-{binary}"
                cmds = []
                for idx, afl in enumerate(fuzzers):
                    name = ["-o", outdir, "-M" if idx == 0 else "-S", str(afl)]
                    cmds.append(["afl-fuzz", "-i", f"{args.basedir}/in"] + name + ["-s", "123", "-D", f"./{binary}"])

                # Prepare the afl-fuzz tasks, and then block while waiting for them to finish.
                fuzztasks = [run_command(cmds[cpu], env_vars) for cpu in fuzzers]
                start_time = datetime.datetime.now()
                await asyncio.gather(*fuzztasks)
                end_time = datetime.datetime.now()

                # Our score is the sum of all execs_per_sec entries in fuzzer_stats files for the run.
                sectasks = [colon_value_or_none(f"{outdir}/{afl}/fuzzer_stats", "execs_per_sec") for afl in fuzzers]
                all_execs_per_sec = await asyncio.gather(*sectasks)
                execs = sum([Decimal(count) for count in all_execs_per_sec if count is not None])
                print(green(execs))
                execs_per_sec.append(execs)

                # Also gather execs_total and total_run_time for this run.
                exectasks = [colon_value_or_none(f"{outdir}/{afl}/fuzzer_stats", "execs_done") for afl in fuzzers]
                all_execs_total = await asyncio.gather(*exectasks)
                execs_total.append(sum([Decimal(count) for count in all_execs_total if count is not None]))
                run_time_total.append((end_time - start_time).total_seconds())

            avg_score = round(Decimal(sum(execs_per_sec) / len(execs_per_sec)), 2)
            afl_execs_total = int(sum([Decimal(execs) for execs in execs_total]))
            total_run_time = float(round(Decimal(sum(run_time_total)), 2))
            results["targets"][binary][mode.name] = { # (Using float() because Decimal() is not JSON-serializable.)
                "afl_execs_per_second": float(avg_score),
                "afl_execs_total":      afl_execs_total,
                "fuzzers_used":         len(fuzzers),
                "start_time_of_run":    str(start_time),
                "total_execs_per_sec":  float(round(Decimal(afl_execs_total / total_run_time), 2)),
                "total_run_time":       total_run_time,
            }
            print(f" [*] Average score for this test across all runs was: {green(avg_score)}")
            if (((max(execs_per_sec) - min(execs_per_sec)) / avg_score) * 100) > 15:
                print(yellow(" [*] The difference between your slowest and fastest runs was >15%, maybe try again?"))
    await clean_up_tempfiles()
    await save_benchmark_results()

if __name__ == "__main__":
    asyncio.run(main())

