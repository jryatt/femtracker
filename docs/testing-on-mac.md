# Testing FEM Tracker on a Mac

Everything except the Windows installer runs on macOS, so you can try the whole workflow on a Mac
(including real jobs on your HPC) before building for Windows.

## 1. What you need

    xcode-select --install          # git and command-line tools, if you don't have them
    brew install python@3.12        # macOS's own python3 is too old (3.9)
    brew install putty              # optional: plink/pscp/puttygen, to test exactly the Windows path

## 2. Run the tests (5 minutes, no HPC needed)

    cd fem-tracker
    python3.12 -m venv .venv && source .venv/bin/activate
    pip install -e ".[test]"
    pytest tests -q

The tests start a real server and drive the agents against a fake HPC on your Mac: plink, pscp and
Abaqus stand-ins, with real bash and real files. All of them should pass.

## 3. Try the app without an HPC

    femtracker start                # opens http://127.0.0.1:8000 and creates the settings file
    femtracker config               # opens ~/.config/FEMTracker/config.ini in TextEdit

Set this in `config.ini`, then restart `femtracker start`:

    [hpc]
    transport = local
    remote_workdir = /Users/<you>/fem_scratch/runs

With `transport = local` the "HPC" is your Mac. Jobs still go through tmux (or nohup), heartbeats,
population, versioning and export exactly as they would on the cluster. A study whose commands don't
need a solver:

| Run setup field | Value |
|---|---|
| Local input folder | `/Users/<you>/fem_scratch/inputs` (any folder with a file in it, ideally a git repository) |
| Files to upload | `*` |
| Commands | `sleep 20` and `python3 -c "import json; p=json.load(open('job_params.json')); open('Post_Results.txt','w').write('Max_Mises = %s\n' % (p['load_N'] * 0.46))"` |
| Files to bring back | `Post_Results.txt` |

Queue a job with parameters `{"load_N": 1000}`, and watch it go through every stage. Then try
**Run again…**, **Compare attempts**, **Populate again**, **Cancel run**, **History** and **Export**.

Open the study's **Notebook** tab and try it too: write an entry, paste a screenshot
(`Cmd+Ctrl+Shift+4` on a Mac) or drag an image file in, and link to the job you just ran with
`[[job:1]]`. Mark it a **Finding** and it shows up under the study's results; **Export** picks it up
in a `notebook/` folder alongside the run.

## 4. Connect to your real HPC

Pick one; both are fully supported.

**PuTTY, the same tools as on Windows** (`brew install putty`):

    [hpc]
    transport = putty
    user = <you>
    host = <hpc login address>
    ppk = /Users/<you>/.ssh/hpc_key.ppk      # the same .ppk you use on Windows
    remote_workdir = /scratch/<you>/fem_tracker

Then accept the host key once, by logging in interactively: `plink -i ~/.ssh/hpc_key.ppk <you>@<host>`,
answer `y`, then `exit`. plink and pscp are found automatically in Homebrew's folder.

**OpenSSH, built into macOS**:

    [hpc]
    transport = openssh
    user = <you>
    host = <hpc login address>
    ssh_key = /Users/<you>/.ssh/hpc_key     # an OpenSSH key; convert a .ppk with:
                                             # puttygen hpc_key.ppk -O private-openssh -o ~/.ssh/hpc_key
    remote_workdir = /scratch/<you>/fem_tracker

Log in once with `ssh -i ~/.ssh/hpc_key <you>@<host>` to accept the host key. Keys held by
`ssh-agent` work too.

Then run `femtracker check`. All four lines should say `[ok]`. Set the study's local input folder to
a Mac path (for example `/Users/<you>/FEM/Bracket`); `C:\...` paths don't exist on a Mac.

## 5. Test the built program too

    packaging/build.sh              # tests, then builds build/dist/FEMTracker/FEMTracker and a zip
    build/dist/FEMTracker/FEMTracker start

This is the same PyInstaller build as on Windows, so it checks the bundling (hidden imports, the web
page and guide inside the program) as well as the code. It uses the same settings file as the source
version. GitHub's **Tests** workflow also builds it and offers it as a download (`FEMTracker-macos`).

A zip downloaded from GitHub is quarantined by macOS ("cannot be opened because the developer cannot
be verified"). After unzipping, clear that once: `xattr -dr com.apple.quarantine FEMTracker`.
A program you built yourself isn't affected.

## What a Mac test does and doesn't cover

| Covered on a Mac | Only testable on Windows |
|---|---|
| The web page, database, versioning, history, export | The installer, Start menu entries, uninstall |
| Talking to your real HPC: login, uploads, tmux, heartbeats, cancelling, cleanup | `%APPDATA%` / `%LOCALAPPDATA%` locations, Notepad/Explorer opening |
| Your study's real commands, queues and result files | `C:\` input paths and Windows file names |
| plink/pscp with your `.ppk` (with `brew install putty`) | PuTTY's Windows `.exe` paths and the console window |
| The PyInstaller bundle | Windows file properties, SmartScreen and antivirus reactions |

So a good plan is: run everything on the Mac against your real HPC until a study works end to end,
then on Windows do one install and one job to check the Windows-only column.
