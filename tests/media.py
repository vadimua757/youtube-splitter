import subprocess
from pathlib import Path


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_audio(path: Path, codec: str, seconds: int = 10) -> Path:
    _ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", codec, str(path)])
    return path


def make_video(path: Path, seconds: int = 10) -> Path:
    _ffmpeg([
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ])
    return path


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(out.strip())
