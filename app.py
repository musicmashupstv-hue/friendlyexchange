from flask import Flask, request, jsonify, render_template
import requests
import re
import json
import csv
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from bs4 import BeautifulSoup

app = Flask(__name__)

# ---------- Helper ----------
def is_stream_url(url):
    patterns = [r'\.m3u8($|\?)', r'\.m3u($|\?)', r'\.mpd($|\?)', r'\.ts($|\?)',
                r'manifest\.mpd', r'master\.m3u8', r'playlist\.m3u8', r'\.mp4($|\?)',
                r'\.flv($|\?)', r'\.mkv($|\?)']
    return any(re.search(p, url, re.I) for p in patterns)

def fetch_url(url):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise ValueError(f"Failed to fetch URL: {str(e)}")

# ---------- Parser Functions ----------
def parse_m3u(content):
    channels = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            name_match = re.search(r'#EXTINF:[^,]*,?(.+)$', line)
            name = name_match.group(1).strip() if name_match else "Unknown"
            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith('#'):
                    channels.append({"name": name, "url": url})
        elif line.startswith('#EXT-X-STREAM-INF') or line.startswith('#EXT-X-MEDIA'):
            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith('#'):
                    channels.append({"name": f"Variant ({url.split('/')[-1]})", "url": url})
        i += 1
    return channels

def parse_json(content):
    try:
        data = json.loads(content)
        channels = []
        if isinstance(data, list):
            channels = [{"name": item.get("name") or item.get("title") or "Unnamed",
                         "url": item.get("url") or item.get("stream") or ""} for item in data]
        elif isinstance(data, dict):
            for key in ["channels", "items", "streams"]:
                if key in data and isinstance(data[key], list):
                    channels = [{"name": item.get("name") or item.get("title") or "Unnamed",
                                 "url": item.get("url") or item.get("stream") or ""} for item in data[key]]
                    break
            else:
                for k, v in data.items():
                    if isinstance(v, dict) and v.get("url"):
                        channels.append({"name": v.get("name") or k, "url": v["url"]})
        return [ch for ch in channels if ch["url"]]
    except Exception as e:
        raise ValueError(f"Invalid JSON: {str(e)}")

def parse_webpage(html, base_url=""):
    soup = BeautifulSoup(html, 'html.parser')
    channels = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        full_url = urljoin(base_url, href) if base_url else href
        if is_stream_url(full_url):
            name = a.get_text(strip=True) or a.get('title') or "Stream Link"
            channels.append({"name": name, "url": full_url})
    for iframe in soup.find_all('iframe', src=True):
        src = iframe['src']
        full_url = urljoin(base_url, src) if base_url else src
        if is_stream_url(full_url):
            name = iframe.get('title') or "iFrame Stream"
            channels.append({"name": name, "url": full_url})
    for video in soup.find_all(['video', 'source']):
        src = video.get('src')
        if src:
            full_url = urljoin(base_url, src) if base_url else src
            if is_stream_url(full_url):
                channels.append({"name": "Video Stream", "url": full_url})
    seen = set()
    unique = []
    for ch in channels:
        if ch["url"] not in seen:
            seen.add(ch["url"])
            unique.append(ch)
    return unique

def parse_direct_stream(url):
    if not is_stream_url(url):
        raise ValueError("URL does not look like a stream")
    return [{"name": "Direct Stream", "url": url}]

def parse_iframe(html):
    soup = BeautifulSoup(html, 'html.parser')
    iframes = soup.find_all('iframe', src=True)
    if not iframes:
        raise ValueError("No iframe found in the provided HTML")
    channels = []
    for idx, iframe in enumerate(iframes):
        name = iframe.get('title') or f"iFrame {idx+1}"
        channels.append({"name": name, "url": iframe['src']})
    return channels

def parse_xml(content):
    try:
        root = ET.fromstring(content)
        channels = []
        # XMLTV style
        for elem in root.findall('.//channel'):
            name_elem = elem.find('display-name')
            name = name_elem.text if name_elem is not None else elem.get('id', 'Unknown')
            url_elem = elem.find('url')
            url = url_elem.text if url_elem is not None else None
            if url:
                channels.append({"name": name, "url": url})
        if not channels:
            for elem in root.findall('.//*'):
                name = None
                url = None
                for child in elem:
                    if child.tag in ['name', 'title', 'channel'] and child.text:
                        name = child.text
                    if child.tag in ['url', 'link', 'src'] and child.text:
                        url = child.text
                if name and url:
                    channels.append({"name": name, "url": url})
        return channels
    except Exception as e:
        raise ValueError(f"XML parsing error: {str(e)}")

def parse_rss(content):
    try:
        root = ET.fromstring(content)
        channels = []
        for item in root.findall('.//item'):
            title_elem = item.find('title')
            title = title_elem.text if title_elem is not None else "RSS Item"
            enclosure = item.find('enclosure')
            if enclosure is not None and enclosure.get('url'):
                url = enclosure.get('url')
                channels.append({"name": title, "url": url})
            else:
                link = item.find('link')
                if link is not None and link.text and is_stream_url(link.text):
                    channels.append({"name": title, "url": link.text})
        return channels
    except Exception as e:
        raise ValueError(f"RSS parsing error: {str(e)}")

def parse_xspf(content):
    try:
        root = ET.fromstring(content)
        channels = []
        ns = {'xspf': 'http://xspf.org/ns/0/'}
        for track in root.findall('.//xspf:track', ns):
            location = track.find('xspf:location', ns)
            title = track.find('xspf:title', ns)
            if location is not None and location.text:
                name = title.text if title is not None else "XSPF Track"
                channels.append({"name": name, "url": location.text})
        return channels
    except Exception as e:
        raise ValueError(f"XSPF parsing error: {str(e)}")

def parse_csv(content):
    try:
        reader = csv.reader(content.splitlines())
        rows = list(reader)
        if not rows:
            return []
        header = rows[0]
        name_idx, url_idx = None, None
        for i, col in enumerate(header):
            col_lower = col.lower()
            if 'name' in col_lower or 'title' in col_lower or 'channel' in col_lower:
                name_idx = i
            if 'url' in col_lower or 'link' in col_lower or 'stream' in col_lower:
                url_idx = i
        if name_idx is None and url_idx is None and len(header) >= 2:
            name_idx, url_idx = 0, 1
        elif name_idx is None:
            name_idx = 0
        elif url_idx is None:
            url_idx = 1
        channels = []
        start_row = 1 if header else 0
        for i in range(start_row, len(rows)):
            row = rows[i]
            if len(row) > max(name_idx, url_idx):
                name = row[name_idx].strip() if name_idx is not None else f"CSV Entry {i}"
                url = row[url_idx].strip() if url_idx is not None else ""
                if url:
                    channels.append({"name": name, "url": url})
        return channels
    except Exception as e:
        raise ValueError(f"CSV parsing error: {str(e)}")

def parse_pls(content):
    channels = []
    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if line.startswith('File'):
            if '=' in line:
                url = line.split('=', 1)[1].strip()
                channels.append({"name": f"PLS Entry {len(channels)+1}", "url": url})
        elif line.startswith('Title'):
            if '=' in line:
                title = line.split('=', 1)[1].strip()
                if channels:
                    channels[-1]["name"] = title
    return channels

def parse_text(content):
    channels = []
    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ',' in line:
            parts = line.split(',', 1)
            name = parts[0].strip()
            url = parts[1].strip()
            if url and (is_stream_url(url) or url.startswith('http')):
                channels.append({"name": name, "url": url})
        else:
            if is_stream_url(line) or line.startswith('http'):
                channels.append({"name": "URL Entry", "url": line})
    return channels

# ---------- Flask Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/parse', methods=['POST'])
def parse():
    data = request.json
    parser_type = data.get('parser')
    input_data = data.get('input', '').strip()

    if parser_type != 'direct' and re.match(r'^https?://', input_data, re.I):
        try:
            input_data = fetch_url(input_data)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    try:
        if parser_type == 'm3u':
            channels = parse_m3u(input_data)
        elif parser_type == 'json':
            channels = parse_json(input_data)
        elif parser_type == 'webpage':
            channels = parse_webpage(input_data)
        elif parser_type == 'direct':
            channels = parse_direct_stream(input_data)
        elif parser_type == 'iframe':
            channels = parse_iframe(input_data)
        elif parser_type == 'xml':
            channels = parse_xml(input_data)
        elif parser_type == 'rss':
            channels = parse_rss(input_data)
        elif parser_type == 'xspf':
            channels = parse_xspf(input_data)
        elif parser_type == 'csv':
            channels = parse_csv(input_data)
        elif parser_type == 'pls':
            channels = parse_pls(input_data)
        elif parser_type == 'text':
            channels = parse_text(input_data)
        else:
            return jsonify({"error": "Unknown parser type"}), 400

        return jsonify({"channels": channels})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == '__main__':
    app.run(debug=True)
