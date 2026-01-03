import os
import re
import sys
import subprocess
import tempfile
from clang import cindex

# 新しいクラス: 署名付き/非署名の衝突を解決するための修正器
class SignedTypeFixer:
    def __init__(self, src_file="example.c", compile_args=None, macro_table=None, type_table=None):
        self.src_file = src_file
        self.compile_args = compile_args or ["-std=c11", "-Iinclude"]
        # マクロ表と型表を private に保持
        self._macro_table = macro_table or []
        self._type_table = type_table or    []

    def _resolve_type(self, type_str: str) -> str:
        """
        typedef テーブルを使って与えられた型記述を再帰的に展開する。
        self._type_table の各要素は [alias, actual, rels, [file, [lines...]]] という形を想定。
        深さ制限を入れて無限ループを防ぐ。
        """
        if not type_str:
            return type_str

        # type_table を辞書化(alias -> actual)
        type_map = {}
        try:
            for entry in self._type_table:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    alias = str(entry[0])
                    actual = str(entry[1]) if entry[1] is not None else ""
                    type_map[alias] = actual
        except Exception:
            return type_str

        token_re = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\b')

        sys.setrecursionlimit(max(1000, sys.getrecursionlimit()))

        def resolve_token(tok: str, seen: set, depth: int) -> str:
            if depth > 50:
                return tok
            if tok in seen:
                return tok
            if tok in type_map and type_map[tok] and type_map[tok] != tok:
                seen.add(tok)
                base = type_map[tok]
                return token_re.sub(lambda m: resolve_token(m.group(1), seen.copy(), depth + 1), base)
            return tok

        try:
            resolved = token_re.sub(lambda m: resolve_token(m.group(1), set(), 0), type_str)
            return " ".join(resolved.split())
        except Exception:
            return type_str

    def solveSignedTypedConflict(self, run_result, line_pair):
        """
        変更:
        - line_pair は [指摘番号, 行番号] のみ（単一）。
        - 戻り値は [指摘番号, 行番号, 成功フラグ, 修正後の行または None]s
        """
        try:
            # 基本バリデーション
            if run_result == -1 or not isinstance(run_result, list):
                return [line_pair[0], line_pair[1], False, None]

            if not isinstance(line_pair, (list, tuple)) or len(line_pair) < 2:
                return [None, None, False, None]
            idx_id, ln = line_pair[0], int(line_pair[1])

            # 元ソースを読み込む
            with open(self.src_file, 'r', encoding='utf-8', errors='ignore') as f:
                original_lines = f.readlines()

            if ln <= 0 or ln > len(original_lines):
                return [idx_id, ln, False, None]

            # ルックアップ用: 行番号 -> エントリのリスト
            line_map = {}
            for e in run_result:
                try:
                    lno = int(e.get('line', -1))
                except Exception:
                    lno = -1
                if lno >= 0:
                    line_map.setdefault(lno, []).append(e)

            entries = line_map.get(ln, [])
            if not entries:
                return [idx_id, ln, False, None]

            # ワーキングコピー
            working_lines = original_lines[:]
            line_idx = ln - 1
            original_line_text = working_lines[line_idx]
            new_line_text = original_line_text
            # 各エントリに対して逐次的に修正
            for ent in entries:
                A_type = ent.get('A_type', '')
                B_type = ent.get('B_type', '')
                A_name = ent.get('A_name', '')
                B_name = ent.get('B_name', '')

                resolved_A = self._resolve_type(A_type)
                resolved_B = self._resolve_type(B_type)

                target_name = None
                new_type = None
                print(target_name)
                if B_name:
                    if self._is_unsigned(resolved_A) != self._is_unsigned(resolved_B):
                        new_type = self._toggle_type(resolved_B)
                        target_name = B_name
                if not target_name and A_name:
                    if self._is_unsigned(resolved_A) != self._is_unsigned(resolved_B):
                        new_type = self._toggle_type(resolved_A)
                        target_name = A_name

                if not target_name or not new_type:
                    continue

                if self._is_integer_literal_token(target_name):
                    new_line_text, did = self._replace_literal_with_toggled(new_line_text, target_name, new_type)
                else:
                    new_line_text, did = self._replace_var_with_cast(new_line_text, target_name, new_type)
            
            # 変更がなければ失敗
            if new_line_text == original_line_text:
                return [idx_id, ln, False, None]

            # 成功（Git commit/push は呼び出し側で行う）
            return [idx_id, ln, True, new_line_text]
        except Exception:
            return [line_pair[0] if isinstance(line_pair, (list, tuple)) and len(line_pair) > 0 else None,
                    line_pair[1] if isinstance(line_pair, (list, tuple)) and len(line_pair) > 1 else None,
                    False, None]

    def _is_unsigned(self, type_str: str) -> bool:
        try:
            if not type_str:
                return False
            s = self._resolve_type(type_str).replace("const", "").replace("volatile", "").strip().lower()
            if not s:
                return False
            if "unsigned" in s.split():
                return True
            if re.match(r'^uint\d+(_t)?\b', s):
                return True
            return False
        except Exception:
            return False

    def _toggle_type(self, type_str: str) -> str:
        """
        unsigned 型を対応する signed 型へ簡易変換する。
        完全網羅ではないが一般的なパターンに対応する。
        """
        try:
            if not type_str:
                return type_str
            s = self._resolve_type(type_str).strip()
            low = s.lower()

            mapping = {
                "uint8_t": "int8_t",
                "uint16_t": "int16_t",
                "uint32_t": "int32_t",
                "uint64_t": "int64_t",
                "unsigned char": "signed char",
                "unsigned short": "short",
                "unsigned long long": "long long",
                "unsigned long": "long",
                "unsigned int": "int",
                "unsigned": "int",
            }
            for k, v in mapping.items():
                if low.startswith(k):
                    # preserve any trailing qualifiers (e.g. " const")
                    return v + s[len(k):]

            m = re.match(r'^(u?int)(\d+)(_t)?', low)
            if m:
                bits = m.group(2)
                return f"int{bits}_t"

            # 最終手段: 'unsigned' を取り除く
            if "unsigned" in s:
                return " ".join([tok for tok in s.split() if tok.lower() != "unsigned"])

            return s
        except Exception:
            return type_str

    def _is_integer_literal_token(self, token: str) -> bool:
        if not token:
            return False
        # 10進、16進、接尾子(u,l) を許容
        return bool(re.match(r'^(0x[0-9A-Fa-f]+|[0-9]+)[uUlL]*$', token))

    def _replace_literal_with_toggled(self, line: str, token: str, new_type: str):
        """
        整数リテラル token を ((new_type)token) に置換する。
        置換が起きた場合は (new_line, True) を返す。
        """
        try:
            pat = r'(?<![\w_])' + re.escape(token) + r'(?![\w_])'
            repl = f'(({new_type}){token})'
            new_line, n = re.subn(pat, repl, line)
            return new_line, n > 0
        except Exception:
            return line, False

    def _replace_var_with_cast(self, line: str, varname: str, new_type: str):
        """
        変数名 varname の出現を ((new_type)varname) に置換する（簡易実装）。
        必要ならより文脈に依存した処理に差し替えてください。
        """
        try:
            pat = r'(?<![\w_])' + re.escape(varname) + r'(?![\w_])'
            repl = f'(({new_type}){varname})'
            new_line, n = re.subn(pat, repl, line)
            return new_line, n > 0
        except Exception:
            return line,
