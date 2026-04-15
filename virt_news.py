#!/usr/bin/env python3
import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import markdown
import requests
from bs4 import BeautifulSoup
from packaging.version import Version

# Configuration defaults
RELEASE_LIMIT = 10
CACHE_FILE = "virt_news_cache.json"
REQUEST_TIMEOUT = 15
CACHE_TTL_DAYS = 7


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
        for project, releases in raw.items():
            valid = {}
            for version, entry in releases.items():
                if entry.get("cached_at", 0) >= cutoff:
                    valid[version] = entry
                else:
                    expired += 1
            if valid:
                filtered[project] = valid
        if expired:
            print(f"Cache: dropped {expired} expired entr{'y' if expired == 1 else 'ies'} (TTL={ttl_days}d)")
        return filtered
    except Exception as e:
        print(f"Error loading cache: {e}")
        return {}


def save_cache(cache, cache_file=CACHE_FILE):
    try:
        with open(cache_file, "w") as f:
            json.dump(cache, f, indent=4)
    except Exception as e:
        print(f"Error saving cache: {e}")


ARCH_KEYWORDS = {
    "x86_64": ["x86", "x86_64", "amd64", "intel"],
    "aarch64": ["arm", "aarch64", "arm64"],
    "ppc64": ["powerpc", "ppc64", "ppc", "power"],
    "s390x": ["s390x", "s390", "s290"],  # Including s290 as it was in original README
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
        r = requests.get("https://wiki.qemu.org/ChangeLog/", timeout=REQUEST_TIMEOUT)
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
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
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
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
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
    url = f"https://api.github.com/repos/{repo_path}/releases"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
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
                    md_r = requests.get(raw_url, timeout=REQUEST_TIMEOUT)
                    if md_r.status_code == 200:
                        body = md_r.text
                except Exception:
                    pass

            html_body = markdown.markdown(body)
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
                        title = first_line[:100] + ("..." if len(first_line) > 100 else "")

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
        r = requests.get(f"{url_base}/LinuxVersions", timeout=REQUEST_TIMEOUT)
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
            r_lc = requests.get(f"{url_base}/LinuxChanges", timeout=REQUEST_TIMEOUT)
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
                r_rel = requests.get(rel_url, timeout=REQUEST_TIMEOUT)
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
        f'#tab-{pid}:checked ~ #content-{pid}' for pid in tab_ids
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
                f'View Source</a>'
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
                    f'                    <summary>\n'
                    f'                        <span class="news-title">{item["category"]}</span>\n'
                    f'                        <div class="tags">{arch_tags}{cc_tags}</div>\n'
                    f'                    </summary>\n'
                    f'                    <div class="news-content">\n'
                    f'                        {item["content"]}\n'
                    f'                    </div>\n'
                    f'                </details>\n'
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
    ET.SubElement(channel, "description").text = (
        "Virtualization technology news — QEMU, Libvirt, KVM, and more"
    )
    ET.SubElement(channel, "lastBuildDate").text = datetime.now().strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    strip_tags = re.compile(r"<[^>]+>")

    for project in all_news:
        for release in project["releases"]:
            if not release.get("news"):
                continue
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = (
                f"{project['name']} {release['version']}"
            )
            ET.SubElement(item, "link").text = release["url"]
            ET.SubElement(item, "guid", isPermaLink="false").text = (
                f"{release['url']}#{release['version']}"
            )
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
        choices=["html", "json", "rss"],
        default="html",
        help="output format (default: html)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Resolve output filename
    if args.output is not None:
        output_file = args.output
    else:
        ext = {"html": ".html", "json": ".json", "rss": ".rss"}[args.format]
        output_file = f"virt_news_report{ext}"

    # Load cache (empty dict if --no-cache)
    cache = {} if args.no_cache else load_cache(args.cache_file, args.cache_ttl)

    # Filter project list if --projects was given
    if args.projects:
        requested = {p.strip() for p in args.projects.split(",")}
        available = {p["name"] for p in PROJECTS_CONFIG}
        unknown = requested - available
        if unknown:
            print(f"Unknown project(s): {', '.join(sorted(unknown))}")
            print(f"Available: {', '.join(p['name'] for p in PROJECTS_CONFIG)}")
            raise SystemExit(1)
        selected = [p for p in PROJECTS_CONFIG if p["name"] in requested]
    else:
        selected = PROJECTS_CONFIG

    # Parallel fetch
    print(f"Fetching {len(selected)} project(s) in parallel (limit={args.limit})...")
    all_news_dict: dict = {}
    with ThreadPoolExecutor(max_workers=len(selected)) as executor:
        futures = {
            executor.submit(p["fetch"], cache, args.limit): p["name"]
            for p in selected
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                all_news_dict[name] = result
                n = sum(len(r["news"]) for r in result["releases"])
                print(f"  [done] {name} — {len(result['releases'])} release(s), {n} item(s)")
            except Exception as e:
                print(f"  [error] {name}: {e}")

    # Preserve configured order
    all_news = [all_news_dict[p["name"]] for p in selected if p["name"] in all_news_dict]

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
