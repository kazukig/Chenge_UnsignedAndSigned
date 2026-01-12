from clang import cindex
import os


class FunctionTable:
    def __init__(self, tu, srcfile="example.c", preproc_map=None):
        self.tu = tu
        self.src_file = srcfile
        self.preproc_map = preproc_map or {}
        self.data = []

    def make(self):
        """
        関数表を作成して返す（src_file で定義/宣言されている関数のみに限定）。

        返却形式:
          [
            [関数名, 引数の数, [{"name": 引数名, "type": 引数型}, ...], 返り値の型],
            ...
          ]
        """
        if self.tu is None:
            return []

        rows = []
        seen = set()

        def _is_in_srcfile(cur: cindex.Cursor) -> bool:
            """
            cursor.location は「プリプロセス後のファイル/行」になることがあるので、
            preproc_map があれば start.line から元ファイル(orig_file)へ戻して比較する。
            """
            try:
                start = cur.extent.start
                mapped = self.preproc_map.get(start.line) if self.preproc_map else None
                orig_file = mapped[0] if mapped else None

                if not orig_file:
                    orig_file = cur.location.file.name if cur.location and cur.location.file else None

                if not orig_file or not self.src_file:
                    return False

                # ファイル名（basename）で比較
                return os.path.basename(orig_file) == os.path.basename(self.src_file)
            except Exception:
                return False

        def _add_func(cur: cindex.Cursor):
            try:
                if not _is_in_srcfile(cur):
                    return

                name = (cur.spelling or "").strip()
                if not name:
                    return

                # 重複排除
                try:
                    f = cur.location.file.name if cur.location and cur.location.file else ""
                    ln = int(cur.location.line) if cur.location else 0
                except Exception:
                    f, ln = "", 0
                key = (name, f, ln)
                if key in seen:
                    return
                seen.add(key)

                # 返り値型
                try:
                    ret_type = cur.result_type.spelling if getattr(cur, "result_type", None) else ""
                except Exception:
                    ret_type = ""

                # 引数（[{name,type}, ...]）
                args_list = []
                try:
                    args = list(cur.get_arguments())
                except Exception:
                    args = []

                for a in args:
                    try:
                        aname = (a.spelling or "").strip()
                    except Exception:
                        aname = ""
                    try:
                        atype = a.type.spelling if getattr(a, "type", None) else ""
                    except Exception:
                        atype = ""
                    args_list.append({"name": aname, "type": atype})

                argc = len(args_list)
                rows.append([name, argc, args_list, ret_type])
            except Exception:
                return

        def _walk(n: cindex.Cursor):
            try:
                for ch in n.get_children():
                    if ch.kind == cindex.CursorKind.FUNCTION_DECL:
                        _add_func(ch)
                    _walk(ch)
            except Exception:
                return

        _walk(self.tu.cursor)
        self.data = rows
        return self.data

    def getFunctionInfo(self, name: str):
        """
        引数:
          name: 関数名称

        返り値:
          {
            "name": 関数名,
            "argc": 引数の数,
            "args": [{"name": 引数名, "type": 引数型}, ...],
            "ret": 返り値の型
          }
          見つからない場合は [] を返す
        """
        if not name:
            return []
        if not self.data or not isinstance(self.data, list):
            return []

        for row in self.data:
            try:
                # row: [関数名, 引数の数, args_list, 返り値の型]
                if not isinstance(row, (list, tuple)) or len(row) < 4:
                    continue
                if str(row[0]) != name:
                    continue
                return {
                    "name": row[0],
                    "argc": row[1],
                    "args": row[2],
                    "ret": row[3],
                }
            except Exception:
                continue

        return []