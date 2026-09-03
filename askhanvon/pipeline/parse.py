"""内容解析：Markdown / TXT / EPUB / PDF → 结构化章节（卷/章/节 + 段落）。

统一中间格式 ParsedBook，后续 chunk/embedding/索引都以此为输入。
XML 解析安全：解析前拒绝包含 DTD/ENTITY 的内容并限制大小（防实体扩展攻击）。
"""
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
    "九": 9, "十": 10, "百": 100, "千": 1000, "两": 2,
}

_XML_MAX_BYTES = 10 * 1024 * 1024


def _safe_xml(text: str) -> str:
    """解析不可信 XML 前的安全闸：限大小、拒绝 DTD/ENTITY。"""
    if len(text) > _XML_MAX_BYTES:
        raise ValueError("XML 内容超过大小限制")
    probe = text[:65536].lower()
    if "<!doctype" in probe or "<!entity" in probe:
        raise ValueError("拒绝解析包含 DTD/ENTITY 声明的 XML")
    return text


def cn2int(s: str) -> int:
    """中文数字/阿拉伯数字 → int；失败返回 0。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        v = _CN_NUM.get(ch)
        if v is None:
            return 0
        if v in (10, 100, 1000):
            cur = (cur or 1) * v
            total += cur
            cur = 0
        else:
            cur = max(cur, 0) + v if cur < v else cur + v
    return total + cur


_CHAPTER_RE = re.compile(
    r"^\s*(?:第\s*([0-9零一二三四五六七八九十百千两]+)\s*[回章节讲篇])\s*[:：．.]?\s*(.*)$"
)
_VOL_RE = re.compile(
    r"^\s*(?:第\s*([0-9零一二三四五六七八九十百千两]+)\s*[卷部])\s*[:：．.]?\s*(.*)$"
)


@dataclass
class ParsedChapter:
    vol: str = ""
    no: int = 0
    title: str = ""
    paragraphs: list = field(default_factory=list)


@dataclass
class ParsedBook:
    title: str = ""
    author: str = ""
    category: str = ""
    tags: list = field(default_factory=list)
    description: str = ""
    chapters: list = field(default_factory=list)


def _clean(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", (text or "")).strip()


def parse_heading_text(line: str):
    """尝试把一行解析为 (kind, no, title)；非章节行返回 None。"""
    m = _VOL_RE.match(line)
    if m:
        return ("vol", cn2int(m.group(1)), _clean(m.group(2)) or "卷")
    m = _CHAPTER_RE.match(line)
    if m:
        return ("chapter", cn2int(m.group(1)), _clean(m.group(2)))
    return None


def parse_markdown(text: str) -> ParsedBook:
    """项目样书格式：h1=元数据(竖线分隔)，h2=卷，h3=章，空行分段。"""
    book = ParsedBook()
    cur_ch: ParsedChapter | None = None
    cur_vol = ""
    buf: list = []

    def flush():
        if cur_ch is not None and buf:
            joined = "\n".join(_clean(x) for x in buf if _clean(x))
            cur_ch.paragraphs.extend(p for p in joined.split("\n") if p)
        buf.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# "):
            parts = [p.strip() for p in line[2:].split("|")]
            book.title = parts[0] if parts else "未命名"
            book.author = parts[1] if len(parts) > 1 else ""
            book.category = parts[2] if len(parts) > 2 else "未分类"
            book.tags = [t for t in (parts[3].split(",") if len(parts) > 3 else []) if t]
            book.description = parts[4] if len(parts) > 4 else ""
            continue
        if line.startswith("## "):
            parsed = parse_heading_text(line[3:])
            if parsed and parsed[0] == "chapter":
                flush()
                cur_ch = ParsedChapter(vol=cur_vol, no=parsed[1], title=parsed[2])
                book.chapters.append(cur_ch)
            else:
                cur_vol = line[3:].strip()
            continue
        if line.startswith("### "):
            parsed = parse_heading_text(line[4:])
            flush()
            if parsed and parsed[0] == "chapter":
                cur_ch = ParsedChapter(vol=cur_vol, no=parsed[1], title=parsed[2])
            else:
                cur_ch = ParsedChapter(vol=cur_vol, no=len(book.chapters) + 1,
                                       title=line[4:].strip())
            book.chapters.append(cur_ch)
            continue
        if line.startswith("#"):
            continue
        if not line.strip():
            flush()
            continue
        if cur_ch is not None:
            buf.append(line.strip())
    flush()
    if not book.chapters:  # 无章节结构：整本一章
        body = [p for p in (_clean(x) for x in text.splitlines())
                if p and not p.startswith("#")]
        if body:
            book.chapters.append(ParsedChapter(no=1, title="全文", paragraphs=body))
    return book


def parse_plain_text(text: str) -> ParsedBook:
    """TXT：按「第X回/章/讲/篇」正则切章；空行分段；无匹配则按每 12 段切章。

    修复：首章标题出现前的正文（序/楔子/书名页）不再被丢弃，保留为「开篇」章节；
    无章节时兜底分章用收集到的正文行（且不含被消费的元信息行）。
    """
    book = ParsedBook()
    lines = text.splitlines()
    meta_found = False
    cur: ParsedChapter | None = None
    pending: list = []      # 首章标题前的正文，避免开头内容丢失
    body_lines: list = []   # 全部正文行，供无章节兜底分章使用
    for raw in lines:
        line = raw.strip()
        if not meta_found and line and not cur:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                book.title = parts[0]
                book.author = parts[1]
                book.category = parts[2]
                book.tags = [t for t in (parts[3].split(",") if len(parts) > 3 else []) if t]
                book.description = parts[4] if len(parts) > 4 else ""
                meta_found = True
                continue
        parsed = parse_heading_text(line)
        if parsed and parsed[0] == "vol":
            continue
        if parsed and parsed[0] == "chapter":
            if not book.chapters and pending:
                book.chapters.append(ParsedChapter(no=0, title="开篇", paragraphs=pending))
            pending = []
            cur = ParsedChapter(no=parsed[1], title=parsed[2] or ("第" + str(parsed[1]) + "章"))
            book.chapters.append(cur)
            continue
        if cur is not None and line:
            cur.paragraphs.append(line)
            body_lines.append(line)
        elif line:
            pending.append(line)
            body_lines.append(line)
    if not book.chapters:
        body = body_lines or [l.strip() for l in lines if l.strip()]
        for i in range(0, len(body), 12):
            book.chapters.append(
                ParsedChapter(no=i // 12 + 1, title="节选" + str(i // 12 + 1),
                              paragraphs=body[i : i + 12])
            )
    if not book.title:
        book.title = "未命名"
    return book


def _html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)[\s\S]*?</\1>", "", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>|</div>|</h[1-6]>|</li>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", html)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )
    return text


def _epub_meta(root) -> dict:
    """从 OPF 根节点读取 dc: 元数据（命名空间无关，取首个非空值）。"""
    out = {"title": "", "creator": "", "subject": "", "description": ""}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        val = (el.text or "").strip()
        if tag in out and val and not out[tag]:
            out[tag] = val
    return out


def _epub_category(title: str) -> str:
    """技术书无 dc:subject 时，按书名关键词归类（内容侧分类，供推荐冷启动）。"""
    rules = [
        (("算法", "数据结构", "趣学", "趣题", "计算", "程序设计", "编码"), "计算机基础"),
        (("操作系统", "内核", "虚拟化", "真象还原"), "操作系统"),
        (("网络", "分布式", "微服务", "缓存", "Zookeeper", "一致性", "SDN", "DPDK", "Paxos"),
         "分布式与网络"),
        (("安全", "加密", "赛博", "CPK"), "信息安全"),
        (("机器学习", "自然语言", "NLP", "TensorFlow", "数据挖掘", "文本", "软计算"), "人工智能与数据"),
        (("架构", "系统架构", "设计", "面向对象"), "软件架构"),
        (("硬件", "电路", "芯片", "SoC", "嵌入式", "龙芯"), "硬件与嵌入式"),
        (("图形", "图像", "OpenLayers", "WebGIS", "图形学", "数字图像"), "图形与多媒体"),
        (("Surface", "玩全", "SharePoint", "Office"), "办公与工具"),
    ]
    for kws, cat in rules:
        for kw in kws:
            if kw in title:
                return cat
    return "技术图书"


def parse_epub(path_or_file) -> ParsedBook:
    """EPUB：按 spine 顺序抽取文本后走 TXT 章节识别，并用 OPF 元数据覆盖书名/作者/分类。"""
    with zipfile.ZipFile(path_or_file) as zf:
        container = _safe_xml(zf.read("META-INF/container.xml").decode("utf-8", "ignore"))
        opf_path = ""
        root = ElementTree.fromstring(container)
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        rootfile = root.find(".//c:rootfile", ns)
        if rootfile is not None:
            opf_path = rootfile.get("full-path", "")
        texts = []
        meta = {}
        if opf_path:
            opf = _safe_xml(zf.read(opf_path).decode("utf-8", "ignore"))
            base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            oroot = ElementTree.fromstring(opf)
            meta = _epub_meta(oroot)
            items = {}
            for item in oroot.iter():
                tag = item.tag.rsplit("}", 1)[-1]
                if tag == "item" and item.get("media-type") == "application/xhtml+xml":
                    items[item.get("id")] = base + item.get("href", "")
            for item in oroot.iter():
                tag = item.tag.rsplit("}", 1)[-1]
                if tag == "itemref":
                    idref = item.get("idref")
                    if idref in items:
                        try:
                            texts.append(
                                _html_to_text(zf.read(items[idref]).decode("utf-8", "ignore"))
                            )
                        except KeyError:
                            continue
        if not texts:  # 兜底：全部 xhtml
            for name in zf.namelist():
                if name.endswith((".xhtml", ".html", ".htm")):
                    texts.append(
                        _html_to_text(zf.read(name).decode("utf-8", "ignore"))
                    )
    book = parse_plain_text("\n".join(texts))
    # 覆盖 EPub 元数据（parse_plain_text 无法读到 OPF 里的标识信息）
    if meta.get("title"):
        book.title = meta["title"]
    if meta.get("creator"):
        book.author = meta["creator"]
    if meta.get("subject"):
        book.category = meta["subject"]
    elif not book.category and meta.get("title"):
        # 技术书常缺 dc:subject → 按标题归类，避免推荐冷启动无分类可依
        book.category = _epub_category(meta["title"])
    if meta.get("description") and not book.description:
        book.description = meta["description"]
    return book


def parse_pdf(path_or_file) -> ParsedBook:
    """PDF：逐页抽文本（依赖 pypdf，可选依赖），按标题正则切章。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("解析 PDF 需要安装 pypdf（pip install pypdf）")
    reader = PdfReader(path_or_file)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return parse_plain_text("\n".join(pages))


def parse_file(path: str) -> ParsedBook:
    lower = path.lower()
    if lower.endswith((".md", ".markdown")):
        with open(path, "r", encoding="utf-8") as f:
            return parse_markdown(f.read())
    if lower.endswith(".epub"):
        return parse_epub(path)
    if lower.endswith(".pdf"):
        return parse_pdf(path)
    # TXT：自动尝试 utf-8 / gbk / utf-16
    data = open(path, "rb").read()
    text = ""
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            text = data.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            text = ""
    if not text:
        raise ValueError("无法识别文件编码: " + path)
    return parse_plain_text(text)


def decode_text_bytes(data: bytes) -> str:
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    raise ValueError("无法识别文本编码")


def parse_stream(ext: str, data: bytes) -> ParsedBook:
    """内存解析入口（上传不落盘）。ext 为服务端判定的固定类别。"""
    if ext == "md":
        return parse_markdown(data.decode("utf-8"))
    if ext == "txt":
        return parse_plain_text(decode_text_bytes(data))
    if ext == "epub":
        import io

        return parse_epub(io.BytesIO(data))
    if ext == "pdf":
        import io

        return parse_pdf(io.BytesIO(data))
    raise ValueError("不支持的格式类别: " + str(ext))
