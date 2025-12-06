#!/usr/bin/env python3

import atexit
import os
import shlex
import signal
import subprocess
import textwrap
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk


class RepositoryError(RuntimeError):
    """Custom exception raised for repository management errors."""


@dataclass
class RepositoryEntry:
    identifier: str
    label: str
    uri: str
    suite: str
    components: Sequence[str]
    enabled: bool
    source_type: str  # 'list' or 'sources'
    file_path: str
    line_number: Optional[int]
    section_index: Optional[int]
    options: Optional[str]
    entry_type: str
    group: str

    def subtitle(self) -> str:
        parts: List[str] = []
        if self.uri:
            parts.append(self.uri)
        if self.suite:
            parts.append(self.suite)
        if self.components:
            parts.append(" ".join(self.components))
        return " • ".join(parts)


class RepositoryManager:
    SOURCES_LIST_PATH = "/etc/apt/sources.list"
    SOURCES_DIR_PATH = "/etc/apt/sources.list.d"
    MIRRORS_URL = "https://mirrors.ubuntu.com/mirrors.txt"

    def __init__(self):
        self._entries: List[RepositoryEntry] = []

    def refresh(self) -> List[RepositoryEntry]:
        entries: List[RepositoryEntry] = []
        entries.extend(self._parse_list_file(Path(self.SOURCES_LIST_PATH)))
        entries.extend(self._parse_sources_directory(Path(self.SOURCES_DIR_PATH)))
        self._entries = entries
        return entries

    def get_entries(self) -> List[RepositoryEntry]:
        if not self._entries:
            self.refresh()
        return list(self._entries)

    def set_enabled(self, entry: RepositoryEntry, enabled: bool) -> None:
        if entry.source_type == "list":
            self._set_list_entry_enabled(entry, enabled)
        elif entry.source_type == "sources":
            self._set_sources_entry_enabled(entry, enabled)
        else:
            raise RepositoryError("Unsupported repository entry type.")

    def benchmark_mirrors(self, result_count: int = 5, sample_size: int = 12) -> List[Tuple[str, float]]:
        try:
            with urllib.request.urlopen(self.MIRRORS_URL, timeout=10) as response:
                raw = response.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # pragma: no cover - network errors should surface to UI
            raise RepositoryError(f"Unable to download Ubuntu mirrors list: {exc}") from exc

        mirrors = [
            line.strip().rstrip("/") + "/"
            for line in raw.splitlines()
            if line.strip()
        ]
        if not mirrors:
            raise RepositoryError("Ubuntu mirror inventory is empty.")

        codename = self._detect_codename()
        test_path = f"dists/{codename}/Release" if codename else "dists/stable/Release"

        results: List[Tuple[str, float]] = []
        for candidate in mirrors[:sample_size]:
            test_url = urllib.parse.urljoin(candidate, test_path)
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(test_url, timeout=5) as check:
                    check.read(1024)
            except Exception:
                continue
            elapsed = time.perf_counter() - start
            results.append((candidate, elapsed))
            if len(results) >= result_count:
                break

        if not results:
            raise RepositoryError("No mirrors responded during benchmarking.")

        results.sort(key=lambda item: item[1])
        return results

    def apply_official_mirror(self, uri: str) -> bool:
        path = Path(self.SOURCES_LIST_PATH)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise RepositoryError(f"{path} not found.") from exc
        except PermissionError as exc:
            raise RepositoryError(f"Cannot read {path}: {exc}") from exc

        changed = False
        for index, line in enumerate(lines):
            parsed = self._parse_deb_line(path, line)
            if not parsed:
                continue
            group = self._determine_group(path, parsed["uri"])
            if group != "official":
                continue
            if parsed["uri"] == uri:
                continue
            parsed["uri"] = uri
            lines[index] = self._format_deb_line(parsed)
            changed = True

        if not changed:
            return False

        content = "\n".join(lines) + "\n"
        self._write_text(path, content)
        return True

    def _parse_sources_directory(self, directory: Path) -> List[RepositoryEntry]:
        entries: List[RepositoryEntry] = []
        if not directory.exists():
            return entries
        for child in sorted(directory.iterdir()):
            name = child.name
            if name.endswith(".list"):
                entries.extend(self._parse_list_file(child))
            elif name.endswith(".sources"):
                entries.extend(self._parse_sources_file(child))
        return entries

    def _parse_list_file(self, path: Path) -> List[RepositoryEntry]:
        entries: List[RepositoryEntry] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return entries
        except PermissionError as exc:
            raise RepositoryError(f"Cannot read {path}: {exc}") from exc

        for index, line in enumerate(lines):
            parsed = self._parse_deb_line(path, line)
            if not parsed:
                continue
            group = self._determine_group(path, parsed["uri"])
            label = self._make_label(parsed["uri"], parsed["suite"])
            entry = RepositoryEntry(
                identifier=f"{path}:{index}",
                label=label,
                uri=parsed["uri"],
                suite=parsed["suite"],
                components=parsed["components"],
                enabled=not parsed["disabled"],
                source_type="list",
                file_path=str(path),
                line_number=index,
                section_index=None,
                options=parsed["options"],
                entry_type=parsed["entry_type"],
                group=group,
            )
            entries.append(entry)
        return entries

    def _parse_sources_file(self, path: Path) -> List[RepositoryEntry]:
        entries: List[RepositoryEntry] = []
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return entries
        except PermissionError as exc:
            raise RepositoryError(f"Cannot read {path}: {exc}") from exc

        sections = self._split_deb822_sections(content)
        for index, section in enumerate(sections):
            data = self._parse_deb822_section(section)
            if not data:
                continue
            types = data.get("Types") or data.get("Type") or ""
            if "deb" not in types.split():
                continue
            uri_field = data.get("URIs") or data.get("URI") or ""
            uri = uri_field.split()[0] if uri_field else ""
            suite_field = data.get("Suites") or data.get("Suite") or ""
            suite = suite_field.split()[0] if suite_field else ""
            components_field = data.get("Components") or ""
            components = components_field.split()
            enabled_text = (data.get("Enabled") or "yes").strip().lower()
            enabled = enabled_text not in {"no", "false", "0"}
            group = self._determine_group(path, uri)
            label = self._make_label(uri, suite)
            entry = RepositoryEntry(
                identifier=f"{path}::section{index}",
                label=label,
                uri=uri,
                suite=suite,
                components=components,
                enabled=enabled,
                source_type="sources",
                file_path=str(path),
                line_number=None,
                section_index=index,
                options=None,
                entry_type="deb",
                group=group,
            )
            entries.append(entry)
        return entries

    def _parse_deb_line(self, path: Path, line: str) -> Optional[Dict[str, object]]:
        stripped = line.strip()
        if not stripped:
            return None
        disabled = stripped.startswith("#")
        effective = stripped[1:].lstrip() if disabled else stripped
        if not effective.startswith("deb"):
            return None
        try:
            tokens = shlex.split(effective, comments=False, posix=True)
        except ValueError:
            return None
        if not tokens:
            return None
        try:
            entry_type, options, uri, suite, components = self._decompose_deb_tokens(tokens)
        except RepositoryError:
            return None
        if not uri:
            return None
        return {
            "entry_type": entry_type,
            "options": options,
            "uri": uri,
            "suite": suite,
            "components": components,
            "disabled": disabled,
        }

    def _decompose_deb_tokens(
        self, tokens: Sequence[str]
    ) -> Tuple[str, Optional[str], str, str, List[str]]:
        if not tokens:
            raise RepositoryError("Empty repository entry.")
        entry_type = tokens[0]
        if entry_type not in {"deb", "deb-src"}:
            raise RepositoryError("Unsupported entry type.")
        index = 1
        options: Optional[str] = None
        if index < len(tokens) and tokens[index].startswith("[") and tokens[index].endswith("]"):
            options = tokens[index]
            index += 1
        if index >= len(tokens):
            raise RepositoryError("Missing URI in repository entry.")
        uri = tokens[index]
        index += 1
        suite = tokens[index] if index < len(tokens) else ""
        if suite:
            index += 1
        components = list(tokens[index:]) if index < len(tokens) else []
        return entry_type, options, uri, suite, components

    def _format_deb_line(self, parsed: Dict[str, object]) -> str:
        parts: List[str] = [str(parsed.get("entry_type", "deb"))]
        options = parsed.get("options")
        if options:
            parts.append(str(options))
        uri = str(parsed.get("uri", "")).strip()
        if uri:
            parts.append(uri)
        suite = str(parsed.get("suite", "")).strip()
        if suite:
            parts.append(suite)
        components = parsed.get("components") or []
        for component in components:
            parts.append(str(component))
        line = " ".join(parts).strip()
        if not line:
            raise RepositoryError("Cannot build repository line.")
        if parsed.get("disabled"):
            return f"# {line}"
        return line

    def _set_list_entry_enabled(self, entry: RepositoryEntry, enabled: bool) -> None:
        path = Path(entry.file_path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise RepositoryError(f"{path} not found.") from exc
        except PermissionError as exc:
            raise RepositoryError(f"Cannot read {path}: {exc}") from exc
        if entry.line_number is None or entry.line_number >= len(lines):
            raise RepositoryError(f"Cannot locate repository entry in {path}.")
        parsed = self._parse_deb_line(path, lines[entry.line_number])
        if not parsed:
            raise RepositoryError("Failed to parse repository entry.")
        parsed["disabled"] = not enabled
        parsed["entry_type"] = entry.entry_type
        parsed["options"] = entry.options
        parsed["uri"] = entry.uri
        parsed["suite"] = entry.suite
        parsed["components"] = list(entry.components)
        lines[entry.line_number] = self._format_deb_line(parsed)
        content = "\n".join(lines) + "\n"
        self._write_text(path, content)

    def _set_sources_entry_enabled(self, entry: RepositoryEntry, enabled: bool) -> None:
        path = Path(entry.file_path)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RepositoryError(f"{path} not found.") from exc
        except PermissionError as exc:
            raise RepositoryError(f"Cannot read {path}: {exc}") from exc
        sections = self._split_deb822_sections(content)
        if not sections:
            raise RepositoryError(f"No repository blocks found in {path}.")
        index = entry.section_index or 0
        if index >= len(sections):
            raise RepositoryError("Invalid repository block index.")
        lines = sections[index].splitlines()
        updated = False
        for idx, line in enumerate(lines):
            if line.strip().lower().startswith("enabled:"):
                lines[idx] = f"Enabled: {'yes' if enabled else 'no'}"
                updated = True
                break
        if not updated:
            lines.append(f"Enabled: {'yes' if enabled else 'no'}")
        sections[index] = "\n".join(lines)
        new_content = "\n\n".join(sections)
        if content.endswith("\n"):
            new_content += "\n"
        self._write_text(path, new_content)

    def _split_deb822_sections(self, text: str) -> List[str]:
        sections: List[str] = []
        current: List[str] = []
        for raw_line in text.splitlines():
            if raw_line.strip() == "":
                if current:
                    sections.append("\n".join(current))
                    current = []
            else:
                current.append(raw_line)
        if current:
            sections.append("\n".join(current))
        return sections

    def _parse_deb822_section(self, section: str) -> Dict[str, str]:
        data: Dict[str, str] = {}
        last_key: Optional[str] = None
        for raw_line in section.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in raw_line:
                key, value = raw_line.split(":", 1)
                key = key.strip()
                value = value.strip()
                data[key] = value
                last_key = key
            elif last_key:
                # Continuation line
                data[last_key] = f"{data[last_key]} {stripped}"
        return data

    def _write_text(self, path: Path, content: str) -> None:
        try:
            with path.open("w", encoding="utf-8") as handle:
                handle.write(content)
        except PermissionError:
            self._write_text_privileged(path, content)

    def _write_text_privileged(self, path: Path, content: str) -> None:
        script = textwrap.dedent(
            f"""
            import pathlib

            target = pathlib.Path({repr(str(path))})
            data = {repr(content)}
            target.write_text(data, encoding='utf-8')
            """
        ).strip()

        last_error: Optional[BaseException] = None
        for command in (
            ["pkexec", "python3", "-c", script],
            ["sudo", "python3", "-c", script],
        ):
            try:
                subprocess.run(command, check=True)
                return
            except FileNotFoundError as exc:
                last_error = exc
                continue
            except subprocess.CalledProcessError as exc:
                last_error = exc
                continue

        raise RepositoryError(f"Elevated write to {path} failed.") from last_error

    def _make_label(self, uri: str, suite: str) -> str:
        if not uri:
            return "Unknown repository"
        try:
            parsed = urllib.parse.urlparse(uri)
        except Exception:
            parsed = urllib.parse.urlsplit(uri)
        host = parsed.netloc or uri
        if suite:
            return f"{host} — {suite}"
        return host

    def _determine_group(self, path: Path, uri: str) -> str:
        name = path.name.lower()
        if str(path) == self.SOURCES_LIST_PATH:
            return "official"
        host = urllib.parse.urlparse(uri).netloc.lower() if uri else ""
        if "ubuntu-esm" in name or "esm.ubuntu.com" in host:
            return "official"
        if "launchpad" in host:
            return "ppa"
        if host.endswith("ubuntu.com") or host.endswith("canonical.com") or ".ubuntu.com" in host:
            return "official"
        return "ppa" if "launchpad" in uri else "other"

    def _detect_codename(self) -> Optional[str]:
        os_release = Path("/etc/os-release")
        try:
            data = os_release.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        for line in data.splitlines():
            if line.startswith("VERSION_CODENAME="):
                value = line.partition("=")[2].strip().strip('"')
                if value:
                    return value
        return None

class CommandLogView(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._buffer = self._text_view.get_buffer()

        self.set_child(self._text_view)

        self._pid = None
        self._stdout_fd = None
        self._stderr_fd = None
        self._stdout_source = None
        self._stderr_source = None
        self._child_watch_id = None

    def _cleanup(self):
        if self._stdout_source is not None:
            GLib.source_remove(self._stdout_source)
            self._stdout_source = None
        if self._stderr_source is not None:
            GLib.source_remove(self._stderr_source)
            self._stderr_source = None
        if self._stdout_fd is not None:
            try:
                os.close(self._stdout_fd)
            except OSError:
                pass
            self._stdout_fd = None
        if self._stderr_fd is not None:
            try:
                os.close(self._stderr_fd)
            except OSError:
                pass
            self._stderr_fd = None
        if self._child_watch_id is not None:
            GLib.source_remove(self._child_watch_id)
            self._child_watch_id = None
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            GLib.spawn_close_pid(self._pid)
            self._pid = None

    def _append(self, text: str):
        end_iter = self._buffer.get_end_iter()
        self._buffer.insert(end_iter, text)
        self._text_view.scroll_to_iter(self._buffer.get_end_iter(), 0.0, False, 0.0, 1.0)

    def _on_fd_ready(self, fd, condition, prefix):
        if condition & GLib.IOCondition.IN:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""

            if chunk:
                decoded = chunk.decode("utf-8", errors="replace")
                self._append(decoded)
                return True

        if prefix == "stdout":
            if self._stdout_fd is not None:
                try:
                    os.close(self._stdout_fd)
                except OSError:
                    pass
                self._stdout_fd = None
            self._stdout_source = None
        else:
            if self._stderr_fd is not None:
                try:
                    os.close(self._stderr_fd)
                except OSError:
                    pass
                self._stderr_fd = None
            self._stderr_source = None
        return False

    def _on_child_exit(self, pid, status):
        if pid != self._pid:
            GLib.spawn_close_pid(pid)
            return
        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else status
        self._append(f"\n[exit status {exit_code}]\n")
        GLib.spawn_close_pid(pid)
        self._pid = None
        self._child_watch_id = None

    def run_command(self, command: str, on_error=None):
        self._cleanup()
        self._buffer.set_text("")
        self._append(f"$ {command}\n\n")

        argv = ["/bin/sh", "-c", command]
        try:
            success, pid, stdin_fd, stdout_fd, stderr_fd = GLib.spawn_async_with_pipes(
                None,
                argv,
                None,
                GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD,
                None,
                None,
            )
        except GLib.Error as exc:
            if on_error:
                on_error(f"Failed to run command: {exc.message}")
            else:
                self._append(f"Failed to run command: {exc.message}\n")
            return
        if not success:
            if on_error:
                on_error("Failed to run command.")
            else:
                self._append("Failed to run command.\n")
            return

        if stdin_fd is not None:
            os.close(stdin_fd)

        self._pid = pid
        self._stdout_fd = stdout_fd
        self._stderr_fd = stderr_fd

        self._stdout_source = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            stdout_fd,
            GLib.IOCondition.IN | GLib.IOCondition.HUP,
            self._on_fd_ready,
            "stdout",
        )
        self._stderr_source = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            stderr_fd,
            GLib.IOCondition.IN | GLib.IOCondition.HUP,
            self._on_fd_ready,
            "stderr",
        )
        self._child_watch_id = GLib.child_watch_add(
            GLib.PRIORITY_DEFAULT, pid, self._on_child_exit
        )


class UbuntuRepoManagerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._executor = ThreadPoolExecutor(max_workers=4)
        atexit.register(self._executor.shutdown, wait=False)

        self.set_title("Ubuntu Repository Manager")
        self.set_default_size(1024, 640)

        self._repo_manager = RepositoryManager()
        self._repo_switch_handlers: Dict[Gtk.Switch, int] = {}
        self._mirror_last_results: Optional[List[Tuple[str, float]]] = None
        self._benchmark_running = False

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=150,
        )
        self._stack.set_vexpand(True)

        self._sidebar = Gtk.StackSidebar(stack=self._stack)
        self._sidebar.set_size_request(220, -1)

        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(12)
        content_box.set_margin_start(12)
        content_box.set_margin_end(12)
        content_box.append(self._sidebar)
        content_box.append(self._stack)

        clamp = Adw.Clamp(child=content_box)
        clamp.set_maximum_size(1280)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(clamp)

        header_bar = Adw.HeaderBar()
        header_bar.set_show_start_title_buttons(True)
        header_bar.set_show_end_title_buttons(True)
        header_bar.set_title_widget(
            Adw.WindowTitle(title="Ubuntu Repository Manager", subtitle=None)
        )

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header_bar)
        toolbar_view.set_content(self._toast_overlay)

        self.set_content(toolbar_view)

        self._init_system_updates_page()
        self._init_repositories_page()
        self._init_placeholder_pages()
        self._install_keyboard_navigation()

    def _init_system_updates_page(self):
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        info_label = Gtk.Label(
            label="Command output will appear below (read-only log view)."
        )
        info_label.set_wrap(True)
        info_label.add_css_class("dim-label")
        page_box.append(info_label)

        self.terminal = CommandLogView()
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        self.terminal.set_margin_top(12)

        update_button = Gtk.Button(label="Run sudo apt update")
        full_upgrade_button = Gtk.Button(label="Run sudo apt full-upgrade")

        update_button.connect(
            "clicked", partial(self._spawn_command, "sudo apt update")
        )
        full_upgrade_button.connect(
            "clicked", partial(self._spawn_command, "sudo apt full-upgrade")
        )

        button_box.append(update_button)
        button_box.append(full_upgrade_button)

        page_box.append(button_box)
        page_box.append(self.terminal)

        self._stack.add_titled(
            page_box,
            "system_updates",
            "System Updates",
        )

    def _init_repositories_page(self):
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page_box.set_margin_top(12)
        page_box.set_margin_bottom(12)
        page_box.set_margin_start(12)
        page_box.set_margin_end(12)
        page_box.set_vexpand(True)
        page_box.set_hexpand(True)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header_box.set_hexpand(True)

        self._repo_status_label = Gtk.Label(label="Detecting repositories…")
        self._repo_status_label.add_css_class("dim-label")
        self._repo_status_label.set_xalign(0.0)
        header_box.append(self._repo_status_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header_box.append(spacer)

        self._repo_spinner = Gtk.Spinner()
        self._repo_spinner.set_visible(False)
        header_box.append(self._repo_spinner)

        self._repo_refresh_button = Gtk.Button(label="Refresh")
        self._repo_refresh_button.connect("clicked", self._on_refresh_repositories_clicked)
        header_box.append(self._repo_refresh_button)

        self._repo_benchmark_button = Gtk.Button(label="Benchmark Fastest Mirror")
        self._repo_benchmark_button.connect("clicked", self._on_benchmark_mirrors_clicked)
        header_box.append(self._repo_benchmark_button)

        page_box.append(header_box)

        self._mirror_results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._mirror_results_box.set_visible(False)

        mirror_label = Gtk.Label(label="")
        mirror_label.set_xalign(0.0)
        mirror_label.set_wrap(True)
        self._mirror_summary_label = mirror_label
        self._mirror_results_box.append(mirror_label)

        self._mirror_apply_button = Gtk.Button(label="Apply to Official Sources")
        self._mirror_apply_button.set_sensitive(False)
        self._mirror_apply_button.connect("clicked", self._on_apply_mirror_clicked)
        self._mirror_results_box.append(self._mirror_apply_button)

        page_box.append(self._mirror_results_box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)

        preferences_page = Adw.PreferencesPage()
        self._repo_preferences_page = preferences_page
        scroller.set_child(preferences_page)
        page_box.append(scroller)

        self._repo_groups: Dict[str, Adw.PreferencesGroup] = {}
        for group_id, title, description in [
            (
                "official",
                "Official Ubuntu Repositories",
                "Main archive, updates, security, and backports currently configured on this system.",
            ),
            (
                "ppa",
                "Launchpad PPAs",
                "Personal Package Archives detected from Launchpad.",
            ),
            (
                "other",
                "Other Third-Party Repositories",
                "Additional repositories sourced from vendor-maintained endpoints.",
            ),
        ]:
            group_widget = Adw.PreferencesGroup(title=title, description=description)
            self._repo_groups[group_id] = group_widget
            preferences_page.add(group_widget)

        self._stack.add_titled(page_box, "repositories", "Repositories")
        self._refresh_repository_view()

    def _init_placeholder_pages(self):
        for page_id, title in [
            ("auth_keys", "Authentication Keys"),
            ("foreign_packages", "Foreign Packages"),
            ("maintenance", "Maintenance & Fixes"),
        ]:
            placeholder = Adw.StatusPage(
                icon_name="applications-system-symbolic",
                title=title,
                description="Coming soon.",
            )
            self._stack.add_titled(placeholder, page_id, title)

    def _refresh_repository_view(self):
        try:
            entries = self._repo_manager.refresh()
        except RepositoryError as exc:
            self._repo_status_label.set_label("Failed to read repositories.")
            self._show_toast(str(exc))
            return
        self._render_repository_entries(entries)

    def _render_repository_entries(self, entries: Sequence[RepositoryEntry]):
        grouped: Dict[str, List[RepositoryEntry]] = {key: [] for key in self._repo_groups}
        for entry in entries:
            grouped.setdefault(entry.group, []).append(entry)

        self._repo_switch_handlers.clear()

        for group_id, group_widget in self._repo_groups.items():
            self._clear_preferences_group(group_widget)
            rows = grouped.get(group_id, [])
            if not rows:
                placeholder = Gtk.Label(label="No repositories detected.")
                placeholder.add_css_class("dim-label")
                placeholder.set_xalign(0.0)
                group_widget.add(placeholder)
                continue
            for entry in rows:
                row = self._create_repository_row(entry)
                group_widget.add(row)

        self._repo_status_label.set_label(f"{len(entries)} repositories detected")

    def _clear_preferences_group(self, group: Adw.PreferencesGroup) -> None:
        child = group.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            group.remove(child)
            child = next_child

    def _create_repository_row(self, entry: RepositoryEntry) -> Adw.ActionRow:
        title = entry.label or "Repository"
        subtitle = entry.subtitle()
        row = Adw.ActionRow(title=title, subtitle=subtitle)

        switch = Gtk.Switch()
        switch.set_active(entry.enabled)
        switch.set_valign(Gtk.Align.CENTER)
        handler_id = switch.connect("state-set", self._on_repository_switch_state_set, entry)
        self._repo_switch_handlers[switch] = handler_id

        row.add_suffix(switch)
        row.set_activatable_widget(switch)

        open_button = Gtk.Button.new_from_icon_name("document-open-symbolic")
        open_button.set_valign(Gtk.Align.CENTER)
        open_button.set_tooltip_text("Open configuration file")
        open_button.connect("clicked", self._on_open_repository_file, entry)
        row.add_suffix(open_button)

        return row

    def _on_open_repository_file(self, _button, entry: RepositoryEntry):
        try:
            file = Gio.File.new_for_path(entry.file_path)
            Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
        except GLib.Error as exc:
            self._show_toast(f"Unable to open {entry.file_path}: {exc.message}")

    def _on_refresh_repositories_clicked(self, _button):
        self._repo_status_label.set_label("Refreshing…")
        self._refresh_repository_view()

    def _submit_background(self, func, done_callback):
        future = self._executor.submit(func)

        def _handler(fut):
            GLib.idle_add(done_callback, fut)

        future.add_done_callback(_handler)
        return future

    def _on_repository_switch_state_set(self, switch: Gtk.Switch, state: bool, entry: RepositoryEntry):
        handler_id = self._repo_switch_handlers.get(switch)
        if handler_id is None:
            return False

        def work():
            self._repo_manager.set_enabled(entry, state)
            return state

        def done(future):
            switch.set_sensitive(True)
            try:
                future.result()
            except RepositoryError as exc:
                switch.handler_block(handler_id)
                switch.set_active(entry.enabled)
                switch.handler_unblock(handler_id)
                self._show_toast(str(exc))
            except Exception as exc:  # pragma: no cover - defensive fallback
                switch.handler_block(handler_id)
                switch.set_active(entry.enabled)
                switch.handler_unblock(handler_id)
                self._show_toast(f"Failed to update repository: {exc}")
            else:
                action = "Enabled" if state else "Disabled"
                self._show_toast(f"{action} {entry.label}")
                self._refresh_repository_view()

        switch.set_sensitive(False)
        self._submit_background(work, done)
        return True

    def _on_benchmark_mirrors_clicked(self, _button):
        if self._benchmark_running:
            return
        self._benchmark_running = True
        self._repo_spinner.set_visible(True)
        self._repo_spinner.start()
        self._repo_benchmark_button.set_sensitive(False)
        self._mirror_apply_button.set_sensitive(False)

        def work():
            return self._repo_manager.benchmark_mirrors()

        def done(future):
            self._benchmark_running = False
            self._repo_spinner.stop()
            self._repo_spinner.set_visible(False)
            self._repo_benchmark_button.set_sensitive(True)
            try:
                results = future.result()
            except RepositoryError as exc:
                self._mirror_results_box.set_visible(False)
                self._show_toast(str(exc))
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._mirror_results_box.set_visible(False)
                self._show_toast(f"Benchmark failed: {exc}")
            else:
                self._update_mirror_results(results)

        self._submit_background(work, done)

    def _update_mirror_results(self, results: Sequence[Tuple[str, float]]):
        self._mirror_last_results = list(results)
        if not results:
            self._mirror_results_box.set_visible(False)
            return
        summary_lines = [
            f"{index}. {uri} — {latency * 1000:.0f} ms"
            for index, (uri, latency) in enumerate(results[:5], start=1)
        ]
        self._mirror_summary_label.set_label("\n".join(summary_lines))
        self._mirror_results_box.set_visible(True)
        self._mirror_apply_button.set_sensitive(True)

    def _on_apply_mirror_clicked(self, _button):
        if not self._mirror_last_results:
            return
        best_uri = self._mirror_last_results[0][0]
        self._mirror_apply_button.set_sensitive(False)
        self._repo_spinner.set_visible(True)
        self._repo_spinner.start()

        def work():
            return self._repo_manager.apply_official_mirror(best_uri)

        def done(future):
            self._repo_spinner.stop()
            self._repo_spinner.set_visible(False)
            self._mirror_apply_button.set_sensitive(True)
            try:
                changed = future.result()
            except RepositoryError as exc:
                self._show_toast(str(exc))
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._show_toast(f"Failed to apply mirror: {exc}")
            else:
                if changed:
                    self._show_toast(f"Applied {best_uri} to official repositories.")
                else:
                    self._show_toast("Official repositories already use the selected mirror.")
                self._refresh_repository_view()

        self._submit_background(work, done)

    def _install_keyboard_navigation(self):
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(self, _controller, keyval, keycode, state):
        if (
            keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab)
            and state & Gdk.ModifierType.CONTROL_MASK
        ):
            reverse = keyval == Gdk.KEY_ISO_Left_Tab or bool(
                state & Gdk.ModifierType.SHIFT_MASK
            )
            self._switch_to_next_page(reverse=reverse)
            return True
        return False

    def _switch_to_next_page(self, reverse=False):
        pages = list(self._stack.get_pages())
        if not pages:
            return

        current = self._stack.get_visible_child()
        try:
            index = [page.get_child() for page in pages].index(current)
        except ValueError:
            index = 0

        delta = -1 if reverse else 1
        next_index = (index + delta) % len(pages)
        self._stack.set_visible_child(pages[next_index].get_child())

    def _spawn_command(self, command, _button):
        if not command:
            return

        self.terminal.run_command(command, on_error=self._show_toast)

    def _show_toast(self, message):
        toast = Adw.Toast.new(message)
        self._toast_overlay.add_toast(toast)


class UbuntuRepoManagerApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.example.UbuntuRepoManager",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = UbuntuRepoManagerWindow(application=self)
        window.present()


def main():
    app = UbuntuRepoManagerApplication()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())

