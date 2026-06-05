"""Quick log watch - opens the serial port, captures lines for N seconds, then dumps crash markers."""
import sys
import time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bft import LogSession, LOGS


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM47"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    sess = LogSession(port, baud=115200,
                     log_path=LOGS / f"device_watch_{int(time.time())}.log")
    print(f"Capturing {port} @ 115200 for {seconds}s...", flush=True)
    sess.start()
    try:
        time.sleep(seconds)
    finally:
        sess.stop()
    print(f"crashed={sess.crashed}", flush=True)
    if sess.crashed:
        print("First crash lines:", flush=True)
        for line in sess.crash_lines[:10]:
            print(f"  {line[:200]}", flush=True)


if __name__ == "__main__":
    main()
