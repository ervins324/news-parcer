import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def get_rss_mb():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, os.getpid())
        if handle:
            ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            ctypes.windll.kernel32.CloseHandle(handle)
            return counters.WorkingSetSize / (1024 * 1024)
        return 0.0
    else:
        # Linux: Read from /proc/self/status for exact RSS
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
        except Exception:
            pass
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

if __name__ == "__main__":
    last = get_rss_mb()
    def log_step(name):
        global last
        now = get_rss_mb()
        diff = now - last
        last = now
        print(f"{name:<35}: {now:6.2f} MB (diff: {diff:+6.2f} MB)")

    print("=== Memory usage step-by-step ===")
    log_step("00. Base Python interpreter")

    import config
    log_step("01. import config")

    import aiohttp
    log_step("02. import aiohttp")

    import bs4
    log_step("03. import bs4")

    import lxml
    log_step("04. import lxml")

    import aiogram
    log_step("05. import aiogram")

    import apscheduler
    log_step("06. import apscheduler")

    from openrouter import OpenRouter
    log_step("07. import openrouter")

    from google import genai
    log_step("08. import google.genai")

    import main
    log_step("09. import main")

    import gc
    gc.collect()
    log_step("10. After gc.collect()")
