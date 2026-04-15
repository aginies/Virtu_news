# Virt-News Aggregator

A Python script that aggregates and organizes virtualization release news into a clean, interactive HTML report.

## Features

- **Automated Fetching:** Scrapes the last 10 releases from:
  - [QEMU ChangeLog](https://wiki.qemu.org/ChangeLog/)
  - [Libvirt News](https://libvirt.org/news.html)
  - [Virt-Manager Releases](https://github.com/virt-manager/virt-manager/releases)
  - [Linux Kernel KVM](https://kernelnewbies.org/LinuxVersions)
  - [EDK2 / OVMF](https://github.com/tianocore/edk2/releases)
  - [Cockpit-machines](https://github.com/cockpit-project/cockpit-machines/releases)
  - [Confidential Containers](https://github.com/confidential-containers/confidential-containers/releases)
- **Smart Filtering:** Focuses on **New Features**, **Improvements**, and **Deprecations**.
- **Architectural & Security Focus:** 
  - Highlights news for `x86_64`, `aarch64`, `ppc64`, and `s390x`.
  - **Confidential Computing Tracking:** Automatically detects and badges news related to `TDX`, `SEV`, `SEV-SNP`, `SGX`, `TrustZone`, `CCA`, `Realm`, etc.
- **Efficient Caching:** Uses a local `virt_news_cache.json` to store parsed news, only fetching and parsing new releases to significantly speed up subsequent runs.
- **Deep Content Parsing:** Automatically follows links to detailed Markdown release notes (e.g., for Confidential Containers) to extract full context.
- **Interactive Report:** Generates a `virt_news_report.html` with:
  - Tabbed navigation for quick project switching.
  - Dynamic collapsing sections using native HTML `<details>` and `<summary>`.
  - Color-coded architecture and Confidential Computing badges.
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

## Files

- `virt_news.py`: The main aggregation script.
- `virt_news_cache.json`: Local cache of parsed news (generated after first run).
- `virt_news_report.html`: The generated interactive report.

## Configuration

The script is configured to ignore bug fixes and security-only updates to keep the report focused on architectural changes and new capabilities.
