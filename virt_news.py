#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import json
import re
import sys
import markdown
from datetime import datetime

# Configuration
ARCH_KEYWORDS = {
    'x86_64': ['x86', 'x86_64', 'amd64', 'intel'],
    'aarch64': ['arm', 'aarch64', 'arm64'],
    'ppc64': ['powerpc', 'ppc64', 'ppc', 'power'],
    's390x': ['s390x', 's390', 's290']  # Including s290 as it was in original README
}

RELEVANT_CATEGORIES = ['new features', 'removed features', 'deprecated', 'improvements', 'new deprecated options and features']
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

def get_latest_qemu_versions(limit=5):
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

def get_qemu_news():
    versions = get_latest_qemu_versions(5)
    all_qemu_releases = []
    
    for version in versions:
        url = f"https://wiki.qemu.org/ChangeLog/{version}"
        try:
            r = requests.get(url)
            soup = BeautifulSoup(r.content, 'html.parser')
            news_items = []
            
            for header in soup.find_all(['h2', 'h3', 'h4']):
                header_text = header.get_text().strip()
                is_arch = is_relevant_arch(header_text)
                is_cat = any(cat in header_text.lower() for cat in RELEVANT_CATEGORIES)
                
                if is_arch or is_cat:
                    content = []
                    next_node = header.find_next_sibling()
                    while next_node and next_node.name not in ['h2', 'h3', 'h4']:
                        if next_node.name in ['ul', 'ol', 'p']:
                            content.append(str(next_node))
                        next_node = next_node.find_next_sibling()
                    
                    if content:
                        news_items.append({
                            "category": f"<b>{header_text}</b>",
                            "archs": get_archs_in_text(header_text),
                            "content": "".join(content)
                        })
            
            all_qemu_releases.append({"version": version, "news": news_items, "url": url})
        except Exception as e:
            print(f"Error fetching QEMU news for {version}: {e}")
            
    return {"name": "QEMU", "releases": all_qemu_releases}

def get_libvirt_news():
    url = "https://libvirt.org/news.html"
    try:
        r = requests.get(url)
        soup = BeautifulSoup(r.content, 'html.parser')
        
        all_libvirt_releases = []
        release_headers = [h1 for h1 in soup.find_all('h1') if '(' in h1.text and 'unreleased' not in h1.text.lower()]
        
        for h1 in release_headers[:5]:
            version_text = h1.text.strip().replace('¶', '').strip()
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
                                    "content": str(sub_li)
                                })
                        else:
                            content = "".join([str(c) for c in li.contents if c.name != 'strong'])
                            if content.strip():
                                news_items.append({
                                    "category": f"<b>{cat_text}</b>",
                                    "archs": get_archs_in_text(cat_text),
                                    "content": content
                                })
            
            all_libvirt_releases.append({
                "version": version_text, 
                "news": news_items, 
                "url": f"{url}{section_id}"
            })
            
        return {"name": "Libvirt", "releases": all_libvirt_releases}
    except Exception as e:
        print(f"Error fetching Libvirt news: {e}")
        return {"name": "Libvirt", "releases": []}

def get_virt_manager_news():
    url = "https://api.github.com/repos/virt-manager/virt-manager/releases"
    try:
        r = requests.get(url)
        data = r.json()
        if not data:
            return {"name": "Virt-Manager", "releases": []}
        
        all_virtman_releases = []
        for release in data[:5]:
            version = release['tag_name']
            body = release['body']
            html_body = markdown.markdown(body)
            soup = BeautifulSoup(html_body, 'html.parser')
            
            news_items = []
            current_cat = "Release Notes"
            for element in soup.children:
                if element.name in ['h1', 'h2', 'h3']:
                    current_cat = element.get_text()
                elif element.name in ['ul', 'ol', 'p']:
                    text = element.get_text()
                    if 'bug' not in text.lower():
                        # Use first line as a title
                        title = text.split('\n')[0].strip()[:100]
                        if len(text.split('\n')[0].strip()) > 100: title += "..."
                        news_items.append({
                            "category": f"<b>{current_cat}</b>: {title}",
                            "archs": [], # Not arch dependent for virt-manager
                            "content": str(element)
                        })
            
            all_virtman_releases.append({
                "version": version, 
                "news": news_items, 
                "url": f"https://github.com/virt-manager/virt-manager/releases/tag/{version}"
            })
            
        return {"name": "Virt-Manager", "releases": all_virtman_releases}
    except Exception as e:
        print(f"Error fetching Virt-Manager news: {e}")
        return {"name": "Virt-Manager", "releases": []}

def get_kernel_kvm_news():
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
        
        # Take the most recent 5
        all_kernel_releases = []
        for name, path in release_links[:5]:
            version = f"Linux {name}" if "Linux" not in name else name
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
                                title = clean_text.split('\n')[0][:100]
                                if len(clean_text.split('\n')[0]) > 100: title += "..."
                                
                                news_items.append({
                                    "category": f"<b>Virtualization</b>: {title}",
                                    "archs": get_archs_in_text(clean_text),
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
            
        return {"name": "Kernel KVM", "releases": all_kernel_releases}
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
                if project['name'] == 'Virt-Manager':
                    arch_tags = ""
                else:
                    arch_tags = "".join([f'<span class="arch-tag arch-{arch}">{arch}</span>' for arch in item['archs']])
                
                p_html += f"""
                <details>
                    <summary>
                        {item['category']} {arch_tags}
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
    print("Fetching news from QEMU...")
    qemu = get_qemu_news()
    
    print("Fetching news from Libvirt...")
    libvirt = get_libvirt_news()
    
    print("Fetching news from Virt-Manager...")
    virtman = get_virt_manager_news()
    
    print("Fetching news from Linux Kernel KVM...")
    kernel = get_kernel_kvm_news()
    
    all_news = [qemu, libvirt, virtman, kernel]
    
    html = generate_html(all_news)
    
    with open("virt_news_report.html", "w") as f:
        f.write(html)
    
    print("Report generated: virt_news_report.html")

if __name__ == "__main__":
    main()
