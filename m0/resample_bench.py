#!/usr/bin/env python
"""M0-2a: 업/다운링크 리플러 지연 실측 (마이크 불필요).

검증 대상: PLAN §2.2 의 트랜스코딩 경로
  업링크   WebRTC Opus 48kHz PCM16 mono -> DashScope 16kHz PCM16 mono
  다운링크 DashScope 24kHz PCM16 mono   -> WebRTC 48kHz PCM16 mono

각 20ms 프레임(=960 samples @48k, 480 @24k)을 실제로 변환하고
프레임당 소요시간(ns)을 재서 "0.05ms 미만" 가정을 검증한다.

사용: .venv/bin/python m0/resample_bench.py
"""
import statistics
import sys
import time

import av
import numpy as np
import soxr

FRAME_MS = 20


def make_frame(rate: int, samples: int, freq: float = 440.0) -> av.AudioFrame:
    """rate Hz, s16, mono, samples 길이의 사인파 프레임."""
    t = np.arange(samples) / rate
    pcm = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
    frame.sample_rate = rate
    frame.planes[0].update(pcm.tobytes())
    frame.pts = None
    return frame


def bench_pyav(in_rate: int, out_rate: int, n: int = 2000) -> dict:
    in_samples = in_rate * FRAME_MS // 1000
    resampler = av.AudioResampler(format="s16", layout="mono", rate=out_rate)
    times: list[float] = []
    produced = 0
    for _ in range(n):
        frame = make_frame(in_rate, in_samples)
        t0 = time.perf_counter_ns()
        out = resampler.resample(frame)
        t1 = time.perf_counter_ns()
        times.append((t1 - t0) / 1e6)  # ms
        produced += sum(f.samples for f in out)
    return {
        "engine": "PyAV(AudioResampler)",
        "path": f"{in_rate}->{out_rate}",
        "frames": n,
        "out_samples_expected": produced,
        "mean_ms": statistics.mean(times),
        "p95_ms": sorted(times)[int(0.95 * len(times))],
        "max_ms": max(times),
    }


def bench_soxr(in_rate: int, out_rate: int, n: int = 2000) -> dict:
    in_samples = in_rate * FRAME_MS // 1000
    times: list[float] = []
    produced = 0
    x = (0.3 * np.sin(2 * np.pi * 440 * np.arange(in_samples) / in_rate) * 32767).astype(np.int16)
    for _ in range(n):
        t0 = time.perf_counter_ns()
        y = soxr.resample(x, in_rate, out_rate)
        t1 = time.perf_counter_ns()
        times.append((t1 - t0) / 1e6)
        produced += len(y)
    return {
        "engine": "soxr",
        "path": f"{in_rate}->{out_rate}",
        "frames": n,
        "out_samples_expected": produced,
        "mean_ms": statistics.mean(times),
        "p95_ms": sorted(times)[int(0.95 * len(times))],
        "max_ms": max(times),
    }


def main() -> int:
    print(f"av={av.__version__}  soxr={soxr.__version__}  numpy={np.__version__}")
    print(f"budget: {FRAME_MS}ms 프레임 1개당 실시간 예산 = {FRAME_MS}ms "
          f"(변환 1회 < 0.05ms 여야 무시 가능)\n")

    rows = []
    for fn, in_r, out_r in (
        (bench_pyav, 48000, 16000),   # 업링크 (WebRTC -> DashScope)
        (bench_pyav, 24000, 48000),   # 다운링크 (DashScope -> WebRTC)
        (bench_soxr, 48000, 16000),
        (bench_soxr, 24000, 48000),
    ):
        rows.append(fn(in_r, out_r))

    hdr = f"{'engine':22} {'path':14} {'mean(ms)':>9} {'p95(ms)':>9} {'max(ms)':>9} {'총출력샘플':>10}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['engine']:22} {r['path']:14} {r['mean_ms']:9.4f} {r['p95_ms']:9.4f} "
              f"{r['max_ms']:9.4f} {r['out_samples_expected']:10d}")

    # 무결성: 리샘플러가 실제로 정확한 샘플 수를 만들어내는지(스케일 검증)
    print("\n[무결성] PyAV 48k->16k 20ms 프레임 1개 출력 샘플 수: ", end="")
    res = av.AudioResampler(format="s16", layout="mono", rate=16000)
    outs = res.resample(make_frame(48000, 960))
    print(f"{[f.samples for f in outs]} (기대: 320 근방)")

    print("[무결성] PyAV 24k->48k 20ms 프레임 1개 출력 샘플 수: ", end="")
    res = av.AudioResampler(format="s16", layout="mono", rate=48000)
    outs = res.resample(make_frame(24000, 480))
    print(f"{[f.samples for f in outs]} (기대: 960 근방)")

    worst = max(r["p95_ms"] for r in rows)
    ok = worst < 0.05
    print(f"\n판정: p95 최악 {worst:.4f}ms → {'통과 (<0.05ms)' if ok else '실패 (>=0.05ms)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())