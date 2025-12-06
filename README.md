# Ubuntu Repository Manager

Ubuntu Repository Manager is a modern GTK 4 / libadwaita desktop application for Ubuntu GNOME that brings all of the repository management and recovery workflows you would expect from Linux Mint's [mintsources](https://github.com/linuxmint/mintsources) tool – refreshed with a native GNOME interface and expanded diagnostics for common `apt` errors.

## Highlights

- **Official sources control** – toggle `main`, `restricted`, `universe`, and `multiverse`, plus security, updates, backports, and proposed channels without touching `/etc/apt/sources.list`.
- **Mirror benchmarking** – retrieve Ubuntu's live mirror inventory, compare health metrics, and switch archive mirrors in one click.
- **PPA management** – add Launchpad PPAs with validation, and remove them cleanly alongside their keyring entries.
- **Third-party repositories** – append, disable, or purge extra `deb` lines managed in their own `sources.list.d` file.
- **Authentication keys** – browse the current trusted keyring, import fingerprints from the Ubuntu keyserver, or remove stale keys.
- **Foreign packages** – audit packages that do not originate from Ubuntu or Canonical archives and purge them individually.
- **Workspace tabs navigation** – left-aligned tab sidebar keeps major workflows a `Ctrl+Tab` away while preserving GNOME window chrome.
- **Maintenance dashboard** – run guided fixes for `apt-get update`, `dpkg --configure -a`, `apt-get install -f`, cache cleaning, autoremove, and default-source restoration.
- **Common error remediation** – the log view annotates `apt` failures (missing keys, dpkg lock, hash mismatch, etc.) with suggested commands and next steps.

## Getting Started

### Prerequisites

Ubuntu Repository Manager requires Python 3 and GTK 4 / libadwaita. Install the necessary dependencies on Ubuntu:

```bash
sudo apt update
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
```

### Running the Application

To start the application, run:

```bash
python3 ubuntu_repo_manager/main.py
```

**Note:** Some operations (such as modifying repository configurations) require administrator privileges. The application will prompt for authentication using `pkexec` or `sudo` when needed.

## UI Layout

Five primary tabs sit in a vertical rail on the left. Keyboard users can cycle through them with `Ctrl+Tab`.

- **System Updates** – two side-by-side buttons trigger `sudo apt update` and `sudo apt full-upgrade`. Each launch streams output into the embedded terminal pane. When a command reports problems, the app surfaces a follow-up fix suggestion inline.
- **Repositories** – groups official sources, Launchpad PPAs, and other third-party entries in a single list. All repository management actions—including mirror benchmarking and fastest-server selection—live here.
- **Authentication Keys** – hosts the keyring browser with import and removal controls.
- **Foreign Packages** – audits non-Ubuntu/Canonical packages and allows selective removal.
- **Maintenance & Fixes** – offers the guided recovery commands as buttons. Hover tooltips describe each repair before execution, and the embedded terminal captures output. If a run indicates additional remediation, the UI prompts for confirmation before proceeding.

## Common Fix Recipes

| Symptom | Suggested fix |
| --- | --- |
| Missing public key (`NO_PUBKEY`) | Import fingerprint via `gpg --recv-keys` (automated from the Maintenance page). |
| `dpkg was interrupted` | Run `dpkg --configure -a`, then `apt-get install -f`. |
| Unable to acquire the dpkg frontend lock | Close conflicting package managers, remove stale lock files. |
| Hash sum mismatch | Clean `/var/lib/apt/lists`, refresh mirrors, re-run `apt-get update`. |
| Untrusted package prompt | Import the repository's signing key or disable the repository from the Additional Repositories page. |

Every automated action writes to the in-app logs alongside remediation hints referencing the commands above, so you can copy-paste them into a terminal if you prefer running them manually.

## License

GPL-3.0-or-later – compatible with the original mintsources licensing model so downstream distributions can ship the tool alongside Ubuntu.

