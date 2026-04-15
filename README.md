# Virt-News Aggregator

A Python script that aggregates and organizes release news from QEMU, Libvirt, and Virt-Manager into a clean, interactive HTML report.

## Features

- **Automated Fetching:** Scrapes the last 5 releases from:
  - [QEMU ChangeLog](https://wiki.qemu.org/ChangeLog/)
  - [Libvirt News](https://libvirt.org/news.html)
  - [Virt-Manager Releases](https://github.com/virt-manager/virt-manager/releases)
- **Smart Filtering:** Focuses on **New Features** and **Deprecations**.
- **Architectural Focus:** Highlights news related to `x86_64`, `aarch64`, `ppc64`, and `s390x` (for QEMU and Libvirt).
- **Interactive Report:** Generates a `virt_news_report.html` with:
  - Sticky top navigation bar for quick access to projects.
  - Dynamic collapsing sections using native HTML `<details>` and `<summary>`.
  - Color-coded architecture tags.
  - Direct links to source release notes.

## Requirements

- Python 3.x
- `requests`
- `beautifulsoup4`
- `markdown`

## Usage

1. Run the aggregator script:
   ```bash
   python3 virt_news.py
   ```
2. Open the generated report in your browser:
   ```bash
   xdg-open virt_news_report.html
   ```

## Configuration

The script is configured to ignore bug fixes and security-only updates to keep the report focused on architectural changes and new capabilities.
