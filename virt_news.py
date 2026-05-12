#!/usr/bin/env python3
import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import bleach
import markdown
import requests
from bs4 import BeautifulSoup
from packaging.version import Version

# Configuration defaults
RELEASE_LIMIT = 10
CACHE_FILE = "virt_news_cache.json"
REQUEST_TIMEOUT = 15
CACHE_TTL_DAYS = 7
USER_AGENT = "virt-news-aggregator/1.0 (https://github.com/aginies/Virtu_news)"

# ANSI color codes
_CSI = "\033["
COLORS = {
    "cyan":    f"{_CSI}36m",
    "green":   f"{_CSI}32m",
    "yellow":  f"{_CSI}33m",
    "red":     f"{_CSI}31m",
    "bold":    f"{_CSI}1m",
    "underline": f"{_CSI}4m",
    "reset":   f"{_CSI}0m",
    "dim":     f"{_CSI}2m",
}

def _c(color, text):
    """Wrap text in ANSI color."""
    return COLORS[color] + str(text) + COLORS["reset"]


def _session():
    """Return a requests.Session with a proper User-Agent and, if set,
    a GitHub token for higher API rate limits (60 → 5000 req/h)."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


# Module-level session reused across all fetchers
_SESSION = _session()


def load_cache(cache_file=CACHE_FILE, ttl_days=CACHE_TTL_DAYS):
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, "r") as f:
            raw = json.load(f)
        if ttl_days <= 0:
            return raw
        cutoff = datetime.now().timestamp() - ttl_days * 86400
        filtered = {}
        expired = 0
        for key, value in raw.items():
            if key == "_meta":
                filtered["_meta"] = value
                continue
            if not isinstance(value, dict):
                continue
            valid = {}
            for version, entry in value.items():
                if entry.get("cached_at", 0) >= cutoff:
                    valid[version] = entry
                else:
                    expired += 1
            if valid:
                filtered[key] = valid
        if expired:
            print(
                f"Cache: dropped {expired} expired entr{'y' if expired == 1 else 'ies'} (TTL={ttl_days}d)"
            )
        return filtered
    except Exception as e:
        print(f"Error loading cache: {e}")
        return {}


def save_cache(cache, cache_file=CACHE_FILE):
    try:
        tmp = cache_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=4)
        os.replace(tmp, cache_file)
    except Exception as e:
        print(f"Error saving cache: {e}")


ARCH_KEYWORDS = {
    "x86_64": ["x86", "x86_64", "amd64", "intel"],
    "aarch64": ["arm", "aarch64", "arm64"],
    "ppc64": ["powerpc", "ppc64", "ppc", "power"],
    "s390x": ["s390x", "s390"],
}

CC_KEYWORDS = [
    "tdx",
    "sev",
    "sev-snp",
    "sgx",
    "trustzone",
    "pef",
    "confidential computing",
    "secure execution",
    "cvm",
    "cca",
    "pvm",
    "realm",
]

RELEVANT_CATEGORIES = [
    "new features",
    "removed features",
    "deprecated",
    "improvements",
    "new deprecated options and features",
]
QEMU_CATEGORIES = RELEVANT_CATEGORIES + [
    "kvm",
    "migration",
    "device emulation and assignment",
    "memory backends",
    "monitor",
]
IRRELEVANT_KEYWORDS = ["bug fix", "bugfix", "fixes", "security"]


def is_relevant_arch(text):
    text_lower = text.lower()
    for arch, keywords in ARCH_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return True
    return False


def get_archs_in_text(text):
    text_lower = text.lower()
    found = []
    for arch, keywords in ARCH_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            found.append(arch)
    return found


def get_cc_in_text(text):
    text_lower = text.lower()
    found = []
    for kw in CC_KEYWORDS:
        if kw in text_lower:
            found.append(kw)
    return found


def get_latest_qemu_versions(limit=RELEASE_LIMIT):
    try:
        r = _SESSION.get("https://wiki.qemu.org/ChangeLog/", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        links = soup.find_all("a", href=re.compile(r"^/ChangeLog/\d+\.\d+$"))
        versions = []
        for link in links:
            ver_str = link["href"].split("/")[-1]
            versions.append(ver_str)
        if not versions:
            return []
        versions.sort(key=Version, reverse=True)
        return versions[:limit]
    except Exception as e:
        print(f"Error finding QEMU versions: {e}")
        return []


def get_qemu_news(cache_data=None, limit=RELEASE_LIMIT):
    versions = get_latest_qemu_versions(limit)
    all_qemu_releases = []

    project_cache = cache_data.get("QEMU", {}) if cache_data else {}

    for version in versions:
        if version in project_cache:
            all_qemu_releases.append(project_cache[version])
            continue

        url = f"https://wiki.qemu.org/ChangeLog/{version}"
        try:
            r = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "html.parser")
            news_items = []

            processed_headers = set()
            for header in soup.find_all(["h2", "h3", "h4"]):
                if header in processed_headers:
                    continue

                header_text = header.get_text().strip()
                is_arch = is_relevant_arch(header_text)
                is_cat = any(cat in header_text.lower() for cat in QEMU_CATEGORIES)

                if is_arch or is_cat:
                    h_level = int(header.name[1])
                    content_blocks = []
                    has_real_content = False

                    next_node = header.find_next_sibling()
                    while next_node:
                        if next_node.name and next_node.name.startswith("h"):
                            next_level = int(next_node.name[1])
                            if next_level <= h_level:
                                break

                            # Sub-header: check if it has content before adding it
                            sub_text = next_node.get_text().strip()
                            sub_content = []
                            sub_has_real = False

                            sub_next = next_node.find_next_sibling()
                            while sub_next:
                                if sub_next.name and sub_next.name.startswith("h"):
                                    break
                                if sub_next.name in ["ul", "ol"]:
                                    if sub_next.find("li"):
                                        sub_content.append(str(sub_next))
                                        sub_has_real = True
                                elif sub_next.name == "p":
                                    if sub_next.get_text().strip():
                                        sub_content.append(str(sub_next))
                                        sub_has_real = True
                                sub_next = sub_next.find_next_sibling()

                            if sub_has_real:
                                content_blocks.append(f"<h4>{sub_text}</h4>")
                                content_blocks.extend(sub_content)
                                has_real_content = True

                            processed_headers.add(next_node)
                            # Skip ahead to after this sub-header's content
                            next_node = sub_next
                            continue

                        elif next_node.name in ["ul", "ol"]:
                            if next_node.find("li"):
                                content_blocks.append(str(next_node))
                                has_real_content = True
                        elif next_node.name == "p":
                            if next_node.get_text().strip():
                                content_blocks.append(str(next_node))
                                has_real_content = True

                        next_node = next_node.find_next_sibling()

                    if has_real_content:
                        combined_text = header_text + "".join(content_blocks)
                        news_items.append(
                            {
                                "category": f"<b>{header_text}</b>",
                                "archs": get_archs_in_text(header_text),
                                "cc_keywords": get_cc_in_text(combined_text),
                                "content": "".join(content_blocks),
                            }
                        )

            all_qemu_releases.append(
                {"version": version, "news": news_items, "url": url}
            )
        except Exception as e:
            print(f"Error fetching QEMU news for {version}: {e}")

    return {"name": "QEMU", "releases": all_qemu_releases, "arch_dependent": True}


def get_libvirt_news(cache_data=None, limit=RELEASE_LIMIT):
    url = "https://libvirt.org/news.html"
    try:
        r = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        all_libvirt_releases = []
        release_headers = [
            h1
            for h1 in soup.find_all("h1")
            if "(" in h1.text and "unreleased" not in h1.text.lower()
        ]

        project_cache = cache_data.get("Libvirt", {}) if cache_data else {}

        for h1 in release_headers[:limit]:
            version_text = h1.text.strip().replace("¶", "").strip()

            if version_text in project_cache:
                all_libvirt_releases.append(project_cache[version_text])
                continue

            # Try to find the section ID for anchoring
            section_id = ""
            parent_div = h1.find_parent("div", class_="section")
            if parent_div and parent_div.has_attr("id"):
                section_id = f"#{parent_div['id']}"
            elif h1.has_attr("id"):
                section_id = f"#{h1['id']}"

            release_ul = h1.find_next_sibling("ul")

            if not release_ul:
                continue

            news_items = []
            for li in release_ul.find_all("li", recursive=False):
                strong = li.find("strong")
                if strong:
                    cat_text = strong.text.strip()
                    if (
                        any(cat in cat_text.lower() for cat in RELEVANT_CATEGORIES)
                        and "bug fix" not in cat_text.lower()
                    ):
                        sub_ul = li.find("ul")
                        if sub_ul:
                            for sub_li in sub_ul.find_all("li", recursive=False):
                                item_text = sub_li.get_text()
                                # Extract a title from the content
                                first_p = sub_li.find("p")
                                title_text = (
                                    first_p.text.strip()
                                    if first_p
                                    else item_text.split("\n")[0].strip()
                                )
                                display_title = title_text[:100]
                                if len(title_text) > 100:
                                    display_title += "..."
                                display_cat = f"<b>{cat_text}</b>: {display_title}"

                                news_items.append(
                                    {
                                        "category": display_cat,
                                        "archs": get_archs_in_text(item_text),
                                        "cc_keywords": get_cc_in_text(item_text),
                                        "content": str(sub_li),
                                    }
                                )
                        else:
                            content = "".join(
                                [str(c) for c in li.contents if c.name != "strong"]
                            )
                            if content.strip():
                                news_items.append(
                                    {
                                        "category": f"<b>{cat_text}</b>",
                                        "archs": get_archs_in_text(cat_text),
                                        "cc_keywords": get_cc_in_text(content),
                                        "content": content,
                                    }
                                )

            all_libvirt_releases.append(
                {
                    "version": version_text,
                    "news": news_items,
                    "url": f"{url}{section_id}",
                }
            )

        return {
            "name": "Libvirt",
            "releases": all_libvirt_releases,
            "arch_dependent": True,
        }
    except Exception as e:
        print(f"Error fetching Libvirt news: {e}")
        return {"name": "Libvirt", "releases": []}


def get_github_news(
    repo_path, project_name, arch_dependent=False, cache_data=None, limit=RELEASE_LIMIT
):
    per_page = min(limit, 100)  # GitHub API cap is 100 per page
    url = f"https://api.github.com/repos/{repo_path}/releases?per_page={per_page}"
    try:
        r = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data or not isinstance(data, list):
            return {
                "name": project_name,
                "releases": [],
                "arch_dependent": arch_dependent,
            }

        project_cache = cache_data.get(project_name, {}) if cache_data else {}

        all_releases = []
        for release in data[:limit]:
            version = release["tag_name"]

            if version in project_cache:
                all_releases.append(project_cache[version])
                continue

            body = release.get("body", "")
            if not body:
                continue

            # Check for linked detailed markdown file (common in confidential-containers)
            # Example: [release notes](https://github.com/confidential-containers/confidential-containers/blob/main/releases/v0.18.0.md)
            md_match = re.search(
                r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/([^)\s]+\.md)", body
            )
            if md_match:
                raw_url = (
                    f"https://raw.githubusercontent.com/"
                    f"{md_match.group(1)}/{md_match.group(2)}/"
                    f"{md_match.group(3)}/{md_match.group(4)}"
                )
                try:
                    md_r = _SESSION.get(raw_url, timeout=REQUEST_TIMEOUT)
                    if md_r.status_code == 200:
                        body = md_r.text
                except Exception:
                    pass

            html_body = markdown.markdown(body)
            html_body = bleach.clean(
                html_body,
                tags=[
                    "p",
                    "ul",
                    "ol",
                    "li",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "strong",
                    "em",
                    "code",
                    "del",
                    "pre",
                    "blockquote",
                    "br",
                ],
                attributes={},
                strip=True,
            )
            soup = BeautifulSoup(html_body, "html.parser")

            news_items = []
            current_cat = "Release Notes"
            for element in soup.children:
                if element.name in ["h1", "h2", "h3"]:
                    current_cat = element.get_text()
                elif element.name in ["ul", "ol", "p"]:
                    text = element.get_text().strip()
                    if text and "bug" not in text.lower():
                        # Use first line as a title
                        first_line = text.split("\n")[0].strip()
                        title = first_line[:100] + (
                            "..." if len(first_line) > 100 else ""
                        )

                        news_items.append(
                            {
                                "category": f"<b>{current_cat}</b>: {title}",
                                "archs": get_archs_in_text(text)
                                if arch_dependent
                                else [],
                                "cc_keywords": get_cc_in_text(text),
                                "content": str(element),
                            }
                        )

            all_releases.append(
                {
                    "version": version,
                    "news": news_items,
                    "url": release.get(
                        "html_url",
                        f"https://github.com/{repo_path}/releases/tag/{version}",
                    ),
                }
            )

        return {
            "name": project_name,
            "releases": all_releases,
            "arch_dependent": arch_dependent,
        }
    except Exception as e:
        print(f"Error fetching {project_name} news: {e}")
        return {"name": project_name, "releases": [], "arch_dependent": arch_dependent}


def get_kernel_kvm_news(cache_data=None, limit=RELEASE_LIMIT):
    url_base = "https://kernelnewbies.org"
    try:
        # LinuxVersions has a clear list of version links
        r = _SESSION.get(f"{url_base}/LinuxVersions", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")

        # Find latest release links
        release_links = []
        for a in soup.find_all("a", href=re.compile(r"^/Linux_\d+\.\d+$")):
            # The text should be like '6.13' or 'Linux 6.13'
            ver_text = a.text.strip()
            if ver_text:
                release_links.append((ver_text, a["href"]))

        # Also check LinuxChanges which often points to the absolute latest (even if not in LinuxVersions yet)
        try:
            r_lc = _SESSION.get(f"{url_base}/LinuxChanges", timeout=REQUEST_TIMEOUT)
            r_lc.raise_for_status()
            soup_lc = BeautifulSoup(r_lc.content, "html.parser")
            # Look for the main title or first H1/H2 link
            latest_h1 = soup_lc.find("h1")
            if latest_h1:
                atag = latest_h1.find("a")
                if atag and atag.get("href", "").startswith("/Linux_"):
                    ver_text = atag.text.strip()
                    if ver_text:
                        release_links.insert(0, (ver_text, atag["href"]))
        except Exception:
            pass

        # Deduplicate and maintain order (latest first)
        seen = set()
        unique_links = []
        for name, path in release_links:
            clean_name = name.replace("Linux ", "").strip()
            if clean_name not in seen:
                unique_links.append((name, path))
                seen.add(clean_name)

        project_cache = cache_data.get("Kernel KVM", {}) if cache_data else {}

        # Take the most recent ones
        all_kernel_releases = []
        for name, path in unique_links[:limit]:
            version = f"Linux {name}" if "Linux" not in name else name

            if version in project_cache:
                all_kernel_releases.append(project_cache[version])
                continue

            rel_url = f"{url_base}{path}"
            try:
                r_rel = _SESSION.get(rel_url, timeout=REQUEST_TIMEOUT)
                r_rel.raise_for_status()
                soup_rel = BeautifulSoup(r_rel.content, "html.parser")

                news_items = []
                # Look for a header that mentions Virtualization or KVM
                kvm_section = None
                for header in soup_rel.find_all(["h1", "h2", "h3"]):
                    h_text = header.get_text().strip()
                    if "Virtualization" in h_text or "KVM" in h_text:
                        kvm_section = header
                        break

                if kvm_section:
                    # Content is usually in the next <ul> or <ol>
                    curr = kvm_section.find_next_sibling()
                    while curr and curr.name not in ["h1", "h2", "h3"]:
                        if curr.name in ["ul", "ol"]:
                            for li in curr.find_all("li", recursive=False):
                                text = li.get_text().strip()
                                clean_text = text.replace("commit", "").strip()
                                # Clean up trailing commas and multiple spaces
                                clean_text = re.sub(r"[,\s]+$", "", clean_text)
                                title = clean_text.split("\n")[0].strip()
                                # Some titles have internal multiple commas at the end of the first line
                                title = re.sub(r"[,\s]+$", "", title)
                                if len(title) > 100:
                                    title = title[:100] + "..."

                                news_items.append(
                                    {
                                        "category": f"<b>{title}</b>"
                                        if title
                                        else "<b>Virtualization Update</b>",
                                        "archs": get_archs_in_text(clean_text),
                                        "cc_keywords": get_cc_in_text(clean_text),
                                        "content": str(li),
                                    }
                                )
                        curr = curr.find_next_sibling()

                all_kernel_releases.append(
                    {"version": version, "news": news_items, "url": rel_url}
                )
            except Exception as e:
                print(f"Error fetching detail for {name}: {e}")

        return {
            "name": "Kernel KVM",
            "releases": all_kernel_releases,
            "arch_dependent": True,
        }
    except Exception as e:
        print(f"Error fetching Kernel KVM news: {e}")
        return {"name": "Kernel KVM", "releases": []}


# Project registry — drives both parallel fetching and --projects filtering
PROJECTS_CONFIG = [
    {
        "name": "QEMU",
        "fetch": lambda cache, lim: get_qemu_news(cache, lim),
    },
    {
        "name": "Libvirt",
        "fetch": lambda cache, lim: get_libvirt_news(cache, lim),
    },
    {
        "name": "Virt-Manager",
        "fetch": lambda cache, lim: get_github_news(
            "virt-manager/virt-manager", "Virt-Manager", False, cache, lim
        ),
    },
    {
        "name": "Kernel KVM",
        "fetch": lambda cache, lim: get_kernel_kvm_news(cache, lim),
    },
    {
        "name": "EDK2 / OVMF",
        "fetch": lambda cache, lim: get_github_news(
            "tianocore/edk2", "EDK2 / OVMF", False, cache, lim
        ),
    },
    {
        "name": "Cockpit Machines",
        "fetch": lambda cache, lim: get_github_news(
            "cockpit-project/cockpit-machines", "Cockpit Machines", False, cache, lim
        ),
    },
    {
        "name": "Confidential Containers",
        "fetch": lambda cache, lim: get_github_news(
            "confidential-containers/confidential-containers",
            "Confidential Containers",
            False,
            cache,
            lim,
        ),
    },
]


def generate_html(all_news):
    # Compute tab IDs first — needed for CSS selector generation
    tab_ids = [re.sub(r"[^a-zA-Z0-9-]", "-", p["name"]) for p in all_news]

    # Dynamic CSS selectors for the radio-button tab system
    active_selectors = ",\n        ".join(
        f'#tab-{pid}:checked ~ .tabs-nav label[for="tab-{pid}"]' for pid in tab_ids
    )
    content_selectors = ",\n        ".join(
        f"#tab-{pid}:checked ~ #content-{pid}" for pid in tab_ids
    )

    html_template = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Virt-News Aggregator</title>
    <style>
        :root {
            --primary-color: #2563eb;
            --primary-dark: #1d4ed8;
            --bg-primary: #f8fafc;
            --bg-secondary: #ffffff;
            --bg-tertiary: #f1f5f9;
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --shadow: rgba(0, 0, 0, 0.1);
            --arch-x86_64: #fca5a5;
            --arch-aarch64: #86efac;
            --arch-ppc64: #93c5fd;
            --arch-s390x: #e879f9;
            --cc-color: #2dd4bf;
        }

        body.dark {
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-tertiary: #334155;
            --text-primary: #f1f5f9;
            --text-secondary: #cbd5e1;
            --text-muted: #94a3b8;
            --border-color: #475569;
            --shadow: rgba(0, 0, 0, 0.4);
        }
        body.dark .arch-x86_64 { background: #7f1d1d; color: #fca5a5; }
        body.dark .arch-aarch64 { background: #14532d; color: #86efac; }
        body.dark .arch-ppc64  { background: #1e3a8a; color: #93c5fd; }
        body.dark .arch-s390x  { background: #581c87; color: #e879f9; }
        body.dark .cc-tag      { background: #134e4a; color: #2dd4bf; }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            min-height: 100vh;
        }

        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }

        header {
            text-align: center;
            padding: 40px 20px;
            background: var(--bg-secondary);
            border-radius: 16px;
            margin-bottom: 20px;
            box-shadow: 0 4px 20px var(--shadow);
            border: 1px solid var(--border-color);
        }

        h1 {
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(90deg, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 10px;
        }

        .metadata { color: var(--text-secondary); font-size: 0.9rem; }

        /* ── Dark mode toggle ── */
        .dark-toggle {
            position: fixed;
            top: 16px;
            right: 20px;
            padding: 7px 16px;
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--text-primary);
            z-index: 1000;
            transition: all 0.3s;
            box-shadow: 0 2px 8px var(--shadow);
        }
        .dark-toggle:hover { background: var(--primary-color); color: white; border-color: var(--primary-color); }

        /* ── Filter bar ── */
        .filter-bar {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 14px 18px;
            margin-bottom: 20px;
            box-shadow: 0 4px 16px var(--shadow);
            border: 1px solid var(--border-color);
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
        }
        .filter-section { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
        .filter-label {
            font-size: 0.75rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .filter-btn {
            padding: 5px 12px;
            border: 2px solid var(--border-color);
            border-radius: 20px;
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 0.78rem;
            font-weight: 600;
            transition: all 0.2s;
        }
        .filter-btn:hover { border-color: var(--primary-color); color: var(--primary-color); }
        .filter-btn.active { background: var(--primary-color); color: white; border-color: var(--primary-color); }
        .filter-divider { width: 1px; height: 22px; background: var(--border-color); flex-shrink: 0; }
        #search-input {
            padding: 5px 14px;
            border: 2px solid var(--border-color);
            border-radius: 20px;
            background: var(--bg-primary);
            color: var(--text-primary);
            font-size: 0.85rem;
            outline: none;
            transition: border-color 0.2s;
            width: 210px;
        }
        #search-input:focus { border-color: var(--primary-color); }
        #search-input::placeholder { color: var(--text-muted); }
        .filter-count { font-size: 0.8rem; color: var(--text-muted); margin-left: auto; }

        /* ── Tabs ── */
        .tabs-nav { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
        input[type="radio"].tab-radio { display: none; }
        .tabs-nav label {
            padding: 10px 20px;
            background: var(--bg-secondary);
            color: var(--text-secondary);
            border: 2px solid var(--border-color);
            border-radius: 12px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.9rem;
            transition: all 0.3s;
        }
        .tabs-nav label:hover { background: var(--bg-tertiary); color: var(--text-primary); }

        ACTIVE_SELECTORS_PLACEHOLDER {
            background: var(--primary-color);
            color: white;
            border-color: var(--primary-color);
        }

        .tab-content { display: none; }

        CONTENT_SELECTORS_PLACEHOLDER {
            display: block;
            animation: fadeIn 0.3s ease;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        /* ── Release cards ── */
        .release {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 4px 16px var(--shadow);
        }
        .release-header {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid var(--border-color);
            flex-wrap: wrap;
        }
        .release-title { font-size: 1.4rem; font-weight: 700; color: var(--text-primary); }
        .release-link {
            color: var(--primary-color);
            text-decoration: none;
            font-size: 0.85rem;
            padding: 4px 12px;
            background: var(--bg-tertiary);
            border-radius: 20px;
            transition: all 0.3s;
        }
        .release-link:hover { background: var(--primary-color); color: white; }

        /* ── News items ── */
        details {
            background: var(--bg-tertiary);
            border-radius: 8px;
            margin-bottom: 12px;
            overflow: hidden;
            transition: all 0.3s;
        }
        details:hover { background: var(--bg-secondary); }
        details[open] { box-shadow: 0 4px 12px var(--shadow); }
        summary {
            padding: 16px;
            cursor: pointer;
            font-weight: 600;
            font-size: 1rem;
            color: var(--text-primary);
            list-style: none;
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        summary::-webkit-details-marker { display: none; }
        summary::before {
            content: '▶';
            font-size: 0.7rem;
            transition: transform 0.3s;
            color: var(--text-secondary);
        }
        details[open] summary::before { transform: rotate(90deg); }

        .tags { display: flex; gap: 8px; flex-wrap: wrap; }
        .arch-tag {
            display: inline-flex;
            align-items: center;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
        }
        .arch-x86_64 { background: var(--arch-x86_64); color: #7f1d1d; }
        .arch-aarch64 { background: var(--arch-aarch64); color: #14532d; }
        .arch-ppc64   { background: var(--arch-ppc64);   color: #1d4ed8; }
        .arch-s390x   { background: var(--arch-s390x);   color: #86198f; }
        .cc-tag {
            display: inline-flex;
            align-items: center;
            padding: 4px 12px;
            background: var(--cc-color);
            color: #134e4a;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
        }

        .news-content { padding: 0 16px 16px; color: var(--text-secondary); }
        .news-content ul, .news-content ol { margin: 10px 0; padding-left: 20px; }
        .news-content li { margin: 5px 0; }
        .empty-message { text-align: center; padding: 40px; color: var(--text-muted); font-style: italic; }

        @media (max-width: 768px) {
            .container { padding: 10px; }
            h1 { font-size: 1.8rem; }
            header { padding: 30px 15px; }
            .release-header { flex-direction: column; align-items: flex-start; }
            .tabs-nav label { padding: 8px 16px; font-size: 0.85rem; }
            .dark-toggle { top: 8px; right: 8px; }
            #search-input { width: 150px; }
        }
    </style>
</head>
<body>
    <button class="dark-toggle" id="dark-toggle">&#9790; Dark</button>
    <div class="container">
        <header>
            <h1>&#128240; Virt-News Aggregator</h1>
            <div class="metadata">Generated on: %(datetime)s</div>
        </header>
        FILTER_BAR_PLACEHOLDER
        TABS_PLACEHOLDER
    </div>
    <script>
SCRIPTS_PLACEHOLDER
    </script>
</body>
</html>
"""

    # Inject dynamic CSS selectors
    html_template = html_template.replace(
        "ACTIVE_SELECTORS_PLACEHOLDER", active_selectors
    )
    html_template = html_template.replace(
        "CONTENT_SELECTORS_PLACEHOLDER", content_selectors
    )

    # ── Filter bar ──────────────────────────────────────────────────────────
    filter_bar_html = """\
<div class="filter-bar">
            <div class="filter-section">
                <span class="filter-label">Arch</span>
                <button class="filter-btn arch-btn active" data-arch="all">All</button>
                <button class="filter-btn arch-btn" data-arch="x86_64">x86_64</button>
                <button class="filter-btn arch-btn" data-arch="aarch64">aarch64</button>
                <button class="filter-btn arch-btn" data-arch="ppc64">ppc64</button>
                <button class="filter-btn arch-btn" data-arch="s390x">s390x</button>
            </div>
            <div class="filter-divider"></div>
            <div class="filter-section">
                <span class="filter-label">Focus</span>
                <button class="filter-btn" id="cc-toggle">CC only</button>
            </div>
            <div class="filter-divider"></div>
            <div class="filter-section">
                <input type="text" id="search-input" placeholder="Search items...">
            </div>
            <div class="filter-divider"></div>
            <div class="filter-section">
                <button class="filter-btn" id="reset-filters">Reset</button>
            </div>
            <span class="filter-count" id="filter-count"></span>
        </div>"""

    # ── Tab radios, labels, content ─────────────────────────────────────────
    tab_nav_radios = []
    tab_nav_labels = []
    tab_content_html = []

    for i, project in enumerate(all_news):
        p_id = tab_ids[i]
        checked = 'checked="checked"' if i == 0 else ""

        tab_nav_radios.append(
            f'<input type="radio" class="tab-radio" name="tabs" id="tab-{p_id}" {checked}>'
        )
        tab_nav_labels.append(f'<label for="tab-{p_id}">{project["name"]}</label>')

        content_html = f'<div class="tab-content" id="content-{p_id}">'

        for release in project["releases"]:
            if not release["news"]:
                continue

            content_html += '<div class="release">'
            content_html += '<div class="release-header">'
            content_html += f'<span class="release-title">{release["version"]}</span>'
            content_html += (
                f'<a class="release-link" href="{release["url"]}" target="_blank">'
                f"View Source</a>"
            )
            content_html += "</div>"

            for item in release["news"]:
                if project.get("arch_dependent"):
                    arch_tags = "".join(
                        f'<span class="arch-tag arch-{arch}">{arch}</span>'
                        for arch in item.get("archs", [])
                    )
                else:
                    arch_tags = ""

                cc_tags = ""
                if item.get("cc_keywords"):
                    unique_cc = sorted(set(item["cc_keywords"]))
                    cc_tags = "".join(
                        f'<span class="cc-tag">CC: {kw.upper()}</span>'
                        for kw in unique_cc
                    )

                archs_attr = " ".join(item.get("archs", []))
                cc_attr = " ".join(sorted(set(item.get("cc_keywords", []))))

                content_html += (
                    f'<details class="news-item" data-archs="{archs_attr}" data-cc="{cc_attr}">\n'
                    f"                    <summary>\n"
                    f'                        <span class="news-title">{item["category"]}</span>\n'
                    f'                        <div class="tags">{arch_tags}{cc_tags}</div>\n'
                    f"                    </summary>\n"
                    f'                    <div class="news-content">\n'
                    f"                        {item['content']}\n"
                    f"                    </div>\n"
                    f"                </details>\n"
                )

            content_html += "</div>"  # .release

        content_html += "</div>"  # .tab-content
        tab_content_html.append(content_html)

    tabs_html = (
        "".join(tab_nav_radios)
        + '\n        <div class="tabs-nav">'
        + "".join(tab_nav_labels)
        + "</div>\n        "
        + "".join(tab_content_html)
    )

    # ── JavaScript ──────────────────────────────────────────────────────────
    scripts_js = """\
// Dark mode with localStorage persistence
(function () {
    var toggle = document.getElementById('dark-toggle');
    if (localStorage.getItem('virt-news-theme') === 'dark') {
        document.body.classList.add('dark');
        toggle.innerHTML = '&#9728; Light';
    }
    toggle.addEventListener('click', function () {
        var isDark = document.body.classList.toggle('dark');
        toggle.innerHTML = isDark ? '&#9728; Light' : '&#9790; Dark';
        localStorage.setItem('virt-news-theme', isDark ? 'dark' : 'light');
    });
})();

// Filtering
(function () {
    var activeArch = 'all';
    var ccOnly = false;
    var searchText = '';

    function updateCount() {
        var total = document.querySelectorAll('details.news-item').length;
        var visible = 0;
        document.querySelectorAll('details.news-item').forEach(function (el) {
            if (el.style.display !== 'none') visible++;
        });
        var el = document.getElementById('filter-count');
        if (el) el.textContent = (total === visible) ? '' : (visible + ' / ' + total + ' items shown');
    }

    function applyFilters() {
        document.querySelectorAll('details.news-item').forEach(function (el) {
            var archs = (el.dataset.archs || '').split(' ').filter(Boolean);
            var hasCc = (el.dataset.cc || '').length > 0;
            var titleEl = el.querySelector('.news-title');
            var contentEl = el.querySelector('.news-content');
            var title = titleEl ? titleEl.textContent.toLowerCase() : '';
            var content = contentEl ? contentEl.textContent.toLowerCase() : '';

            var show = true;
            if (activeArch !== 'all' && archs.indexOf(activeArch) === -1) show = false;
            if (ccOnly && !hasCc) show = false;
            if (searchText && title.indexOf(searchText) === -1 && content.indexOf(searchText) === -1) show = false;

            el.style.display = show ? '' : 'none';
        });

        // Hide release cards that have no visible items
        document.querySelectorAll('.release').forEach(function (rel) {
            var items = rel.querySelectorAll('details.news-item');
            if (items.length === 0) return;
            var anyVisible = false;
            items.forEach(function (el) { if (el.style.display !== 'none') anyVisible = true; });
            rel.style.display = anyVisible ? '' : 'none';
        });

        updateCount();
    }

    document.querySelectorAll('.arch-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            document.querySelectorAll('.arch-btn').forEach(function (b) { b.classList.remove('active'); });
            btn.classList.add('active');
            activeArch = btn.dataset.arch;
            applyFilters();
        });
    });

    document.getElementById('cc-toggle').addEventListener('click', function () {
        ccOnly = !ccOnly;
        this.classList.toggle('active', ccOnly);
        applyFilters();
    });

    document.getElementById('search-input').addEventListener('input', function () {
        searchText = this.value.toLowerCase();
        applyFilters();
    });

    document.getElementById('reset-filters').addEventListener('click', function () {
        activeArch = 'all';
        ccOnly = false;
        searchText = '';
        document.querySelectorAll('.arch-btn').forEach(function (b) { b.classList.remove('active'); });
        var allBtn = document.querySelector('.arch-btn[data-arch="all"]');
        if (allBtn) allBtn.classList.add('active');
        document.getElementById('cc-toggle').classList.remove('active');
        document.getElementById('search-input').value = '';
        applyFilters();
    });
})();"""

    html = html_template
    html = html.replace("        FILTER_BAR_PLACEHOLDER", filter_bar_html)
    html = html.replace("        TABS_PLACEHOLDER", tabs_html)
    html = html.replace("SCRIPTS_PLACEHOLDER", scripts_js)
    html = html.replace("%(datetime)s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return html


# ── Diff mode ──────────────────────────────────────────────────────────────

def _version_key(v):
    """Sort key for version strings; falls back to string comparison."""
    try:
        return Version(v)
    except Exception:
        return Version("0.0.0")


def compute_diff(all_news, last_news):
    """Compare current report with previous report.

    Returns a dict:
        {
            "projects": [
                {
                    "name": ...,
                    "added": [{"version": ..., "news": ..., "url": ...}],
                    "removed": [{"version": ..., "news": ..., "url": ...}],
                    "changed": [{"version": ..., "old": ..., "new": ...}],
                }
            ]
        }
    """
    current_map = {}
    for proj in all_news:
        current_map[proj["name"]] = {r["version"]: r for r in proj["releases"]}

    last_map = {}
    for proj in last_news:
        last_map[proj["name"]] = {r["version"]: r for r in proj["releases"]}

    all_names = sorted(set(list(current_map.keys()) + list(last_map.keys())))

    projects = []
    for name in all_names:
        cur = current_map.get(name, {})
        lst = last_map.get(name, {})
        cur_vers = set(cur)
        lst_vers = set(lst)

        added = [cur[v] for v in sorted(cur_vers - lst_vers, key=_version_key)]
        removed = [lst[v] for v in sorted(lst_vers - cur_vers, key=_version_key)]
        changed = []
        for v in sorted(cur_vers & lst_vers, key=_version_key):
            old = lst[v]
            new = cur[v]
            # Compare news items
            old_news = set(n["category"] for n in old.get("news", []))
            new_news = set(n["category"] for n in new.get("news", []))
            if old_news != new_news or old.get("url") != new.get("url"):
                changed.append({"version": v, "old": old, "new": new})

        if added or removed or changed:
            projects.append({
                "name": name,
                "added": added,
                "removed": removed,
                "changed": changed,
            })

    return {"projects": projects}


def _count_news(items):
    """Count total news items in a release."""
    return sum(len(r.get("news", [])) for r in items)


def _plain_text(html_str):
    """Strip HTML tags and get plain text from a news item's content."""
    if not html_str:
        return ""
    try:
        soup = BeautifulSoup(html_str, "html.parser")
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        return html_str


def _short_text(html_str, max_len=120):
    """Get a short plain-text preview of a news item."""
    text = _plain_text(html_str)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _status_text(status):
    return {
        "added": _c("green", "ADDED"),
        "removed": _c("red", "REMOVED"),
        "changed": _c("yellow", "CHANGED"),
    }[status]


def generate_diff_summary(diff_result, now, last_run):
    """Generate a colorized, human-readable diff summary."""
    lines = []

    now_str = now.strftime("%Y-%m-%d %H:%M")
    last_str = last_run.strftime("%Y-%m-%d %H:%M") if last_run else "N/A"

    lines.append(_c("bold", f"  virt-news diff — {now_str} (last: {last_str})"))
    lines.append(_c("dim", "  " + "=" * 68))
    lines.append("")

    total_added = 0
    total_removed = 0
    total_changed = 0

    for proj in diff_result["projects"]:
        if not proj["added"] and not proj["removed"] and not proj["changed"]:
            continue

        for entry in proj["added"]:
            total_added += 1
            lines.append(
                f"  {_c('bold', proj['name'])} {_c('bold', entry['version'])}  {_status_text('added')}"
            )
            for item in entry.get("news", []):
                cat = item.get("category", "")
                text = _short_text(item.get("content", ""))
                if cat:
                    lines.append(f"    {_c('dim', f'• {cat}: {text}')}")
                else:
                    lines.append(f"    {_c('dim', f'• {text}')}")

        for entry in proj["removed"]:
            total_removed += 1
            lines.append(
                f"  {_c('bold', proj['name'])} {_c('bold', entry['version'])}  {_status_text('removed')}"
            )
            for item in entry.get("news", []):
                cat = item.get("category", "")
                text = _short_text(item.get("content", ""))
                if cat:
                    lines.append(f"    {_c('dim', f'• {cat}: {text}')}")
                else:
                    lines.append(f"    {_c('dim', f'• {text}')}")

        for entry in proj["changed"]:
            total_changed += 1
            old_count = len(entry["old"].get("news", []))
            new_count = len(entry["new"].get("news", []))
            if new_count != old_count:
                detail = f"{old_count} → {new_count} item(s)"
            else:
                detail = f"{new_count} item(s)"
            lines.append(
                f"  {_c('bold', proj['name'])} {_c('bold', entry['version'])}  {_status_text('changed')}  {detail}"
            )
            # Show only the items that differ
            old_news = {n["category"]: n for n in entry["old"].get("news", [])}
            new_news = {n["category"]: n for n in entry["new"].get("news", [])}
            old_cats = set(old_news)
            new_cats = set(new_news)
            for cat in sorted(old_cats & new_cats):
                old_text = _short_text(old_news[cat].get("content", ""))
                new_text = _short_text(new_news[cat].get("content", ""))
                if old_text != new_text:
                    lines.append(f"    {_c('yellow', f'• {cat}:')}")
                    lines.append(f"      {_c('red', f'- {old_text}')}")
                    lines.append(f"      {_c('green', f'+ {new_text}')}")
            for cat in sorted(old_cats - new_cats):
                old_text = _short_text(old_news[cat].get("content", ""))
                lines.append(f"    {_c('red', f'• - {cat}: {old_text}')}")
            for cat in sorted(new_cats - old_cats):
                new_text = _short_text(new_news[cat].get("content", ""))
                lines.append(f"    {_c('green', f'• + {cat}: {new_text}')}")

    lines.append("")
    lines.append(_c("dim", "  " + "-" * 68))
    parts = []
    if total_added:
        parts.append(_c("green", f"+{total_added} new"))
    if total_removed:
        parts.append(_c("red", f"-{total_removed} removed"))
    if total_changed:
        parts.append(_c("yellow", f"~{total_changed} changed"))
    if not parts:
        parts.append(_c("dim", "No changes detected"))
    lines.append("  " + "  ".join(parts))
    lines.append("")

    return "\n".join(lines)


def generate_diff_html(diff_result, now, last_run):
    """Generate an HTML report showing the diff summary."""
    now_str = now.strftime("%Y-%m-%d %H:%M")
    last_str = last_run.strftime("%Y-%m-%d %H:%M") if last_run else "N/A"

    status_class = {"added": "added", "removed": "removed", "changed": "changed"}
    status_label = {"added": "ADDED", "removed": "REMOVED", "changed": "CHANGED"}

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>virt-news diff</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f8fafc; color: #1e293b; margin: 0; padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ color: #2563eb; margin-bottom: 4px; }}
        .meta {{ color: #64748b; font-size: 0.9rem; margin-bottom: 24px; }}
        .status {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }}
        .status.added {{ background: #dcfce7; color: #166534; }}
        .status.removed {{ background: #fee2e2; color: #991b1b; }}
        .status.changed {{ background: #fef9c3; color: #854d0e; }}
        h2 {{ font-size: 1.1rem; margin: 20px 0 8px; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; }}
        table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
        th, td {{ text-align: left; padding: 6px 12px; border: 1px solid #e2e8f0; }}
        th {{ background: #f1f5f9; font-weight: 600; }}
        tr:nth-child(even) {{ background: #fafafa; }}
        .count {{ color: #64748b; font-size: 0.85rem; }}
        details {{ margin: 8px 0; border: 1px solid #e2e8f0; border-radius: 6px; }}
        summary {{ padding: 8px 12px; cursor: pointer; font-weight: 600; list-style: none; }}
        summary::-webkit-details-marker {{ display: none; }}
        h3 {{ font-size: 1rem; margin: 12px 0 4px; }}
    </style>
</head>
<body>
<div class="container">
    <h1>virt-news diff</h1>
    <div class="meta">Generated: {now_str} | Last run: {last_str}</div>
"""

    total_added = 0
    total_removed = 0
    total_changed = 0

    for proj in diff_result["projects"]:
        if not proj["added"] and not proj["removed"] and not proj["changed"]:
            continue

        html += f'<h2>{proj["name"]}</h2>\n'

        for entry in proj["added"]:
            total_added += 1
            html += f'<details><summary><b>{entry["version"]}</b> <span class="status added">ADDED</span></summary>\n'
            for item in entry.get("news", []):
                cat = item.get("category", "")
                text = _plain_text(item.get("content", ""))
                html += f'<div style="padding:4px 0;">{cat}: {text}</div>\n'
            html += '</details>\n'

        for entry in proj["removed"]:
            total_removed += 1
            html += f'<details><summary><b>{entry["version"]}</b> <span class="status removed">REMOVED</span></summary>\n'
            for item in entry.get("news", []):
                cat = item.get("category", "")
                text = _plain_text(item.get("content", ""))
                html += f'<div style="padding:4px 0;">{cat}: {text}</div>\n'
            html += '</details>\n'

        for entry in proj["changed"]:
            total_changed += 1
            old_count = len(entry["old"].get("news", []))
            new_count = len(entry["new"].get("news", []))
            if new_count != old_count:
                detail = f"{old_count} &rarr; {new_count}"
            else:
                detail = f"{new_count}"
            html += f'<h3>{entry["version"]} <span class="status changed">CHANGED</span> ({detail} items)</h3>\n'

            old_news = {n["category"]: n for n in entry["old"].get("news", [])}
            new_news = {n["category"]: n for n in entry["new"].get("news", [])}
            old_cats = set(old_news)
            new_cats = set(new_news)

            # Show only items that differ
            for cat in sorted(old_cats & new_cats):
                old_text = _plain_text(old_news[cat].get("content", ""))
                new_text = _plain_text(new_news[cat].get("content", ""))
                if old_text != new_text:
                    html += f'<details><summary>{cat}</summary>\n'
                    html += f'<div style="margin:4px 0;"><span style="color:#991b1b;">- {old_text}</span></div>\n'
                    html += f'<div style="margin:4px 0;"><span style="color:#166534;">+ {new_text}</span></div>\n'
                    html += '</details>\n'

            for cat in sorted(old_cats - new_cats):
                old_text = _plain_text(old_news[cat].get("content", ""))
                html += f'<div style="margin:4px 0;color:#991b1b;">{cat}: {old_text}</div>\n'
            for cat in sorted(new_cats - old_cats):
                new_text = _plain_text(new_news[cat].get("content", ""))
                html += f'<div style="margin:4px 0;color:#166534;">{cat}: {new_text}</div>\n'

    html += f"""\
    <hr style="margin: 24px 0; border: none; border-top: 1px solid #e2e8f0;">
    <div style="font-size: 0.9rem;">
"""
    parts = []
    if total_added:
        parts.append(f'<span style="color: #166534;">+{total_added} new</span>')
    if total_removed:
        parts.append(f'<span style="color: #991b1b;">-{total_removed} removed</span>')
    if total_changed:
        parts.append(f'<span style="color: #854d0e;">~{total_changed} changed</span>')
    if not parts:
        parts.append("No changes detected")
    html += " | ".join(parts)
    html += "</div>\n</div>\n</body>\n</html>"

    return html


def _strip_cache_meta(report):
    """Remove internal cache metadata for clean diff output."""
    stripped = []
    for proj in report:
        proj_out = dict(proj)
        proj_out["releases"] = [
            {k: v for k, v in r.items() if k != "cached_at"}
            for r in proj_out["releases"]
        ]
        stripped.append(proj_out)
    return stripped


def generate_unified_diff(report_a, report_b):
    """Generate a raw unified diff between two report dicts."""
    import difflib
    a_str = json.dumps(_strip_cache_meta(report_a), indent=2, ensure_ascii=False)
    b_str = json.dumps(_strip_cache_meta(report_b), indent=2, ensure_ascii=False)
    return "\n".join(
        difflib.unified_diff(
            a_str.splitlines(keepends=True),
            b_str.splitlines(keepends=True),
            fromfile="previous",
            tofile="current",
        )
    )


def generate_json(all_news):
    """Serialize all_news to JSON, stripping internal cache metadata."""
    output = []
    for project in all_news:
        proj_out = {
            "name": project["name"],
            "arch_dependent": project.get("arch_dependent", False),
            "releases": [],
        }
        for release in project["releases"]:
            rel_out = {
                "version": release["version"],
                "url": release["url"],
                "news": release.get("news", []),
            }
            proj_out["releases"].append(rel_out)
        output.append(proj_out)
    return json.dumps(output, indent=2, ensure_ascii=False)


def generate_rss(all_news):
    """Generate an RSS 2.0 feed with one item per project release."""
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Virt-News Aggregator"
    ET.SubElement(channel, "link").text = "https://github.com/"
    ET.SubElement(
        channel, "description"
    ).text = "Virtualization technology news — QEMU, Libvirt, KVM, and more"
    ET.SubElement(channel, "lastBuildDate").text = datetime.now().strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    strip_tags = re.compile(r"<[^>]+>")

    for project in all_news:
        for release in project["releases"]:
            if not release.get("news"):
                continue
            item = ET.SubElement(channel, "item")
            ET.SubElement(
                item, "title"
            ).text = f"{project['name']} {release['version']}"
            ET.SubElement(item, "link").text = release["url"]
            ET.SubElement(
                item, "guid", isPermaLink="false"
            ).text = f"{release['url']}#{release['version']}"
            desc_lines = []
            for news in release["news"]:
                cat = strip_tags.sub("", news.get("category", "")).strip()
                if cat:
                    desc_lines.append(cat)
            ET.SubElement(item, "description").text = "\n".join(desc_lines)

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="unicode"
    )


def parse_args():
    all_names = ", ".join(p["name"] for p in PROJECTS_CONFIG)
    parser = argparse.ArgumentParser(
        description="Virtualization news aggregator — QEMU, Libvirt, KVM, and more.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available projects: {all_names}",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass cache and re-fetch everything",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="output file path (default: virt_news_report.<ext>)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=RELEASE_LIMIT,
        metavar="N",
        help=f"number of releases to fetch per project (default: {RELEASE_LIMIT})",
    )
    parser.add_argument(
        "--projects",
        default=None,
        metavar="LIST",
        help="comma-separated list of projects to fetch (default: all)",
    )
    parser.add_argument(
        "--cache-file",
        default=CACHE_FILE,
        metavar="PATH",
        help=f"cache file path (default: {CACHE_FILE})",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=CACHE_TTL_DAYS,
        metavar="DAYS",
        help=f"cache TTL in days; 0 = never expire (default: {CACHE_TTL_DAYS})",
    )
    parser.add_argument(
        "--format",
        choices=["html", "json", "rss", "diff"],
        default=None,
        help="output format (default: html for normal mode, text for --diff mode)",
    )
    parser.add_argument(
        "--diff",
        nargs="*",
        metavar="PROJECT VERSION_A VERSION_B",
        help="show diff between current run and last cached run "
             "(no args) or between two specific versions "
             "(PROJECT VERSION_A VERSION_B)",
    )
    return parser.parse_args()


def _fetch_projects(selected, cache, limit):
    """Fetch all selected projects in parallel. Returns (all_news, all_news_dict)."""
    print(f"Fetching {len(selected)} project(s) in parallel (limit={limit})...")
    all_news_dict: dict = {}
    with ThreadPoolExecutor(max_workers=len(selected)) as executor:
        futures = {
            executor.submit(p["fetch"], cache, limit): p["name"] for p in selected
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                all_news_dict[name] = result
                n = sum(len(r["news"]) for r in result["releases"])
                print(
                    f"  [done] {name} — {len(result['releases'])} release(s), {n} item(s)"
                )
            except Exception as e:
                print(f"  [error] {name}: {e}")
    all_news = [
        all_news_dict[p["name"]] for p in selected if p["name"] in all_news_dict
    ]
    return all_news, all_news_dict


def main():
    args = parse_args()

    # ── Diff mode ─────────────────────────────────────────────────────────
    if args.diff is not None:
        # Default format for diff mode is text (terminal summary)
        if args.format is None:
            args.format = "text"
        # Load previous cache
        prev_cache = {} if args.no_cache else load_cache(args.cache_file, args.cache_ttl)
        prev_meta = prev_cache.get("_meta", {})
        prev_report = prev_meta.get("last_report", "")
        prev_run = datetime.fromisoformat(prev_meta.get("last_run", "")) if prev_meta.get("last_run") else None
        prev_news = prev_meta.get("last_news", [])

        if len(args.diff) == 0:
            # --diff without args: compare current vs last
            if not prev_report:
                print(_c("yellow", "No previous report found in cache. Run without --diff first."))
                raise SystemExit(0)
            print(f"Comparing current run vs last cached run ({prev_run.strftime('%Y-%m-%d %H:%M') if prev_run else 'N/A'})")
            print()

            cache = {} if args.no_cache else load_cache(args.cache_file, args.cache_ttl)
            all_news, _ = _fetch_projects(PROJECTS_CONFIG, cache, args.limit)
            diff_result = compute_diff(all_news, prev_news)
            now = datetime.now()

            if args.format == "diff":
                print(generate_unified_diff(prev_news, all_news))
            elif args.format == "html":
                output = generate_diff_html(diff_result, now, prev_run)
                out_file = args.output or "virt_news_diff.html"
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(output)
                print(f"Report written: {out_file}")
            else:
                print(generate_diff_summary(diff_result, now, prev_run))
            return

        elif len(args.diff) == 3:
            # --diff PROJECT VERSION_A VERSION_B
            project_name, version_a, version_b = args.diff
            project_name = project_name.strip()
            version_a = version_a.strip()
            version_b = version_b.strip()

            # Find matching project in config
            proj_config = None
            for pc in PROJECTS_CONFIG:
                if pc["name"].lower() == project_name.lower():
                    proj_config = pc
                    break
            if not proj_config:
                print(f"Unknown project: {project_name}")
                print(f"Available: {', '.join(p['name'] for p in PROJECTS_CONFIG)}")
                raise SystemExit(1)

            print(f"Comparing {project_name} {version_a} vs {version_b}")
            print()

            # Fetch or use cache
            cache = {} if args.no_cache else load_cache(args.cache_file, args.cache_ttl)
            result = proj_config["fetch"](cache, 100)
            releases = result["releases"]

            a_entry = None
            b_entry = None
            for r in releases:
                if r["version"].startswith(version_a):
                    a_entry = r
                if r["version"].startswith(version_b):
                    b_entry = r

            if a_entry is None or b_entry is None:
                available = [r["version"] for r in releases]
                print(f"Could not find requested version(s). Available: {', '.join(available[:10])}")
                raise SystemExit(1)

            diff_result = {"projects": [{
                "name": proj_config["name"],
                "added": [b_entry] if a_entry is None else [],
                "removed": [a_entry] if b_entry is None else [],
                "changed": [{"version": version_a, "old": a_entry, "new": b_entry}] if a_entry and b_entry else [],
            }]}
            now = datetime.now()

            if args.format == "diff":
                # Wrap single releases as a single-project report for clean diff
                report_a = [{"name": proj_config["name"], "releases": [a_entry]}]
                report_b = [{"name": proj_config["name"], "releases": [b_entry]}]
                print(generate_unified_diff(report_a, report_b))
            elif args.format == "html":
                output = generate_diff_html(diff_result, now, None)
                out_file = args.output or "virt_news_diff.html"
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(output)
                print(f"Report written: {out_file}")
            else:
                print(generate_diff_summary(diff_result, now, None))
            return

        else:
            print("Usage: virt_news.py --diff [PROJECT VERSION_A VERSION_B]")
            raise SystemExit(1)

    # ── Normal mode ───────────────────────────────────────────────────────
    # Apply defaults based on mode
    if args.format is None:
        args.format = "html"  # normal mode default

    if args.output is not None:
        output_file = args.output
    else:
        ext = {"html": ".html", "json": ".json", "rss": ".rss"}[args.format]
        output_file = f"virt_news_report{ext}"

    # Load cache (empty dict if --no-cache)
    cache = {} if args.no_cache else load_cache(args.cache_file, args.cache_ttl)

    # Filter project list if --projects was given
    if args.projects:
        requested = {p.strip().lower() for p in args.projects.split(",")}
        name_map = {p["name"].lower(): p for p in PROJECTS_CONFIG}
        unknown = requested - name_map.keys()
        if unknown:
            print(f"Unknown project(s): {', '.join(sorted(unknown))}")
            print(f"Available: {', '.join(p['name'] for p in PROJECTS_CONFIG)}")
            raise SystemExit(1)
        # Preserve configured order
        selected = [
            name_map[r]
            for r in (p["name"].lower() for p in PROJECTS_CONFIG)
            if r in requested
        ]
    else:
        selected = PROJECTS_CONFIG

    all_news, _ = _fetch_projects(selected, cache, args.limit)

    # Update cache: merge new data into existing cache so a partial fetch failure
    # does not discard previously-cached entries for other projects.
    new_cache = cache.copy()
    now_ts = datetime.now().timestamp()
    for project in all_news:
        name = project["name"]
        if name not in new_cache:
            new_cache[name] = {}
        for release in project["releases"]:
            entry = dict(release)
            entry["cached_at"] = now_ts
            new_cache[name][release["version"]] = entry

    # Store last report for diff mode
    if args.format == "html":
        last_report = generate_html(all_news)
    elif args.format == "json":
        last_report = generate_json(all_news)
    else:
        last_report = generate_rss(all_news)
    new_cache["_meta"] = {
        "last_report": last_report,
        "last_run": datetime.now().isoformat(),
        "last_news": all_news,
    }
    save_cache(new_cache, args.cache_file)

    # Generate and write output
    if args.format == "html":
        output = generate_html(all_news)
    elif args.format == "json":
        output = generate_json(all_news)
    else:
        output = generate_rss(all_news)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"Report written: {output_file} (format={args.format})")


if __name__ == "__main__":
    main()
