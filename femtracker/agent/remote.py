"""Talk to the HPC the same way the PuTTY batch script does: plink for commands, pscp for files.

Remote commands are sent as a script file (plink -m / ssh "bash -s"), so there is no quoting
to get wrong between Windows and Linux. Scripts always run under bash, whatever the login shell.
"""
import os
import shlex
import shutil
import subprocess
import tempfile

from femtracker import settings as config

q = shlex.quote


class RemoteError(RuntimeError):
    pass


def target():
    return f"{config.USER}@{config.HOST}"


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # don't flash a console per plink call


def _call(cmd, stdin=None, timeout=300):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise RemoteError(f"Program not found: {cmd[0]}. Check [hpc] in {config.CONFIG_PATH}")
    except subprocess.TimeoutExpired:
        raise RemoteError(f"Timed out after {timeout}s: {' '.join(cmd[:3])} ...")
    if p.returncode != 0:
        raise RemoteError((p.stderr or p.stdout).strip()[-2000:] or f"exit code {p.returncode}")
    return p.stdout


def _ssh_key_args():
    args = ["-i", config.SSH_KEY] if config.SSH_KEY else []
    if config.SSH_PORT:
        args += ["-p", config.SSH_PORT]
    return args


def _scp_port_args():
    return ["-P", config.SSH_PORT] if config.SSH_PORT else []


def _putty_port_args():
    return ["-P", config.SSH_PORT] if config.SSH_PORT else []


def run(script, timeout=300):
    """Run a bash script on the HPC and return its stdout."""
    body = script.replace("\r\n", "\n")
    if config.TRANSPORT == "local":
        return _call(["bash", "-s"], stdin=body, timeout=timeout)
    if config.TRANSPORT == "openssh":
        return _call(["ssh", "-o", "BatchMode=yes", *_ssh_key_args(), target(), "bash -s"], stdin=body, timeout=timeout)
    # PuTTY: plink -m runs a command file through the login shell; hand it straight to bash.
    wrapped = "exec bash -s <<'__FEM_EOF__'\n" + body + "\n__FEM_EOF__\n"
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", newline="\n") as fh:
            fh.write(wrapped)
        return _call([config.PLINK, "-ssh", "-batch", "-i", config.PPK, *_putty_port_args(), "-m", path, target()],
                     timeout=timeout)
    finally:
        os.remove(path)


def upload(local_files, remote_dir, timeout=3600):
    if not local_files:
        return
    if config.TRANSPORT == "local":
        for f in local_files:
            shutil.copy2(f, remote_dir)
        return
    if config.TRANSPORT == "openssh":
        key = ["-i", config.SSH_KEY] if config.SSH_KEY else []
        _call(["scp", "-q", "-o", "BatchMode=yes", *key, *_scp_port_args(), *local_files, f"{target()}:{remote_dir}/"],
             timeout=timeout)
        return
    _call([config.PSCP, "-batch", "-q", "-i", config.PPK, *_putty_port_args(), *local_files, f"{target()}:{remote_dir}/"],
         timeout=timeout)


def download(remote_file, local_dir, timeout=None):
    os.makedirs(local_dir, exist_ok=True)
    if config.TRANSPORT == "local":
        shutil.copy2(remote_file, local_dir)
        return
    if config.TRANSPORT == "openssh":
        key = ["-i", config.SSH_KEY] if config.SSH_KEY else []
        _call(["scp", "-q", "-o", "BatchMode=yes", *key, *_scp_port_args(), f"{target()}:{remote_file}", local_dir],
             timeout=timeout)
        return
    _call([config.PSCP, "-batch", "-q", "-i", config.PPK, *_putty_port_args(), f"{target()}:{remote_file}", local_dir],
         timeout=timeout)


def attempt_dir(a):
    return f"{config.REMOTE_WORKDIR.rstrip('/')}/study_{a['study_id']}/job_{a['job_id']}/attempt_{a['number']}"


def host_label():
    return "local" if config.TRANSPORT == "local" else target()
