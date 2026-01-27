from typing import Optional
from clang import cindex
import re

DEF_DEBUG=True

class SignedTypeFixer:
    def __init__(self, src_file="example.c", compile_args=None, macro_table=None, type_table=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]
        self._type_table = type_table or []

        # TypeTable: [ [alias, actual, related, [file,[lines...]]], ... ]
        self._type_map = {}
        try:
            for row in self._type_table:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    self._type_map[str(row[0]).strip()] = str(row[1]).strip()
        except Exception:
            self._type_map = {}

    def _actual_type_from_typetable(self, type_str: str) -> str:
        if not type_str:
            return ""
        s = " ".join(str(type_str).split())
        token_re = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')

        def repl(m):
            tok = m.group(1)
            return self._type_map.get(tok, tok)

        prev = None
        cur = s
        for _ in range(20):
            if cur == prev:
                break
            prev = cur
            cur = token_re.sub(repl, cur)
            cur = " ".join(cur.split())
        return cur

    def _normalize_actual_type(self, type_str: str) -> str:
        return self._actual_type_from_typetable(type_str) or (type_str or "").strip()

    def _is_integer_type(self, actual_type: str) -> bool:
        s = (actual_type or "")
        s = re.sub(r'\b(const|volatile)\b', '', s)
        s = " ".join(s.split())

        if s in (
            "bool",
            "char", "signed char", "unsigned char",
            "int", "signed", "unsigned", "unsigned int",
            "int8_t", "int16_t", "int32_t", "int64_t",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        ):
            return True

        # ざっくり *_t の整数型も許可
        if s.endswith("_t") and ("int" in s or "uint" in s):
            return True
        return False

    def _is_unsigned(self, actual_type: str) -> bool:
        s = (actual_type or "")
        s = re.sub(r'\b(const|volatile)\b', '', s)
        s = " ".join(s.split())

        if s.startswith("unsigned"):
            return True
        if s in ("uint8_t", "uint16_t", "uint32_t", "uint64_t", "unsigned char"):
            return True
        # char は処理系依存なので unsigned 扱いにしない
        return False

    def _is_integer_literal_token(self, txt: str) -> bool:
        s = (txt or "").strip()
        return bool(re.fullmatch(r'(0[xX][0-9A-Fa-f]+|0[0-7]*|[0-9]+)([uUlL]{0,3})', s))

    def _toggle_unsigned_literal_suffix(self, txt: str, make_unsigned: bool) -> str:
        s = (txt or "").strip()
        m = re.fullmatch(r'(?P<num>0[xX][0-9A-Fa-f]+|0[0-7]*|[0-9]+)(?P<suf>[uUlL]{0,3})', s)
        if not m:
            return s
        num = m.group("num")
        suf = m.group("suf") or ""

        if make_unsigned:
            if "U" in suf.upper():
                return num + suf.replace("u", "U")
            # Uを追加（Lは維持）
            return num + "U" + "".join([c for c in suf if c not in ("u", "U")])
        else:
            # U/u だけ除去（Lは残す）
            return num + "".join([c for c in suf if c not in ("u", "U")])

    def solveSignedTypedConflict(self, analize_result):
        """
        解析結果から符号型不一致を検出し、必要ならキャストを挿入した式文字列を返す。
        Step5: unsigned/signedが一致していても型が異なればキャストする。
        left_type == right_type の場合のみ型変換は行わない。
        """
        def dbg(*args):
            if DEF_DEBUG:
                print("[SignedTypeFixerDBG]", *args)

        # --- 1. eval_datas 取得 ---
        eval_datas = analize_result.get("eval_datas", [])
        if not eval_datas:
            dbg("eval_datas is empty")
            return []

        data = eval_datas[0]
        dbg("STEP1: eval_datas[0]", data)

        left_cursor = data.get("left_val_cursor_head")
        right_cursor = data.get("right_val_cursor_head")
        left_val = data.get("left_val_spelling")
        dbg("left_val(init):", left_val)
        right_val = data.get("right_val_spelling")
        dbg("right_val(init):", right_val)
        op = data.get("operator_spelling")
        spelling = analize_result.get("spelling", "")

        # --- 2. 型取得（typedef名とcanonical型の両方） ---
        def get_types(cursor):
            if cursor is None:
                dbg("get_types: cursor is None")
                return None, None
            try:
                # デバッグ: cursor情報
                try:
                    dbg("get_types: cursor.spelling =", getattr(cursor, "spelling", "<no spelling>"))
                    dbg("get_types: cursor.kind =", getattr(cursor, "kind", "<no kind>"))
                    dbg("get_types: cursor.type.spelling =", getattr(getattr(cursor, "type", None), "spelling", "<no type>"))
                    dbg("get_types: cursor.type.get_canonical().spelling =", getattr(getattr(cursor, "type", None).get_canonical(), "spelling", "<no canonical>") if getattr(cursor, "type", None) else "<no type>")
                except Exception as e:
                    dbg("get_types: debug info error:", e)
                # DECL_REF_EXPRやINTEGER_LITERALなら型を返す
                if hasattr(cursor, "kind"):
                    if cursor.kind.name == "DECL_REF_EXPR" or cursor.kind.name == "INTEGER_LITERAL":
                        type_spelling = getattr(getattr(cursor, "type", None), "spelling", None)
                        canonical_spelling = None
                        try:
                            canonical_spelling = cursor.type.get_canonical().spelling
                        except Exception:
                            canonical_spelling = None
                        dbg("get_types: return type =", type_spelling, "canonical =", canonical_spelling)
                        return type_spelling, canonical_spelling
                # 子ノードを再帰的に探索
                for child in getattr(cursor, "get_children", lambda: [])():
                    t, c = get_types(child)
                    if t or c:
                        return t, c
            except Exception as e:
                dbg("get_types: exception", e)
            return None, None

        left_type, left_type_canon = get_types(left_cursor)
        dbg("left_type(after get_types):", left_type)
        dbg("left_type_canon(after get_types):", left_type_canon)
        right_type, right_type_canon = get_types(right_cursor)
        dbg("right_type(after get_types):", right_type)
        dbg("right_type_canon(after get_types):", right_type_canon)
        dbg("STEP2: left_type", left_type, "left_type_canon", left_type_canon, "right_type", right_type, "right_type_canon", right_type_canon)

        # --- 3. unsigned/signed判定はcanonical型で ---
        def is_primitive_int(t):
            return any(x in t for x in [
                "int", "uint", "int8", "int16", "int32", "int64", "unsigned", "signed"
            ])
        def is_unsigned(t):
            return "unsigned" in t or t.strip().startswith("u")

        left_is_int = is_primitive_int(left_type_canon or "")
        right_is_int = is_primitive_int(right_type_canon or "")
        left_is_unsigned = is_unsigned(left_type_canon or "")
        right_is_unsigned = is_unsigned(right_type_canon or "")
        dbg("STEP3: left_is_int", left_is_int, "right_is_int", right_is_int, "left_is_unsigned", left_is_unsigned, "right_is_unsigned", right_is_unsigned)

        # --- 4. 単行式・定数判定 ---
        def is_numeric_literal(s):
            return bool(re.fullmatch(r'[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?[uUlLfF]*', str(s).strip()))

        left_is_const = is_numeric_literal(left_val)
        dbg("left_is_const(after is_numeric_literal):", left_is_const)
        right_is_const = is_numeric_literal(right_val)
        dbg("right_is_const(after is_numeric_literal):", right_is_const)
        dbg("STEP4: left_is_const", left_is_const, "right_is_const", right_is_const)

        # --- 5. キャスト要否判定（仕様変更） ---
        # 両方int型で型名が一致していればキャスト不要
        if left_is_int and right_is_int and left_type == right_type:
            dbg("STEP5: 型名一致なのでキャスト不要")
            eval_val = f"{left_val} {op} {right_val}"
            dbg("eval_val(no cast):", eval_val)
            result = spelling.replace("@1", eval_val)
            dbg("STEP6: result", result)
            return result

        # 型名が異なればunsigned/signed一致でもキャストする
        if left_is_int and right_is_int:
            dbg("STEP5: 型名不一致なのでキャスト実施")
            # --- 6. キャスト対象決定（typedef名などプリミティブでない型を使う） ---
            which = "right"
            cast_type = left_type  # デフォルトは左辺の型（typedef名などプリミティブでない型）
            if not right_type:
                which = "right"
                cast_type = left_type
            elif not left_type:
                which = "left"
                cast_type = right_type
            elif right_is_const:
                which = "right"
                cast_type = left_type
            elif left_is_const:
                which = "left"
                cast_type = right_type
            elif len(str(left_val)) < len(str(right_val)):
                which = "left"
                cast_type = right_type
            dbg("which(after cast target decision):", which)
            dbg("cast_type(after cast target decision):", cast_type)
            dbg("STEP7: which", which, "cast_type", cast_type)

            # --- 7. キャスト式生成 ---
            def cast_expr(expr, ctype, is_const, make_unsigned):
                if is_const:
                    # 定数の場合はキャストせず、Uサフィックスでunsigned/signedを調整
                    return self._toggle_unsigned_literal_suffix(expr, make_unsigned)
                else:
                    # 変数や式の場合はキャスト
                    if re.match(r'^[\w\d_]+$', str(expr)):
                        return f"({ctype}){expr}"
                    else:
                        return f"({ctype})({expr})"

            # unsigned判定はキャスト型のcanonical型で判定
            make_unsigned = self._is_unsigned(self._normalize_actual_type(cast_type))

            if which == "left":
                left_val = cast_expr(left_val, cast_type, left_is_const, make_unsigned)
                dbg("left_val(after cast):", left_val)
            else:
                right_val = cast_expr(right_val, cast_type, right_is_const, make_unsigned)
                dbg("right_val(after cast):", right_val)
            dbg("STEP8: left_val", left_val, "right_val", right_val)

            eval_val = f"{left_val} {op} {right_val}"
            dbg("eval_val(final):", eval_val)
            result = spelling.replace("@1", eval_val)
            dbg("STEP9: result", result)
            return result

        # int型でない場合はキャスト不要
        dbg("STEP5: どちらかがint系でないのでキャスト不要")
        eval_val = f"{left_val} {op} {right_val}"
        dbg("eval_val(no cast):", eval_val)
        result = spelling.replace("@1", eval_val)
        dbg("STEP6: result", result)
        return result