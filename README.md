# Virt-News Aggregator

A Python script that aggregates and organizes virtualization release news into a clean, interactive HTML report.

## Features

- **Parallel Fetching:** Scrapes the last 10 releases concurrently from:
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
- **Efficient Caching:** Uses a local `virt_news_cache.json` to store parsed news. Entries expire after 7 days by default so data stays fresh without unnecessary re-fetching.
- **Deep Content Parsing:** Automatically follows links to detailed Markdown release notes (e.g., for Confidential Containers) to extract full context.
- **Multiple Output Formats:** HTML report, JSON, or RSS feed.
- **Interactive Report:** Generates `virt_news_report.html` with:
  - Tabbed navigation for quick project switching.
  - **Live filtering** by architecture (x86_64 / aarch64 / ppc64 / s390x), Confidential Computing keywords, and free-text search.
  - Dynamic collapsing sections using native HTML `<details>` and `<summary>`.
  - Color-coded architecture and Confidential Computing badges.
  - **Dark mode** toggle (preference saved in browser localStorage).
  - Direct links to source release notes.

## Requirements

- Python 3.9+
- `requests`
- `beautifulsoup4`
- `markdown`
- `packaging`

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Basic run (generates `virt_news_report.html`):

```bash
python3 virt_news.py
```

Open the report:

```bash
xdg-open virt_news_report.html
```

### CLI Options

```
--no-cache            bypass cache, re-fetch everything
--output FILE         custom output file path
--limit N             number of releases per project (default: 10)
--projects LIST       comma-separated subset of projects to fetch
--cache-file PATH     custom cache file location (default: virt_news_cache.json)
--cache-ttl DAYS      cache TTL in days; 0 = never expire (default: 7)
--format FORMAT       output format: html, json, or rss (default: html)
```

### Examples

```bash
# Fetch only QEMU and Libvirt, last 5 releases each
python3 virt_news.py --projects "QEMU,Libvirt" --limit 5

# Force a full re-fetch ignoring the cache
python3 virt_news.py --no-cache

# Generate a JSON dump
python3 virt_news.py --format json --output virt_news.json

# Generate an RSS feed
python3 virt_news.py --format rss --output virt_news.rss

# Use a longer cache TTL (30 days) and a custom cache file
python3 virt_news.py --cache-ttl 30 --cache-file ~/.cache/virt_news.json
```

Available project names for `--projects`:
`QEMU`, `Libvirt`, `Virt-Manager`, `Kernel KVM`, `EDK2 / OVMF`, `Cockpit Machines`, `Confidential Containers`

## Files

- `virt_news.py`: The main aggregation script.
- `requirements.txt`: Python dependencies.
- `virt_news_cache.json`: Local cache of parsed news (generated after first run).
- `virt_news_report.html`: The generated interactive HTML report.

## Configuration

The script ignores bug fixes and security-only updates to keep the report focused on architectural changes and new capabilities. The `RELEASE_LIMIT`, `CACHE_TTL_DAYS`, and `REQUEST_TIMEOUT` constants at the top of `virt_news.py` set the defaults used when no CLI flags are provided.
