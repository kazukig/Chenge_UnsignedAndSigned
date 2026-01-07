import os
import re
import sys
import subprocess
import tempfile
from typing import Optional
from clang import cindex

# Module-level constants for regex patterns
_INTEGER_LITERAL_PATTERN = re.compile(r'^(0x[0-9A-Fa-f]+|[0-9]+)([uUlL]*)$')

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

    def _actual_type_from_typetable(self, type_str: str) -> str:
        """
        self._type_table のエントリ [alias, actual, ...] を参照して
        alias -> actual のマッピングを返す。見つからなければ _resolve_type を返す。
        """
        if not type_str:
            return type_str
        try:
            # normalize token (use first token as alias lookup)
            first_tok = re.match(r'\b([A-Za-z_][A-Za-z0-9_]*)\b', type_str)
            key = first_tok.group(1) if first_tok else type_str
            for entry in self._type_table:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    alias = str(entry[0])
                    actual = str(entry[1]) if entry[1] is not None else ""
                    if alias == key:
                        return actual or self._resolve_type(type_str)
        except Exception:
            pass
        return self._resolve_type(type_str)

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

    def _is_integer_type(self, type_str: str) -> bool:
        """整数系の型かどうかを判定する（intN_t / uintN_t / int / short / long / char 等）。"""
        if not type_str:
            return False
        s = self._resolve_type(type_str).lower()
        return bool(re.search(r'\b(?:u?int\d+_t|int|short|long|char|signed char|unsigned char|unsigned int|unsigned)\b', s))

    def _type_bitwidth(self, type_str: str):
        """
        型文字列からビット幅を推定する。推定できなければ None を返す。
        """
        if not type_str:
            return None
        s = self._resolve_type(type_str).lower()
        m = re.search(r'u?int(\d+)_t', s)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
        if re.search(r'\bchar\b', s):
            return 8
        if re.search(r'\bshort\b', s):
            return 16
        if re.search(r'\blong long\b', s) or re.search(r'longlong', s):
            return 64
        if re.search(r'\blong\b', s):
            # assume 64 for LP64 platforms; if unknown, return None could be safer
            return 64
        if re.search(r'\bint\b', s):
            return 32
        return None

    def _extract_cast_type_for_var(self, line: str, varname: str) -> Optional[str]:
        """
        行内で varname の直前にあるキャスト (TYPE)varname を探し、
        見つかれば TYPE の文字列を返す。見つからなければ None を返す。
        """
        try:
            # capture TYPE in "( TYPE ) varname" allowing spaces and simple qualifiers
            pat = re.compile(r'\(\s*([A-Za-z_][A-Za-z0-9_ \t\*]*)\s*\)\s*' + re.escape(varname))
            m = pat.search(line)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
        return None

    def _var_has_cast_of_sign(self, line: str, varname: str, desired_unsigned: bool) -> bool:
        """
        varname に対して既にキャストがあり、そのキャスト型の符号性が
        desired_unsigned と一致していれば True を返す。
        """
        try:
            cast_type = self._extract_cast_type_for_var(line, varname)
            if not cast_type:
                return False
            resolved = self._actual_type_from_typetable(cast_type) or self._resolve_type(cast_type)
            return self._is_unsigned(resolved) == bool(desired_unsigned)
        except Exception:
            return False

    def _literal_has_unsigned_suffix(self, line: str, token: str) -> bool:
        """
        Check if a literal token in the line has an unsigned suffix (U or u).
        """
        try:
            pat = r'(?<![\w_])' + re.escape(token) + r'([uU][lL]*)(?![\w_])'
            return bool(re.search(pat, line))
        except Exception:
            return False

    def solveSignedTypedConflict(self, run_result, line_pair):
        """
        変更:
        - line_pair は [指摘番号, 行番号] のみ（単一）。
        - 戻り値は [指摘番号, 行番号, 成功フラグ, 修正後の行または None]
        - 型比較には self._type_table を参照し、実際の型同士で符号性を判定する。
        - 変換が必要な場合は「相手の型に合わせて」キャストを行う。
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
                A_type = ent.get('A_type', '') or ''
                B_type = ent.get('B_type', '') or ''
                A_name = ent.get('A_name', '') or ''
                B_name = ent.get('B_name', '') or ''

                # 実際の型を型テーブルから取得（無ければ _resolve_type で展開）
                resolved_A = self._resolve_type(A_type)
                resolved_B = self._resolve_type(B_type)
                actual_A = self._actual_type_from_typetable(A_type) or resolved_A
                actual_B = self._actual_type_from_typetable(B_type) or resolved_B

                target_name = None
                new_type = None

                # まず、行内で既に (TYPE)var のようなキャストがある場合はそれを優先して実際の型とする
                if A_name:
                    castA = self._extract_cast_type_for_var(original_line_text, A_name)
                    if castA:
                        actual_castA = self._actual_type_from_typetable(castA) or self._resolve_type(castA)
                        if actual_castA:
                            actual_A = actual_castA
                if B_name:
                    castB = self._extract_cast_type_for_var(original_line_text, B_name)
                    if castB:
                        actual_castB = self._actual_type_from_typetable(castB) or self._resolve_type(castB)
                        if actual_castB:
                            actual_B = actual_castB

                # 符号性の判定は実際の型に対して行う
                try:
                    sigA = self._is_unsigned(actual_A)
                    sigB = self._is_unsigned(actual_B)
                except Exception:
                    sigA = self._is_unsigned(resolved_A)
                    sigB = self._is_unsigned(resolved_B)

                # サイズが異なるだけの組み合わせは変換対象から除外する
                try:
                    if self._is_integer_type(actual_A) and self._is_integer_type(actual_B):
                        wA = self._type_bitwidth(actual_A)
                        wB = self._type_bitwidth(actual_B)
                        if wA is not None and wB is not None and wA != wB:
                            # ビット幅が異なればキャスト対象外（例: uint8_t vs uint32_t）
                            # - 同じ符号性（両方 unsigned または両方 signed）なら除外
                            # - 符号性が異なれば変換対象とする（continue しない）
                            if sigA == sigB:
                                continue
                except Exception:
                    pass

                # もし既に行内キャストで符号合わせがされているなら処理不要
                if B_name and self._extract_cast_type_for_var(original_line_text, B_name):
                    # B に対するキャストがあり、そのキャストの符号性が A に合わせられていればスキップ
                    if self._var_has_cast_of_sign(original_line_text, B_name, sigA):
                        continue
                if A_name and self._extract_cast_type_for_var(original_line_text, A_name):
                    if self._var_has_cast_of_sign(original_line_text, A_name, sigB):
                        continue

                if B_name:
                    if sigA != sigB:
                        # B を A に合わせてキャストする (相手の型に合わせる)
                        target_name = B_name
                        new_type = actual_A if actual_A else resolved_A
                if not target_name and A_name:
                    if sigA != sigB:
                        # A を B に合わせてキャストする
                        target_name = A_name
                        new_type = actual_B if actual_B else resolved_B

                if not target_name or not new_type:
                    continue

                # リテラルの場合は既に U サフィックスがあるか確認して不要ならスキップ
                if self._is_integer_literal_token(target_name):
                    # もし変換先が unsigned で既に U が付いているなら不要
                    if self._is_unsigned(new_type) and self._literal_has_unsigned_suffix(new_line_text, target_name):
                        continue
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

    def _is_integer_literal_token(self, token: str) -> bool:
        """
        Check if token is an integer literal (decimal or hex) with optional suffixes.
        """
        if not token:
            return False
        return bool(_INTEGER_LITERAL_PATTERN.match(token))

    def _build_literal_pattern(self, token_to_match: str) -> str:
        """
        リテラルトークンにマッチする正規表現パターンを構築する。
        トークンとその後のサフィックスをキャプチャグループで捕捉する。
        """
        return r'(?<![\w_])(' + re.escape(token_to_match) + r')([uUlL]*)(?![\w_])'

    def _normalize_suffix(self, suffix: str, make_unsigned: bool) -> str:
        """
        接尾子を正規化する。
        - 複数の L は1つの LL に正規化
        - U は追加または削除
        - C標準に従い、U は L の前に配置 (例: UL, ULL)
        """
        # L の数を数える (1つなら L、2つ以上なら LL)
        l_count = suffix.lower().count('l')
        has_l = 'LL' if l_count >= 2 else ('L' if l_count == 1 else '')
        
        if make_unsigned:
            # unsigned への変換: U を L の前に配置 (C標準の慣例に従う)
            return 'U' + has_l
        else:
            # signed への変換: U を削除、L のみ保持
            return has_l

    def _replace_literal_with_toggled(self, line: str, token: str, new_type: str):
        """
        整数リテラル token を unsigned へ変換する場合は接尾子に U を追加、
        signed へ変換する場合は接尾子の U を除去する。
        例:
          a + 4  --(to unsigned)--> a + 4U
          a + 4U --(to signed)  --> a + 4
        置換が起きた場合は (new_line, True) を返す。
        """
        try:
            # 判定: 目的型が unsigned かどうか
            make_unsigned = self._is_unsigned(new_type)

            # トークンがすでにサフィックスを含んでいる場合は、ベース部分を抽出
            # 例: "30U" -> base="30"、そうでない場合はトークンをそのまま使用
            base_num_match = _INTEGER_LITERAL_PATTERN.match(token)
            token_to_match = base_num_match.group(1) if base_num_match else token
            
            # パターンを構築
            pat = self._build_literal_pattern(token_to_match)

            def repl(m):
                # 両方のブランチで一貫してマッチグループを使用
                lit = m.group(1)
                suffix = m.group(2) or ""
                # 接尾子を正規化
                new_suffix = self._normalize_suffix(suffix, make_unsigned)
                return lit + new_suffix

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
            repl = f'({new_type}){varname}'
            new_line, n = re.subn(pat, repl, line)
            return new_line, n > 0
        except Exception:
            return line, False
