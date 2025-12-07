#!/usr/bin/env python3
"""Terminal widget module for displaying command output with search and control features."""

import os
import signal
from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk


class TerminalWidget(Gtk.Frame):
    """A complete terminal widget with header bar, search functionality, and command execution.
    
    Features:
    - Header bar with title, cancel, copy, and close buttons
    - Search bar with Ctrl+F toggle, match count, and navigation (prev/next)
    - Streaming command output
    - Ability to cancel running commands
    """

    def __init__(self, title: str = "Terminal", show_close_button: bool = True):
        """Initialize the terminal widget.
        
        Args:
            title: Title shown in the header bar
            show_close_button: Whether to show the close button (X)
        """
        super().__init__()
        self.add_css_class("view")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._title = title
        self._on_close_callback = None
        self._on_command_done_callback = None

        # Process state
        self._pid = None
        self._stdout_fd = None
        self._stderr_fd = None
        self._stdout_source = None
        self._stderr_source = None
        self._child_watch_id = None

        # Search state
        self._search_matches: List[Tuple[int, int]] = []
        self._current_match_index = -1
        self._search_tag = None

        # Build UI
        self._build_ui(show_close_button)
        self._setup_keyboard_shortcuts()

    def _build_ui(self, show_close_button: bool):
        """Build the complete terminal UI."""
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # === Header bar ===
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(6)
        header.set_margin_bottom(6)
        header.set_margin_start(12)
        header.set_margin_end(6)

        # Title
        self._title_label = Gtk.Label(label=self._title)
        self._title_label.set_xalign(0.0)
        self._title_label.add_css_class("heading")
        header.append(self._title_label)

        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)

        # Cancel button (stops running command)
        self._cancel_button = Gtk.Button.new_from_icon_name("process-stop-symbolic")
        self._cancel_button.set_valign(Gtk.Align.CENTER)
        self._cancel_button.set_tooltip_text("Cancel running command")
        self._cancel_button.add_css_class("flat")
        self._cancel_button.set_sensitive(False)
        self._cancel_button.connect("clicked", self._on_cancel_clicked)
        header.append(self._cancel_button)

        # Search button
        self._search_button = Gtk.ToggleButton()
        self._search_button.set_icon_name("system-search-symbolic")
        self._search_button.set_valign(Gtk.Align.CENTER)
        self._search_button.set_tooltip_text("Search (Ctrl+F)")
        self._search_button.add_css_class("flat")
        self._search_button.connect("toggled", self._on_search_toggled)
        header.append(self._search_button)

        # Copy button
        self._copy_button = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        self._copy_button.set_valign(Gtk.Align.CENTER)
        self._copy_button.set_tooltip_text("Copy terminal output")
        self._copy_button.add_css_class("flat")
        self._copy_button.connect("clicked", self._on_copy_clicked)
        header.append(self._copy_button)

        # Close button
        if show_close_button:
            self._close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
            self._close_button.set_valign(Gtk.Align.CENTER)
            self._close_button.set_tooltip_text("Close terminal")
            self._close_button.add_css_class("flat")
            self._close_button.connect("clicked", self._on_close_clicked)
            header.append(self._close_button)
        else:
            self._close_button = None

        main_box.append(header)

        # Separator after header
        main_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # === Search bar (hidden by default) ===
        self._search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._search_bar.set_margin_top(6)
        self._search_bar.set_margin_bottom(6)
        self._search_bar.set_margin_start(12)
        self._search_bar.set_margin_end(12)
        self._search_bar.set_visible(False)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search in output…")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("activate", self._on_search_next)
        self._search_entry.connect("stop-search", self._on_search_close)
        self._search_bar.append(self._search_entry)

        # Match count label
        self._match_label = Gtk.Label(label="")
        self._match_label.add_css_class("dim-label")
        self._search_bar.append(self._match_label)

        # Previous match button
        prev_button = Gtk.Button.new_from_icon_name("go-up-symbolic")
        prev_button.set_tooltip_text("Previous match")
        prev_button.add_css_class("flat")
        prev_button.connect("clicked", self._on_search_prev)
        self._search_bar.append(prev_button)

        # Next match button
        next_button = Gtk.Button.new_from_icon_name("go-down-symbolic")
        next_button.set_tooltip_text("Next match")
        next_button.add_css_class("flat")
        next_button.connect("clicked", self._on_search_next)
        self._search_bar.append(next_button)

        # Close search button
        close_search_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_search_btn.set_tooltip_text("Close search")
        close_search_btn.add_css_class("flat")
        close_search_btn.connect("clicked", self._on_search_close)
        self._search_bar.append(close_search_btn)

        main_box.append(self._search_bar)

        # === Terminal output area ===
        self._scrolled_window = Gtk.ScrolledWindow()
        self._scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scrolled_window.set_vexpand(True)

        self._text_view = Gtk.TextView()
        self._text_view.set_editable(False)
        self._text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._text_view.set_monospace(True)
        self._text_view.set_left_margin(12)
        self._text_view.set_right_margin(12)
        self._text_view.set_top_margin(8)
        self._text_view.set_bottom_margin(8)
        self._buffer = self._text_view.get_buffer()

        # Create search highlight tag
        self._search_tag = self._buffer.create_tag(
            "search-highlight",
            background="yellow",
            foreground="black"
        )
        self._current_match_tag = self._buffer.create_tag(
            "current-match",
            background="orange",
            foreground="black"
        )

        self._scrolled_window.set_child(self._text_view)
        main_box.append(self._scrolled_window)

        self.set_child(main_box)

    def _setup_keyboard_shortcuts(self):
        """Setup keyboard shortcuts (Ctrl+F for search)."""
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        """Handle key press events."""
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_f or keyval == Gdk.KEY_F:
                self.toggle_search()
                return True
        if keyval == Gdk.KEY_Escape:
            if self._search_bar.get_visible():
                self._close_search()
                return True
        return False

    # === Public API ===

    def set_title(self, title: str):
        """Set the terminal title."""
        self._title = title
        self._title_label.set_label(title)

    def set_on_close(self, callback):
        """Set callback for when close button is clicked."""
        self._on_close_callback = callback

    def set_on_command_done(self, callback):
        """Set callback for when command finishes."""
        self._on_command_done_callback = callback

    def toggle_search(self):
        """Toggle the search bar visibility."""
        is_visible = self._search_bar.get_visible()
        self._search_bar.set_visible(not is_visible)
        self._search_button.set_active(not is_visible)
        if not is_visible:
            self._search_entry.grab_focus()
        else:
            self._clear_search_highlights()

    def append_text(self, text: str):
        """Append text to the terminal output."""
        end_iter = self._buffer.get_end_iter()
        self._buffer.insert(end_iter, text)
        self._text_view.scroll_to_iter(self._buffer.get_end_iter(), 0.0, False, 0.0, 1.0)

    def clear(self):
        """Clear the terminal output."""
        self._buffer.set_text("")
        self._clear_search_highlights()

    def get_text(self) -> str:
        """Get all text from the terminal."""
        start = self._buffer.get_start_iter()
        end = self._buffer.get_end_iter()
        return self._buffer.get_text(start, end, True)

    def is_running(self) -> bool:
        """Check if a command is currently running."""
        return self._pid is not None

    def cancel(self):
        """Cancel the currently running command."""
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._append("[cancelled]\n")

    def run_command(self, command: str, on_error=None):
        """Run a shell command and stream output to the terminal.
        
        Args:
            command: Shell command to execute
            on_error: Optional callback for error messages
        """
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
            self._update_cancel_button()
            return
        if not success:
            if on_error:
                on_error("Failed to run command.")
            else:
                self._append("Failed to run command.\n")
            self._update_cancel_button()
            return

        if stdin_fd is not None:
            os.close(stdin_fd)

        self._pid = pid
        self._stdout_fd = stdout_fd
        self._stderr_fd = stderr_fd
        self._update_cancel_button()

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

    # === Internal methods ===

    def _update_cancel_button(self):
        """Update cancel button sensitivity based on running state."""
        self._cancel_button.set_sensitive(self._pid is not None)

    def _cleanup(self):
        """Clean up process resources."""
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
        self._update_cancel_button()

    def _append(self, text: str):
        """Append text to buffer and scroll to bottom."""
        end_iter = self._buffer.get_end_iter()
        self._buffer.insert(end_iter, text)
        self._text_view.scroll_to_iter(self._buffer.get_end_iter(), 0.0, False, 0.0, 1.0)

    def _on_fd_ready(self, fd, condition, prefix):
        """Handle data ready on stdout/stderr."""
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
        """Handle child process exit."""
        if pid != self._pid:
            GLib.spawn_close_pid(pid)
            return
        exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else status
        self._append(f"\n[exit status {exit_code}]\n")
        GLib.spawn_close_pid(pid)
        self._pid = None
        self._child_watch_id = None
        self._update_cancel_button()
        if self._on_command_done_callback:
            self._on_command_done_callback(exit_code)

    # === Button handlers ===

    def _on_cancel_clicked(self, button):
        """Handle cancel button click."""
        self.cancel()

    def _on_copy_clicked(self, button):
        """Handle copy button click."""
        text = self.get_text()
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)

    def _on_close_clicked(self, button):
        """Handle close button click."""
        if self._on_close_callback:
            self._on_close_callback()
        else:
            self.set_visible(False)

    def _on_search_toggled(self, button):
        """Handle search toggle button."""
        is_active = button.get_active()
        self._search_bar.set_visible(is_active)
        if is_active:
            self._search_entry.grab_focus()
        else:
            self._clear_search_highlights()

    # === Search functionality ===

    def _on_search_changed(self, entry):
        """Handle search text changes."""
        query = entry.get_text()
        self._perform_search(query)

    def _on_search_next(self, *args):
        """Go to next search match."""
        if not self._search_matches:
            return
        self._current_match_index = (self._current_match_index + 1) % len(self._search_matches)
        self._highlight_current_match()

    def _on_search_prev(self, *args):
        """Go to previous search match."""
        if not self._search_matches:
            return
        self._current_match_index = (self._current_match_index - 1) % len(self._search_matches)
        self._highlight_current_match()

    def _on_search_close(self, *args):
        """Close the search bar."""
        self._close_search()

    def _close_search(self):
        """Close search bar and clear highlights."""
        self._search_bar.set_visible(False)
        self._search_button.set_active(False)
        self._clear_search_highlights()

    def _perform_search(self, query: str):
        """Perform text search and highlight matches."""
        self._clear_search_highlights()
        self._search_matches = []
        self._current_match_index = -1

        if not query:
            self._match_label.set_label("")
            return

        # Find all matches
        start_iter = self._buffer.get_start_iter()
        while True:
            result = start_iter.forward_search(
                query,
                Gtk.TextSearchFlags.CASE_INSENSITIVE,
                None
            )
            if result is None:
                break
            match_start, match_end = result
            self._search_matches.append((match_start.get_offset(), match_end.get_offset()))
            self._buffer.apply_tag(self._search_tag, match_start, match_end)
            start_iter = match_end

        # Update match count
        count = len(self._search_matches)
        if count == 0:
            self._match_label.set_label("No matches")
        else:
            self._match_label.set_label(f"{count} match{'es' if count != 1 else ''}")
            self._current_match_index = 0
            self._highlight_current_match()

    def _highlight_current_match(self):
        """Highlight and scroll to current match."""
        if not self._search_matches or self._current_match_index < 0:
            return

        # Remove old current match highlight
        start = self._buffer.get_start_iter()
        end = self._buffer.get_end_iter()
        self._buffer.remove_tag(self._current_match_tag, start, end)

        # Apply current match highlight
        offset_start, offset_end = self._search_matches[self._current_match_index]
        match_start = self._buffer.get_iter_at_offset(offset_start)
        match_end = self._buffer.get_iter_at_offset(offset_end)
        self._buffer.apply_tag(self._current_match_tag, match_start, match_end)

        # Scroll to match
        self._text_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.5)

        # Update label
        total = len(self._search_matches)
        current = self._current_match_index + 1
        self._match_label.set_label(f"{current} of {total}")

    def _clear_search_highlights(self):
        """Clear all search highlights."""
        start = self._buffer.get_start_iter()
        end = self._buffer.get_end_iter()
        self._buffer.remove_tag(self._search_tag, start, end)
        self._buffer.remove_tag(self._current_match_tag, start, end)
        self._search_matches = []
        self._current_match_index = -1
        self._match_label.set_label("")


# Backwards compatibility alias
CommandLogView = TerminalWidget

