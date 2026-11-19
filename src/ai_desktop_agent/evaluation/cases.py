"""Small, versioned desktop tasks. Expected answers stay outside the guest."""

import io
import re
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from docker.errors import NotFound

SUITE_VERSION = 2
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


@dataclass(frozen=True)
class DesktopCase:
    id: str
    filename: str
    instruction: str
    initial: str
    expected: str
    renamed: str | None = None

    def manifest(self):
        return asdict(self)


CASES = (
    DesktopCase(
        "text-edit",
        "議事録.txt",
        "開いているMousepadで{directory}/議事録.txtを編集し、内容を次の3行にして、最終行の末尾にも改行を入れて保存してください。"
        "\n会議メモ\n担当: 佐藤\n期限: 金曜日\n",
        "会議メモ\n担当: 未定\n",
        "会議メモ\n担当: 佐藤\n期限: 金曜日\n",
    ),
    DesktopCase(
        "file-rename",
        "下書き.txt",
        "開いているファイルマネージャーで{directory}/下書き.txtを共有メモ.txtに変更してください。"
        "内容と、同じフォルダーの変更しない.txtは変えないでください。",
        "次回の打ち合わせは金曜日です。\n",
        "次回の打ち合わせは金曜日です。\n",
        "共有メモ.txt",
    ),
    DesktopCase(
        "spreadsheet-total",
        "売上集計.ods",
        "開いているLibreOffice Calcで{directory}/売上集計.odsを編集してください。"
        "A4に合計、B4にSUM関数でB2からB3の合計を入れて保存してください。既存の値は変えないでください。",
        "",
        "",
    ),
    DesktopCase(
        "text-append",
        "引継ぎ.txt",
        "開いているMousepadで{directory}/引継ぎ.txtの既存2行を保ち、"
        "末尾に「確認者: 高橋」を1行追記してください。最終行も改行して上書き保存してください。",
        "受付番号: 103\n予定: 木曜日\n",
        "受付番号: 103\n予定: 木曜日\n確認者: 高橋\n",
    ),
)


def initial_spreadsheet() -> bytes:
    rows = (
        '<table:table-row><table:table-cell office:value-type="string"><text:p>商品</text:p>'
        '</table:table-cell><table:table-cell office:value-type="string"><text:p>金額</text:p>'
        "</table:table-cell></table:table-row>"
    )
    for title, amount in (("ノート", 1200), ("ペン", 800)):
        rows += (
            '<table:table-row><table:table-cell office:value-type="string">'
            f"<text:p>{title}</text:p></table:table-cell>"
            f'<table:table-cell office:value-type="float" office:value="{amount}">'
            f"<text:p>{amount}</text:p></table:table-cell></table:table-row>"
        )
    namespaces = " ".join(f'xmlns:{name}="{uri}"' for name, uri in NS.items())
    content = (
        f'<office:document-content {namespaces} office:version="1.2"><office:body>'
        f'<office:spreadsheet><table:table table:name="売上">{rows}</table:table>'
        "</office:spreadsheet></office:body></office:document-content>"
    )
    manifest = (
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
        ' manifest:version="1.2"><manifest:file-entry manifest:full-path="/"'
        ' manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("content.xml", content)
        archive.writestr("META-INF/manifest.xml", manifest)
    return output.getvalue()


def read_artifact(container, path: str) -> bytes | None:
    """Read one bounded regular file without extracting guest archive paths onto the host."""
    try:
        chunks, _ = container.get_archive(path)
    except NotFound:
        return None
    data = bytearray()
    for chunk in chunks:
        data.extend(chunk)
        if len(data) > MAX_ARTIFACT_BYTES + 64 * 1024:
            raise ValueError("成果物が検査サイズの上限を超えました")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("成果物は通常ファイルである必要があります")
        if members[0].size > MAX_ARTIFACT_BYTES:
            raise ValueError("成果物が検査サイズの上限を超えました")
        file = archive.extractfile(members[0])
        if file is None:
            raise ValueError("成果物を読み取れません")
        return file.read(MAX_ARTIFACT_BYTES + 1)


def spreadsheet_matches(data: bytes) -> bool:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("content.xml")
        if info.file_size > MAX_ARTIFACT_BYTES:
            return False
        with archive.open(info) as file:
            content = file.read(MAX_ARTIFACT_BYTES + 1)
        if len(content) > MAX_ARTIFACT_BYTES:
            return False
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        return False
    root = ET.fromstring(content)
    table = root.find("office:body/office:spreadsheet/table:table", NS)
    if table is None:
        return False

    def cell(row_index, column_index):
        current_row = 0
        for row in table.findall("table:table-row", NS):
            rows = int(row.get(f"{{{NS['table']}}}number-rows-repeated", "1"))
            if rows <= 0:
                return None
            current_row += rows
            if current_row <= row_index:
                continue
            current_column = 0
            for item in row:
                if item.tag not in (
                    f"{{{NS['table']}}}table-cell",
                    f"{{{NS['table']}}}covered-table-cell",
                ):
                    continue
                columns = int(item.get(f"{{{NS['table']}}}number-columns-repeated", "1"))
                if columns <= 0:
                    return None
                current_column += columns
                if current_column > column_index:
                    return item
            return None
        return None

    def text(row, column):
        item = cell(row, column)
        return (
            ""
            if item is None
            else "\n".join(
                "".join(paragraph.itertext()) for paragraph in item.findall("text:p", NS)
            )
        )

    def value(row, column):
        item = cell(row, column)
        return None if item is None else item.get(f"{{{NS['office']}}}value")

    total = cell(3, 1)
    if total is None:
        return False
    formula = total.get(f"{{{NS['table']}}}formula", "").split(":=", 1)[-1]
    formula = re.sub(r"\s+", "", formula).replace("$", "").upper()
    return (
        text(0, 0) == "商品"
        and text(0, 1) == "金額"
        and text(1, 0) == "ノート"
        and text(2, 0) == "ペン"
        and text(3, 0) == "合計"
        and Decimal(value(1, 1) or "NaN") == 1200
        and Decimal(value(2, 1) or "NaN") == 800
        and Decimal(value(3, 1) or "NaN") == 2000
        and formula in {"SUM([.B2:.B3])", "SUM([.B2];[.B3])"}
    )


def filenames(case: DesktopCase) -> tuple[str, ...]:
    return (case.filename, case.renamed, "変更しない.txt") if case.renamed else (case.filename,)


def check_case(case: DesktopCase, files: dict[str, bytes | None]) -> tuple[bool, str]:
    try:
        data = files.get(case.renamed or case.filename)
        if data is None:
            return False, "保存先にファイルがありません"
        if case.id == "spreadsheet-total":
            passed = spreadsheet_matches(data)
        else:
            passed = data == case.expected.encode("utf-8")
        if case.renamed:
            passed = (
                passed
                and files.get(case.filename) is None
                and files.get("変更しない.txt") == b"keep this file\n"
            )
        return passed, "保存内容が一致しました" if passed else "保存内容が期待した結果と異なります"
    except (
        ValueError,
        KeyError,
        InvalidOperation,
        tarfile.TarError,
        zipfile.BadZipFile,
        ET.ParseError,
        RuntimeError,
    ) as error:
        return False, f"成果物の形式を検査できません: {type(error).__name__}"
