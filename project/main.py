import os
import re
import sys
import subprocess
import tempfile
import shutil
import json
from clang import cindex
from Git.GitHost import GitHost
from analyzer.CodeAnalyzer import CodeAnalyzer
from fixer.SignedTypeFixer import SignedTypeFixer
from analyzer.MacroTable import MacroTable
from analyzer.TypeTable import TypeTable


class CommitManager:
    """
    main 側のコミット / JSON 出力処理を担当するクラス。
    """
    def __init__(self, repo_path='henge_UnsignedAndSigned', user_name='kazukig', user_email='mannen5656@gmail.com', token=None):
        self.repo_path = repo_path
        self.user_name = user_name
        self.user_email = user_email
        self.token = token

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

    def makeOutputFile(self, input_path: str, output_path: str, res) -> bool:
        """
        入力パスのファイルをコピーし、res に従って該当行を置換して出力パスに書き出す。
        res: [指摘番号, 行番号, 成功フラグ, 修正後の行文字列]
        入力と出力が同一パスなら上書きする。
        成功時 True, 失敗時 False を返す。
        """
        try:
            with open(input_path, 'r', encoding='utf-8', errors='ignore') as rf:
                lines = rf.readlines()
        except Exception:
            return False

        # デフォルトはコピーのみ
        new_lines = list(lines)

        try:
            if isinstance(res, (list, tuple)) and len(res) >= 4:
                try:
                    ln = int(res[1])
                except Exception:
                    ln = None
                ok = bool(res[2])
                new_line_text = res[3] if res[3] is not None else ""
                if ok and ln and 1 <= ln <= len(new_lines):
                    # Ensure newline at end
                    if not new_line_text.endswith("\n"):
                        new_line_text = new_line_text + "\n"
                    new_lines[ln - 1] = new_line_text
        except Exception:
            return False

        # 出力先ディレクトリ作成
        out_dir = os.path.dirname(output_path) or "."
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass

        try:
            with open(output_path, 'w', encoding='utf-8') as wf:
                wf.writelines(new_lines)
            return True
        except Exception:
            return False

    def perform(self, commit_flag: str, result, src_path: str, message=None):
        """
        result: fixer.solveSignedTypedConflict の戻り値想定 [id, line, ok_bool, new_line_text]
        src_path: example.c のパス
        message: commit message に使う（None ならデフォルトを生成）
        """
        flag = commit_flag

        # 成功かつコミットフラグが '1' の場合は Git commit & push
        print(result)
        if flag == "1" and isinstance(result, (list, tuple)) and len(result) >= 4 and result[2]:
            try:
                gh = GitHost(
                    repo_path=self.repo_path,
                    user_name=self.user_name,
                    user_email=self.user_email,
                    token=self.token
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

                commit_msg = None
                if message is not None:
                    # message がリスト等なら文字列化して使う
                    if isinstance(message, (list, tuple, dict)):
                        commit_msg = json.dumps(message, ensure_ascii=False)
                    else:
                        commit_msg = str(message)
                else:
                    commit_msg = f"Toggle signedness at line {result[1]}"

                commit_res = gh.commitAndPush(changes, message=commit_msg)
                return commit_res
            except Exception as e:
                return {"ok": False, "reason": f"git failed: {e}"}

        # それ以外は result.json を作成（既存処理）
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
    src = "../test_kaizen/example.c"
    args = ["-std=c11", "-Iinclude"]

    # 最初にマクロ表と型表を作成する
    #mtab = MacroTable(src_file=src, compile_args=args).make()
    ttab = TypeTable(src_file=src, compile_args=args).make()

    # コミット/JSON 出力は CommitManager に委譲する
    mgr = CommitManager(repo_path='../test_kaizen', user_name='kazukig', user_email='mannen5656@gmail.com', token="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")

    analyzer = CodeAnalyzer(src_file=src, compile_args=args)
    x = analyzer.run()
    
    #指摘表([指摘番号,行番号])
    chlist = [[0,56], [0,66],[0,72]]
    for coords in chlist:
        # SignedTypeFixer にテーブルを渡す
        fixer = SignedTypeFixer(src_file=src, compile_args=args, macro_table=None, type_table=ttab)
        # 例: 指摘番号 0, 行 157 を処理
        res = fixer.solveSignedTypedConflict(x, coords)
        print("修正結果:", res)

        # ここで出力ファイルを生成（入力=出力で上書き）
        wrote = mgr.makeOutputFile(src, src, res)
        print("makeOutputFile wrote:", wrote)

        # ファイル化ができたら perform を呼び出し、res を commit message として渡す
        if wrote:
            op_res = mgr.perform("1", res, src, message=res)
        else:
            op_res = {"ok": False, "reason": "makeOutputFile failed"}

        print("CommitManager result:", op_res)