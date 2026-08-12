#!/usr/bin/env python3
"""Download the IACR ePrint papers listed on this website.

The script first tries an ordinary HTTP download. If IACR responds with its
Cloudflare browser challenge, it opens Chrome through Selenium. Complete any
challenge in that window and the script will continue automatically.

Usage:
    python3 -m pip install -r requirements-download-papers.txt
    python3 download_papers.py

Run ``python3 download_papers.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import http.cookiejar
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
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = (ROOT / "cv.html", ROOT / "index.html")
DEFAULT_OUTPUT_DIR = ROOT / "papers"
EPRINT_PATTERN = re.compile(
    r"https?://(?:www\.)?eprint\.iacr\.org/"
    r"(?P<year>\d{4})/(?P<number>\d+)(?:\.pdf)?"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
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
    driver: object,
    directory: Path,
    files_before: set[Path],
    timeout: int,
    headless: bool,
) -> Path:
    deadline = time.monotonic() + timeout
    challenge_announced = False

    while time.monotonic() < deadline:
        new_files = completed_downloads(directory) - files_before
        for path in new_files:
            if is_valid_pdf(path):
                return path

        try:
            title = str(getattr(driver, "title", ""))
        except Exception:
            # Chrome may briefly detach the page while handing a PDF to its
            # download manager. The file watcher remains authoritative.
            title = ""
        if "just a moment" in title.lower() and not challenge_announced:
            if headless:
                print("    Cloudflare challenge detected (headless mode cannot solve it).")
            else:
                print("    Complete the Cloudflare check in the Chrome window; waiting …")
            challenge_announced = True
        time.sleep(0.5)

    raise DownloadError(f"browser download timed out after {timeout} seconds")


def download_with_selenium(
    papers: list[Paper],
    output_dir: Path,
    timeout: int,
    headless: bool,
    no_sandbox: bool,
) -> tuple[list[Paper], list[tuple[Paper, str]]]:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        raise DownloadError(
            "Selenium is not installed. Run: "
            "python3 -m pip install -r requirements-download-papers.txt"
        ) from exc

    downloaded: list[Paper] = []
    failed: list[tuple[Paper, str]] = []

    with tempfile.TemporaryDirectory(prefix="paper-download-") as temporary_dir:
        browser_download_dir = Path(temporary_dir).resolve()
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        if no_sandbox:
            options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(browser_download_dir),
                "download.directory_upgrade": True,
                "download.prompt_for_download": False,
                "plugins.always_open_pdf_externally": True,
                "safebrowsing.enabled": True,
            },
        )

        try:
            driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise DownloadError(f"Could not start Chrome through Selenium: {exc}") from exc

        try:
            driver.set_page_load_timeout(timeout)
            try:
                driver.execute_cdp_cmd(
                    "Browser.setDownloadBehavior",
                    {
                        "behavior": "allow",
                        "downloadPath": str(browser_download_dir),
                    },
                )
            except WebDriverException:
                # Chrome preferences above are sufficient on versions without this CDP method.
                pass

            for paper in papers:
                print(f"  Browser: {paper.paper_id}")
                files_before = completed_downloads(browser_download_dir)
                navigation_error = ""
                try:
                    driver.get(paper.landing_url)
                    driver.get(paper.pdf_url)
                except TimeoutException:
                    # A download can abort or outlive navigation while still succeeding.
                    pass
                except WebDriverException as exc:
                    # Chrome commonly reports ERR_ABORTED when navigation turns
                    # into a download, so check the download directory first.
                    navigation_error = str(exc)

                try:
                    downloaded_file = wait_for_browser_download(
                        driver,
                        browser_download_dir,
                        files_before,
                        timeout,
                        headless,
                    )
                    target = output_dir / paper.filename
                    os.replace(downloaded_file, target)
                    downloaded.append(paper)
                except DownloadError as exc:
                    failed.append((paper, navigation_error or str(exc)))
        finally:
            driver.quit()

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
        help="do not open Selenium when direct downloads fail",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run Chrome without a window (cannot complete interactive challenges)",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="pass --no-sandbox to Chrome (occasionally needed in containers)",
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
    opener = make_http_opener()
    downloaded: list[Paper] = []
    skipped: list[Paper] = []
    browser_pending: list[Paper] = []

    for paper in papers:
        target = output_dir / paper.filename
        if target.exists() and is_valid_pdf(target) and not arguments.force:
            print(f"  Skip:    {paper.paper_id} (valid file exists)")
            skipped.append(paper)
            continue

        print(f"  Direct:  {paper.paper_id}")
        try:
            download_direct(paper, target, opener, min(arguments.timeout, 60))
            downloaded.append(paper)
        except DownloadError as exc:
            print(f"    Direct download failed: {exc}")
            browser_pending.append(paper)

    failures: list[tuple[Paper, str]] = []
    if browser_pending and not arguments.direct_only:
        print(f"Trying {len(browser_pending)} paper(s) in Chrome via Selenium …")
        try:
            browser_downloaded, failures = download_with_selenium(
                browser_pending,
                output_dir,
                arguments.timeout,
                arguments.headless,
                arguments.no_sandbox,
            )
            downloaded.extend(browser_downloaded)
        except DownloadError as exc:
            failures = [(paper, str(exc)) for paper in browser_pending]
    elif browser_pending:
        failures = [(paper, "direct download failed") for paper in browser_pending]

    print(
        f"Finished: {len(downloaded)} downloaded, "
        f"{len(skipped)} already present, {len(failures)} failed."
    )
    for paper, reason in failures:
        print(f"  FAILED {paper.paper_id}: {reason}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
