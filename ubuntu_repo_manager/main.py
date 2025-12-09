#!/usr/bin/env python3

import sys
from pathlib import Path

# Add parent directory to Python path to allow imports when running as script
_parent_dir = Path(__file__).parent.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

import atexit
import json
import os
import random
import shlex
import signal
import subprocess
import textwrap
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
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

from ubuntu_repo_manager.terminal import TerminalWidget


class RepositoryError(RuntimeError):
    """Custom exception raised for repository management errors."""


def country_code_to_flag(code: str) -> str:
    """Convert a 2-letter country code to a flag emoji.
    
    Example: 'TR' -> '🇹🇷', 'US' -> '🇺🇸'
    """
    if not code or len(code) != 2:
        return ""
    code = code.upper()
    try:
        # Regional indicator symbols start at U+1F1E6 for 'A'
        return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code)
    except (ValueError, TypeError):
        return ""


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
    CACHE_DIR = os.path.expanduser("~/.cache/ubuntu-repo-manager")
    MIRROR_CACHE_FILE = os.path.join(CACHE_DIR, "mirror_benchmark.json")

    def __init__(self):
        self._entries: List[RepositoryEntry] = []
        self._ensure_cache_dir()

    def _ensure_cache_dir(self):
        """Create cache directory if it doesn't exist."""
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    def save_mirror_cache(self, results: List[Tuple[str, str, float, float]]) -> None:
        """Save benchmark results to cache file.
        
        Args:
            results: List of (url, hostname, ping_latency, download_latency) tuples
        """
        try:
            cache_data = {
                "timestamp": time.time(),
                "results": [
                    {"url": url, "hostname": hostname, "ping": ping, "download": dl}
                    for url, hostname, ping, dl in results
                ]
            }
            with open(self.MIRROR_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)
        except Exception:
            pass  # Silently fail if can't write cache

    def load_mirror_cache(self) -> Optional[List[Tuple[str, str, float, float]]]:
        """Load benchmark results from cache file.
        
        Returns:
            List of (url, hostname, ping_latency, download_latency) tuples, or None if no cache
        """
        try:
            if not os.path.exists(self.MIRROR_CACHE_FILE):
                return None
            with open(self.MIRROR_CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            results = [
                (item["url"], item["hostname"], item["ping"], item["download"])
                for item in cache_data.get("results", [])
            ]
            return results if results else None
        except Exception:
            return None

    def get_cache_age(self) -> Optional[float]:
        """Get the age of the cache in seconds, or None if no cache."""
        try:
            if not os.path.exists(self.MIRROR_CACHE_FILE):
                return None
            with open(self.MIRROR_CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            timestamp = cache_data.get("timestamp", 0)
            return time.time() - timestamp
        except Exception:
            return None

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

    def _ping_host(self, hostname: str) -> Optional[float]:
        """Run ping test and return average latency in seconds, or None if failed."""
        try:
            result = subprocess.run(
                ["ping", "-q", "-c", "2", "-W", "1", "-i", "0.5", hostname],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            # Parse: rtt min/avg/max/mdev = 14.532/15.679/16.826/1.147 ms
            for line in result.stdout.splitlines():
                if "rtt" in line or "round-trip" in line:
                    parts = line.split("=")
                    if len(parts) >= 2:
                        values = parts[1].strip().split("/")
                        if len(values) >= 2:
                            return float(values[1]) / 1000.0  # Convert ms to seconds
            return None
        except Exception:
            return None

    def benchmark_mirrors(
        self,
        progress_callback: Optional[callable] = None,
        preferred_country: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        def report(msg: str):
            if progress_callback:
                progress_callback(msg)

        report("Loading Ubuntu mirror database…\n")
        
        # Use python-apt's built-in mirror list
        try:
            import aptsources.distro
            import aptsources.sourceslist
            
            distro = aptsources.distro.get_distro()
            distro.get_sources(aptsources.sourceslist.SourcesList())
            
            if not distro.source_template or not distro.source_template.mirror_set:
                raise RepositoryError("No mirror database found in python-apt.")
            
            mirror_set = distro.source_template.mirror_set
            codename = distro.codename
        except ImportError:
            raise RepositoryError("python-apt is not installed. Install with: sudo apt install python3-apt")
        except Exception as exc:
            raise RepositoryError(f"Failed to load mirror database: {exc}") from exc

        # Extract mirror URLs with country codes, skip the meta mirror
        mirrors: List[Tuple[str, str, str]] = []  # (url, hostname, country_code)
        mirror_country_map: Dict[str, str] = {}  # hostname -> country_code
        for hostname, mirror in mirror_set.items():
            if hostname == "mirrors.ubuntu.com":
                continue
            urls = mirror.get_repo_urls()
            if urls:
                url = urls[0].rstrip("/") + "/"
                if url.startswith("http://") or url.startswith("https://"):
                    country = getattr(mirror, 'location', '') or ''
                    mirrors.append((url, hostname, country))
                    mirror_country_map[hostname] = country

        if not mirrors:
            raise RepositoryError("No mirrors available in database.")

        # Filter mirrors by preferred country if specified
        mirrors_to_test = mirrors
        if preferred_country:
            mirrors_to_test = [(url, hostname, country) for url, hostname, country in mirrors if country == preferred_country]
            if not mirrors_to_test:
                raise RepositoryError(f"No mirrors found for country: {preferred_country}")
            report(f"Found {len(mirrors_to_test)} mirrors in {preferred_country}.\n\n")
        else:
            report(f"Found {len(mirrors)} mirrors.\n\n")

        # ═══════════════════════════════════════════════════════════
        # PHASE 1: Ping test (parallel) on selected mirrors
        # ═══════════════════════════════════════════════════════════
        report("━━━ Phase 1: Ping Test ━━━\n")
        report(f"Testing {len(mirrors_to_test)} mirrors with ping (parallel)…\n\n")
        
        ping_results: List[Tuple[str, str, str, float]] = []  # (url, hostname, country, latency)
        ping_completed = 0
        ping_lock = threading.Lock()
        total_to_test = len(mirrors_to_test)
        
        def ping_worker(url: str, hostname: str, country: str):
            nonlocal ping_completed
            latency = self._ping_host(hostname)
            with ping_lock:
                ping_completed += 1
                if latency is not None:
                    ping_results.append((url, hostname, country, latency))
                    report(f"  [{ping_completed}/{total_to_test}] {hostname}: {latency * 1000:.0f} ms\n")
                else:
                    report(f"  [{ping_completed}/{total_to_test}] {hostname}: FAILED\n")
        
        # Run ping tests in parallel (max 25 threads like software-properties)
        with ThreadPoolExecutor(max_workers=25) as executor:
            futures = [executor.submit(ping_worker, url, hostname, country) for url, hostname, country in mirrors_to_test]
            for future in futures:
                future.result()  # Wait for all to complete
        
        if not ping_results:
            raise RepositoryError("No mirrors responded to ping.")
        
        # Detect user's country (only needed if preferred_country is None)
        user_country = preferred_country
        
        if not user_country:
            # Try from current mirror
            current_mirror_uri = self.get_current_official_mirror()
            if current_mirror_uri:
                try:
                    parsed = urllib.parse.urlparse(current_mirror_uri)
                    current_hostname = parsed.netloc.split(":")[0] if ":" in parsed.netloc else parsed.netloc
                    user_country = mirror_country_map.get(current_hostname)
                except Exception:
                    pass
            
            # If no country from current mirror, use country of fastest ping results
            if not user_country and ping_results:
                # Group by country and find most common country in top 10
                ping_results.sort(key=lambda x: x[3])  # Sort by latency
                top_10_countries = [country for _, _, country, _ in ping_results[:10] if country]
                if top_10_countries:
                    country_counts = Counter(top_10_countries)
                    user_country = country_counts.most_common(1)[0][0]
        
        # Sort by ping latency and take top 15 for download test
        ping_results.sort(key=lambda x: x[3])
        top_ping = ping_results[:15]
        
        # Ensure default Ubuntu mirror is in download test list
        default_mirror_uri = self.get_current_official_mirror()
        if default_mirror_uri:
            try:
                parsed = urllib.parse.urlparse(default_mirror_uri)
                default_hostname = parsed.netloc.split(":")[0] if ":" in parsed.netloc else parsed.netloc
                default_url = default_mirror_uri.rstrip("/") + "/"
                
                # Check if default mirror is already in top_ping
                default_in_list = any(hostname == default_hostname for _, hostname, _, _ in top_ping)
                
                if not default_in_list:
                    # Try to find it in ping_results
                    default_ping_result = None
                    for url, hostname, country, latency in ping_results:
                        if hostname == default_hostname or url == default_url:
                            default_ping_result = (url, hostname, country, latency)
                            break
                    
                    if default_ping_result:
                        report(f"\nAdding default mirror {default_hostname} to download test.\n")
                        top_ping.append(default_ping_result)
                    else:
                        # Default mirror wasn't pinged (maybe filtered out), ping it now
                        report(f"\nPinging default mirror {default_hostname}… ")
                        default_latency = self._ping_host(default_hostname)
                        if default_latency is not None:
                            default_country = mirror_country_map.get(default_hostname, "")
                            report(f"{default_latency * 1000:.0f} ms\n")
                            top_ping.append((default_url, default_hostname, default_country, default_latency))
                        else:
                            report("FAILED\n")
            except Exception:
                pass  # Silently skip if we can't process default mirror
        
        # Ensure archive.ubuntu.com is always in download test list
        archive_hostname = "archive.ubuntu.com"
        archive_in_list = any(hostname == archive_hostname for _, hostname, _, _ in top_ping)
        
        if not archive_in_list:
            # Try to find it in ping_results
            archive_ping_result = None
            for url, hostname, country, latency in ping_results:
                if hostname == archive_hostname:
                    archive_ping_result = (url, hostname, country, latency)
                    break
            
            if archive_ping_result:
                report(f"\nAdding {archive_hostname} to download test.\n")
                top_ping.append(archive_ping_result)
            else:
                # archive.ubuntu.com wasn't pinged (maybe filtered out), ping it now
                report(f"\nPinging {archive_hostname}… ")
                archive_latency = self._ping_host(archive_hostname)
                if archive_latency is not None:
                    archive_country = mirror_country_map.get(archive_hostname, "")
                    # Find the URL for archive.ubuntu.com
                    archive_url = None
                    for url, hostname, _ in mirrors:
                        if hostname == archive_hostname:
                            archive_url = url
                            break
                    if not archive_url:
                        archive_url = f"http://{archive_hostname}/ubuntu/"
                    report(f"{archive_latency * 1000:.0f} ms\n")
                    top_ping.append((archive_url, archive_hostname, archive_country, archive_latency))
                else:
                    report("FAILED\n")
        
        # If no preferred country (All Countries selected), add mirrors from user's country
        if not preferred_country and user_country:
            country_mirrors = [
                (url, hostname, country, latency)
                for url, hostname, country, latency in ping_results
                if country == user_country and (url, hostname, country, latency) not in top_ping
            ]
            if country_mirrors:
                report(f"\nAdding {len(country_mirrors)} mirrors from {user_country} to download test.\n")
                top_ping.extend(country_mirrors)
        
        report(f"\n{len(ping_results)} mirrors responded to ping.\n")
        report(f"{len(top_ping)} mirrors selected for download test.\n\n")

        # ═══════════════════════════════════════════════════════════
        # PHASE 2: Download test on top ping results
        # ═══════════════════════════════════════════════════════════
        report("━━━ Phase 2: Download Test ━━━\n")
        
        test_path = f"dists/{codename}/Release" if codename else "dists/noble/Release"
        report(f"Test file: {test_path}\n\n")

        # (url, hostname, ping_latency, download_latency)
        download_results: List[Tuple[str, str, float, float]] = []
        
        for idx, (url, hostname, country, ping_latency) in enumerate(top_ping, start=1):
            test_url = urllib.parse.urljoin(url, test_path)
            country_info = f" [{country}]" if country else ""
            report(f"[{idx}/{len(top_ping)}] {hostname}{country_info} (ping: {ping_latency * 1000:.0f} ms)… ")
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(test_url, timeout=5) as check:
                    check.read(102400)  # Download 100KB for better measurement
            except Exception as exc:
                report(f"FAILED ({type(exc).__name__})\n")
                continue
            elapsed = time.perf_counter() - start
            report(f"{elapsed * 1000:.0f} ms\n")
            download_results.append((url, hostname, ping_latency, elapsed))

        report(f"\n{len(download_results)} mirrors passed download test.\n")

        if not download_results:
            raise RepositoryError("No mirrors responded during download test.")

        # Sort by download latency
        download_results.sort(key=lambda item: item[3])
        
        report("\n━━━ Final Results (fastest to slowest) ━━━\n")
        for idx, (uri, hostname, ping_lat, dl_lat) in enumerate(download_results, start=1):
            report(f"  {idx}. ping: {ping_lat * 1000:5.0f} ms | download: {dl_lat * 1000:5.0f} ms | {hostname}\n")
        report("\n")

        return download_results

    def get_current_official_mirror(self) -> Optional[str]:
        """Get the current official mirror URI from sources.list and sources.list.d."""
        # Check sources.list
        path = Path(self.SOURCES_LIST_PATH)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                parsed = self._parse_deb_line(path, line)
                if not parsed:
                    continue
                group = self._determine_group(path, parsed["uri"])
                if group == "official":
                    uri = parsed["uri"].rstrip("/") + "/"
                    return uri
        except (FileNotFoundError, PermissionError):
            pass
        
        # Check sources.list.d files
        sources_dir = Path(self.SOURCES_DIR_PATH)
        if sources_dir.exists():
            for child in sorted(sources_dir.iterdir()):
                if child.name.endswith(".list"):
                    try:
                        lines = child.read_text(encoding="utf-8").splitlines()
                        for line in lines:
                            parsed = self._parse_deb_line(child, line)
                            if not parsed:
                                continue
                            group = self._determine_group(child, parsed["uri"])
                            if group == "official":
                                uri = parsed["uri"].rstrip("/") + "/"
                                return uri
                    except (FileNotFoundError, PermissionError):
                        continue
        
        return None

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

class UbuntuRepoManagerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._executor = ThreadPoolExecutor(max_workers=4)
        atexit.register(self._executor.shutdown, wait=False)

        self.set_title("Ubuntu Repository Manager")
        self.set_default_size(1024, 640)

        self._repo_manager = RepositoryManager()
        self._repo_switch_handlers: Dict[Gtk.Switch, int] = {}
        # Cache: (url, hostname, ping_latency, download_latency)
        self._mirror_last_results: Optional[List[Tuple[str, str, float, float]]] = None
        self._benchmark_running = False
        self._all_repo_entries: List[RepositoryEntry] = []
        self._repo_search_query = ""

        # Shared terminal instance for all tabs
        self._shared_terminal = TerminalWidget(title="Terminal", show_close_button=False)
        self._shared_terminal.set_margin_top(12)

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
        self._init_mirror_page()
        self._init_placeholder_pages()
        self._install_keyboard_navigation()
        
        # Initialize search key capture based on initial page
        GLib.idle_add(self._update_search_key_capture)

    def _init_system_updates_page(self):
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

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
        # Use shared terminal
        self._shared_terminal.set_title("Command Output")
        page_box.append(self._shared_terminal)

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
        self._repo_page_box = page_box

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

        page_box.append(header_box)

        # Search bar - initially hidden, appears when typing
        self._repo_search_bar = Gtk.SearchBar()
        self._repo_search_bar.set_show_close_button(True)
        
        self._repo_search_entry = Gtk.SearchEntry()
        self._repo_search_entry.set_placeholder_text("Search repositories…")
        self._repo_search_entry.set_hexpand(True)
        self._repo_search_entry.connect("search-changed", self._on_repo_search_changed)
        self._repo_search_entry.connect("stop-search", self._on_repo_search_stopped)
        
        self._repo_search_bar.set_child(self._repo_search_entry)
        self._repo_search_bar.connect_entry(self._repo_search_entry)
        page_box.append(self._repo_search_bar)
        
        self._repo_search_query = ""
        self._all_repo_entries: List[RepositoryEntry] = []

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)

        preferences_page = Adw.PreferencesPage()
        self._repo_preferences_page = preferences_page
        scroller.set_child(preferences_page)
        page_box.append(scroller)

        self._repo_groups: Dict[str, Adw.PreferencesGroup] = {}
        self._repo_group_rows: Dict[str, List[Gtk.Widget]] = {}
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
            self._repo_group_rows[group_id] = []
            preferences_page.add(group_widget)

        self._stack.add_titled(page_box, "repositories", "Repositories")
        self._refresh_repository_view()
        
        # Connect stack visibility changes to toggle search key capture
        self._stack.connect("notify::visible-child", self._on_stack_page_changed)

    def _init_mirror_page(self):
        """Initialize the Mirror Selection tab."""
        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page_box.set_margin_top(12)
        page_box.set_margin_bottom(12)
        page_box.set_margin_start(12)
        page_box.set_margin_end(12)
        page_box.set_vexpand(True)
        page_box.set_hexpand(True)
        
        # Store all mirrors data: {hostname: (url, hostname, country_code)}
        self._all_mirrors_data: Dict[str, Tuple[str, str, str]] = {}
        self._filtered_mirrors: List[Tuple[str, str, str]] = []
        self._filter_timeout_id: Optional[int] = None
        self._populating_listbox = False
        self._selected_country: Optional[str] = None  # None = all countries

        # Header with description
        header_label = Gtk.Label()
        header_label.set_markup(
            "<b>Mirror Selection</b>\n"
            "<small>Choose an Ubuntu mirror or run benchmark to find the fastest one.</small>"
        )
        header_label.set_xalign(0.0)
        header_label.set_wrap(True)
        page_box.append(header_label)

        # Current mirror info box
        self._current_mirror_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._current_mirror_box.add_css_class("info")
        self._current_mirror_box.set_margin_bottom(8)
        
        current_mirror_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        current_mirror_icon.set_pixel_size(16)
        self._current_mirror_box.append(current_mirror_icon)
        
        self._current_mirror_label = Gtk.Label(label="")
        self._current_mirror_label.set_xalign(0.0)
        self._current_mirror_label.set_hexpand(True)
        self._current_mirror_label.set_wrap(True)
        self._current_mirror_box.append(self._current_mirror_label)
        
        page_box.append(self._current_mirror_box)
        self._update_current_mirror_display()

        # Search and filter row
        search_filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        # Search entry for filtering mirrors
        self._mirror_search_entry = Gtk.SearchEntry()
        self._mirror_search_entry.set_placeholder_text("Search mirrors by hostname or country…")
        self._mirror_search_entry.set_hexpand(True)
        self._mirror_search_entry.connect("search-changed", self._on_mirror_search_changed)
        search_filter_box.append(self._mirror_search_entry)
        
        # Country filter dropdown
        country_label = Gtk.Label(label="Country:")
        country_label.set_margin_end(4)
        search_filter_box.append(country_label)
        
        self._country_dropdown = Gtk.DropDown()
        self._country_dropdown.set_size_request(150, -1)
        self._country_dropdown.connect("notify::selected", self._on_country_selected)
        search_filter_box.append(self._country_dropdown)
        
        page_box.append(search_filter_box)

        # Action buttons row
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        self._mirror_benchmark_button = Gtk.Button(label="Run Benchmark")
        self._mirror_benchmark_button.add_css_class("suggested-action")
        self._mirror_benchmark_button.connect("clicked", self._on_benchmark_mirrors_clicked)
        action_box.append(self._mirror_benchmark_button)
        
        self._mirror_spinner = Gtk.Spinner()
        self._mirror_spinner.set_visible(False)
        action_box.append(self._mirror_spinner)
        
        self._mirror_count_label = Gtk.Label(label="")
        self._mirror_count_label.add_css_class("dim-label")
        self._mirror_count_label.set_hexpand(True)
        self._mirror_count_label.set_xalign(1.0)
        action_box.append(self._mirror_count_label)
        
        page_box.append(action_box)

        # Mirror list in scrolled window
        mirror_list_frame = Gtk.Frame()
        mirror_list_frame.add_css_class("view")
        
        mirror_list_scroller = Gtk.ScrolledWindow()
        mirror_list_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        mirror_list_scroller.set_min_content_height(200)
        mirror_list_scroller.set_vexpand(True)
        
        self._mirror_listbox = Gtk.ListBox()
        self._mirror_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._mirror_listbox.add_css_class("boxed-list")
        self._mirror_listbox.connect("row-selected", self._on_mirror_row_selected)
        
        mirror_list_scroller.set_child(self._mirror_listbox)
        mirror_list_frame.set_child(mirror_list_scroller)
        page_box.append(mirror_list_frame)

        # Verification and apply section
        self._mirror_action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        
        # Verification status row
        verify_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        self._mirror_verify_spinner = Gtk.Spinner()
        self._mirror_verify_spinner.set_visible(False)
        verify_box.append(self._mirror_verify_spinner)
        
        self._mirror_verify_label = Gtk.Label(label="")
        self._mirror_verify_label.set_xalign(0.0)
        self._mirror_verify_label.set_hexpand(True)
        verify_box.append(self._mirror_verify_label)
        
        self._mirror_action_box.append(verify_box)

        self._mirror_apply_button = Gtk.Button(label="Apply Selected Mirror")
        self._mirror_apply_button.set_sensitive(False)
        self._mirror_apply_button.add_css_class("suggested-action")
        self._mirror_apply_button.connect("clicked", self._on_apply_mirror_clicked)
        self._mirror_action_box.append(self._mirror_apply_button)

        page_box.append(self._mirror_action_box)

        # Use shared terminal for benchmark output
        # The terminal will be shown/hidden and title updated as needed
        


        self._stack.add_titled(page_box, "mirrors", "Mirror Selection")
        self._mirror_page_box = page_box  # Store reference for stack page change detection
        self._load_cached_mirror_results()
        # Load mirrors in background to avoid blocking UI
        self._load_all_mirrors_async()

    def _populate_country_dropdown(self):
        """Populate country dropdown with available countries and auto-select user's country."""
        if not self._all_mirrors_data:
            return
        
        # Collect all unique countries
        countries = set()
        for _, (_, _, country) in self._all_mirrors_data.items():
            if country:
                countries.add(country)
        
        # Sort countries
        sorted_countries = sorted(countries)
        
        # Create string list for dropdown: "All Countries" + country list with flags
        string_list = Gtk.StringList()
        string_list.append("All Countries")  # Index 0
        
        for country_code in sorted_countries:
            flag = country_code_to_flag(country_code)
            if flag:
                string_list.append(f"{flag}  {country_code}")
            else:
                string_list.append(country_code)
        
        self._country_dropdown.set_model(string_list)
        
        # Auto-detect and select user's country
        auto_country = self._detect_user_country()
        if auto_country and auto_country in sorted_countries:
            # Find index of auto-detected country (index 0 is "All Countries", so +1)
            country_index = sorted_countries.index(auto_country) + 1
            self._country_dropdown.set_selected(country_index)
            self._selected_country = auto_country
        else:
            # Select "All Countries"
            self._country_dropdown.set_selected(0)
            self._selected_country = None

    def _detect_user_country(self) -> Optional[str]:
        """Detect user's country from current mirror or fastest ping results."""
        # Try from current mirror
        current_mirror_uri = self._repo_manager.get_current_official_mirror()
        if current_mirror_uri and self._all_mirrors_data:
            try:
                parsed = urllib.parse.urlparse(current_mirror_uri)
                current_hostname = parsed.netloc.split(":")[0] if ":" in parsed.netloc else parsed.netloc
                if current_hostname in self._all_mirrors_data:
                    _, _, country = self._all_mirrors_data[current_hostname]
                    if country:
                        return country
            except Exception:
                pass
        
        # Try from benchmark results (fastest mirrors are likely in user's country)
        if self._mirror_last_results:
            top_countries = [country for _, _, _, _ in self._mirror_last_results[:10] if country]
            if top_countries:
                country_counts = Counter(top_countries)
                most_common = country_counts.most_common(1)
                if most_common:
                    return most_common[0][0]
        
        return None

    def _on_country_selected(self, dropdown, _pspec):
        """Handle country selection change."""
        selected_index = dropdown.get_selected()
        if selected_index == Gtk.INVALID_LIST_POSITION:
            return
        
        if selected_index == 0:
            # "All Countries" selected
            self._selected_country = None
        else:
            # Extract country code from selected string (format: "🇹🇷  TR" or "TR")
            model = dropdown.get_model()
            if model and selected_index < model.get_n_items():
                selected_text = model.get_string(selected_index)
                # Extract country code (last 2 characters or after flag)
                parts = selected_text.strip().split()
                if parts:
                    country_code = parts[-1]
                    if len(country_code) == 2:
                        self._selected_country = country_code
                    else:
                        self._selected_country = None
                else:
                    self._selected_country = None
            else:
                self._selected_country = None
        
        # Re-filter mirrors
        self._filter_mirrors(self._mirror_search_entry.get_text())

    def _update_current_mirror_display(self):
        """Update the display showing the currently active mirror."""
        try:
            current_uri = self._repo_manager.get_current_official_mirror()
            if current_uri:
                # Normalize URI (ensure trailing slash)
                current_uri_normalized = current_uri.rstrip("/") + "/"
                
                # Extract hostname from URI
                try:
                    parsed = urllib.parse.urlparse(current_uri_normalized)
                    hostname = parsed.netloc or current_uri_normalized
                    # Remove port if present
                    if ":" in hostname:
                        hostname = hostname.split(":")[0]
                except Exception:
                    hostname = current_uri_normalized
                
                # Try to find matching mirror in our list to show country flag
                display_text = f"<b>Current mirror:</b> {hostname}"
                if self._all_mirrors_data:
                    for mirror_hostname, (mirror_url, _, country) in self._all_mirrors_data.items():
                        mirror_url_normalized = mirror_url.rstrip("/") + "/"
                        if mirror_url_normalized == current_uri_normalized or mirror_hostname == hostname:
                            flag = country_code_to_flag(country)
                            if flag:
                                display_text = f"<b>Current mirror:</b> {flag}  {hostname}"
                            break
                
                self._current_mirror_label.set_markup(display_text)
                self._current_mirror_box.set_visible(True)
            else:
                self._current_mirror_box.set_visible(False)
        except Exception:
            self._current_mirror_box.set_visible(False)

    def _load_all_mirrors_async(self):
        """Load all mirrors from python-apt database in background."""
        def work():
            try:
                import aptsources.distro
                import aptsources.sourceslist
                
                distro = aptsources.distro.get_distro()
                distro.get_sources(aptsources.sourceslist.SourcesList())
                
                if not distro.source_template or not distro.source_template.mirror_set:
                    return {}
                
                mirrors_data = {}
                for hostname, mirror in distro.source_template.mirror_set.items():
                    if hostname == "mirrors.ubuntu.com":
                        continue
                    urls = mirror.get_repo_urls()
                    if urls:
                        url = urls[0].rstrip("/") + "/"
                        if url.startswith("http://") or url.startswith("https://"):
                            country = getattr(mirror, 'location', '') or ''
                            mirrors_data[hostname] = (url, hostname, country)
                return mirrors_data
            except Exception:
                return {}
        
        def done(future):
            try:
                mirrors_data = future.result()
                if mirrors_data:
                    self._all_mirrors_data = mirrors_data
                    # Populate country dropdown
                    self._populate_country_dropdown()
                    # Re-filter and re-sort with current query and benchmark data if available
                    self._filter_mirrors(self._mirror_search_entry.get_text())
            except Exception:
                pass
        
        self._submit_background(work, done)

    def _load_all_mirrors(self):
        """Load all mirrors from python-apt database (synchronous, for compatibility)."""
        try:
            import aptsources.distro
            import aptsources.sourceslist
            
            distro = aptsources.distro.get_distro()
            distro.get_sources(aptsources.sourceslist.SourcesList())
            
            if not distro.source_template or not distro.source_template.mirror_set:
                return
            
            self._all_mirrors_data.clear()
            for hostname, mirror in distro.source_template.mirror_set.items():
                if hostname == "mirrors.ubuntu.com":
                    continue
                urls = mirror.get_repo_urls()
                if urls:
                    url = urls[0].rstrip("/") + "/"
                    if url.startswith("http://") or url.startswith("https://"):
                        country = getattr(mirror, 'location', '') or ''
                        self._all_mirrors_data[hostname] = (url, hostname, country)
            
            self._filter_mirrors("")
        except Exception:
            pass

    def _filter_mirrors(self, query: str):
        """Filter mirrors based on search query, country filter, and sort by benchmark results if available."""
        if not self._all_mirrors_data:
            return
        
        query = query.lower().strip()
        
        # Filter mirrors by search query and country
        self._filtered_mirrors = []
        for hostname, (url, _, country) in self._all_mirrors_data.items():
            # Apply country filter
            if self._selected_country and country != self._selected_country:
                continue
            
            # Apply search query filter
            if query:
                if query not in hostname.lower() and query not in country.lower():
                    continue
            
            self._filtered_mirrors.append((url, hostname, country))
        
        # Sort by benchmark results if available, otherwise by hostname
        if self._mirror_last_results:
            # Create a lookup dict for benchmark data: hostname -> (ping, download)
            benchmark_lookup = {
                hostname: (ping_lat, dl_lat)
                for _, hostname, ping_lat, dl_lat in self._mirror_last_results
            }
            
            def sort_key(mirror_data):
                url, hostname, country = mirror_data
                if hostname in benchmark_lookup:
                    ping_lat, dl_lat = benchmark_lookup[hostname]
                    # Sort by download latency first, then ping
                    return (dl_lat, ping_lat, hostname)
                else:
                    # Mirrors without benchmark data go to the end
                    return (float('inf'), float('inf'), hostname)
            
            self._filtered_mirrors.sort(key=sort_key)
        else:
            # No benchmark data, sort by hostname
            self._filtered_mirrors.sort(key=lambda x: x[1])
        
        # Update count first (fast)
        total = len(self._all_mirrors_data)
        shown = len(self._filtered_mirrors)
        if query:
            self._mirror_count_label.set_label(f"{shown} of {total} mirrors")
        else:
            self._mirror_count_label.set_label(f"{total} mirrors")
        
        # Update listbox (may be slow, but uses lazy loading)
        self._populate_mirror_listbox()

    def _populate_mirror_listbox(self):
        """Populate the mirror listbox with filtered results (lazy loading for performance)."""
        if self._populating_listbox:
            return
        
        self._populating_listbox = True
        
        # Clear existing rows efficiently
        while True:
            row = self._mirror_listbox.get_row_at_index(0)
            if row is None:
                break
            self._mirror_listbox.remove(row)
        
        # Create benchmark lookup once
        benchmark_lookup = {}
        if self._mirror_last_results:
            benchmark_lookup = {
                hostname: (ping_lat, dl_lat)
                for _, hostname, ping_lat, dl_lat in self._mirror_last_results
            }
        
        # Detect current mirror and archive.ubuntu.com
        current_mirror_hostname = None
        try:
            current_mirror_uri = self._repo_manager.get_current_official_mirror()
            if current_mirror_uri:
                parsed = urllib.parse.urlparse(current_mirror_uri)
                current_mirror_hostname = parsed.netloc.split(":")[0] if ":" in parsed.netloc else parsed.netloc
        except Exception:
            pass
        
        archive_hostname = "archive.ubuntu.com"
        
        # Batch loading: add mirrors in chunks to avoid blocking UI
        def add_mirrors_batch(start_idx=0, batch_size=100):
            end_idx = min(start_idx + batch_size, len(self._filtered_mirrors))
            
            for i in range(start_idx, end_idx):
                url, hostname, country = self._filtered_mirrors[i]
                row = Adw.ActionRow()
                
                # Build title with flag and badges
                title_parts = []
                
                # Add flag emoji if country is available
                flag = country_code_to_flag(country)
                if flag:
                    title_parts.append(flag)
                
                # Add hostname
                title_parts.append(hostname)
                
                # Add badges for special mirrors
                badges = []
                if hostname == current_mirror_hostname:
                    badges.append("(Current)")
                if hostname == archive_hostname:
                    badges.append("(Main Mirror)")
                
                if badges:
                    title_parts.append(" ".join(badges))
                
                row.set_title("  ".join(title_parts))
                
                # Show country code and ping/download info if available
                subtitle_parts = []
                if country:
                    subtitle_parts.append(country)
                
                # Check benchmark data
                if hostname in benchmark_lookup:
                    ping_lat, dl_lat = benchmark_lookup[hostname]
                    subtitle_parts.append(f"ping: {ping_lat * 1000:.0f} ms")
                    subtitle_parts.append(f"download: {dl_lat * 1000:.0f} ms")
                
                if subtitle_parts:
                    row.set_subtitle(" • ".join(subtitle_parts))
                
                # Store data in row
                row._mirror_data = (url, hostname, country)
                
                self._mirror_listbox.append(row)
            
            # Continue with next batch if there are more mirrors
            if end_idx < len(self._filtered_mirrors):
                GLib.idle_add(add_mirrors_batch, end_idx, batch_size)
            else:
                self._populating_listbox = False
                return False
            
            return False  # Don't repeat automatically
        
        # Start batch loading
        if self._filtered_mirrors:
            GLib.idle_add(add_mirrors_batch, 0, 100)
        else:
            self._populating_listbox = False

    def _on_mirror_search_changed(self, search_entry):
        """Handle mirror search entry changes with debounce."""
        # Cancel previous timeout
        if self._filter_timeout_id is not None:
            GLib.source_remove(self._filter_timeout_id)
            self._filter_timeout_id = None
        
        query = search_entry.get_text()
        
        # Debounce: wait 300ms after user stops typing
        def do_filter():
            self._filter_timeout_id = None
            self._filter_mirrors(query)
            return False
        
        self._filter_timeout_id = GLib.timeout_add(300, do_filter)

    def _on_mirror_row_selected(self, listbox, row):
        """Handle mirror row selection."""
        if row is None:
            self._mirror_apply_button.set_sensitive(False)
            self._mirror_verify_label.set_label("")
            return
        
        url, hostname, country = row._mirror_data
        
        # Show verification in progress
        self._mirror_verify_spinner.set_visible(True)
        self._mirror_verify_spinner.start()
        self._mirror_verify_label.set_label(f"Verifying {hostname}…")
        self._mirror_verify_label.remove_css_class("success")
        self._mirror_verify_label.remove_css_class("error")
        self._mirror_apply_button.set_sensitive(False)
        
        def verify():
            return self._repo_manager._ping_host(hostname)
        
        def done(future):
            self._mirror_verify_spinner.stop()
            self._mirror_verify_spinner.set_visible(False)
            try:
                new_latency = future.result()
            except Exception:
                new_latency = None
            
            if new_latency is not None:
                self._mirror_verify_label.set_label(f"✓ {hostname} is reachable (ping: {new_latency * 1000:.0f} ms)")
                self._mirror_verify_label.add_css_class("success")
                self._mirror_apply_button.set_sensitive(True)
            else:
                self._mirror_verify_label.set_label(f"✗ {hostname} is not responding")
                self._mirror_verify_label.add_css_class("error")
                self._mirror_apply_button.set_sensitive(False)
        
        self._submit_background(verify, done)

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
        self._all_repo_entries = entries
        self._filter_repository_entries()

    def _render_repository_entries(self, entries: Sequence[RepositoryEntry], is_filtered: bool = False):
        grouped: Dict[str, List[RepositoryEntry]] = {key: [] for key in self._repo_groups}
        for entry in entries:
            grouped.setdefault(entry.group, []).append(entry)

        self._repo_switch_handlers.clear()

        for group_id, group_widget in self._repo_groups.items():
            self._clear_preferences_group(group_id)
            rows = grouped.get(group_id, [])
            if not rows:
                placeholder_text = "No matching repositories." if is_filtered else "No repositories detected."
                placeholder = Gtk.Label(label=placeholder_text)
                placeholder.add_css_class("dim-label")
                placeholder.set_xalign(0.0)
                group_widget.add(placeholder)
                self._repo_group_rows[group_id].append(placeholder)
                continue
            for entry in rows:
                row = self._create_repository_row(entry)
                group_widget.add(row)
                self._repo_group_rows[group_id].append(row)

        if is_filtered:
            total = len(self._all_repo_entries)
            shown = len(entries)
            self._repo_status_label.set_label(f"Showing {shown} of {total} repositories")
        else:
            self._repo_status_label.set_label(f"{len(entries)} repositories detected")

    def _clear_preferences_group(self, group_id: str) -> None:
        group_widget = self._repo_groups.get(group_id)
        if group_widget is None:
            return
        tracked_rows = self._repo_group_rows.get(group_id, [])
        for row in tracked_rows:
            group_widget.remove(row)
        self._repo_group_rows[group_id] = []

    def _create_repository_row(self, entry: RepositoryEntry) -> Adw.ActionRow:
        title = entry.label or "Repository"
        if not entry.enabled:
            title = f"{title} (Disabled)"
        subtitle = entry.subtitle()
        row = Adw.ActionRow(title=title, subtitle=subtitle)

        if not entry.enabled:
            row.add_css_class("dim-label")

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

    def _on_stack_page_changed(self, stack, _param):
        """Enable/disable search key capture based on current page."""
        self._update_search_key_capture()
        # Update current mirror display when mirror page becomes visible
        if hasattr(self, '_mirror_page_box') and stack.get_visible_child() == self._mirror_page_box:
            self._update_current_mirror_display()
        
        # Update terminal title based on active page
        visible_child = stack.get_visible_child()
        if visible_child:
            page_name = None
            for page in stack.get_pages():
                if page.get_child() == visible_child:
                    page_name = page.get_name()
                    break
            
            if page_name == "system_updates":
                self._shared_terminal.set_title("Command Output")
                # Terminal is always visible on System Updates page
                self._shared_terminal.set_visible(True)
            elif page_name == "mirrors":
                # Terminal is managed by benchmark operations on this page
                # It will be shown when benchmark runs
                pass

    def _update_search_key_capture(self):
        """Update search key capture widget based on current page."""
        is_repo_page = self._stack.get_visible_child() == self._repo_page_box
        if is_repo_page:
            self._repo_search_bar.set_key_capture_widget(self)
        else:
            self._repo_search_bar.set_key_capture_widget(None)
            # Close search when leaving the page
            if self._repo_search_bar.get_search_mode():
                self._repo_search_bar.set_search_mode(False)
                self._repo_search_entry.set_text("")
        return False  # For GLib.idle_add

    def _on_repo_search_changed(self, search_entry):
        self._repo_search_query = search_entry.get_text().strip().lower()
        self._filter_repository_entries()

    def _on_repo_search_stopped(self, _search_entry):
        self._repo_search_bar.set_search_mode(False)
        self._repo_search_entry.set_text("")
        self._repo_search_query = ""
        self._filter_repository_entries()

    def _filter_repository_entries(self):
        if not self._repo_search_query:
            # Show all entries
            self._render_repository_entries(self._all_repo_entries)
        else:
            # Filter entries based on search query
            filtered = [
                entry for entry in self._all_repo_entries
                if self._entry_matches_search(entry, self._repo_search_query)
            ]
            self._render_repository_entries(filtered, is_filtered=True)

    def _entry_matches_search(self, entry: RepositoryEntry, query: str) -> bool:
        searchable = " ".join([
            entry.label or "",
            entry.uri or "",
            entry.suite or "",
            " ".join(entry.components) if entry.components else "",
            entry.group or "",
            entry.file_path or "",
        ]).lower()
        return query in searchable

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

    def _append_benchmark_output(self, text: str):
        """Append text to shared terminal (must be called from main thread)."""
        self._shared_terminal.append_text(text)

    def _on_benchmark_mirrors_clicked(self, _button):
        if self._benchmark_running:
            return
        self._benchmark_running = True
        self._mirror_spinner.set_visible(True)
        self._mirror_spinner.start()
        self._mirror_benchmark_button.set_sensitive(False)
        self._mirror_apply_button.set_sensitive(False)
        
        # Show and clear the shared terminal with benchmark title
        # Switch to System Updates page to show terminal
        system_updates_page = None
        for page in self._stack.get_pages():
            if page.get_name() == "system_updates":
                system_updates_page = page.get_child()
                break
        
        if system_updates_page:
            self._stack.set_visible_child(system_updates_page)
        
        self._shared_terminal.set_title("Benchmark Progress")
        self._shared_terminal.clear()
        self._shared_terminal.set_visible(True)

        def progress_callback(msg: str):
            """Thread-safe progress reporter using GLib.idle_add."""
            GLib.idle_add(self._append_benchmark_output, msg)

        def work():
            return self._repo_manager.benchmark_mirrors(
                progress_callback=progress_callback,
                preferred_country=self._selected_country
            )

        def done(future):
            self._benchmark_running = False
            self._mirror_spinner.stop()
            self._mirror_spinner.set_visible(False)
            self._mirror_benchmark_button.set_sensitive(True)
            try:
                results = future.result()
            except RepositoryError as exc:
                self._append_benchmark_output(f"\n❌ Error: {exc}\n")
                self._show_toast(str(exc))
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._append_benchmark_output(f"\n❌ Benchmark failed: {exc}\n")
                self._show_toast(f"Benchmark failed: {exc}")
            else:
                self._append_benchmark_output("✓ Benchmark complete!\n")
                self._repo_manager.save_mirror_cache(results)
                self._update_mirror_results(results)

        self._submit_background(work, done)

    def _load_cached_mirror_results(self):
        """Load cached benchmark results on startup."""
        cached = self._repo_manager.load_mirror_cache()
        if cached:
            self._mirror_last_results = cached
            cache_age = self._repo_manager.get_cache_age()
            age_str = ""
            if cache_age is not None:
                if cache_age < 3600:
                    age_str = f" ({int(cache_age / 60)} min ago)"
                elif cache_age < 86400:
                    age_str = f" ({int(cache_age / 3600)} hours ago)"
                else:
                    age_str = f" ({int(cache_age / 86400)} days ago)"
            
            # If mirrors are already loaded, re-sort them
            if self._all_mirrors_data:
                self._filter_mirrors(self._mirror_search_entry.get_text())
            
            self._mirror_verify_label.set_label(f"Benchmark results loaded{age_str}")

    def _update_mirror_results(self, results: Sequence[Tuple[str, str, float, float]]):
        """Update mirror cache with benchmark results and re-sort the list.
        
        Args:
            results: List of (url, hostname, ping_latency, download_latency) tuples
        """
        self._mirror_last_results = list(results)
        
        # Re-filter and re-sort mirrors with benchmark data
        current_query = self._mirror_search_entry.get_text()
        self._filter_mirrors(current_query)
        
        # Select the fastest mirror in the list
        if results:
            fastest_hostname = results[0][1]
            for i in range(1000):  # Reasonable limit
                row = self._mirror_listbox.get_row_at_index(i)
                if row is None:
                    break
                if hasattr(row, '_mirror_data') and row._mirror_data[1] == fastest_hostname:
                    self._mirror_listbox.select_row(row)
                    break

    def _on_apply_mirror_clicked(self, _button):
        selected_row = self._mirror_listbox.get_selected_row()
        if selected_row is None or not hasattr(selected_row, '_mirror_data'):
            self._show_toast("Please select a mirror first.")
            return
        
        selected_url, selected_hostname, _ = selected_row._mirror_data
        self._mirror_apply_button.set_sensitive(False)
        self._mirror_spinner.set_visible(True)
        self._mirror_spinner.start()

        def work():
            return self._repo_manager.apply_official_mirror(selected_url)

        def done(future):
            self._mirror_spinner.stop()
            self._mirror_spinner.set_visible(False)
            self._mirror_apply_button.set_sensitive(True)
            try:
                changed = future.result()
            except RepositoryError as exc:
                self._show_toast(str(exc))
            except Exception as exc:  # pragma: no cover - defensive fallback
                self._show_toast(f"Failed to apply mirror: {exc}")
            else:
                if changed:
                    self._show_toast(f"Applied {selected_hostname} to official repositories.")
                else:
                    self._show_toast("Official repositories already use the selected mirror.")
                self._refresh_repository_view()
                self._update_current_mirror_display()
                # Update mirror list to reflect new current mirror
                self._filter_mirrors(self._mirror_search_entry.get_text())

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

        # Update terminal title based on command
        if "update" in command:
            self._shared_terminal.set_title("Command Output: apt update")
        elif "upgrade" in command:
            self._shared_terminal.set_title("Command Output: apt full-upgrade")
        else:
            self._shared_terminal.set_title("Command Output")
        
        # Ensure terminal is visible
        self._shared_terminal.set_visible(True)
        self._shared_terminal.run_command(command, on_error=self._show_toast)

    def _show_toast(self, message):
        # Escape special characters that Pango interprets as markup
        escaped = GLib.markup_escape_text(str(message))
        toast = Adw.Toast.new(escaped)
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

