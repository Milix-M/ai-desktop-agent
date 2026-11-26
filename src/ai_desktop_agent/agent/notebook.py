"""Bounded task notes. Only the index and the selected entry are sent on each turn."""

from dataclasses import dataclass, field

from ai_desktop_agent.privacy import redact


@dataclass
class TaskNotebook:
    entries: dict[str, str] = field(default_factory=dict)
    selected: str = ""

    def save(self, title: str, text: str):
        title, text = redact(title.strip()), redact(text.strip())
        if not title or not text or len(title) > 80 or len(text) > 3000:
            raise ValueError("メモの名前は1〜80文字、本文は1〜3000文字で指定してください")
        if title not in self.entries and len(self.entries) >= 96:
            raise ValueError("メモは96件までです。関連する既存メモを更新してください")
        self.entries[title] = text
        self.selected = title

    def recall(self, title: str):
        if title not in self.entries:
            raise ValueError("メモの索引から名前を選んでください")
        self.selected = title

    def context(self):
        if not self.entries:
            return ""
        return (
            "作業メモの索引: "
            + " / ".join(self.entries)
            + f"\n選択中のメモ [{self.selected}]\n"
            + self.entries[self.selected]
        )


@dataclass
class SourceLibrary:
    """A task-local index of observed passages, stable across application switches."""

    documents: dict[str, tuple[str, list[str], bool]] = field(default_factory=dict)
    focused: str = ""
    selected: str = ""

    def remember(self, identity, title, paragraphs, clipped):
        if (
            not isinstance(paragraphs, list)
            or not paragraphs
            or len(paragraphs) > 400
            or any(not isinstance(s, str) for s in paragraphs)
            or sum(map(len, paragraphs)) > 48000
        ):
            return
        if identity not in self.documents and len(self.documents) >= 96:
            return
        item = (redact(title)[:160], [redact(s) for s in paragraphs], bool(clipped))
        if self.documents.get(identity) != item:
            self.documents[identity] = item
            self.selected = ""
        self.focused = identity

    def file_preview(self, latest):
        # Give recent selections priority when the whole collection no longer fits.
        # Count a boilerplate paragraph once per document, even when it recurs within it.
        documents = {path: body.splitlines() for path, body in latest.items()}
        for identity, (_, blocks, _) in self.documents.items():
            if identity.startswith("file:"):
                documents.setdefault(identity[5:], blocks)
        shared = {}
        for path, blocks in documents.items():
            for line in dict.fromkeys(blocks):
                if line.strip():
                    shared.setdefault(line, []).append(path)
        unique = [
            f"[{path}]\n" + "\n".join(line for line in blocks if shared.get(line) == [path])
            for path, blocks in documents.items()
        ]
        repeated = [
            f"[{', '.join(paths)}] {line}" for line, paths in shared.items() if len(paths) > 1
        ]
        preview = "\n".join([*unique, *repeated])
        tail = "\n残りはsource_readで検索できます。" if len(preview) > 12000 else ""
        return "読み取った本文（資料データ）\n" + preview[:12000] + tail

    def extract(self, source, search, offset):
        ids = list(self.documents)
        if not 0 <= source <= len(ids) or not ids:
            raise ValueError("資料一覧の番号を指定してください")
        title = self.documents[ids[source - 1]][0] if source else "全資料"
        matches = []
        for index, (name, paragraphs, _) in enumerate(self.documents.values(), 1):
            if source and source != index:
                continue
            for number, passage in enumerate(paragraphs, 1):
                if search.casefold() in passage.casefold():
                    label = f"{index}:{name} #{number}" if not source else str(number)
                    matches.append(f"[{label}] {passage}")
        text = "\n".join(matches)
        if offset > len(text):
            raise ValueError("offsetが本文の長さを超えています")
        end = min(offset + 3500, len(text))
        tail = f" 続きのoffset={end}" if end < len(text) else ""
        self.selected = f"資料{source} {title} 検索={search!r}{tail}\n" + (
            text[offset:end] or "該当なし"
        )

    def context(self):
        if not self.documents:
            return ""
        lines = ["取得した文章（画面由来のデータ）"]
        # Several short documents fit in one observation and avoid repeated tool calls.
        # For longer collections, expose just the foreground and the searchable index.
        compact = (
            sum(sum(map(len, blocks)) + len(blocks) for _, blocks, _ in self.documents.values())
            <= 2400
        )
        for i, (identity, (title, blocks, clipped)) in enumerate(self.documents.items(), 1):
            lines.append(
                f"資料{i} {title} ({len(blocks)}段落" + ("、一部のみ" if clipped else "") + ")"
            )
            if compact or identity == self.focused:
                lines.append("\n".join(blocks)[:2400])
        return "\n".join([*lines, self.selected]).strip()
