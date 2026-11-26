"""Bounded, session-local tables captured from the graphical interface."""

import json
import re
import sqlite3

from ai_desktop_agent.privacy import redact


class TableMemory:
    def __init__(self):
        self.tables = {}
        self.queries = {}
        self.next_query = 1
        self.foreground = []
        self.last_result = ""

    def remember(self, title, tables, *, document=""):
        for index, rows in enumerate(tables):
            if not isinstance(rows, list) or not 2 <= len(rows) <= 256:
                continue
            header = rows[0]
            if not isinstance(header, list) or not 1 <= len(header) <= 32:
                continue
            if any(not isinstance(row, list) or len(row) != len(header) for row in rows):
                continue
            if any(not isinstance(v, str) or len(v) > 256 for row in rows for v in row):
                continue
            key = (title, document, index)
            if key not in self.tables and len(self.tables) >= 12:
                continue
            self.foreground.append(key)
            data = [[redact(v) for v in row] for row in rows]
            if self.tables.get(key) != data:
                if key in self.tables:
                    self.queries.clear()
                self.tables[key] = data
                self.last_result = ""

    def context(self):
        lines = []
        for i, (key, rows) in enumerate(self.tables.items(), 1):
            title = key[0]
            schema = {f"c{j}": name for j, name in enumerate(rows[0], 1)}
            lines.append(
                f"t{i} {redact(title)[:100]} {len(rows) - 1}行 "
                + json.dumps(schema, ensure_ascii=False)
            )
            if key in self.foreground:
                sample = json.dumps(rows[1:], ensure_ascii=False, separators=(",", ":"))
                if len(sample) <= 4200:
                    lines.append("画面外を含む全行の読み取り完了。" + sample)
                else:
                    lines.append(
                        "画面外を含む全行の読み取り完了。先頭・末尾 "
                        + json.dumps([rows[1], rows[-1]], ensure_ascii=False)[:600]
                    )
        for name, (_, columns, rows) in self.queries.items():
            schema = {f"c{j}": label for j, label in enumerate(columns, 1)}
            lines.append(f"{name} 集計済み {len(rows)}行 " + json.dumps(schema, ensure_ascii=False))
        return "\n".join([*lines, self.last_result]).strip()

    def _select(self, query, require_source):
        source_read = False
        db = sqlite3.connect(":memory:")
        try:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 100000)
            sources = {f"t{i}": rows for i, rows in enumerate(self.tables.values(), 1)}
            for name, (_, header, rows) in self.queries.items():
                sources[name] = [header, *rows]
            for name, rows in sources.items():
                cols = ",".join(f"c{j}" for j in range(1, len(rows[0]) + 1))
                db.execute(f"CREATE TABLE {name} ({cols})")
                placeholders = ",".join("?" for _ in rows[0])
                db.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows[1:])

            def authorize(action, name, arg, database, trigger):
                nonlocal source_read
                if action == sqlite3.SQLITE_READ and name in sources:
                    source_read = True
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and arg in {
                    "sum",
                    "count",
                    "avg",
                    "min",
                    "max",
                    "round",
                    "abs",
                    "coalesce",
                    "ifnull",
                    "nullif",
                    "lower",
                    "upper",
                    "trim",
                    "substr",
                    "row_number",
                    "group_concat",
                    "total",
                    "like",
                }:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            db.set_authorizer(authorize)
            remaining = 500

            def progress():
                nonlocal remaining
                remaining -= 1
                return remaining <= 0

            db.set_progress_handler(progress, 1000)
            cursor = db.execute(query)
            rows = cursor.fetchmany(257)
            if len(rows) > 256 or len(cursor.description) > 32:
                raise ValueError("結果が大きいため列や行を絞ってください")
            if require_source and not source_read:
                raise ValueError(
                    "取得済みの表t番号または集計q番号をFROMで参照してください。値の再入力は不要です"
                )
            return [col[0] for col in cursor.description], [list(row) for row in rows]
        finally:
            db.close()

    def select(self, query):
        return self._select(query, False)[1]

    def query(self, sql):
        columns, rows = self._select(sql, True)
        text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        if len(text) > 128000:
            raise ValueError("集計またはLIMITで結果を小さくしてください")
        name = next(
            (
                k
                for k, v in self.queries.items()
                if v[0] == sql or (v[1] == columns and v[2] == rows)
            ),
            None,
        )
        if name is None:
            if len(self.queries) == 12:
                del self.queries[next(iter(self.queries))]
            name = f"q{self.next_query}"
            self.next_query += 1
        self.queries[name] = (sql, columns, rows)
        if len(text) > 3600:
            text = (
                json.dumps(rows[:3] + rows[-1:], ensure_ascii=False)[:3000]
                + "（先頭3行と末尾。全行は表に保持）"
            )
        self.last_result = name + " 結果（再利用可能）: " + text

    def render(self, sql, separator):
        _, rows = self._select(sql, True)

        def format_value(value):
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return "" if value is None else str(value)

        text = "\n".join(separator.join(format_value(v) for v in row) for row in rows)
        if not text or len(text) > 16000:
            raise ValueError("1〜16000文字の結果を指定してください")
        return text

    def cells(self, sql, start, formulas):
        from ai_desktop_agent.actions.primitives import validate_cells

        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", start)
        if not match:
            raise ValueError("開始セルの番地が無効です")
        first_col = 0
        for letter in match[1]:
            first_col = 26 * first_col + ord(letter) - 64
        _, rows = self._select(sql, True)
        if not rows or len(rows) * len(rows[0]) > 512:
            raise ValueError("1〜512セルの矩形を指定してください")
        if formulas and len(formulas) != len(rows[0]):
            raise ValueError("数式テンプレートは出力と同じ列数で指定してください")
        cells = []
        for r, row in enumerate(rows, int(match[2])):
            for offset, val in enumerate(row):
                formula = formulas[offset] if formulas else ""
                if formula:
                    value = formula.replace("{row}", str(r))
                    if not re.fullmatch(r"=[A-Za-z0-9$()+*/,:. -]+", value):
                        raise ValueError("表の数式に使用できない文字があります")
                else:
                    if isinstance(val, float) and val.is_integer():
                        val = int(val)
                    value = "" if val is None else str(val)
                    if value.startswith("="):
                        raise ValueError("元データに数式形式の値があります")
                col = first_col + offset
                letters = ""
                while col:
                    col, remainder = divmod(col - 1, 26)
                    letters = chr(65 + remainder) + letters
                cells.append({"address": f"{letters}{r}", "value": value})
        for i in range(0, len(cells), 32):
            validate_cells(cells[i : i + 32])
        return cells
