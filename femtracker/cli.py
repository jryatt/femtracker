"""FEMTracker.exe: one program for the web server and both HPC agents.

  FEMTracker.exe                 same as "start"
  FEMTracker.exe start           web server + run agent + populate agent, opens the browser
                                 (add --no-browser to skip opening it)
  FEMTracker.exe server          web server only
  FEMTracker.exe run-agent ...   step 1 on its own (submit | sync | loop)
  FEMTracker.exe populate ...    step 2 on its own (see populate --help)
  FEMTracker.exe check           test the HPC connection with the current settings
  FEMTracker.exe export --study N [--out DIR]
                                 write a study as plain files for git (see export --help)
  FEMTracker.exe import --study N --name NAME [--params JSON] [--note TEXT] FOLDER
                                 add results you produced outside FEM Tracker as a new job
  FEMTracker.exe config          open config.ini
  FEMTracker.exe paths           show where settings, database, logs and results live
  FEMTracker.exe folder          open the data folder in Explorer
  FEMTracker.exe guide           open the user guide
  FEMTracker.exe version
"""
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser

from femtracker import __version__, settings


def setup_logging(console=True):
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(settings.LOG_DIR / "femtracker.log", maxBytes=2_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if console and sys.stderr:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root.addHandler(ch)


def pause_if_double_clicked():
    """Keep the window open after an error when the user double-clicked the exe."""
    if getattr(sys, "frozen", False) and settings.WINDOWS and len(sys.argv) <= 2:
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def server_running():
    import requests
    try:
        return requests.get(settings.SERVER + "/api/health", timeout=2).json().get("app") == "FEM Tracker"
    except Exception:
        return False


def set_title(text):
    if settings.WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except Exception:
            pass


def cmd_start(agents=True, browser=True):
    setup_logging()
    log = logging.getLogger("femtracker")
    set_title("FEM Tracker")
    url = settings.SERVER
    if server_running():
        log.info(f"FEM Tracker is already running at {url}.")
        if browser:
            webbrowser.open(url)
        time.sleep(1.5)
        return
    import uvicorn
    try:
        from femtracker import server
    except settings.OldSchema as e:
        log.error(str(e))
        pause_if_double_clicked()
        sys.exit(1)

    stop = threading.Event()

    def after_start():
        for _ in range(100):
            if server_running():
                break
            time.sleep(0.2)
        else:
            return
        log.info(f"FEM Tracker {__version__} is running at {url}")
        log.info(f"Settings: {settings.CONFIG_PATH}")
        log.info("Close this window to stop FEM Tracker.")
        if agents and settings.hpc_problems():
            log.warning("HPC connection not set up yet; runs will wait in the queue. Missing: "
                        + "; ".join(settings.hpc_problems()))
            log.warning(f"Edit {settings.CONFIG_PATH} (Start menu: FEM Tracker settings), then restart.")
        from femtracker.agent import populate, run_agent
        if agents and settings.RUN_AGENT:
            threading.Thread(target=run_agent.loop, args=(stop,), name="run-agent", daemon=True).start()
        if agents and settings.POPULATE_AGENT:
            threading.Thread(target=populate.loop, args=(stop,), name="populate", daemon=True).start()
        if browser and settings.OPEN_BROWSER:
            webbrowser.open(url)

    threading.Thread(target=after_start, daemon=True).start()
    config = uvicorn.Config(server.app, host=settings.BIND_HOST, port=settings.PORT,
                            log_config=None, access_log=False)
    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):  # Ctrl+C / window closing: stop the agents first
            stop.set()
            super().handle_exit(sig, frame)

    srv = Server(config)
    try:
        srv.run()
    finally:
        stop.set()
        time.sleep(0.5)
    if not srv.started:
        log.error(f"Could not start the web server on port {settings.PORT}. Is another program using it? "
                  f"Change [server] port in {settings.CONFIG_PATH}.")
        pause_if_double_clicked()
        sys.exit(1)


def cmd_check():
    import tempfile
    from femtracker.agent import remote
    print(f"Settings file: {settings.CONFIG_PATH}")
    print(f"Transport:     {settings.TRANSPORT}")
    if settings.TRANSPORT == "putty":
        print(f"plink:         {settings.PLINK or '(not found)'}")
        print(f"pscp:          {settings.PSCP or '(not found)'}")
        print(f"Key:           {settings.PPK or '(empty)'}")
    print(f"Target:        {remote.host_label()}:{settings.REMOTE_WORKDIR or '(empty)'}")
    print(f"Results go to: {settings.LOCAL_RESULTS_ROOT}\n")
    problems = settings.hpc_problems()
    if problems:
        print("Fix these in the settings file first:")
        for p in problems:
            print(f"  [!!] {p}")
        return False
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            print(f"  [ok] {name}{': ' + detail if detail else ''}")
            return True
        except Exception as e:
            print(f"  [!!] {name}: {e}")
            ok = False
            return False

    w = remote.q(settings.REMOTE_WORKDIR)
    if not step("log in and run a command", lambda: remote.run("echo connected to $(hostname)").strip()):
        print("\nIf this is the first connection, open PuTTY once and accept the host key.")
        return False
    step("remote folder is writable", lambda: remote.run(
        f"mkdir -p {w} && test -w {w} && echo {settings.REMOTE_WORKDIR}").strip())
    step("tmux is available", lambda: remote.run(
        "command -v tmux >/dev/null && tmux -V || { echo 'not found (nohup will be used instead)'; }").strip())

    def roundtrip():
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "fem_check.txt")
            with open(src, "w") as fh:
                fh.write("FEM Tracker connection check\n")
            remote.upload([src], settings.REMOTE_WORKDIR)
            back = os.path.join(tmp, "back")
            remote.download(settings.REMOTE_WORKDIR.rstrip("/") + "/fem_check.txt", back)
            remote.run(f"rm -f {w}/fem_check.txt")
            if open(os.path.join(back, "fem_check.txt")).read() != "FEM Tracker connection check\n":
                raise RuntimeError("file came back different")
        return "upload and download work"
    step("copy files both ways", roundtrip)
    print("\nAll good. Start FEM Tracker and queue a job." if ok else "\nSome checks failed; see above.")
    return ok


def open_path(path):
    """Show a file or folder with the desktop's default app (Notepad/Explorer, TextEdit/Finder, ...)."""
    import subprocess
    if settings.WINDOWS:
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def cmd_config():
    p = settings.ensure_config()
    print(p)
    open_path(p)


def cmd_export(argv):
    import argparse
    from femtracker import export
    ap = argparse.ArgumentParser(prog="FEMTracker.exe export", description=export.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--study", type=int, required=True, help="study number (shown in the web page's address)")
    ap.add_argument("--out", default=str(settings.default_results_dir().parent / "FEM Tracker exports"),
                    help="folder to write into; the study gets its own subfolder, replaced on each export")
    ap.add_argument("--max-file-mb", type=float, default=20, help="copy files up to this size (default 20)")
    a = ap.parse_args(argv)
    target, n = export.write_folder(settings.DB_PATH, a.study, a.out, a.max_file_mb)
    print(f"Wrote {n} files to {target}")
    print("To keep a history: make that folder (or its parent) a git repository and commit after each export.")


def cmd_import(argv):
    import argparse
    import json
    from femtracker.agent import api, populate
    ap = argparse.ArgumentParser(prog="FEMTracker.exe import",
                                 description="Add results produced outside FEM Tracker (for example with an old "
                                             "batch script) as a new job with one imported attempt.")
    ap.add_argument("folder")
    ap.add_argument("--study", type=int, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--params", default="{}", help='JSON, for example {"load_N": 2500}')
    ap.add_argument("--description", default="")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if not server_running():
        raise SystemExit("Start FEM Tracker first; importing goes through the running app.")
    try:
        params = json.loads(a.params)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--params is not valid JSON: {e}")
    job = api.call("POST", "/api/jobs", json={"study_id": a.study, "name": a.name, "description": a.description,
                                              "params": params, "queue": False})
    att = populate.import_folder(job["id"], a.folder, a.note)
    print(f"Imported {a.folder} as job {job['id']} ({a.name}), attempt {att['number']}")


def cmd_folder():
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(settings.DATA_DIR)
    open_path(settings.DATA_DIR)


def cmd_guide():
    from pathlib import Path
    guide = Path(__file__).resolve().parent / "static" / "guide.html"
    print(guide)
    webbrowser.open(guide.as_uri())


def cmd_paths():
    print(f"Program:    {settings.app_dir()}")
    print(f"Settings:   {settings.CONFIG_PATH}")
    print(f"Database:   {settings.DB_PATH}")
    print(f"Logs:       {settings.LOG_DIR}")
    print(f"Results:    {settings.LOCAL_RESULTS_ROOT}")
    print(f"Web page:   {settings.SERVER}")
    print(f"Portable:   {'yes' if settings.portable() else 'no'}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd, rest = (argv[0], argv[1:]) if argv else ("start", [])
    if settings.LOAD_ERROR:
        print(f"Settings problem: {settings.LOAD_ERROR}")
        pause_if_double_clicked()
        sys.exit(1)
    try:
        if cmd == "start":
            cmd_start(browser="--no-browser" not in rest)
        elif cmd == "server":
            cmd_start(agents=False, browser="--no-browser" not in rest)
        elif cmd == "run-agent":
            setup_logging()
            from femtracker.agent import run_agent
            run_agent.main(rest)
        elif cmd == "populate":
            setup_logging()
            from femtracker.agent import populate
            populate.main(rest)
        elif cmd == "check":
            ok = cmd_check()
            pause_if_double_clicked()
            sys.exit(0 if ok else 1)
        elif cmd == "export":
            cmd_export(rest)
        elif cmd == "import":
            setup_logging()
            cmd_import(rest)
        elif cmd == "config":
            cmd_config()
        elif cmd == "paths":
            cmd_paths()
        elif cmd == "folder":
            cmd_folder()
        elif cmd == "guide":
            cmd_guide()
        elif cmd in ("version", "--version", "-V"):
            print(f"FEM Tracker {__version__}")
        elif cmd in ("help", "--help", "-h", "/?"):
            print(__doc__)
        else:
            print(f"Unknown command: {cmd}\n{__doc__}")
            sys.exit(2)
    except ValueError as e:  # bad value in config.ini
        print(f"Settings problem: {e}")
        pause_if_double_clicked()
        sys.exit(1)
    except KeyboardInterrupt:
        pass
