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

    def solveSignedTypedConflict(self, run_result):
        """
        引数:
          run_result: {operator,A,B,type,text,kakko} node を root に持つ木構造のリスト

        返り値:
          ボトムアップ処理後の木構造リスト。
          最終的には各要素が leaf: {"type":..., "text":...} になる（= 1つの式に縮約）。

        処理(ボトムアップ):
          (1) 末端node(A,Bがleaf)をワークから探す
          (2) A/Bどちらをキャストするか決める（定数優先、両変数ならB）
          (3) キャスト先型を決める（相手の type 表記）
          (4) キャストを leaf.text に反映し、node を {type,text} に縮約
          (5) 結果リスト(work自体)を更新
        """
        if not run_result or not isinstance(run_result, list):
            return run_result

        import copy
        work = copy.deepcopy(run_result)

        def _is_node(n) -> bool:
            return isinstance(n, dict) and "operator" in n and "A" in n and "B" in n

        def _is_leaf(n) -> bool:
            return isinstance(n, dict) and "operator" not in n and "type" in n and "text" in n

        def _wrap_once(s: str) -> str:
            """
            既に「式全体」が最外の () で1組に包まれているならそのまま。
            そうでなければ ( ... ) を付与する。

            例:
              "(a+b)"        -> そのまま
              "(a+b)+c"      -> "((a+b)+c)"  （先頭'('が途中で閉じる）
              "(b-4)%(2+c)"  -> "((b-4)%(2+c))"（複合）
              "a"            -> "(a)"
            """
            s = (s or "").strip()
            if not s:
                return s

            # まず先頭/末尾が括弧でないなら付与
            if not (s.startswith("(") and s.endswith(")")):
                return f"({s})"

            # 先頭 '(' が末尾 ')' とペアになって「全体」を包んでいるかチェック
            depth = 0
            first_pair_closes_at = None
            for i, ch in enumerate(s):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        first_pair_closes_at = i
                        break

            # パースできない/途中で閉じた → 全体を包む括弧ではないので追加
            if first_pair_closes_at is None:
                return f"({s})"

            # 先頭の '(' が末尾の ')' で閉じている（=最外が全体を包む）なら追加しない
            if first_pair_closes_at == len(s) - 1:
                return s

            # 途中で閉じた（= "(...)+..." や "(... )%( ... )" など複合）→ 追加
            return f"({s})"

        def _needs_wrap_for_cast(txt: str) -> bool:
            s = (txt or "").strip()
            if not s:
                return False
            if s.startswith("(") and s.endswith(")"):
                return False
            return bool(re.search(r'[\s\+\-\*/%<>=&|^]', s))

        def _prec(op: str) -> int:
            if op == "=":
                return 1
            if op in ("||",):
                return 2
            if op in ("&&",):
                return 3
            if op in ("==", "!=", "<=", ">=", "<", ">"):
                return 4
            if op in ("<<", ">>"):
                return 5
            if op in ("+", "-"):
                return 6
            if op in ("*", "/", "%"):
                return 7
            return 10

        def _assoc(op: str) -> str:
            return "right" if op == "=" else "left"

        def _is_associative(op: str) -> bool:
            return op in ("+", "*", "&&", "||")

        def _needs_paren(child, parent_op: str, is_right_child: bool) -> bool:
            if not _is_node(child):
                return False
            cop = (child.get("operator") or "").strip()
            pop = (parent_op or "").strip()
            if not pop:
                return False
            cp, pp = _prec(cop), _prec(pop)
            if cp < pp:
                return True
            if cp > pp:
                return False
            if _assoc(pop) == "right":
                return not is_right_child
            if _assoc(pop) == "left" and pop in ("-", "/", "%", "<<", ">>"):
                return is_right_child
            if _is_associative(pop) and cop == pop:
                return False
            return True

        def _to_expr(n, parent_op: str = "", is_right_child: bool = False) -> str:
            if _is_leaf(n):
                return (n.get("text") or "").strip()
            if not _is_node(n):
                return str(n)

            op = (n.get("operator") or "").strip()
            A = n.get("A")
            B = n.get("B")
            left = _to_expr(A, op, False)
            right = _to_expr(B, op, True)

            if _needs_paren(A, op, False):
                left = _wrap_once(left)
            if _needs_paren(B, op, True):
                right = _wrap_once(right)

            expr = f"{left} {op} {right} "

            if parent_op and _needs_paren(n, parent_op, is_right_child):
                expr = _wrap_once(expr)

            if bool(n.get("kakko")):
                expr = _wrap_once(expr)

            return expr

        # (1) 末端 node を探す（深い側優先）
        def _find_bottom_node(root):
            if not _is_node(root):
                return None
            A = root.get("A")
            B = root.get("B")
            if _is_node(A):
                t = _find_bottom_node(A)
                if t is not None:
                    return t
            if _is_node(B):
                t = _find_bottom_node(B)
                if t is not None:
                    return t
            if _is_leaf(A) and _is_leaf(B):
                return root
            return None

        # (2) キャスト対象の決定
        def _choose_cast_side(node) -> str:
            A = node["A"]
            B = node["B"]
            a_const = self._is_integer_literal_token(A.get("text", ""))
            b_const = self._is_integer_literal_token(B.get("text", ""))
            if a_const and b_const:
                return "B"
            if a_const:
                return "A"
            if b_const:
                return "B"
            return "B"

        # (3) キャスト先型
        def _decide_cast_type(node, cast_side: str) -> str:
            if cast_side == "A":
                return node["B"].get("type", "x")
            return node["A"].get("type", "x")

        def _is_mismatch(a_type: str, b_type: str) -> bool:
            at = self._normalize_actual_type(a_type)
            bt = self._normalize_actual_type(b_type)
            if not self._is_integer_type(at) or not self._is_integer_type(bt):
                return False
            return self._is_unsigned(at) != self._is_unsigned(bt)

        # (4) leaf.text 更新
        def _apply_cast_to_leaf(leaf: dict, target_type: str) -> dict:
            new_leaf = dict(leaf)
            txt = (new_leaf.get("text") or "").strip()

            if self._is_integer_literal_token(txt):
                make_unsigned = self._is_unsigned(self._normalize_actual_type(target_type))
                new_leaf["text"] = self._toggle_unsigned_literal_suffix(txt, make_unsigned)
            else:
                if _needs_wrap_for_cast(txt):
                    new_leaf["text"] = f"({target_type}){_wrap_once(txt)}"
                else:
                    new_leaf["text"] = f"({target_type}){txt}"

            new_leaf["type"] = target_type
            return new_leaf

        # 1要素(root)を完全にleafへ縮約
        def _reduce_one(root):
            guard = 0
            while True:
                guard += 1
                if guard > 100000:
                    break

                if _is_leaf(root):
                    return root
                if not _is_node(root):
                    # 想定外は文字列化してleaf化
                    return {"type": "x", "text": str(root)}

                target = _find_bottom_node(root)
                if target is None:
                    # これ以上分解できないので丸ごと式化してleaf化
                    return {"type": root.get("type", "x"), "text": _to_expr(root)}

                A = target["A"]
                B = target["B"]
                if DEF_DEBUG: print("A = ", A)
                if DEF_DEBUG: print("B = ", B)

                if _is_mismatch(A.get("type"), B.get("type")):
                    side = _choose_cast_side(target)
                    if DEF_DEBUG:  print("side = ", side)
                    cast_type = _decide_cast_type(target, side)
                    if DEF_DEBUG:  print("cast_type = ", cast_type)
                    if side == "A":
                        target["A"] = _apply_cast_to_leaf(A, cast_type)
                    else:
                        target["B"] = _apply_cast_to_leaf(B, cast_type)
                    target["type"] = cast_type
                else:
                    target["type"] = target["A"].get("type", "x")
                if DEF_DEBUG: print("A2 = ", target["A"])
                if DEF_DEBUG: print("B2 = ", target["B"])
                if DEF_DEBUG: print("target type = ", target["type"])

                # nodeをleafへ縮約（ボトムアップを進めるため必須）
                if DEF_DEBUG: print("previous target = ", target)
                target_leaf = {"type": target.get("type", "x"), "text": _to_expr(target)}
                target.clear()
                target.update(target_leaf)

        for i in range(len(work)):
            work[i] = _reduce_one(work[i])

        return work