"""Build FEMTracker.exe, smoke-test it, and package it (portable zip + Inno Setup installer).

  python packaging/windows/build.py                 everything that's possible here
  python packaging/windows/build.py --no-installer  skip Inno Setup
  python packaging/windows/build.py --require-installer   fail if Inno Setup is missing (CI)
Run it with the build environment's Python (build.bat does this).
"""
import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUILD, DIST = ROOT / "build", ROOT / "dist"
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "femtracker" / "__init__.py").read_text()).group(1)
EXE_NAME = "FEMTracker.exe" if os.name == "nt" else "FEMTracker"


def step(msg):
    print(f"\n=== {msg}", flush=True)


def run(cmd, **kw):
    print("  >", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def pyinstaller():
    step(f"Building FEMTracker {VERSION} with PyInstaller")
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", BUILD / "dist", "--workpath", BUILD / "work", HERE / "femtracker.spec"], cwd=ROOT)
    app = BUILD / "dist" / "FEMTracker"
    if (app / "config.ini").exists():  # would put the installed app into portable mode
        (app / "config.ini").unlink()
    return app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read()
        return json.loads(body) if r.headers.get_content_type() == "application/json" else body


def upload_file(url, field, filename, data, content_type):
    """POST a single multipart/form-data file, without needing the requests library."""
    boundary = "----femtracker-smoke-test"
    body = (f"--{boundary}\r\n"
           f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
           f"Content-Type: {content_type}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def tiny_png():
    """A minimal real 1x1 PNG, built with only the standard library (no Pillow)."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes([0, 255, 0, 0]))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def smoke_test(app):
    step("Smoke-testing the built program")
    exe = app / EXE_NAME
    with tempfile.TemporaryDirectory() as tmp:
        port = free_port()
        env = {**os.environ, "FEM_CONFIG": str(Path(tmp) / "config.ini"), "FEM_DATA_DIR": str(Path(tmp) / "data"),
               "FEM_PORT": str(port), "FEM_OPEN_BROWSER": "no", "FEM_POLL_SECONDS": "2"}
        out = subprocess.run([exe, "version"], env=env, capture_output=True, text=True, timeout=120)
        assert VERSION in out.stdout, f"'version' printed {out.stdout!r} {out.stderr!r}"
        print(f"  version: {out.stdout.strip()}")
        proc = subprocess.Popen([exe, "start", "--no-browser"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(150):
                try:
                    assert http(base + "/api/health")["version"] == VERSION
                    break
                except Exception:
                    if proc.poll() is not None:
                        raise RuntimeError("FEMTracker exited early:\n" + proc.stdout.read())
                    time.sleep(0.2)
            else:
                raise RuntimeError("the web server did not come up within 30 s")
            assert b"FEM Tracker" in http(base + "/")
            assert http(base + "/static/icon.png")[:4] == b"\x89PNG"
            sid = http(base + "/api/studies", {"name": "smoke", "run_config": {"commands": ["echo hi"]}})["id"]
            aid = http(base + "/api/jobs", {"study_id": sid, "name": "smoke job"})["attempt_id"]
            assert "echo hi" in http(f"{base}/api/attempts/{aid}/script")["script"]
            for _ in range(50):  # agents start, find no HPC settings, and say so
                roles = {a["role"]: a["state"] for a in http(base + "/api/agents")}
                if len(roles) == 2:
                    break
                time.sleep(0.2)
            assert roles == {"run": "not_configured", "populate": "not_configured"}, roles
            print("  web page, API, database and both agents: ok")

            png = tiny_png()
            asset = upload_file(f"{base}/api/notebook/assets", "file", "smoke.png", png, "image/png")
            assert (asset["mime"], asset["width"], asset["height"]) == ("image/png", 1, 1)
            entry = http(f"{base}/api/studies/{sid}/notebook", {
                "title": "Smoke entry", "kind": "note", "format": "markdown",
                "body": f"![shot](asset:{asset['sha256']})"})
            assert f"/api/notebook/assets/{asset['sha256']}" in entry["rendered_html"]
            rendered = http(f"{base}/api/notebook/render", {"format": "markdown", "body": "<script>bad()</script>ok"})
            assert "<script" not in rendered["html"] and "ok" in rendered["html"]
            with urllib.request.urlopen(f"{base}/api/notebook/assets/{asset['sha256']}", timeout=5) as r:
                downloaded = r.read()
                assert r.headers.get("X-Content-Type-Options") == "nosniff"
            assert downloaded == png
            print("  notebook: image upload, render and download: ok")
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def portable_zip(app):
    step("Making the portable zip")
    os.environ["FEM_CONFIG"] = str(BUILD / "tmp-config.ini")  # importing settings must not touch the real one
    sys.path.insert(0, str(ROOT))
    from femtracker.settings import DEFAULT_INI
    DIST.mkdir(exist_ok=True)
    tag = "" if os.name == "nt" else f"-{'macos' if sys.platform == 'darwin' else sys.platform}-{platform.machine()}"
    zpath = DIST / f"FEMTracker-{VERSION}{tag}-portable.zip"
    exe = "FEMTracker.exe" if os.name == "nt" else "./FEMTracker"
    readme = ("FEM Tracker (portable)\r\n\r\n1. Edit config.ini: fill in the [hpc] section.\r\n"
              f"2. Run: {exe} check     (tests the HPC connection)\r\n"
              f"3. Run: {exe} start     (opens http://127.0.0.1:8000)\r\n\r\n"
              "Database, logs and results are kept in this folder (data, results).\r\n")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(app.rglob("*")):
            if f.is_file():
                z.write(f, Path("FEMTracker") / f.relative_to(app))
        z.writestr("FEMTracker/config.ini", DEFAULT_INI.replace("\n", "\r\n"))
        z.writestr("FEMTracker/README.txt", readme)
    print(f"  {zpath} ({zpath.stat().st_size / 1e6:.1f} MB)")
    return zpath


def find_iscc():
    candidates = [os.environ.get("ISCC"), shutil.which("iscc")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def installer(app, required):
    step("Making the installer (Inno Setup)")
    iscc = find_iscc()
    if not iscc:
        msg = "Inno Setup 6 not found (https://jrsoftware.org/isdl.php, or set ISCC=path\\to\\ISCC.exe)."
        if required:
            raise SystemExit("[ERROR] " + msg)
        print("  skipped: " + msg)
        return None
    run([iscc, "/Q", f"/DAppVersion={VERSION}", f"/DDistDir={app}", f"/DOutDir={DIST}", HERE / "installer.iss"])
    out = DIST / f"FEMTracker-{VERSION}-setup.exe"
    print(f"  {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-installer", action="store_true")
    ap.add_argument("--require-installer", action="store_true")
    ap.add_argument("--skip-smoke-test", action="store_true")
    a = ap.parse_args()
    app = pyinstaller()
    if not a.skip_smoke_test:
        smoke_test(app)
    outputs = [portable_zip(app)]
    if not a.no_installer:
        if os.name != "nt":
            print("\n  (the installer can only be built on Windows)")
        else:
            outputs.append(installer(app, a.require_installer))
    step("Finished")
    for o in outputs:
        if o:
            print(f"  {o}")


if __name__ == "__main__":
    main()
