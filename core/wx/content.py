import html
import re
from typing import Optional

from bs4 import BeautifulSoup, NavigableString

PICTURE_LIST_MARKER = "window.picture_page_info_list"
PICTURE_DESCRIPTION_MARKER = "window.desc"


def parse_article_content(text: str) -> Optional[str]:
    """Extract standard and WeChat picture-article content from static HTML."""
    if not text:
        return None
    soup = BeautifulSoup(text, "html.parser")
    container = soup.find("div", {"id": "js_content"})
    if container is not None:
        container.attrs.pop("style", None)
        for image in container.find_all("img"):
            if image.get("data-src"):
                image["src"] = image.attrs.pop("data-src")
            if image.get("style"):
                image["style"] = re.sub(
                    r"width\s*:\s*\d+\s*px",
                    "width: 1080px",
                    image["style"],
                )
        return container.prettify()
    return _parse_picture_article(text)


def _parse_picture_article(text: str) -> Optional[str]:
    array_text = _extract_balanced_assignment(text, PICTURE_LIST_MARKER, "[", "]")
    if array_text is None:
        return None
    pictures = []
    seen_urls = set()
    for item in _split_top_level_objects(array_text):
        top_level = _mask_nested_objects(item)
        url = _extract_string_property(top_level, "cdn_url")
        if not url:
            continue
        url = _normalize_image_url(_decode_js_string(url))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        pictures.append(
            (
                url,
                _extract_integer_property(top_level, "width"),
                _extract_integer_property(top_level, "height"),
            )
        )
    if not pictures:
        return None

    soup = BeautifulSoup("", "html.parser")
    container = soup.new_tag("div", id="js_content")
    container["class"] = ["wx-picture-article"]
    description = _extract_string_assignment(text, PICTURE_DESCRIPTION_MARKER)
    if description:
        node = soup.new_tag("div")
        node["class"] = ["wx-picture-description"]
        lines = description.split("\n")
        for index, line in enumerate(lines):
            node.append(NavigableString(line))
            if index < len(lines) - 1:
                node.append(soup.new_tag("br"))
        container.append(node)
    for index, (url, width, height) in enumerate(pictures, start=1):
        image = soup.new_tag("img")
        image["src"] = url
        image["alt"] = f"文章图片 {index}"
        image["style"] = "display: block; max-width: 100%; height: auto; margin: 0 auto;"
        if width:
            image["width"] = str(width)
        if height:
            image["height"] = str(height)
        container.append(image)
    return container.prettify()


def _extract_balanced_assignment(text, marker, opening, closing):
    marker_index = text.find(marker)
    while marker_index >= 0:
        assignment = text.find("=", marker_index + len(marker))
        if assignment < 0:
            return None
        start = assignment + 1
        while start < len(text) and text[start].isspace():
            start += 1
        if start < len(text) and text[start] == opening:
            return _extract_balanced(text, start, opening, closing)
        marker_index = text.find(marker, marker_index + len(marker))
    return None


def _extract_balanced(text, start, opening, closing):
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _split_top_level_objects(array_text):
    objects = []
    start = None
    depth = 0
    quote = None
    escaped = False
    for index, char in enumerate(array_text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(array_text[start:index + 1])
                start = None
    return objects


def _mask_nested_objects(object_text):
    output = []
    depth = 0
    quote = None
    escaped = False
    for char in object_text:
        if quote:
            output.append(char if depth == 1 else " ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            output.append(char if depth == 1 else " ")
        elif char == "{":
            depth += 1
            output.append(char if depth == 1 else " ")
        elif char == "}":
            output.append(char if depth == 1 else " ")
            depth = max(0, depth - 1)
        else:
            output.append(char if depth == 1 else " ")
    return "".join(output)


def _extract_string_property(text, name):
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(['\"])(.*?)\1",
        text,
        re.DOTALL,
    )
    return match.group(2) if match else None


def _extract_integer_property(text, name):
    match = re.search(rf"\b{re.escape(name)}\s*:\s*['\"]?(\d+)", text)
    return int(match.group(1)) if match else None


def _extract_string_assignment(text, marker):
    index = text.find(marker)
    while index >= 0:
        assignment = text.find("=", index + len(marker))
        if assignment < 0:
            return None
        start = assignment + 1
        while start < len(text) and text[start].isspace():
            start += 1
        if start < len(text) and text[start] in {'"', "'"}:
            quote = text[start]
            escaped = False
            value = []
            for char in text[start + 1:]:
                if escaped:
                    value.extend(["\\", char])
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    return _decode_js_string("".join(value))
                else:
                    value.append(char)
            return _decode_js_string("".join(value))
        index = text.find(marker, index + len(marker))
    return None


def _decode_js_string(value):
    value = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), value)
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
    escapes = {"\\": "\\", "/": "/", "'": "'", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
    value = re.sub(r"\\([\\/'\"nrt])", lambda m: escapes[m.group(1)], value)
    return html.unescape(value)


def _normalize_image_url(url):
    url = url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("http://mmbiz.qpic.cn/"):
        url = f"https://{url[len('http://'):]}"
    return url if url.startswith(("http://", "https://")) else None
