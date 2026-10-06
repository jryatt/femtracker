# FEM Tracker

Track FEM studies, run their jobs on an HPC over PuTTY (plink/pscp), and store the results:
descriptions, study conclusions, metrics and result files, all in one local database with a web page.
Running and populating the database are separate steps with their own queues.

The full user guide, with a walkthrough, scheduler recipes, a settings reference and troubleshooting,
is `femtracker/static/guide.html`. It opens from the Start menu, from the app's sidebar, or with
`FEMTracker.exe guide`.

## Install on Windows

**Installer.** Run `FEMTracker-<version>-setup.exe`. It installs for your user only, so no administrator
rights are needed; administrators can choose "all users" on the first page. It adds these Start menu entries:

| Start menu entry | What it does |
|---|---|
| **FEM Tracker** | starts the web page and both HPC agents, and opens http://127.0.0.1:8000 |
| **FEM Tracker settings** | opens `config.ini` in Notepad |
| **Test HPC connection** | checks login, remote folder, tmux and file copies |
| **FEM Tracker user guide** | the full illustrated guide (also linked from the app's sidebar) |
| **FEM Tracker data folder** | database, logs |

Optional during setup: a desktop shortcut, and starting FEM Tracker minimised when you sign in.

**Portable.** Unzip `FEMTracker-<version>-portable.zip` anywhere (a USB stick or network drive works),
edit `config.ini`, and run `FEMTracker.exe`. Settings, database and results stay in that folder.

Nothing else needs installing except **PuTTY** (plink, pscp), which you already use.

## First-time setup
1. Open **FEM Tracker settings** and fill in `[hpc]`, using the same values as your PuTTY batch scripts:
   ```ini
   [hpc]
   user = user_name
   host = Host_name
   ppk = C:\Users\me\.ssh\hpc_key.ppk
   remote_workdir = /scratch/user_name/fem_tracker
   ```
   `plink` and `pscp` are found automatically in `C:\Program Files\PuTTY`.
2. Connect once with PuTTY so the HPC's host key is cached.
3. Run **Test HPC connection**. All four lines should say `[ok]`.
4. Start **FEM Tracker**. The bottom of the left sidebar shows whether both agents are connected.

The console window that opens is FEM Tracker itself, showing its log. Closing it stops FEM Tracker.
Queued jobs simply wait, and running HPC jobs keep running; they are picked up again on the next start.

## Versions and history
Nothing is overwritten. Every run of a job is an **attempt** that freezes its parameters, run setup and
exact HPC script, and records its input files (checksums, plus copies up to `store_inputs_max_mb`,
each distinct file stored once) and the git commit of the input folder. **Run again…** makes a new
attempt with a note; **Populate again** adds a new population round and keeps the earlier one; every
edit to a study's texts and run setup is kept (**History**). `FEMTracker.exe export --study N` writes
a study as deterministic plain files (JSON, CSV, scripts, inputs, results) to commit to git.

## Notebook
Each study has a **Notebook** tab for findings and decisions written next to the runs they're about:
Markdown or HTML, with a live preview. Paste a screenshot (`Win+Shift+S` / `Cmd+Ctrl+Shift+4`) or drag
one in; `![caption](asset:<sha256>)` is inserted and stored once per checksum, like input files.
`[[job:5]]`, `[[attempt:5/2]]`, `[[result:123]]`, `[[metrics:job:5]]` and `[[git:a1b2c3d]]` (a commit
prefix, like `git log --oneline`) link to what they name, or show a "not found" marker if it's gone.
**Attach folder…** zips a whole folder of input files and attaches it as one object, content-addressed
like the images. Mark an entry **Finding** to list it under the study's results. Every save keeps a
full revision; **Archive** hides an entry without deleting it. Markdown and HTML are rendered and
sanitized on the server — the same code path for the preview, the saved view and the export — so a
shared FEM Tracker page can't be used to run a script in someone's browser.

## Study classes and KPIs
A **study class** (sidebar: **Study classes**) is a reusable template: a named kind of study with a
standard list of KPIs it should report (a name, the exact metrics field it expects, e.g. `Max_Mises`,
and a unit). Assign a class to a study and its page shows a **KPIs** box where each KPI is pinned to a
job in that study; its value is that job's current result for the expected field, read live. A pinned
job that hasn't reported that exact field shows **not reported** rather than a guess — the mechanism
that keeps reporting consistent across studies of the same class. Exports include `kpis.json`.

A class can have a **parent class** (single inheritance, like a base class): it inherits every KPI
the parent defines, and a KPI it defines itself overrides an inherited one with the same metric key.
The class editor's **Effective KPIs** table shows the full resolved set, each marked "this class" or
"inherited from …". Cycles (including a class becoming its own ancestor) are refused.

## Where things are
| | Installed | Portable |
|---|---|---|
| Settings | `%APPDATA%\FEMTracker\config.ini` | `config.ini` next to the exe |
| Database, logs | `%LOCALAPPDATA%\FEMTracker\` | `data\` |
| Downloaded results | `Documents\FEM Tracker results\` | `results\` |

`FEMTracker.exe paths` prints them. Uninstalling keeps settings, database and results. Upgrading means
running the newer installer over the old one.

## Using it
- **Study**: description, study results, and the run setup shared by its jobs. The run setup has the
  same sections as the HPC Batch Script Builder: files to upload, commands, and files to bring back.
- **Job**: JSON parameters. They are uploaded as `job_params.json` and can be used in commands as
  `{cpus}`, `{mesh_size_mm}` and so on. `{job_name}` and `{job_id}` are always available.
- **Step 1, run**: the job is uploaded to `<remote_workdir>/study_<id>/job_<id>` and started in tmux.
  It is checked every `poll_seconds`. Keep the solver in the foreground (`interactive` or a `:wait` queue).
- **Step 2, populate**: result files are downloaded and stored in the database, with metrics read from
  lines like `Max_Mises = 212.4`. Optionally the remote folder is then deleted.

Command line (the exe is in the install folder):

    FEMTracker.exe check
    FEMTracker.exe populate --job 42                          populate job 42's latest attempt again (new round)
    FEMTracker.exe import --study 1 --name "old run" C:\old\run3   load a run done outside FEM Tracker
    FEMTracker.exe export --study 1 --out C:\FEM\records      plain files for git
    FEMTracker.exe run-agent sync                             one check of running jobs
    FEMTracker.exe help

## Try it on a Mac first
Everything except the installer runs on macOS, including real HPC jobs. See
[docs/testing-on-mac.md](docs/testing-on-mac.md): run the tests, try the app with no HPC
(`transport = local`), connect with PuTTY from Homebrew or with OpenSSH, and build the Mac program
with `packaging/build.sh`.

## Build it yourself
You need Windows 10/11 with:
- [Python 3.10+](https://www.python.org/downloads/) (tick "Add python.exe to PATH");
- [Inno Setup 6](https://jrsoftware.org/isdl.php), only for the installer.

Then double-click `packaging\windows\build.bat`, or run it from a command prompt. It:
1. creates `build\venv` with the pinned versions in `packaging\windows\requirements-build.txt`;
2. freezes the app with PyInstaller (`packaging\windows\femtracker.spec`, one-folder build);
3. **starts the built exe and tests it** (web page, API, database, agents), so a broken build is not packaged;
4. writes `dist\FEMTracker-<version>-portable.zip` and, if Inno Setup is installed,
   `dist\FEMTracker-<version>-setup.exe` (`packaging\windows\installer.iss`).

Without a Windows PC, use GitHub: push the project and run **Actions → Windows build**.
Pushing a tag such as `v1.0.0` also attaches both files to a release.

To release a new version, change `__version__` in `femtracker/__init__.py`; the exe, installer and
file names all follow it.

**Antivirus / SmartScreen.** Unsigned exes built with PyInstaller can trigger "Windows protected your PC"
or a false alarm from a company scanner. If you have a code-signing certificate, sign both files after
building:

    signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f cert.pfx /p <password> build\dist\FEMTracker\FEMTracker.exe
    (rebuild the installer with ISCC, then sign dist\FEMTracker-<version>-setup.exe the same way)

Otherwise, ask IT to allow-list the install folder.

## Run from source (development)
    python -m venv .venv && .venv\Scripts\activate
    pip install -e .[test]
    femtracker start          (or: python -m femtracker start)

## Tests
    pytest tests

The tests run a real server and drive the agents' plink/pscp code paths against a fake HPC
(stand-in plink, pscp and Abaqus scripts), so they need bash: run them on Linux, macOS or WSL.
GitHub runs them on every push (`.github/workflows/tests.yml`).

## Project layout
    femtracker/cli.py            FEMTracker.exe commands
    femtracker/settings.py       config.ini (defaults and documentation in DEFAULT_INI)
    femtracker/server.py         web API and SQLite database (schema and versioning model at the top)
    femtracker/export.py         study export for git
    femtracker/notebook.py       notebook Markdown/HTML rendering and sanitizing (one path for all three uses)
    femtracker/static/           web page
    femtracker/agent/            run agent (step 1), populate agent (step 2), PuTTY/SSH wrapper
    packaging/windows/           build.bat, build.py, PyInstaller spec, Inno Setup script, icon
    packaging/build.sh           build and test on macOS or Linux
    docs/testing-on-mac.md       trying everything on a Mac before the Windows build
    tests/                       end-to-end tests with a fake HPC
    .github/workflows/           tests on every push; Windows build on demand or on a version tag
