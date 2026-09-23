"""Record the screen WITH the computer's own sound, into one MP4.

Two captures run side by side and are muxed at the end:

  video   ffmpeg's `gdigrab` over the whole desktop, H.264 ultrafast,
          downscaled so a long run stays small. gdigrab copies with
          CAPTUREBLT, so layered windows — the agent's drawn cursor — are
          in the picture (unless they opted out of capture).
  audio   WASAPI LOOPBACK of the default speakers, via PyAudioWPatch:
          exactly what the machine plays, not the microphone. ffmpeg on
          this machine can only see the microphone (no "Stereo Mix"), which
          is why this half is done in Python.

The two start a few hundred milliseconds apart; the gap is measured and
applied as an audio offset when muxing, so sound and picture line up.

    rec = Recorder("run.mp4"); rec.start()
    ...
    path = rec.stop()
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

RECORDINGS = Path(__file__).resolve().parent / "recordings"


class Recorder:
    def __init__(self, out: str | Path | None = None, fps: int = 20,
                 width: int = 1440, max_s: float = 0.0) -> None:
        RECORDINGS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.out = Path(out) if out else RECORDINGS / f"run-{stamp}.mp4"
        self._video = self.out.with_suffix(".video.mp4")
        self._audio = self.out.with_suffix(".audio.wav")
        self.fps, self.width = fps, width
        #: Hard cap on the video, enforced by ffmpeg itself (`-t`), so a run
        #: that hangs cannot record forever. The audio is cut to match when
        #: muxing (`-shortest`).
        self.max_s = max_s
        self._ff: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._audio_thread: threading.Thread | None = None
        self._t_video = self._t_audio = 0.0
        self.audio_ok = False
        self.ffmpeg = shutil.which("ffmpeg")

    # -- audio -------------------------------------------------------------

    def _audio_loop(self, ready: threading.Event) -> None:
        try:
            import pyaudiowpatch as pa
        except ImportError:
            ready.set()
            return
        p = pa.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            dev = next((d for d in p.get_loopback_device_info_generator()
                        if spk["name"] in d["name"]), None)
            if dev is None:
                ready.set()
                return
            ch = int(dev["maxInputChannels"])
            rate = int(dev["defaultSampleRate"])
            stream = p.open(format=pa.paInt16, channels=ch, rate=rate,
                            input=True, frames_per_buffer=1024,
                            input_device_index=dev["index"])
            with wave.open(str(self._audio), "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(rate)
                self._t_audio = time.time()
                self.audio_ok = True
                ready.set()
                silence = b"\x00" * (1024 * ch * 2)
                while not self._stop.is_set():
                    # Loopback delivers NOTHING while nothing plays, so a
                    # blocking read would stall and the wav would come out
                    # shorter than the video, drifting every quiet stretch.
                    # Pad with silence at the pace of real time instead.
                    if stream.get_read_available() >= 1024:
                        w.writeframes(stream.read(1024,
                                                  exception_on_overflow=False))
                    else:
                        due = (time.time() - self._t_audio) * rate
                        have = w.tell()
                        if due - have > 1024:
                            w.writeframes(silence)
                        else:
                            time.sleep(0.005)
            stream.stop_stream()
            stream.close()
        finally:
            ready.set()
            p.terminate()

    # -- control -----------------------------------------------------------

    def start(self) -> bool:
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is not on PATH")
        ready = threading.Event()
        self._audio_thread = threading.Thread(target=self._audio_loop,
                                              args=(ready,), daemon=True)
        self._audio_thread.start()
        ready.wait(5.0)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "gdigrab", "-framerate", str(self.fps),
               "-draw_mouse", "0",
               *(["-t", f"{self.max_s:g}"] if self.max_s else []),
               "-i", "desktop",
               "-vf", f"scale={self.width}:-2",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-pix_fmt", "yuv420p", str(self._video)]
        self._ff = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    creationflags=0x08000000)  # no console
        self._t_video = time.time()
        return True

    def stop(self) -> Path:
        """Stop both, mux, and return the finished file."""
        self._stop.set()
        if self._ff is not None:
            try:
                self._ff.stdin.write(b"q")        # ffmpeg's clean stop
                self._ff.stdin.flush()
            except Exception:
                pass
            try:
                self._ff.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._ff.kill()
        if self._audio_thread is not None:
            self._audio_thread.join(timeout=5)

        if not self.audio_ok or not self._audio.exists():
            self._video.replace(self.out)
            return self.out
        offset = self._t_audio - self._t_video    # + means audio began later
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-i", str(self._video),
               "-itsoffset", f"{offset:.3f}", "-i", str(self._audio),
               "-map", "0:v", "-map", "1:a", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "160k", "-shortest", str(self.out)]
        subprocess.run(cmd, creationflags=0x08000000, check=False)
        if self.out.exists():
            self._video.unlink(missing_ok=True)
            self._audio.unlink(missing_ok=True)
        return self.out
