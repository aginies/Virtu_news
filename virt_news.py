#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import json
import re
import sys
import markdown
from datetime import datetime

import os

# Configuration
RELEASE_LIMIT = 10
CACHE_FILE = "virt_news_cache.json"

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading cache: {e}")
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=4)
    except Exception as e:
        print(f"Error saving cache: {e}")

ARCH_KEYWORDS = {
    'x86_64': ['x86', 'x86_64', 'amd64', 'intel'],
    'aarch64': ['arm', 'aarch64', 'arm64'],
    'ppc64': ['powerpc', 'ppc64', 'ppc', 'power'],
    's390x': ['s390x', 's390', 's290']  # Including s290 as it was in original README
}

CC_KEYWORDS = ['tdx', 'sev', 'sev-snp', 'sgx', 'trustzone', 'pef', 'confidential computing', 'secure execution', 'cvm', 'cca', 'pvm', 'realm']

RELEVANT_CATEGORIES = ['new features', 'removed features', 'deprecated', 'improvements', 'new deprecated options and features']
QEMU_CATEGORIES = RELEVANT_CATEGORIES + ['kvm', 'migration', 'device emulation and assignment', 'memory backends', 'monitor']
IRRELEVANT_KEYWORDS = ['bug fix', 'bugfix', 'fixes', 'security']

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
        r = requests.get("https://wiki.qemu.org/ChangeLog/")
        soup = BeautifulSoup(r.content, 'html.parser')
        links = soup.find_all('a', href=re.compile(r'^/ChangeLog/\d+\.\d+$'))
        versions = []
        for link in links:
            ver_str = link['href'].split('/')[-1]
            try:
                versions.append(tuple(map(int, ver_str.split('.'))))
            except:
                continue
        if not versions:
            return []
        versions.sort(reverse=True)
        return [f"{v[0]}.{v[1]}" for v in versions[:limit]]
    except Exception as e:
        print(f"Error finding QEMU versions: {e}")
        return []

def get_qemu_news(cache_data=None):
    versions = get_latest_qemu_versions(RELEASE_LIMIT)
    all_qemu_releases = []
    
    project_cache = cache_data.get("QEMU", {}) if cache_data else {}
    
    for version in versions:
        if version in project_cache:
            all_qemu_releases.append(project_cache[version])
            continue
            
        url = f"https://wiki.qemu.org/ChangeLog/{version}"
        try:
            r = requests.get(url)
            soup = BeautifulSoup(r.content, 'html.parser')
            news_items = []
            
            processed_headers = set()
            for header in soup.find_all(['h2', 'h3', 'h4']):
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
                        if next_node.name and next_node.name.startswith('h'):
                            next_level = int(next_node.name[1])
                            if next_level <= h_level:
                                break
                            
                            # Sub-header: check if it has content before adding it
                            sub_text = next_node.get_text().strip()
                            sub_content = []
                            sub_has_real = False
                            
                            sub_next = next_node.find_next_sibling()
                            while sub_next:
                                if sub_next.name and sub_next.name.startswith('h'):
                                    break
                                if sub_next.name in ['ul', 'ol']:
                                    if sub_next.find('li'):
                                        sub_content.append(str(sub_next))
                                        sub_has_real = True
                                elif sub_next.name == 'p':
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

                        elif next_node.name in ['ul', 'ol']:
                            if next_node.find('li'):
                                content_blocks.append(str(next_node))
                                has_real_content = True
                        elif next_node.name == 'p':
                            if next_node.get_text().strip():
                                content_blocks.append(str(next_node))
                                has_real_content = True
                        
                        next_node = next_node.find_next_sibling()
                    
                    if has_real_content:
                        combined_text = header_text + "".join(content_blocks)
                        news_items.append({
                            "category": f"<b>{header_text}</b>",
                            "archs": get_archs_in_text(header_text),
                            "cc_keywords": get_cc_in_text(combined_text),
                            "content": "".join(content_blocks)
                        })
            
            all_qemu_releases.append({"version": version, "news": news_items, "url": url})
        except Exception as e:
            print(f"Error fetching QEMU news for {version}: {e}")
            
    return {"name": "QEMU", "releases": all_qemu_releases, "arch_dependent": True}

def get_libvirt_news(cache_data=None):
    url = "https://libvirt.org/news.html"
    try:
        r = requests.get(url)
        soup = BeautifulSoup(r.content, 'html.parser')
        
        all_libvirt_releases = []
        release_headers = [h1 for h1 in soup.find_all('h1') if '(' in h1.text and 'unreleased' not in h1.text.lower()]
        
        project_cache = cache_data.get("Libvirt", {}) if cache_data else {}
        
        for h1 in release_headers[:RELEASE_LIMIT]:
            version_text = h1.text.strip().replace('¶', '').strip()
            
            if version_text in project_cache:
                all_libvirt_releases.append(project_cache[version_text])
                continue

            # Try to find the section ID for anchoring
            section_id = ""
            parent_div = h1.find_parent('div', class_='section')
            if parent_div and parent_div.has_attr('id'):
                section_id = f"#{parent_div['id']}"
            elif h1.has_attr('id'):
                section_id = f"#{h1['id']}"
            
            release_ul = h1.find_next_sibling('ul')
            
            if not release_ul:
                continue

            news_items = []
            for li in release_ul.find_all('li', recursive=False):
                strong = li.find('strong')
                if strong:
                    cat_text = strong.text.strip()
                    if any(cat in cat_text.lower() for cat in RELEVANT_CATEGORIES) and 'bug fix' not in cat_text.lower():
                        sub_ul = li.find('ul')
                        if sub_ul:
                            for sub_li in sub_ul.find_all('li', recursive=False):
                                item_text = sub_li.get_text()
                                # Extract a title from the content
                                first_p = sub_li.find('p')
                                title_text = first_p.text.strip() if first_p else item_text.split('\n')[0].strip()
                                display_title = title_text[:100]
                                if len(title_text) > 100: display_title += "..."
                                display_cat = f"<b>{cat_text}</b>: {display_title}"
                                
                                news_items.append({
                                    "category": display_cat,
                                    "archs": get_archs_in_text(item_text),
                                    "cc_keywords": get_cc_in_text(item_text),
                                    "content": str(sub_li)
                                })
                        else:
                            content = "".join([str(c) for c in li.contents if c.name != 'strong'])
                            if content.strip():
                                news_items.append({
                                    "category": f"<b>{cat_text}</b>",
                                    "archs": get_archs_in_text(cat_text),
                                    "cc_keywords": get_cc_in_text(content),
                                    "content": content
                                })
            
            all_libvirt_releases.append({
                "version": version_text, 
                "news": news_items, 
                "url": f"{url}{section_id}"
            })
            
        return {"name": "Libvirt", "releases": all_libvirt_releases, "arch_dependent": True}
    except Exception as e:
        print(f"Error fetching Libvirt news: {e}")
        return {"name": "Libvirt", "releases": []}

def get_github_news(repo_path, project_name, arch_dependent=False, cache_data=None):
    url = f"https://api.github.com/repos/{repo_path}/releases"
    try:
        r = requests.get(url)
        data = r.json()
        if not data or not isinstance(data, list):
            return {"name": project_name, "releases": [], "arch_dependent": arch_dependent}
        
        project_cache = cache_data.get(project_name, {}) if cache_data else {}
        
        all_releases = []
        for release in data[:RELEASE_LIMIT]:
            version = release['tag_name']
            
            if version in project_cache:
                all_releases.append(project_cache[version])
                continue

            body = release.get('body', '')
            if not body: continue
            
            # Check for linked detailed markdown file (common in confidential-containers)
            # Example: [release notes](https://github.com/confidential-containers/confidential-containers/blob/main/releases/v0.18.0.md)
            md_match = re.search(r'https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/([^)\s]+\.md)', body)
            if md_match:
                raw_url = f"https://raw.githubusercontent.com/{md_match.group(1)}/{md_match.group(2)}/{md_match.group(3)}/{md_match.group(4)}"
                try:
                    md_r = requests.get(raw_url)
                    if md_r.status_code == 200:
                        body = md_r.text
                except:
                    pass

            html_body = markdown.markdown(body)
            soup = BeautifulSoup(html_body, 'html.parser')
            
            news_items = []
            current_cat = "Release Notes"
            for element in soup.children:
                if element.name in ['h1', 'h2', 'h3']:
                    current_cat = element.get_text()
                elif element.name in ['ul', 'ol', 'p']:
                    text = element.get_text().strip()
                    if text and 'bug' not in text.lower():
                        # Use first line as a title
                        title = text.split('\n')[0].strip()[:100]
                        if len(text.split('\n')[0].strip()) > 100: title += "..."
                        
                        news_items.append({
                            "category": f"<b>{current_cat}</b>: {title}",
                            "archs": get_archs_in_text(text) if arch_dependent else [],
                            "cc_keywords": get_cc_in_text(text),
                            "content": str(element)
                        })
            
            all_releases.append({
                "version": version, 
                "news": news_items, 
                "url": release.get('html_url', f"https://github.com/{repo_path}/releases/tag/{version}")
            })
            
        return {"name": project_name, "releases": all_releases, "arch_dependent": arch_dependent}
    except Exception as e:
        print(f"Error fetching {project_name} news: {e}")
        return {"name": project_name, "releases": [], "arch_dependent": arch_dependent}

def get_kernel_kvm_news(cache_data=None):
    url_base = "https://kernelnewbies.org"
    try:
        # LinuxVersions has a clear list of version links
        r = requests.get(f"{url_base}/LinuxVersions")
        soup = BeautifulSoup(r.content, 'html.parser')
        
        # Find latest release links
        release_links = []
        for a in soup.find_all('a', href=re.compile(r'^/Linux_\d+\.\d+$')):
            # The text should be like '6.13' or 'Linux 6.13'
            ver_text = a.text.strip()
            if ver_text:
                release_links.append((ver_text, a['href']))
        
        # Also check LinuxChanges which often points to the absolute latest (even if not in LinuxVersions yet)
        try:
            r_lc = requests.get(f"{url_base}/LinuxChanges")
            soup_lc = BeautifulSoup(r_lc.content, 'html.parser')
            # Look for the main title or first H1/H2 link
            latest_h1 = soup_lc.find('h1')
            if latest_h1:
                atag = latest_h1.find('a')
                if atag and atag.get('href', '').startswith('/Linux_'):
                    ver_text = atag.text.strip()
                    if ver_text:
                        release_links.insert(0, (ver_text, atag['href']))
        except:
            pass

        # Deduplicate and maintain order (latest first)
        seen = set()
        unique_links = []
        for name, path in release_links:
            clean_name = name.replace('Linux ', '').strip()
            if clean_name not in seen:
                unique_links.append((name, path))
                seen.add(clean_name)
        
        project_cache = cache_data.get("Kernel KVM", {}) if cache_data else {}
        
        # Take the most recent ones
        all_kernel_releases = []
        for name, path in unique_links[:RELEASE_LIMIT]:
            version = f"Linux {name}" if "Linux" not in name else name
            
            if version in project_cache:
                all_kernel_releases.append(project_cache[version])
                continue

            rel_url = f"{url_base}{path}"
            try:
                r_rel = requests.get(rel_url)
                soup_rel = BeautifulSoup(r_rel.content, 'html.parser')
                
                news_items = []
                # Look for a header that mentions Virtualization or KVM
                kvm_section = None
                for header in soup_rel.find_all(['h1', 'h2', 'h3']):
                    h_text = header.get_text().strip()
                    if 'Virtualization' in h_text or 'KVM' in h_text:
                        kvm_section = header
                        break
                
                if kvm_section:
                    # Content is usually in the next <ul> or <ol>
                    curr = kvm_section.find_next_sibling()
                    while curr and curr.name not in ['h1', 'h2', 'h3']:
                        if curr.name in ['ul', 'ol']:
                            for li in curr.find_all('li', recursive=False):
                                text = li.get_text().strip()
                                clean_text = text.replace('commit', '').strip()
                                # Clean up trailing commas and multiple spaces
                                clean_text = re.sub(r'[,\s]+$', '', clean_text)
                                title = clean_text.split('\n')[0].strip()
                                # Some titles have internal multiple commas at the end of the first line
                                title = re.sub(r'[,\s]+$', '', title)
                                if len(title) > 100: title = title[:100] + "..."
                                
                                news_items.append({
                                    "category": f"<b>{title}</b>" if title else "<b>Virtualization Update</b>",
                                    "archs": get_archs_in_text(clean_text),
                                    "cc_keywords": get_cc_in_text(clean_text),
                                    "content": str(li)
                                })
                        curr = curr.find_next_sibling()
                
                all_kernel_releases.append({
                    "version": version,
                    "news": news_items,
                    "url": rel_url
                })
            except Exception as e:
                print(f"Error fetching detail for {name}: {e}")
            
        return {"name": "Kernel KVM", "releases": all_kernel_releases, "arch_dependent": True}
    except Exception as e:
        print(f"Error fetching Kernel KVM news: {e}")
        return {"name": "Kernel KVM", "releases": []}

def generate_html(all_news):
    html_template = """
<!DOCTYPE html>
<html>
<head>
    <title>Virt-News Aggregator</title>
    <style>
        body { font-family: sans-serif; line-height: 1.6; max-width: 1100px; margin: 0 auto; padding: 20px; background-color: #f4f4f9; }
        h1 { color: #333; text-align: center; margin-bottom: 30px; }
        
        /* Tab System */
        .tabs { display: flex; flex-wrap: wrap; margin-top: 20px; }
        .tabs input[type="radio"] { display: none; }
        .tabs label { 
            order: 1; display: block; padding: 12px 25px; margin-right: 4px; 
            cursor: pointer; background: #ddd; font-weight: bold; 
            border-radius: 8px 8px 0 0; transition: background 0.2s;
            border: 1px solid #ccc; border-bottom: none;
        }
        .tabs .tab-content { 
            order: 99; flex-grow: 1; width: 100%; display: none; 
            padding: 20px; background: #fff; border: 1px solid #ccc; 
            border-radius: 0 8px 8px 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        }
        .tabs input[type="radio"]:checked + label { background: #0056b3; color: white; border-color: #0056b3; }
        .tabs input[type="radio"]:checked + label + .tab-content { display: block; }
        
        h2 { color: #0056b3; border-bottom: 2px solid #eef; padding-bottom: 10px; margin-top: 0; }
        h3 { border-left: 5px solid #0056b3; padding-left: 10px; margin-top: 30px; color: #444; background: #f9f9f9; padding: 10px; }
        
        details { margin-bottom: 12px; background: white; padding: 12px; border-radius: 6px; border: 1px solid #eee; transition: box-shadow 0.2s; }
        details:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
        summary { font-weight: bold; cursor: pointer; font-size: 1.1em; outline: none; }
        
        .arch-tag { display: inline-block; background: #ddd; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; margin-left: 8px; vertical-align: middle; }
        .arch-x86_64 { background: #ffd1d1; border: 1px solid #ffb1b1; }
        .arch-aarch64 { background: #d1ffd1; border: 1px solid #b1ffb1; }
        .arch-ppc64 { background: #d1d1ff; border: 1px solid #b1b1ff; }
        .arch-s390x { background: #ffd1ff; border: 1px solid #ffb1ff; }
        .cc-tag { display: inline-block; background: #e0f7fa; color: #006064; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; margin-left: 8px; vertical-align: middle; border: 1px solid #b2ebf2; font-weight: bold; }
        
        .news-content { margin-top: 15px; padding-left: 10px; border-left: 2px solid #f0f0f0; }
        .metadata { font-size: 0.9em; color: #666; text-align: center; margin-bottom: 10px; }
        .release-link { font-size: 0.85em; font-weight: normal; margin-left: 15px; color: #0056b3; text-decoration: none; }
        .release-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>Virt-News Aggregator</h1>
    <div class="metadata">Generated on: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """</div>
    
    <div class="tabs">
    <!-- Tabs will be inserted here -->
    </div>
</body>
</html>
"""
    tab_html = []
    for i, project in enumerate(all_news):
        p_id = project['name'].replace(' ', '-')
        checked = 'checked="checked"' if i == 0 else ""
        
        p_html = f'<input type="radio" name="tabs" id="tab-{p_id}" {checked}>'
        p_html += f'<label for="tab-{p_id}">{project["name"]}</label>'
        p_html += f'<div class="tab-content">'
        p_html += f"<h2>{project['name']} Releases</h2>"
        
        for release in project['releases']:
            p_html += f"<h3>Release: {release['version']} <a class='release-link' href='{release['url']}' target='_blank'>[Source]</a></h3>"
            
            if not release['news']:
                 p_html += "<p>No major architectural features or deprecations found in this release.</p>"
            
            for item in release['news']:
                if project.get('arch_dependent'):
                    arch_tags = "".join([f'<span class="arch-tag arch-{arch}">{arch}</span>' for arch in item.get('archs', [])])
                else:
                    arch_tags = ""
                
                cc_tags = ""
                if item.get('cc_keywords'):
                    # Deduplicate and show unique CC keywords found
                    unique_cc = sorted(list(set(item['cc_keywords'])))
                    cc_tags = "".join([f'<span class="cc-tag">CC: {kw.upper()}</span>' for kw in unique_cc])

                p_html += f"""
                <details>
                    <summary>
                        {item['category']} {arch_tags} {cc_tags}
                    </summary>
                    <div class="news-content">
                        {item['content']}
                    </div>
                </details>
                """
        p_html += "</div>"
        tab_html.append(p_html)
    
    parts = html_template.split('<div class="tabs">')
    header = parts[0]
    footer = parts[1].split('</div>')[1]
    
    full_html = header + '<div class="tabs">' + "".join(tab_html) + "</div>" + footer
    return full_html

def main():
    cache = load_cache()
    
    print("Fetching news from QEMU...")
    qemu = get_qemu_news(cache)
    
    print("Fetching news from Libvirt...")
    libvirt = get_libvirt_news(cache)
    
    print("Fetching news from Virt-Manager...")
    virtman = get_github_news("virt-manager/virt-manager", "Virt-Manager", False, cache)
    
    print("Fetching news from Linux Kernel KVM...")
    kernel = get_kernel_kvm_news(cache)
    
    print("Fetching news from EDK2 (Firmware)...")
    edk2 = get_github_news("tianocore/edk2", "EDK2 / OVMF", False, cache)
    
    print("Fetching news from Cockpit Machines...")
    cockpit = get_github_news("cockpit-project/cockpit-machines", "Cockpit Machines", False, cache)
    
    print("Fetching news from Confidential Containers...")
    coco = get_github_news("confidential-containers/confidential-containers", "Confidential Containers", False, cache)
    
    all_news = [qemu, libvirt, virtman, kernel, edk2, cockpit, coco]
    
    # Update cache
    new_cache = {}
    for project in all_news:
        new_cache[project['name']] = {release['version']: release for release in project['releases']}
    save_cache(new_cache)
    
    html = generate_html(all_news)
    
    with open("virt_news_report.html", "w") as f:
        f.write(html)
    
    print("Report generated: virt_news_report.html")

if __name__ == "__main__":
    main()
