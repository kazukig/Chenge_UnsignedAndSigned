import os
import re
import sys
import subprocess
import tempfile
from clang import cindex
from Git.GitHost import GitHost
from analyzer.CodeAnalyzer import CodeAnalyzer
from fixer.SignedTypeFixer import SignedTypeFixer

class CommitManager:
    """
    main 側のコミット / JSON 出力処理を担当するクラス。

    実行ルール:
      - コマンドライン第2引数 (sys.argv[2]) が "1" の場合 -> Git に commit & push を行う
      - それ以外 -> 修正結果を result.json として出力する
    """
    def __init__(self, repo_path='henge_UnsignedAndSigned', user_name='kazukig', user_email='mannen5656@gmail.com'):
        self.repo_path = repo_path
        self.user_name = user_name
        self.user_email = user_email

    def _compute_column(self, before_line: str, after_line: str) -> int:
        if before_line is None:
            return 1
        try:
            for i, (a, b) in enumerate(zip(before_line, after_line)):
                if a != b:
                    return i + 1
            # 先頭一致だが長さが違う場合は差分の次の位置
            if len(before_line) != len(after_line):
                return min(len(before_line), len(after_line)) + 1
        except Exception:
            pass
        return 1

    def perform(self, commit_flag: str, result, src_path: str):
        """
        result: fixer.solveSignedTypedConflict の戻り値想定 [id, line, ok_bool, new_line_text]
        src_path: example.c のパス
        """
        flag = commit_flag

        # 成功かつコミットフラグが '1' の場合は Git commit & push
        if flag == "1" and isinstance(result, (list, tuple)) and len(result) >= 4 and result[2]:
            try:
                gh = GitHost(
                    repo_path=self.repo_path,
                    user_name=self.user_name,
                    user_email=self.user_email
                )
                try:
                    with open(src_path, 'r', encoding='utf-8') as rf:
                        content = rf.read()
                except Exception as e:
                    return {"ok": False, "reason": f"read source failed: {e}"}

                changes = [{
                    "path": os.path.basename(src_path),
                    "action": "modify",
                    "content": content
                }]
                commit_res = gh.commitAndPush(changes, message=f"Toggle signedness at line {result[1]}")
                return commit_res
            except Exception as e:
                return {"ok": False, "reason": f"git failed: {e}"}

        # それ以外は result.json を作成
        try:
            file_name = os.path.basename(src_path)
            before_line = ""
            try:
                with open(src_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                ln = int(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else None
                if ln and 1 <= ln <= len(lines):
                    before_line = lines[ln - 1].rstrip('\n')
            except Exception:
                before_line = ""

            after_line = result[3] if isinstance(result, (list, tuple)) and len(result) > 3 else ""
            col = self._compute_column(before_line, after_line)

            out = {
                "修正するファイル名称": file_name,
                "指摘番号": result[0] if isinstance(result, (list, tuple)) and len(result) > 0 else None,
                "指摘位置_行番号": result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else None,
                "指摘位置_列番号": col,
                "変更前のコード": before_line,
                "変更後のコード": after_line
            }
            with open('result.json', 'w', encoding='utf-8') as jf:
                json.dump(out, jf, ensure_ascii=False, indent=2)
            return {"ok": True, "written": "result.json"}
        except Exception as e:
            return {"ok": False, "reason": f"write result.json failed: {e}"}

if __name__ == "__main__":
    src = "../input/example.c"
    args = ["-std=c11", "-Iinclude"]

    # 最初にマクロ表と型表を作成する
    #mtab = MicroTable(src_file=src, compile_args=args).make()
    #ttab = TypeTable(src_file=src, compile_args=args).make()

    analyzer = CodeAnalyzer(src_file=src, compile_args=args)
    x = analyzer.run()
    # SignedTypeFixer にテーブルを渡す
    fixer = SignedTypeFixer(src_file=src, compile_args=args, macro_table=None, type_table=None)
    # 例: 指摘番号 0, 行 157 を処理
    res = fixer.solveSignedTypedConflict(x, [0,160])
    print("修正結果:", res)

    # コミット/JSON 出力は CommitManager に委譲する
    mgr = CommitManager(repo_path='henge_UnsignedAndSigned', user_name='kazukig', user_email='mannen5656@gmail.com')
    op_res = mgr.perform(None, res, src)
    print("CommitManager result:", op_res)