#!/usr/bin/env python3
"""Download the IACR ePrint papers listed on this website.

The script first tries an ordinary HTTP download. If IACR responds with its
Cloudflare browser challenge, it opens the PDFs in a regular Chromium window
(with a dedicated profile, not remote-controlled, so Cloudflare accepts it).
Complete any challenge in that window and the script will continue
automatically. The revision advertised by each ePrint record is stored next to
the PDFs in ``.eprint-revisions.json``; when that value changes, the existing
PDF is replaced with the new revision.

Usage:
    python3 download_papers.py

Run ``python3 download_papers.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = (ROOT / "cv.html", ROOT / "index.html")
DEFAULT_OUTPUT_DIR = ROOT / "papers"
REVISION_STATE_FILENAME = ".eprint-revisions.json"
EPRINT_PATTERN = re.compile(
    r"https?://(?:www\.)?eprint\.iacr\.org/"
    r"(?P<year>\d{4})/(?P<number>\d+)(?:\.pdf)?"
)
HISTORY_REVISION_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*:\s*(?:received|revised)", re.IGNORECASE
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
REVISION_REQUEST_DELAY = 1.0
REVISION_REQUEST_ATTEMPTS = 3
BROWSER_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome-stable",
    "google-chrome",
    "brave-browser",
    "brave",
)
# Kept between runs so the Cloudflare clearance cookie is reused.
BROWSER_PROFILE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    / "download-papers-browser"
)


@dataclass(frozen=True, order=True)
class Paper:
    year: int
    number: int

    @property
    def paper_id(self) -> str:
        return f"{self.year}/{self.number}"

    @property
    def filename(self) -> str:
        return f"{self.year}-{self.number}.pdf"

    @property
    def landing_url(self) -> str:
        return f"https://eprint.iacr.org/{self.paper_id}"

    @property
    def pdf_url(self) -> str:
        return f"{self.landing_url}.pdf"


class DownloadError(RuntimeError):
    pass


class RevisionMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.modified_time: str | None = None

    def handle_starttag(
        self, tag: str, attributes: list[tuple[str, str | None]]
    ) -> None:
        if tag != "meta":
            return
        values = dict(attributes)
        if values.get("property") == "article:modified_time":
            self.modified_time = values.get("content")


def discover_papers(source_files: Iterable[Path]) -> list[Paper]:
    papers: set[Paper] = set()
    for source_file in source_files:
        try:
            html = source_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise DownloadError(f"Cannot read {source_file}: {exc}") from exc

        for match in EPRINT_PATTERN.finditer(html):
            papers.add(Paper(int(match["year"]), int(match["number"])))

    return sorted(papers, reverse=True)


def is_valid_pdf(path: Path) -> bool:
    try:
        if path.stat().st_size < 1_024:
            return False
        with path.open("rb") as pdf:
            if b"%PDF-" not in pdf.read(1_024):
                return False
            pdf.seek(max(0, path.stat().st_size - 4_096))
            if b"%%EOF" not in pdf.read():
                return False
    except OSError:
        return False

    qpdf = shutil.which("qpdf")
    if qpdf:
        check = subprocess.run(
            [qpdf, "--check", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        # qpdf uses exit code 3 for a readable PDF that only has warnings.
        return check.returncode in (0, 3)

    return True


def make_http_opener() -> urllib.request.OpenerDirector:
    cookies = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))


def fetch_remote_revision(
    paper: Paper,
    opener: urllib.request.OpenerDirector,
    timeout: int,
) -> str:
    request = urllib.request.Request(
        paper.landing_url,
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "User-Agent": USER_AGENT,
        },
    )
    html = ""
    for attempt in range(REVISION_REQUEST_ATTEMPTS):
        try:
            with opener.open(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == REVISION_REQUEST_ATTEMPTS - 1:
                raise DownloadError(str(exc)) from exc
            retry_after = exc.headers.get("Retry-After")
            try:
                retry_delay = max(1, int(retry_after))
            except (TypeError, ValueError):
                retry_delay = 5 * (attempt + 1)
            print(f"    IACR rate limit reached; retrying in {retry_delay}s …")
            time.sleep(retry_delay)
        except (OSError, urllib.error.URLError) as exc:
            raise DownloadError(str(exc)) from exc

    parser = RevisionMetadataParser()
    parser.feed(html)
    if parser.modified_time:
        return parser.modified_time

    # This fallback still detects revisions if IACR removes the Open Graph tag.
    history_dates = HISTORY_REVISION_PATTERN.findall(html)
    if history_dates:
        return f"history:{max(history_dates)}"
    raise DownloadError("record page did not contain revision metadata")


def load_revision_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": 1, "papers": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Cannot read revision state {path}: {exc}") from exc

    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or not isinstance(state.get("papers"), dict)
    ):
        raise DownloadError(f"Unsupported revision state format in {path}")
    return state


def save_revision_state(path: Path, state: dict[str, object]) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as destination:
            json.dump(state, destination, indent=2, sort_keys=True)
            destination.write("\n")
        os.replace(temporary_path, path)
    except OSError as exc:
        raise DownloadError(f"Cannot write revision state {path}: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def tracked_revision(state: dict[str, object], paper: Paper) -> str | None:
    papers = state["papers"]
    assert isinstance(papers, dict)
    entry = papers.get(paper.paper_id)
    if isinstance(entry, dict) and isinstance(entry.get("revision"), str):
        return entry["revision"]
    return None


def set_tracked_revision(
    state: dict[str, object], paper: Paper, revision: str
) -> None:
    papers = state["papers"]
    assert isinstance(papers, dict)
    papers[paper.paper_id] = {
        "filename": paper.filename,
        "revision": revision,
    }


def download_direct(
    paper: Paper,
    target: Path,
    opener: urllib.request.OpenerDirector,
    timeout: int,
) -> None:
    request = urllib.request.Request(
        paper.pdf_url,
        headers={
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            "Referer": paper.landing_url,
            "User-Agent": USER_AGENT,
        },
    )

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}-", suffix=".part", dir=target.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as destination:
            try:
                with opener.open(request, timeout=timeout) as response:
                    shutil.copyfileobj(response, destination)
            except (OSError, urllib.error.URLError) as exc:
                raise DownloadError(str(exc)) from exc

        if not is_valid_pdf(temporary_path):
            raise DownloadError("server response was not a valid PDF")
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def completed_downloads(directory: Path) -> set[Path]:
    return {
        path
        for path in directory.iterdir()
        if path.is_file() and not path.name.endswith((".crdownload", ".tmp"))
    }


def wait_for_browser_download(
    directory: Path,
    files_before: set[Path],
    timeout: int,
) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in completed_downloads(directory) - files_before:
            if is_valid_pdf(path):
                return path
        time.sleep(0.5)

    raise DownloadError(f"browser download timed out after {timeout} seconds")


def find_browser(requested: str | None) -> str:
    for candidate in (requested,) if requested else BROWSER_CANDIDATES:
        executable = shutil.which(candidate)
        if executable:
            return executable
    if requested:
        raise DownloadError(f"Browser not found: {requested}")
    raise DownloadError(
        "No Chromium-based browser found; pass --browser /path/to/chrome"
    )


def prepare_browser_profile(profile_dir: Path, download_dir: Path) -> None:
    """Make the profile save PDFs to ``download_dir`` instead of showing them."""
    preferences_path = profile_dir / "Default" / "Preferences"
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        preferences = {}
    if not isinstance(preferences, dict):
        preferences = {}

    def section(name: str) -> dict[str, object]:
        if not isinstance(preferences.get(name), dict):
            preferences[name] = {}
        return preferences[name]

    section("download").update(
        {
            "default_directory": str(download_dir),
            "directory_upgrade": True,
            "prompt_for_download": False,
        }
    )
    section("plugins")["always_open_pdf_externally"] = True
    # The script closes the browser itself; avoid a "Restore pages?" prompt.
    section("profile").update({"exit_type": "Normal", "exited_cleanly": True})

    try:
        preferences_path.write_text(json.dumps(preferences), encoding="utf-8")
    except OSError as exc:
        raise DownloadError(f"Cannot write {preferences_path}: {exc}") from exc


def download_with_browser(
    papers: list[Paper],
    output_dir: Path,
    timeout: int,
    browser: str | None,
    no_sandbox: bool,
) -> tuple[list[Paper], list[tuple[Paper, str]]]:
    # The browser is started as an ordinary process rather than through
    # WebDriver: Cloudflare detects automated browsers and never lets them
    # pass its challenge.
    executable = find_browser(browser)
    download_dir = BROWSER_PROFILE_DIR / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    prepare_browser_profile(BROWSER_PROFILE_DIR, download_dir)

    command = [
        executable,
        f"--user-data-dir={BROWSER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if no_sandbox:
        command.append("--no-sandbox")

    print("  Complete any Cloudflare check in the browser window; waiting …")
    downloaded: list[Paper] = []
    failed: list[tuple[Paper, str]] = []
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for paper in papers:
            print(f"  Browser: {paper.paper_id}")
            files_before = completed_downloads(download_dir)
            # The first call starts the browser; later calls hand the URL to
            # the running instance (in a new tab) and exit immediately.
            processes.append(
                subprocess.Popen(
                    [*command, paper.pdf_url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            try:
                downloaded_file = wait_for_browser_download(
                    download_dir, files_before, timeout
                )
                shutil.move(downloaded_file, output_dir / paper.filename)
                downloaded.append(paper)
            except (DownloadError, OSError) as exc:
                failed.append((paper, str(exc)))
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    return downloaded, failed


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"destination directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="download again even when a valid destination PDF already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list discovered papers without downloading them",
    )
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="do not open a browser when direct downloads fail",
    )
    parser.add_argument(
        "--browser",
        help="Chromium-based browser executable (default: first one found of "
        + ", ".join(BROWSER_CANDIDATES)
        + ")",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="pass --no-sandbox to the browser (occasionally needed in containers)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="per-download timeout in seconds (default: 120)",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.timeout < 1:
        print("error: --timeout must be at least 1 second", file=sys.stderr)
        return 2

    try:
        papers = discover_papers(DEFAULT_SOURCES)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not papers:
        print("error: no IACR ePrint links found in cv.html or index.html", file=sys.stderr)
        return 1

    output_dir = arguments.output_dir.expanduser().resolve()
    print(f"Discovered {len(papers)} papers; destination: {output_dir}")

    if arguments.dry_run:
        for paper in papers:
            print(f"  {paper.paper_id} -> {paper.filename}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    revision_state_path = output_dir / REVISION_STATE_FILENAME
    try:
        revision_state = load_revision_state(revision_state_path)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    opener = make_http_opener()
    downloaded: list[Paper] = []
    skipped: list[Paper] = []
    browser_pending: list[Paper] = []
    remote_revisions: dict[Paper, str] = {}
    revision_check_failures: list[tuple[Paper, str]] = []
    revision_state_changed = False

    for paper in papers:
        target = output_dir / paper.filename
        local_pdf_is_valid = target.exists() and is_valid_pdf(target)
        remote_revision: str | None = None

        try:
            remote_revision = fetch_remote_revision(
                paper, opener, min(arguments.timeout, 60)
            )
            remote_revisions[paper] = remote_revision
        except DownloadError as exc:
            print(f"  Warning: {paper.paper_id} revision check failed: {exc}")
            revision_check_failures.append((paper, str(exc)))
        time.sleep(REVISION_REQUEST_DELAY)

        if local_pdf_is_valid and not arguments.force:
            local_revision = tracked_revision(revision_state, paper)
            if remote_revision is None:
                print(f"  Skip:    {paper.paper_id} (could not check for updates)")
                skipped.append(paper)
                continue
            if local_revision is None:
                # Migration path for PDFs downloaded before revision tracking
                # was introduced. --force can be used for a one-time refresh.
                set_tracked_revision(revision_state, paper, remote_revision)
                revision_state_changed = True
                print(
                    f"  Track:   {paper.paper_id} "
                    f"(existing PDF, revision {remote_revision})"
                )
                skipped.append(paper)
                continue
            if local_revision == remote_revision:
                print(f"  Skip:    {paper.paper_id} (revision {remote_revision})")
                skipped.append(paper)
                continue
            print(
                f"  Update:  {paper.paper_id} "
                f"({local_revision} -> {remote_revision})"
            )

        print(f"  Direct:  {paper.paper_id}")
        try:
            download_direct(paper, target, opener, min(arguments.timeout, 60))
            downloaded.append(paper)
            if remote_revision is not None:
                set_tracked_revision(revision_state, paper, remote_revision)
                revision_state_changed = True
        except DownloadError as exc:
            print(f"    Direct download failed: {exc}")
            browser_pending.append(paper)

    failures: list[tuple[Paper, str]] = []
    if browser_pending and not arguments.direct_only:
        print(f"Trying {len(browser_pending)} paper(s) in the browser …")
        try:
            browser_downloaded, failures = download_with_browser(
                browser_pending,
                output_dir,
                arguments.timeout,
                arguments.browser,
                arguments.no_sandbox,
            )
            downloaded.extend(browser_downloaded)
            for paper in browser_downloaded:
                remote_revision = remote_revisions.get(paper)
                if remote_revision is not None:
                    set_tracked_revision(revision_state, paper, remote_revision)
                    revision_state_changed = True
        except DownloadError as exc:
            failures = [(paper, str(exc)) for paper in browser_pending]
    elif browser_pending:
        failures = [(paper, "direct download failed") for paper in browser_pending]

    state_save_failed = False
    if revision_state_changed:
        try:
            save_revision_state(revision_state_path, revision_state)
        except DownloadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            state_save_failed = True

    print(
        f"Finished: {len(downloaded)} downloaded or updated, "
        f"{len(skipped)} up to date, {len(failures)} downloads failed, "
        f"{len(revision_check_failures)} revision checks failed."
    )
    for paper, reason in failures:
        print(f"  FAILED {paper.paper_id}: {reason}", file=sys.stderr)

    return 1 if failures or revision_check_failures or state_save_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
