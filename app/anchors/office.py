"""Render text and simple tables from Office Open XML files to safe HTML."""

from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _text(node: ET.Element, tag: str) -> str:
    return "".join(element.text or "" for element in node.iter(tag))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>{html.escape(title)}</title>
<style>
body{{font:16px/1.55 system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#222}}
nav{{position:sticky;top:0;background:#fff;padding:.7rem;border-bottom:1px solid #ddd}}
section{{scroll-margin-top:5rem;margin:1rem 0;padding:.5rem}}
.target{{background:#fff3a6;outline:2px solid #e4b900}}
table{{border-collapse:collapse;max-width:100%;overflow:auto}}td,th{{border:1px solid #bbb;padding:.3rem .5rem;white-space:pre-wrap}}
article{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}}
</style><body>{body}
<script>
const raw=decodeURIComponent(location.hash.slice(1));
const params=new URLSearchParams(raw);
let target=null;
if(params.has('paragraph')) target=document.getElementById('paragraph-'+params.get('paragraph'));
if(params.has('slide')) target=document.getElementById('slide-'+params.get('slide'));
if(params.has('sheet')){{
 const sheet=[...document.querySelectorAll('[data-sheet-name]')].find(x=>x.dataset.sheetName===params.get('sheet'));
 if(sheet) target=sheet;
 const range=params.get('range');
 if(sheet && range){{
  const parse=(ref)=>{{const m=ref.match(/[$]?([A-Z]+)[$]?(\\d+)/i);if(!m)return null;let c=0;for(const x of m[1].toUpperCase())c=c*26+x.charCodeAt(0)-64;return [Number(m[2]),c]}};
  const refs=range.split(':').map(parse);
  if(refs[0]){{const lo=refs[1]||refs[0];for(const cell of sheet.querySelectorAll('[data-row][data-col]')){{const r=+cell.dataset.row,c=+cell.dataset.col;if(r>=refs[0][0]&&r<=lo[0]&&c>=refs[0][1]&&c<=lo[1])cell.classList.add('target')}}}}
 }}
}}
if(target){{target.classList.add('target');target.scrollIntoView()}}
</script></body></html>"""


def render_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{W}body")
    blocks: list[str] = []
    paragraph_no = 0
    if body is not None:
        for element in body:
            if element.tag == f"{W}p":
                text = _text(element, f"{W}t").strip()
                if not text:
                    continue
                paragraph_no += 1
                style = element.find(f"{W}pPr/{W}pStyle")
                style_name = style.get(f"{W}val", "") if style is not None else ""
                heading = re.fullmatch(r"Heading([1-6])", style_name)
                escaped = html.escape(text)
                content = f"<h{heading[1]}>{escaped}</h{heading[1]}>" if heading else f"<p>{escaped}</p>"
                blocks.append(f'<section id="paragraph-{paragraph_no}">{content}</section>')
            elif element.tag == f"{W}tbl":
                rows = []
                for row in element.findall(f"{W}tr"):
                    cells = "".join(f"<td>{html.escape(_text(cell, f'{W}t'))}</td>" for cell in row.findall(f"{W}tc"))
                    rows.append(f"<tr>{cells}</tr>")
                blocks.append(f"<table>{''.join(rows)}</table>")
    return _page(path.name, f"<h1>{html.escape(path.name)}</h1>{''.join(blocks)}")


def render_xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target", "") for rel in relations.findall(f"{REL}Relationship")}
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [_text(item, f"{S}t") for item in strings.findall(f"{S}si")]
        sections: list[str] = []
        links: list[str] = []
        for index, sheet in enumerate(workbook.findall(f"{S}sheets/{S}sheet"), start=1):
            name = sheet.get("name", f"Лист {index}")
            target = targets.get(sheet.get(f"{R}id", ""), "")
            sheet_path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            sheet_path = str(Path("xl") / Path(sheet_path).name) if ".." in Path(sheet_path).parts else sheet_path
            xml = ET.fromstring(archive.read(sheet_path))
            rows_html: list[str] = []
            for row in xml.findall(f".//{S}sheetData/{S}row"):
                cells_html = []
                for cell in row.findall(f"{S}c"):
                    ref = cell.get("r", "")
                    match = re.match(r"([A-Z]+)(\d+)", ref)
                    if not match:
                        continue
                    col = 0
                    for char in match[1]:
                        col = col * 26 + ord(char) - 64
                    value = _text(cell, f"{S}v")
                    inline = cell.find(f"{S}is")
                    if inline is not None:
                        value = _text(inline, f"{S}t")
                    elif cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    cells_html.append(
                        f'<td data-row="{match[2]}" data-col="{col}" data-cell="{html.escape(ref)}">{html.escape(value)}</td>'
                    )
                rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
            section_id = f"sheet-{index}"
            links.append(f'<a href="#sheet={html.escape(name, quote=True)}">{html.escape(name)}</a>')
            sections.append(
                f'<section id="{section_id}" data-sheet-name="{html.escape(name, quote=True)}">'
                f"<h2>{html.escape(name)}</h2><table>{''.join(rows_html)}</table></section>"
            )
    return _page(path.name, f"<h1>{html.escape(path.name)}</h1><nav>{' · '.join(links)}</nav>{''.join(sections)}")


def render_pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        slides = []
        for name in archive.namelist():
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
            if match:
                slides.append((int(match[1]), name))
        blocks = []
        for number, name in sorted(slides):
            root = ET.fromstring(archive.read(name))
            paragraphs = []
            for paragraph in root.findall(f".//{P}sp/{P}txBody/{A}p"):
                text = _text(paragraph, f"{A}t").strip()
                if text:
                    paragraphs.append(f"<p>{html.escape(text)}</p>")
            blocks.append(
                f'<article id="slide-{number}"><h2>Слайд {number}</h2>{"".join(paragraphs)}</article>'
            )
    return _page(path.name, f"<h1>{html.escape(path.name)}</h1>{''.join(blocks)}")


def render_office(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return render_docx(path)
    if suffix == ".xlsx":
        return render_xlsx(path)
    if suffix == ".pptx":
        return render_pptx(path)
    raise ValueError(f"Неподдерживаемый формат: {suffix}")
