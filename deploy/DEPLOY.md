# Deploying the Basanos collector

A general, host-agnostic runbook for running the collector as a systemd
service on a fresh Ubuntu box. No step below names a specific host, IP, or
box-specific secret -- there isn't one to name (this is a read-only
collector making outbound GET requests; it holds no credentials). Where a
step is host-specific (SSH access, firewall, DNS), that's the operator's
own setup and stays out of this repo.

## 1. Prerequisites (fresh box)

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
```

Confirm versions (3.9+ is enough; this project was built and tested
against 3.14, but nothing in it is version-specific):

```bash
python3 --version
git --version
```

## 2. Create the service user

Non-root, no login shell, no interactive use -- exactly what the unit
file's `User=`/`Group=` expect.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin basanos
```

`--create-home` gives it `/home/basanos` (unused by the collector, just a
sane default); the actual code and data live elsewhere, set up next.

## 3. Clone the repo

Pick a parent directory the service user can read (root-owned is fine,
since ProtectSystem=strict in the unit only needs the tree readable, not
writable, outside the data dir):

```bash
sudo mkdir -p /opt/basanos
sudo git clone https://github.com/midasbal/Basanos.git /opt/basanos/Basanos
sudo chown -R basanos:basanos /opt/basanos
```

(`git clone <url> <dir>` names the destination explicitly here so the
result is always `/opt/basanos/Basanos` regardless of what the repo is
called locally -- matching `deploy/basanos.service.example`'s
`WorkingDirectory`.)

## 4. Virtualenv + install, with normal TLS

```bash
sudo -u basanos python3 -m venv /opt/basanos/Basanos/.venv
sudo -u basanos /opt/basanos/Basanos/.venv/bin/pip install --upgrade pip
sudo -u basanos /opt/basanos/Basanos/.venv/bin/pip install -r /opt/basanos/Basanos/requirements.txt
sudo -u basanos /opt/basanos/Basanos/.venv/bin/pip install pytest
```

No `--trusted-host` flags, no disabled certificate verification --
those were only ever needed inside this project's own sandboxed
*development* environment, which sat behind a proxy that intercepted
TLS to PyPI. A normal Ubuntu box talking directly to the public internet
doesn't have that problem; if `pip install` fails here, that's a real
network/TLS issue on this box worth investigating properly, not something
to route around with `--trusted-host`.

`pytest` is a one-time sanity-check dependency (step 5), not something
the running service needs -- it's fine left installed in the venv, but if
you'd rather keep the deployed venv minimal, `pip uninstall pytest`
after step 5 passes.

## 5. Run the test suite once, as a sanity check

Before trusting this checkout to run unattended:

```bash
cd /opt/basanos/Basanos
sudo -u basanos /opt/basanos/Basanos/.venv/bin/python -m pytest tests/ -v
```

All tests should pass (99 at the time of writing). This only exercises
the collector's own logic against fixtures -- it makes no network calls
and touches nothing outside a pytest tmp_path, so it's safe to run as
the unprivileged service user before the data directory even exists.

If anything fails here, stop and fix it before continuing -- installing
the service on top of a checkout that fails its own tests just moves the
failure to production.

## 6. Create the data directory

Owned by the service user, and the only path the hardened unit is allowed
to write to (`ReadWritePaths=` in the unit file):

```bash
sudo mkdir -p /var/lib/basanos
sudo chown basanos:basanos /var/lib/basanos
```

## 7. Install, enable, and start the unit

```bash
sudo cp /opt/basanos/Basanos/deploy/basanos.service.example /etc/systemd/system/basanos.service
```

Open `/etc/systemd/system/basanos.service` and confirm the placeholders
match what you actually set up above (`User=`, `WorkingDirectory=`,
`ExecStart=`'s venv path, `ReadWritePaths=`) -- they're written to match
this runbook's own paths exactly, so if you followed steps 1-6 as
written, nothing needs to change.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now basanos
```

## 8. Verify it's actually running and collecting

```bash
sudo systemctl status basanos
```

Expect `active (running)`. Then watch the logs for a few seconds:

```bash
sudo journalctl -u basanos -f
```

You should see the startup line (`starting continuous collection
against https://technocore.chat ...`) and no repeated tracebacks. `Ctrl-C`
to stop following.

Finally, confirm data is actually landing:

```bash
ls -la /var/lib/basanos
ls -la /var/lib/basanos/rooms/lobby
```

`data/rooms_snapshots.jsonl`, `data/rooms/events/messages.jsonl`, and
`data/rooms/lobby/messages.jsonl` should all exist and grow over the next
few minutes (`wc -l` a file, wait a minute, `wc -l` it again). If they
don't grow, check `journalctl -u basanos` for failure records before
assuming the collector itself is broken -- the failure/gap/coverage
files (`data/failures.jsonl`, `data/gaps.jsonl`, `data/coverage.jsonl`)
are built specifically to make "why isn't this collecting" answerable
without guessing.

## Stopping / restarting

```bash
sudo systemctl stop basanos     # sends SIGTERM; the collector catches it,
                                 # writes a session-stop record to
                                 # data/sessions.jsonl, and exits cleanly
sudo systemctl restart basanos
```

## Config knobs and their defaults

Every knob below is a CLI flag (`collector/cli.py`) backed by a `Config`
field (`collector/config.py`); the unit file only overrides `--data-dir`
(required, since `ProtectSystem=strict` makes anywhere else read-only).
Add any of these to `ExecStart=` in the unit to change them.

| Flag | Default | What it controls |
|---|---|---|
| `--rooms` | `lobby` | Which rooms to follow message-by-message (space-separated for more than one). The room-discovery log (`/r/events`) is always followed regardless. |
| `--message-interval` | `5` s | How often the message-room/events drain cadence fires. |
| `--snapshot-interval` | `300` s | How often the whole-commons `/rooms` snapshot fires. |
| `--message-timeout` | `2.5` s | Per-attempt HTTP timeout for a single message-room fetch. |
| `--message-max-attempts` | `4` | Total attempts (including the first) before a message-room fetch gives up and records a failure. Worst-case block for one fetch: `4 * 2.5s + 3 * 1.0s = 13.0s` (see `collector/config.py`'s comment for the full derivation against lobby's ~20s ring cycle). |
| `--message-backoff-cap` | `1.0` s | Ceiling on a message-room fetch's inter-attempt/429 backoff sleep. |
| `--snapshot-timeout` | `4.0` s | Per-attempt HTTP timeout for the `/rooms` snapshot. |
| `--snapshot-max-attempts` | `2` | Total attempts before the snapshot gives up. Worst case: `2 * 4.0s + 1 * 1.0s = 9.0s`. |
| `--snapshot-backoff-cap` | `1.0` s | Ceiling on the snapshot's inter-attempt/429 backoff sleep. |
| `--data-dir` | `data` (relative) | Where everything gets written. The unit file sets this to `/var/lib/basanos` explicitly. |
| `--base-url` | `https://technocore.chat` | The service to collect from. |

**The message/snapshot timeouts above are starting points, not a final
tuning.** They were derived from one confirmed live incident (a stalled
fetch blocking the single-threaded loop for minutes) and reasoned
defensively from there, not from sustained production telemetry on this
specific box's actual network path to the service. Once this unit has
been running for a while, watch `data/coverage.jsonl` (the `coverage`
field per room, and the `__all__` rollup) and `data/failures.jsonl`: if
failures are frequent and coverage is suffering because the timeouts are
too tight for this box's real latency, loosen `--message-timeout`/
`--snapshot-timeout` a little; if coverage looks fine and failures are
rare, the defaults are probably already adequate and there's no reason
to touch them. Tune from that evidence, not from guessing.
