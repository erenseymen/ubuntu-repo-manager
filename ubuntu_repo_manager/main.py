#!/usr/bin/env python3

import os
import signal
from functools import partial

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk


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

        self.set_title("Ubuntu Repository Manager")
        self.set_default_size(1024, 640)

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
        self.set_content(self._toast_overlay)

        self._init_system_updates_page()
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

    def _init_placeholder_pages(self):
        for page_id, title in [
            ("repositories", "Repositories"),
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

    def _install_keyboard_navigation(self):
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(self, _controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Tab and state & Gdk.ModifierType.CONTROL_MASK:
            self._switch_to_next_page(reverse=bool(state & Gdk.ModifierType.SHIFT_MASK))
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

